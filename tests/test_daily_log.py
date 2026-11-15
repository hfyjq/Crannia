"""日报（daily log）的机械读者。

设计判据：**「每轮 append · `/clear` 不清 · 保留 90 天 · 框架直接写，不走 LLM」**。
这些测试钉住其中的每条硬边：

  · 一天一个文件、表头只写一次、一轮一段（时间 + 会话 + 两侧截断的话）；
  · 截断与折叠是行为（多行压单行；超长给 `...`，几周后读得出被截过）；
  · 保留 90 天的边界：第 90 天整天还在，第 91 天删；认不出的文件名不碰；
  · `/clear` 不清 = `purge_session`（唯一入口）不许动它（行为断言）；
  · 失败出声但不阻断（写不进去返回 None，永不抛）；
  · 并发 append 串行（第 2 步起一进程多会话真并行）；
  · manager 的挂点：`record_turn` 真的写（fake 依赖走通全函数）。

全程临时目录；manager 那条用 `object.__new__` + 假依赖，不碰生产 `data/memory/`。

ASCII-only 输出（Windows 控制台默认 GBK）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_daily_log.py
"""

from __future__ import annotations

import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import core.daily_log as DL  # noqa: E402


# ----------------------------------------------------------------------
# 1. 形状：表头一次、一轮一段
# ----------------------------------------------------------------------

def test_append_creates_header_once_and_entries():
    now = datetime(2026, 10, 1, 14, 30)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        p1 = DL.log_turn(td, "ws:private", "第一句", "第一答", now=now)
        p2 = DL.log_turn(td, "ws:private", "第二句", "第二答",
                         now=now.replace(hour=15, minute=0))
        assert p1 == p2 and p1.name == "2026-10-01.md", (p1, p2)
        text = p1.read_text(encoding="utf-8")
        assert text.count("# 2026-10-01") == 1, "表头写了不止一次"
        assert text.count("## 14:30") == 1 and text.count("## 15:00") == 1
        assert "- 用户: 第一句" in text and "- 助手: 第二答" in text
        assert text.count("- 用户:") == 2, "一轮必须恰好一段"


def test_collapse_and_truncation():
    assert DL._collapse("  a\n\n b\t c  ") == "a b c", "空白没有折叠成单行"
    long = "字" * 400
    got = DL._collapse(long)
    assert got.endswith("..."), "截断点没有记号"
    assert len(got) == DL._MAX_SIDE_CHARS + 3

    # 进文件的也是折叠 + 截断后的版本
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        p = DL.log_turn(td, "s", "多行\n用户话", "答案\n\n也\n多行")
        text = p.read_text(encoding="utf-8")
        assert "多行 用户话" in text and "答案 也 多行" in text


# ----------------------------------------------------------------------
# 2. 保留 90 天（边界两侧各打一发）
# ----------------------------------------------------------------------

def test_prune_retention_boundary():
    now = datetime(2026, 10, 1, 9, 0)
    cutoff = (now.date() - timedelta(days=DL.RETENTION_DAYS)).isoformat()
    just_over = (now.date() - timedelta(days=DL.RETENTION_DAYS + 1)).isoformat()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        d = Path(td)
        (d / (cutoff + ".md")).write_text("还活着\n", encoding="utf-8")
        (d / (just_over + ".md")).write_text("该走了\n", encoding="utf-8")
        (d / "notes.md").write_text("认不出形状\n", encoding="utf-8")

        DL.log_turn(td, "s", "u", "a", now=now)

        assert (d / (cutoff + ".md")).exists(), "第 90 天整天应当还在"
        assert not (d / (just_over + ".md")).exists(), "第 91 天应当被剪"
        assert (d / "notes.md").exists(), "认不出的文件名不许碰"


# ----------------------------------------------------------------------
# 3. 「/clear 不清」= purge_session 不许动它（行为断言）
# ----------------------------------------------------------------------

def test_purge_session_does_not_touch_logs():
    """`/clear 不清` 是行为断言：`purge_session` 是 `/clear` 的
    唯一入口 —— 日报不在它的清单里，加了这条就红。"""
    from core.state_registry import purge_session

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        p = DL.log_turn(td, "ws:private", "清我试试", "好")
        assert p is not None and p.exists()
        purge_session("whatever:private")
        assert p.exists(), "purge_session 动了日报 —— /clear 不许清它（#32）"


# ----------------------------------------------------------------------
# 4. 失败出声但不阻断 · 并发串行
# ----------------------------------------------------------------------

def test_failure_is_loud_but_not_fatal():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        blocker = Path(td) / "blocker"
        blocker.write_text("x", encoding="utf-8")
        got = DL.log_turn(blocker / "logs", "s", "u", "a")   # 在文件底下建目录 -> OSError
        assert got is None, "写不进去应当返回 None（永不抛）"


def test_concurrent_appends_are_serialized():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        errs = []

        def one(i: int) -> None:
            try:
                DL.log_turn(td, "conc:%d" % i, "u%d" % i, "a%d" % i)
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)

        threads = [threading.Thread(target=one, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errs, errs
        files = list(Path(td).glob("*.md"))
        assert len(files) == 1, files
        text = files[0].read_text(encoding="utf-8")
        assert text.count("## ") == 8, "并发下丢了条目或交错坏了"
        for i in range(8):
            assert ("conc:%d" % i) in text


# ----------------------------------------------------------------------
# 5. manager 的挂点：record_turn 真的写（fake 依赖走通全函数）
# ----------------------------------------------------------------------

def test_record_turn_appends_via_manager():
    import core.memory.manager as MM

    class _ST:
        def save_messages(self, sid, msgs, tokens):
            pass

    class _SM:
        def update(self, **kw):
            return ""

        def save(self, sid, text):
            pass

    m = object.__new__(MM.MemoryManager)
    m.short_term = _ST()
    m.session_memory = _SM()
    m._provider = None
    m._token_total = {}
    m._saved_count = {}
    m._session_note_cache = {}
    m._note_bg_status = lambda line: None
    # 手工搭的替身：`MemoryManager.__init__` 有的会话态这里要逐个点名。
    #    会话笔记的量闸累计器少这一格就是
    #    `AttributeError: 'MemoryManager' object has no attribute '_note_gate'`。
    #    （替身不继承真 `__init__`，新加一格里没有机械读者会去核它。）
    m._note_gate = {}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        m._logs_dir = Path(td)   # 生产里这个由 storage_path 推导（见 __init__）
        m.record_turn(
            "hook:private",
            {"role": "user", "content": "记录我这句"},
            {"role": "assistant", "content": "好"},
            token_count=7,
        )
        files = list(Path(td).glob("*.md"))
        assert len(files) == 1, files
        text = files[0].read_text(encoding="utf-8")
        assert "记录我这句" in text and "hook:private" in text
        assert "- 助手: 好" in text


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
