"""正式施工前的前提验证：DeepSeek 的 `thinking` 到底能不能开。

## 为什么要有这个脚本

`providers/openai_compat.py:246,368` 把 `thinking` 写死关闭，注释逐字：

> *"v0.8.0: Disable DeepSeek thinking mode by default. Reasoning chain is expensive and
> requires careful multi-turn echo-back — disable until explicitly needed."*

那根能力轴（"更快 ↔ 更聪明"）的"聪明"那一半就落在这两行上。打不开，那半就是空的。
所以这个脚本要在动手之前回答四个问题 —— 不能猜：

  1. 上游认不认 `thinking: {"type": "enabled"}`（不认是报错还是静默忽略）
  2. 开了之后贵多少 / 慢多少（同一句话，on / off 各一次）
  3. 多轮怎么办 —— assistant 的 `reasoning_content` 要不要回传？
     （注释里 "careful multi-turn echo-back" 指的就是这件事；三种做法各试一次）
  4. 流式那条路有没有 `delta.reasoning_content`（我们现在会丢掉它）
  5. 非法值（例如 `{"type": "bogus"}`），报错还是忽略？
     （对应那条判据：不支持的档要拒，不许静默）

## 它会花真 token、走真 key

所以它不是测试（`tests/` 下没有它），要手动跑：

    cd <仓库根>
    .venv/Scripts/python.exe scripts/check_thinking_mode.py            # 全部实验
    .venv/Scripts/python.exe scripts/check_thinking_mode.py --quick    # 只跑 1+2（最省）
    .venv/Scripts/python.exe scripts/check_thinking_mode.py --effort   # 只跑 effort 阶梯

不打印任何 key（同 `measure_request.py` 的规矩）。退出码：`0` = 可以开，`1` = 开不了。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

#: 一次实验的提示词 —— 故意短（这是探针，不是基准测试）。挑一个"想一下才答得对"的。
_PROMPT = "一个笼子里有鸡和兔共 8 只，脚共 22 只。鸡兔各几只？只回两个数字。"


def _fmt_int(x) -> str:
    return f"{x:,}" if isinstance(x, int) else str(x)


def _usage(resp) -> dict:
    """把上游报的用量抄出来（读不到就是 None，不是 0）。"""
    u = getattr(resp, "usage", None)
    if u is None:
        return {"prompt": None, "completion": None, "hit": None}
    from providers.base import read_cache_hit
    return {
        "prompt": getattr(u, "prompt_tokens", None),
        "completion": getattr(u, "completion_tokens", None),
        "hit": read_cache_hit(u),
    }


def _reasoning_text(msg) -> str:
    """把 reasoning（思考）正文取出来 —— 字段名可能有三处，都要看。"""
    for attr in ("reasoning_content", "reasoning"):
        v = getattr(msg, attr, None)
        if isinstance(v, str) and v.strip():
            return v
    extra = getattr(msg, "model_extra", None) or {}
    for k in ("reasoning_content", "reasoning"):
        v = extra.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _call(client, model, messages, *, thinking: str | None, stream=False, max_tokens=512,
          **extra):
    """发一次。*thinking* 为 None 表示完全不传这个键（对照组）。

    *extra* 原样并进去（实验 6 用它试各种形状；`extra_body` 会被合并而不是覆盖）。
    """
    kwargs = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    if thinking is not None:
        kwargs["extra_body"] = {"thinking": {"type": thinking}}
    for k, v in extra.items():
        if k == "extra_body" and "extra_body" in kwargs:
            kwargs["extra_body"] = {**kwargs["extra_body"], **v}
        else:
            kwargs[k] = v
    if stream:
        kwargs["stream"] = True
    t0 = time.monotonic()
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001 —— 探针要的就是"它怎么炸"
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "elapsed": time.monotonic() - t0}
    return {"ok": True, "resp": resp, "elapsed": time.monotonic() - t0}


def _run_stream(client, model, thinking: str):
    """流式：看分片里有没有 reasoning 增量（我们现在的代码会丢掉它）。"""
    kwargs = {"model": model, "messages": [{"role": "user", "content": _PROMPT}],
              "max_tokens": 512, "temperature": 0.3, "stream": True,
              "extra_body": {"thinking": {"type": thinking}}}
    seen_reasoning = 0
    text = []
    t0 = time.monotonic()
    try:
        for chunk in client.chat.completions.create(**kwargs):
            choice = (getattr(chunk, "choices", None) or [None])[0]
            delta = getattr(choice, "delta", None) if choice else None
            if delta is None:
                continue
            if _reasoning_text(delta):
                seen_reasoning += 1
            c = getattr(delta, "content", None)
            if c:
                text.append(c)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "elapsed": time.monotonic() - t0}
    return {"ok": True, "reasoning_chunks": seen_reasoning,
            "chars": len("".join(text)), "elapsed": time.monotonic() - t0}


def _experiment_effort(client, model, msgs) -> None:
    """只跑这一段（`--effort`）：effort 阶梯，thinking 与 effort 一起发。

    若 `__top__` 分支丢掉 body 的其余字段，`thinking` 就不会发出去
这一段量不出差别（见 `:259` 附近的更正）。

    这一段的判据是「收不收」，不是「长度」：单次读数的长度噪声极大
      （题太浅、采样随机），要拿长度做档位差别得每档多次取中位。
      而"发这个值会不会 400"当场就分得清。
    """
    print("── effort 阶梯（`thinking` 与 `reasoning_effort` **一起**发）──")
    print("   %-16s %-6s %-10s %s" % ("effort", "结果", "思考字符", "输出 tokens"))
    rows: list = [("（不传 effort）", None)]
    for lvl in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
        rows.append((lvl, lvl))
    for label, lvl in rows:
        extra: dict = {"extra_body": {"thinking": {"type": "enabled"}}}
        if lvl is not None:
            extra["reasoning_effort"] = lvl
        r = _call(client, model, msgs, thinking=None, **extra)
        if not r["ok"]:
            print("   %-16s X %s" % (label, r["error"][:70]))
            continue
        reason = _reasoning_text(r["resp"].choices[0].message)
        u = _usage(r["resp"])
        print("   %-16s ok     %-10d %s"
              % (label, len(reason), _fmt_int(u["completion"])))
    print()
    print("   [!] 判据是**收不收**（400 = 发不得）；长度单次不算数，要多次取中位。")
    print()


def main() -> int:
    quick = "--quick" in sys.argv
    effort_only = "--effort" in sys.argv

    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env", override=True)

    from bus.core import load_config

    # **复用 `measure_request.boot_provider`，不抄第二份装载逻辑** ——
    # 抄一份的代价见 `measure_request.py` 里 MCP 那段注（少了两个工具、而那看起来像结论）。
    _SCRIPTS = str(Path(__file__).parent)
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)
    from measure_request import boot_provider

    config = load_config(Path("config.yaml"))
    # 复用生产的 provider 实例：key / base_url / model 三者必须与线上一致
    # （判据：探针的输入源必须先核对生产从哪里读）。
    provider = boot_provider(config)
    client, model = provider._client, provider._model
    print("=" * 68)
    print("DeepSeek thinking 前提验证  (model=%s)" % model)
    print("[!]  这会花真 token。以下**不打印任何 key**。")
    print("=" * 68)
    print()

    msgs = [{"role": "user", "content": _PROMPT}]

    if effort_only:
        _experiment_effort(client, model, msgs)
        return 0
    results = {}

    # 实验 1/2：关 / 开（同一句话）
    for label, thinking in (("关（生产现状）", "disabled"), ("开", "enabled")):
        r = _call(client, model, msgs, thinking=thinking)
        if not r["ok"]:
            print("[%s] X %s" % (label, r["error"]))
            results[label] = r
            continue
        resp = r["resp"]
        msg = resp.choices[0].message
        u = _usage(resp)
        reason = _reasoning_text(msg)
        results[label] = {"ok": True, "usage": u, "reasoning_chars": len(reason),
                          "elapsed": r["elapsed"], "msg": msg}
        print("[%s] ok  %.2fs  输入 %s / 输出 %s / 命中 %s   reasoning %d 字符"
              % (label, r["elapsed"], _fmt_int(u["prompt"]), _fmt_int(u["completion"]),
                 _fmt_int(u["hit"]), len(reason)))
    print()

    on = results.get("开", {})
    if not on.get("ok"):
        print("结论：FAIL **thinking 打不开**（上面有报错）—— 那半根轴是空的。")
        print("      -> 档位仍然可以做，但它不叫 effort：只剩「手段」那半（子 agent 位/并发/…）。")
        return 1

    # 实验 3：非法值（对应"不支持的档要拒"）
    from providers.base import read_cache_hit  # noqa: F401  (已在 _usage 里用过)
    bad = _call(client, model, msgs, thinking="bogus_value_xyz")
    print("[非法值 type=bogus] %s" % ("X 报错了（好：说明它会拒）" if not bad["ok"]
                                      else "ok 居然接受了 -> 上游**静默忽略**未知值"))
    if not bad["ok"]:
        print("     报错原文：%s" % bad["error"][:200])
    print()

    # 实验 4：多轮回传（注释里那句 "careful multi-turn echo-back"）
    msg = on["msg"]
    reason = _reasoning_text(msg)
    content = getattr(msg, "content", "") or ""
    print("── 多轮：assistant 的 reasoning_content 要不要回传 ──")
    variants = [
        ("① 原样带 reasoning_content", {"role": "assistant", "content": content,
                                      "reasoning_content": reason}),
        ("② 剥掉（只留 content）", {"role": "assistant", "content": content}),
        ("③ 完全不带 content 只留 reasoning", {"role": "assistant",
                                          "reasoning_content": reason, "content": ""}),
    ]
    if not reason:
        print("  [!] 开了也没有 reasoning 正文 —— 上游把思考**藏起来了**")
        print("     -> 多轮那条不必试；但那也意味着'思考'对我们是**不可观测**的")
    else:
        for label, am in variants:
            r = _call(client, model, msgs + [am, {"role": "user", "content": "再问一次：鸡几只？"}],
                      thinking="enabled")
            print("  [%s] %s%s" % (label, "ok 接受" if r["ok"] else "X " + r["error"][:120],
                                   "" if r["ok"] else ""))
    print()

    # 实验 5：流式里有没有 reasoning 增量
    if not quick:
        s = _run_stream(client, model, "enabled")
        if s["ok"]:
            print("[流式] ok %.2fs  正文 %d 字符  **带 reasoning 的分片：%d 个**"
                  % (s["elapsed"], s["chars"], s["reasoning_chunks"]))
            if s["reasoning_chunks"] == 0:
                print("      [!] 流式里没有 reasoning 增量 —— 与 sync 那条不一致")
        else:
            print("[流式] X %s" % s["error"][:160])
        print()

    # 实验 6：枚举 / 预算旋钮 / 以及"被接受 ≠ 生效"
    if not quick:
        print("── thinking 的全部形状 ──")
        shapes = [
            ("disabled（生产现状）", {"thinking": {"type": "disabled"}}),
            ("enabled", {"thinking": {"type": "enabled"}}),
            ("adaptive", {"thinking": {"type": "adaptive"}}),
            ("★ enabled + budget_tokens=256",
             {"thinking": {"type": "enabled", "budget_tokens": 256}}),
            ("reasoning_effort=high（顶层 + thinking 开）",
             {"__top__": {"reasoning_effort": "high"},
              "extra_body": {"thinking": {"type": "enabled"}}}),
            ("output_config.effort=high",
             {"output_config": {"effort": "high"}, "thinking": {"type": "enabled"}}),
        ]
        for label, body in shapes:
            kwargs = {}
            if "__top__" in body:
                kwargs.update(body["__top__"])
                # 顶层键之外剩下的进 extra_body —— `__top__` 那一支若把
                #   body 的其余部分整个丢掉，`thinking` 就没发出去
                #那段实验无效（见文件头的更正）。
                rest = {k: v for k, v in body.items() if k != "__top__"}
                if rest:
                    kwargs["extra_body"] = rest
            else:
                kwargs["extra_body"] = body
            r = _call(client, model, msgs, thinking=None, **kwargs)
            if not r["ok"]:
                print("  %-32s X %s" % (label, r["error"][:90]))
                continue
            u = _usage(r["resp"])
            reason = _reasoning_text(r["resp"].choices[0].message)
            print("  %-32s ok 入%s/出%s  思考 %d 字符"
                  % (label, _fmt_int(u["prompt"]), _fmt_int(u["completion"]), len(reason)))
        print()
        print("  [!] 上面那张表里")
        print("     `reasoning_effort=high（顶层）` 那一行**是无效的** —— 它走 `__top__` 分支，")
        print("     而那里 `thinking=None` -> **`thinking` 键根本没发** -> 在思考关着的前提")
        print("     下测思考强度，必然量不出差别。★ 而 `381/347/322` 是**三种不同形状**的")
        print("     读数，本就无可比性（拿它们论证单调性是**类别错误**）。")
        print("     -> **别再说「上游静默忽略 effort」**（`core/tiers.py` 那句判据就是这么来的）。")
        print("     上游官方 SDK 示例与它自家 harness 都是 **`thinking` + `reasoning_effort` 一起发**。")
        print("     -> 单次读数的**长度**仍不是判据（噪声大）；要长度得**多次取中位**。")
        print("        但「**收不收**」当场就能判 —— 那才是这一段要回答的。")
        print()

    # 结论
    off = results.get("关（生产现状）", {})
    if off.get("ok"):
        o, n = off["usage"], on["usage"]
        if o["prompt"] and n["prompt"]:
            print("── 代价 ──")
            print("  输入 %s → %s   输出 %s → %s   耗时 %.2fs → %.2fs"
                  % (_fmt_int(o["prompt"]), _fmt_int(n["prompt"]),
                     _fmt_int(o["completion"]), _fmt_int(n["completion"]),
                     off["elapsed"], on["elapsed"]))
    print()
    print("结论：OK **thinking 打得开** —— 那根轴的'聪明'那半有落点。")
    print("      -> 下一步：把 `providers/openai_compat.py:246,368` 的硬编码改成档位决定，")
    print("        并按上面的多轮结论决定 `reasoning_content` 怎么处置。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
