"""多 agent 群聊 —— 群的账本。

一个群至多 8 个人；每个人后面是什么模型由用户自己选，
也允许 8 个人都用同一个模型。**存在主理人讲话的情况下，用户和主理人沟通；
不存在的情况下就直接和其他人沟通。**

## 这一片只做「群是什么」这一格

判据：「GUI 上一个聊天窗口 8 个头像就算是一个群聊了」
群 = 一条会话 + 一组成员，不是一个新的持久实体。
本模块落的是成员那一半（谁、什么模型、谁是主理人）；"会话"那一半是现成的
（`core/session_key.py` 早就会把群消息推成 `workspace:group:<群 id>`，见下 `session_key_of`）。

它不派活、不跑模型、不进事件流。
这一片是数据 + 判据，落盘 `data/runtime/groups.json`
（运行数据一律进 `data/runtime/`）。
别把这一片读成"群聊能用了"：它只是"建得出来、存得下、读得回"。

## 六条判据

1. 至多 8 个成员。超了拒，不静默砍
   （静默少一个 = 让用户以为建成了）。
2. 主理人至多 1 个，且可以没有；他占 8 个位之一
   （8 = 1 主理人 + 7 成员）。
3. 名字可自定义，不自定义就 `workerN`。
4. 每个成员一个模型，可为空（空 = 用会话默认）；建群时选、建后可在 GUI 里改。
   这里只存字符串 —— "这个模型配没配过"是配置页那一格的事
   （同一个判据：一处定义），不在这里再判一次。
5. 头像是随机的。这一格存的是
   槽位号（`0..7`），由前端按槽位派生一个纯色 SVG 图形。
   它不是图片路径 —— 别拿它去 `open()`；也别现在就写死"以后一定换成图片"
   —— 槽位号两边都够用（真图来了，`0..7` 照样是键）。
6. `id` 是铸出来的（`g` + 8 位十六进制），不是会话键 ——
   （`/clear` 会复用会话键，拿它当 id 就会读到上一段的群）。

## 与子 agent 的关系

群不是常驻的子 agent（后者匿名、一次性），
群是新的一层（成员注册表 = 本模块）；但「给某成员派一句话」那个动作
复用 `core/subagent.run_child` 那台 fork 机器，不另造一套子调用。
"""

from __future__ import annotations

import json
import os
import random
import tempfile
import threading
import time
import uuid
from pathlib import Path

#: 账本落点。运行数据一律进 `data/runtime/`
#: —— 不许落在 `core/` 下。
LEDGER_PATH = Path("data/runtime/groups.json")

#: 成员数上限。
MAX_MEMBERS = 8

#: 一个用户最多建几个群 —— 当前是 8（从 5 抬上来的）。
#: 这个数将来会动，它必须只有一处
#: （前端从 `GET /api/groups` 拿 `max_groups`，不自己写死一个数）。
MAX_GROUPS = 8

#: 头像槽位个数。与前端 `packages/ui/src/avatar.js::AVATAR_SLOTS` 两边对账
#: （读者 `tests/test_desktop_avatar.py`：少一边就红）。
AVATAR_SLOTS = 8

#: 三段文本的长度上限。超了拒，不截断（静默改一个字 = 改的是用户起的名）。
#: `MAX_NAME` = 20 —— 同一个数管群名与成员名（两处都是"起个名"，
#:   没理由不一样）。
#: `MAX_ROLE` = 500：要的是「一段话说明模型职责」
#:   —— 40 字只够一个短语。前端从 `GET /api/groups` 的 `max_role` 读，不写死。
MAX_NAME, MAX_MODEL, MAX_ROLE = 20, 80, 500

#: 群级 brief（整群一段共同背景）。
#: 放在 system 的那一段（对所有发言人都成立，不破坏前缀缓存），
#: 与成员各自的 `role` 正交：brief 说"我们在干什么"，role 说"你是谁"。
MAX_BRIEF = 1_000

#: 不自定义名字时的默认前缀（判据 3）：`worker1` / `worker2` …
DEFAULT_NAME_FMT = "worker%d"

_LOCK = threading.Lock()
_cache: dict[str, dict] | None = None


# ----------------------------------------------------------------------
# 账本读写（形状照 `bus/mcp_disabled.py` —— 同一族的账本，不另造一套）
# ----------------------------------------------------------------------

def _read() -> dict[str, dict]:
    try:
        raw = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:                                    # noqa: BLE001
        # 账本坏了，当没有群。判据同 `bus/mcp_disabled._read`：
        #   宁可让用户重建一个群，不可凭一个读不动的文件把界面上那 8 个头像
        #   渲染成半截（那看起来像"群还在，只是坏了"）。
        print(f"[groups] 账本读不动（{type(exc).__name__}: {exc}）—— 按空账本走")
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(data: dict[str, dict]) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(LEDGER_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, LEDGER_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load() -> dict[str, dict]:
    """读账本。启动时调一次（与 `quarantine.load` / `mcp_disabled.load` 并排）。"""
    global _cache
    with _LOCK:
        _cache = _read()
    return dict(_cache)


def all_groups() -> dict[str, dict]:
    """当前全部群（懒加载）。返回副本。"""
    global _cache
    if _cache is None:
        return load()
    return dict(_cache)


def get(gid: str) -> dict | None:
    """取一个群。不存在回 `None`（不是空字典 —— 两者在调用方长得一样，
    而"没有这个群"与"这个群是空的"是两件事）。"""
    entry = all_groups().get(str(gid or "").strip())
    return dict(entry) if entry else None


def session_key_of(gid: str) -> str:
    """群 id → 它的会话键。与 `core/session_key.py` 的群聊分支逐字同形
    （`workspace:group:<id>`）。

    这是两处拼同一个键（那一处从 `MessageSession` 推、这一处从群 id 推）
它们是同一个口径的两面，不是两份真相源：`session_key.py` 那半边管
    "一条 IM 群消息属于哪条会话"，这半边管"我们铸的这个群是哪条会话"。
    判据是群 id 铸得出唯一的键，id 必须是我们铸的（判据 6），
    不能用用户输入（"我的群"和别人的"我的群"会撞进同一条会话）。
    """
    gid = str(gid or "").strip()
    return "workspace:group:" + gid if gid else ""


def new_id() -> str:
    """铸一个群 id。判据 6：不是会话键、也不是用户的输入。"""
    return "g" + uuid.uuid4().hex[:8]


# ----------------------------------------------------------------------
# 判据（纯函数 —— 建群 / 改群两条路共用同一套，别各判一次）
# ----------------------------------------------------------------------

class GroupError(ValueError):
    """建群/改群的入参不合法。消息说得出是哪一条判据（够用户改）。

    继承 `ValueError`：端点那边 `except ValueError` 就能回 400，
    不必再 import 这个类（少一处耦合）。
    """


def _text(raw, limit: int, what: str, index: int) -> str:
    """取一段文本。超限拒，不截断 —— 截断改的是用户起的名。"""
    s = str(raw or "").strip()
    if len(s) > limit:
        raise GroupError("第 %d 个成员的%s太长（%d 字 > 上限 %d 字）"
                         % (index + 1, what, len(s), limit))
    return s


def _group_name(raw) -> str:
    """群名。独立一处（不复用成员那个 `_text`）—— 它的报错话术里没有
    "第 N 个成员"那一段，借过来的话超长时会说成"第 0 个成员"，一句错话。"""
    s = str(raw or "").strip()
    if len(s) > MAX_NAME:
        raise GroupError("群名太长（%d 字 > 上限 %d 字）" % (len(s), MAX_NAME))
    return s or "未命名群聊"


def _brief_of(raw) -> str:
    """群级背景。超限拒，不截断（静默砍掉用户写的东西）。"""
    s = str(raw or "").strip()
    if len(s) > MAX_BRIEF:
        raise GroupError("群背景太长（%d 字 > 上限 %d 字）" % (len(s), MAX_BRIEF))
    return s


def _roundtable_of(raw) -> bool:
    """圆桌开关：只认真布尔（同 `host` 那条 —— 这个口子面向我们自己的界面）。

    有主理人就不能开圆桌（圆桌的前提是不选举主理人 ——
    那就没有主理人的概念了），拒，并给路线。
    """
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise GroupError("roundtable 只认 true / false（收到 %r）" % (raw,))
    return raw


def _host_of(raw, index: int) -> bool:
    """`host` 只认真 `bool`（同 `destructive` 那条纪律：`"true"` / `1` /
    `yes` 都不算）。缺省 / `None` = 不是主理人。

    为什么不像 `readonly` 那样"认得出就认"：那个口子面向模型（它会发
    字符串），这个口子面向我们自己的界面（它发的是 JSON 布尔）。
    两处要求不同，判据不同，别统一。
    """
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    raise GroupError("第 %d 个成员的 host 只认 true / false（收到 %r）"
                     % (index + 1, raw))


def _avatar_of(raw, index: int) -> int:
    """头像槽位。不填 = 随机（判据 5）。填了就必须是 `0..7` 的整数。"""
    if raw is None or raw == "":
        return random.randrange(AVATAR_SLOTS)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise GroupError("第 %d 个成员的 avatar 只认整数槽位 0..%d（收到 %r）"
                         % (index + 1, AVATAR_SLOTS - 1, raw))
    if not 0 <= raw < AVATAR_SLOTS:
        raise GroupError("第 %d 个成员的 avatar 槽位越界：%d（只许 0..%d）"
                         % (index + 1, raw, AVATAR_SLOTS - 1))
    return raw


def normalize_members(raw) -> list[dict]:
    """把界面来的成员表判一遍，返回干净的副本。不合规，`GroupError`。

    一处定义：建群与改群都走它 —— 两处各判一次就是"会各自漂"的形状。
    它不改入参（返回新列表），调用方拿到的才是能落盘的那份。
    """
    if not isinstance(raw, list) or not raw:
        raise GroupError("群至少要有一个成员")
    if len(raw) > MAX_MEMBERS:
        raise GroupError("一个群至多 %d 个成员（收到 %d 个）"
                         % (MAX_MEMBERS, len(raw)))

    out: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GroupError("第 %d 个成员不是一个对象（收到 %r）"
                             % (i + 1, type(item).__name__))
        name = _text(item.get("name"), MAX_NAME, "名字", i) \
            or (DEFAULT_NAME_FMT % (i + 1))
        out.append({
            "name": name,
            "model": _text(item.get("model"), MAX_MODEL, "模型名", i),
            "role": _text(item.get("role"), MAX_ROLE, "职责", i),
            "host": _host_of(item.get("host"), i),
            "avatar": _avatar_of(item.get("avatar"), i),
        })

    # 「一个群别出现相同头像的」。
    #   缺省 / 重复的槽位从没用过的那几个里补（`AVATAR_SLOTS == MAX_MEMBERS`，永远补得齐）。
    #   老账本里已有的重复，会在下一次整表保存时被这里去重；只读列表不追溯。
    used: set[int] = set()
    dup: list[int] = []
    for i, m in enumerate(out):
        if m["avatar"] in used:
            dup.append(i)
        else:
            used.add(m["avatar"])
    if dup:
        free_slots = [s for s in range(AVATAR_SLOTS) if s not in used]
        random.shuffle(free_slots)
        for i in dup:
            out[i]["avatar"] = free_slots.pop()

    # 重名拒 —— 界面上两个 `worker1` 分不出谁是谁，而成员是按名字被 @ 的。
    #重名不是美观问题，是功能问题。
    seen: dict[str, int] = {}
    for i, m in enumerate(out):
        if m["name"] in seen:
            raise GroupError("成员名字重复：%r（第 %d 个与第 %d 个）"
                             % (m["name"], seen[m["name"]] + 1, i + 1))
        seen[m["name"]] = i

    # 主理人至多 1 个（判据 2）。 0 个是合法的（"也可以不要讲话的"）——
    #   所以这里的下界是 0，别顺手写成"恰好 1 个"。
    hosts = [m["name"] for m in out if m["host"]]
    if len(hosts) > 1:
        raise GroupError("主理人至多 1 个（收到 %d 个：%s）"
                         % (len(hosts), "、".join(hosts)))

    # 没有主理人，每个成员的职责必填（有主理人时可不填）
    #   —— 判据是拓扑：有主理人时它替团队对外说话、可以自己分工；
    #   没有主理人时，每个人的职责就是唯一的"谁干什么"。
    #   拒绝要点名（拒绝要带路线）—— 只说"职责不能空"，
    #   用户在八个人里找不到是哪一个。
    #   这条只管新建/整表保存；逐个填走 `update_member()` ——
    #   整表规则套在单成员保存上会让无主理人的群永远填不完
    #   （第一次填任何一个都被另外几个空着的拒掉）。
    if not hosts:
        missing = [m["name"] for m in out if not m["role"]]
        if missing:
            raise GroupError(
                "这个群没有主理人，职责不能空：%s —— "
                "展开群条目逐个填写每位成员的职责；"
                "或者给这个群加一个主理人（有主理人时职责可以不填）。"
                % "、".join(missing))
    return out


def missing_roles(group: dict) -> list[str]:
    """职责为空的成员名（不看有没有主理人 —— 那是调用方的事）。

    只此一处数"谁没写职责"：界面角标、主理人提示词、测试都问它，
      别各自再遍历一遍 `members`。
    """
    return [str(m.get("name") or "")
            for m in (group or {}).get("members") or []
            if not str(m.get("role") or "").strip()]


def host_of(group: dict) -> str:
    """这个群的主理人叫什么。没有主理人回 `""` —— 那是合法状态（判据 2），
    调用方据此走"用户直接跟成员说话"那条拓扑。"""
    for m in (group or {}).get("members") or []:
        if m.get("host"):
            return str(m.get("name") or "")
    return ""


# ----------------------------------------------------------------------
# 写
# ----------------------------------------------------------------------

def create(*, name: str = "", members=None, brief="",
           roundtable=False) -> dict:
    """建一个群。不合规，`GroupError`（一个字节都不落盘 —— 同
    `skill_factory` 那条"先体检、后落盘"）。"""
    clean = normalize_members(members)
    rt = _roundtable_of(roundtable)
    if rt and any(m.get("host") for m in clean):
        raise GroupError("有主理人的群不能启用圆桌 —— "
                         "圆桌的前提是**不设主理人**（把 host 去掉再开）。")
    now = time.time()
    gid = new_id()
    entry = {
        "id": gid,
        "name": _group_name(name),
        "members": clean,
        #: 群级背景 + 圆桌开关（老账本里没有，缺省值）。
        "brief": _brief_of(brief),
        "roundtable": rt,
        "created_at": now,
        "updated_at": now,
    }
    global _cache
    with _LOCK:
        data = dict(_cache if _cache is not None else _read())
        #: 群数上限。
        #: 在锁里数、在写之前拒 —— 数在锁外面的话，两次并发的新建
        #:   会各自看到 4 个、各自通过（而这里确实是并发的：WebUI 是
        #:   `waitress threads=12`）。这是本仓库唯一一处必须在临界区里判的
        #:   判据（别的判据都在纯函数里，那些只看入参、与并发无关）。
        #: 也必须是拒，不是"悄悄不建" —— 静默失败 = 用户以为建成了。
        if len(data) >= MAX_GROUPS:
            raise GroupError("最多只能有 %d 个群聊（已经有 %d 个）—— "
                             "先解散一个，或者用现成的那个。"
                             % (MAX_GROUPS, len(data)))
        data[gid] = entry
        _cache = data
        try:
            _write(data)
        except Exception as exc:                                # noqa: BLE001
            # 写不进去也照样算建成了（本次运行内是真的）—— 同
            #   `mcp_disabled.disable` 那条；但要说出来。
            print(f"[groups] 账本写不进去（{type(exc).__name__}: {exc}）"
                  f" —— 本次运行**仍然有**这个群 '{gid}'")
    return dict(entry)


def update(gid: str, *, name=None, members=None, brief=None,
           roundtable=None) -> dict:
    """改一个群（名字 / 成员表 / 群背景 / 圆桌开关）。`KeyError` = 没有这个群。"""
    gid = str(gid or "").strip()
    global _cache
    with _LOCK:
        data = dict(_cache if _cache is not None else _read())
        entry = data.get(gid)
        if not entry:
            raise KeyError(gid)
        next_entry = dict(entry)
        if name is not None:
            next_entry["name"] = _group_name(name)
        if members is not None:
            next_entry["members"] = normalize_members(members)
        if brief is not None:
            next_entry["brief"] = _brief_of(brief)
        if roundtable is not None:
            next_entry["roundtable"] = _roundtable_of(roundtable)
        # 这条判在合并之后 —— 有可能这次只改了 members（加了个主理人），
        #   而圆桌开关是上一次开的：那份旧账同样不成立。
        if next_entry.get("roundtable") and any(
                m.get("host") for m in next_entry.get("members") or []):
            raise GroupError("有主理人的群不能启用圆桌 —— "
                             "圆桌的前提是**不设主理人**（把 host 去掉再开）。")
        next_entry["updated_at"] = time.time()
        data[gid] = next_entry
        _cache = data
        try:
            _write(data)
        except Exception as exc:                                # noqa: BLE001
            print(f"[groups] 账本写不进去（{type(exc).__name__}: {exc}）"
                  f" —— 本次运行**改动仍然生效** '{gid}'")
    return dict(next_entry)


def update_member(gid: str, index: int, *, name: str = "",
                  role=None, model=None) -> dict:
    """只改一个成员的职责/模型，其余原样保留。

    为什么需要它（而不是复用 `update()`）：`update()` 会走
      `normalize_members`，而那里有一条"无主理人，全部职责必填"。
若单成员保存也走整表规则，无主理人的群第一次填任何一个都会失败
      （其他成员还空着），永远填不完。
这条路径有意不套整表规则，只判这一格；整表规则仍在
      `update()`/`create()` 上 —— 两条路各管一件事，不互相拆台。

    `index` 是账本位次（不是界面上的展示顺序；主理人排最前只是展示）。
    `name` 由界面带上，用来挡过期瓦片：两个窗口同时开着时，B 窗口
      按旧位次改人 = 改到别人身上，名字对不上就拒（"刷新页面再改"）。
    `role` / `model` 传 `None` = 不动这一格；传空串 = 清空。
    """
    gid = str(gid or "").strip()
    if isinstance(index, bool) or not isinstance(index, int):
        raise GroupError("成员位次只认整数（收到 %r）" % (index,))
    global _cache
    with _LOCK:
        data = dict(_cache if _cache is not None else _read())
        entry = data.get(gid)
        if not entry:
            raise KeyError(gid)
        members = [dict(m) for m in (entry.get("members") or [])]
        if not 0 <= index < len(members):
            raise GroupError("这个群只有 %d 个成员（收到第 %d 个）—— 刷新页面再试"
                             % (len(members), index + 1))
        cur = members[index]
        want = str(name or "").strip()
        if want and want != str(cur.get("name") or ""):
            raise GroupError("这个群的成员表已经变了（第 %d 个现在是「%s」，不是「%s」）"
                             " —— 刷新页面再改。"
                             % (index + 1, cur.get("name") or "", want))
        if role is not None:
            cur["role"] = _text(role, MAX_ROLE, "职责", index)
        if model is not None:
            cur["model"] = _text(model, MAX_MODEL, "模型名", index)
        # 单成员这一格也要守同一条拓扑判据（不然就等于把规则删了）：
        #   无主理人 + 这一格空着，拒，并点名。
        if not host_of(entry) and not str(cur.get("role") or "").strip():
            raise GroupError("这个群没有主理人，「%s」的职责不能空 —— "
                             "写一句它负责什么；或者给这个群加一个主理人。"
                             % (cur.get("name") or ""))
        members[index] = cur
        next_entry = dict(entry)
        next_entry["members"] = members
        next_entry["updated_at"] = time.time()
        data[gid] = next_entry
        _cache = data
        try:
            _write(data)
        except Exception as exc:                                # noqa: BLE001
            print(f"[groups] 账本写不进去（{type(exc).__name__}: {exc}）"
                  f" —— 本次运行**改动仍然生效** '{gid}'")
    return dict(next_entry)


def delete(gid: str) -> bool:
    """删一个群。返回是不是真的删掉了一个。

    只删账本条目 —— 那条会话的消息行一条都不删（同 `/clear` 的判据：
    留档）。"群没了"之后 `history_search` 还能搜到它说过的话。
    """
    gid = str(gid or "").strip()
    global _cache
    with _LOCK:
        data = dict(_cache if _cache is not None else _read())
        if gid not in data:
            return False
        data.pop(gid, None)
        _cache = data
        try:
            _write(data)
        except Exception as exc:                                # noqa: BLE001
            print(f"[groups] 账本写不进去（{type(exc).__name__}: {exc}）"
                  f" —— 本次运行**仍然删掉了** '{gid}'")
    return True


def reset_for_tests() -> None:
    """清内存缓存。不碰磁盘（同 `mcp_disabled.reset_for_tests`）。"""
    global _cache
    with _LOCK:
        _cache = {}
