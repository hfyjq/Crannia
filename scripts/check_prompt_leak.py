#!/usr/bin/env python3
"""
不变量检查 —— system prompt 有没有又"漏回"消息表 / 又落库了？

这条不变量要求 system prompt 是独立请求参数
（provider.chat(..., system=)，每轮重建、不落库、不参与压缩）。
这个脚本是对它的客观核验：它只看 SQLite，不问代码。

用法::

    # 默认：检查 data/memory/short_term.db，基线取脚本内常量
    .venv/Scripts/python.exe scripts/check_prompt_leak.py

    # 看每一条违规行的原文摘要
    .venv/Scripts/python.exe scripts/check_prompt_leak.py --verbose

    # 库在别处 / 基线要覆盖 / 落地时刻要手填
    .venv/Scripts/python.exe scripts/check_prompt_leak.py --db <path> --baseline 7
    .venv/Scripts/python.exe scripts/check_prompt_leak.py --p1-time <YYYY-MM-DDTHH:MM:SS>

退出码: `0` 全部通过 · `1` 发现泄漏 · `2` 环境问题（库不存在等）

──────────────────────────────────────────────────────────────────────
检查哪四项，以及为什么判据是"不再增长"而不是"为零"
──────────────────────────────────────────────────────────────────────

① 孤儿 system 行 —— `role='system'` 且 `extra_json` 里没有 `kind`。

这是那类 bug 的指纹：若 `agent_loop` 在 `len(messages)==0` 时
建 prompt 并把它塞进 `messages[0]`，而 `record_turn` 把列表增量落库 ->
整份系统提示词被当普通消息写进 SQLite；同时 `load_session` 的 role 过滤
把它丢掉（只写不读）-> 每次重启后模型完全没有系统提示词。

[!] 库里已有的旧行不会自己消失，也不该删 —— 它们是 `load_session`
那条 role 过滤仍然必须存在的理由（删掉过滤，旧 prompt 行会复活成普通
system 消息）。所以判据是「计数不再增长」。

② 有没有 `created_at` 晚于不变量落地时刻的孤儿行 —— 那才是新泄漏。
落地时刻是一个固定常数（`_P1_SINCE_UTC`，绝对时刻），比较前换算成
与 `created_at` 相同的本机墙上时间。

③ 摘要行 必须带 `kind='history_summary'`，且必须是 `user` 角色。
出现 `role='system'` 的摘要 -> Anthropic 侧 `system` 字段
收的是历史，模型会把它当当前指令。另外：内容以 `<historical_context>` 开头
却没有 `kind` 的行 = 摘要丢了标记，重启时会被静默丢弃。

④ 续聊 nudge 指纹 —— '[This is a continuous conversation'。
它进的是 system 请求参数，不再落库；旧行同样只判"不再增长"。

──────────────────────────────────────────────────────────────────────
怎么用（配合真实运行）
──────────────────────────────────────────────────────────────────────

1. 跑一次，记下三个计数（这就是基线，不必相信脚本里的常量）
2. 正常用一段时间，期间让 agent 干活（尤其是长会话 / 大文件，好触发压缩）
3. 再跑一次 —— 计数应当完全不变。变了就是泄漏，用 `--verbose` 看是哪几条
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 给孤儿行划"新旧"分界的时间点：它之前的是观察基线，它之后的一律算泄漏。
#: 这里不放提交号 —— 公开副本的历史里没有那个对象，写死它只会让判据在那边失效。
#: 存的是绝对时刻（UTC），比较前换成本机墙上时间 —— 库里的 `created_at`
#: 是 `time.localtime()` 的结果、不带时区；写死一个本机时间的字符串，
#: 换一台时区不同的机器，同一条分界线会整体平移几小时。
_P1_SINCE_UTC = "2026-09-24T14:51:05+00:00"

#: 观察基线（不变量落地之前留下的旧行）。仅供对比，
#: 脚本不会因为"等于基线"而放过新行 —— ② 才是硬判据。
_BASELINE_ORPHAN_SYSTEM = 7
_BASELINE_NUDGE_ROWS = 2

NUDGE_FINGERPRINT = "[This is a continuous conversation"
SUMMARY_MARKER = "<historical_context>"


def _p1_time() -> str:
    """分界线：这个时刻之前的孤儿行算观察基线，之后的算泄漏。

    返回本机墙上时间，与库里 `created_at` 的格式和时区语义一致。
    """
    ts = datetime.fromisoformat(_P1_SINCE_UTC).timestamp()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def _fetch(db: Path) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    def rows(sql, *args):
        return [dict(r) for r in cur.execute(sql, args)]

    data = {}
    data["orphan_system"] = rows(
        "SELECT id, session_id, length(content) AS chars, created_at,"
        "       substr(content, 1, 60) AS head"
        "  FROM messages"
        " WHERE role='system'"
        "   AND json_extract(extra_json,'$.kind') IS NULL"
        " ORDER BY id"
    )
    data["nudges"] = rows(
        "SELECT id, session_id, created_at FROM messages"
        " WHERE role='system'"
        "   AND json_extract(extra_json,'$.kind') IS NULL"
        "   AND content LIKE ?"
        " ORDER BY id", NUDGE_FINGERPRINT + "%"
    )
    data["summary_rows"] = rows(
        "SELECT id, role, session_id,"
        "       COALESCE(json_extract(extra_json,'$.kind'),'') AS kind,"
        "       created_at"
        "  FROM messages"
        " WHERE json_extract(extra_json,'$.kind') = 'history_summary'"
        " ORDER BY id"
    )
    # <historical_context> 开头但没带标记 = 摘要丢了 kind
    data["unmarked_summaries"] = rows(
        "SELECT id, role, session_id, created_at, substr(content,1,40) AS head"
        "  FROM messages"
        " WHERE content LIKE ?"
        "   AND json_extract(extra_json,'$.kind') IS NULL"
        " ORDER BY id", SUMMARY_MARKER + "%"
    )
    data["kind_counts"] = rows(
        "SELECT role, COALESCE(json_extract(extra_json,'$.kind'),'(none)') AS kind,"
        "       COUNT(*) AS n"
        "  FROM messages GROUP BY role, kind ORDER BY n DESC"
    )
    con.close()
    return data


def _calibration() -> str:
    """校准器现状 —— 只读参考。探针/压测可能污染它，顺手可见。"""
    p = ROOT / "data" / "runtime" / "calibration.json"
    if not p.exists():
        return "(无 calibration.json)"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return ", ".join(
            f"{k}: factor={v.get('ema'):.4f} samples={v.get('samples')}"
            for k, v in d.items()
        )
    except Exception as e:
        return f"(读不出: {type(e).__name__})"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="P1 不变量检查：system prompt 有没有又落库 / 又漏回消息表",
    )
    ap.add_argument("--db", default=str(ROOT / "data" / "memory" / "short_term.db"),
                    help="short_term.db 路径")
    ap.add_argument("--baseline", type=int, default=_BASELINE_ORPHAN_SYSTEM,
                    help=f"孤儿 system 行的观察基线（默认 {_BASELINE_ORPHAN_SYSTEM}）")
    ap.add_argument("--p1-time", default=None,
                    help="P1 落地时刻，本机墙上时间（YYYY-MM-DDTHH:MM:SS）；"
                         "默认由脚本内固定绝对时刻换算")
    ap.add_argument("--verbose", action="store_true", help="打印每一条违规行")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"[check] 数据库不存在: {db}")
        print("[check] 还没跑过框架就没有这个消息表 —— 这不是失败。")
        return 2

    p1_time = args.p1_time or _p1_time()
    d = _fetch(db)

    orphans = d["orphan_system"]
    nudges = d["nudges"]
    summaries = d["summary_rows"]
    unmarked = d["unmarked_summaries"]

    new_orphans = [r for r in orphans if (r.get("created_at") or "") > p1_time]
    new_nudges = [r for r in nudges if (r.get("created_at") or "") > p1_time]
    bad_summary_role = [r for r in summaries if r.get("role") != "user"]

    print("=" * 66)
    print("P1 不变量检查 — system prompt 是否仍是「请求参数」")
    print("=" * 66)
    print(f"库          : {db}")
    print(f"P1 落地时刻 : {p1_time}")
    print()

    failures = []

    # ① 孤儿 system 行（判据：不再增长）
    n = len(orphans)
    grew = n > args.baseline
    print(f"[1] 孤儿 system 行 (role=system 且无 kind) : {n}"
          f"   （观察基线 {args.baseline}）")
    if n:
        print("    note: 旧行不会消失也不该删 —— 它们正是 load_session"
              " 的 role 过滤仍然必须存在的原因")
    if grew:
        failures.append(f"孤儿 system 行从基线 {args.baseline} 涨到 {n}")
    elif n:
        print(f"    OK 未增长")

    # ② 新泄漏（硬判据）
    print(f"[2] 其中 created_at 晚于 P1 的（= 新泄漏）: {len(new_orphans)}")
    if new_orphans:
        failures.append(f"{len(new_orphans)} 条 P1 之后新写入的 system prompt 行")
        for r in new_orphans:
            print(f"    FAIL id={r['id']} {r['created_at']} "
                  f"{r['chars']}字符  {str(r['head'])[:44]}")
    else:
        print(f"    OK 没有新泄漏")

    # ③ 摘要行
    print(f"[3] 摘要行 (kind=history_summary)          : {len(summaries)}")
    if bad_summary_role:
        failures.append(f"{len(bad_summary_role)} 条摘要不是 user 角色")
        for r in bad_summary_role:
            print(f"    FAIL id={r['id']} role={r['role']!r} —— Anthropic 会把"
                  f"历史当指令发（应为 user）")
    elif summaries:
        print(f"    OK 全部是 user 角色")
    print(f"    其中丢了 kind 的 <historical_context> 行 : {len(unmarked)}")
    if unmarked:
        failures.append(f"{len(unmarked)} 条摘要丢了 kind 标记（B5 回归路径）")
        for r in unmarked:
            print(f"    FAIL id={r['id']} {r['created_at']}  {str(r['head'])[:44]}")

    # ④ nudge 指纹
    print(f"[4] 续聊 nudge 落库行                      : {len(nudges)}"
          f"   （观察基线 {_BASELINE_NUDGE_ROWS}）")
    print(f"    其中 P1 之后的                          : {len(new_nudges)}")
    if new_nudges:
        failures.append(f"{len(new_nudges)} 条 P1 之后落库的 nudge")
        for r in new_nudges:
            print(f"    FAIL id={r['id']} {r['created_at']}")

    # 分布 + 校准器（参考）
    print()
    print("── 参考（不参与判定）" + "─" * 40)
    print("role × kind 分布:")
    for r in d["kind_counts"]:
        print(f"    {r['role']:<10} {r['kind']:<18} {r['n']}")
    print(f"校准器: {_calibration()}")

    if args.verbose:
        print()
        print("── 全部孤儿 system 行 ──")
        for r in orphans:
            print(f"    id={r['id']:<5} {r['created_at']}  {r['chars']:>6}字符  "
                  f"{str(r['head'])[:50]}")

    print()
    print("=" * 66)
    if failures:
        print("结论: FAIL —— 发现泄漏")
        for f in failures:
            print(f"  * {f}")
        print()
        print("怎么办:")
        print("  * 新孤儿 system 行 -> 有人在往 messages 里插 system 消息。")
        print("    查 core/agent_loop.py 是否又出现「找第一条 role==system」")
        print("    或 messages.insert(0, {...})。system prompt 必须是请求参数，")
        print("    不进 messages、不落库、不参与压缩。")
        print("  * 摘要不是 user 角色 -> core/memory/compressor.py 的")
        print("    _summary_message() 被改回去了。")
        print("  * 摘要丢了 kind -> _persist_compaction / replace_messages 链路。")
        print("=" * 66)
        return 1

    print("结论: PASS —— 未发现 system prompt 泄漏")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
