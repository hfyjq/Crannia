"""交互模式轴：手动 / 自动 / 计划 / 完全权限。

## 它是哪一根轴

```
模式轴（行为）：手动 ──── 自动 ──── 计划 ──── 完全权限      ← 本模块
投入档：          low … ultra                                ← core/tiers.py，另一根
```

两根轴正交，别塞进同一张表 —— 投入档只允许「只有两格随档」。

| 值 | 含义 |
|---|---|
| `manual` | 默认：写工具该问就问 |
| `plan` | 只读 + 计划文件 + 一次性人工批准 |
| `auto` | 闸 + 分类器（不是"全放行"） |
| `full_access` | 完全权限：审批统统不问而放；`deny` 档（`curl`/`wget`…）与受保护路径照旧硬拒；批量删除仍问一次；ICE 信息层派生关闭 |

## 三条硬约束（都从缓存来）

1. 会话内稳定 —— `current_mode` 冻结，`/clear` 才重读设置；
2. 改档 = 一次冷启动且要出声 —— 模式进提示缓存键，换档 = 这一段的前缀变了；
3. 绝不每请求重算。

依据是一次改档实验：改动后缓存命中率出现 45x 尖峰（已回退）。
进缓存键的东西绝不每请求重算。

## 三条不许回退的规矩

### ① 读不到，默认；读到了但做不到，拒

反例是静默吞掉、以及静默夹取。

### ② 四档现在都能设
> 但 `full_access` 不能当默认档（`default_allowed=False`）——
> 只许 `/mode` 按会话开。

不许让它落进"选了没反应"（「标签在说谎」）——
将来再加一档，先做成 `implemented=False` 列出来，落地那天再翻。

### ③ 不许复用 `agent_mode` 这个名字

那一整条轴（`_get_agent_mode()` + 300 秒 TTL + `quick`/`expert` 两支）
连同 `config.yaml` 的键已删除（孤儿轴 · 与投入档口径冲突 · 与
`## CONVERSATION STYLE` 重复）。`tests/test_identity_prompt.py` 钉着"不许回来"。
本模块的键叫 `default_mode`。

### ④ `/clear` 不清模式

清会话动作不碰模式字段：它会清会话元数据，但进程仍停在同一模式。
本模块不提供 `/clear` 的复位钩子 —— 「我要重新想一遍」
和「我退出计划模式」是两件事，别把它们合成一个动作。
（`/clear` 要清的是计划模式的 plan slug，那是另一格。）
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple
from core.i18n import t as tr


@dataclass(frozen=True)
class ModeSpec:
    """一档模式。每个字段都必填 —— 漏写会 TypeError，不会静默用默认值。"""
    key: str
    name: str            # 中文名（界面直接用，不硬编码）
    desc: str            # 一句话，给设置页与 `/mode` 的输出
    implemented: bool    # `False` = 列得出、设不了（照 `TIERS[6]`）
    #: 能不能作为设置页那个默认档。
    #: `full_access` = 不能 —— 它只许 `/mode` 按会话开，
    #: 否则一次重启就静默落在"不问而放"。
    default_allowed: bool = True


#: 唯一一份模式表。档名从它推（`MODE_NAMES`），不许在别处硬编码。
_MODES: Tuple[ModeSpec, ...] = (
    ModeSpec(
        key="manual", name="手动",
        desc="今天的行为：写工具、执行命令该问就问。",
        implemented=True,
    ),
    ModeSpec(
        key="auto", name="自动",
        desc="闸 + 分类器：确定性拒绝照旧，其余交一次 LLM 判定（不是全放行）。",
        implemented=True,       # 已落地：这一个布尔翻过来了
    ),
    ModeSpec(
        key="plan", name="计划",
        desc="只读：改动只许写计划文件，做完申请一次人工批准。",
        implemented=True,       # 已落地：这一个布尔翻过来了
    ),
    ModeSpec(
        key="full_access", name="完全权限",
        desc=("审批不问而放（写 / 删 / 命令 / 出网都一样）；"
              "`deny` 档与受保护路径仍硬拒，批量删除仍问一次；ICE 信息层关闭。"),
        implemented=True,
        # 只能 `/mode` 按会话开，不许做默认档
        #    （否则一次重启就静默落在"不问而放"）。
        default_allowed=False,
    ),
)

MODES: Dict[str, ModeSpec] = {m.key: m for m in _MODES}

#: 键的清单（给界面派生用）—— 顺序即表序。
MODE_KEYS: Tuple[str, ...] = tuple(m.key for m in _MODES)

#: 中文名（从表里推，不可能漂移）。
MODE_NAMES: Dict[str, str] = {m.key: m.name for m in _MODES}

#: 没写设置时用哪个。
DEFAULT_MODE = "manual"

#: 不是 `agent_mode`（那条轴已删，见模块 docstring ③）。
SETTING_KEY = "default_mode"
_SETTINGS_PATH = Path("data/settings.json")


class ModeUnsupported(RuntimeError):
    """读到了，但做不到 —— 拒绝比静默夹取好（同 `TierUnsupported`）。"""


def _require_key(value) -> str:
    """取值，键。类型判定单独一处（那个"看着对、其实漏"的坑同款）。"""
    if isinstance(value, bool):
        # `isinstance(True, int)` 是 True，但布尔不是模式名
        raise ModeUnsupported("模式不能是布尔值（%r）" % (value,))
    if isinstance(value, str):
        return value
    raise ModeUnsupported("模式必须是字符串，收到 %s" % type(value).__name__)


def mode_of(text: str) -> str:
    """把 `/mode auto` 里的那个词变成键 —— 不合法就拒，不吞错。

    认英文键，也认中文名（`自动` / `计划` / `手动`）。
    它不吞错：`/mode aotu` 必须被拒，而不是静默落回默认
    （「看起来成功了」是最坏的成功）。
    """
    want = (text or "").strip()
    if want in MODES:
        return want
    for m in _MODES:
        if want == m.name:
            return m.key
    raise ModeUnsupported(
        "没有这个模式：%r —— 可选：%s"
        % (want, " / ".join("%s(%s)" % (m.key, m.name) for m in _MODES)))


def validate_mode(value) -> str:
    """设置文件里的值，键。读到了，说出来就必须做到。"""
    key = _require_key(value)
    if key not in MODES:
        raise ModeUnsupported(
            "设置里的模式名不认识：%r（可选：%s）" % (key, ", ".join(MODE_KEYS)))
    return key


def spec_for(key: str) -> ModeSpec:
    return MODES[key]


def is_implemented(key: str) -> bool:
    """这一档现在能不能用。`/mode` 与设置页都拿它当闸（`implemented=False`，拒）。"""
    spec = MODES.get(key)
    return bool(spec and spec.implemented)


def is_default_allowed(key: str) -> bool:
    """这一档能不能当设置页那个默认档。

    `full_access` 不能：它只许 `/mode` 按会话开 —— 默认档会跟着
    "打开新会话 / 重启"静默生效，而"不问而放"正是最不该静默的那一档。
    """
    spec = MODES.get(key)
    return bool(spec and spec.default_allowed)


def configured_mode() -> str:
    """读设置里此刻写着的模式（不冻结，每次都现读）。

    读不到，默认；读到了但做不到，拒（同 `configured_level`）。
    注意"做不到"在这里不检查 `implemented` —— 那是设置页/命令层的闸。
    这里只判"值认不认识"：一个写着 `plan` 而该档还没落地的设置，
    应该是默认按手动跑，不是让每一轮对话都报错。
    （另一根轴（投入档）不同：档 6 未实现是拒，因为它已经在档表里且用户能选到。
    这里的区别是：模式表里的 `implemented=False` 档本来就设不进去。）
    """
    try:
        if not _SETTINGS_PATH.exists():
            return DEFAULT_MODE
        raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or SETTING_KEY not in raw:
            return DEFAULT_MODE
        value = raw[SETTING_KEY]
    except Exception:
        # 文件坏了 / 不是 JSON —— 「读不到」，默认，不许让一轮对话失败
        # （同 `_stream_enabled` 的规矩）。
        return DEFAULT_MODE
    try:
        key = validate_mode(value)
        # 默认档不许是完全权限（手改 JSON 也要挡住）。
        #   判据与设置页那条 400 同源（`is_default_allowed`）——
        #   两处各写一份"哪些能当默认"，迟早分叉。
        if not is_default_allowed(key):
            print("[mode] 设置里的 %s=%r 不能当默认档 —— 这一轮按 %s 跑"
                  "（完全权限只能 /mode 按会话开）" % (SETTING_KEY, value,
                                                      DEFAULT_MODE))
            return DEFAULT_MODE
        return key
    except ModeUnsupported:
        # 值不认识 —— 这是"读到了但做不到"的设置层版本。不抛：
        # 一条坏设置不该让整轮对话失败（与档位那条不同，见上面 docstring）。
        print("[mode] 设置里的 %s=%r 不认识 —— 这一轮按 %s 跑"
              % (SETTING_KEY, value, DEFAULT_MODE))
        return DEFAULT_MODE


_frozen: Dict[str, str] = {}
_freeze_lock = threading.Lock()


def _is_group_key(session_id: str) -> bool:
    """这条会话键是不是群聊（`workspace:group:<id>`）。

    **三种群聊（有主理人 / 无主理人 / 圆桌）一律固定
    「自动」模式**，发送框也不画模式选择器。
    拆键只许走 `core.session_key.group_id_of`（它自己写着"拆键只许这一处"）。
    """
    try:
        from core.session_key import group_id_of
        return bool(group_id_of(str(session_id or "")))
    except Exception:                                  # noqa: BLE001
        return False


def current_mode(session_id: str = "") -> str:
    """本会话的模式 —— 第一次问的时候读一次，之后钉死。

    不要改回"每请求现读"（`configured_mode` 就在旁边，那样写很方便）——
    模式进提示缓存键，那一次 45x 缓存尖峰就是这件事的代价。
    """
    key = session_id or ""
    # 群聊固定 auto —— 不读 `_frozen`、
    #   不冻结、不出声（每次请求都会走到这里，打印会把日志刷爆）。
    if _is_group_key(key):
        return "auto"
    with _freeze_lock:
        mode = _frozen.get(key)
        if mode is None:
            mode = configured_mode()
            _frozen[key] = mode
            print("[mode] 交互模式 = %s（%s）—— 本会话冻结"
                  % (mode, MODE_NAMES.get(mode, mode)))
        return mode


def current_spec(session_id: str = "") -> ModeSpec:
    return spec_for(current_mode(session_id))


def frozen_mode(session_id: str = "") -> Optional[str]:
    """本会话已经冻结的模式；还没冻结就返回 `None`。

    它不冻结、不出声、不动任何状态 —— 唯一的理由是读接口：
    `/api/status` 与 `/api/health` 每 5 秒轮询一次，
    一个读接口不该有副作用（同 `core/tiers.py::frozen_level`）。
    """
    if _is_group_key(session_id or ""):
        return "auto"
    with _freeze_lock:
        return _frozen.get(session_id or "")


def set_mode(session_id: str, key: str) -> str:
    """改本会话的模式（`/mode` 与设置页 POST 都走这一处）。

    当场生效 + 出声：返回一句给用户看的话，说清它是冷启动。
    只动这一个会话 —— 六个通道共用同一个 scheduler，
    全清会顺手把别人那段活会话的模式也换掉。
    """
    if key not in MODES:
        raise ModeUnsupported(tr("没有这个模式：%r") % (key,))
    if not is_implemented(key):
        raise ModeUnsupported(
            tr("「%s」这一档暂不可用，其他档位不受影响。")
            % MODE_NAMES.get(key, key))
    sid = session_id or ""
    # 群聊只有自动这一档。
    #   `auto` 本身是幂等的 —— 返回一句实话，不报错（免得"设成它已经在的那个"也崩）。
    if _is_group_key(sid):
        if key != "auto":
            raise ModeUnsupported(
                tr("群聊固定为「自动」模式，不能切到「%s」。")
                % MODE_NAMES.get(key, key))
        return tr("群聊固定为「自动」模式，无需切换。")
    with _freeze_lock:
        before = _frozen.get(sid)
        _frozen[sid] = key
    # 接管只属于「完全权限」 —— 切走就停。
    #   否则用户切到自动模式之后，那条会话手里还攥着鼠标键盘，而界面上的
    #   「接管」按钮已经不可用了（「标签在说谎」的镜像形状）。
    #   延迟 import + 吞异常：模式切换不能因为接管模块坏了而失败。
    if key != "full_access":
        try:
            from core.computer import session as _computer_session
            if _computer_session.disable_for_session(sid, "mode=%s" % key):
                print("[computer] 模式切到 %s ⇒ 接管已停止" % key)
        except Exception as exc:                     # noqa: BLE001
            print("[computer] 停止接管失败（模式切换照常）：%s: %s"
                  % (type(exc).__name__, exc))
    # 进入 `plan` 时记下从哪来（记下进入之前的那一档）。
    #
    # 失败模式：用户用 `/mode plan` 进去的，退出时却回到了 `manual` ——
    # 而进来前明明是 `auto`。根因：`remember_previous_mode()` 原来只有一个调用点
    # （`core/plan.enter_plan_mode`，即"模型自己调 `enter_plan_mode`"那条路）
    #用户用斜杠命令进去的那条路没记，退出时落回默认档。
    #收到唯一改变模式的地方来：变成 `plan` 那一刻就记。
    # 判据是"`before != plan`" —— 否则退出（`_leave_plan_mode` 也走 `set_mode`）
    # 会把"从哪来"改写成 `plan` 自己（那正好是它要还原掉的值）。
    # 延迟 import（`core/plan.py` 那边也是延迟 import `core.modes`）——
    # 两边都不在模块层互相拉，绕开了循环 import。
    if key == "plan" and before != "plan":
        try:
            from core.plan import remember_previous_mode
            remember_previous_mode(sid, before or configured_mode())
        except Exception as exc:  # noqa: BLE001
            print("[mode] 记 previous_mode 失败（退出时会落回默认档）：%s" % exc)
    if key == "full_access":
        return (tr("已切到「完全权限」。本次对话内审批不再询问；出网命令和受保护路径仍会拦截，批量删除仍会确认一次。"))
    return (tr("已切到「%s」，只对当前对话生效，下一条消息起算。")
            % MODE_NAMES.get(key, key))


def reset_mode_cache(session_id: Optional[str] = None) -> None:
    """丢掉冻结值。`/clear` 有意不调它（见模块 docstring ④）。

    留着它是给两处用的：① 测试；② 将来若真要做"改设置 = 新会话生效"那条路。
    `session_id=None` = 全清（生产目前零调用者 —— 那正是 `forget_discovered`
    那条老坑的形状，所以这里明说：它现在是给测试的）。
    """
    with _freeze_lock:
        if session_id is None:
            _frozen.clear()
        else:
            _frozen.pop(session_id or "", None)


__all__ = [
    "ModeSpec", "MODES", "MODE_KEYS", "MODE_NAMES", "DEFAULT_MODE",
    "SETTING_KEY", "ModeUnsupported",
    "mode_of", "validate_mode", "spec_for", "is_implemented",
    "is_default_allowed",
    "configured_mode", "current_mode", "current_spec", "frozen_mode",
    "set_mode", "reset_mode_cache",
]
