"""摘 models.dev 的窗口表进仓库（窗口解析的「查表」层）。

## 它做什么

1. 抓 `https://models.dev/api.json`（约 5 MB、200+ 家 provider）
2. 只留我们支持的 16 家（`providers/*/manifest.yaml` 那一批）里
   `limit.context` 有值的模型
3. 落成 `providers/window_table.json`（约 26 KB，200 倍压缩 ——
   只取我们支持的 provider/模型那几十条，别整张塞）

## 为什么是一个脚本、不是一次手工产物

判据同本仓其它生成物：手工摘一次的表格会烂，而且烂得没人知道。
脚本至少把"怎么来的"钉住了：
重跑一次就能看到上游变了什么，而 `window_table.json` 里存着 `fetched` 日期。

它联网（`urllib`，一次 GET）。所以它不在任何测试里被调用 ——
测试读的是仓库里那份 JSON（静态、离线）。要更新表就人来跑一次。

它不判断哪个数对：上游说多少就是多少。矛盾由 `providers/*/manifest.yaml`
与 `config.yaml` 的声明去暴露（那是 `_check_window_claim` 已经在做的事）。

ASCII-only 输出（Windows GBK 控制台）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe scripts/refetch_window_table.py          # 抓 + 落盘
    .venv/Scripts/python.exe scripts/refetch_window_table.py --dry    # 只报差异
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()

SOURCE_URL = "https://models.dev/api.json"

#: 我们的 provider 目录名 → models.dev 的 provider id。
#: 这张映射必须跟着 `providers/` 目录长：目录里多一家而这里没有，
#: 那家就永远查不到表（`tests/test_window_table.py` 拿目录反查这张表）。
PROVIDER_MAP = {
    "anthropic": "anthropic",
    "azure": "azure",
    "deepseek": "deepseek",
    "gemini": "google",
    "grok": "xai",
    "hunyuan": "tencent-tokenhub",
    "lmstudio": "lmstudio",
    "minimax": "minimax",
    "moonshot": "moonshotai",
    "ollama": "ollama-cloud",
    "openai": "openai",
    "openrouter": "openrouter",
    "qwen": "alibaba",
    "siliconflow": "siliconflow",
    "volcengine": "volcengine",
    "zhipu": "zai",
}

OUT_PATH = _PROJECT_ROOT / "providers" / "window_table.json"

#: 价格表是同一份 `api.json` 里的另一个字段
#: （`cost: {input, output, cache_read, cache_write, reasoning}`，单位是
#: USD / 每 100 万 token）。它另落一个文件，理由有两条：
#:   ① 两者读的时机不同 —— 窗口表在每次解析窗口时读，价格只在算钱时读；
#:   ② 两者烂的方式不同 —— 窗口半年不动，价格说变就变
#:分开之后，各自带自己的 `fetched` 日期，谁过期了一眼看得出。
COST_OUT_PATH = _PROJECT_ROOT / "providers" / "cost_table.json"

#: 能力表 —— 同一份 `api.json` 的第三个字段
#: （`modalities: {input: [...], output: [...]}`）。另落一个文件的理由同价格表：
#: 它读的时机不同（能力在准备一轮输入时读，窗口在解析窗口时读），
#: 烂的方式也不同（新模型随时出现，而窗口一年不动），各自的 `fetched` 分得开。
CAP_OUT_PATH = _PROJECT_ROOT / "providers" / "model_capabilities.json"


def excerpt(raw: dict) -> dict:
    """从 `api.json` 的解析结果里摘出 `{我们的 provider: {model_id: 一条}}`。

    纯函数（负对照直接喂自造字典）—— 判据：只有 `limit.context` 是真数的才收，
    缺了就不收（读不到 ≠ 0，不收 = 落回声明/兜底，
    而不是拿一个假的 0 去当窗口）。

    ## 一条长什么样

    ```json
    "deepseek-v4-flash": {"context": 1000000, "output": 393216,
                          "reasoning": {"types": ["toggle", "effort"],
                                        "effort_values": ["low", "high", "max"]}}
    ```

    为什么从裸整数改成嵌套：
    - `output` —— 输出上限。只摘 `context` 时 `max_tokens` 只能拍一个常数
      （曾用 4096，正好等于最老那档），而
      `deepseek-v4-flash` 是 393216（= 那个常数的 96 倍）、`qwen-max` 只有 8192
一刀切会打穿小窗口那家（所以发送前按表夹取）。
    - `reasoning` —— 模型自己声明的思考能力（下游用它做发送前拦：
      一致的做法是「能力声明 + 发送前拦」，而不是"撞 400 再降级"）。

    不收的情况一律留空（不是填 0、不是填 `false`）：`output` 读不到，键都不出现；
    `reasoning_options` 缺，`reasoning` 键不出现。判据同上：读不到 ≠ 0
    （`false` 会被下游读成"这家不支持思考"，而真相是"这家没告诉我"—— 那是两个决定）。
    """
    out: dict = {}
    for ours, mid in PROVIDER_MAP.items():
        models = (raw.get(mid) or {}).get("models") or {}
        rows = {}
        for name, entry in models.items():
            entry = entry or {}
            limit = entry.get("limit") or {}
            ctx = limit.get("context")
            if not (isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0):
                continue
            row: dict = {"context": ctx}
            out_max = limit.get("output")
            if isinstance(out_max, int) and not isinstance(out_max, bool) \
                    and out_max > 0:
                row["output"] = out_max
            # `reasons`（上游那个布尔）与 `types`（可切换的选项）是两件事：
            #   · `reasons: false`，这家明确没有思考能力（`qwen-max`
            #     就是：`reasoning=false` + 无 `reasoning_options`），下游不发那个参数；
            #   · 行根本不在表里，那是"这家没告诉我"，与上面不是一件事
            #     （读不到 ≠ 0），下游维持现状。
            #缺了 `reasons` 这两个就分不开，下游只能二选一，两种都会错一头。
            reasons = entry.get("reasoning")
            ro = entry.get("reasoning_options")
            r: dict = {}
            if isinstance(reasons, bool):
                r["reasons"] = reasons
            if isinstance(ro, list):
                types = [o.get("type") for o in ro
                         if isinstance(o, dict) and isinstance(o.get("type"), str)]
                if types:
                    r["types"] = sorted(set(types))
                vals = [v for o in ro if isinstance(o, dict)
                        and o.get("type") == "effort"
                        for v in (o.get("values") or [])
                        if isinstance(v, str)]
                if vals:
                    r["effort_values"] = vals
            if r:
                row["reasoning"] = r
            rows[name] = row
        if rows:
            out[ours] = dict(sorted(rows.items()))
    return out


#: 价格表里收哪几个字段。 `reasoning` 不收 —— 它是思考档的价，
#: 而我们的 `completion_tokens` 已经把思考算在里面了（收它会重复计费）。
_COST_KEYS = ("input", "output", "cache_read", "cache_write")


def excerpt_costs(raw: dict) -> dict:
    """从同一份 `api.json` 里摘出 `{我们的 provider: {model: {价}}}`。

    判据与 `excerpt` 同一条：读不到就不收（不拿 0 去当"免费"）。
    `input` 与 `output` 两个都要有才收 —— 只有一个的话，
    算出来的钱是一个半真半假的数，而那比"没价"更坏
    （读不到 ≠ 0，而 0 看起来像一个结论：免费）。
    """
    out: dict = {}
    for ours, mid in PROVIDER_MAP.items():
        models = (raw.get(mid) or {}).get("models") or {}
        rows = {}
        for name, entry in models.items():
            cost = (entry or {}).get("cost") or {}
            if not isinstance(cost, dict):
                continue
            if not isinstance(cost.get("input"), (int, float)) or \
                    not isinstance(cost.get("output"), (int, float)):
                continue
            row = {k: cost[k] for k in _COST_KEYS
                   if isinstance(cost.get(k), (int, float))}
            rows[name] = row
        if rows:
            out[ours] = dict(sorted(rows.items()))
    return out


def excerpt_capabilities(raw: dict) -> dict:
    """从同一份 `api.json` 里摘 `modalities` —— `{我们的 provider: {model: 一条}}`。

    ## 判据（与 `excerpt` / `excerpt_costs` 同族）

    * 读不到就不收：模型没有 `modalities` / `input` 不是列表，整条不收
      —— 那表示"上游没告诉我"，与"它只能收文本"不是一件事。
      下游（`core/model_capabilities.lookup`）拿 `None` 时落回兜底。
    * `input` 只留字符串；`output` 有就留。空列表照收：它是一条声明
      （"这个模型什么输入都不收"——罕见，但那是上游的措辞）。
    * 键是我们的目录名（`PROVIDER_MAP` 的另一侧），与窗口表/价格表同一条。
    """
    out: dict = {}
    for ours, mid in PROVIDER_MAP.items():
        models = (raw.get(mid) or {}).get("models") or {}
        rows = {}
        for name, entry in models.items():
            mod = (entry or {}).get("modalities")
            if not isinstance(mod, dict):
                continue
            inp = mod.get("input")
            if not isinstance(inp, list):
                continue
            row = {"input": [m for m in inp if isinstance(m, str)]}
            outp = mod.get("output")
            if isinstance(outp, list):
                row["output"] = [m for m in outp if isinstance(m, str)]
            rows[name] = row
        if rows:
            out[ours] = dict(sorted(rows.items()))
    return out


def main(argv: list) -> int:
    dry = "--dry" in argv
    #: 只重建能力表 —— 用一份局部的、可能更旧的快照补第三张表时，
    #: 不能顺手把窗口/价格两张也覆盖掉（那会静默移动压缩闸门与账单）。
    only_cap = "--only-cap" in argv
    # 两个坑都踩过：
    #   ① 必须带 User-Agent —— 裸 `urllib` 的默认 UA 会被上游 403；
    #   ② 于是留一个 `--from <文件>` 的口：网络那一步可以被别的手段替代
    #      （`curl` 存一份再喂进来），脚本本身只负责"摘"，不负责"必须能联网"。
    src_file = None
    if "--from" in argv:
        src_file = argv[argv.index("--from") + 1]

    if src_file:
        print("[window_table] 从本地文件读：%s" % src_file)
        raw = json.loads(Path(src_file).read_text(encoding="utf-8"))
    else:
        print("[window_table] 抓 %s ..." % SOURCE_URL)
        req = urllib.request.Request(
            SOURCE_URL, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    table = excerpt(raw)
    total = sum(len(v) for v in table.values())
    print("[window_table] 摘出 %d 家 / %d 条" % (len(table), total))
    costs = excerpt_costs(raw)
    ctotal = sum(len(v) for v in costs.values())
    print("[cost_table]   摘出 %d 家 / %d 条有价" % (len(costs), ctotal))
    caps = excerpt_capabilities(raw)
    captotal = sum(len(v) for v in caps.values())
    print("[cap_table]    摘出 %d 家 / %d 条有能力声明" % (len(caps), captotal))

    if dry:
        old = json.loads(OUT_PATH.read_text(encoding="utf-8")) if OUT_PATH.exists() else {}
        for p in sorted(set(old.get("providers", {})) | set(table)):
            a = (old.get("providers", {}).get(p) or {})
            b = (table.get(p) or {})
            if a != b:
                print("  %-12s %d -> %d（+%d / -%d）"
                      % (p, len(a), len(b),
                         len(set(b) - set(a)), len(set(a) - set(b))))
        print("[window_table] --dry：没有写盘")
        return 0

    import datetime
    today = datetime.date.today().isoformat()
    payload = {
        "schema": 2,
        "source": SOURCE_URL,
        "fetched": today,
        "note": ("models.dev 的 limit（context + output）与 reasoning_options 摘录，"
                 "只含我们支持的 provider。重跑 scripts/refetch_window_table.py 更新。"
                 "★ schema 2（2026-10-06）：一条从**裸整数**（= context）改成**嵌套字典**"
                 "—— 裸整数那条读法仍然认（`core/window_estimate.py::_row_field`），"
                 "所以旧表不会读崩。"),
        "providers": table,
    }
    if not only_cap:
        OUT_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n")
        print("[window_table] 已写 %s（%d 字节）"
              % (OUT_PATH.relative_to(_PROJECT_ROOT), OUT_PATH.stat().st_size))

    # 价格表落成另一个文件（理由见 `COST_OUT_PATH` 上面那段）。
    cost_payload = {
        "source": SOURCE_URL,
        "fetched": today,
        "note": ("models.dev 的 cost 摘录 —— **USD / 每 100 万 token**。"
                 "只收 input+output 两个都有的模型（缺了就整条不收："
                 "半个价算出来的钱比没价更坏）。重跑 scripts/refetch_window_table.py 更新。"
                 "[!] 价格是**会长毛的数据** —— `fetched` 决定它有多旧。"),
        "providers": costs,
    }
    if not only_cap:
        COST_OUT_PATH.write_text(
            json.dumps(cost_payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n")
        print("[cost_table]   已写 %s（%d 字节）"
              % (COST_OUT_PATH.relative_to(_PROJECT_ROOT), COST_OUT_PATH.stat().st_size))

    # 能力表（`modalities`）—— 第三张，理由见 `CAP_OUT_PATH`。
    cap_payload = {
        "schema": 1,
        "source": SOURCE_URL if not src_file else "local snapshot: %s" % Path(src_file).name,
        "fetched": today,
        "note": ("models.dev 的 modalities（input / output）摘录，只含我们支持的 provider。"
                 "★ `input` 里有 text/image/audio/video/pdf；缺这条的模型**不收进表**"
                 "（读不到 != 只能收文本）。重跑 scripts/refetch_window_table.py 更新。"
                 "注意：端配置页换模型时，能力从这张表按 (provider, model) 查 —— "
                 "`config.yaml` 的显式声明仍然**赢过**它。"),
        "providers": caps,
    }
    if src_file:
        cap_payload["snapshot"] = Path(src_file).name
    CAP_OUT_PATH.write_text(
        json.dumps(cap_payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    print("[cap_table]    已写 %s（%d 字节）"
          % (CAP_OUT_PATH.relative_to(_PROJECT_ROOT), CAP_OUT_PATH.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
