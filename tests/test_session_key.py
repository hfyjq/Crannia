"""会话键的机械读者。

它钉三件事：

  1. 六个通道的私聊 = 同一条会话 —— 平台不进键；
  2. 群聊各自独立（群聊里"同一个人"不成立）—— type 与群 id 进键；
  3. 推导点只有一处（源级读者）：`core/session_key.py`。
     任何地方再自己拼一次键，这条读者当场红。

空会话（`session=None`），空串：退化调用的既定契约（测试直驱派发器），
别把它当一条私聊会话。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_session_key.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TRIPLE_S = "'''"

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from channels.base import MessageSession, MessageType  # noqa: E402
from core.session_key import (  # noqa: E402
    GROUP_PREFIX, PRIVATE_KEY, group_id_of, is_workspace_private, session_key_for)


def test_group_id_of_is_the_inverse_of_the_key():
    """拆键那半必须与拼键那半严格互逆。

    为什么要单立一条："这条会话是不是一个群"从今天起有第二个读者
    （工具表那一格 —— 群会话才发 `ask_member`）。拆错一位的症状是
    "那个工具时有时无"，而它不报错（只是工具表少一个名字）。

    判据成对：群键能拆回原样的群 id · 非群键一律拆出空串
    （`workspace:private` 尤其危险 —— 拆出 `"private"` 就得了一个"看着像群 id"
    的东西，而它会去 `groups.get("private")` 查一次）。
    """
    for sid in ("oc_111", "duck", "g1a2b3c4d"):
        key = session_key_for(_sess("feishu", "group", sid))
        assert group_id_of(key) == sid, (key, group_id_of(key), sid)
    assert group_id_of(PRIVATE_KEY) == "", group_id_of(PRIVATE_KEY)
    assert group_id_of("") == "" and group_id_of("feishu:private:oc_1") == ""
    #: 前缀本身就是一处定义的（`session_key_for` 用它拼、`group_id_of` 用它拆）
    assert session_key_for(_sess("feishu", "group", "x")).startswith(GROUP_PREFIX)
    #: 边界：没有 id 的群键（退化调用）拆不出 id，别把它当成一个群
    assert group_id_of("workspace:group") == ""


def _sess(platform, mtype, sid):
    return MessageSession(platform, MessageType(mtype), sid)


# ----------------------------------------------------------------------
# 1. 键的形状
# ----------------------------------------------------------------------

def test_private_channels_share_one_key():
    """`feishu` / `wechat` / `telegram` 的私聊是同一条会话。"""
    keys = {
        session_key_for(_sess("feishu", "private", "ou_abc")),
        session_key_for(_sess("wechat", "private", "wxid_1")),
        session_key_for(_sess("telegram", "private", "12345")),
        session_key_for(_sess("web", "private", "web:cli:default")),
    }
    assert keys == {PRIVATE_KEY}, (
        f"私聊没有共用一条会话：{keys} —— #2 的全部内容就是「它们是一条」")
    assert is_workspace_private(PRIVATE_KEY)


def test_groups_stay_separate():
    """群聊各自独立（type 与群 id 进键）。"""
    g1 = session_key_for(_sess("feishu", "group", "oc_111"))
    g2 = session_key_for(_sess("wechat", "group", "G2"))
    assert g1 != g2 and g1.startswith("workspace:group:")
    assert g1 != PRIVATE_KEY and g2 != PRIVATE_KEY
    # 同平台同群，同键（稳定）
    assert session_key_for(_sess("feishu", "group", "oc_111")) == g1


def test_duck_typing_and_degenerate_cases():
    class _S:                     # 只有两个字段的鸭子对象
        message_type = "group"    # 裸字符串也认
        session_id = "duck"
    assert session_key_for(_S()) == "workspace:group:duck"

    class _NoSid:
        message_type = "group"
        session_id = ""
    assert session_key_for(_NoSid()) == "workspace:group"

    assert session_key_for(None) == "", (
        "空会话必须是空串（退化契约）—— 别把它当一条私聊会话，"
        "那会凭空造出 workspace:private")


# ----------------------------------------------------------------------
# 2. 推导点只有一处（源级读者）
# ----------------------------------------------------------------------

_SCANNED_FILES = (
    "core/pipeline/scheduler.py",
    "core/pipeline/stages/pre_process.py",
    "core/pipeline/stages/post_process.py",
    "core/pipeline/stages/agent_loop.py",
    "web/server.py",
    "channels/cli_channel/handler.py",
)

#: 旧形状：`str(ctx.session)` / `str(event.session)` / `str(evt.session)`……
_STR_SESSION = re.compile(r"str\(\s*[a-z_]*\.session\s*\)")
#: 旧形状之二：`str(getattr(ctx, "session", ""))`
_GETATTR_SESSION = re.compile(r"str\(\s*getattr\([^)]*,\s*[\"']session[\"']")


def test_derivation_lives_in_exactly_one_place():
    """源级读者。两处有意的例外，都写在下面：
      · 文档字符串要剥掉 —— 描述旧形状的 docstring 会让检查器自己打自己；
      · `scheduler._origin_of` 那行含 `"platform"` —— 它读的是平台
        （web/im 分流），不是推会话键。
    """
    offenders = []
    for rel in _SCANNED_FILES:
        p = _PROJECT_ROOT / rel
        src = p.read_text(encoding="utf-8", errors="replace")
        body = re.sub('""" .*? """'.replace(" ", ""), "", src, flags=re.S)
        body = re.sub(TRIPLE_S + ".*?" + TRIPLE_S, "", body, flags=re.S)
        for i, line in enumerate(body.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _STR_SESSION.search(line):
                offenders.append("%s:%d" % (rel, i))
            elif _GETATTR_SESSION.search(line) and "platform" not in line:
                offenders.append("%s:%d" % (rel, i))
    assert not offenders, (
        "这些地方又自己推会话键了（必须走 core/session_key.py）：%r\n"
        "  `session_key_for(...)` 或各阶段的 `session_key_of_ctx(ctx)`" % (offenders,))


def test_stages_use_the_ctx_helper():
    """三个阶段必须用 `session_key_of_ctx`（它认路由改派，`str()` 不认）。"""
    for rel in ("core/pipeline/stages/pre_process.py",
                "core/pipeline/stages/post_process.py",
                "core/pipeline/stages/agent_loop.py"):
        src = (_PROJECT_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        assert "session_key_of_ctx" in src, f"{rel} 没接 session_key_of_ctx"


# ----------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------

def _main():
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except AssertionError as exc:
            failed += 1
            print("FAIL  " + name + ": " + str(exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
