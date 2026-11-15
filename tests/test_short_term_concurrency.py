#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""同一个 SQLite 连接被多条会话线程共用时的事务边界。

    .venv/Scripts/python.exe tests/test_short_term_concurrency.py

判据：一个写入方在自己的事务里失败，不许把另一个写入方的数据带走。

做法：两个线程共用同一个 `ShortTermMemory`。A 每次写入后抛异常（模拟事务中途
失败），B 正常提交。没有锁时，B 的 `with conn` 会把 A 已经写进连接、还没提交的
那一条一起提交掉 —— 于是 A 明明失败了，库里却有它的行。

[!] 输出一律 ASCII（GBK 控制台）。
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.memory.short_term import ShortTermMemory                        # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED = list()


def check(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + str(detail)) if detail else ""))


def test_failed_transaction_does_not_leak_into_another():
    with tempfile.TemporaryDirectory() as tmp:
        st = ShortTermMemory(str(Path(tmp) / "t.db"))
        real_insert = st._insert_message

        def insert(session_id, msg, tc=0):
            rid = real_insert(session_id, msg, tc)
            if threading.current_thread().name == "A":
                raise RuntimeError("fail after insert")
            return rid

        st._insert_message = insert
        rounds = 300
        errs = []

        def worker(name):
            for _ in range(rounds):
                try:
                    st.save_messages(name, [{"role": "user", "content": name}])
                except RuntimeError:
                    pass
                except Exception as exc:                                  # noqa: BLE001
                    errs.append("%s: %s" % (type(exc).__name__, exc))

        ta = threading.Thread(target=worker, args=("A",), name="A")
        tb = threading.Thread(target=worker, args=("B",), name="B")
        ta.start()
        tb.start()
        ta.join()
        tb.join()

        check("A 的失败事务没有留下任何行", st.count_messages("A") == 0,
              st.count_messages("A"))
        check("B 的提交一条不丢", st.count_messages("B") == rounds,
              st.count_messages("B"))
        check("没有意外的数据库异常", not errs, errs[:3])
        st.close()


def main():
    for fn in (test_failed_transaction_does_not_leak_into_another,):
        print("\n-- %s --" % fn.__name__)
        try:
            fn()
        except Exception as exc:                       # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s 没抛异常（抛了 %s）" % (fn.__name__, type(exc).__name__),
                  False, str(exc))

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
