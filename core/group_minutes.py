"""群聊的「会议纪要」。

## 它是什么

无主理人群聊（含圆桌）里，跨轮次留下来的那一段事实：群目标 · 名册与职责 ·
当前任务 · 已完成/产物 · 关键决策 · 未决事项 · 谁做了什么。

它不是消息、不是发言人、不显示给用户。用户看到的是每位成员自己的
回复卡；纪要是机器层的上下文块，注入到每个成员的用户消息尾部。

## 为什么单独存，不塞进 messages

同 `core/group_digest.py` 的三条：

- 塞进消息流会变成一条 `user` / `assistant` 行，污染轮次锚点
  （`collect_anchor_rows` 取的是最后一条 `user`，收藏的 会指到纪要上）；
- 它每次更新都变，放进 system prompt 会改写缓存前缀；
- 它要活过压缩 —— 压缩只动 `messages`，纪要不在里面就天然不会被压掉。
  这就是「压缩保留清单」，不需要额外补注入。

落 `data/runtime/group_minutes.json`，键 = 存储 id（`/clear` 铸新 id
旧纪要天然失效，purge 清单不用加行）。

## 什么时候更新

- 每 N 轮（`EVERY_N_TURNS`，默认 5）；
- 压缩之后兜底一次 —— 压缩是把旧轮次挤出活动窗口的时刻，
  纪要必须已经包含那些轮次。

## 谁生成

独立一次小 LLM 调用，不复用压缩器：
压缩器是通用的，群聊格式不该带进单聊。

## 可见性

现在不显示。面板是后续可能性。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

LEDGER_PATH = Path("data/runtime/group_minutes.json")

#: 最多留多少条（一个存储 id 一条；超了按 updated_at 丢最老的）。
MAX_ENTRIES = 100

#: 纪要正文上限（字符）。它按每条成员请求计费 —— 宁短勿长。
MAX_TEXT = 1800

#: 每多少用户轮自动更新一次。
EVERY_N_TURNS = 5

#: 生成时喂进去的对话上限（字符）。原话还在库里，细则走 `history_search`。
MAX_INPUT_CHARS = 12000
_IN_USER_MAX = 1500
_IN_ASSIST_MAX = 600
_IN_TOOL_MAX = 300

_LOCK = threading.Lock()
_cache: dict[str, dict] | None = None


# ----------------------------------------------------------------------
# 账本
# ----------------------------------------------------------------------

def _read() -> dict[str, dict]:
    try:
        raw = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:                                    # noqa: BLE001
        print("[group_minutes] 账本读不动（%s: %s）—— 按空账本走"
              % (type(exc).__name__, exc))
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
    global _cache
    with _LOCK:
        _cache = _read()
    return dict(_cache)


def _all() -> dict[str, dict]:
    global _cache
    if _cache is None:
        _cache = _read()
    return _cache


def get(storage_id: str) -> dict | None:
    """这一段的纪要；没有就 `None`（不是空串 —— 读不到 ≠ 没有）。"""
    key = str(storage_id or "")
    if not key:
        return None
    with _LOCK:
        rec = _all().get(key)
        return dict(rec) if isinstance(rec, dict) else None


def set(storage_id: str, *, group_id: str = "", text: str = "",
        turns: int = 0) -> dict:
    """覆盖写。空正文不覆盖旧纪要（返回旧值）—— 一次失败的生成不该抹掉好纪要。"""
    key = str(storage_id or "")
    if not key:
        raise ValueError("storage_id 不能为空")
    body = str(text or "").strip()
    with _LOCK:
        data = _all()
        if not body:
            old = data.get(key)
            return dict(old) if isinstance(old, dict) else {}
        rec = {
            "group_id": str(group_id or ""),
            "text": body[:MAX_TEXT],
            "turns": int(turns or 0),
            "updated_at": int(time.time()),
        }
        data[key] = rec
        # 超上限按 updated_at 丢最老的 —— 只丢别的存储 id，不丢刚写的这条。
        if len(data) > MAX_ENTRIES:
            ordered = sorted(data.items(),
                             key=lambda kv: int((kv[1] or {}).get("updated_at") or 0))
            for stale, _ in ordered[:len(data) - MAX_ENTRIES]:
                if stale != key:
                    data.pop(stale, None)
        _write(data)
        return dict(rec)


def clear(storage_id: str) -> None:
    key = str(storage_id or "")
    if not key:
        return
    with _LOCK:
        data = _all()
        if key not in data:
            return
        data.pop(key, None)
        _write(data)


def reset_for_tests() -> None:
    global _cache
    with _LOCK:
        _cache = None


# ----------------------------------------------------------------------
# 生成
# ----------------------------------------------------------------------

MINUTES_SYSTEM_PROMPT = """你是群聊的**记录员**，不是发言人。你的任务是把这段群聊整理成一份
**会议纪要**，供下一轮参与的成员快速恢复上下文。

只输出纪要正文，不要任何解释、不要 Markdown 代码围栏。固定七个二级标题，缺内容的写「（暂无）」：

## 群目标
## 名册与职责
## 当前任务
## 已完成与产物
## 关键决策
## 未决事项
## 谁做了什么

规则：
- 只写**对话里已经发生的事实**；没说的不要补，不确定的写「（未确认）」。
- 产物要带**路径**；决策要带**理由**（一句话即可）。
- 未决事项写清「在等谁 / 等什么」。
- 整篇不超过 1200 字符；每条 1–2 句，宁可短。
- 与已有纪要重叠时**合并**，不要丢掉仍然有效的事实。
- 用户原话里的关键约束、阈值、文件名**逐字保留**。
"""


def _content_text(content) -> str:
    """消息 content 的纯文本视图 —— 带图那一轮是块列表，不能直接 `str()`。"""
    try:
        from core.memory.compressor import content_text
        return content_text(content)
    except Exception:                                            # noqa: BLE001
        return str(content or "")


def build_minutes_user_text(group: dict, messages: list[dict],
                            previous: str = "") -> str:
    """把这一段群聊拼成生成器的输入。

    标注说话人：群历史里是 `role=assistant` + `speaker` 字段，
      不标的话记录员分不清谁做了什么。
    """
    gname = str((group or {}).get("name") or "未命名群聊")
    members = (group or {}).get("members") or []
    roster = "；".join(
        "%s（%s）" % (str(m.get("name") or ""),
                     str(m.get("role") or "").strip() or "未写职责")
        for m in members) or "（名册为空）"

    lines: list[str] = [
        "群名称：%s" % gname,
        "名册：%s" % roster,
    ]
    if str(previous or "").strip():
        lines += ["", "## 已有纪要（请合并，不要丢掉仍然有效的事实）",
                  str(previous).strip()[:MAX_TEXT]]
    lines += ["", "## 对话"]

    used = 0
    skipped = 0
    for m in messages or []:
        role = str(m.get("role") or "")
        if role not in ("user", "assistant", "tool"):
            continue
        text = _content_text(m.get("content"))
        if role == "tool":
            if not text:
                continue
            line = "Tool: %s" % _flat(text)[:_IN_TOOL_MAX]
        elif role == "user":
            line = "User: %s" % _flat(text)[:_IN_USER_MAX]
        else:
            who = str(m.get("speaker") or "").strip()
            label = ("Assistant(%s)" % who) if who else "Assistant"
            line = "%s: %s" % (label, _flat(text)[:_IN_ASSIST_MAX])
        if used + len(line) > MAX_INPUT_CHARS:
            skipped += 1
            continue
        lines.append(line)
        used += len(line) + 1

    if skipped:
        lines.append("[还有 %d 条更早的消息没能纳入本次输入；原话仍在历史库里]"
                     % skipped)
    return "\n".join(lines)


def _flat(text: str) -> str:
    """把换行压成空格 —— 一行一条消息，记录员才分得清边界。"""
    return " ".join(str(text or "").split())


def parse_minutes(raw: str) -> str:
    """清理模型输出：去代码围栏、去首尾空白、按 `MAX_TEXT` 截断并出声。"""
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT].rstrip() + "\n（纪要超长，已截断）"
    return text


def update_now(group: dict, storage_id: str, messages: list[dict],
               provider, turn_no: int = 0) -> str:
    """同步生成一次纪要并落盘。失败/空结果，保留旧纪要，返回空串。

    不抛：它是轮末的记账，坏掉不该影响一轮已经跑完的对话
      （同 `log_turn` / `seal_turn` 的判据）。
    """
    if provider is None or not messages:
        return ""
    old = get(storage_id) or {}
    previous = str(old.get("text") or "")
    try:
        transcript = build_minutes_user_text(group, messages, previous)
        # system 是请求参数，不进 messages（同 `GroupReplyStage` 的路由器）。
        resp = provider.chat(
            [{"role": "user", "content": transcript}],
            system=MINUTES_SYSTEM_PROMPT,
        )
        text = parse_minutes(getattr(resp, "content", "") or "")
    except Exception as exc:                                     # noqa: BLE001
        print("[group_minutes] 生成失败（%s: %s）—— 保留旧纪要"
              % (type(exc).__name__, exc))
        return ""
    if not text:
        print("[group_minutes] 模型没有给出纪要 —— 保留旧纪要")
        return ""
    try:
        set(storage_id, group_id=str((group or {}).get("id") or ""),
            text=text, turns=int(turn_no or 0))
    except Exception as exc:                                     # noqa: BLE001
        print("[group_minutes] 落盘失败（%s: %s）" % (type(exc).__name__, exc))
        return ""
    return text


def should_update(group: dict, was_compressed: bool, turn_no: int) -> bool:
    """这一轮该不该更新纪要。

    判据收在一处，`MemoryManager.record_turn` 只负责调用：
      · 只对无主理人的群生成（含圆桌 —— 圆桌复用本模块；
        圆桌共享一段历史、涨得比非圆桌快，压缩会更早发生，
        没有纪要，被挤出活动窗口的轮次就只剩通用摘要里的代码向九段；
        有主理人的群本来就有主理人自己的上下文，不生成）；
      · 每 `EVERY_N_TURNS` 轮一次；
      · 压缩之后兜底一次 —— 压缩正是旧轮次被挤出活动窗口的时刻。
    """
    if not group:
        return False
    if any(bool(m.get("host")) for m in (group.get("members") or [])):
        return False
    if was_compressed:
        return True
    return bool(turn_no and turn_no % EVERY_N_TURNS == 0)


def spawn_update(group: dict, storage_id: str, messages: list[dict],
                 provider, turn_no: int = 0) -> None:
    """后台跑一次 `update_now` —— 轮末的写入不该拖住用户那一轮的回复。"""
    msgs = list(messages or [])

    def _run() -> None:
        update_now(group, storage_id, msgs, provider, turn_no)

    try:
        t = threading.Thread(target=_run, name="group-minutes", daemon=True)
        t.start()
    except Exception as exc:                                     # noqa: BLE001
        print("[group_minutes] 后台线程起不来（%s: %s）"
              % (type(exc).__name__, exc))
