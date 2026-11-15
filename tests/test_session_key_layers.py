"""两层键的台账读者 —— `ctx.session_id` / `ctx.storage_id` 的每一处读/写都要来报到。

## 它在防什么

会话键分成两层：

    session_key(通道推导) == ctx.session_id == MemoryManager.session_id == 存储 id

拆开之后，存史的要存储键、认通道的要通道键。
两者在不少路径上恰好是同一个串，传错了不会红，这正是高危静默点的成因。

## 这份读者的职责不是"现在就拆"

拆键是 `L` 级、改用户可见行为、且要两个通道同时在跑才验得出来
（功能验证交给用户），它不在这里做。

这份读者做的是前置 ②，而且是它今天唯一可验的那个形态：
把"谁在读这个字段"钉成一份台账，于是
  ① 每新增一处 `ctx.session_id` 都要来改这张表（= 一次有意的决定，不是一次习惯）；
  ② 真动手那天，表里那一列就是待办清单；
  ③ 它把最要命的那个事实摆到脸上：
     这个字段被当成两个键在用 —— 写它的是存储 id，而读它的大量是
     通道作用域的子系统（档位 / 发现集 / 取消 / 日期）。两者相等时，
     没有任何东西会红。

白名单式读者给的"干净"是"我没查到"。
配了自造靶子（`test_the_scanner_can_actually_catch_things`），否则"0 新增"
与"扫描器坏了"长得一模一样。

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_session_key_layers.py
"""

import ast
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_SCAN_DIRS = ("core", "bus", "web", "skills")

#: 台账：`(文件, 函数, "read"/"write") -> (处数, 这一处要哪个键)`。
#:
#: 「通道键」= `/clear` 之后该重新开始的那些（档位 / 发现集 / 取消 / 日期提示）；
#: 「存储键」= `/clear` 之后要留着的那些（消息行 / 笔记 / 重放落库）。
#:
#: 记"处数"而不只记函数名 —— 只记到函数的话，
#: 「在已登记的函数里再加一处」抓不到（变异验过）。
#: 而那个洞正是：白名单给的"干净"是"我没查到"。
#:同一函数里多一处也红，逼人把新那一处单独判一次。
#:
#: 改这张表 = 改一次决定。新加一行红，删一行也红。
_TABLE = {
    ("core/pipeline/stages/agent_loop.py", "process", "session_id", "read"): (7, {
        "通道键": "档位(`spec`)· 日期提示 · 一处日志 · 两处 `agent_loop(...session_id=)` · 重放派发 · 权限解禁 `pin`",
    }),
    #: 轮首把这一段的工作目录钉进上下文。
    #: 它要的是存储键 —— 那一格记录（`session_workspaces`）是按
    #:   存储 id 键的（`/clear` 铸新 id，天然回默认工作区；`/resume` 回旧
    #:   线程，当年选的那个还在）。拿通道键去查会读到上一段的目录
    #:   （两条相等时，这处传错不会红 —— 这正是本文件存在的理由）。
    ("core/pipeline/stages/agent_loop.py", "process", "storage_id", "read"): (1, {
        "存储键": "`session_workspace.pin(ctx.storage_id)` —— 那格记录按存储 id 键",
    }),
    ("core/pipeline/stages/agent_loop.py", "_persist_replay", "storage_id", "read"): (1, {
 "存储键": "`mm.repersist_messages(...)` —— 它写的是**消息行**",
    }),
    # 无主理人群聊的发言层。
    #   三个 `session_id` 读点全是通道键：认群（`group_id_of`）·
    #     档位（`current_spec`）· 工具发现集（`get_active_tool_names`）。
    #   三个 `storage_id` 读点全是存储键：工作目录钉 · 摘要读 · 摘要写。
    #   两个键相等时，传错不会红；这正是这份台账存在的理由。
    ("core/pipeline/stages/group_reply.py", "process", "session_id", "read"): (3, {
        "通道键": "`group_id_of` · `current_spec` · `get_active_tool_names`",
    }),
    ("core/pipeline/stages/group_reply.py", "process", "storage_id", "read"): (4, {
        "存储键": "`session_workspace.pin` · `group_digest.get` · `group_digest.set` · "
                  "`group_minutes.get`（纪要与摘要同键 —— `/clear` 换 id，天然失效）",
    }),
    ("core/pipeline/stages/group_reply.py", "_run_one", "session_id", "read"): (1, {
        "通道键": "`agent_loop(...session_id=)` —— 工具发现集 / 取消 / 档位都认它",
    }),
    # 圆桌那一轮（与 `process` 同一套键的分工）。
    ("core/pipeline/stages/group_reply.py", "_roundtable", "session_id", "read"): (3, {
        "通道键": "`current_spec` · `get_active_tool_names` · `agent_loop(session_id=)`",
    }),
    ("core/pipeline/stages/group_reply.py", "_roundtable", "storage_id", "read"): (1, {
        "存储键": "`group_digest.set`（摘要按**这一段对话**存 —— `/clear` 换 id，天然失效）",
    }),
    # 命令分支已从 `process()` 搬进各自的方法，按新家分开记：
    #   `process` 只剩常规流那 1 处，`/clear` 与 `/compact` 各回各家。
    #   这不是"数目对不上就改账本" —— 是这几处真的换了主人，
    #   而这份账本记的正是"谁在写这两个键"，所以它必须跟着走。
    ("core/pipeline/stages/pre_process.py", "process", "session_id", "write"): (1, {
        "通道键": "常规流那一次 `ctx.session_id = session_key`",
    }),
    ("core/pipeline/stages/pre_process.py", "process", "storage_id", "write"): (1, {
        "存储键": "`ctx.storage_id = sid` —— 与上面**逐一配对**",
    }),
    ("core/pipeline/stages/pre_process.py", "_cmd_clear", "session_id", "write"): (1, {
 "通道键": "`/clear` 给这一轮重新绑键（甲：铸新存储 id，通道键不变）",
    }),
    ("core/pipeline/stages/pre_process.py", "_cmd_clear", "storage_id", "write"): (1, {
        "存储键": "与上面**逐一配对** —— `/clear` 动的就是这一格",
    }),
    ("core/pipeline/stages/pre_process.py", "_cmd_compact", "session_id", "write"): (2, {
        "通道键": "`/compact` 的两条出口（压过 / 没压）都要把键重设回去",
    }),
    ("core/pipeline/stages/pre_process.py", "_cmd_compact", "storage_id", "write"): (2, {
        "存储键": "与上面**逐一配对**",
    }),
    ("core/pipeline/stages/pre_process.py", "_handle_btw", "session_id", "write"): (1, {
        "通道键": "`/btw` 那条受限子调用也要带会话身份",
    }),
    ("core/pipeline/stages/pre_process.py", "_handle_btw", "storage_id", "write"): (1, {
        "存储键": "同上，逐一配对",
    }),
    ("core/pipeline/stages/pre_process.py", "_handle_recap", "session_id", "write"): (1, {
        "通道键": "`/recap` 同上",
    }),
    ("core/pipeline/stages/pre_process.py", "_handle_recap", "storage_id", "write"): (1, {
        "存储键": "同上，逐一配对",
    }),
    # `/resume` 切完线程要把 ctx 的两个键都重设 ——
    #   `session_id` 不变（还是这条通道），`storage_id` 换成切过去那条。
    #这一格正是"两个键分开写"最直白的一处：通道没动、存储换了。
    ("core/pipeline/stages/pre_process.py", "_handle_resume", "session_id", "write"): (1, {
        "通道键": "`/resume` 之后**不变**（切的是线程，不是通道）",
    }),
    ("core/pipeline/stages/pre_process.py", "_handle_resume", "storage_id", "write"): (1, {
        "存储键": "换成切过去那条线程的 id",
    }),
    # `/rewind` 与 `/fork` 共用这一个收尾 ——
    #   它把 `git_status` / 档位冻结丢掉（冷启动）并把 ctx 的两个键重设。
    #   合成一处不是省事：两条命令的收尾必须逐字一样（少做一样就是一个
    #   "换线程了但缓存没丢"的静默洞），写在两个地方就会漂。
    ("core/pipeline/stages/pre_process.py", "_post_thread_switch", "session_id", "write"): (1, {
        "通道键": "换线程之后**不变**（切的是线程，不是通道）",
    }),
    ("core/pipeline/stages/pre_process.py", "_post_thread_switch", "storage_id", "write"): (1, {
        "存储键": "换成新分出来那条线程的 id",
    }),
    # 压缩之后立的那面旗必须记在通道键上。
    #   判据是与读它的人同一个键：`build_plan_reminder` / `plan_mode_active`
    #   收到的就是 `ctx.session_id`（`agent_loop` 传下去的），立旗用别的键
    #   ＝旗子立了没人读得到，而症状是"压缩之后那份提醒偶尔不来"。
    ("core/pipeline/stages/post_process.py", "process", "session_id", "read"): (1, {
        "通道键": "`note_compressed(ctx.session_id)` —— 计划那几格（slug / 回合数 / "
                  "pre_mode / 这面旗）**同一个会话键**，而 `/clear` 清的也是它",
    }),
    # `task.end` 多带一格 `ctx.storage_id`。
    #   判据是读者是谁：红点 / 「排队中」要点在左栏那一行上，而那一行
    #   就是存储 id。通道键在这儿分不出来 —— 私聊在所有通道上塌成
    #   `workspace:private`，带通道键等于"只说有人跑完了一轮，不说哪一段"。
    ("core/pipeline/scheduler.py", "_execute_serial", "storage_id", "read"): (1, {
        "存储键": "`task.end` 带它出去 —— 前端按左栏那一行（存储 id）点红点/排队中",
    }),
}

#: 这份读者盯的两个字段。
_ATTRS = ("session_id", "storage_id")

#: 一句话：把上面那张表的两个词合起来读，就是这条不变量今天的样子 ——
#: 写的是存储 id，读的全是通道作用域的东西。两者今天是同一个串。
_KEY_LAYERS = (
    "★★ 两个键已经分开：`ctx.session_id`"
    "（**通道键**：档位 / 发现集 / 取消 / ICE / 计划 / 待办 / 审批 / 锁）与"
    "`ctx.storage_id`（**存储键**：消息行 / 会话笔记 / 重放落库）"
    "**从 `/clear` 那一刻起是两个不同的串**。-> 传错了从「不会红」变成"
    "「静默地回到上一段会话」那种红法 —— 所以每一处仍然要在台账里判一次。"
)


def _enclosing(tree):
    owner = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for c in ast.walk(n):
                owner[id(c)] = n.name
    return owner


def scan_source(rel, src):
    """一个文件里所有 `ctx.session_id` / `ctx.storage_id` 的读/写点 —— 一处一条。

    返回 `(文件, 函数, 字段, "read"/"write")`。把两个字段都收进来：
    只看 `session_id` 的话，"某处忘了写 `storage_id`"看不见
    （那正是拆键那天最可能的漏法）。

    必须是 list 不是 set：去重之后同一个函数里的两处会塌成一条
"在已登记的函数里再加一处"就抓不到了（变异验过）。
    （去重是一种筛子，而筛掉的东西不会说话。）
    """
    tree = ast.parse(src)
    owner = _enclosing(tree)
    found = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Attribute) and n.attr in _ATTRS
                and isinstance(n.value, ast.Name) and n.value.id == "ctx"):
            kind = "write" if isinstance(n.ctx, ast.Store) else "read"
            found.append((rel, owner.get(id(n), "<module>"), n.attr, kind))
    return found


def scan_all():
    found = []
    for d in _SCAN_DIRS:
        base = _PROJECT_ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            rel = str(p.relative_to(_PROJECT_ROOT)).replace("\\", "/")
            try:
                found += scan_source(rel, p.read_text(encoding="utf-8",
                                                      errors="replace"))
            except SyntaxError:
                continue
    return found


# 1. 台账 == 实际扫描结果

def test_every_read_and_write_of_the_stage_key_is_registered():
    """承重：多一处要解释，少一处要报警。

    新加一处 `ctx.session_id`（无论读还是写），当场红，逼人来这张表里
    写下"它要哪个键"。没有这条，真正拆键那天没人知道该改哪些地方。
    """
    found = scan_all()
    counts = {}
    for site in found:
        counts[site] = counts.get(site, 0) + 1

    missing = sorted(set(counts) - set(_TABLE))  # noqa: E501  (set() 只为查'没登记')
    stale = sorted(set(_TABLE) - set(counts))
    assert not missing, (
        "有 %d 个位置（文件::函数）没登记 —— 每一处都要写明它要哪个键"
        "（通道键 / 存储键），否则拆键那天它就是静默分叉点：\n  %s"
        % (len(missing), "\n  ".join(str(x) for x in missing)))
    assert not stale, (
        "台账里这 %d 行现场已经没有了（改名 / 删了没回来改表）：\n  %s"
        % (len(stale), "\n  ".join(str(x) for x in stale)))

    # 处数也要对上 —— 否则"在已登记的函数里再加一处"溜过去
    #    （变异验过）。见 `_TABLE` 上方的注释。
    for site, (want, _verdict) in sorted(_TABLE.items()):
        got = counts.get(site, 0)
        assert got == want, (
            "%s::%s 的 `ctx.%s` %s 点从 %d 变成了 %d —— "
            "★ **新那一处要单独判一次它要哪个键**，然后把这张表的数改上来"
            % (site[0], site[1], site[2], site[3], want, got))
    print("  [ok] %d 处 `ctx.session_id` / `ctx.storage_id` 全部登记，"
          "且**处数逐个对上**" % sum(counts.values()))


def test_every_write_site_sets_both_layers():
    """写点必须成对。

    每一处写 `ctx.session_id` 的地方，同一函数里必须有一处写 `ctx.storage_id`
    （反之亦然，且处数相等）。

    两者相等时，值一个字节不变；这条钉的是形状 ——
    少了任何一半，拆键那天就会有一个键没被设上，而它的症状是
    "某些子系统悄悄回到上一段会话"，不会红。
    """
    found = scan_all()
    sess_w: dict = {}
    stor_w: dict = {}
    for rel, fn, attr, kind in found:
        if kind != "write":
            continue
        bucket = sess_w if attr == "session_id" else stor_w
        bucket[(rel, fn)] = bucket.get((rel, fn), 0) + 1

    # 负对照的同伴：一处写点都扫不到，扫描器坏了，不是"成对"
    assert sess_w, "一处 session_id 写点都没有 —— 扫描器坏了（别读成通过）"
    only_sess = sorted(set(sess_w) - set(stor_w))
    only_stor = sorted(set(stor_w) - set(sess_w))
    assert not only_sess and not only_stor, (
        "写点没有成对：只写 session_id 的 %r / 只写 storage_id 的 %r"
        % (only_sess, only_stor))
    odd = {k: (sess_w[k], stor_w[k]) for k in sess_w if sess_w[k] != stor_w[k]}
    assert not odd, "同一函数里两边的处数不等（session_id, storage_id）：%r" % (odd,)
    print("  [ok] %d 个写点全部成对（`session_id` <-> `storage_id`）" % len(sess_w))


def test_new_session_goes_through_the_bridge():
    """两个键不再恒等 —— 两层键的本体已经落地。

    判据必须按语义，不能按子串：如果去查
    `or session_key` 这个子串，而 `core/pipeline/scheduler.py` 里
    另一处 `... or session_key_for(event.session)` 恰好也含它，
    删掉 `core/session_pool.py` 里的真货之后，那条断言照样命中 = 假绿。

现在按 AST 判语义，不 grep：`SessionPool.new_session` 里
    必须走那座桥（`bound_storage_id`）取旧线程，不许再拿通道键顶上。
    只有按 AST 才躲得开那条老陷阱 —— 注释里会提到旧写法。
    """
    import ast

    src = (_PROJECT_ROOT / "core" / "session_pool.py").read_text(
        encoding="utf-8", errors="replace")
    fn = None
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.FunctionDef) and n.name == "new_session":
            fn = n
    assert fn is not None, "`SessionPool.new_session` 不见了"

    calls = {n.func.attr for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "bound_storage_id" in calls, (
        "旧线程 id 必须**走那座桥**（`bound_storage_id`）取，"
        "不许再拿通道键顶上 —— 那读到的会是**另一条**线程（换扇门）")
    # 负对照的同伴：真的看得到东西（不然上面那条可能恒真）
    assert "storage_id_for" in calls or "short_term" in {
        getattr(n.func.value, "id", "") or getattr(n.func.value, "attr", "")
        for n in ast.walk(fn) if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)}, (
        "扫描器什么属性访问都没看到 —— 它坏了，别读成通过")
    print("  [ok] `new_session` 走那座桥（**按 AST 判**，不是 grep）")


def test_the_three_fail_open_fallbacks_are_registered():
    """三个兜底不是 bug，是有意的方向；但拆键后 `""` 会同时是
    「没有通道」与「没有存储」，必须逐条重新判一次。

    这条只做普查（它们还在不在），不替它们做决定 ——
    那三条的决定属于拆键本体。
    """
    spots = [
        ("core/request_snapshot.py", "def current_session_id"),
        ("core/pipeline/scheduler.py", "def _session_exists"),
        ("core/workspace.py", "def _projection_is_mine"),
    ]
    for rel, needle in spots:
        src = (_PROJECT_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        assert needle in src, (
            "%s 里的 `%s` 不见了 —— 拆键之前先把这三条兜底重新判一遍"
            "（它们是有意的 fail-open，不是 bug）" % (rel, needle))
    print("  [ok] 三个 fail-open 兜底都在册（拆键时要逐条重判）")


# 2. 负对照 —— 扫描器必须能真的抓到东西

def test_the_scanner_can_actually_catch_things():
    """白名单式读者的自证（它给的"干净"是"我没查到"）。

    没有这条，"0 新增"与"扫描器坏了 / 判据写反了"看起来一模一样。
    """
    found = scan_source("x.py", "def f(ctx):\n    return ctx.session_id\n")
    assert found == [("x.py", "f", "session_id", "read")], found

    wrote = scan_source("y.py", "def f(ctx):\n    ctx.session_id = 'z'\n")
    assert wrote == [("y.py", "f", "session_id", "write")], wrote

    # 第二个字段也要认出来（漏掉它 = 漏掉"忘了写 storage_id"）
    stor = scan_source("s.py", "def f(ctx):\n    ctx.storage_id = 'z'\n")
    assert stor == [("s.py", "f", "storage_id", "write")], stor

    # 同一函数里两处要出两条（去重会让"再加一处"溜过去）
    twice = scan_source(
        "t.py", "def f(ctx):\n    a = ctx.session_id\n    b = ctx.session_id\n")
    assert twice == [("t.py", "f", "session_id", "read"),
                     ("t.py", "f", "session_id", "read")], twice

    # 负对照的负对照：别的东西上的 `session_id` 不许被算进来
    other = scan_source("z.py", "def f(o):\n    return o.session_id\n")
    assert not other, other
    # 注释里提到它也不算（AST 不看注释 —— 这正是它比 grep 强的地方）
    commented = scan_source("c.py", "# ctx.session_id\nX = 1\n")
    assert not commented, commented
    print("  [ok] 扫描器拿合成靶子验过（读/写/别人的字段/注释四个方向）")


# ------------------------------------------------------------------

def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("  FAIL  %s: %s" % (t.__name__, e))
    print("\n%d tests: %d passed, %d failed" % (passed + failed, passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
