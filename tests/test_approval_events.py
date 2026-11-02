"""审批浮窗的渲染源与回传判定。

## 一条核心规则

浮窗从 `state` 渲染，不从事件重建。

`approval.request` / `approval.resolved` 只是"去重取一次"的失效信号；
真渲染源是 `scheduler.pending_approval(session_key)` → `build_payload`。
理由见 `core/events.py` 的规矩 3：存活性只能来自当前进程的观察。
从事件重建的话，进程重启后日志里那两条会把一个幽灵浮窗复活出来，
而对应的待审批早就不在了。

所以本套里最要紧的两条是：
  * 答过之后 `pending_approval()` 必须返回 `None`（无幽灵）
  * 两个事件名必须注册（否则前端根本收不到"去重取"的信号）

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_approval_events.py
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.session_key import session_key_for  # noqa: E402

# ASCII-only output -- the Windows console defaults to GBK and emoji would
# crash the whole run (and the failure reporter with it).

def _sess():
    """键由 `session_key_for` 统一推导。"""
    from channels.base import MessageSession, MessageType
    return MessageSession("web", MessageType.PRIVATE, "alice")


ALICE = session_key_for(_sess())      # = "workspace:private"
HTML = _PROJECT_ROOT / "web" / "templates" / "chat.html"
SERVER = _PROJECT_ROOT / "web" / "server.py"


def _bare_scheduler():
    """只装那四个 dict 的调度器 —— 不跑构造器（它要 provider/session_pool）。"""
    from core.pipeline.scheduler import PipelineScheduler

    s = object.__new__(PipelineScheduler)
    # 按 `_PENDING_STORES` 建，不手写五格。手写的名单在加
    # `egress`/`command` 之后不会跟着加 —— 于是这两条测试直接
    # `AttributeError`，而生产那边（同一份名单）静默地少了两个 kind。
    # 照名单建，以后加 kind 这两条自动覆盖到。
    from core.pipeline.scheduler import _PENDING_STORES
    for _k, attr in _PENDING_STORES:
        setattr(s, attr, {})
    s._approval_ask_counts = {}
    return s


# ------------------------------------------------------------------
# 1. 渲染源：无幽灵
# ------------------------------------------------------------------

def test_pending_is_none_when_nothing_outstanding():
    """什么都没挂，`None`。这是"无幽灵"的另一半。"""
    s = _bare_scheduler()
    assert s.pending_approval(ALICE) is None
    print("  [ok] 没有待审批时返回 None")


def test_pending_shows_up_and_then_disappears():
    """挂着，有 payload；答过（载体清空），立刻 `None`。

    这条是"无幽灵浮窗"的唯一保证。红了就意味着：
    用户答完之后，浮窗还会从某个地方再冒出来一次。
    """
    s = _bare_scheduler()
    s._pending_cross_zone[ALICE] = {
        "active": True, "stage": "ask", "hint": "报告.docx",
        "request_id": "cz-abc123",
    }
    p = s.pending_approval(ALICE)
    assert p is not None, "挂着的审批没被渲染出来"
    assert p["request_id"] == "cz-abc123"
    assert p["kind"] == "cross_zone"
    assert [o["key"] for o in p["options"]] == ["1", "2", "3", "4"], (
        f"第 1 次该给 4 项：{[o['key'] for o in p['options']]}"
    )

    # 答过之后：`_drop_pending` 把它清成空 dict
    s._pending_cross_zone[ALICE] = {}
    assert s.pending_approval(ALICE) is None, (
        "答过之后还有浮窗 -- 幽灵（渲染源必须是 state，不是事件）"
    )

    # `active=False` 而不是清空 —— 也不该出浮窗
    s._pending_cross_zone[ALICE] = {"active": False, "stage": "done"}
    assert s.pending_approval(ALICE) is None, "active=False 还在渲染浮窗"
    print("  [ok] 答过之后立刻没有浮窗（无幽灵）")


def test_middle_state_asks_for_text_not_options():
    """中间态（等一句话），`expect="text"` + 问题，不给选项。

    这样刷新也能恢复输入框 —— 仍然是单一来源。
    """
    s = _bare_scheduler()
    s._pending_cross_zone[ALICE] = {
        "active": True, "stage": "await_location", "hint": "报告.docx",
        "request_id": "cz-mid",
    }
    p = s.pending_approval(ALICE)
    assert p["expect"] == "text", f"中间态该要文本：{p['expect']!r}"
    assert p["question"], "中间态没给问题文本"
    assert p["options"] == [], f"中间态不该给选项：{p['options']}"
    print("  [ok] 中间态要文本、不给选项")


def test_delete_pending_carries_the_file_list():
    """删除那条的 body 要带文件列表 —— 被审批的文本就是被同意的文本。"""
    s = _bare_scheduler()
    s._pending_delete[ALICE] = {
        "active": True, "stage": "ask",
        "file_paths": ["D:/x/报告A.docx", "D:/x/报告B.docx"],
        "request_id": "del-1",
    }
    p = s.pending_approval(ALICE)
    assert p["kind"] == "delete"
    assert "报告A.docx" in p["body"], f"没带文件列表：{p['body']!r}"
    assert "报告B.docx" in p["body"]
    print("  [ok] 删除的浮窗带上了文件列表")


# ------------------------------------------------------------------
# 1b. 被审批物的列表只有一个排版策略
# ------------------------------------------------------------------

def test_compact_file_list_folds_and_says_how_many():
    """折叠规则本身：≤10 全列；>10 折叠并且明说省了几个。

    判据：「被审批的文本就是被同意的文本，藏掉尾巴等于让用户
    同意他读不到的东西。」，折叠只许显式标注，不许静默丢弃。
    所以这条对"另外 N 个"的那个 N 也要断言，不能只断言"有个省略号"。
    """
    from core.approval import compact_file_list

    ten = ["f%02d" % i for i in range(1, 11)]
    assert compact_file_list(ten) == "\n".join(ten), "10 个不该折叠（边界）"

    twelve = ["f%02d" % i for i in range(1, 13)]
    out = compact_file_list(twelve, total_note="文件 （共 12 个文件，1.0 KB）")
    assert "f05" in out, "前 5 个应当都在"
    assert "f06" not in out, "第 6 个起应当被折叠掉"
    assert "另外 7 个" in out, f"没明说省了几个：{out!r}"
    assert "共 12 个文件" in out, f"没明说总数：{out!r}"
    print("  [ok] compact_file_list：10 不折、12 折成 5 且明说省 7 个")


def test_both_render_paths_produce_the_same_body():
    """同一个删除授权，两条渲染通道必须长得一样。

    若不一致，删 12 个文件时会变成：
      * IM / 文本通道 —— 列 5 个 + 「另外 7 个」（`handler.py` 里那半个 if）
      * WebUI 浮窗 —— 全列 12 个（`scheduler` 自己 `"\\n".join(basenames)`）
    而 `render_delete_body` 的 docstring 写着「调用方拼 `file_list` 时已经这么做」
    —— 对文本那个调用方成立，对这里不成立。

    夹具到不了的条件 = 没测到：若夹具只有 2 个文件（< 10），
    折叠分支根本走不到，而两套实现在 ≤10 时恰好一致。
    """
    import os
    from core.approval import compact_file_list, render_delete_body
    from skills.file_parser.handler import _shorten_name  # noqa: F401  (存在性)

    paths = ["D:/ws/审批测试_%02d.txt" % i for i in range(1, 13)]
    # 工具那侧会拼成什么样（带序号；大小这儿不关心）
    rows = ["  %d. %s" % (i + 1, os.path.basename(p))
            for i, p in enumerate(paths)]
    carried = compact_file_list(
        rows, total_note="文件 （共 12 个文件，1.0 KB）")

    s = _bare_scheduler()
    s._pending_delete[ALICE] = {
        "active": True, "stage": "ask",
        "file_paths": paths,          # 全量（浮窗不许拿它自己拼）
        "file_list": carried,         # 工具拼好的那份
        "request_id": "del-12",
    }
    body = s.pending_approval(ALICE)["body"]

    assert body == render_delete_body(carried), (
        "浮窗的正文不是工具给的那份 -- 又开了第二个渲染源"
    )
    assert "审批测试_12" not in body, (
        f"12 个文件时浮窗把最后一个也列出来了（该折叠）：{body!r}"
    )
    assert "另外 7 个" in body, f"折叠了但没说省几个：{body!r}"

    # 负对照：全量直接 join 的拼法确实会全列
    # 不靠"把源码改回去"，直接算一遍那种拼法 —— 两者必须不同，
    # 否则上面那些断言就是在测一个本来就没坏的东西。
    pre_fix = render_delete_body(
        "\n".join(os.path.basename(p) for p in paths))
    assert "审批测试_12" in pre_fix, (
        "负对照失效：修前的写法居然也不列第 12 个 -- 那这条测试抓的不是这个洞"
    )
    assert pre_fix != body, "两条通道的正文一模一样 -- 这个测试没在测通道差异"
    print("  [ok] 两条通道正文一致；且修前的写法确实会全列（负对照）")


def test_fallback_uses_the_same_policy_on_old_pendings():
    """升级前就挂着的 pending（没有 `file_list`）走兜底 —— 但仍然折叠。

    兜底分支存在的唯一理由是"平滑升级"。它不是第二个策略：
    它调的是同一个 `compact_file_list`，只是少了文件大小。
    """
    s = _bare_scheduler()
    s._pending_delete[ALICE] = {
        "active": True, "stage": "ask",
        "file_paths": ["D:/ws/f%02d.txt" % i for i in range(1, 13)],
        "request_id": "del-old",
    }
    body = s.pending_approval(ALICE)["body"]
    assert "f12.txt" not in body, f"兜底分支没有折叠：{body!r}"
    assert "另外 7 个" in body, f"兜底分支没说省几个：{body!r}"
    print("  [ok] 兜底也走同一个折叠策略（平滑升级，不是第二套）")


# ------------------------------------------------------------------
# 2. build_payload 纯净
# ------------------------------------------------------------------

def test_build_payload_is_pure_and_stable():
    """同一个 `request_id`，同样的 dict（纯函数）。

    这条钉的是"渲染源是 state"：如果它每次调用都变（比如自己生成新的
    `request_id`），那每次轮询都会得到一个"新的待审批"，
    `reply_decision` 的陈旧判定就永远对不上 —— 用户点不中任何东西。
    """
    from core.approval import build_payload

    a = build_payload("cross_zone", 2, body="x", request_id="R1")
    b = build_payload("cross_zone", 2, body="x", request_id="R1")
    assert a == b, f"两次调用结果不同：\n  {a}\n  {b}"
    assert a["request_id"] == "R1", "把传进来的 request_id 丢了"
    print("  [ok] build_payload 纯且按 request_id 稳定")


def test_default_selection_is_never_the_escalation():
    """默认选中永远不是升级项，且升级项排最后。

    两条零成本缓解 —— 第 5 项是疲劳加速器：
    不能在用户最想赶紧点完的时候，把范围最大的选项递过去。
    """
    from core.approval import build_payload

    for idx in (1, 2, 3, 5):
        p = build_payload("cross_zone", idx, request_id="R")
        assert p["default_key"] == "1", f"第 {idx} 次默认选中了 {p['default_key']}"
        esc = [o["key"] for o in p["options"] if o["is_escalation"]]
        if esc:
            assert p["options"][-1]["is_escalation"], "升级项没排最后"
            assert p["default_key"] not in esc, "默认选中了升级项"
    print("  [ok] 默认永不选升级项，且它排最后")


# ------------------------------------------------------------------
# 3. 回传判定
# ------------------------------------------------------------------

def test_reply_decision_matrix():
    """`ok` / `stale` / `bad_choice` 三态。

    `stale` 挡的是双击 / 双发 / 刷新后重放：用户看到了一个已经不存在的
    浮窗。没有这一条，第二下会把一个已经结算的审批再答一次。
    """
    from core.approval import (
        REPLY_BAD_CHOICE, REPLY_OK, REPLY_STALE, reply_decision,
    )

    def call(**kw):
        base = dict(request_id="R1", current_request_id="R1",
                    kind="cross_zone", ask_index=1)
        base.update(kw)
        return reply_decision(**base)

    assert call(choice="1")[0] == REPLY_OK, "正常的 1 没通过"
    assert call(request_id="R0")[0] == REPLY_STALE, "陈旧请求没被挡住"
    assert call(request_id="")[0] == REPLY_STALE, "空 request_id 没被挡住"
    assert call(current_request_id="")[0] == REPLY_STALE, (
        "当前没有待审批时，任何回传都该判陈旧"
    )
    assert call(choice="9")[0] == REPLY_BAD_CHOICE, "没给出的键被收了"
    assert call(choice="5")[0] == REPLY_BAD_CHOICE, (
        "第 1 次问就收下了第 5 项 -- 它这次根本没给出去"
    )
    assert call(choice="5", ask_index=3)[0] == REPLY_OK, "第 3 次该收下 5"
    assert call(choice="", text="D 盘")[0] == REPLY_OK, "中间态的自由文本没收"
    assert call(choice="", text="")[0] == "not_a_choice", "空回传没被拒"
    print("  [ok] reply_decision: ok / stale / bad_choice 三态齐")


# ------------------------------------------------------------------
# 4. 事件注册（前端收不到信号 = 浮窗不出现）
# ------------------------------------------------------------------

def test_approval_events_are_registered():
    """两条事件必须在 `__all__` 且在 `DURABLE_TYPES`。

    不在 `__all__`，别的模块 import 不到；不在 `DURABLE_TYPES`，不落盘，
    "有 request 无 resolved"这个孤儿就检测不出来。
    """
    import core.events as E

    for name in ("EVT_APPROVAL_REQUEST", "EVT_APPROVAL_RESOLVED"):
        assert name in E.__all__, f"{name} 不在 __all__"
        # 查的是常量取到的值，不是常量的名字 ——
        # `DURABLE_TYPES` 装的是 "approval.request" 这种值。
        # 写成 `name in E.DURABLE_TYPES`（拿名字比一集值）会永远为假：
        # 它红了，但红的是这条断言自己写错了。
        value = getattr(E, name)
        assert value in E.DURABLE_TYPES, (
            f"{name}（={value!r}）不在 DURABLE_TYPES 里"
        )
    assert E.EVT_APPROVAL_REQUEST == "approval.request"
    assert E.EVT_APPROVAL_RESOLVED == "approval.resolved"
    print("  [ok] 两条审批事件已注册（__all__ + DURABLE_TYPES）")


def test_every_kind_has_a_store_and_the_window_can_see_it():
    """那张名单有三个读者，各写一份就会分叉。

    加 `egress`/`command` 时，`StageContext`、工具、结算器、调度器的投影
    全都改了 —— 而 `scheduler.py` 里三处硬编码的三元组
    （浮窗渲染源 / 轮首快照 / 轮末信号）若一处没改，症状是：
    聊天里写着「需要你确认执行这条命令 —— 见浮窗」，
    而浮窗永远不出现 —— `GET /api/approval` 返回 `None`。
    没有任何东西报错，只是窗口不出来。

这一条按 `approval._TABLES`（选项表的真相）反查 `_PENDING_STORES`：
    加一个新 kind 而忘了登记，这里立刻红。
    """
    from core.approval import _TABLES
    from core.pipeline.scheduler import _PENDING_STORES
    from core.pipeline.stages.pre_process import _APPROVAL_KINDS

    stores = {k for k, _ in _PENDING_STORES}
    # 命令只剩一种 kind 了，格与 kind 现在是 1:1。
    # 这一条仍然留着 —— 它守的是"格的表"与"选项表"不许分叉。
    known = stores
    missing = sorted(set(_TABLES) - known)
    assert not missing, (
        "这些 kind 有选项表、但 `_PENDING_STORES` 里没有格子：%r\n"
        "=> 后果不是报错，是**浮窗永远不出现**。加一行。" % missing
    )
    # 反向：store 表里的 kind 都得有选项表（否则渲染不出来）
    no_table = sorted(stores - set(_TABLES))
    assert not no_table, "这些格子没有选项表，渲染不出来：%r" % no_table

    # 派发器那张表也要对得上（第四份名单，别让它漂）
    dispatch = {k for k, _ in _APPROVAL_KINDS}
    drift = sorted(dispatch ^ stores)
    assert not drift, (
        "`pre_process._APPROVAL_KINDS` 与 `scheduler._PENDING_STORES` 漂了：%r\n"
        "=> 一个管'认不认用户的回答'，一个管'浮窗看不看得见'。"
        "两处不一致 = 用户答了但窗口还在，或者窗口在但答了没用。" % drift
    )
    # 一格只许一个 kind 用。
    #
    # 这条不变量若只靠注释保着，下一个想"省一格"的人拦不住。
    #
    # 若两个 kind 共用一格（例如 `command_guarded` 曾与 `command` 共用
    # `_pending_command`），"格的键"与"公开的 kind"就是两个东西，每个读者
    # 都得记得去载荷里取；漏掉会依次出现：
    #     ① 浮窗永远不出现（`pending_approval` 用格的键渲染）
    #     ② 失效信号发的是错的 kind
    #     ③ 「不同意」被读成「同意」，且授权发到错的 kind
    #无限审批循环
    # 结论：只要两个 kind 共用一格，这个坑就一直在。
    #
    #那份表格（`_PENDING_STORES`）今天七格七 kind、全不相同，
    #    所以这条不变量成立、可以上闸。红了就说那句：别共用，另开一格。
    _names = [s for _, s in _PENDING_STORES]
    dupes = sorted({s for s in _names if _names.count(s) > 1})
    assert not dupes, (
        "这些格子被**两个以上的 kind 共用**：%r\n"
        "=> 先例在 `core/approval.py:366`（`command_guarded`），它栽了三次，"
        "最重的一次是**「不同意」被读成「同意」-> 无限审批循环**。\n"
        "=> **别共用，另开一格**（新 kind 的代价是四张表各一行，"
        "共用的代价是那三个症状）" % dupes
    )
    print("  [ok] %d 个 kind 都有格子，且两张派发表一致（%s）"
          % (len(_TABLES), ", ".join(sorted(_TABLES))))
    print("  [ok] ★ 一格只许一个 kind 用（%d 格 / %d 个名字，无重复）"
          % (len(_names), len(set(_names))))


def test_public_kind_comes_from_the_payload_not_the_slot():
    """公开的 kind 一律取自载荷（`pend["kind"]`），不是那一格的键。

    若 `command` 与 `command_guarded` 共用 `_pending_command` 一格，而
    三个读者（浮窗渲染源 / 轮首快照 / 轮末信号）都用了格的键，
    「不同意」被读成「同意」+ 无限审批循环。

    那一档现在取消了，格与 kind 是 1:1，**这个具体的错配在生产里不会
    再发生。但那一行代码必须留着**：`_PENDING_STORES` 是"格"的表，
    将来只要有一格装得下两个 kind，同样的形状就会回来。

所以这一条故意制造一个错配（给 `_pending_command` 装一个
    `kind="egress"` 的载荷）来证明：浮窗拿到的是载荷说的那个，
    不是格的键。没有这个错配，这条测试就是恒真的。
    """
    import core.pipeline.scheduler as S

    s = S.PipelineScheduler.__new__(S.PipelineScheduler)
    for _k, attr in S._PENDING_STORES:
        setattr(s, attr, {})
    s._approval_ask_counts = {}
    key = "sess"
    # 故意错配：装在 command 那格里，却自称 egress
    s._pending_command[key] = {
        "active": True, "stage": "ask", "kind": "egress",
        "subject": "example.com", "request_id": "r1", "ask_index": 1,
    }
    p = s.pending_approval(key)
    assert p is not None, "浮窗看不见这一格 —— `_PENDING_STORES` 里漏了它"
    assert p["kind"] == "egress", (
        "浮窗拿到的 kind 是 %r —— 它应当取自**载荷**（egress），"
        "而不是那一格的键（command）。取错了就会拿错选项表，"
        "而今天那个「不同意被读成同意」就是这么来的" % (p["kind"],)
    )
    keys = [o["key"] for o in p["options"]]
    assert keys and keys[0] == "1", (
        "渲染出来的选项表是 %r —— 拿错表了" % keys
    )
    print("  [ok] 公开 kind 取自载荷（故意错配也读对）")


def test_resolved_event_keeps_the_request_kind():
    """`resolved` 的 kind 必须与 `request` 一致。

    若不一致，同一条审批会带着两个 kind 出现在事件流里
    （`approval.request | command_guarded` 与 `approval.resolved | command`）。
    根因：结算时载荷已经被 `pend.clear()` 清空，`pend.get("kind")` 取不到
退到了"格的键"。

修法是轮首把 `(request_id, kind)` 一起拍下来，轮末用它。
    这一条驱动真的 `emit_approval_signals`，造出"载荷已清空"那一刻。

    抽方法的原因和 `persist_turn_carriers` 一样：这段若写在
    `execute()` 里，测试只能够到它的复制品 —— 而"抄一份"正是反复吃亏的形状。
    """
    import core.pipeline.scheduler as S

    class _Bus:
        def __init__(self):
            self.sent = []

        def emit(self, type_, session_key="", **data):
            self.sent.append((type_, data))

    s = S.PipelineScheduler.__new__(S.PipelineScheduler)
    for _k, attr in S._PENDING_STORES:
        setattr(s, attr, {})
    key = "sess"
    bus = _Bus()

    # 轮首：一个 `egress` 的待审批（用 egress 是为了与格的键 `_pending_egress`
    # 无关 —— 这样"退到格的键"不会碰巧相等，断言才有分辨力）
    before = {k: ("r1", "egress") for k, _ in S._PENDING_STORES}
    # 轮末：载荷被结算清空了（`_drop_pending` 就是 `pend.clear()`）
    for _k, attr in S._PENDING_STORES:
        getattr(s, attr)[key] = {}

    s.emit_approval_signals(bus, key, before)
    resolved = [d for t, d in bus.sent if t == S.EVT_APPROVAL_RESOLVED]
    assert resolved, ("resolved 根本没发 —— 载荷清了就该发它（sent=%r）"
                      % (bus.sent,))
    r = resolved[0]
    assert r["request_id"] == "r1", repr(r)
    assert r["kind"] == "egress", (
        "resolved 的 kind 是 %r —— 它应当与 request 的 egress 一致。"
        "退到格的键就是当天那条 21:28 -> 21:29 不一致" % (r["kind"],)
    )
    print("  [ok] resolved 沿用轮首快照的 kind（载荷清空也不漂）")


def test_frontend_listens_for_both_events():
    """前端 `EVT_TYPES` 里必须同时有这两条。

    少一条的症状很隐蔽：浮窗要么永远不出现（少了 request），
    要么答完不消失（少了 resolved）。
    """
    src = HTML.read_text(encoding="utf-8")
    assert "'approval.request'" in src, "前端没订阅 approval.request"
    assert "'approval.resolved'" in src, "前端没订阅 approval.resolved"
    print("  [ok] 前端订阅了两条审批事件")


# ------------------------------------------------------------------
# 5. 机械读（假 DOM 挖出来的洞）
# ------------------------------------------------------------------

def test_socket_exists_and_the_guard_is_not_vacuous():
    """`id="approval-modal"` 必须存在于 HTML 源码里。

    这条是机械读，不是行为断言 —— 因为假 DOM 的 `_mkId` 会
    自动创建任何 id，所以把那个 div 从 HTML 里删掉，前端套件照样绿。
    （同族教训："一条会自己跳过的护栏等于没有护栏"。）

    下面那句"JS 里也确实在用它"是负对照：只断言 `id=` 存在的话，
    一个没人引用的死 div 也能让它变绿。
    """
    src = HTML.read_text(encoding="utf-8")
    # 查的是整个开标签，不是 `id="approval-modal"` 那半截 ——
    # 这附近有一段注释里恰好写着那半截，于是把真的 div 删掉、注释留着，
    # 半截断言照样绿。是负对照（把 div 删掉）抓到的，不是看出来的。
    assert '<div id="approval-modal"' in src, (
        "浮窗的 div 不在 HTML 里 -- 假 DOM 会替它造一个，前端套件发现不了"
    )
    assert "getElementById('approval-modal')" in src, (
        "HTML 里有那个 div，但 JS **从来没引它** -- 那是个死 div，"
        "断言『开标签存在』在这里是空跑的"
    )
    print("  [ok] 浮窗 div 真在 HTML 里，且 JS 真的在用它")


def test_the_modal_is_not_replayed_from_session_storage():
    """浮窗不许进 `sessionStorage`。

    它是瞬时态：重放会造出幽灵浮窗（而 `pending_approval()` 早就返回 None 了）。
    聊天历史可以存，审批不可以 —— 两者在本文件里用的是不同的键。
    """
    src = HTML.read_text(encoding="utf-8")
    for line in src.splitlines():
        if "sessionStorage" in line and "approval" in line.lower():
            raise AssertionError(
                f"浮窗被写进/读出 sessionStorage 了：{line.strip()!r}\n"
                f"  -> 刷新之后会重放出一个幽灵浮窗"
            )
    print("  [ok] 浮窗不进 sessionStorage（瞬时态不重放）")


def test_the_modal_render_path_exists():
    """`refreshApproval` / `approvalKey` / `approvalReply` / `approvalDock`
    四个具名函数必须在，且 `POST /api/approval` 端点已接上。

    必须具名：键盘监听挂在 `document` 上，而夹具里
    `addEventListener` 是空操作 —— 夹具只能直接调这些函数。
    写在匿名回调里 = 前端行为测不到。
    """
    src = HTML.read_text(encoding="utf-8")
    for fn in ("function refreshApproval(", "function approvalKey(",
               "function approvalReply(", "function approvalDock("):
        assert fn in src, f"少了具名函数 {fn} -- 夹具调不到它"
    srv = SERVER.read_text(encoding="utf-8")
    assert '"/api/approval", methods=["GET"]' in srv, "没有 GET /api/approval"
    assert '"/api/approval", methods=["POST"]' in srv, "没有 POST /api/approval"
    print("  [ok] 四个具名函数 + 两个端点都在")


class _FakeCtx:
    """`_check_cross_zone_pending` 只碰这几个属性 —— 鸭子类型就够。"""

    def __init__(self, platform="web", response=""):
        from types import SimpleNamespace
        from core.pipeline.stages.agent_loop import _PENDING_STORE_ATTRS
        self.session = SimpleNamespace(platform=platform)
        self.response = response
        self.messages = []
        # 按名单建，不手写 —— 手写的那份在加 kind 之后不会跟着加，
        #   而症状是"这条分支 AttributeError"（真格的：加 `hook_ask` 时
        #   正是这么发现的）。名单本身由 `_APPROVAL_KINDS` 反查着。
        for attr in _PENDING_STORE_ATTRS:
            setattr(self, attr, {})
        self.approval_ask_counts = {}
        self.user_message = None
        self.approval_replay = {}
        self.metadata = {}


#: 一条最小的「待审批」工具回执（形状取自 `core/approval.pending_payload`）。
_PENDING_TOOL_RESULT = (
    '{"status": "pending_command_approval", "kind": "command", "ask_index": 1, '
    '"body": "Crannia 想执行以下命令：\\n  ls", '
    '"prompt": "Crannia 想执行以下命令：\\n  ls\\n1. 同意\\n2. 不同意\\n回复数字即可。"}'
)


def test_web_channel_never_takes_over_the_model_response():
    """web 上待审批不许占 `ctx.response`。

    那个格子里装的是模型自己的正文。若往里写「…见浮窗。」一行，会把
    一整篇正文内容顶掉：库里原文完好，而用户收到的只有那行指路语。
    更糟的是可能命中的是一条模型自己早就绕过去的陈旧 pending。

web 上什么都不写：浮窗从 `GET /api/approval` 的 state 渲染、
    由 `approval.request` 事件触发，不依赖聊天流里那句话。
    """
    from core.pipeline.stages.agent_loop import _check_cross_zone_pending

    ctx = _FakeCtx("web", "REPORT")
    ctx.messages = [{"role": "tool", "content": _PENDING_TOOL_RESULT}]
    _check_cross_zone_pending(ctx, 0)
    assert ctx.pending_command.get("active") is True, (
        "待审批没被认出来 —— 这条断言会跟着变绿，先修识别"
    )
    assert ctx.response == "REPORT", (
        f"web 通道把模型的正文顶掉了：{ctx.response!r}\n"
        f"  -> 用户会看不到这一轮真正写出来的东西"
    )

    # 负对照（成对，缺一半 = "把所有通道都改成不写"也会全绿）：
    #   非 web 通道没有浮窗，用户唯一的可见处就是这条消息。
    ctx = _FakeCtx("feishu", "REPORT")
    ctx.messages = [{"role": "tool", "content": _PENDING_TOOL_RESULT}]
    _check_cross_zone_pending(ctx, 0)
    assert "1. 同意" in ctx.response, (
        f"非 web 通道没把选项送给用户，他那边看不到任何东西：{ctx.response!r}"
    )
    # 这半边钉的是今天的行为（靠覆盖 `ctx.response` 实现）。
    #    "模型的话 + 菜单"那种更正确的形状在待办里，落地时这条要跟着改。

    # 源级读者：`web_pointer` 不许回来。
    src = (_PROJECT_ROOT / "core" / "pipeline" / "stages"
           / "agent_loop.py").read_text(encoding="utf-8")
    assert "web_pointer" not in src, (
        "`web_pointer` 又被加回来了 —— 先读 `core/approval.py` 里那段墓志铭"
    )
    print("  [ok] web 不占模型正文（非 web 仍印菜单 —— 成对）")


def test_a_pending_is_always_armed_even_if_the_model_kept_working():
    """只要本轮出现过 pending 就必须武装 —— 哪怕模型之后又调了别的工具。

    判据不能写成「绕过去的不武装」：那会造成死锁 ——
    模型说它给了条等待审批的命令，而用户这边什么都没有：

        run_command -> ok      ← 返 pending
        say: "I need your approval to run a quick verification command."
        create_document -> ok  ← pending 之后又调了别的工具
        轮末：没有 `approval.request` 事件，浮窗永远不出现，模型在等，用户看不见

    为什么"只认最后一条 tool 结果"做不到：两个场景逐字同形
    （都是 pending 之后继续调工具、都把活干完了），唯一差别是模型嘴上那句话
    （一个说「我改用内置工具，不需要审批」/ 一个说「我需要你的批准」）
    —— 那是语义，机械判不出来。

判据（fail-open 的方向在这里是"武装"）：
      不武装 = 死锁（用户看不见）；武装 = 多弹一次窗（看一眼 `subject` 就知道，不理它即可）。
    """
    from core.pipeline.stages.agent_loop import _check_cross_zone_pending

    def _pending(subject):
        import json as _j
        return {"role": "tool", "content": _j.dumps(
            {"status": "pending_command_approval", "kind": "command",
             "ask_index": 1, "subject": subject, "body": "x",
             "prompt": "1. 同意\n回复数字即可。"}, ensure_ascii=False)}

    def _tool(name):
        import json as _j
        return {"role": "tool", "content": _j.dumps({"success": True, "t": name})}

    # ① 真在等：pending 就是最后一条 tool 结果
    ctx = _FakeCtx("web", "")
    ctx.messages = [_tool("search_tools"), _pending("echo hi"),
                    {"role": "assistant", "content": "需要你批准"}]
    _check_cross_zone_pending(ctx, 0)
    assert ctx.pending_command.get("active") is True, "真在等的那条没被认出来"
    assert ctx.pending_command.get("subject") == "echo hi"

    # ② 死锁那条形状：pending 之后模型又调了别的工具，然后停 —— 仍须武装
    ctx = _FakeCtx("web", "")
    ctx.messages = [_pending("echo hi"), _tool("create_document"),
                    {"role": "assistant",
                     "content": "The command is waiting for your approval."}]
    _check_cross_zone_pending(ctx, 0)
    assert ctx.pending_command.get("active") is True, (
        "模型**在等**、却没武装 —— 浮窗永远不会出现 = **死锁**\n"
        "    => 判据不许要求「pending 必须是最后一条 tool 结果」（有反馈撞过）"
    )
    assert ctx.pending_command.get("subject") == "echo hi"
    assert ctx.response == "", "★ web 上仍然一个字都不写（`W59` 那半不许被这次撤回带走）"

    # ③ 绕完又撞上：最后那条 pending 才是当下真在等的（A 早作废）
    ctx = _FakeCtx("web", "")
    ctx.messages = [_pending("echo A"), _tool("read_excel_data"), _pending("echo B")]
    _check_cross_zone_pending(ctx, 0)
    assert ctx.pending_command.get("subject") == "echo B", (
        f"武装了作废的那条：{ctx.pending_command.get('subject')!r}"
    )

    # ④ 负对照（缺了它，"干脆永远不武装"也会全绿）：非 web 真在等，菜单必须印
    ctx = _FakeCtx("feishu", "REPORT")
    ctx.messages = [_pending("echo hi")]
    _check_cross_zone_pending(ctx, 0)
    assert "1. 同意" in ctx.response, (
        f"非 web 真在等时没印菜单，用户什么都看不到：{ctx.response!r}"
    )

    print("  [ok] 只要出现过 pending 就武装（含那条死锁现场）")


# ------------------------------------------------------------------
# 5b. 被中和的那条待审批要出声
# ------------------------------------------------------------------

def test_neutralized_pending_is_announced_to_the_user():
    """被挤掉的那条待审批必须在界面上留一行。

    框架只能挂一条（同一条 assistant 消息里的所有 `tool_call`
    都必须拿到结果 —— 少一条上游 400），另一条当场被换成
    `NEUTRALIZED_PENDING_NOTE`。而这件事若只有模型看得见：
    界面上只有一张浮窗，「另一件没执行」全靠模型的嘴在说 ——
    它完全可能不说。

    判据：用户侧的事实不许凭空消失；而且这一条要由框架发 ——
    不是模型的转述。
    """
    import json as _j

    import core.events as E
    from core.pipeline.stages.agent_loop import (
        NEUTRALIZED_PENDING_NOTE, _check_cross_zone_pending,
    )

    class _Bus:
        def __init__(self):
            self.sent = []

        def emit(self, type_, session_key="", **data):
            self.sent.append((type_, data))

    def _pend(call_id):
        m = {"role": "tool", "content": _j.dumps(
            {"status": "pending_command_approval", "kind": "command",
             "ask_index": 1, "subject": "ls", "body": "x",
             "prompt": "1. 同意"}, ensure_ascii=False)}
        if call_id:
            m["tool_call_id"] = call_id
        return m

    # 一条 assistant 消息里两个 `tool_call` 都撞审批
    assistant = {"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "run_command", "arguments": "{}"}},
        {"id": "c2", "type": "function",
         "function": {"name": "send_email", "arguments": "{}"}},
    ]}
    msgs = [assistant, _pend("c1"), _pend("c2")]

    ctx = _FakeCtx("web", "")
    ctx.messages = msgs
    bus = _Bus()
    ctx.events = bus
    _check_cross_zone_pending(ctx, 0)

    # setup：确实发生了中和（否则下面的断言是在空跑）
    neutralized = [m for m in msgs
                   if m.get("role") == "tool"
                   and m.get("content") == NEUTRALIZED_PENDING_NOTE]
    assert neutralized, "setup 不对：没有一条 pending 被中和 —— 先修识别"

    got = [d for t, d in bus.sent if t == E.EVT_TOOL_NEUTRALIZED]
    assert got, (
        "★ 一次中和都没发声 —— 用户侧只有一张浮窗，"
        "「另一件被取消了」全靠模型的嘴"
    )
    assert len(got) == 1, "该只发一条（一次中和），实得 %d" % len(got)
    assert got[0].get("tool") == "run_command", (
        "没说清是**哪一件**没执行：%r" % (got[0],)
    )

    # 负对照：没有中和时一条都不许发（否则"每轮都发一句"也会全绿）
    ctx2 = _FakeCtx("web", "")
    ctx2.messages = [assistant, _pend("c2")]
    bus2 = _Bus()
    ctx2.events = bus2
    _check_cross_zone_pending(ctx2, 0)
    assert not [d for t, d in bus2.sent if t == E.EVT_TOOL_NEUTRALIZED], (
        "负对照失效：没有中和也发了 tool.neutralized"
    )
    print("  [ok] 中和出声（一次一条、带工具名；无中和时不发 —— 成对）")


def test_neutralized_event_is_registered_and_subscribed():
    """新事件要三处齐 —— `__all__` / `DURABLE_TYPES` / 前端 `EVT_TYPES`。

    前端那条最容易漏：`to_sse()` 发的是命名事件，折叠器里加了 `case`
    但 `EVT_TYPES` 没登记，浏览器压根收不到。
    """
    import core.events as E

    assert "EVT_TOOL_NEUTRALIZED" in E.__all__, "不在 __all__"
    assert E.EVT_TOOL_NEUTRALIZED in E.DURABLE_TYPES, "不在 DURABLE_TYPES"
    src = HTML.read_text(encoding="utf-8")
    assert "'tool.neutralized'" in src, "前端没订阅 tool.neutralized"
    print("  [ok] tool.neutralized 三处齐（__all__ / durable / 前端订阅）")


# ------------------------------------------------------------------
# 6. 时序：信号必须在 state 可读之后才发
# ------------------------------------------------------------------

class _AskStage:
    """模拟 `_check_cross_zone_pending` 做的事：设一个待审批，然后停下。"""

    async def process(self, ctx):
        ctx.pending_cross_zone["active"] = True
        ctx.pending_cross_zone["stage"] = "ask"
        ctx.pending_cross_zone["hint"] = "报告.docx"
        ctx.pending_cross_zone["request_id"] = "cz-TIMING"
        ctx.response = "请审批"
        ctx.metadata["stop"] = True
        return ctx


class _ProbeBus:
    """收到审批事件时，当场去读渲染源，看它可不可读。"""

    def __init__(self, sched, key):
        self.sched = sched
        self.key = key
        self.seen: list = []          # [(事件类型, 那一刻 state 可读吗)]

    def emit(self, type_, **kw):
        if str(type_).startswith("approval"):
            self.seen.append((type_, self.sched.pending_approval(self.key) is not None))


class _StubWM:
    """投影与轮末清理要碰的那几样，够跑一轮就行。"""

    def __init__(self):
        self.skip_delete_approval = False
        self.cross_zone_session_auto = False
        # `approval_ask_count` → `approval_ask_counts`（按审批种类分）。
        # 替身必须跟着真类走，否则投影会静默失败。
        self.approval_ask_counts = {}
        self.approval_scope = ""

    def reset_one_time_approval(self): pass
    def reset_delete_approval(self): pass
    def reset_target_grants(self): pass
    def clear_approval_projection(self): pass
    def set_target_session(self, kind, subjects): pass


def test_the_signal_is_emitted_only_after_the_state_is_readable():
    """信号必须在"它让你去读的那份 state"可读之后才发。

    若 `approval.request` 发在 `_check_cross_zone_pending` 里（阶段中），
    而 `self._pending_*` 要到轮末才持久化，前端收到信号立刻
    `GET /api/approval` 时读到的还是空的，浮窗根本不出现。
    结果用户只能手打 "1" 走完审批：功能没坏，浮窗白做了。

这不是"发早了慢一拍"（下次轮询就好了）—— 发早了读到的一定是空的，
    永远错。所以它是一条硬不变量，不是优化。
    """
    import asyncio

    from core.pipeline.scheduler import PipelineScheduler

    class _Pool:
        def get_or_create(self, key, provider):
            return "sid", []
        def new_session(self, key):
            return "sid", []

    sched = PipelineScheduler(session_pool=_Pool(), provider=None, config={})
    bus = _ProbeBus(sched, ALICE)
    sched.events = bus
    sched._stages = [_AskStage()]

    import bus.registry as R
    saved = R.get_workspace_manager()
    R.register_workspace_manager(_StubWM())
    try:
        ev = _event()
        asyncio.run(sched.execute(ev))
    finally:
        R.register_workspace_manager(saved)

    assert bus.seen, (
        "一条审批事件都没发出来 -- 前端永远等不到信号（浮窗不出现）"
    )
    for type_, readable in bus.seen:
        assert readable, (
            f"{type_} 发出来的那一刻，`pending_approval()` 还是 None -- "
            f"前端收到信号立刻重取会拿到空 -> **浮窗不出现**。\n"
            f"  -> 信号必须在**持久化之后**发"
        )
    print(f"  [ok] 审批事件发出时 state 已可读（{len(bus.seen)} 条）")


def _event():
    from channels.base import MessageEvent, MessageType, Message as ChannelMessage, ChannelMetadata
    from channels.message import MessageChain, Plain

    class _S:
        platform = "web"
        message_type = MessageType.PRIVATE
        session_id = "alice"

        def __str__(self):
            return ALICE

    s = _S()
    return MessageEvent(
        session=s,
        message=ChannelMessage(sender_id="u", message_chain=MessageChain([Plain(text="hi")]),
                               sender_name="U", message_type=MessageType.PRIVATE, session=s),
        platform_meta=ChannelMetadata(name="web", description="", platform_type="web"),
    )


# ------------------------------------------------------------------
# 7. 负对照
# ------------------------------------------------------------------

def test_nc_a_dead_div_would_pass_a_naive_id_check():
    """负对照①：只查 `id=` 是空跑的 —— 死 div 也能通过。

    证明上面那条"JS 确实在用它"的半句是承重的。
    """
    dead = '<div id="approval-modal"></div>'
    # 半截判据：引号前没有 `<div `，所以真的 div 和注释一视同仁 ——
    # 这正是半截判据认不出"div 被删了、注释还在"的原因。
    assert 'id="approval-modal"' in dead, (
        "setup 不对：这段假 HTML 本该通过半截判据"
    )
    assert '<div id="approval-modal"' in dead, "整标签判据在这段上也该成立"
    # 而只有注释的那份：半截判据过、整标签判据不过
    comment_only = '<!-- 见 id="approval-modal" 那段说明 -->'
    assert 'id="approval-modal"' in comment_only, "setup 不对"
    assert '<div id="approval-modal"' not in comment_only, (
        "负对照失效：只有注释时整标签判据居然也过 -- 那它挡不住这个洞"
    )
    assert "getElementById('approval-modal')" not in dead, (
        "负对照失效：假 div 里居然有引用 -- 那『死 div 也能过』就不成立"
    )
    print("  [ok] 负对照①：只查 id= 的话死 div 也能过")


def test_answering_the_same_pending_twice_is_refused():
    """连点两次「确认」，模型回了两遍。

    根因是 TOCTOU：`/api/approval` 先读一次 `pending_approval`，再跑那一轮 ——
    而结算发生在那一轮的 pre_process 里（晚一步），两个并发 POST
    都能通过校验、各跑一轮。
修法是"占用"必须是一个原子动作（检查与占用同一把锁）。
    """
    from web.server import _claim_approval, _release_approval

    key = "workspace:private:test-inflight"
    _release_approval(key)
    assert _claim_approval(key) is True, "第一次应该占到"
    assert _claim_approval(key) is False, (
        "第二次没被拒 —— 这就是那次『模型回了两遍』的形状"
    )
    _release_approval(key)
    assert _claim_approval(key) is True, (
        "释放之后必须能再占 —— 否则这条通道的审批从此永久卡死"
    )
    #: 负对照：不同通道互不影响（闸是按通道键的，不是全局一把）。
    assert _claim_approval(key + ":b") is True, "别的通道被这条挡了"
    _release_approval(key)
    _release_approval(key + ":b")

    src = (_PROJECT_ROOT / "web" / "server.py").read_text(encoding="utf-8")
    assert "if not _claim_approval(_key):" in src, (
        "端点在跑那一轮之前没走这道闸"
    )
    print("  [ok] 同一条回答只接一次（占用是原子的）")


def test_nc_an_event_missing_from_durable_would_go_unnoticed():
    """负对照②：把一条从 `DURABLE_TYPES` 里去掉，必须能被发现。

    这里直接构造"少了 resolved 的那一份"，证明上面那条断言不是恒真。
    """
    import core.events as E

    shrunk = frozenset(t for t in E.DURABLE_TYPES
                       if t != E.EVT_APPROVAL_RESOLVED)
    assert E.EVT_APPROVAL_RESOLVED not in shrunk, "setup 不对：没去掉"
    assert E.EVT_APPROVAL_RESOLVED in E.DURABLE_TYPES, (
        "负对照失效：真表里本来就没有它 -- 上面那条断言在骗自己"
    )
    print("  [ok] 负对照②：掉出 DURABLE_TYPES 是可检测的")


def test_hook_ask_is_armed_and_settled():
    """ 第二批：`hook_ask` 载荷武装得上，用户答「1」交回重放。

    两半都在这一条里 —— 只测其中一半会漏掉"浮窗永远不出现"那种形状
    （载荷认不出来，用户看不见；认出来但结算器没登记，答了没用）。
    """
    import json
    from core.pipeline.stages.agent_loop import _check_cross_zone_pending
    from core.pipeline.stages.pre_process import _dispatch_approval

    payload = json.dumps({
        "status": "pending_hook_ask_approval", "kind": "hook_ask",
        "ask_index": 1, "subject": "probe_tool", "tool": "probe_tool",
        "body": "钩子要求先确认：这一步", "prompt": "1. 同意\n2. 不同意",
        "arguments": '{"text": "改过的"}',
    }, ensure_ascii=False)
    ctx = _FakeCtx("web", "")
    ctx.messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_9", "type": "function",
             "function": {"name": "probe_tool", "arguments": '{"text": "原"}'}}]},
        {"role": "tool", "tool_call_id": "call_9", "content": payload},
    ]
    _check_cross_zone_pending(ctx, 0)
    pend = ctx.pending_hook_ask
    assert pend.get("active") is True, "hook_ask 没被认出来 —— 浮窗永远不出现"
    assert pend.get("kind") == "hook_ask", pend
    assert pend.get("call_id") == "call_9", pend
    assert pend.get("arguments") == '{"text": "改过的"}', (
        "★ 钩子改过的参数没被带进挂起载荷 —— 重放会跑模型原来那份，"
        "而用户看到的是改过的那份（正文与实际不一致）"
    )

    # 答「1」，交回重放（不新开一轮），并且不发任何授权
    _dispatch_approval("1", ctx)
    assert ctx.pending_hook_ask == {}, "结算之后挂起状态没清 —— 浮窗会复活"
    rep = ctx.approval_replay or {}
    assert rep.get("name") == "probe_tool" and rep.get("call_id") == "call_9", rep
    assert rep.get("arguments") == '{"text": "改过的"}', rep
    assert "deny" not in rep, rep
    print("  [ok] hook_ask 武装得上、答「1」交回重放（参数是钩子改过的那份）")


# ------------------------------------------------------------------

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
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print("ERROR  " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
