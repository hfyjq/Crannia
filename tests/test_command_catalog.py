"""框架命令的只读目录：名单、说明、出口。

## 它治的病

前端要做「输入 `/` 弹候选」，而候选得有说明。若只有一份没有描述、
也没有对外出口的名单（`core/pipeline/stages/pre_process.py::_FRAMEWORK_COMMANDS`），
前端只能手抄一份，那就是又一次「两套真相源」
（两份都会漂，而两份看着都对）。

落点 = 后端一个只读端点（读名单 + 挂在它旁边的说明表），前端只渲染。
判据：手打 `/xxx` 不从补全里选中，整串当普通文本——
框架命令的判定必须在服务端，客户端补全列表不承担语义。

## 五条断言

1. 目录恰好覆盖「框架命令 ∪ 指令型 skill 命令」（两个方向都查）—— 漏一条 / 多一条都红；
   只比框架那一份名单时，只在 `skills/` 里没有
   指令型 skill 时才对 —— 把外部指令型 skill 拷进 `skills/` 会当场撞红
见 `_expected_catalog_commands` 的 docstring；
2. 每条都有非空说明（新加一条命令忘了写，当场红，这就是它的读者）；
3. `implemented: false` 的那几条两处一致（名字在名单里、说明里也写了"无实现"）；
4. 端点经 HTTP 层真吐得出来（手工调函数 ≠ 生产会走的那条路）；
5. 自造靶子：上面那条"每条都有说明"的检查器自己得能红
   （不变量今天没有反例，判别力寄存于未来）。

用法:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_command_catalog.py

ASCII-only 输出 —— Windows 控制台默认 GBK，emoji 会让整个运行崩掉。
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _catalog():
    from core.pipeline.stages.pre_process import (
        _COMMAND_HELP, _COMMAND_UNIMPLEMENTED, _FRAMEWORK_COMMANDS,
        framework_command_catalog)
    return (_FRAMEWORK_COMMANDS, _COMMAND_HELP, _COMMAND_UNIMPLEMENTED,
            framework_command_catalog())


def _missing_lines(commands, help_map) -> list:
    """哪些命令没有说明。抽出来是为了能让它自己当靶子（第 5 条）。"""
    return sorted(c for c in commands if not (help_map.get(c) or "").strip())


# ------------------------------------------------------------------
# 1 / 2 / 3 —— 表自己
# ------------------------------------------------------------------

def _expected_catalog_commands() -> set:
    """目录应该有哪些 = 框架那十几条 ∪ 指令型 skill 命令。

    只比 `_FRAMEWORK_COMMANDS` 一个集合是错的 —— 那**只在
    "`skills/` 里没有指令型 skill" 时才是对的**。

    把外部指令型 skill 原样拷进 `skills/` 时：目录里多出那条 skill 的命令，
    而旧断言说"多了，红"。

判据不是错的，是旧的 —— 它写于 skill 那半还不存在的时候。
    而它之前一直是绿的，绿的理由却是"那一半恰好是空的"
不是"不变量没有反例"，是"不变量没有对象"。
一个只有把真东西放进去才生效的读者，等于没读过。
    """
    from core.pipeline.stages.pre_process import (
        _instruction_map_for_catalog, _FRAMEWORK_COMMANDS)
    return set(_FRAMEWORK_COMMANDS) | set(_instruction_map_for_catalog())


def test_the_catalog_covers_the_command_set_exactly():
    """两个方向：该有的一条不漏，目录里不许有多的。

    期望集 = 框架命令 ∪ 指令型 skill 命令（见 `_expected_catalog_commands`）。
    """
    _commands, _help, _un, cat = _catalog()
    expected = _expected_catalog_commands()
    got = [e["cmd"] for e in cat]
    assert len(got) == len(set(got)), "目录里有重复项：%r" % got
    assert set(got) == expected, (
        "目录与该有的对不上：\n  少了 %r\n  多了 %r"
        % (sorted(expected - set(got)), sorted(set(got) - expected)))
    print("  [ok] 目录恰好覆盖 %d 条命令（框架 %d + 指令型 skill %d；两个方向都查过）"
          % (len(got), len(_commands), len(expected) - len(_commands)))


def test_every_command_has_a_line():
    """这就是这张表的读者 —— 新加一条命令忘了写说明，这里当场红。"""
    commands, help_map, _un, cat = _catalog()
    missing = _missing_lines(commands, help_map)
    assert not missing, (
        "%d 条命令没有说明（`_COMMAND_HELP` 里得补上）：%r" % (len(missing), missing))
    for e in cat:
        assert e["desc"].strip(), e
    print("  [ok] %d 条命令每条都有说明" % len(cat))


def test_unimplemented_ones_are_marked_in_both_places():
    """不许承诺兑现不了的东西。

    `_COMMAND_UNIMPLEMENTED` 可能是空的 —— 那 4 条
    （`/删除` `/完成` `/全量` `/清空`）是 freenote 的前缀名，
    随 freenote 一起剥走了。
    但这条两向检查仍然承重（不是空转）：
      · 谁新加一条"声明了却没实现"的命令，目录与集合必须同时改；
      · 而"只查一个方向"那个坑还在：
        只查「集合 ⊆ 说明」的话，把集合清空照样全绿 ——
        而"集合被清空"恰恰是这一格最容易出的错。
两个方向都查（下面那几行），空集也要过 `set() == set()` 这一关。
    """
    commands, help_map, un, cat = _catalog()
    assert un <= set(commands), (
        "`_COMMAND_UNIMPLEMENTED` 里有名单外的名字（那是一句谎）：%r"
        % sorted(un - set(commands)))
    # 两个方向都要查：
    #   · 集合里有的，说明必须写着"无实现"；
    #   · 说明里写着"无实现"的，必须在集合里。
    # 只查一个方向时，把集合清空照样全绿 —— 而那正是这条测试要防的事。
    in_desc = {e["cmd"] for e in cat if "无实现" in e["desc"]}
    assert in_desc == set(un), (
        "「未实现」这件事在**两处说法不一致** —— "
        "集合里有而说明没写：%r · 说明写了而集合里没有：%r"
        % (sorted(set(un) - in_desc), sorted(in_desc - set(un))))
    for e in cat:
        assert e["implemented"] is (e["cmd"] not in un), e
    print("  [ok] 未实现的那 %d 条两处方向一致（%s）"
          % (len(un), " ".join(sorted(un))))


# ------------------------------------------------------------------
# 4 —— 出口（经 HTTP 层）
# ------------------------------------------------------------------

def test_the_endpoint_serves_the_catalog():
    """判据：手工调函数 ≠ 生产会走的那条路。

    这一条走真的 Flask 测试客户端 ——
    端点名拼错、忘了登记、返回的不是 JSON，三种坏法只有这一层看得见。
    """
    import web.server as S

    S.app.config["TESTING"] = True
    c = S.app.test_client()
    r = c.get("/api/commands?token=" + S._LAUNCH_TOKEN)
    assert r.status_code == 200, (
        "端点没通（%d）—— 名字拼错了？没注册？" % r.status_code)
    body = r.get_json()
    assert body and body.get("ok") is True, body
    got = [e["cmd"] for e in body["commands"]]
    expected = _expected_catalog_commands()
    assert set(got) == expected, (
        "端点吐的与该有的不是同一份：%r" % (set(got) ^ expected,))
    # 三个字段都要在（前端只渲染，不补算）
    for e in body["commands"]:
        assert set(e) == {"cmd", "desc", "implemented"}, e
    print("  [ok] `GET /api/commands` 经 HTTP 层吐出 %d 条" % len(got))


def test_the_endpoint_is_read_only():
    """只读：POST 不许有副作用（405 = 没为它注册 POST）。"""
    import web.server as S

    S.app.config["TESTING"] = True
    c = S.app.test_client()
    r = c.post("/api/commands?token=" + S._LAUNCH_TOKEN, json={})
    assert r.status_code == 405, (
        "`/api/commands` 竟然收 POST（%d）—— 它该是纯只读的" % r.status_code)
    print("  [ok] 只读（POST -> 405）")


# ------------------------------------------------------------------
# 5 —— 自造靶子
# ------------------------------------------------------------------

def test_the_missing_line_checker_can_fire():
    """自造靶子：第 2 条那个检查器自己得能红。

    不变量今天没有反例，它的判别力寄存于未来 —— 所以现在就要证明
    "真漏了一条说明时，它抓得住"。
    """
    fake_commands = {"/a", "/b"}
    fake_help = {"/a": "有说明"}
    assert _missing_lines(fake_commands, fake_help) == ["/b"], "靶子没打中"
    # 空白字符串不算"有说明"（`"  "` 与 `""` 一样是没写）
    assert _missing_lines({"/c"}, {"/c": "   "}) == ["/c"]
    # 而真有说明时它不许报
    assert _missing_lines({"/d"}, {"/d": "一句话"}) == []
    print("  [ok] 漏说明时检查器会红，有说明时不误报")


# ------------------------------------------------------------------

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
