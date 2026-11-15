#!/usr/bin/env python3
"""探一个模型：把"它到底有多大能耐"当场量出来，而不是抄表。

    .venv/Scripts/python.exe scripts/probe_model.py --name qwen-max
    .venv/Scripts/python.exe scripts/probe_model.py --name deepseek --probe output
    .venv/Scripts/python.exe scripts/probe_model.py \
        --base-url https://…/v1 --model qwen-max --api-key-env DASHSCOPE_API_KEY

## 它回答三个问题

  1. 输出上限（`max_tokens` 允许多大）—— 这是我们要的那一格
  2. 思考能力：收不收 `thinking`；`reasoning_effort` 收哪些值
  3. 量不到时如实说，并把 `models.dev` 那张表的值并排摆出来（表 vs 实际）

## 为什么这不算"故意发 400"

判据是「不为了探测故意发一次 400」—— 那条管的是自动路径：
框架在正常跑的时候不许偷偷试。而这里是明确点名的、一次性的诊断，
发起者知道要花一次调用。两件事，不是一条。这条区别写在这里，
是免得下一个人拿那句话把整个功能毙掉。

## 三种"量不到"，必须分开说（判据：读不到 != 0）

  · 上游报错并说了上限，精确值
  · 上游不报错，读不到。不许把"我试到 10000000 它没吭声"
    当成"上限是 10000000" —— 那可能只是它静默夹取了。
  · 没有 key，什么都不发，直接说。

不打印任何 key。退出码：`0` 正常 · `1` 什么都没问到 · `2` 用法/配置问题。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

#: 荒谬到不可能被任何模型接受的值 —— 它的用途就是撞出上游那句报错。
_ABSURD_MAX = 10_000_000

#: 思考阶梯（我们那六档的档名 + 几个上游认的）。判据是收不收，不是长度。
_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

_PROMPT = "只回一个字：好"


# 目标解析：--name 走 config.yaml 的 models: 那张表
def _resolve(args) -> dict:
    """把命令行的三种写法收成一个 `{base_url, model, key, provider}`。"""
    if args.base_url and args.model:
        key = os.environ.get(args.api_key_env or "", "")
        return {"base_url": args.base_url, "model": args.model,
                "key": key, "key_env": args.api_key_env, "provider": "?"}

    if not args.name:
        raise SystemExit("要么给 --name（config.yaml 里的名字），"
                         "要么给 --base-url + --model")

    from bus.core import load_config
    cfg = load_config(Path("config.yaml"))
    entry = (cfg.get("models") or {}).get(args.name)
    if not isinstance(entry, dict):
        have = sorted((cfg.get("models") or {}).keys())
        raise SystemExit("`models:` 里没有 %r。有的是：%s" % (args.name, ", ".join(have)))

    provider = entry.get("provider") or "?"
    key_env = args.api_key_env

    # 环境变量名的权威源是 `config.yaml` 的 `${...}` 占位符 ——
    #   `provider_loader.py:134` 读的是 `model_cfg["api_key"]`，而那个值由
    #   `load_config` 从占位符展开。不能读展开后的 config（未设的变量会
    #   被展开成空串，名字就没了），这里读原始 yaml。
    cfg_env = None
    try:
        import yaml
        raw_cfg = yaml.safe_load(
            (_PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        raw_ak = str((((raw_cfg.get("models") or {}).get(args.name)) or {})
                     .get("api_key") or "")
        m = re.search(r"\$\{([A-Za-z0-9_]+)\}", raw_ak)
        cfg_env = m.group(1) if m else None
    except Exception:                                        # noqa: BLE001
        cfg_env = None

    # manifest 的 `env_keys` 只作后备；两边不一致时把两个都打出来
    mf_env = None
    try:
        import yaml
        mf = _PROJECT_ROOT / "providers" / provider / "manifest.yaml"
        keys = (yaml.safe_load(mf.read_text(encoding="utf-8")) or {}).get("env_keys") or []
        mf_env = keys[0] if keys else None
    except Exception:                                        # noqa: BLE001
        mf_env = None

    if not key_env:
        key_env = cfg_env or mf_env
    warn = ""
    if cfg_env and mf_env and cfg_env != mf_env:
        warn = ("[!] 同一件事两处声明不一致：config.yaml 写 %s，manifest 写 %s "
                "—— 以 config.yaml 为准" % (cfg_env, mf_env))
    return {"base_url": entry.get("base_url"), "model": entry.get("model_name"),
            "key": os.environ.get(key_env or "", ""), "key_env": key_env,
            "cfg_env": cfg_env, "mf_env": mf_env, "warn": warn,
            "provider": provider}


# 从上游报错里抠数字
_NUM = re.compile(r"(\d[\d_,]{3,})")
#: 上游爱把 req id 也塞进报错里（`chatcmpl-32048348-…`）。
#:   那些数不是上限 —— 只按裸数字抓会把 ID 一起端上来。
#:   判据：上限几乎总是长成 `[1, N]` 或紧挨着 `max_tokens`/`limit`/`range`。
_RANGE = re.compile(r"[\[(]\s*\d+\s*,\s*(\d[\d_,]*)\s*[\])]")


def _numbers_in(text: str) -> list:
    """原文里的数 —— 范围式的最可靠，排在最前（其余按出现顺序）。"""
    text = text or ""
    out: list = []
    for m in _RANGE.finditer(text):
        try:
            out.append(int(m.group(1).replace(",", "").replace("_", "")))
        except ValueError:
            pass
    for m in _NUM.finditer(text):
        try:
            n = int(m.group(1).replace(",", "").replace("_", ""))
        except ValueError:
            continue
        # 紧挨着关键词的才算候选（`chatcmpl-12345678` 不是）
        seg = text[max(0, m.start() - 24):m.start()].lower()
        if any(k in seg for k in ("max_tokens", "max output", "limit", "range", "maximum")):
            out.append(n)
    seen, uniq = set(), []
    for n in out:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def _say_unreadable(why: str) -> None:
    print("     => [!] 读不到（%s）" % why)
    print("        表里那个数是渠道声明，不是这个端点报出来的。")
    print("        要它准，只能手填（端配置页那张表就是给这个用的）。")


def probe_output(client, model: str, table: dict) -> bool:
    """探输出上限。一次调用：发个荒谬的 max_tokens，看上游认不认。"""
    print("── ① 输出上限（`max_tokens` 允许多大）──")
    t = table.get("output")
    print("     models.dev 的表值：%s" % (("%d" % t) if t else "（没有 / 为 0 = 未知）"))
    try:
        client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": _PROMPT}],
            max_tokens=_ABSURD_MAX, temperature=0)
    except Exception as exc:                                 # noqa: BLE001
        msg = str(exc)
        print("     发 max_tokens=%d => 上游报错（这正是我们要的）" % _ABSURD_MAX)
        print("     上游原文：%s" % msg[:300].replace("\n", " "))
        nums = _numbers_in(msg)
        if nums:
            print("     原文里的数（**排在最前的是  那种区间式，最可信**）：%s"
                  % ", ".join(str(n) for n in nums))
            return True
        _say_unreadable("报错了，但原文里没有数")
        return False

    print("     发 max_tokens=%d => 上游**没报错**" % _ABSURD_MAX)
    _say_unreadable("上游收了，但那不代表上限有这么高 —— 它可能只是静默夹取")
    return False


def probe_effort(client, model: str, table: dict) -> bool:
    """探思考：收不收 `thinking`；`reasoning_effort` 收哪些值。每个值一次调用。"""
    print()
    print("── ② 思考能力（收不收、收哪些值）──")
    rows = table.get("reasoning_options")
    print("     models.dev 的表值：%s" % (json.dumps(rows, ensure_ascii=False)
                                          if rows else "（没有）"))
    base = {"model": model, "messages": [{"role": "user", "content": _PROMPT}],
            "max_tokens": 256, "temperature": 0}
    ok_any = False

    # 2a. thinking 开关
    try:
        client.chat.completions.create(**base, extra_body={"thinking": {"type": "enabled"}})
        print("     thinking.enabled                ok")
        ok_any = True
    except Exception as exc:                                 # noqa: BLE001
        print("     thinking.enabled                X  %s" % str(exc)[:90])

    # 2b. effort 阶梯（与 thinking 一起发 —— 只发 effort 的那次实验是无效的）
    accepted, rejected = [], []
    for lvl in _EFFORTS:
        try:
            client.chat.completions.create(
                **base, reasoning_effort=lvl,
                extra_body={"thinking": {"type": "enabled"}})
            accepted.append(lvl)
        except Exception:                                    # noqa: BLE001
            rejected.append(lvl)
    print("     reasoning_effort（与 thinking 一起发）")
    print("       收：%s" % (", ".join(accepted) or "（一个都不收）"))
    if rejected:
        print("       拒：%s" % ", ".join(rejected))
    return ok_any or bool(accepted)


def main() -> int:
    ap = argparse.ArgumentParser(description="探一个模型的输出上限与思考能力")
    ap.add_argument("--name", help="config.yaml 的 models: 里的名字，如 qwen-max")
    ap.add_argument("--base-url")
    ap.add_argument("--model")
    ap.add_argument("--api-key-env", help="环境变量名；不给则从 provider manifest 读")
    ap.add_argument("--probe", choices=("all", "output", "effort"), default="all")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env", override=True)

    tgt = _resolve(args)
    print("=" * 66)
    print("探模型：provider=%s  model=%s" % (tgt["provider"], tgt["model"]))
    print("base_url=%s   key=%s" % (tgt["base_url"], "有" if tgt["key"] else "**没有**"))
    if tgt.get("warn"):
        print(tgt["warn"])
    print("[!] 会花真 token。**不打印任何 key**。")
    print("=" * 66)

    if not tgt["key"]:
        print()
        print("★ 没有 key（环境变量 %s 是空的）—— 一个请求都不发。"
              % (tgt["key_env"] or "?"))
        print("  在 `.env` 里补上它，或者自己设好那个环境变量再来。")
        return 2
    if not tgt["base_url"] or not tgt["model"]:
        print("★ base_url / model 缺一个，没法发。")
        return 2

    # 表值（并排摆出来，好对照）
    table = {}
    try:
        wt = _PROJECT_ROOT / "providers" / "window_table.json"
        if wt.exists():
            data = json.loads(wt.read_text(encoding="utf-8"))
            rows = ((data.get("providers") or {}).get(tgt["provider"]) or {})
            # 本机那张表是扁平的：{provider: {model: <int context>}} ——
            #   不是嵌套对象。两种形状都认（将来加了 output 会是 dict）。
            row = rows.get(tgt["model"])
            table = row if isinstance(row, dict) else (
                {"context": row} if isinstance(row, int) else {})
    except Exception:                                        # noqa: BLE001
        pass

    from openai import OpenAI
    client = OpenAI(api_key=tgt["key"], base_url=tgt["base_url"])

    print()
    got = False
    if args.probe in ("all", "output"):
        got = probe_output(client, tgt["model"], table) or got
    if args.probe in ("all", "effort"):
        got = probe_effort(client, tgt["model"], table) or got

    print()
    print("=" * 66)
    print("结论：%s" % ("问到了，见上面" if got else "**什么都没问到** —— 如实说，不猜"))
    print("=" * 66)
    return 0 if got else 1


if __name__ == "__main__":
    sys.exit(main())
