"""域名级出网审批闸的机械读者。

它守的不是"闸门逻辑对不对"，是四件能被踩回去的事：

  1. 接上了没有 —— 消费端
     （`pipeline/stages/agent_loop.py` 认 `pending_egress_approval`）早就写好了，
     而生产侧零生产者（「「我写了这个机制」≠「这条路径上会调它」」）。
     所以本文件读源码，钉住两扇门都调了它。
  2. `deny` 先于 `ask` —— 求值顺序是 deny，然后 ask，最后 allow。
     判据是调用次序：`check_url_egress` 在 `egress_gate` 之前。
  3. 生产端产的东西消费端认不认** —— 把闸门返回的 dict 真的走一遍
     `_tool_result_of` → `_parse_approval_payload`。少了这条，两半各自绿、
     合起来审批永远不出现。
  4. 通配的防钓鱼语义（判据）——
     `*.example.com` 不匹配 `example.com` 本身；其余位置的 `*`
     只吃两个点之间的一段（挡住攻击者能注册的那些域名）。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_egress_gate.py
"""

import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(_PROJECT_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "tests"))

from core import allowlist as AL              # noqa: E402
from core.egress_gate import egress_gate, host_of, is_granted   # noqa: E402

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


# 1. 主机名

def test_host_extraction():
    check("普通 URL", host_of("https://Example.COM/a/b") == "example.com")
    check("带端口", host_of("http://example.com:8080/x") == "example.com")
    check("带凭据也取得到（凭据由地板拒，不归这里管）",
          host_of("https://u:p@example.com/x") == "example.com")
    check("不是 URL / 空串 -> 空串（不抛）", host_of("") == "" and host_of(":::") == "")
    check("★ 取不到主机名**不是**一次放行：`egress_gate('')` 返回 None",
          egress_gate("") is None)


# 2. 通配语义（防钓鱼规定）

def test_wildcard_semantics():
    m = AL.matches
    check("裸 `*` = 全部", m("*", "anything.example.org"))
    check("逐字命中", m("example.com", "example.com"))
    check("逐字**不**命中别的", not m("example.com", "evil.com"))
    check("大小写不敏感", m("Example.COM", "example.com"))

    # 前导 `*.` —— 任意深度子域
    check("★ `*.example.com` 命中一层子域", m("*.example.com", "a.example.com"))
    check("★ `*.example.com` 命中**任意深度**子域",
          m("*.example.com", "a.b.c.example.com"))
    check("★★ `*.example.com` **不**命中基址本身（判据）",
          not m("*.example.com", "example.com"))
    check("★ `*.example.com` 不命中后缀陷阱 `notexample.com`",
          not m("*.example.com", "notexample.com"))
    check("★ `*.example.com` 不命中 `example.com.evil.com`",
          not m("*.example.com", "example.com.evil.com"))

    # 其余位置：`*` 只吃两个点之间的一段
    check("★ `example.*` 命中 `example.com`（同一段）",
          m("example.*", "example.com"))
    check("★★ `example.*` **不**命中 `example.co.uk`（跨了两个点 —— 攻击者可注册）",
          not m("example.*", "example.co.uk"))
    check("★ `web.*.com` 命中 `web.foo.com`", m("web.*.com", "web.foo.com"))
    check("★ `web.*.com` **不**命中 `web.a.b.com`（同样跨点）",
          not m("web.*.com", "web.a.b.com"))

    check("空模式 / 空值 -> False", not m("", "x") and not m("x", ""))
    check("matches_any：空表 -> False（照问）", not AL.matches_any([], "x"))
    check("matches_any：任一命中即可",
          AL.matches_any(["a.com", "*.b.com"], "x.b.com"))


def test_wildcards_do_NOT_leak_into_command_matching():
    """通配只属于域名域 —— 命令那边是整条精确匹配。

    判据：`has_target_grant` 那一侧走的仍是 `s in allowed`（不是 `matches`）。
    """
    src = (_PROJECT_ROOT / "core" / "workspace.py").read_text(encoding="utf-8")
    m = re.search(r"def has_target_grant.*?(?=\n    def )", src, re.S)
    body = m.group(0) if m else ""
    check("★ `has_target_grant` 里**没有**通配匹配器（`matches`）",
          "matches" not in body, body[:200])
    check("★ 它仍然用 `in allowed` 逐字比对", "in allowed" in body)


# 3. 闸门本身

def test_gate_asks_when_the_domain_is_not_on_the_list():
    from _sandbox import sandbox
    with sandbox():
        payload = egress_gate("https://example.com/page", tool_name="web_fetch")
        check("未批过的域名 -> 返回待审批载荷", isinstance(payload, dict), repr(payload))
        if not isinstance(payload, dict):
            return
        check("★ status 是消费端认的那个串",
              payload.get("status") == "pending_egress_approval", payload.get("status"))
        check("★ kind = egress（派发/结算/选项表都按它查）",
              payload.get("kind") == "egress", payload.get("kind"))
        check("★ 被审批物 = **域名**（不是整条 URL、不是工具名）",
              payload.get("subject") == "example.com", payload.get("subject"))
        check("正文里既有域名也有 URL（被审批的文本就是被同意的文本）",
              "example.com" in payload.get("body", "")
              and "https://example.com/page" in payload.get("body", ""))
        check("第一次问 -> 4 项（升级项 escalate_at=2 还不出现）",
              "5." not in payload.get("prompt", ""), payload.get("prompt", "")[:120])


def test_a_url_in_the_user_message_does_not_grant_access():
    """提到不等于授权 —— 这一条盯的是一个被删掉的判据。

    旧实现有第四半：域名以完整地址出现在本轮用户消息里就免审批。
    它被一句「不要访问 https://example.com，这个地址是恶意的」满足 ——
    纯字符串匹配分不出授权、讨论、否定、引用，而那个结论被拿去免审批。

    判据：提到过的域名照样问一次。用户点了同意之后本会话内不再问，
    那是 ①（`has_target_grant`）的活。
    """

    class _WM:
        def __init__(self, text):
            self.turn_user_message = text

        def has_target_grant(self, kind, host):
            return False

    from _sandbox import sandbox

    with sandbox():
        from bus.registry import register_workspace_manager
        try:
            register_workspace_manager(
                _WM("不要访问 https://example.com，这个地址是恶意的。"))
            negated = egress_gate("https://example.com/page")
            check("★ 否定句里的地址仍然要问", isinstance(negated, dict), repr(negated))

            register_workspace_manager(_WM("帮我打开 https://example.com 看看"))
            asked = egress_gate("https://example.com/page")
            check("★ 用户明确要访问也照样问一次（点过之后本会话内不再问）",
                  isinstance(asked, dict), repr(asked))
        finally:
            register_workspace_manager(None)


def test_gate_passes_when_the_domain_is_persistently_allowed():
    from _sandbox import sandbox
    with sandbox():
        AL.add("egress", "example.com")
        check("★ 白名单里逐字有的 -> 放行（None）",
              egress_gate("https://example.com/x") is None)
        check("★ 别的域名照样问",
              egress_gate("https://other.com/x") is not None)
        check("★ 子域**不**被逐字条目顺带放行（要写 `*.example.com` 才行）",
              egress_gate("https://a.example.com/x") is not None)


def test_gate_honours_a_wildcard_entry():
    from _sandbox import sandbox
    with sandbox():
        AL.add("egress", "*.example.com")
        check("★ `*.example.com` 放行子域",
              egress_gate("https://a.b.example.com/x") is None)
        check("★★ 但**不放行**基址本身（通配语义）",
              egress_gate("https://example.com/x") is not None)


def test_gate_is_fail_closed_on_a_broken_allowlist_file():
    from _sandbox import sandbox
    with sandbox():
        p = AL.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ this is not json", encoding="utf-8")
        check("★ 白名单文件坏了 -> **照问**（不是照放）",
              egress_gate("https://example.com/x") is not None)
        check("而 `is_granted` 直接回答 False", is_granted("example.com") is False)


# 4. 生产端 → 消费端：这一条是全部要点

def test_the_producer_payload_is_recognised_by_the_real_consumer():
    """把闸门返回的 dict 真的走一遍 消费端那两个函数。

    少了这条，两半各自绿、合起来审批永远不出现 ——
    那正是"消费端在等生产端"的状态，也是"包一层就检测不到"的坑。
    """
    from _sandbox import sandbox
    from core.agent_loop import _parse_approval_payload, _tool_result_of
    with sandbox():
        payload = egress_gate("https://example.com/x", tool_name="web_fetch")
        check("夹具前提：它确实是个待审批载荷", isinstance(payload, dict))
        if not isinstance(payload, dict):
            return
        content = _tool_result_of(payload, "web_fetch")
        check("★ 走完 `_tool_result_of` 之后仍以 `{` 开头（不然检测器不认）",
              isinstance(content, str) and content.startswith("{"),
              repr(content)[:80])
        data = _parse_approval_payload(content)
        check("★★ 消费端**认出来了**（`_parse_approval_payload` 非 None）",
              data is not None)
        if data is None:
            return
        check("★★ 认出来的正好是出网那一支",
              data.get("status") == "pending_egress_approval", data.get("status"))
        check("★ `kind` 落在派发器认的两个值里",
              data.get("kind") in ("egress", "command"), data.get("kind"))


# 5. 接线：两扇门都调了它，而且地板在前

def _src(rel: str) -> str:
    return (_PROJECT_ROOT / rel).read_text(encoding="utf-8")


def test_every_door_calls_the_gate_and_the_floor_comes_first():
    """这一条就是本文件存在的理由（"写了" ≠ "会调它"）。

    名单是逐条列的，不是扫目录 —— 新加一扇出网的门要往这里补一行，
    否则它接没接闸这件事没人钉。
    """
    for rel, label in (("mcpx/servers/web_search.py", "web_fetch"),):
        src = _src(rel)
        i_floor = src.find("check_url_egress(")
        i_gate = src.find("egress_gate(")
        check("★ %s：接了闸门（有 `egress_gate(` 调用）" % label, i_gate >= 0)
        check("★ %s：地板也在" % label, i_floor >= 0)
        check("★★ %s：**先地板、后闸门**（deny 先于 ask，不许重排）" % label,
              i_floor >= 0 and i_gate >= 0 and i_floor < i_gate,
              "floor@%d gate@%d" % (i_floor, i_gate))


def test_the_web_fetch_gate_is_checked_inside_the_hop_loop():
    """逐跳都要过 —— 只查第一跳等于把围栏装在旁边那扇门上。"""
    src = _src("mcpx/servers/web_search.py")
    m = re.search(r"for _hop in range\(.*?\n(.*?)\n    else:", src, re.S)
    body = m.group(1) if m else ""
    check("★ `egress_gate(` 落在跳转循环**体内**", "egress_gate(" in body,
          "循环体没抓到" if not body else "")
    check("★ 而且排在发请求 `_http_get_once(` 之前",
          body.find("egress_gate(") < body.find("_http_get_once(")
          if ("egress_gate(" in body and "_http_get_once(" in body) else False)


def main() -> int:
    tests = [
        test_host_extraction,
        test_wildcard_semantics,
        test_wildcards_do_NOT_leak_into_command_matching,
        test_gate_asks_when_the_domain_is_not_on_the_list,
        test_gate_passes_when_the_domain_is_persistently_allowed,
        test_gate_honours_a_wildcard_entry,
        test_gate_is_fail_closed_on_a_broken_allowlist_file,
        test_a_url_in_the_user_message_does_not_grant_access,
        test_the_producer_payload_is_recognised_by_the_real_consumer,
        test_every_door_calls_the_gate_and_the_floor_comes_first,
        test_the_web_fetch_gate_is_checked_inside_the_hop_loop,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s 没抛异常（抛了 %s）" % (fn.__name__, type(exc).__name__),
                  False, str(exc))
    print("")
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
