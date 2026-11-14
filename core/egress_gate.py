"""域名级出网审批闸。

## 它补的是哪一半

`core/security.py::check_url_egress` 是地板：内网 / 回环 /
保留段一律拒。它不挡公网域名 —— 那正是这一条要补的那半：

    地板  = "这个目的地能不能发"      （确定性，不问）
    本模块 = "这个目的地该不该发"      （要问一次，可记住）

顺序不可重排：`deny` 先于 `ask`。
求值顺序是 deny → ask → allow，第一个命中决定结果
（"任何『先 allow 再补 deny』的重排都是漏洞"）。
调用点一律写成 `check_url_egress(...)` 之后紧接本模块，见下面「接线」。

## 判据：机制存在 ≠ 这条路径上会调它

生产侧必须真的接到消费端：`core/pipeline/stages/agent_loop.py` 会处理
`pending_egress_approval`，而工具必须产生它（只有测试在喂不算）。
本模块存在的前提是把消费端接上生产端，而不是再写一套新机制。

## 放行判据（三半，都 fail-closed）

① 会话级 / 一次性 / 逐字的持久白名单 —— `wm.has_target_grant("egress", host)`。
   投影那一侧已经把 `data/allowlist.json` 并进来了（`pipeline/scheduler.py`），
   所以"用户在设置页加了域名"与"他刚才点了同意"在工具这一侧是同一个问题。
② 通配模式 —— `core/allowlist.py::matches_any`（`*.example.com` 这类）。
   为什么不能只靠 ①：`has_target_grant` 的比对是 `s in allowed`
   （逐字），模式落不到它身上。也不能把通配接进 `has_target_grant`
   —— 那一条同时服务 `command`，而命令是整条精确匹配（理由见
   `core/allowlist.py` 那段）。
③ 预批准域名表 —— `core/egress_preapproved.py`。
   「为什么可以预置」写在那个文件里。
   它不放宽 `deny` 与地板：见本模块开头"deny 先于 ask"。

分类器判错的方向是「少问一次」，而本模块判错的方向是「多问一次」。
本仓库一贯把误差往安全那边掰。

读不到一律朝"问"倒：任何异常都只让 `is_granted` 返回 `False`。

## 为什么没有"用户提过就放行"那一半

有过。判据是"这个域名以独立片段出现在本轮用户消息里"，实际它能被一句
「不要访问 https://example.com」满足 —— 纯字符串匹配分不出授权、讨论、
否定、引用，而它却拿这个结论去免掉一次审批。原实现自己在 docstring 里
写着这条局限，然后照样把它当授权用。

两个误差方向不对等：多问一次的代价是一次点击；少问一次的代价是
"用户明确说过别去，我们还是去了"。所以这一半删了。

用户贴的链接现在和任何新域名一样问一次；点过同意之后本会话内不再问 ——
那是 ① 的活，不是这里。

## 接线（这一条是本模块的验收判据）

    check_url_egress(url)   ← 地板（每跳）
        ↓ 紧接
    egress_gate(url)        ← 本模块（每跳；同域只问一次，因为批过就记住了）
        ↓ None 才继续
    _http_get_once(url)

工具 handler 拿不到 `ctx`（签名是 `handler(**tool_args)`）—— 会话身份只能
走 `bus.registry.get_workspace_manager()` 那台投影（同 `core/shell.py`）。
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from core.i18n import t as tr
from core.i18n import tr_fmt as trf

__all__ = ["host_of", "egress_gate", "is_granted"]


def host_of(url: str) -> str:
    """URL 的主机名（小写、不带端口）；取不到返回 `""`。

    取不到不抛 —— 那种 URL 会在地板那一层被拒（"URL 没有主机名"），
    不该在这里编一个出来。本函数只服务"这个域名批过没有"这一个问题。
    """
    try:
        return (urlsplit(str(url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""


def is_granted(host: str, path: str = "/") -> bool:
    """这个域名放不放行 —— 三半（见模块 docstring）。fail-closed。

    `path` 只有预批准表里那几条带路径的条目会用到（默认 `/`）。
    """
    h = str(host or "").strip().lower()
    if not h:
        return False
    wm = None
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
    except Exception as exc:  # noqa: BLE001
        print(f"[egress] 取 wm 失败（按未放行处理）：{type(exc).__name__}: {exc}")
    # ① 会话级 ∪ 一次性 ∪ 逐字的持久白名单（调度器每轮投影进 wm）
    try:
        if wm is not None and wm.has_target_grant("egress", h):
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"[egress] 会话授权查询失败（按未放行处理）："
              f"{type(exc).__name__}: {exc}")
    # ② 通配模式（`*.example.com`）—— 见模块 docstring 里"为什么不能只靠 ①"。
    #    它原来直接 `return` —— 现在下面还有 ③④，改成命中才 return，
    #       读失败不吞掉后面两层（吞了 = 表里的站点反而被问，是"更严"那一侧，
    #       但那不是我们的本意：读失败与不命中是两件事）。
    try:
        from core.allowlist import list_for, matches_any
        if matches_any(list_for("egress"), h):
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"[egress] 白名单读取失败（按未放行处理）："
              f"{type(exc).__name__}: {exc}")
    # ③ 预批准域名表
    try:
        from core.egress_preapproved import is_preapproved
        if is_preapproved(h, path):
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"[egress] 预批准表读取失败（按未放行处理）："
              f"{type(exc).__name__}: {exc}")
    return False


def _ask_index(kind: str = "egress") -> int:
    """这是第几次问起 —— 升级项（选项 5）按它决定出不出现。

    真值在调度器上（`wm.approval_ask_counts` 是投影），由
    `core/pipeline/stages/agent_loop.py` 在工具返回 `pending_*` 时推进。
    取不到，1（= 当成第一次），不会把升级项提前顶出来。
    """
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        return int((getattr(wm, "approval_ask_counts", {}) or {}).get(kind, 0)) + 1
    except Exception:  # noqa: BLE001
        return 1


def egress_gate(url: str, *, tool_name: str = "web_fetch") -> Optional[dict]:
    """要问，返回待审批载荷；放行，返回 `None`。

    调用前必须先过 `check_url_egress`（`deny` 先于 `ask`，见模块 docstring）。
    """
    host = host_of(url)
    if not host:
        # 没有主机名，那是地板的事，不在这里变成一次静默放行 或 一次误问。
        return None
    # 路径也要传给判定 —— 预批准表里有带路径的条目
    #   （`github.com/anthropics`），只看 host 会把整个 `github.com` 放进来。
    try:
        _path = urlsplit(str(url or "").strip()).path or "/"
    except ValueError:
        _path = "/"
    if is_granted(host, _path):
        return None

    from core.approval import pending_payload

    return pending_payload(
        "egress",
        # 正文要说清去哪、谁要去 —— 被审批的是"访问这个域名"，
        # 而用户要看的正是那个域名（被审批的文本就是被同意的文本）。
        body=trf("Crannia 想访问 **{host}** 上的内容（{tool}）：\n{url}",
                 host, tool_name, url),
        ask_index=_ask_index(),
        footer=True,
        subject=host,
        detail=host,
        url=url,
        tool=tool_name,
        # `status` 是工具返回的契约：`core/pipeline/stages/agent_loop.py`
        # 认的就是这个串（按 `kind` 分派）。`pending_payload` 给的默认值是
        # 跨区那套的 `pending_approval`，覆盖掉（同 `core/shell.py`）。
        status="pending_egress_approval",
    )
