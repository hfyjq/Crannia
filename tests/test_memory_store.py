"""记忆底座的存储层读者 —— slug / frontmatter / tombstone / 派生索引 / 检索。

钉住存储层的每一条硬边：

  · slug 是身份（tombstone 与"新建 vs 更新"都按它判）—— 用例写死行为；
  · 上限拒，不静默砍（name/desc/正文）；索引超限截断 + 出声（两种上限都测）；
  · tombstone 挂写路径（删过的 slug 不许重新生成）；账本坏掉出声但视为空；
  · 「写记忆」开关读 `data/settings.json`，读不到/坏掉，默认开；
  · 索引派生：排序确定（slug）、降序无、坏文件跳过且不连累别人；
  · append 是副本语义（原列表一个字不动）。

全程把 `MS.FACTS_ROOT` / `MS.TOMBSTONES_PATH` 钉到临时目录（`_pinned`），
绝不碰生产的 `data/memory/`。开关那条另用临时 cwd 里的 settings.json。

ASCII-only 输出（Windows 控制台默认 GBK）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_memory_store.py
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import core.memory_store as MS  # noqa: E402


@contextlib.contextmanager
def _pinned():
    """facts 根 + tombstone 账本指向临时目录（跑完恢复）。"""
    old_root, old_tob = MS.FACTS_ROOT, MS.TOMBSTONES_PATH
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        base = Path(td)
        MS.FACTS_ROOT = base / "facts"
        MS.TOMBSTONES_PATH = base / ".tombstones.json"
        try:
            yield base
        finally:
            MS.FACTS_ROOT, MS.TOMBSTONES_PATH = old_root, old_tob


def _write_ok(name, desc="一句钩子", body="正文内容"):
    r = MS.write_entry(name, desc, body)
    assert r.get("success"), f"夹具写入失败：{r}"
    return r


# ----------------------------------------------------------------------
# 1. slug —— 身份来源
# ----------------------------------------------------------------------

def test_slugify_rules():
    assert MS.slugify("用户喜欢简洁回答") == "用户喜欢简洁回答"   # CJK 原样保留
    assert MS.slugify("hello world") == "hello-world"
    assert MS.slugify("a/b\\c:d*e?f") == "a-b-c-d-e-f"           # 路径/禁用字符
    assert MS.slugify("  ..名字..  ") == "名字"                   # 首尾点/空白剥掉
    assert MS.slugify("多---折-叠") == "多-折-叠"
    assert len(MS.slugify("很长" * 100)) <= 60
    assert MS.slugify("///") == "" or MS.slugify("") == ""


# ----------------------------------------------------------------------
# 2. 写 / 读 往返
# ----------------------------------------------------------------------

def test_write_then_read_roundtrip():
    with _pinned():
        r = _write_ok("咖啡偏好", "用户爱喝手冲", "不加糖。")
        assert r["created"] is True and r["slug"] == "咖啡偏好"
        assert r["index_line"] == "- [咖啡偏好](咖啡偏好.md) — 用户爱喝手冲"

        e = MS.read_entry("咖啡偏好")
        assert e and e["name"] == "咖啡偏好" and e["body"] == "不加糖。"

        r2 = _write_ok("咖啡偏好", "用户爱喝手冲", "加了燕麦奶。")
        assert r2["created"] is False, "同名写入必须按更新处理"
        assert MS.read_entry("咖啡偏好")["body"] == "加了燕麦奶。"

        # 文件是 frontmatter 的形状（别的读者按这个解析）
        text = (MS.FACTS_ROOT / "workspace" / "咖啡偏好.md").read_text(encoding="utf-8")
        assert text.startswith("---\nname: 咖啡偏好\n")


def test_validation_refuses_and_writes_nothing():
    with _pinned():
        cases = [
            dict(name="", description="d", content="c"),
            dict(name="n", description="", content="c"),
            dict(name="n", description="d", content=""),
            dict(name="n" * (MS.MAX_NAME_CHARS + 1), description="d", content="c"),
            dict(name="n", description="d" * (MS.MAX_DESC_CHARS + 1), content="c"),
            dict(name="n", description="d", content="c" * (MS.MAX_BODY_CHARS + 1)),
            dict(name="两\n行", description="d", content="c"),
        ]
        for kw in cases:
            r = MS.write_entry(**kw)
            assert r["success"] is False and r.get("error"), kw
        root = MS.FACTS_ROOT / "workspace"
        got = list(root.glob("*.md")) if root.exists() else []
        assert got == [], f"被拒的写入居然落了盘：{got}"


# ----------------------------------------------------------------------
# 3. tombstone（#21）
# ----------------------------------------------------------------------

def test_tombstone_refuses_resurrection():
    with _pinned():
        slug = MS.add_tombstone("删过的条目")
        assert slug == "删过的条目"
        r = MS.write_entry("删过的条目", "试图重生", "正文")
        assert r["success"] is False and "删" in r["error"], r
        # 别的名字不受影响
        assert _write_ok("别的条目")["created"] is True
        # 账本是落盘的 JSON
        data = json.loads(MS.TOMBSTONES_PATH.read_text(encoding="utf-8"))
        assert "删过的条目" in data["tombstones"]


def test_corrupt_tombstones_file_is_loud_but_empty():
    with _pinned():
        MS.TOMBSTONES_PATH.parent.mkdir(parents=True, exist_ok=True)
        MS.TOMBSTONES_PATH.write_text("{ 不是 JSON", encoding="utf-8")
        assert MS.load_tombstones() == {}       # 视为空（出声在实现里）
        assert _write_ok("任意")["success"] is True


# ----------------------------------------------------------------------
# 4. 「写记忆」开关（#29）—— 读不到，默认开
# ----------------------------------------------------------------------

def test_write_switch_off_via_settings():
    cwd = os.getcwd()
    with _pinned(), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        os.chdir(td)
        try:
            (Path(td) / "data").mkdir()
            (Path(td) / "data" / "settings.json").write_text(
                json.dumps({"memory_write": False}), encoding="utf-8")
            r = MS.write_entry("关掉时", "d", "c")
            assert r["success"] is False and "关闭" in r["error"], r

            (Path(td) / "data" / "settings.json").write_text(
                "{ 坏 JSON", encoding="utf-8")
            assert _write_ok("坏设置时")["success"] is True, "坏设置必须落回默认（开）"

            (Path(td) / "data" / "settings.json").unlink()
            assert _write_ok("没设置时")["success"] is True
        finally:
            os.chdir(cwd)


# ----------------------------------------------------------------------
# 5. 派生索引（#22 / 纪律 ①②）
# ----------------------------------------------------------------------

def test_index_lines_sorted_and_formatted():
    with _pinned():
        assert MS.build_memory_index() == "", "空库必须回空串（调用方据此跳过注入）"
        _write_ok("乙条目", "描述乙")
        _write_ok("甲条目", "描述甲")
        idx = MS.build_memory_index()
        lines = idx.splitlines()
        assert lines == ["- [乙条目](乙条目.md) — 描述乙",
                         "- [甲条目](甲条目.md) — 描述甲"], lines
        # 排序确定（按 slug 的 unicode 序）：再跑一次逐字节一样
        assert MS.build_memory_index() == idx


def test_index_caps_truncate_both_ways():
    with _pinned():
        for i in range(5):
            _write_ok(f"条目{i}", "说明")
        full = MS.build_memory_index().splitlines()
        assert len(full) == 5

        old_lines = MS.MEMORY_INDEX_MAX_LINES
        old_chars = MS.MEMORY_INDEX_MAX_CHARS
        try:
            MS.MEMORY_INDEX_MAX_LINES = 3
            got = MS.build_memory_index().splitlines()
            assert got == full[:3], "行数上限没截对"
            MS.MEMORY_INDEX_MAX_LINES = old_lines

            MS.MEMORY_INDEX_MAX_CHARS = len(full[0]) + 1
            got2 = MS.build_memory_index().splitlines()
            assert got2 == full[:1], "字符上限没截对"
        finally:
            MS.MEMORY_INDEX_MAX_LINES = old_lines
            MS.MEMORY_INDEX_MAX_CHARS = old_chars


def test_index_skips_broken_files_without_killing_others():
    with _pinned():
        _write_ok("好条目", "好的")
        root = MS.FACTS_ROOT / "workspace"
        (root / "坏的.md").write_text("没有 frontmatter 的裸文件", encoding="utf-8")
        idx = MS.build_memory_index()
        assert "好条目" in idx and "坏的" not in idx.splitlines()[0]
        assert len(idx.splitlines()) == 1


# ----------------------------------------------------------------------
# 6. 检索
# ----------------------------------------------------------------------

def test_search_scoring_limit_and_no_hit():
    with _pinned():
        _write_ok("咖啡记录", "随便写", "正文")
        _write_ok("别的", "关于咖啡", "正文")
        _write_ok("再别的", "随便写", "正文提到咖啡")
        _write_ok("无关", "无关", "无关")

        hits, total = MS.search_entries("咖啡")
        assert total == 4 and [h["name"] for h in hits] == [
            "咖啡记录", "别的", "再别的"], [h["name"] for h in hits]
        assert hits[0]["score"] == 3 and hits[1]["score"] == 2 and hits[2]["score"] == 1

        out = MS.memory_search("咖啡", limit=2)
        assert "命中" in out and "咖啡记录" in out and "别的" in out
        assert "再别的" not in out

        out2 = MS.memory_search("一定会落空的词xyz")
        assert "没有匹配" in out2 and "4" in out2, out2
        assert "没有给 query" in MS.memory_search("")


# ----------------------------------------------------------------------
# 7. 读工具的回执（纯文本 + 带路线）
# ----------------------------------------------------------------------

def test_read_missing_lists_available_and_truncates():
    with _pinned():
        assert "空的" in MS.memory_read("不存在")

        _write_ok("甲", "描述甲")
        _write_ok("乙", "描述乙")
        out = MS.memory_read("不存在")
        assert "甲" in out and "乙" in out and "没有" in out

        # 手改过的超长文件，读侧自裁（写侧上限防不住手改）
        root = MS.FACTS_ROOT / "workspace"
        (root / "超长.md").write_text(
            "---\nname: 超长\ndescription: d\n---\n\n" + "字" * (MS.MAX_READ_CHARS + 500),
            encoding="utf-8")
        got = MS.memory_read("超长")
        assert "已截断" in got and len(got) < MS.MAX_READ_CHARS + 200, len(got)


# ----------------------------------------------------------------------
# 8. append 的副本语义
# ----------------------------------------------------------------------

def test_append_is_copy_and_skips_empty():
    msgs = [{"role": "user", "content": "喂"}]
    assert MS.append_memory_index(msgs, "") is msgs, "空索引必须原样返回"

    out = MS.append_memory_index(msgs, "- [甲](甲.md) — 描述")
    assert out is not msgs and msgs[0]["content"] == "喂", "原列表被改了"
    assert "<memory_index>" in out[0]["content"]
    assert out[0]["content"].count("</memory_index>") == 1

    # 富文本链（非 str content）不动它
    rich = [{"role": "user", "content": ["富文本"]}]
    assert MS.append_memory_index(rich, "x") is rich


# ----------------------------------------------------------------------

def test_delete_seals_by_default_and_can_be_switched_off():
    """删除的两种语义：默认落封条；全局开关关掉后不落。

    封条挡的是"后台抽取器把删掉的事实再生成回来"；关掉它等于放弃那条保障。
    """
    cwd = os.getcwd()
    with _pinned(), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        os.chdir(td)
        try:
            (Path(td) / "data").mkdir()
            #: 默认（没有设置文件），封条照落。
            _write_ok("默认封条")
            r = MS.delete_entry("默认封条")
            assert r["success"] and r.get("sealed") is True, r
            assert MS.slugify("默认封条") in MS.load_tombstones(), "默认应落封条"

            #: 显式关掉，不落；返回里的 `sealed` 要是 False（回执不许说谎）。
            _write_ok("不封条")
            r2 = MS.delete_entry("不封条", seal=False)
            assert r2["success"] and r2.get("sealed") is False, r2
            assert MS.slugify("不封条") not in MS.load_tombstones(), "关掉后不该落封条"

            #: 设置里关掉，与 `seal=False` 同一条路。
            (Path(td) / "data" / "settings.json").write_text(
                json.dumps({"memory_auto_seal": False}), encoding="utf-8")
            _write_ok("设置关掉")
            r3 = MS.delete_entry("设置关掉")
            assert r3["success"] and r3.get("sealed") is False, r3

            #: 坏设置，落回默认（开）。
            (Path(td) / "data" / "settings.json").write_text("{坏", encoding="utf-8")
            _write_ok("坏设置默认开")
            r4 = MS.delete_entry("坏设置默认开")
            assert r4["success"] and r4.get("sealed") is True, r4
        finally:
            os.chdir(cwd)


def test_memory_delete_tool_wraps_the_same_action():
    """模型口与界面口同一个动作（回执带一句人话说明后果）。"""
    with _pinned():
        _write_ok("要被删的")
        r = MS.memory_delete("要被删的")
        assert r["success"] and "已删除" in r.get("note", ""), r
        assert MS.read_entry("要被删的") is None
        miss = MS.memory_delete("不存在的名字")
        assert miss["success"] is False, miss
        empty = MS.memory_delete("")
        assert empty["success"] is False and "name" in empty["error"], empty


def test_concurrent_write_never_yields_a_torn_read():
    """写与读并发时，读者绝不许看到半截文件。

    为什么这条是真需求（不是假想）—— 后台抽取 fork 与用户轮真会重叠
    （1.44s 窗口），而 `build_memory_index()` 在每个用户轮开头扫所有条目、
    `memory_search` 也随时在扫。若写法是 `open(path,"w")`（先截断再写）
并发读者看到半截，`parse_entry` 对坏文件返回 `None`
那条记忆这一轮静默消失（不报错、不留痕）。

    实测（写循环 × 并发读，3 秒）：坏读 503/9448 = 5.32%；
    正确实现下 0。

    这条断言是"零容忍"：一有坏读就红。它的牙齿同时钉住了那个
    Windows 陷阱：用"临时文件 + `os.replace`"在 Windows 上更糟（9.3% 坏读），
    因为 `MoveFileEx` 碰不开着的读句柄，见 `core/memory_store.py::_IO_LOCK`
    的注释（那条路别再试第二次）。
    """
    with _pinned():
        import contextlib as _ctx
        import io as _io
        import threading as _th
        import time as _tm

        target = MS.FACTS_ROOT / "workspace" / "probe.md"
        big = "正文" * 1500
        stop = _th.Event()
        seen = {"reads": 0, "torn": 0}

        def _reader():
            while not stop.is_set():
                seen["reads"] += 1
                if MS.parse_entry(target) is None:
                    seen["torn"] += 1

        _write_ok("probe", body="warm")
        t = _th.Thread(target=_reader, daemon=True)
        t.start()
        try:
            # 写那条会打印一行 —— 压掉它，否则几千行噪声盖住报告
            with _ctx.redirect_stdout(_io.StringIO()):
                #: 样本量不许绑在墙上时钟上：写固定 0.6 秒在
                #:   16 路并行下会假红 —— 实测读者只拿到
                #:   191 / 194 / 197 / 200 次（另一次 200 刚好卡在线上），
                #:   于是下面那条活性守卫报警「夹具没跑起来」。
                #:   守卫本身是对的（它拒绝空跑 —— 看着它绿之前，
                #:   先确认它本来是绿的）。错的是把样本量绑在墙上时钟上：
                #:   机器一忙，读者拿不到 CPU，样本就少，而"读者没跑够"被报成了
                #:   "夹具坏了"。
                #:判据是「写者一直写，直到读者攒够样本，或撞硬上限」——
                #:     判据（`> 200` 与 `torn == 0`）一个字都没动，
                #:     变的只是"跑多久"。
                t0 = _tm.time()
                while seen["reads"] <= 200 and _tm.time() - t0 < 5.0:
                    MS.write_entry("probe", "一句钩子", big)
        finally:
            stop.set()
            t.join(timeout=2)

        #: 硬上限那条路仍然照红（写满 5 秒还读不到 200 次，机器是真跑不动，
        #:   那这条断言确实会空跑，必须报而不是混过去）。
        assert seen["reads"] > 200, (
            "只读了 %d 次 —— 夹具没跑起来，这条断言会**空跑**" % seen["reads"])
        assert seen["torn"] == 0, (
            "%d/%d 次读到半截/空文件 —— 并发写没串起来，那条记忆会静默消失"
            % (seen["torn"], seen["reads"]))
        # 临时文件不许残留在记忆目录里
        left = [f.name for f in (MS.FACTS_ROOT / "workspace").iterdir()
                if ".tmp" in f.name]
        assert left == [], "原子写的临时文件没清干净：%r" % left


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
            print("ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
