#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""思考回传探针：造一段「assistant 缺 reasoning_content」的历史，发一次真请求。

    .venv/Scripts/python.exe scripts/probe_reasoning_passback.py
    .venv/Scripts/python.exe scripts/probe_reasoning_passback.py --rounds 12

两条臂，同一段历史：

    有补齐 —— `providers/openai_compat.py::_fill_reasoning_passback`（今天的行为）
    无补齐 —— 把那一层临时摘掉（负对照）

## 判据怎么读

上游只在部分请求上校验（与它的上下文缓存状态相关），所以负对照被拒不算意外，
负对照没被拒也不说明补齐没用：它说明这一次没复现。要的是"补齐那一臂从被拒
变成不被拒"，而复现不出来时只能照实记 —— 别把"没复现"写成"修好了"。

## 副作用

它会真的调一次上游（有 token 成本），而 `TokenCalibrator` 会顺手把这两次读数
记进 `data/runtime/calibration.json`。探针自己会在结束时把那个文件还原 ——
两次人工构造的短请求不该进用户的校准账。

ASCII-only 输出（Windows 控制台默认 GBK）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_CAL = _ROOT / "data" / "runtime" / "calibration.json"
_CAL_BAK = _CAL.with_suffix(".json.probe-bak")

_TOOLS = [{"type": "function", "function": {
    "name": "get_time", "description": "current time",
    "parameters": {"type": "object", "properties": {}}}}]


def _history(rounds: int) -> list:
    """N 轮「带 tool_calls、没有 reasoning_content」的历史。"""
    msgs: list = [{"role": "user", "content": "what time is it?"}]
    for i in range(rounds):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_%d" % i, "type": "function",
            "function": {"name": "get_time", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": "call_%d" % i,
                     "content": "12:0%d" % (i % 10)})
        msgs.append({"role": "user", "content": "go on"})
    return msgs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="reasoning passback probe")
    ap.add_argument("--rounds", type=int, default=8,
                    help="历史里有几轮缺 reasoning 的 assistant（默认 8）")
    args = ap.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env", override=True)

    from bus.core import load_config
    from bus.loaders.provider_loader import load_provider
    from bus.registry import get_provider
    from core.tiers import ThinkingSpec
    from providers import openai_compat as OC

    bak = None
    if _CAL.exists():
        bak = _CAL_BAK
        shutil.copyfile(_CAL, bak)

    config = load_config(str(_ROOT / "config.yaml"))
    name = str((config.get("model") or {}).get("provider") or "deepseek")
    load_provider(_ROOT / "providers" / name, config)
    provider = get_provider(name)
    print("provider = %s  model = %s" % (name, getattr(provider, "_model", "?")))

    spec = ThinkingSpec(enabled=True, effort="low")
    system = "You are a probe. Answer in one short sentence."

    def run(label: str, rounds: int) -> bool:
        try:
            resp = provider.chat(_history(rounds), tools=_TOOLS, system=system,
                                 thinking=spec)
        except Exception as exc:                             # noqa: BLE001
            print("[!!] %s rejected: %s: %s"
                  % (label, type(exc).__name__, str(exc)[:300]))
            return False
        print("[ok] %s: content=%r reasoning_chars=%d"
              % (label, (resp.content or "")[:40],
                 len(resp.reasoning_content or "")))
        return True

    try:
        print("\n-- 1. with the passback fill (%d rounds) --" % args.rounds)
        fixed_ok = run("fill", args.rounds)

        print("\n-- 2. negative control: fill removed, same history --")
        orig = OC.OpenAICompatProvider._fill_reasoning_passback
        OC.OpenAICompatProvider._fill_reasoning_passback = staticmethod(
            lambda messages, **kw: messages)
        try:
            raw_ok = run("no-fill", args.rounds)
        finally:
            OC.OpenAICompatProvider._fill_reasoning_passback = orig
    finally:
        if bak is not None:
            shutil.copyfile(bak, _CAL)
            bak.unlink(missing_ok=True)
            print("\n[..] calibration.json restored (probe readings discarded)")

    print("\nresult: fill=%s  no-fill=%s"
          % ("ok" if fixed_ok else "rejected",
             "ok (not reproduced this time)" if raw_ok else "rejected (control holds)"))
    return 0 if fixed_ok else 1


if __name__ == "__main__":
    sys.exit(main())
