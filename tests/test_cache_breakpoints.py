"""显式缓存断点的形状约束（默认关闭，测的是接线能力）。

钉五组判据：
  1. 消息级 vs 内容块级形状不同；
  2. 标记在块上而不是消息对象上（内容块级那一路）；
  3. 不改入参；
  4. 上限与关闭开关生效；
  5. system 标记单独提供，形状按 cache_format 分。

ASCII-only 输出（GBK 控制台）。

用法：
    .venv/Scripts/python.exe tests/test_cache_breakpoints.py
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.cache_breakpoints import (  # noqa: E402
    place_breakpoints, shape_of, system_breakpoint,
)

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(desc: str, ok: bool, note: str = "") -> None:
    global _PASS, _FAIL
    print("  [%s] %s%s" % ("ok" if ok else "!!", desc,
                           ("  -- " + note) if note and not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
        _FAILED.append(desc)


def _history():
    return [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "calling", "tool_calls": [
            {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "old result"},
        {"role": "user", "content": "another"},
        {"role": "tool", "tool_call_id": "c2", "content": "newest result"},
    ]


def test_markers_fit_real_wire_shapes():
    """用真实的 Anthropic 内容块翻译器验证标记有地方挂。"""
    from providers.anthropic.client import _to_anthropic_content
    raw = [{"type": "text", "text": "result"},
           {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
    blocks = _to_anthropic_content(raw)
    check("real wire content is a block list", isinstance(blocks, list) and blocks)
    marked = place_breakpoints(
        [{"role": "tool", "content": raw}],
        cache_format="alibaba", max_breakpoints=4)[0]
    check("marker survives on a real-shaped block list",
          "cache_control" in marked["content"][-1], str(marked["content"][-1]))
    check("block shapes are untouched on the internal side",
          [b["type"] for b in marked["content"]] == [b["type"] for b in raw])
    check("wire translation still produces its own shapes",
          [b["type"] for b in _to_anthropic_content(
              marked["content"])] == ["text", "image"])


def main() -> int:
    print("=" * 62)
    print("Cache breakpoints: message vs block shape / purity / off switch")
    print("=" * 62)

    print("\n-- shape dispatch --")
    check("anthropic is message-level", shape_of("anthropic") == "message")
    check("alibaba is block-level", shape_of("alibaba") == "block")
    check("other formats have no explicit shape", shape_of("openai_native") == "none")
    check("system marker follows the same dispatch",
          system_breakpoint("anthropic") == {"type": "ephemeral"} and
          system_breakpoint("openai_native") is None)

    print("\n-- message level --")
    msgs = _history()
    out = place_breakpoints(msgs, cache_format="anthropic", max_breakpoints=4)
    check("marker sits on the last tool-result message",
          out[-1].get("cache_control") == {"type": "ephemeral"}, str(out[-1]))
    check("no marker on the older tool result",
          "cache_control" not in out[2], str(out[2]))
    check("input messages untouched",
          all("cache_control" not in m for m in msgs))

    print("\n-- block level --")
    msgs2 = _history()
    block_out = place_breakpoints(msgs2, cache_format="alibaba", max_breakpoints=4)
    last = block_out[-1]
    check("string content is lifted to a block list",
          isinstance(last["content"], list), str(last["content"]))
    check("marker is on the block, not the message",
          "cache_control" not in last and
          last["content"][-1]["cache_control"] == {"type": "ephemeral"},
          str(last))
    check("input untouched at block level",
          all("cache_control" not in m for m in msgs2))

    print("\n-- off switch and bounds --")
    for fmt in ("anthropic", "alibaba"):
        check("%s with zero breakpoints is a no-op" % fmt,
              place_breakpoints(_history(), cache_format=fmt,
                                max_breakpoints=0) == _history())
    check("unknown format is a no-op",
          place_breakpoints(_history(), cache_format="openai_native",
                            max_breakpoints=4) == _history())
    check("no tool result means no marker anywhere",
          not any("cache_control" in m for m in place_breakpoints(
              [{"role": "user", "content": "hi"}],
              cache_format="anthropic", max_breakpoints=4)))
    check("marker is a fresh dict every call",
          system_breakpoint("anthropic") is not system_breakpoint("anthropic"))

    print("\n-- real wire shapes --")
    test_markers_fit_real_wire_shapes()

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
