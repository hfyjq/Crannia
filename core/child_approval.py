"""子 agent 的审批 —— 就地等用户回答，不占用父那一轮。

## 为什么需要另一条路

父那一轮里的审批靠「用户的回答变成新的一轮」兑现（`pre_process._dispatch_approval`
→ `_settle_*` → 重放）。子 agent 跑在父的一次工具调用里，那一轮进不来
（回答会排在后面）—— 照搬父那条路就是死锁。所以这条：

```
子的工具返回 pending_*  →  登记一条待批  →  子阻塞等  →  回答直接交给它
```

工具返回 `pending_*` 那一刻还没动手（本仓库审批的定义），所以用户同意之后
由框架补一次一次性授权、就地重跑那次调用 —— 用户看到的是事情直接做成。

## 范围

只服务动作审批：命令 / 出网 / 跨区 / 区外写 / 删除 / 兜底闸。
计划批准、真值放行、应用接管不在这里 —— 它们改的是模式或整机授权，
由子代理发起会把"一个人的决定"变成"一个子任务的决定"。

## 无响应兜底

等 `core.approval_timeout` 的那个秒数（默认 120，`0` = 不超时）。
到点结算成 `unavailable`（不是 `rejected`）：没有可问的通道，不是用户的决定。

## 冲突

按会话一条队列：同一时刻只显示队首那条（面板本来也只渲染一条）。
两个子同时要审批时，后一条排队，不会互相覆盖；答案按 `request_id` 路由，
晚到的答案按 `stale` 拒。
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Tuple

from core.approval import (REPLY_OK, REPLY_STALE, build_payload,
                           new_request_id)

__all__ = ["ALLOW", "DENY", "UNAVAILABLE", "ask", "pending", "answer",
           "cancel_session", "queue_len"]

ALLOW = "allow"
DENY = "deny"
UNAVAILABLE = "unavailable"

#: 会话键 -> 队列。每条：`request_id` / `kind` / 载荷里那几格 /
#: `requester` / `event` / `decision`。
_QUEUES: Dict[str, List[dict]] = {}
_LOCK = threading.Lock()


def _timeout_of(session_key: str) -> int:
    """这一次等多久（秒）。`0` = 不超时（用户自己关掉了兜底）。"""
    try:
        from core.approval_timeout import seconds
        return int(seconds(session_key) or 0)
    except Exception:                                   # noqa: BLE001
        return 0


def ask(session_key: str, pend: dict, *, requester: str = "") -> str:
    """登记一条子代理的审批，阻塞到有答案或超时。

    返回 `ALLOW` / `DENY` / `UNAVAILABLE`。
    """
    entry = {
        "request_id": new_request_id("child"),
        "kind": str(pend.get("kind") or ""),
        "subject": str(pend.get("subject") or ""),
        "body": str(pend.get("body") or ""),
        "file_list": str(pend.get("file_list") or ""),
        "rule": str(pend.get("rule") or ""),
        "requester": str(requester or ""),
        "session_key": str(session_key or ""),
        "event": threading.Event(),
        "decision": "",
    }
    with _LOCK:
        _QUEUES.setdefault(str(session_key or ""), []).append(entry)
    print(f"[subagent] 等待审批：{entry['kind']}（{requester or '-'}）"
          f" request={entry['request_id']}")
    try:
        wait_s = _timeout_of(session_key)
        got = entry["event"].wait(wait_s if wait_s > 0 else None)
        if not got:
            print(f"[subagent] 审批超时（{wait_s}s，无回答）：{entry['kind']}")
            return UNAVAILABLE
        return entry["decision"] or UNAVAILABLE
    finally:
        with _LOCK:
            q = _QUEUES.get(str(session_key or "")) or []
            if entry in q:
                q.remove(entry)
            if not q:
                _QUEUES.pop(str(session_key or ""), None)


def pending(session_key: str) -> Optional[dict]:
    """这个会话队首那条子代理审批（面板的渲染源）。没有就 `None`。"""
    with _LOCK:
        q = _QUEUES.get(str(session_key or "")) or []
        head = q[0] if q else None
    if head is None:
        return None
    return build_payload(
        head["kind"], 1,
        body=head["body"],
        request_id=head["request_id"],
        file_list=head["file_list"],
        rule=head["rule"],
        options=[{"key": "1", "description": ""},
                 {"key": "2", "description": ""}],
        requester=head["requester"],
    )


def answer(session_key: str, request_id: str, choice: str) -> Tuple[str, str]:
    """回答队首那条。返回 `(verdict, 一句原因)` —— 与 `reply_decision` 同形。

    `1` = 同意，其余 = 不同意（这一格只有两项，选项表由 `pending()` 给出）。
    """
    key = str(session_key or "")
    with _LOCK:
        q = _QUEUES.get(key) or []
        head = q[0] if q else None
        if head is None:
            return (REPLY_STALE, "这条已经回答过了。")
        if request_id and str(request_id) != head["request_id"]:
            return (REPLY_STALE, "这条已经回答过了（请求号对不上）。")
        head["decision"] = ALLOW if str(choice or "").strip() == "1" else DENY
        head["event"].set()
    return (REPLY_OK, "")


def cancel_session(session_key: str) -> None:
    """`/clear`：把还在等的子代理全部放走（记 `unavailable`，不是拒绝）。"""
    key = str(session_key or "")
    with _LOCK:
        q = _QUEUES.pop(key, None) or []
    for entry in q:
        entry["decision"] = UNAVAILABLE
        entry["event"].set()


def queue_len(session_key: str) -> int:
    with _LOCK:
        return len(_QUEUES.get(str(session_key or "")) or [])
