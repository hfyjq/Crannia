"""审批载荷的锁。

这一层最怕的不是崩，是"悄悄答错"：用户按 2，送上去的却是别的 key。
所以断言集中在两处 —— 载荷解析与序号到 key 的映射。
（面板那一层由 `test_approval_ui.py` 钉着。）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crannia_tui import approval as A

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [ok] {label}")
    else:
        _FAIL += 1
        _FAILED.append(label)
        print(f"  [!!] {label}" + (f"  —— {detail}" if detail else ""))


#: 一份与后端 `build_payload` 同形的载荷。
PAYLOAD = {
    "request_id": "req-42",
    "kind": "command",
    "ask_index": 1,
    "body": "Crannia 想执行 rm -rf build/",
    "multi": False,
    "options": [
        {"key": "1", "label": "同意", "description": "", "is_escalation": False},
        {"key": "2", "label": "同意，本会话不再询问", "description": "", "is_escalation": True},
        {"key": "3", "label": "拒绝", "description": "", "is_escalation": False},
    ],
    "default_key": "1",
    "expect": "",
}


def test_from_payload() -> None:
    print("\n解析：与后端载荷一一对应")
    p = A.Prompt.from_payload(PAYLOAD)
    check("能解析", p is not None)
    assert p is not None
    check("request_id 保留", p.request_id == "req-42")
    check("body 保留", "rm -rf" in p.body)
    check("三个选项", len(p.options) == 3, str(len(p.options)))
    check("label 保留", p.options[1].label == "同意，本会话不再询问")
    check("默认项", p.default_key == "1")
    check("不是要文本", not p.wants_text)


def test_from_payload_rejects_garbage() -> None:
    print("\n解析：坏载荷给 None，不抛")
    check("不是 dict", A.Prompt.from_payload(None) is None)
    check("空 dict", A.Prompt.from_payload({}) is None)
    check("缺 request_id", A.Prompt.from_payload({"kind": "x"}) is None)
    check("request_id 非字符串", A.Prompt.from_payload({"request_id": 12}) is None)


def test_options_are_skipped_when_broken() -> None:
    print("\n解析：坏选项单独跳过，不影响整体")
    p = A.Prompt.from_payload({
        "request_id": "r", "options": [{"key": "1", "label": "好的"}, {}, "x", {"key": ""}],
    })
    check("只留合法的那一个", p is not None and len(p.options) == 1, str(p.options if p else None))


def test_number_mapping() -> None:
    print("\n序号 -> key：用户看的是 1/2/3，key 由后端定")
    p = A.Prompt.from_payload(PAYLOAD)
    assert p is not None
    check("1 -> 第一个 key", p.key_for_number("1") == "1")
    check("3 -> 第三个 key", p.key_for_number("3") == "3")
    check("0 越界", p.key_for_number("0") is None)
    check("4 越界", p.key_for_number("4") is None)
    check("非数字不猜", p.key_for_number("好") is None)


def test_number_mapping_with_non_numeric_keys() -> None:
    print("\n序号 -> key：key 不是数字时也不许混")
    p = A.Prompt.from_payload({
        "request_id": "r",
        "options": [{"key": "allow", "label": "同意"},
                    {"key": "deny", "label": "拒绝"}],
        "default_key": "allow",
    })
    assert p is not None
    check("1 -> allow", p.key_for_number("1") == "allow")
    check("2 -> deny", p.key_for_number("2") == "deny")
    check("3 越界", p.key_for_number("3") is None)


def test_wants_text() -> None:
    print("\n中间态：expect=text 时不要选项")
    p = A.Prompt.from_payload({"request_id": "r", "expect": "text"})
    check("要文本", p is not None and p.wants_text)
    # 后端在中间态不给选项表；即使给了，判据也只看 expect。
    p2 = A.Prompt.from_payload({"request_id": "r", "expect": "text",
                                "options": [{"key": "1", "label": "x"}]})
    check("有选项也仍要文本", p2 is not None and p2.wants_text)
    p3 = A.Prompt.from_payload({"request_id": "r"})
    check("一个选项都没有时当作要文本", p3 is not None and p3.wants_text)


def main() -> int:
    test_from_payload()
    test_from_payload_rejects_garbage()
    test_options_are_skipped_when_broken()
    test_number_mapping()
    test_number_mapping_with_non_numeric_keys()
    test_wants_text()

    print("\n" + "=" * 54)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  -", f)
    print(f"{_PASS + _FAIL} checks: {_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
