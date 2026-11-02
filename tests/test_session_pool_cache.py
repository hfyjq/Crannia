# -*- coding: utf-8 -*-
"""`SessionPool._loaded` 的条数上限。

## 钉住的一件事实

活动会话的完整消息表常驻在 `_loaded` 里，而它原来无上限：进程跑得越久、
走过的会话越多，表只增不减。这条量的是"有上限、且淘汰的是最久没用的那条"，
以及"被淘汰的下一次取用会从库里重载"（内容一致的前提是每轮都已落库）。

ASCII-only 输出（Windows 控制台 GBK）。

用法：
    .venv/Scripts/python.exe tests/test_session_pool_cache.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.session_pool import MAX_LOADED_SESSIONS, SessionPool      # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    ok = bool(ok)
    print("  [%s] %s%s" % ("ok" if ok else "!!", desc,
                           ("  -- " + detail) if detail and not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
        _FAILED.append(desc)


class _FakeMgr:
    """只记账：`get_or_create` 的装载走被替换掉的 `_load_thread`。"""

    def __init__(self) -> None:
        self.loaded: list = []

    def load_session(self, sid: str):
        self.loaded.append(sid)
        return sid, [{"role": "user", "content": sid}]


def _pool() -> tuple:
    mgr = _FakeMgr()
    pool = SessionPool(mgr)
    reloads: list = []

    def _load_thread(key: str):
        reloads.append(key)
        return key, [{"role": "user", "content": key}]

    pool._load_thread = _load_thread
    return pool, mgr, reloads


def test_cache_is_capped():
    print("\n-- 1. 上限 --")
    pool, _mgr, _reloads = _pool()
    keys = ["k%02d" % i for i in range(MAX_LOADED_SESSIONS + 5)]
    for k in keys:
        pool.get_or_create(k)
    check("★★ 条数不超过上限（%d）" % MAX_LOADED_SESSIONS,
          len(pool._loaded) == MAX_LOADED_SESSIONS, str(len(pool._loaded)))
    check("★ 最早的那条被淘汰了", keys[0] not in pool._loaded,
          repr(list(pool._loaded)[:3]))
    check("★ 最近的那条还在", keys[-1] in pool._loaded)


def test_hit_refreshes_and_reloads_from_store():
    print("\n-- 2. 命中刷新热度 / 淘汰后从库里重载 --")
    pool, _mgr, reloads = _pool()
    keys = ["k%02d" % i for i in range(MAX_LOADED_SESSIONS)]
    for k in keys:
        pool.get_or_create(k)

    # 摸一下最早的那条 —— 它不该再是"最久没用"的
    pool.get_or_create(keys[0])
    pool.get_or_create("fresh")
    check("★★ 刚用过的那条不许被淘汰", keys[0] in pool._loaded,
          repr(list(pool._loaded)[:3]))
    check("★ 被淘汰的是真正的 LRU（原来的第二条）",
          keys[1] not in pool._loaded, repr(list(pool._loaded)[:3]))

    before = len(reloads)
    pool.get_or_create(keys[1])
    check("★★ 淘汰之后再取用 -> 从库里重载（不是凭空造一条空的）",
          len(reloads) == before + 1 and keys[1] in pool._loaded,
          "%d -> %d" % (before, len(reloads)))
    body = pool._loaded[keys[1]][1]
    check("★ 重载回来的是那条会话自己的消息",
          body and body[0].get("content") == keys[1], repr(body)[:120])


def test_cache_critical_section_is_mutually_exclusive():
    """命中路径（get + move_to_end）期间，别的线程不许改这张表。

    判据不是"跑很多线程没炸"（那在没锁的版本上也是绿的 —— 实测过），
    而是在临界区里放一个窗口，看另一个线程能不能挤进来：
    有锁：它被挡在外面；没锁：它当场就进去了（这条会红）。
    """
    import threading
    from collections import OrderedDict

    pool, _mgr, _reloads = _pool()
    pool._loaded["hot"] = ("hot", [])       # 预置一条，走命中路径

    entered = threading.Event()
    release = threading.Event()
    writer_done = threading.Event()
    state = {"inside": False}

    class _Window(OrderedDict):
        def get(self, key, default=None):
            got = super().get(key, default)
            if key == "hot":
                state["inside"] = True
                entered.set()
                release.wait(timeout=2.0)   # A 停在临界区中间
            return got

    pool._loaded = _Window(pool._loaded)

    def writer() -> None:
        entered.wait(timeout=2.0)
        pool._remember("other", ("other", []))
        writer_done.set()

    # reader 跑进窗口后停住；主线程看到 `entered` 才起 writer ——
    # 顺序由事件保证，不靠 sleep 猜。
    reader = threading.Thread(target=pool.get_or_create, args=("hot",),
                              daemon=True)
    reader.start()
    entered.wait(timeout=2.0)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    got_in = writer_done.wait(timeout=0.5)

    check("★★ 临界区内别的线程写不进来", not got_in,
          "writer 在 0.5 秒内就写进了缓存 —— 说明临界区没有互斥")
    release.set()
    reader.join(timeout=5)
    t.join(timeout=5)
    check("★ 读的那条最终没抛", reader.is_alive() is False, "reader 卡住了")
    check("★ 窗口确实进过（不是空转）", state["inside"])


def main() -> int:
    print("=" * 62)
    print("session pool: _loaded is capped")
    print("=" * 62)
    for fn in (test_cache_is_capped, test_hit_refreshes_and_reloads_from_store,
               test_cache_critical_section_is_mutually_exclusive):
        try:
            fn()
        except Exception as exc:                              # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(fn.__name__ + " 没抛异常", False,
                  "%s: %s" % (type(exc).__name__, exc))
    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
