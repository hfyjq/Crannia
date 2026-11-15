"""工作区会话键。

六个通道的私聊是同一条会话（桌面前做一半、走开后用微信接着跟），所以 platform
不进键。群聊各自独立，保留 type 与群 id。落地形式：

    私聊   workspace:private
    群聊   workspace:group:<群 id>

改键有固有代价：旧键（feishu:private:xxx 这一族）仍在 short_term.db 里，新键从空
开始，不做迁移。认领“最近的一条会话”正是要防的那个 bug（/clear 之后接上了别的
通道的对话），宁可从空开始。

本模块只推导键，不持有状态；路由在 core/im_routing.py 与 data/runtime/routing.json。
"""

from __future__ import annotations

# 工作区会话键的命名空间段。
WORKSPACE = "workspace"

# 私聊共用的那一条，它是一条持续会话。
PRIVATE_KEY = "workspace:private"

# 群聊段的前缀。只在此处定义：session_key_for 用它拼，group_id_of 用它拆。
# 两份字符串迟早漂。
GROUP_PREFIX = WORKSPACE + ":group:"


def session_key_for(session) -> str:
    """MessageSession（或其鸭子类型）到会话桶键。

    只读 message_type（枚举或裸字符串都认）与 session_id。platform 有意不读，
    这正是“私聊跨通道同一条会话”的实现。

    session 为 None 时返回 ""：没有会话对象就没有键。这是退化调用的既定契约，
    测试直驱派发器时不造会话，空键让 wm 的属主判定保持原样（owner="" == scope=""）。
    不要把空键当成一条私聊会话，那会凭空造出 workspace:private。
    """
    if session is None:
        return ""
    mtype = getattr(session, "message_type", None)
    mtype = getattr(mtype, "value", mtype)
    mtype = str(mtype or "private")
    sid = str(getattr(session, "session_id", "") or "")
    if mtype == "group":
        return GROUP_PREFIX + sid if sid else WORKSPACE + ":group"
    return PRIVATE_KEY


def group_id_of(key: str) -> str:
    """会话键到群 id；不是群键就返回 ""。session_key_for 的逆。

    第二个读者是工具表：群会话才发 ask_member。
    拆键只许在此处进行。在别处写 key.split(":")[-1] 的话，workspace:private 会拆
    出一个 "private"，看着像群 id，其实不是。

    它不校验群存不存在（那是 core.groups.get 的事），只判形状。
    """
    k = str(key or "")
    if not k.startswith(GROUP_PREFIX):
        return ""
    return k[len(GROUP_PREFIX):]


def is_workspace_private(key: str) -> bool:
    """这条键是不是私聊共用的那一条。"""
    return key == PRIVATE_KEY


def session_key_of_ctx(ctx) -> str:
    """从 StageContext 取会话键，各阶段唯一的取键口。

    路由改派优先（扣住重放的、/c# 指定的，落在 event.extras["routed_session_key"]），
    否则统一推导。新写的阶段不要再各自 str(ctx.session)。
    """
    ev = getattr(ctx, "event", None)
    if ev is not None:
        key = (getattr(ev, "extras", None) or {}).get("routed_session_key")
        if key:
            return str(key)
    return session_key_for(getattr(ctx, "session", None))
