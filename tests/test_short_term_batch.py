"""短期记忆的写事务（`core/memory/short_term.py`）的机械读者。

它守的是一条名字说对了、行为可能是假的的判据：

    `save_messages` 的 docstring 写着 "Persist multiple messages in a
    single transaction"，但如果它里面调的是 `save_message` ——
    后者自己 `commit()`，那条事务每写一条就被切碎一次。

"多写几条" 的代价是每条一次 fsync（`commit` 在 WAL 下要落盘）。
一轮对话要落 3 条以上消息（见 `MemoryManager.record_turn`），所以这条不是纸面问题。

**判据必须数 `COMMIT` 的条数，不能看代码里有没有 `with self._conn`** ——
那段代码可以逐字就写着 `with self._conn:`，而它一点用都没有。
这正是"绿了但没测到"的形状：读源码的检查器会看着那行说 OK。

负对照：把 `save_messages` 改成"循环调 `save_message`"，本文件必须红。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_short_term_batch.py
"""

import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.memory.short_term import ShortTermMemory  # noqa: E402


def _with_db(fn):
    """在临时目录里建库跑 —— 绝不碰生产 `data/memory/short_term.db`。"""
    with tempfile.TemporaryDirectory() as d:
        st = ShortTermMemory(str(Path(d) / "st.db"))
        try:
            return fn(st)
        finally:
            st._conn.close()


def _count_commits(st):
    """装一个 trace 回调，返回 `(计数器, 读值函数)`。

    数的是真的执行过的 `COMMIT` 语句，不是"代码里写了 commit"。
    """
    seen = {"n": 0}

    def _trace(sql):
        if sql.strip().upper().startswith("COMMIT"):
            seen["n"] += 1

    st._conn.set_trace_callback(_trace)
    return seen


def test_save_messages_commits_only_once():
    """五条消息 = 一次 `COMMIT`。"""
    def _run(st):
        st.init_session("s-batch")
        st._conn.set_trace_callback(None)
        seen = _count_commits(st)
        msgs = [{"role": "user", "content": "m%d" % i} for i in range(5)]
        st.save_messages("s-batch", msgs)
        st._conn.set_trace_callback(None)
        return seen["n"]

    got = _with_db(_run)
    assert got == 1, (
        "5 条消息应当只 COMMIT 一次，实际 %d 次 —— "
        "多一次就是多一次 fsync" % got)


def test_the_batch_really_wrote_everything():
    """省 fsync 不许省掉数据 —— 五条一条都不能少。"""
    def _run(st):
        st.init_session("s-keep")
        msgs = [{"role": "user", "content": "m%d" % i} for i in range(5)]
        st.save_messages("s-keep", msgs)
        rows = st._conn.execute(
            "SELECT content FROM messages WHERE session_id=? ORDER BY id",
            ("s-keep",),
        ).fetchall()
        return [r[0] for r in rows]

    got = _with_db(_run)
    assert got == ["m0", "m1", "m2", "m3", "m4"], "落库内容不对: %r" % got


def test_save_message_still_commits_on_its_own():
    """单条那条路不变：它自己开一个事务（别的调用方还靠它）。"""
    def _run(st):
        st.init_session("s-one")
        st._conn.set_trace_callback(None)
        seen = _count_commits(st)
        st.save_message("s-one", {"role": "user", "content": "x"})
        st._conn.set_trace_callback(None)
        return seen["n"]

    got = _with_db(_run)
    assert got == 1, "单条写入应当 COMMIT 一次，实际 %d 次" % got


def test_the_commit_counter_can_actually_fail():
    """负对照：把实现改成循环调单条，计数器必须数出 N 次。

    不这么验一遍的话，上面那条 `== 1` 可能只是"这个计数器根本不会动"。
    """
    def _run(st):
        st.init_session("s-old")
        st._conn.set_trace_callback(None)
        seen = _count_commits(st)
        # 反例形状：循环调 save_message（每条自己 commit）
        for i in range(5):
            st.save_message("s-old", {"role": "user", "content": "m%d" % i})
        st._conn.set_trace_callback(None)
        return seen["n"]

    got = _with_db(_run)
    assert got == 5, (
        "旧形状应当数出 5 次 COMMIT（计数器本身没坏才能这么断言），实际 %d" % got)


def test_record_turn_batches_through_the_single_entry_point():
    """接线：`MemoryManager.record_turn` 里不许再出现逐条 `save_message`。

    这是"改了一处、留了一处"的防线 —— 批量写入在新函数上，
    而调用方不换的话，省下来的 fsync 一个都没省。
    """
    src = (_PROJECT_ROOT / "core" / "memory" / "manager.py").read_text(
        encoding="utf-8", errors="replace")
    assert "self.short_term.save_messages(" in src, (
        "record_turn 没有走批量的那一条 —— 事务还是被切碎的")
    # 只数调用形状，不数定义（`short_term.py` 里那个不算）
    bad = [ln for ln in src.splitlines()
           if "self.short_term.save_message(" in ln]
    assert not bad, (
        "record_turn 里还有逐条的 save_message 调用（每条一次 fsync）：%r" % bad)


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
