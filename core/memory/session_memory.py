"""
L3 Session Memory: structured Markdown session notes.

Maintains a file at `data/memory/sessions/<session_id>.md` that is
incrementally updated via an LLM call when the quantity gate opens
(攒够才写，而不是每轮都写一次). This pre-built note replaces
the LLM-generated compression summary, making compression a
zero-API-cost operation.

Template::

    # Session Note — <session_id>
    ## Current Status
    ## Key Decisions
    ## Open Questions
    ## Next Steps
"""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

#: 归档件的名字形状：<键>.<YYYYMMDD-HHMMSS>.md —— 即"文件主干尾部是一段
#: 8 位日期 + `-` + 6 位时间"。
#:
#: 判据不是"含不含点" —— 会话键允许含点（`_sanitize_filename` 只替
#: `:<>"/\|?*`，不动 `.`），所以要认的是整个尾段的形状。一个会话键碰巧长成
#: `<名字>.<8位>-<6位>` 会被误判成归档件（代价：它不进会话闸），概率可忽略，
#: 而反过来的错更贵（把归档件当会话，喂错 `memory_organize` 的 ≥5 会话闸）。
_ARCHIVE_TAIL_RE = re.compile(r"^(.+)\.(\d{8}-\d{6})$")


# ------------------------------------------------------------------
# 会话笔记的量闸
# ------------------------------------------------------------------
# 三个数（10000 / 5000 / 3）判的都是闸门窗口内的累计量，
#    不是全会话总量。
# 判据是「这一轮干了多少事」：不看轮数，看工具调用量 ——
#    「一轮跑了 20 个工具」与「一句『谢谢』」价值完全不同，而按轮计数看不出这件事。
# 三个数都不许按"看起来更省"改小：闸门前面那一步是一次 LLM 调用，
#    后面那一步（压缩）拿的就是这份笔记（`session_memory` 的 docstring：
#    "making compression a zero-API-cost operation"），闸门开得太稀，
#    压缩那一刻手上的笔记就太旧。要改数先量一遍滞后。
NOTE_INIT_TOKENS = 10_000
NOTE_UPDATE_TOKENS = 5_000
NOTE_UPDATE_TOOL_CALLS = 3


def note_gate_met(pending_tokens: int, pending_tools: int, has_note: bool) -> bool:
    """攒够没有 —— 纯函数（读者 `tests/test_session_memory.py`）。

    * 还没有笔记，只认 `NOTE_INIT_TOKENS`
      （首份笔记的门槛：第一份笔记之前先攒够量，
      否则头几轮那点寒暄会先立一份空笔记，而它一立起来就被当成"已有的"）。
    * 已经有笔记，token 或 工具调用任一攒够就够。

    判据是 `>=`，不是 `>`；负数/荒唐值不特殊处理（调用方给的是累计量，
    特殊处理只会让"闸门为什么不开"多一条说不清的支路）。
    """
    if not has_note:
        return pending_tokens >= NOTE_INIT_TOKENS
    return (pending_tokens >= NOTE_UPDATE_TOKENS
            or pending_tools >= NOTE_UPDATE_TOOL_CALLS)


# ------------------------------------------------------------------
# Prompt for updating the session note
# ------------------------------------------------------------------

SESSION_NOTE_PROMPT = """You maintain a structured session note for an ongoing AI conversation. You will receive the CURRENT session note and the LATEST user-assistant exchange. Update the note to reflect the new exchange.

## Sections (always keep all seven)
- **Current Status**: What is the user doing right now? What task or topic are they working on?
- **Key Decisions**: Concrete decisions made by the user or agreed upon during the conversation.
- **Open Questions**: Questions the user has asked that remain unanswered, or items they said they'd return to.
- **Next Steps**: What should happen next? What did the user say they will do or want the assistant to do?
- **Document Index**: Which files matter for THIS work — **one line each**:
  `path — what it is` (the description is the point: it is what makes the line
  usable without opening the file). List only files this session actually read,
  wrote, or referred to; drop a line once that file stops mattering.
  **`- (None)` is the normal early state.** Do NOT list the project's document
  tree — a stale or bloated index is worse than none.
- **Errors & Corrections**: What went wrong and how it got fixed; approaches that
  **failed and must not be retried**; places the user corrected you.
  ★ 这是最值钱的一段 —— 缺了它，下一轮会把同一条死路**再走一遍**。
  Record only what actually happened. `- (None)` is the normal early state.
- **Key Results**: Concrete results already produced — numbers, thresholds,
  paths, verdicts. ★ **抄逐字**（`85%`、某个版本号、某条判据的原话），
  别改写成概述：概述过一次，「他当时到底怎么说的」就没了。
  `- (None)` is the normal early state.

## CRITICAL: Do NOT invent narratives
- If the exchange contains NO concrete task, NO user request, and NO meaningful decision (e.g. just greetings, "hello", "你好", "在吗"), leave all sections EMPTY (just "- (None)" or "- N/A").  Do NOT make up "the user wants to retry a tool" or "the user authorized something" — if it didn't happen, don't write it.
- Only record things the user ACTUALLY said or did.  Never infer intent from an assistant's question.

## Rules
1. Keep it **bounded** — bullets preferred, **at most 8 items per section**, and
   **the whole note under 4000 characters**. 超了就合并、或删掉最旧的那条 ——
   这份笔记**每一轮都会被重写一次**（那次调用要把它整个读进去），
   它长胖的代价是**每轮**都付，不是只有一次。
2. Update Current Status to reflect the MOST RECENT activity.
3. Add new decisions, questions, and next steps without losing old ones (unless resolved).
4. If a question was answered, move it out of Open Questions and note the resolution in Key Decisions or Current Status.
5. Do NOT summarize the entire conversation verbatim — capture the essential state.
6. Write in a continuous, present-tense style.  Do NOT use third-person
   recollection phrases like "the user previously mentioned" or "the assistant
   suggested" — this note tracks an ongoing conversation, not a historical record.
7. Output ONLY the full updated Markdown note, nothing else.
8. Use the same language as the user's message. Output ONLY valid Markdown — no code fences, no explanations."""


# ------------------------------------------------------------------
# Template for brand-new sessions
# ------------------------------------------------------------------

def _new_session_template(session_id: str) -> str:
    """Return the initial (mostly empty) session note for a new session."""
    return (
        f"# Session Note — {session_id}\n\n"
        "## Current Status\n"
        "(New session — no activity yet)\n\n"
        "## Key Decisions\n"
        "(None yet)\n\n"
        "## Open Questions\n"
        "(None yet)\n\n"
        "## Next Steps\n"
        "(None yet)\n\n"
        # 第五段 —— 「agent 自己知道到哪了」的那半张地图。
        #   它与上面四段同生同死：`SESSION_NOTE_PROMPT` 的段名单和
        #   这里必须逐字一致，`tests/test_session_memory.py` 拿两边对账。
        "## Document Index\n"
        "(None yet)\n\n"
        # 后两段是内容类的（Errors & Corrections / Key Results）。
        #   上界是整篇 ≤4000 字符 —— 因为真正的
        #   经常性成本不在"注入"（那是一次性的，`get_context` 有闸），
        #   而在每轮重写它的那次 LLM 调用
        #   （输入 = 整份笔记 + 这一轮），要封的是总量，不是条数。
        "## Errors & Corrections\n"
        "(None yet)\n\n"
        "## Key Results\n"
        "(None yet)\n"
    )


# ------------------------------------------------------------------
# SessionMemory
# ------------------------------------------------------------------

class SessionMemory:
    """Manages persistent, structured Markdown session notes on disk."""

    NOTES_DIR = "data/memory/sessions"

    def __init__(self, sessions_dir: str | None = None) -> None:
        self._sessions_dir = Path(sessions_dir or self.NOTES_DIR)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, session_id: str) -> Optional[str]:
        """Read the session note from disk.  Returns `None` if not found."""
        note_path = self.get_note_path(session_id)
        try:
            return note_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def save(self, session_id: str, note: str) -> None:
        """
        Write the session note to disk atomically.

        Writes to a temporary sibling file first, then replaces the
        target path so that a crash mid-write never corrupts the
        existing note.

        空内容拒绝写入 —— 这是不变量，不是防御。

        任何让 LLM 返回空串的路径都会把上一份好笔记覆盖成空文件，
        而且走的是成功分支（日志打 `Session note updated (0 chars)`）。
        一条机械的触发路径就在本文件里：模型只回三个反引号时，
        下面 `update()` 剥围栏那三行正好把它削成 `""`。

        失败方向必须是"保持不动"，不是"写坏" —— 宁可不出摘要，
        也不落半截摘要。
        """
        if not note.strip():
            print(f"[memory] 拒绝用空内容覆盖会话笔记 ({session_id}) —— "
                  f"上一次的笔记保持不动")
            return

        note_path = self.get_note_path(session_id)
        tmp_path = note_path.with_suffix(".md.tmp")

        tmp_path.write_text(note, encoding="utf-8")
        os.replace(tmp_path, note_path)  # atomic on Windows + POSIX

    # ------------------------------------------------------------------
    # 归档（`/clear` 走这条路，不再 unlink）
    # ------------------------------------------------------------------

    def archive(self, session_id: str, now: Optional[datetime] = None) -> Optional[Path]:
        """把当前会话笔记改名归档，返回新路径；没有笔记，`None`。

        ## 为什么是"改名"而不是"删除"

        判据：`/clear` = 这段对话及其派生物；清 ≠ 删。
        `/clear` 要的是把这段对话从当前会话里摘掉，不是从磁盘上销毁它。
        而这笔笔记是全仓最贵的派生制品（每次量闸打开时一次 LLM 调用养出来的），
        同时又是 `memory_organize` 的原料 —— 销毁它等于从长期记忆的原料里抽掉一份。

        文件名能逃出会话键，所以这条能成立：`/clear` 复用会话键，
        若文件名还是 `<键>.md`，归档就等于"留给下一段对话读"
        —— 而下一段对话会把它当成自己的笔记。加时间戳，新会话只认 `<键>.md`，
        归档件永远不被 `load()` 命中，也永不被当会话笔记注入
        （笔记是"提醒"，归档件是"资料"）。

        消息行留不了档（`messages.session_id` 的键就是会话键本身，逃不掉）。
        """
        note_path = self.get_note_path(session_id)
        if not note_path.is_file():
            return None
        try:
            content = note_path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not content.strip():
            # 空笔记没有归档价值（`save()` 那条"拒绝空覆盖"的另一面）
            try:
                note_path.unlink()
            except OSError:
                pass
            return None

        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        target = note_path.with_name("%s.%s.md" % (note_path.stem, stamp))
        # 同一秒内 `/clear` 两次，撞名。不覆盖，往后挪一秒。
        n = 1
        while target.exists() and n < 60:
            stamp2 = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
            target = note_path.with_name("%s.%s-%d.md" % (note_path.stem, stamp2, n))
            n += 1
        os.replace(note_path, target)     # 原子（Windows + POSIX）
        return target

    def update(
        self,
        session_id: str,
        current_note: str,
        user_message: str,
        assistant_message: str,
        provider,  # BaseProvider for the LLM call
    ) -> str:
        """
        Call the LLM to produce an updated session note based on the
        latest user-assistant exchange.

        Args:
            session_id: Session identifier (used for logging).
            current_note: The existing session note (empty string for new).
            user_message: The user's latest message text.
            assistant_message: The assistant's latest response text.
            provider: LLM provider for the note-update call.

        Returns:
            The updated session note as a Markdown string.
        """
        # Skip garbage exchanges — auth prompts, simple greetings,
        # capability demos.  These pollute the session note with
        # erroneous "pending tasks" that the LLM keeps trying to
        # resolve across restarts.
        _NOISE_PATTERNS = (
            "需要用户授权", "awaiting NL authorisation",
            "⚠️ Agent 想要", "回复:",
            "请重试刚才被阻止",
            "用户已授权此操作",          # injected by NL auth (shouldn't happen but guard)
            "请告诉我您想要执行什么",   # capability demo / eager tool listing
            "请告诉我具体的文件名",     # eager workspace listing
            "我不确定你指的是哪个",     # agent confused by fabricated auth
            "之前被阻止的工具调用",     # agent repeating the fabricated narrative
        )
        if any(marker in assistant_message or marker in user_message
               for marker in _NOISE_PATTERNS):
            return current_note or _new_session_template(session_id)

        # Seed new sessions with the empty template
        if not current_note or not current_note.strip():
            current_note = _new_session_template(session_id)

        # Build a compact prompt with only the current note and latest
        # exchange — the note itself is the cumulative summary.
        user_prompt = (
            f"## CURRENT SESSION NOTE\n\n{current_note}\n\n"
            f"## LATEST EXCHANGE\n\n"
            f"User: {user_message[:800]}\n\n"
            f"Assistant: {assistant_message[:800]}"
        )

        response = provider.chat([
            {"role": "system", "content": SESSION_NOTE_PROMPT},
            {"role": "user", "content": user_prompt},
        ])

        updated = (response.content or "").strip()

        # Strip markdown code fences if the LLM wrapped its output
        if updated.startswith("```"):
            # Remove opening fence line (e.g. `markdown or `)
            first_nl = updated.find("\n")
            if first_nl != -1:
                updated = updated[first_nl + 1:]
            # Remove closing fence
            if updated.endswith("```"):
                updated = updated[:-3]
            updated = updated.strip()

        # 一个字都没拿到，返回旧笔记，不要交出一个空串让调用方去写。
        # `save()` 那边也有守卫（那是最后一道），这里先短路是为了让日志
        # 说实话：调用方打的是 "updated (N chars)"，返回旧笔记时 N 才是真的。
        if not updated:
            return current_note

        return updated

    def exists(self, session_id: str) -> bool:
        """Return `True` if a `.md` file exists for this session."""
        return self.get_note_path(session_id).is_file()

    @staticmethod
    def _sanitize_filename(session_id: str) -> str:
        """Replace characters that are illegal in filenames (Windows)."""
        for char in ':<>"/\\|?*':
            session_id = session_id.replace(char, "_")
        return session_id

    @staticmethod
    def is_archive_stem(stem: str) -> bool:
        """这个文件主干是不是一份归档件（`<键>.<YYYYMMDD-HHMMSS>`）。

        唯一的判据出口 —— `core/memory_organize.list_sessions_touched_since`
        靠它把归档件排除出"会话"（否则那份清单会把归档件当成一个个会话，
        喂错 ≥5 会话闸）。别在别处再抄一份正则。
        """
        return bool(_ARCHIVE_TAIL_RE.match(stem or ""))

    @staticmethod
    def note_stem(session_id: str) -> str:
        """会话号 → 笔记文件的文件名主干（归一化只有这一处）。

        为什么要把它单独开出来：
        记忆巩固要按"最近碰过几个会话"数门控，而它是按磁盘上的文件名数的
        （`list_sessions_touched_since` 返回的是 stem）—— 拿原始的 `session_id`
        （`workspace:private` 这种，带冒号）去比，永远比不相等，
        于是当前会话永远排除不掉。
        顺带一个 Windows 陷阱：`probe:memorganize.md` 这种名字根本不会生成一个
        普通文件 —— 冒号会被系统当成 NTFS 备用数据流（目录里只出现
        `probe`，而 `glob("*.md")` 看不见它）。
        """
        return SessionMemory._sanitize_filename(session_id)

    def get_note_path(self, session_id: str) -> Path:
        """Return the filesystem path to the session note `.md` file."""
        safe = self._sanitize_filename(session_id)
        return self._sessions_dir / f"{safe}.md"
