r"""IM 选址 —— 一个渠道的入站消息进哪条会话。

策略：

  1. 如果最近只有一个活跃任务，直接接上去。
  2. 如果 10 分钟内存在其他会话有来自用户的输入/agent 输出，则优先拦截并询问
     「当前 crannia 上存在多个活跃对话：1.xxx，2.xxx……希望接入哪个对话？」。
  3. 用户过一会想跟进另一个，前缀保底，`/c#`。
     想换默认接入，用 `/change #`。

落地成一条纯策略 + 一张绑定表：

```
渠道绑定（routing.json）有，直连它（不再问）
没有绑定：
  近 10 分钟活跃会话 = 0 条，走默认键（私聊 → workspace:private；
                                群聊 → 它自己的 workspace:group:<id>）
  恰好 1 条，直连那一条（"只有一个活跃任务，直接接上"）
  ≥ 2 条，问 + 列编号，并扣住原事件等回答
```

回答 ` /c#`：框架刚问完，设为默认（写进 routing.json，并出 tips）；
其他时候，临时（`/c# 正文` 只影响这一条；裸 `/c#` 把本渠道的临时目标切过去）。
`/change #` = 显式改默认（固定词，走 pre_process 的框架命令表）。

## 几条判据（都是设计里定死的，别改）

  · `/c#` 用正则 `^/c\d+$`，绝不能用前缀 —— `/clear`·`/cancel`·`/compact`
    都是 `/c` 打头。
  · 编号固定：编号表一旦展示给用户就冻结；指向的会话**没了，报错，
    绝不静默接别的**（"猜错比多问一次贵"）。
  · 扣住的是事件对象本身（带 `reply_callback`），回答之后重放它，
    回复天然回原渠道；重放带 `routed_session_key` 标记，防二次拦截。
  · 前端看得见自己在哪个任务，Web/CLI 不拦；Web 有输入，
    作废挂着的编号凭证，但不动渠道默认（默认是用户显式设过的）。
  · 回复头只贴 IM，且只贴"被改派过的"轮次；tips 只在默认真的变了时出
    （每轮都发 = 粘住的旧值伪装成新事实）。

## 状态（模块级，登记在会话级状态登记表里）

  · `_bindings`  —— 渠道 → 默认会话（持久：`data/runtime/routing.json`）；
  · `_pending`   —— 每渠道挂着的一次询问（编号表 + 扣住的事件，内存）；
  · `_numbering` —— 每渠道最后展示过的编号表（内存；`/c#` 不经过询问时靠它）；
  · `_temp`      —— 每渠道的临时目标（内存；只活到进程结束）。
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

#: 绑定表的落点（它是运行时数据，进 `data/runtime/`）。
ROUTING_PATH = Path("data/runtime/routing.json")

#: "活跃"的时间窗 = 10 分钟。
ACTIVE_WINDOW_SECONDS = 600.0

#: 不参与拦截的渠道（前端看得见自己在哪；CLI 是本地直连）。
SKIP_PLATFORMS = frozenset({"web", "cli"})

#: `/c#`：必须整词。带正文的形式是 `/c2 帮我看看`。
_RE_BARE = re.compile(r"^/c(\d+)$")
_RE_PREFIX = re.compile(r"^/c(\d+)\s+(.+)$", re.S)

# 状态（登记表里有这三条；下面 `_lock` 护全部读写）
_bindings: Dict[str, str] = {}
_pending: Dict[str, dict] = {}
_numbering: Dict[str, Dict[int, str]] = {}
_temp: Dict[str, str] = {}
_lock = threading.Lock()
_loaded = False


def _load() -> None:
    """惰性读 routing.json。坏文件，按空表算 + 出声（判据 4）。"""
    global _loaded
    with _lock:
        if _loaded:
            return
        _loaded = True
        try:
            if ROUTING_PATH.exists():
                data = json.loads(ROUTING_PATH.read_text(encoding="utf-8"))
                bindings = data.get("bindings") or {}
                if isinstance(bindings, dict):
                    _bindings.update(
                        {str(k): str(v) for k, v in bindings.items() if v})
        except Exception as exc:  # noqa: BLE001
            print("[routing] routing.json 读不出来，按空表算：%s: %s"
                  % (type(exc).__name__, exc))


def _save_locked() -> None:
    """落盘（调用方持 `_lock`）。失败出声，不抛 —— 绑定丢一条 = 多问一次。"""
    try:
        ROUTING_PATH.parent.mkdir(parents=True, exist_ok=True)
        ROUTING_PATH.write_text(
            json.dumps({"bindings": _bindings}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print("[routing] routing.json 写不进去：%s: %s" % (type(exc).__name__, exc))


# 决策

@dataclass
class Decision:
    """一条入站消息的选址结果。

    `kind`：
      · `"pass"`    —— 不拦（Web/CLI、框架命令、已改派过的重放）；
      · `"direct"`  —— 直连 `key`；
      · `"ask"`     —— 扣住事件，把 `question` 回给用户；
      · `"replay"`  —— 这是一个 `/c#` 回答：把 `event`（扣住的原始事件）
                         按 `key` 重放；`note` 是要贴给用户的一句（可为空）。
    """
    kind: str
    key: str = ""
    question: str = ""
    event: object = None
    note: str = ""


def _looks_like_command(text: str) -> bool:
    """框架命令不参与拦截（它们直接作用于本渠道当前的会话）。

    判据不新造 —— 借 `pre_process._is_framework_command`（惰性 import：
    这个模块在路由热路径上被调用，而 pre_process 那头很重）。
    """
    try:
        from core.pipeline.stages.pre_process import _is_framework_command
        return _is_framework_command(text)
    except Exception:
        return (text or "").strip().startswith("/")


def channel_key_of(session_or_event) -> str:
    """渠道键 = 完整的 `platform:type:session_id`。

    与会话键是两件事（`core/session_key.py`）：
      · 渠道键 —— "这条消息从哪个聊天来"（绑定表 `routing.json` 绑它）；
      · 会话键 —— "这条消息进哪条会话"（私聊跨通道同一条）。
    命名出来是为了让"谁是键"在调用点一眼可读 —— 也让
    `tests/test_session_key.py` 的源级读者分得清这两族。
    """
    s = getattr(session_or_event, "session", session_or_event)
    return str(s or "")


def _channel_key(event) -> str:
    return channel_key_of(event)


def bound_session_key(channel: str) -> Optional[str]:
    """这个渠道当前绑到哪条会话（`/c#` / `/change` 的产物）；没绑给 `None`。

    与 `route()` 分开是刻意的：`route()` 还会看"最近活跃"、并可能问编号，
    而周期任务的选址不许问 —— 问了就是在等一个"到点那一刻不在场的人"。
    """
    _load()
    with _lock:
        return _bindings.get(channel) or None


def _text_of(event) -> str:
    try:
        return str(event.message.content or "").strip()
    except Exception:
        return ""


def _platform_of(event) -> str:
    try:
        return str(getattr(event.session, "platform", "") or "").lower()
    except Exception:
        return ""


def hot_sessions(activity: Dict[str, float], now: float,
                 window: float = ACTIVE_WINDOW_SECONDS) -> List[str]:
    """近 *window* 秒内有动静的会话，按最近活跃排序。"""
    hot = [(k, t) for k, t in activity.items() if (now - t) <= window]
    hot.sort(key=lambda kv: kv[1], reverse=True)
    return [k for k, _ in hot]


def _render_question(numbering: Dict[int, str],
                     title_of: Callable[[str], str]) -> str:
    lines = ["当前 crannia 上存在多个活跃对话："]
    for n in sorted(numbering):
        lines.append("%d. %s" % (n, title_of(numbering[n])))
    lines.append("希望接入哪个对话？回复 `/c<编号>`，之后该渠道默认接入它。")
    return "\n".join(lines)


def _tips() -> str:
    #: 这是给用户看的一句话：不用 Markdown（`**`）、不用内部词
    #: （"接入"/"换默认"），说清两个动作各自会发生什么。
    return ("提示：临时切到其他会话，发 `/c<编号> 你的话`；设为默认，发 `/change <编号>`。")


def route(event, activity: Dict[str, float],
          title_of: Callable[[str], str],
          exists: Callable[[str], bool],
          now: Optional[float] = None) -> Optional[Decision]:
    """一条入站消息的选址。`None` = 不管（调用方照默认键走）。

    顺序承重：Web 先作废凭证 → `/c#` 先于"命令放行"
    （`/c2` 不在框架命令表里，但 `/clear` 在 —— 后者必须直接放过）→
    已改派过的重放直接放过（防二次拦截）。
    """
    _load()
    now = time.monotonic() if now is None else now

    # 框架自己投的事件（周期任务）不进这一层
    #   判据：选址问的两个问题（"哪个渠道来的" / "最近谁在说话"）对一条
    #   到点自动跑的任务都不成立 —— 它的目标由任务自己带着（`extras`），
    #   而"≥2 条活跃，问编号"会让它在没人在场的那一刻去等一个回答。
    #   必须排在下面 `/c#` 那条之前：prompt 是模型写的，
    #   一条以 `/c2 ...` 开头的 prompt 会被当成路由指令（见下面那两行）。
    if (getattr(event, "extras", None) or {}).get("origin") in ("cron", "background"):
        return None

    platform = _platform_of(event)

    # 前端有输入，作废挂着的编号凭证（不动渠道默认）
    if platform in SKIP_PLATFORMS:
        with _lock:
            if _pending:
                _pending.clear()
        return None

    channel = _channel_key(event)
    text = _text_of(event)

    # `/c#`（整词正则）—— 必须在"命令放行"之前判
    m_bare, m_prefix = _RE_BARE.match(text), _RE_PREFIX.match(text)
    if m_bare or m_prefix:
        return _handle_slash_c(event, channel, m_bare or m_prefix, now, exists)

    # 框架命令：不拦（直接作用于本渠道当前会话）
    if _looks_like_command(text):
        return None

    # 已改派过的（扣住重放 / 临时路由），放过（防二次拦截）
    if (getattr(event, "extras", None) or {}).get("routed_session_key"):
        return None

    with _lock:
        binding = _bindings.get(channel)
        temp = _temp.get(channel)

    if temp and exists(temp):
        return Decision("direct", key=temp)
    if binding and exists(binding):
        return Decision("direct", key=binding)

    hot = hot_sessions(activity, now)
    natural = None
    try:
        from core.session_key import session_key_for
        natural = session_key_for(event.session)
    except Exception:
        natural = "workspace:private"

    if len(hot) == 0:
        return Decision("direct", key=natural)
    if len(hot) == 1:
        return Decision("direct", key=hot[0])

    # ≥ 2 条活跃，问 + 列编号（编号就是从现在这一刻冻结）
    numbering = {i + 1: k for i, k in enumerate(hot[:9])}
    with _lock:
        _numbering[channel] = dict(numbering)
        # 扣住事件对象本身 —— 带着 `reply_callback`，回答后重放它，
        # 回复就天然回原渠道。别改存成别的形状（拷一份就没有回调了）。
        _pending[channel] = {"at": now, "numbering": dict(numbering),
                             "event": event}
    return Decision("ask", question=_render_question(numbering, title_of))


def _handle_slash_c(event, channel: str, m, now: float,
                    exists: Callable[[str], bool]) -> Decision:
    """`/c#` 两种语义：刚问完，设默认（+tips）；其他时候，临时。"""
    n = int(m.group(1))
    rest = (m.group(2) or "").strip() if m.re is _RE_PREFIX else ""

    with _lock:
        pend = _pending.pop(channel, None)
        numbering = dict(pend["numbering"]) if pend else dict(
            _numbering.get(channel) or {})

    if not numbering:
        # 没展示过任何编号表，现取一条（当前活跃表）
        return Decision("ask",
                        question="现在没有可选的会话编号。先发一条消息，"
                                 "或等出现多个活跃对话时再看提示。")

    key = numbering.get(n)
    if key is None:
        return Decision("ask",
                        question="编号 %d 不在当前编号表里（现有：%s）。"
                                 % (n, "、".join(str(i) for i in sorted(numbering))))
    if not exists(key):
        # 指向的会话没了，报错，绝不静默接别的
        return Decision("ask",
                        question="编号 %d 指向的会话已不存在，消息不会发出。请重发，或用 `/change <编号>` 重设默认。"
                                 % n)

    if pend is not None:
        # 刚问完：设为默认（写盘）+ 重放扣住的事件 + tips
        with _lock:
            _bindings[channel] = key
            _save_locked()
            held = pend.get("event")
        if held is not None:
            try:
                held.extras["routed_session_key"] = key
                held.extras["routing_header"] = True
            except Exception:
                pass
            return Decision("replay", key=key, event=held, note="#%d（已设为默认）" % n)
        return Decision("direct", key=key, note="#%d（已设为默认）" % n)

    # 其他时候：临时（不写盘）
    if rest:
        # `/c2 正文` —— 把正文当一条消息、路由到 #2（同一条事件，改内容 + 标记）
        try:
            from channels.message import MessageChain, Plain
            event.message.message_chain = MessageChain([Plain(text=rest)])
        except Exception:
            pass
        event.extras["routed_session_key"] = key
        event.extras["routing_header"] = True
        return Decision("pass")
    with _lock:
        _temp[channel] = key
    return Decision("ask",
                    question="已临时接入编号 %d（不会保存）。"
                             % n)


def numbering_for(channel: str) -> Dict[int, str]:
    """这个渠道最后一次展示过的编号表（`/change` 用它决定"编号指什么"）。

    为什么要有这个访问器：`/change` 有两种编号来源（"多会话活跃时问过的那张表"
      与"本渠道的对话列表"），判据只能一处 —— 拿不到就在这里空着，
      调用方据此回落到对话列表。别让调用方自己去摸 `_numbering`。
    """
    with _lock:
        return dict(_numbering.get(channel) or {})


def change_default(channel: str, n: int,
                   exists: Callable[[str], bool]) -> str:
    """`/change #` —— 显式改默认。返回给用户的一句（含 tips）。"""
    with _lock:
        numbering = dict(_numbering.get(channel) or {})
    key = numbering.get(n)
    if key is None:
        return ("更改失败，此编号不存在，现有：%s"
                % ("、".join(str(i) for i in sorted(numbering)) or "无"))
    if not exists(key):
        return "更改失败，此会话已消失"
    with _lock:
        _bindings[channel] = key
        _save_locked()
        _pending.pop(channel, None)
    return ("更改成功，默认接入对话为#%d，"
            "使用/c# 内容 以临时接入其他对话" % n)


def clear_temp(channel: str) -> bool:
    """清掉某渠道的临时目标（`/c0`？—— 先不开放，留给 UI 那批）。"""
    with _lock:
        return bool(_temp.pop(channel, None))


def reset_state() -> None:
    """测试专用：清掉内存态（不碰 routing.json）。"""
    with _lock:
        _pending.clear()
        _numbering.clear()
        _temp.clear()
