"""接管的硬拒绝层。

从坐标/键码识别"意图"做不到 —— 这是本模块的第一条判据。
能确定性做到的只有目标级拒绝：

| 层 | 判据 |
|---|---|
| 应用黑名单 | 按 exe 名 / 窗口类 / 窗口标题拒：终端、登录/UAC、密码管理器、安全设置 |
| 按键黑名单 | Windows 键 · `Ctrl+Alt+Del` · 急停键本身（纵深：即使钩子被摘掉也按不出来） |
| 锁屏 / 桌面切换 | `OpenInputDesktop` 拿不到，停 |
| 前台身份 | 每次注入前校验目标仍是前台；变了就中止（不盲打） |
| 完整性级别 | 目标比我们高（提权窗口），提前说"够不到"，不试（UIPI 会静默失败） |

命中就拒绝，没命中不等于安全。动作级的高危确认是另一层，
    而且是启发式 —— 它不在这里，也不改变这里的任何一条。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import win32


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:                      # 允许 `if decision:`
        return self.ok


OK = Decision(True, "")


def deny(reason: str) -> Decision:
    return Decision(False, reason)


# 应用黑名单
#: 判据是"这个目标一旦被自动化，这一下就可能让围栏失效或被对方钓鱼"。
#: 终端是第一条 —— 因为终端里的任意命令会绕开整道围栏。
DENY_EXES: frozenset[str] = frozenset({
    # 终端 / shell
    "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "wt.exe",
    "conhost.exe", "openconsole.exe", "mintty.exe", "bash.exe", "wsl.exe",
    "wslhost.exe", "ubuntu.exe", "debian.exe",
    # 登录 / UAC / 锁屏
    "logonui.exe", "lockapp.exe", "consent.exe", "credentialuibroker.exe",
    "useraccountcontrolsettings.exe", "wuaudt.exe",
    # 密码管理器
    "keepass.exe", "keepassxc.exe", "1password.exe", "bitwarden.exe",
    "lastpass.exe", "dashlane.exe", "keeper.exe", "roboform.exe",
    # Windows 安全 / 系统设置 / 注册表 / 任务管理器
    "securityhealthui.exe", "msascui.exe", "mpcmdrun.exe", "windowsdefender.exe",
    "systemsettings.exe", "regedit.exe", "taskmgr.exe", "mmc.exe",
    "secpol.msc", "gpedit.msc", "services.msc", "eventvwr.exe",
})

#: 窗口类层面的补充（exe 可能藏在 `ApplicationFrameHost` 后面）。
DENY_CLASSES: frozenset[str] = frozenset({
    "credentialuibroker_win32class",
    "windows.ui.core.corewindow",           # UWP 宿主：按 exe 判不出来时保守拒
})

#: 标题里出现这些 且 是对话框类，拒。"运行"框是典型。
DENY_TITLE_WORDS: tuple[str, ...] = (
    "运行", "run", "用户账户控制", "user account control",
    "windows 安全", "windows security", "windows defender",
)


def _title_looks_dangerous(info: win32.WindowInfo) -> bool:
    t = (info.title or "").strip().lower()
    if not t:
        return False
    # 只在对话框上判标题（普通窗口标题里出现 "run" 太常见了）
    if info.class_name != "#32770" and not info.class_name.startswith("Credential"):
        return False
    return any(w in t for w in DENY_TITLE_WORDS)


def window_decision(info: win32.WindowInfo, our_pids=(), *,
                    for_activation: bool = False) -> Decision:
    """这个窗口能不能被观察 / 操作。

    `for_activation=True` 时允许最小化。
    判据：`activate_window` 的职责就是"把窗口弄到前台"，而窗口最小化正是它
    要处理的常态。两边共用一道"最小化就拒"的闸会成死路：拒绝话术让模型去
    `activate_window`，而 `activate_window` 自己也被同一句拒掉。
    其余黑名单（终端 / 登录 / 密码管理器 / 自家窗口 / cloaked）照旧。
    """
    if info is None:
        return deny("窗口已经不在了")
    # `list_windows` 会把我们自己的窗口（接管横幅 / 主窗）
    #   也列成了"未授权"——那等于允许用户把"控制 Crannia 自己"授权出去，
    #   而急停按钮就在那个窗口上。绝不接管自己。
    if info.pid in set(our_pids or ()):
        return deny("这是 Crannia 自己的窗口，不接管")
    if info.exe in DENY_EXES:
        return deny("「%s」不在可接管的范围内（终端 / 登录 / 密码管理器 / 安全设置）"
                    % (info.exe or info.title))
    if info.class_name.lower() in DENY_CLASSES:
        return deny("窗口类「%s」不在可接管的范围内" % info.class_name)
    if _title_looks_dangerous(info):
        return deny("「%s」看起来是系统安全 / 运行对话框，不接管" % info.label)
    if info.cloaked:
        return deny("窗口在另一个虚拟桌面上（cloaked），够不到")
    if info.minimized and not for_activation:
        return deny("窗口已最小化 —— 先 `activate_window` 再观察")
    return OK


def activation_decision(info: win32.WindowInfo, our_pids=()) -> Decision:
    """激活窗口的判据 —— 允许最小化，其余同 `window_decision`。"""
    return window_decision(info, our_pids, for_activation=True)


# 按键黑名单
WIN_KEYS = frozenset({"win", "lwin", "rwin", "meta", "super", "os",
                      "left_win", "right_win", "left_meta", "right_meta"})
#: 急停键说明：只对物理按键。注入的 Esc 现在**允许送到
#: 目标应用**（关弹窗/搜索框）——`watch.py` 在 LL 钩子上本来就能分清物理与注入。
#: 注入一律吞掉会让模型永远关不掉弹窗（微信搜索会卡住），
#: 而它点「取消」按钮一样关得掉，那条防御本来就不成立。
#: 物理 Esc 的急停判据在 `watch.py::_key_cb`，不在这里。


def combo_decision(tokens: list[str]) -> Decision:
    """`tokens` 是已经归一化的小写键名（见 `input.parse_combo`）。"""
    if not tokens:
        return deny("没有按键")
    lowered = {t.lower() for t in tokens}
    if lowered & WIN_KEYS:
        return deny("Windows 键不允许由 agent 注入（会打开开始菜单 / 系统界面）")
    # 不拒绝注入 Esc —— 物理 Esc 仍是急停（watch 的 LL 钩子
    #   只对物理按键触发 stop）；注入 Esc 只是关弹窗/搜索框的正常动作。
    if {"ctrl", "control"} & lowered and "alt" in lowered and "del" in lowered:
        return deny("Ctrl+Alt+Del 是安全序列，不能注入")
    return OK


# 启动程序（`launch_app`）
def launch_decision(target: str) -> Decision:
    """能不能用 `launch_app` 启动这个目标。

    判据是目标身份，不是语义：
    · 只收 exe 或裸程序名（`notepad.exe` / `notepad`）——
      脚本（`.bat/.cmd/.ps1/.vbs/.js/.msi/.lnk`…）是执行代码，走 `run_command`；
    · URL / 协议链接（`https://` · `ms-settings:` · `shell:`）不是"启动程序"；
    · 终端 / 登录 / 密码管理器 / 安全设置照旧硬拒（复用 `DENY_EXES`）；
    · 不接受命令行参数（`launch_app` 只有一个 `app` 字段）。
    """
    t = str(target or "").strip()
    if not t:
        return deny("launch_app 要给一个程序名或 exe 路径")
    low = t.lower()
    if "://" in low or low.startswith(("ms-", "shell:", "run:")):
        return deny("launch_app 只启动本地程序，不打开 URL / 协议链接")
    if any(ch in t for ch in "\r\n\0"):
        return deny("程序名里有换行或空字符")
    name = os.path.basename(low.replace("\\", "/"))
    if name in DENY_EXES or (name + ".exe") in DENY_EXES:
        return deny("「%s」不在可启动的范围内（终端 / 登录 / 密码管理器 / 安全设置）" % name)
    # 有扩展名就必须是 .exe；脚本 / 快捷方式 / 安装包一律不在这里启动。
    if "." in name and not name.endswith(".exe"):
        return deny("launch_app 只收 exe（或裸程序名），不收「%s」—— 脚本请走 run_command"
                    % name)
    return OK


# 运行前预检
def preflight(target_pid: int) -> Decision:
    """锁屏 / 完整性级别。在真正注入之前跑。"""
    if win32.is_locked():
        return deny("桌面已锁屏 —— 接管立即停止（不许去点锁屏界面）")
    if target_pid:
        theirs = win32.integrity_rid(target_pid)
        ours = win32.current_integrity_rid()
        if theirs is not None and ours is not None and theirs > ours:
            return deny("目标进程的权限比我们高（提权窗口）—— Windows 会**静默**挡掉注入，"
                        "所以这里直接说够不到")
    return OK


def foreground_decision(hwnd: int) -> Decision:
    """注入前最后一道：目标必须仍是前台。

    用户一动（或者把我们的窗口点到前面）就中止 —— 这一条比"退出接管"
      更细：它保证即使退出信号还没到，也不会把键打进别的窗口。
    """
    if not win32.is_foreground(hwnd):
        current = win32.foreground_hwnd()
        info = win32.window_info(current) if current else None
        return deny("前台已经变了（现在是「%s」）—— 动作已中止，重新观察后再试"
                    % (info.label if info else "未知窗口"))
    return OK


def our_window(hwnd: int) -> bool:
    """是不是我们自己的窗口（审批面板 / 停止按钮 / 聊天框）。

    落在这些窗口上的物理输入不算"干预" ——
      否则逐应用授权和停止按钮一点就退出接管。
    """
    info = win32.window_info(hwnd) if hwnd else None
    return bool(info and info.pid in win32.our_pids())
