"""查历史的机械读者。

判据：「独立、零风险、只读 `short_term.db`」。
这一批钉住：

  · 窗口显式（`turns`）且诚实：窗口外还有更早的 · 摘要为界 ·
    列 8 条要报总数 —— 三种"到边"各有各的话；
  · `没有匹配` 是扫完的结论（带扫了几轮的读数），不是"搜不到"；
  · 摘要行可搜：剥掉 `<historical_context>` 包裹、标成「摘要」；
  · 只搜当前会话（别的会话的行搜不到）；
  · 接线如实：manager 没登记 / 会话号为空 / turns 坏 / 超上限 —— 报"不可用"，
    不许伪装成"没搜到"；
  · 接线读者：注册 + 常驻 + 权限档 read + 纯文本名单 + 隔离分类
    （后两个是手动登记：名字不落读家族，反查读者不会替它报警）。

全程临时 `short_term.db` + 假 manager 注册（跑真 handler）；`_session()` 与
`test_file_tools` 同形。`_lab()` 退出时把注册的 manager 换回原值。

ASCII-only 输出（Windows 控制台默认 GBK）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_history_search.py
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
for _p in (str(_PROJECT_ROOT), str(_PROJECT_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.history_tools import history_search  # noqa: E402

SID = "history:private:probe"


@contextlib.contextmanager
def _session(session_id=SID):
    from core import request_snapshot

    request_snapshot.set_current(request_snapshot.capture(
        [], [], "", model="probe", session_id=session_id))
    try:
        yield
    finally:
        request_snapshot.clear_current()


@contextlib.contextmanager
def _lab():
    """临时 short_term.db + 假 manager 注册（真 handler 走全链路）。"""
    import bus.registry as R
    from core.memory.short_term import ShortTermMemory

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        st = ShortTermMemory(str(Path(td) / "st.db"))
        # 夹具要摆出生产的形状 —— 通道键是绑过的。
        #    那条映射只有 `session_bindings` 一张表，
        #    而 `history_search` 要的是存储键（它读的是消息行）。
        #    不绑的话它会查到空 —— 那句回执会说"拿不到当前会话号"，
        #    看起来像"历史真的没了"。
        st.bind_session(SID, SID)

        class _M:
            pass

        m = _M()
        m.short_term = st
        saved = R.get_memory_manager()
        R.register_memory_manager(m)
        try:
            yield st
        finally:
            R.register_memory_manager(saved)


def _pair(st, u, a, sid=SID):
    st.save_messages(sid, [{"role": "user", "content": u},
                           {"role": "assistant", "content": a}], [0, 0])


# ----------------------------------------------------------------------
# 1. 找得到 + 窗口读数
# ----------------------------------------------------------------------

def test_finds_and_reports_window():
    with _lab() as st, _session():
        _pair(st, "早先的话", "早先的答")
        _pair(st, "新问题：咖啡机坏了", "那要修")
        out = history_search("咖啡")
        assert "命中 1 条" in out, out
        assert "在最近 2 轮" in out, out
        assert "窗口内第 3 条 · 用户" in out, out
        assert "咖啡机" in out, out


def test_window_boundary_and_earlier_hint():
    with _lab() as st, _session():
        _pair(st, "第一轮 秘密代号 apple", "记下了")
        for i in range(2, 7):
            _pair(st, f"第{i}轮闲聊", "嗯")

        out = history_search("apple", turns=1)
        assert "没有匹配" in out, out
        assert "还有更早的 5 轮" in out, "窗口外有料必须说出来"

        out2 = history_search("apple", turns=6)
        assert "命中 1 条" in out2 and "apple" in out2, out2
        assert "还有更早的" not in out2, "搜全了就不该再提示外面还有"


# ----------------------------------------------------------------------
# 2. 摘要为界（压缩把原件删了 —— 如实说）
# ----------------------------------------------------------------------

def test_summary_is_searchable_and_boundary_is_stated():
    with _lab() as st, _session():
        # 摘要 + 保留轮的形状（压缩追加这两样，不整表重写）
        st.replace_messages(SID, [
            {"role": "user", "kind": "history_summary",
             "content": "<historical_context>\n摘要里提到苹果派配方\n</historical_context>"},
            {"role": "user", "content": "最近说的话"},
            {"role": "assistant", "content": "好"},
        ])
        out = history_search("苹果")
        assert "命中 1 条" in out and "· 摘要]" in out, out
        assert "苹果派" in out, out
        assert "<historical_context>" not in out, "包裹没剥掉（噪声进 snippet）"
        # 这句话与旧形状相反：压缩若做 DELETE + INSERT 整表，
        #    原件就真的不在库里了。现在压缩只追加，必须说相反的话，
        #    否则工具在撒谎。
        assert "原文都还在库里" in out, (
 "压缩不再删原话 —— 那句「原件不在库里」必须改成相反的")

        # 摘要行不算一轮 —— 窗口读数是 1 轮，不是 2 轮
        assert "在最近 1 轮" in out, out


def test_w168_signpost_is_not_part_of_the_summary_text():
    """摘要在包裹外面带一句路标 —— 它不是摘要正文。

    两处共用一个剥包裹的函数（`compressor.strip_summary_wrapper`）。若改成
    "把两个标签 `replace` 掉、其余原样"，路标会被算成摘要正文，
    搜「历史库」会把每条摘要都搜出来（而那句路标正是每个摘要都有的）。
    """
    from core.memory.compressor import SUMMARY_POINTER

    with _lab() as st, _session():
        st.replace_messages(SID, [
            {"role": "user", "kind": "history_summary",
             "content": "<historical_context>\n摘要里提到苹果派配方\n"
                        "</historical_context>\n\n" + SUMMARY_POINTER},
            {"role": "user", "content": "最近说的话"},
            {"role": "assistant", "content": "好"},
        ])
        # 摘要正文照样搜得到
        assert "苹果派" in history_search("苹果")
        # 而路标那句话不算摘要内容 —— 它只在包裹外面
        out = history_search("历史库", turns=50)
        assert "没有匹配" in out, (
            "包裹外面的路标被当成摘要正文了（两处没用同一个剥包裹的函数）")


def test_w168_compressed_away_turns_are_still_searchable():
    """判据 ①：压缩把轮移出上下文之后，`history_search` 仍搜得到原文。

    若把 `_persist_compaction` 换回整表重写，这条当场红
    （那时原件真的被删了）。
    """
    with _lab() as st, _session():
        st.save_message(SID, {"role": "user", "content": "早年埋的暗号 火龙果"})
        st.save_message(SID, {"role": "assistant", "content": "记下了"})
        _pair(st, "最近说的话", "好")
        rows = st.load_message_rows(SID)[0]
        kept_id = rows[-2]["id"]              # 保留轮的第一行
        st.append_compaction(
            SID,
            {"role": "user", "kind": "history_summary",
             "content": "<historical_context>\n早年在聊水果\n</historical_context>"},
            kept_from_id=kept_id,
        )

        out = history_search("火龙果", turns=50)
        assert "火龙果" in out and "没有匹配" not in out, out
        assert "用户" in out, "命中的应当是那一句原话（不是摘要）"

        # 对照臂：窗口开大之后，更早的那些轮也不该被吞掉
        assert "还有更早的" not in out, out


# ----------------------------------------------------------------------
# 3. "0 个" 的两种含义 · 只搜当前会话
# ----------------------------------------------------------------------

def test_zero_means_searched_not_unreachable():
    with _lab() as st, _session():
        _pair(st, "甲甲甲", "好好好")
        out = history_search("一定不存在的词xyz")
        assert "搜完了，没有匹配" in out, out
        assert "2 条消息" in out, "结论要带扫了多少（报数）"
        assert "还有更早的" not in out, "没有更早的就不许瞎提示"
        assert "不可用" not in out, "不许把'扫完没有'说成'查不了'"


def test_only_current_session():
    with _lab() as st, _session():
        _pair(st, "别的会话里的机密菠萝", "好", sid="other:session")
        _pair(st, "本会话的话", "好")
        out = history_search("菠萝")
        assert "没有匹配" in out, "搜到别的会话去了"
        assert "机密菠萝" not in out


# ----------------------------------------------------------------------
# 4. 接线如实（坏消息不许伪装成"没搜到"）
# ----------------------------------------------------------------------

def test_wiring_truthfulness():
    import bus.registry as R

    with _lab() as st, _session():
        assert "整数" in history_search("x", turns="abc")

        out = history_search("x", turns=999)
        assert "上限" in out, out

    # manager 未登记
    saved = R.get_memory_manager()
    R.register_memory_manager(None)
    try:
        with _session():
            out = history_search("x")
            assert "不可用" in out and "没接线" in out.replace("没有接线", "没接线"), out
    finally:
        R.register_memory_manager(saved)

    # 会话号缺失（快照为空）
    with _lab(), _session(""):
        assert "拿不到当前会话号" in history_search("x")


def test_empty_history_says_so():
    with _lab(), _session():
        out = history_search("任何词")
        assert "没有历史" in out, out


# ----------------------------------------------------------------------
# 5. 列 8 条 · 报总数（报数不报字符）
# ----------------------------------------------------------------------

def test_listed_cap_reports_total_count():
    with _lab() as st, _session():
        for i in range(12):
            _pair(st, f"第{i}条都提到葡萄", "好")
        out = history_search("葡萄")
        assert "命中 12 条" in out, out
        assert "只列最近 8 条" in out, out
        assert out.count("窗口内第") == 8, "列出来的条数不对"


# ----------------------------------------------------------------------
# 6. 接线读者：注册 / 常驻 / 权限 / 纯文本 / 隔离分类
# ----------------------------------------------------------------------

def test_registration_and_classification():
    from measure_request import boot_registry

    import bus.registry as R
    import core.agent_loop as AL
    from core.file_tools import PLAIN_TEXT_TOOLS, as_text

    boot_registry()
    assert "history_search" in R.list_tools()
    assert "history_search" in R._CORE_TOOLS, "不常驻 —— 模型不知道它存在就没法用"
    assert R.get_tool_permission("history_search") == "read"
    assert "history_search" in PLAIN_TEXT_TOOLS
    assert as_text("纯文本", "history_search") == "纯文本"
    assert AL._isolation_decision_for("history_search") is False, (
        "被隔离了 —— 判据：本会话自己的对话，不是第三人文本（手动登记的理由见名单）")


# ----------------------------------------------------------------------

def _main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
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
            print("ERROR  " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
