"""文件留档的轮次索引 / 封印 / 恢复的机械读者。

## 它守什么

留档层存的是改前内容。而"回到第 N 轮时这个文件长什么样"这个问题，
它单独答不了 —— 改前只知道"我们改它之前它是什么"，不知道那是第几轮的事。

-> 三条判据：

  1. 哪一版属于哪一轮（索引）；恢复取的是"最早的、轮次 > N 的那一版"
     —— 它拍到的正是"第 N 轮结束时"的样子
  2. 安全闸：写回之前核对"现在的内容还是我们上次留下的那份吗"。
     不符（用户自己改过）-> 拒，而且整批一条都不写
  3. 写回的是字节，不是文本 —— `read_version` 返回的是 `read_text(...,
     errors="replace")` 的结果；拿它去写回一个非 UTF-8 的文件，认不出的字节会被
     静默换成 U+FFFD。-> 恢复那一侧必须走原始字节。

负对照：把安全闸那道核对摘掉，判据 2 必须红。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_file_rewind.py
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "tests"))

from _sandbox import sandbox  # noqa: E402
from core import file_history as FH  # noqa: E402

SID = "workspace:private"

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


def _write(p: Path, text, turn, sid=SID):
    """装成"某一轮里的一次改动"：先留改前档，再改内容。"""
    FH.snapshot_before(sid, str(p), turn=turn)
    p.write_text(text, encoding="utf-8")


def _paths(plan):
    return sorted(Path(it["path"]).name for it in plan)


# 1. 索引 + 计划

def test_the_index_records_which_turn_each_version_belongs_to():
    with sandbox() as tmp:
        p = Path(tmp) / "a.txt"
        p.write_text("v0", encoding="utf-8")
        _write(p, "v1", turn=0)          # 第 1 轮里改的（那时已完成 0 轮）
        _write(p, "v2", turn=2)          # 第 3 轮里改的（那时已完成 2 轮）

        idx = FH._load_index(SID)
        turns = [e["turn"] for e in idx[str(p)]]
        check("★ 记的是「拍这一版时已完成几轮」（第 K 轮里 = K-1）",
              turns == [0, 2], repr(turns))
        check("版本号还是那套（1 开始）",
              [e["v"] for e in idx[str(p)]] == [1, 2], repr(idx[str(p)]))
        check("目录里的内容留档没被索引文件挤坏",
              len(FH._versions(SID, str(p))) == 2,
              repr(FH._versions(SID, str(p))))


def test_plan_takes_the_earliest_version_after_the_cut():
    with sandbox() as tmp:
        p = Path(tmp) / "a.txt"
        p.write_text("v0", encoding="utf-8")
        _write(p, "v1", turn=0)          # 第 1 轮改的
        _write(p, "v2", turn=2)          # 第 3 轮又改的
        FH.seal_turn(SID, 2)             # 轮末封印（真实流程里由 `record_turn` 调）

        # 「回到第 2 轮之前」-> 要"第 1 轮结束时"的样子
        plan = FH.plan_restore(SID, before_turn=2)

        check("计划里有一条", len(plan) == 1, repr(plan))
        check("★ 取的是**第 3 轮那一版**（它的改前 = 第 1 轮结束时的样子）",
              plan and plan[0]["version"] == 2, repr(plan))
        check("没被卡住", plan and not plan[0]["blocked"], repr(plan))


def test_files_untouched_after_the_cut_are_not_in_the_plan():
    with sandbox() as tmp:
        a = Path(tmp) / "a.txt"
        b = Path(tmp) / "b.txt"
        a.write_text("x", encoding="utf-8")
        b.write_text("y", encoding="utf-8")
        _write(a, "x1", turn=0)
        _write(b, "y1", turn=0)
        _write(b, "y3", turn=2)

        plan = FH.plan_restore(SID, before_turn=2)
        check("★ 第 1 轮之后再没动过的那个**不在计划里**",
              _paths(plan) == ["b.txt"], repr(_paths(plan)))


def test_a_file_created_after_the_cut_is_marked_new():
    with sandbox() as tmp:
        a = Path(tmp) / "a.txt"
        a.write_text("x", encoding="utf-8")
        _write(a, "x1", turn=0)
        new = Path(tmp) / "brand_new.txt"
        FH.snapshot_before(SID, str(new), turn=2)     # 第 3 轮才创建它
        new.write_text("hello", encoding="utf-8")
        FH.seal_turn(SID, 2)

        plan = FH.plan_restore(SID, before_turn=2)
        item = [it for it in plan if it["path"].endswith("brand_new.txt")]
        check("★ 它的形态是 `new`（那一轮时还不存在）",
              item and item[0]["kind"] == "new", repr(plan))

        n, lines = FH.apply_restore(SID, plan)
        check("★ 恢复 = **把它删掉**", not new.exists(), "还在")
        check("回执说清了是删", any("删掉" in x for x in lines), repr(lines))


def test_the_turn_arithmetic_is_spelled_out():
    """「回到第 N 轮之前」↔「哪些版本算数」的换算 —— 这一格最容易错一位。

    记的是"拍这一版时已完成几轮"（第 K 轮里 = K-1）->
    回到第 N 轮之前 = 最早那一版里 `turn >= N-1` 的「改前」。
    """
    with sandbox() as tmp:
        p = Path(tmp) / "a.txt"
        p.write_text("T0-original", encoding="utf-8")
        _write(p, "T1-after", turn=0)     # 第 1 轮里改的
        _write(p, "T2-after", turn=1)     # 第 2 轮里改的
        _write(p, "T3-after", turn=2)     # 第 3 轮里改的
        FH.seal_turn(SID, 2)

        def _target(before_turn):
            """回到第 N 轮之前时，这个文件会被写成什么（只看计划，不改盘）。"""
            plan = FH.plan_restore(SID, before_turn=before_turn)
            if not plan:
                return "(不变)"
            raw = FH._version_bytes(SID, str(p), plan[0]["version"])
            return raw.decode("utf-8") if raw is not None else "(读不回)"

        check("回到第 1 轮之前 => **最初那份**（T0-original）",
              _target(1) == "T0-original", _target(1))
        check("回到第 2 轮之前 => **第 1 轮改完**的样子（T1-after）",
              _target(2) == "T1-after", _target(2))
        check("回到第 3 轮之前 => **第 2 轮改完**的样子（T2-after）",
              _target(3) == "T2-after", _target(3))
        check("回到第 4 轮之前 => 第 3 轮改完的样子 = **不用动**（T3-after 就是现在）",
              _target(4) == "(不变)", _target(4))


# 2. 安全闸

def test_seal_turn_records_the_after_state():
    with sandbox() as tmp:
        p = Path(tmp) / "a.txt"
        p.write_text("v0", encoding="utf-8")
        _write(p, "v1", turn=0)
        check("封印前是没盖的", FH._load_index(SID)[str(p)][-1]["after"] is None)

        n = FH.seal_turn(SID, 0)
        check("★ 轮末封印了一条", n == 1, repr(n))
        check("★ 盖的是**现在**的内容的哈希",
              FH._load_index(SID)[str(p)][-1]["after"] == FH._sha256_file(p))

        check("再封印一次 = 0（幂等）", FH.seal_turn(SID, 0) == 0)


def test_the_gate_refuses_when_the_file_was_edited_outside():
    with sandbox() as tmp:
        p = Path(tmp) / "a.txt"
        p.write_text("v0", encoding="utf-8")
        _write(p, "v1", turn=0)
        _write(p, "v2", turn=2)
        FH.seal_turn(SID, 2)

        # 用户自己在编辑器里改了（没经过我们）
        p.write_text("USER-EDITED", encoding="utf-8")

        plan = FH.plan_restore(SID, before_turn=2)
        check("★★ 计划里把它标成**卡住**", plan and plan[0]["blocked"],
              repr(plan))
        check("★ 而且说清了是「被外面改过」",
              "外面改过" in (plan[0]["blocked"] if plan else ""),
              repr(plan))

        n, lines = FH.apply_restore(SID, plan)
        check("★★ **一条都没写**", n == 0, repr(n))
        check("★★ 文件还是用户改的那个（没被覆盖）",
              p.read_text(encoding="utf-8") == "USER-EDITED",
              p.read_text(encoding="utf-8"))
        check("回执说了「一个文件都没写」",
              any("一个文件都没写" in x for x in lines), repr(lines))


def test_apply_is_all_or_nothing():
    """两个文件，一个干净一个被外面改了 -> 两个都不许动
    （判据：我们没有补偿机制，那就不开"写一半"这个头）。"""
    with sandbox() as tmp:
        a = Path(tmp) / "a.txt"
        b = Path(tmp) / "b.txt"
        a.write_text("a0", encoding="utf-8")
        b.write_text("b0", encoding="utf-8")
        _write(a, "a1", turn=0)
        _write(b, "b1", turn=0)
        _write(a, "a3", turn=2)
        _write(b, "b3", turn=2)
        FH.seal_turn(SID, 2)

        b.write_text("USER-EDITED-B", encoding="utf-8")   # 只有 b 被外面改了

        plan = FH.plan_restore(SID, before_turn=2)
        n, lines = FH.apply_restore(SID, plan)

        check("★ 一条都没写", n == 0, repr(n))
        check("★★ **干净的那个也没动**（整批停）",
              a.read_text(encoding="utf-8") == "a3",
              a.read_text(encoding="utf-8"))
        check("被外面改的那个原样", b.read_text(encoding="utf-8") == "USER-EDITED-B")


def test_a_version_that_cannot_be_read_stops_the_whole_batch():
    """留档读不回来 -> 整批不动（写之前就得知道写什么）。"""
    with sandbox() as tmp:
        a = Path(tmp) / "a.txt"
        b = Path(tmp) / "b.txt"
        a.write_text("a0", encoding="utf-8")
        b.write_text("b0", encoding="utf-8")
        _write(a, "a1", turn=0)
        _write(b, "b1", turn=0)
        _write(a, "a3", turn=2)
        _write(b, "b3", turn=2)
        FH.seal_turn(SID, 2)

        plan = FH.plan_restore(SID, before_turn=2)
        plan[0]["version"] = 999                 # 那一版不存在
        n, lines = FH.apply_restore(SID, plan)

        check("★★ 一条都没写", n == 0, repr(n))
        check("★★ 好的那个也没动（a 还是 a3）",
              a.read_text(encoding="utf-8") == "a3",
              a.read_text(encoding="utf-8"))
        check("b 也没动", b.read_text(encoding="utf-8") == "b3")
        check("回执说了「留档读不回来」", any("读不回来" in x for x in lines),
              repr(lines))


def test_a_write_failure_midway_rolls_back_what_was_done():
    """写到第二个失败 -> 第一个必须还原，并如实说（不是"回退了一半"当成功）。"""
    with sandbox() as tmp:
        a = Path(tmp) / "a.txt"
        b = Path(tmp) / "b.txt"
        a.write_text("a0", encoding="utf-8")
        b.write_text("b0", encoding="utf-8")
        _write(a, "a1", turn=0)
        _write(b, "b1", turn=0)
        _write(a, "a3", turn=2)
        _write(b, "b3", turn=2)
        FH.seal_turn(SID, 2)

        plan = FH.plan_restore(SID, before_turn=2)
        real_write = FH._atomic_write_bytes

        def _flaky(dst, data):
            if Path(dst).name == "b.txt":
                raise OSError("disk full (simulated)")
            return real_write(dst, data)

        FH._atomic_write_bytes = _flaky
        try:
            n, lines = FH.apply_restore(SID, plan)
        finally:
            FH._atomic_write_bytes = real_write

        check("★★ 净改动为 0（第一处写过的被还原了）", n == 0, repr(n))
        check("★★ a 回到了写之前的样子",
              a.read_text(encoding="utf-8") == "a3",
              a.read_text(encoding="utf-8"))
        check("b 没被动过", b.read_text(encoding="utf-8") == "b3")
        check("回执说了「已还原 / 一个文件都没写」",
              any("还原" in x or "一个文件都没写" in x for x in lines),
              repr(lines))


def test_a_file_changed_after_the_plan_stops_the_batch():
    """计划是刚才算的，执行是现在的 —— 中间被改过就不许覆盖。

    负对照是"不核就写"：`plan_restore` 核过一次，但那次核的是计划那一刻；
    执行侧不拿计划里的哈希再核一次，外面刚写的东西会被"退回去"。
    """
    with sandbox() as tmp:
        a = Path(tmp) / "a.txt"
        a.write_text("a0", encoding="utf-8")
        _write(a, "a1", turn=0)
        _write(a, "a3", turn=2)
        FH.seal_turn(SID, 2)

        plan = FH.plan_restore(SID, before_turn=2)
        a.write_text("EDITED-AFTER-PLAN", encoding="utf-8")   # 计划之后被改
        n, lines = FH.apply_restore(SID, plan)

        check("★★ 一条都没写", n == 0, repr(n))
        check("★★ 外面写的那份还在（没被覆盖）",
              a.read_text(encoding="utf-8") == "EDITED-AFTER-PLAN",
              a.read_text(encoding="utf-8"))
        check("回执说了「计划之后被改过」",
              any("改过" in x for x in lines), repr(lines))


def test_a_file_deleted_after_the_plan_stops_the_batch():
    """计划时它还在，执行时没了 —— 也不许写。

    判据是同一份：执行侧只认"现在还在、且还是我们留下的那份"。
    此前这一支被 `if existed and want` 跳过校验，于是"退回旧版"会把一个
    外面刚删掉的文件重新创建出来 —— 用我们的留档盖掉别人的删除。
    """
    with sandbox() as tmp:
        a = Path(tmp) / "a.txt"
        a.write_text("a0", encoding="utf-8")
        _write(a, "a1", turn=0)
        _write(a, "a3", turn=2)
        FH.seal_turn(SID, 2)

        plan = FH.plan_restore(SID, before_turn=2)
        check("计划里有它（前提）", len(plan) == 1, repr(plan))
        a.unlink()                                   # 计划之后被外面删掉
        n, lines = FH.apply_restore(SID, plan)

        check("★★ 一条都没写", n == 0, repr(n))
        check("★★ 文件没有被重新创建出来", not a.exists(), str(a))
        check("回执说了「计划之后被删掉」",
              any("删掉" in x for x in lines), repr(lines))


def test_a_failed_rollback_is_recorded_in_the_incident_ledger():
    """补偿失败必须留下持久痕迹 —— `/rewind` 的回执只说一句话，不许改。"""
    from core import incidents as I

    with sandbox() as tmp:
        I.clear()
        a = Path(tmp) / "a.txt"
        b = Path(tmp) / "b.txt"
        a.write_text("a0", encoding="utf-8")
        b.write_text("b0", encoding="utf-8")
        _write(a, "a1", turn=0)
        _write(b, "b1", turn=0)
        _write(a, "a3", turn=2)
        _write(b, "b3", turn=2)
        FH.seal_turn(SID, 2)

        plan = FH.plan_restore(SID, before_turn=2)
        real_write = FH._atomic_write_bytes
        seen = {"a": 0}

        def _flaky(dst, data):
            name = Path(dst).name
            if name == "b.txt":
                raise OSError("disk full (simulated)")
            seen["a"] += 1
            if seen["a"] >= 2:            # 第 2 次写 a = 补偿那一次
                raise OSError("rollback also failed")
            return real_write(dst, data)

        FH._atomic_write_bytes = _flaky
        try:
            n, lines = FH.apply_restore(SID, plan)
        finally:
            FH._atomic_write_bytes = real_write

        check("★ 补偿失败 -> 返回值如实报 1 个没还原", n == 1, repr(n))
        check("回执里写着「还原失败」", any("还原失败" in x for x in lines),
              repr(lines))
        items = I.all()
        check("★★ 事故账本里有它（回执不说，账本说）",
              any(it.get("kind") == "rewind_rollback" for it in items),
              repr(items))
        if items:
            check("账本那条带着是哪一次回退",
                  "文件回退" == items[0].get("component"), repr(items[0]))
        I.clear()


def test_without_a_seal_the_gate_blocks():
    """没封印 -> 认不出现在这份是不是我们留下的 -> 拒（不是"当作没问题"）。"""
    with sandbox() as tmp:
        p = Path(tmp) / "a.txt"
        p.write_text("v0", encoding="utf-8")
        _write(p, "v1", turn=0)
        _write(p, "v2", turn=2)          # 故意不 seal_turn

        plan = FH.plan_restore(SID, before_turn=2)
        check("★ 没封印 -> 卡住", plan and plan[0]["blocked"], repr(plan))
        check("而且说清了是没封印", "还没有被封印" in (plan[0]["blocked"] if plan else ""))


# 3. 写回的是字节，不是文本

def test_restore_round_trips_non_utf8_bytes():
    """`read_version` 返回的是 `read_text(errors="replace")` ——
    拿它写回会把认不出的字节静默换成 U+FFFD。恢复那一侧必须走原始字节。"""
    with sandbox() as tmp:
        p = Path(tmp) / "blob.bin"
        original = b"\x00\x01\xff\xfe\x80 raw bytes \xc3\x28\n"
        p.write_bytes(original)

        FH.snapshot_before(SID, str(p), turn=0)      # 留的正是这份原始字节
        p.write_bytes(b"REPLACED")                   # 我们改成别的
        FH.seal_turn(SID, 0)

        plan = FH.plan_restore(SID, before_turn=1)
        n, _lines = FH.apply_restore(SID, plan)

        check("动了一条", n == 1, repr(n))
        check("★★ 逐字节回到了原样（没有被 U+FFFD 替换）",
              p.read_bytes() == original, repr(p.read_bytes()[:40]))
        # 负对照：文本那条路确实会把它改坏 —— 证明这条判据不是空转
        kind, text = FH.read_version(SID, str(p), 1)
        check("（负对照）`read_version` 那条路确实会丢字节",
              kind == "txt" and text.encode("utf-8", "replace") != original,
              repr(text[:20]))


# 4. 没有索引 / 没有留档时的退化

def test_an_empty_or_missing_index_is_just_empty():
    with sandbox():
        check("没索引 -> 空计划（不是崩）", FH.plan_restore(SID, before_turn=1) == [])
        check("空计划 -> 一条都不动", FH.apply_restore(SID, []) == (0, []))
        check("封印没索引的会话 -> 0", FH.seal_turn(SID, 0) == 0)
        check("空 sid -> 一律退化成空", FH.plan_restore("", 1) == []
              and FH.seal_turn("", 0) == 0)


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
