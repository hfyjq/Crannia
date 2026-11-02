"""ICE 边界助手：消息表变换 / 工具参数还原 / 转义陷阱。

这一层是把 :mod:`core.ice` 接到本仓数据结构上的唯一接口。它自己不判定任何事，
但它有两个地方会静默错，所以两个都有专门的用例：

  还原必须在 JSON 解析之后做。 `arguments` 是序列化过的字符串，
     句柄里的 `【】`(U+3010/U+3011) 会被 `ensure_ascii=True` 写成 `\\u3010` /
     `\\u3011` —— 在原始 JSON 串里那个精确形态根本不存在（解析出来才有）。
文本层替换会静默不生效，而症状看起来像"保护没开"。
     连带一条：判"有没有句柄"的快路不能用 `HANDLE_RE.search(原始串)`
     —— 上面那条正是它失效的原因。用 `"PII:" in raw`（纯 ASCII，两种写法都在）。

  审批载荷不碰。它是框架↔模型的协议消息，
     一个"文件名里恰好有 18 位连号"的删除请求会让审批面板上出现句柄。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ice                                                # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


ID_OK = "11010519491231002X"


def _guard(**kw):
    return ice.ICEGuard(store=ice.SessionStore(), **kw)


def _issue(text, sid="s", guard=None):
    """跑一遍伪名化，返回 (上模型的那一份, 句柄列表, guard)。"""
    g = guard or _guard()
    out, hs = g.pseudonymize(text, sid)
    return out, hs, g


# 一、消息表变换 —— 只覆盖「读来的」

def test_only_tool_messages_are_pseudonymized():
    """读来的（role=tool）换；用户自己打的不换。"""
    g = _guard()
    msgs = [
        {"role": "user", "content": "我的身份证是 " + ID_OK},
        {"role": "assistant", "content": "好的"},
        {"role": "tool", "tool_call_id": "t1", "content": "表里写着 " + ID_OK},
    ]
    out = ice.pseudonymize_messages(msgs, "s", guard=g)
    check("user message untouched", out[0]["content"] == msgs[0]["content"])
    check("assistant message untouched", out[1]["content"] == msgs[1]["content"])
    check("tool message replaced", ID_OK not in out[2]["content"])
    check("and a handle took its place", ice.HANDLE_RE.search(out[2]["content"]))
    check("extra fields survive verbatim", out[2]["tool_call_id"] == "t1")


def test_messages_transform_returns_a_copy():
    g = _guard()
    msgs = [{"role": "tool", "tool_call_id": "t1", "content": "ID " + ID_OK}]
    out = ice.pseudonymize_messages(msgs, "s", guard=g)
    check("original message table is untouched", msgs[0]["content"] == "ID " + ID_OK)
    check("and the copy is a different object", out[0] is not msgs[0])


def test_non_string_and_empty_contents_are_left_alone():
    g = _guard()
    msgs = [
        {"role": "tool", "tool_call_id": "t1", "content": ""},
        {"role": "tool", "tool_call_id": "t2", "content": None},
        {"role": "tool", "tool_call_id": "t3", "content": ["a", "b"]},
    ]
    out = ice.pseudonymize_messages(msgs, "s", guard=g)
    check("empty string survives", out[0]["content"] == "")
    check("None survives", out[1]["content"] is None)
    check("non-str content survives", out[2]["content"] == ["a", "b"])


def test_protocol_payloads_are_never_touched():
    """审批载荷是协议，不是内容 —— 碰它会让审批面板上出现句柄。"""
    g = _guard()
    payload = json.dumps({
        "status": "pending_delete_approval",
        "file_paths": ["/tmp/%s.docx" % ID_OK],
        "prompt": "请确认删除",
    }, ensure_ascii=False)
    msgs = [{"role": "tool", "tool_call_id": "t1", "content": payload}]
    out = ice.pseudonymize_messages(msgs, "s", guard=g)
    check("approval payload passes through byte-for-byte", out[0]["content"] == payload)


def test_switch_off_means_no_pseudonymization():
    g = _guard(enabled=False)
    msgs = [{"role": "tool", "tool_call_id": "t1", "content": "ID " + ID_OK}]
    out = ice.pseudonymize_messages(msgs, "s", guard=g)
    check("off: tool content untouched", out[0]["content"] == "ID " + ID_OK)


def test_pseudonymize_block_covers_framework_blocks():
    """记忆索引 / 会话笔记 / 历史检索 —— 这些块进来。"""
    g = _guard()
    _out, hs, g = _issue("ID " + ID_OK, guard=g)
    blk = ice.pseudonymize_block("笔记里记着 ID " + ID_OK, "s", guard=g)
    check("framework block is protected", ID_OK not in blk)
    check("and it reuses the same handle", hs[0].text in blk)
    check("empty block stays empty",
          ice.pseudonymize_block("", "s", guard=g) == "")


# 二、还原 —— 必须在 JSON 解析之后

def test_tool_args_are_restored_deeply():
    store = ice.SessionStore()
    g = ice.ICEGuard(store=store)
    _, hs = g.pseudonymize("ID " + ID_OK, "s")
    handle = hs[0].text
    args = {"path": "a/" + handle + ".txt",
            "rows": ["x", handle],
            "meta": {"id": handle, "n": 7}}
    got = ice.restore_tool_args(args, "s", guard=g)
    check("top-level string restored", got["path"] == "a/" + ID_OK + ".txt")
    check("inside a list restored", got["rows"][1] == ID_OK)
    check("inside a nested dict restored", got["meta"]["id"] == ID_OK)
    check("non-string leaves untouched", got["meta"]["n"] == 7)
    check("input dict is not mutated", args["path"] == "a/" + handle + ".txt")


def test_restore_value_passes_non_strings_through():
    g = _guard()
    for v in (1, 1.5, True, None):
        check("passthrough %r" % (v,), ice.restore_value(v, "s", guard=g) == v)


def test_restore_is_a_noop_without_handles():
    g = _guard()
    plain = "no handles here at all"
    check("plain text untouched", ice.restore_value(plain, "s", guard=g) is plain)


def test_restore_json_arguments_returns_none_when_nothing_matched():
    g = _guard()
    raw = json.dumps({"path": "/tmp/a.docx"})
    check("no handle -> None (leave the raw string alone)",
          ice.restore_json_arguments(raw, "s", guard=g) is None)
    check("bad json -> None", ice.restore_json_arguments("{not json", "s", guard=g) is None)
    check("empty -> None", ice.restore_json_arguments("", "s", guard=g) is None)
    check("non-dict json -> None", ice.restore_json_arguments("[1,2]", "s", guard=g) is None)


def test_escaped_json_form_is_resolved_by_parsing_not_by_text():
    """这条是整层存在理由的实证。

    `json.dumps(..., ensure_ascii=True)` 会把句柄里的 `【】` 写成 `\\u3010` / `\\u3011`
    —— 在原始 JSON 串里，`【PII:1】` 这个精确形态根本不存在。
    所以文本层替换会静默不生效，而先解析再还原能拿到真值。
    """
    g = _guard()
    _, hs = g.pseudonymize("ID " + ID_OK, "s")
    handle = hs[0].text

    raw = json.dumps({"id": handle}, ensure_ascii=True)
    # 别对整串 `.lower()` —— 那会把 `PII` 一起降成 `pii`，于是探针永远不命中。
    #    json 的十六进制本来就是小写（`【`），不需要动。
    check("the raw json really carries the escape form",
          ("\\u3010PII:%d\\u3011" % hs[0].index) in raw, raw)
    check("and NOT the plain form (that is the whole point)", handle not in raw)

    naive = ice.restore_value(raw, "s", guard=g)
    check("NC: text-level restore does NOT resolve it", ID_OK not in naive)

    fixed = ice.restore_json_arguments(raw, "s", guard=g)
    check("json-level restore DOES resolve it", fixed is not None)
    check("and the true value is back",
          fixed is not None and json.loads(fixed)["id"] == ID_OK)

    # 另一种写法（ensure_ascii=False）—— 文本层也能行，但只有解析那条两种写法都对
    raw2 = json.dumps({"id": handle}, ensure_ascii=False)
    check("the plain form is present here", handle in raw2)
    fixed2 = ice.restore_json_arguments(raw2, "s", guard=g)
    check("unescaped form also resolves",
          fixed2 is not None and json.loads(fixed2)["id"] == ID_OK)


def test_fast_path_does_not_use_the_exact_regex():
    """从上一那条推出来的连带判据：快路不能用 `HANDLE_RE.search(原始串)`。"""
    g = _guard()
    _, hs = g.pseudonymize("ID " + ID_OK, "s")
    raw = json.dumps({"id": hs[0].text}, ensure_ascii=True)
    check("HANDLE_RE misses the escaped raw string",
          ice.HANDLE_RE.search(raw) is None)
    check("but the fast path (`PII:` in raw) still lets it through",
          "PII:" in raw)


def test_full_boundary_roundtrip():
    """端到端：工具结果换句柄 → 模型搬句柄 → 参数还原 → 历史存真值。"""
    store = ice.SessionStore()
    g = ice.ICEGuard(store=store)
    tool_msg = {"role": "tool", "tool_call_id": "t1",
                "content": "请把那位的身份证填进表：" + ID_OK}
    sent = ice.pseudonymize_messages([tool_msg], "s", guard=g)
    handle = ice.HANDLE_RE.search(sent[0]["content"]).group(0)

    # 模型照着句柄回一条 tool_call（JSON 经序列化，`【】` 在这里变成转义形态）
    raw_args = json.dumps({"id": handle, "row": 1}, ensure_ascii=True)

    # ① 派发：解析后还原，handler 拿到真值
    dispatched = ice.restore_tool_args(json.loads(raw_args), "s", guard=g)
    check("dispatch sees the true value", dispatched["id"] == ID_OK)
    check("and non-target keys survive", dispatched["row"] == 1)

    # ② 进历史：还原后重新序列化，落盘是真值（不变量②）
    hist = ice.restore_json_arguments(raw_args, "s", guard=g)
    check("history entry carries the true value",
          hist is not None and json.loads(hist)["id"] == ID_OK)
    check("history entry carries no handle",
          hist is not None and ice.HANDLE_RE.search(hist) is None)


def test_boundary_restore_ignores_the_switch():
    store = ice.SessionStore()
    on = ice.ICEGuard(store=store)
    _, hs = on.pseudonymize("ID " + ID_OK, "s")
    off = ice.ICEGuard(enabled=False, store=store)
    got = ice.restore_tool_args({"id": hs[0].text}, "s", guard=off)
    check("OFF: tool args still restored", got["id"] == ID_OK)


def main() -> int:
    print("=" * 62)
    print("ICE boundary: message transform / arg restore / escape trap")
    print("=" * 62)
    test_only_tool_messages_are_pseudonymized()
    test_messages_transform_returns_a_copy()
    test_non_string_and_empty_contents_are_left_alone()
    test_protocol_payloads_are_never_touched()
    test_switch_off_means_no_pseudonymization()
    test_pseudonymize_block_covers_framework_blocks()
    test_tool_args_are_restored_deeply()
    test_restore_value_passes_non_strings_through()
    test_restore_is_a_noop_without_handles()
    test_restore_json_arguments_returns_none_when_nothing_matched()
    test_escaped_json_form_is_resolved_by_parsing_not_by_text()
    test_fast_path_does_not_use_the_exact_regex()
    test_full_boundary_roundtrip()
    test_boundary_restore_ignores_the_switch()

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
