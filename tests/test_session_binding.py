"""通道键 -> 存储 id 那座桥的机械读者。

## 它守什么

「哪个通道现在在哪条线程上」必须是一张持久的表
（`short_term.session_bindings`）。只靠 `SessionPool._loaded` 那份内存缓存
不够：进程一停就没了。而 `/clear` 会给存储面铸新 id，没有那张表，重启之后
只能回落到"按通道键加载"，读到的可能正是第一条线程。

四条判据，各自对应一个真会坏的方式：

  1. 新键要铸自己的线程 —— 不是拿通道键当 id（两者必须分开）
  2. 重启之后还认得出 —— 内存缓存没了，只剩那张表
  3. `/clear` 之前那一段不许被读回来 —— 地基可以换（"删干净"或
     "绑定指对了那条线程"），但不变量不变：旧内容不许被重新读回。
     `/clear` 会故意不删行，所以这条钉的不是"删了"，是"没指回来"。
  4. `bound_storage_id` 是纯查询 —— 问一句"在不在"不许建出会话来
     （路由判活那一族会问它）。

负对照：把 `SessionPool.new_session` 里那句 `bind_session` 摘掉，判据 3 必须红。
旧行还在库上时，红的样子就是"读回了旧线程"。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_session_binding.py
"""

import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

KEY = "workspace:private"
OTHER = "workspace:group:g-1"
SENTINEL = "OLD-THREAD-SENTINEL"

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


def _make_pool(tmp):
    """一个完全落在 *tmp* 里的 `(MemoryManager, SessionPool)`。

    与 `test_clear_session._make_mgr` 同一个形状 —— 两个绝对路径进去，
    于是它写不出临时目录（测试不许碰生产目录）。
    """
    import core.memory.manager as manager_mod
    from core.session_pool import SessionPool

    cfg = {
        "memory": {
            "storage_path": str(Path(tmp) / "memory"),
            "short_term": {"max_history": 100},
            "compression": {"token_threshold": 0.85, "keep_recent_turns": 3},
            "session_memory": {"sessions_dir": str(Path(tmp) / "sessions")},
        }
    }
    mgr = manager_mod.MemoryManager(cfg, provider=None)
    return mgr, SessionPool(mgr)


def _seed(pool, key, text):
    """在这个键当前的线程上写一句用户话，返回那条线程的 id。"""
    sid, _msgs = pool.get_or_create(key)
    pool._mgr.short_term.save_messages(sid, [{"role": "user", "content": text}])
    return sid


def _texts(mgr, sid):
    return "\n".join(str(m.get("content", ""))
                     for m in mgr.short_term.load_messages(sid))


# 1. 新键铸自己的线程 + 绑上

def test_a_new_key_gets_its_own_thread_and_a_binding():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        mgr, pool = _make_pool(tmp)
        try:
            sid, _msgs = pool.get_or_create(KEY)
            check("新键拿到的是一个**铸出来的** id（不是通道键自己）",
                  sid and sid != KEY, repr(sid))
            check("它长得像铸出来的（`session_` 前缀）",
                  sid.startswith("session_"), repr(sid))
            check("★ 通道键在绑定表里指向它",
                  mgr.short_term.bound_storage_id(KEY) == sid,
                  repr(mgr.short_term.bound_storage_id(KEY)))
        finally:
            mgr.close()


# 2. 同一个键两次 = 同一条线程；重启之后还是同一条

def test_the_same_key_twice_is_one_thread():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        mgr, pool = _make_pool(tmp)
        try:
            a, _ = pool.get_or_create(KEY)
            b, _ = pool.get_or_create(KEY)
            check("同一进程里两次 = 同一条", a == b, "%r vs %r" % (a, b))
            check("另一个键是**另一条**",
                  pool.get_or_create(OTHER)[0] != a)
        finally:
            mgr.close()


def test_a_restart_finds_the_same_thread():
    """`_loaded` 是内存缓存 —— 这条钉的是只剩那张表时还认得出。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        mgr1, pool1 = _make_pool(tmp)
        try:
            sid1 = _seed(pool1, KEY, SENTINEL)
        finally:
            mgr1.close()

        mgr2, pool2 = _make_pool(tmp)          # 「重启」
        try:
            sid2, msgs = pool2.get_or_create(KEY)
            check("★ 重启之后还是同一条线程", sid2 == sid1, "%r vs %r" % (sid2, sid1))
            check("★ 而且内容也真的回来了", SENTINEL in _texts(mgr2, sid2))
        finally:
            mgr2.close()


# 3. /clear 之前那一段不许被读回来

def test_clear_does_not_resurrect_the_previous_thread():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        mgr, pool = _make_pool(tmp)
        try:
            old_sid = _seed(pool, KEY, SENTINEL)
            new_sid, msgs = pool.new_session(KEY)
            # 不断言"id 换了新的" —— 这条钉的是结果
            #    （清完那一条读不到旧内容），对"行被删了"与"行留着、
            #    只是换了条线程"两种实现都成立。
            check("★ `/clear` 之后那一条**读不到**旧内容",
                  SENTINEL not in _texts(mgr, new_sid))
            check("★ 绑定**跟着走了**（这就是负对照的靶子）",
                  mgr.short_term.bound_storage_id(KEY) == new_sid,
                  repr(mgr.short_term.bound_storage_id(KEY)))
            check("★ `/clear` 给的是**新铸的**线程 id",
                  new_sid != old_sid,
                  "%r vs %r" % (new_sid, old_sid))
        finally:
            mgr.close()

        mgr2, pool2 = _make_pool(tmp)          # 「重启」
        try:
            sid3, msgs2 = pool2.get_or_create(KEY)
            check("★ 重启后认的是清完那一条", sid3 == new_sid,
                  "%r vs %r" % (sid3, new_sid))
            check("★★ **旧那一段没有被读回来**",
                  SENTINEL not in _texts(mgr2, sid3),
                  "读回了 %r" % SENTINEL)
        finally:
            mgr2.close()


# 3b. 这条才是 `bind_session` 的真靶子

def test_clear_as_the_first_message_after_a_restart():
    """上一条负对照打不中它 —— 这一条才是那句 `bind_session` 的靶子。

    如果 `/clear` 之前先 `get_or_create` 过一次，`_loaded` 里有那条铸出来的
    id，`new_session` 复用的就是它，绑定本来就对，摘掉 `bind_session`
    整批仍然全绿（这正是"负对照的靶子自己打不中"那个形状）。

    而这里 `/clear` 是重启后的第一条，`_loaded` 是空的，
    回退到通道键当旧 id，复用通道键，绑定还指着上一条铸出来的线程。
不重绑的话，下一次解析就把它读回来（下一次重启按 key 又把旧行装回来）。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        mgr1, pool1 = _make_pool(tmp)
        try:
            sid1 = _seed(pool1, KEY, SENTINEL)
        finally:
            mgr1.close()

        mgr2, pool2 = _make_pool(tmp)          # 「重启」
        try:
            pool2.new_session(KEY)             # /clear 是重启后的第一条
        finally:
            mgr2.close()

        mgr3, pool3 = _make_pool(tmp)          # 再「重启」
        try:
            sid3, _msgs = pool3.get_or_create(KEY)
            check("★ 再重启时认的**不是**上一条线程", sid3 != sid1,
                  "%r vs %r" % (sid3, sid1))
            check("★★ 旧那一段**没有被读回来**",
                  SENTINEL not in _texts(mgr3, sid3),
                  "读回了 %r" % SENTINEL)
        finally:
            mgr3.close()


# 4. 纯查询不许建会话

def test_bound_storage_id_is_a_pure_lookup():
    """与 `storage_id_for` 是两个动词：问一句"在不在"不许建出会话。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        mgr, pool = _make_pool(tmp)
        try:
            before = mgr.short_term._conn.execute(
                "SELECT COUNT(*) FROM sessions").fetchone()[0]
            got = mgr.short_term.bound_storage_id("never-seen")
            after = mgr.short_term._conn.execute(
                "SELECT COUNT(*) FROM sessions").fetchone()[0]
            check("没绑过 => None", got is None, repr(got))
            check("★ **一行都没多**（它是纯查询）", before == after,
                  "%d -> %d" % (before, after))
            check("负对照：`storage_id_for` **会**建（同一个键）",
                  bool(mgr.storage_id_for("never-seen")))
        finally:
            mgr.close()


# 5. 退化调用（空键）与今天逐字节相同

def test_an_empty_key_stays_degenerate():
    """`session_key_for(None)` 返回 `""` —— 那是既定契约，
    别让它变成"铸一个新线程"（每次调用铸一个 = 无界垃圾行）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        mgr, pool = _make_pool(tmp)
        try:
            a = mgr.storage_id_for("")
            b = mgr.storage_id_for("")
            check("空键两次是同一个值", a == b, "%r vs %r" % (a, b))
            check("它仍是空串（与今天相同）", a == "", repr(a))
            check("空键**不进**绑定表",
                  mgr.short_term.bound_storage_id("") is None)
        finally:
            mgr.close()


# 6. 顺手修好的那条：投影盖的章与快照里的号现在是同一个键

def test_the_projection_stamp_uses_the_channel_key():
    """`core/workspace.py::_projection_is_mine` 比的两个东西。

    它比的是 `wm._projected_for`（调度器盖的章）与 `current_session_id()`
    （请求快照里的会话号）。通道键与存储键分开之后，两者必须都是通道键
    —— 否则每一份投影都会被判成"不是我的"，
    而 `session_cwd` / 跨区放行 / 删除授权会静默退回安全值
    （没有东西会红：那是 fail-open 的那一侧）。

    取的是赋值语句的值（AST），不是 grep —— 注释里会提到旧写法。
    """
    import ast

    src = (_PROJECT_ROOT / "core" / "pipeline" / "scheduler.py").read_text(
        encoding="utf-8", errors="replace")
    stamped = []
    for n in ast.walk(ast.parse(src)):
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Attribute)
                and n.targets[0].attr == "_projected_for"):
            stamped.append(ast.unparse(n.value).strip())
    check("找得到盖章那一处（`_projected_for = ...`）", bool(stamped),
          "它改名/挪走了 —— 先确认 `_projection_is_mine` 还在不在")
    check("★★ 盖章用的是**通道键**", all(v == "session_key" for v in stamped),
          "实得 %r —— 拆键之后 `_projection_is_mine` 会恒为 False" % (stamped,))


# ------------------------------------------------------------------

def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s 没抛异常" % t.__name__, False, str(e))
    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
