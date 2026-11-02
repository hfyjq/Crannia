"""后台记忆抽取（fork 形态）。

## 它是什么

主 agent 每轮结束后，轮末 fire-and-forget 一个后台抽取器：不开新对话，
直接拿父对话 + 一条提取指令再问一次模型，把最近对话里值得跨会话保留的事实
用 `memory_write` 写进记忆库。写入本身零新机制（`memory_read/search/write`
三件工具），新的是触发。

## 闸序

  ① 「写记忆」开关（`data/settings.json` 的 `memory_write`）—— 关了什么都不发生
     （开关的读取复用 `memory_store` 那一份，不抄第二份）
  ② 在飞检查 —— 有抽取正在跑，本轮跳过（那一窗消息由游标窗口在下一轮自然覆盖，
     代价在案）
  ③ 请求快照可用性 —— fork 的 `tools`/`system`
     必须取自请求快照（与父同前缀，吃缓存），拿不到就跳过
  ④ 互斥 —— 主 agent 在游标窗口里自己写过记忆，跳过 fork + 推游标
     （"合格轮" = 过了这道闸的轮）
  ⑤ 节流 —— 每 N 个合格轮才跑（默认 3）
  ⑥ 预注入已有清单（`build_memory_index`）—— 放在节流之后，被跳过的轮
     不付这次全库扫描的成本（判据：跳过的轮不该为这次扫描付钱）

## fork 形状（与子 agent 同一条判据：同前缀吃缓存）

- `messages` = 父对话（轮末副本，deepcopy 一次、之后不再改）+ directive
  （经 `agent_loop` 的 `user_message=` 追加在末尾）
- `_tools` / `_system` 取自请求快照；不摘工具表：工具白名单在
  执行侧（`_tool_allowlist`，只许记忆三件真正跑）。判据：给 fork 换一份
  工具表会断掉与父的提示缓存共享（与"执行层拒、tools 不动"同形）。
- `max_turns=5` · 不落库（fork 本来就不写会话行）·
  失败出声 + 游标不推

## 游标（cursor）—— 为什么是指纹而不是下标

没有消息 uuid，用「条数 + 末条指纹」当游标：
`cursor_msgs` = 处理到第几条，`cursor_fp` = 游标所在位置最后一条消息的指纹。**消息表会被
原地改，光存下标会指到错的消息上**、还不出声，所以下一轮先验指纹。
会改表的现在有两处：
① `repersist_messages`（审批重放）整表重写；
② `prune_old_tool_messages`（超 500MB 时删最旧那几组 `role='tool'`）。
压缩只追加、不再重写，它不再会改行序，但另外两处还在，指纹照旧需要。
失配就按两条兜底走（分开的，别合并）：

- 互斥扫描：游标失效，当"没写过"（找不到就是 `false`，不回退全量 ——
  回退会让一条陈年 `memory_write` 把抽取永久关掉）
- 计数（写进 directive 的"N 条"）：游标失效，全量（找不到就数全部 ——
  返回 0 会让抽取永久关掉）

## 三格会话状态（登记在 `core/state_registry.py`，`/clear` 清）

`cursor_msgs` / `cursor_fp` · `turns_since`（节流）· `last_turn_at`（轮末墙钟，
滑窗冷闸读它）；外加 `in_progress`（在飞标志）。
钥匙 = session_id；进程内共享，必须进登记表。

## 三条写在案的取舍

- 线程里跑 `agent_loop`：与子 agent 不同（子在同一线程里同步跑），本 fork 起
  一条 daemon 线程 —— provider 并发是既有的（会话笔记线程就在后台用同一个
  provider）；进程退出时线程直接死掉（与笔记线程同款，v0 不设 drain）。
- 与用户中断共用 session_id：抽取中途用户 `/stop`（cancel 标志落在同一会话号）
抽取那一轮被中断路径收尾，游标照推、那一窗不再重抽。代价极小（后台活），在案。
- deepcopy 发生在闸门之后：节流跳过的轮不付复制成本（与把清单扫描
  放在节流之后同一条理由）。

ASCII-only 打印（Windows GBK 控制台）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core import request_snapshot
from core import tiers

#: 白名单：fork 里只许这几个工具真正执行。
#: 与 `tools=`（照发父的整表，保缓存）不是一回事 —— 那个是呈现，这个是执行。
EXTRACT_ALLOWLIST = frozenset({"memory_read", "memory_search", "memory_write"})

#: fork 的轮次上限（判据：正常抽取 2–4 轮就该做完（读 → 写）；
#: 硬上限防止它陷进验证的兔子洞、白烧轮次）。
EXTRACT_MAX_TURNS = 5

#: 节流旋钮（`data/settings.json` 的 `memory_extract_every_n_turns`，设置页可改）。
#: 默认 20 —— 记忆的主要入口是模型自己判断要不要写；后台抽取只做
#:   "隔一阵再兜一遍"，不是每两轮敲一次门。
#: `0` = 不自动抽取（只靠模型自己写）。它是个真值，不是"没设置"。
THROTTLE_DEFAULT = 20
THROTTLE_MIN = 0
THROTTLE_MAX = 50

#: 会话状态（三格 + 在飞标志）。登记在 `core/state_registry.py` ——
#: 新增/改名/删除都会让 census 当场红。
_state: Dict[str, Dict[str, Any]] = {}


def _blank() -> Dict[str, Any]:
    return {"cursor_msgs": 0, "cursor_fp": "", "turns_since": 0,
            "last_turn_at": 0.0, "in_progress": False}


def forget(session_id: str) -> None:
    """`/clear` 的清理（`state_registry` 的 purge 入口）。"""
    _state.pop(session_id, None)


def last_turn_at(session_id: str) -> Optional[float]:
    """上一轮结束的墙钟（滑窗冷闸读它）。没有记录回 `None` ——

    `None` 与 `0.0` 是两件事：`None` = 从没见过这个会话，
    调用方该按"不冷"处理（理由：会话没有可裁剪的历史，冷闸本来就不用动作）。
    """
    st = _state.get(session_id)
    if not st or not st.get("last_turn_at"):
        return None
    return float(st["last_turn_at"])


# 读设置（两条：开关复用 memory_store 的；节流是本模块自己的）

def _throttle_n() -> int:
    """每几个合格轮抽一次。读不到 / 脏值，默认（"读不到，默认"）。

    `isinstance(True, int)` 是 True，bool 必须单独拒（
    「`effort_level: true` 会被当成档 1」的同款坑）。
    """
    try:
        data = json.loads(Path("data/settings.json").read_text(encoding="utf-8"))
    except Exception:
        return THROTTLE_DEFAULT
    val = data.get("memory_extract_every_n_turns", THROTTLE_DEFAULT)
    if isinstance(val, bool) or not isinstance(val, int):
        return THROTTLE_DEFAULT
    if not (THROTTLE_MIN <= val <= THROTTLE_MAX):
        return THROTTLE_DEFAULT
    return val


# 游标 / 窗口（纯函数，读者直接测它们）

def _fingerprint(msg) -> str:
    """一条消息的稳定指纹（跨进程稳定 —— 不用 `id()`/`hash()`，字符串哈希逐进程随机化）。"""
    try:
        blob = json.dumps(msg, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 —— 指纹不许把抽取炸掉
        blob = repr(msg)
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:16]


def _window(messages: List[dict], st: Dict[str, Any]) -> Optional[List[dict]]:
    """游标之后的消息窗口。`None` = 游标失效（表被压缩重写等）—— 见模块 docstring。"""
    n = int(st.get("cursor_msgs") or 0)
    if n <= 0:
        return list(messages)
    if n <= len(messages) and _fingerprint(messages[n - 1]) == st.get("cursor_fp"):
        return messages[n:]
    return None


def _wrote_memory_since_cursor(messages: List[dict], st: Dict[str, Any]) -> bool:
    """互斥判据：窗口里有没有 `memory_write` 的调用。

    判的是"调用过"，不是"写成功" —— 主 agent 试过就够了：读它调用参数
    里的 name 去对齐只会造第二个判据面（与"判提到不判碰到"同一条取舍）。
    """
    window = _window(messages, st)
    if window is None:
        return False          # 游标失效，当"没写过"（不许回退全量，见 docstring）
    for m in window:
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            if isinstance(tc, dict) and \
                    (tc.get("function") or {}).get("name") == "memory_write":
                return True
    return False


def _count_new(messages: List[dict], st: Dict[str, Any]) -> int:
    """写进 directive 的"N 条"：窗口里的模型可见消息数。

    游标失效时回退全量 —— 与互斥那条方向相反（判据：宁多数、不返回 0，
    "0 会让抽取永久关掉"）。
    """
    window = _window(messages, st)
    src = window if window is not None else messages
    return sum(1 for m in src if m.get("role") in ("user", "assistant"))


def _advance(messages: List[dict], st: Dict[str, Any]) -> None:
    """把游标推到表尾（成功抽取 / 互斥跳过时调；失败不调）。"""
    if messages:
        st["cursor_msgs"], st["cursor_fp"] = len(messages), _fingerprint(messages[-1])
    else:
        st["cursor_msgs"], st["cursor_fp"] = 0, ""


# 提示词（素材：抽取提示词 + 记忆类型表；
#         七类目的打捞件来自删掉的 `core/memory/fact_extractor.py`）

def build_extract_prompt(new_count: int, manifest: str) -> str:
    """fork 的 directive —— 附在父对话末尾的唯一一段新指令。

    每个字都按每次抽取计费（不是每请求）—— 改措辞要重量，
    这里计费单位是"每 N 个合格轮一次调用"。
    """
    manifest_block = ""
    if manifest:
        manifest_block = (
            "【已有记忆】写之前先看这份清单 —— 能更新已有的就**不要**新建（避免写重）：\n"
            f"{manifest}\n"
            "（要某条的全文用 memory_read(name)。）\n\n"
        )
    return (
        "你现在临时担任「记忆抽取」子任务：只做一件事 —— 从上面这段对话里挑出"
        "**值得跨会话保留的事实**，写进长期记忆。这不是在回复用户，不要对用户说话。\n\n"
        "【工具与预算】你只有三个工具：memory_search / memory_read（先查已有，避免写重）、"
        "memory_write（写入）；**其余工具一律会被拒绝**，别试。上限 5 轮工具调用，"
        "最优打法：第 1 轮把该查的一次发完，第 2 轮把该写的一次发完，不要读一条写一条。\n\n"
        f"【素材边界】只依据**最近约 {new_count} 条消息**（以及查到的已有记忆），"
        "**不要**为核实去翻代码、跑命令、查网页 —— 为查证多花轮次是浪费，"
        "写不确定的东西比不写更坏。\n\n"
        f"{manifest_block}"
        # 四类型 + 四情形之外，还必须有一条总纲 ——
        #   否则 `project`（正在做的事）与【不要记】的「进行中的活」直接打架，
        #   模型对不上号时自由发挥，抽出一堆无关紧要的。
        #   出事之后补一句永远补不完：补的是情形，不是原则。
        "【★★ 总纲 —— 先看这一句，再对下面四条】\n"
        "只记「**不能从当前项目状态推出来**」的东西：读代码、grep、`git log`、项目文档\n"
        "能得到的，**一律不记**。下面四条是它的**具体情形**，**不是它的全部** ——\n"
        "★ 遇到对不上号的场面，**按这一句判**，别按「它像哪一条」判。\n\n"
        "【该记什么】（四类；其余不记）\n"
        "- user：用户是谁 —— 身份、角色、偏好、习惯、常用工具/语言/工作流\n"
        "  ⚠️ **不许写**两类：可能被读成**负面评价**的；**与手上这件事无关**的。\n"
        "- feedback：用户对「该怎么做工作」给的指导 —— 纠正（「别这样」）与确认（「对，就这样」）"
        "**都算**；要写上**为什么**（下次遇到边缘情况才判断得了）\n"
        "  ★ **纠正好认、确认难察觉** —— 用户说「对，就这样」也是指导，要留意。\n"
        "  ★ **失败与成功都要记** —— 只记纠正会让模型**偏离用户已验证过的做法**，"
        "变得过度谨慎。\n"
        "- project：正在做的事 —— 目标、架构决策、硬性约束（不许做什么）、已经定下的取舍\n"
        "  ★★ **限定（缺了它这一条会与下面的【不要记】打架）**："
        "**只记「不能从代码或 git 历史推出来」的那些**。\n"
        "  ★ 这类**变得快** -> 写的时候**带上时间**；"
        "**相对日期一律转绝对**（「周四」-> 「2026-10-08」）。\n"
        "- reference：哪里能找到什么 —— 外部系统入口、人/项目/系统之间的关系、"
        "用户分享过的关键事实\n\n"
        "【不要记】（查得到、或会过期）\n"
        "- 代码结构、文件路径、项目惯例 —— 读代码就有\n"
        "- git 历史、谁改了什么 —— git log 才是权威\n"
        "- 调试过程与修复配方 —— 修复在代码里\n"
        "- 项目文档里已经写着的；一次性的任务细节（进行中的活、临时状态）\n"
        "★ 用户明确要求记的优先 —— 但活动流水账（「这周做了 X」）例外："
        "只记其中**意外/不显然**的部分。\n"
        "★★ **反过来的那半同样算数**：用户说「**别记 / 忘掉 / 不要存**」的，"
        "**一个字都不许写**。"
        "⚠️ 你**没有删除工具** —— 已存在的旧条目**不要**试图改写或绕过，"
        "让用户去「设置页 → 记忆库」自己删（删除是**用户**的动作）。\n\n"
        "【绝不许记】（这一条**压倒**上面「该记什么」）\n"
        "- ★ **凭据与机密**：API key、密码、token、口令、私钥、连接串里的凭据 —— "
        "**一个字都不许写**，**用户主动口述也不写**（那不是「用户要求记」，是危险）。\n"
        "  ⚠️ 这条是**我们自己的加强**：本仓的 `settings.json` 里躺着明文邮箱口令"
        "⇒ 对话里出现凭据的概率不是零。\n"
        # 三条同源的纪律 ——
        # 都是"把不是事实的东西写成了事实"。
        "- ★★ **你的推测不是事实**：**你自己推断出来的东西**（尤其「**为什么**失败了 /"
        "为什么不对」这类**归因**）**一个字都不许写** —— 那是解释，不是观察到的事实，"
        "写进去之后会被当成「已知」复用。\n"
        "  ⚠️ 典型失败：模型把「搜不到文件」归因成"
        "**「文件名里带了个多余的右括号」**（真因是**索引没刷新**），"
        "而这条**错误归因**被写进了长期记忆。\n"
        "- ★★ **二手身份不算「用户是谁」**：**只写用户自己说过的**。"
        "你在**工具结果里读到的**、或**你自己算出来的**身份信息（身份证号 / 住址 / 电话…），"
        "**不许写成「用户自称…」** —— 那是把第三方数据（甚至测试数据）冒充成用户的陈述。\n"
        "  ⚠️ 典型失败：探针文件里的**假身份证号**被写成「**用户自称**身份证号为…」，"
        "还带了一句「涉及该号的校验任务时用」。\n"
        "- ★★ **一次交互 != 长期偏好**：从**单次**对话里概括出的「用户习惯 / 用户要什么」"
        "**不许写** —— 那只是**一个样本**。要写 user / feedback 类，"
        "得是用户**明确说过**的偏好，或**多轮 / 多次**一致的表现。\n"
        "  ⚠️ 典型失败：「用户让『读一下报表』时，期望的是结构化要点小结」"
        "「用户拿到报表后要的是『数据+结论+建议+图』四件套」—— **两条都来自一次交互**。\n\n"
        "【怎么存】一条记忆 = 一次 memory_write(name, description, content)：\n"
        "- name ≤40 字（索引用）；description ≤120 字（一句话钩子，"
        "说清「什么时候该想起它」）\n"
        "- content ≤4000 字：先写事实本身；feedback / project 类再用 \"**Why:** …\" 与 "
        "\"**How to apply:** …\" 两行收尾\n\n"
        "【收尾】没有值得记的就**什么都不写**，回一句「无新记忆」。宁缺毋滥。"
    )


# 闸门（同步、便宜）+ 开线程

def _status(status: Optional[Callable[[str], None]], line: str) -> None:
    if status is not None:
        status(line)
    else:
        print(line)


def _spawn(*, target, kwargs) -> threading.Thread:
    """起后台线程 —— 单独一个函数是为了测试能同步替换它（`_spawn` 猴子补丁）。"""
    t = threading.Thread(target=target, kwargs=kwargs, daemon=True)
    t.start()
    return t


def maybe_extract(*, session_id: str, messages: Optional[List[dict]], provider,
                  status: Optional[Callable[[str], None]] = None) -> None:
    """轮末入口（`MemoryManager.record_turn` 调）。同步部分只做闸门，重活在后台线程。

    *status* 是状态行回调（生产 = manager 的 `_note_bg_status`，收进有界缓冲
    由 CLI 冲刷 —— 后台线程直接 print 会跟 `You>` 提示行交错）。
    """
    if not session_id or not messages or provider is None:
        return
    st = _state.setdefault(session_id, _blank())
    # `last_turn_at` 每轮都记（与闸门无关）—— 7b 的冷闸判的是"上一轮结束是多久前"。
    st["last_turn_at"] = time.time()

    # ① 开关（复用 memory_store 那一份读取，不抄第二份）
    from core import memory_store as _store
    if not _store._memory_write_enabled():
        return

    # ② 在飞，本轮跳过（简化版，见模块 docstring）
    if st.get("in_progress"):
        _status(status, "[memory] 后台抽取进行中 —— 本轮跳过")
        return

    # ③ 请求快照（fork 的 tools/system 只能从这里拿；拿不到就 fork 不出"同前缀"）
    snap = request_snapshot.get_current()
    if snap is None or snap.session_id != session_id:
        _status(status, "[memory] 没有本会话的请求快照 —— 跳过后台抽取")
        return

    # ④ 互斥：主 agent 自己写过，跳过 + 推游标（节流计数不动，同一条判据）
    if _wrote_memory_since_cursor(messages, st):
        _advance(messages, st)
        _status(status, "[memory] 主 agent 本轮已写记忆 —— 跳过后台抽取（游标前进）")
        return

    # ⑤ 节流：每 N 个合格轮一次（在互斥之后递增 —— 写过的那一轮不算"合格"）
    n = _throttle_n()
    #: `0` = 不自动抽取 —— 直接返回（游标不动、计数不动）：
    #:   只靠模型自己在轮内 `memory_write`。这是三档预设里「不计」那一档。
    if n <= 0:
        return
    st["turns_since"] = int(st.get("turns_since") or 0) + 1
    if st["turns_since"] < n:
        return
    st["turns_since"] = 0

    # ⑥ 开跑：deepcopy 在这里（节流跳过的轮不付复制成本；fork 必须冻在轮末这一刻，
    #    子 agent 的同款判据：快照捕获一次、不重新 fork 父的新历史）
    #    把这个状态对象本身交给线程（不是让它按会话号再取一次）：`/clear` 可能
    #    在抽取在飞时把登记表里那条换掉 —— 线程收尾时推的游标该落回它出发时的那份，
    #    不许写进新对话那一格（写进去 = 新对话一开口就继承旧游标）。
    st["in_progress"] = True
    _spawn(target=run_extraction, kwargs=dict(
        session_id=session_id,
        messages=copy.deepcopy(messages),
        provider=provider,
        tools=list(snap.tools),
        system=snap.system,
        status=status,
        st=st,
    ))


# 后台本体（测试直接调它 —— 不走线程）

def run_extraction(*, session_id: str, messages: List[dict], provider,
                   tools, system: str,
                   status: Optional[Callable[[str], None]] = None,
                   st: Optional[Dict[str, Any]] = None) -> None:
    """线程体：拼 directive → fork 一把 → 收集写了几条 → 推游标 / 发事件。

    *messages* 必须是调用方给好的副本（我们会让 `agent_loop` 往里 append）。
    *st* 是 `maybe_extract` 交下来的同一个状态对象（见那边的注释：`/clear`
    竞态下不许写进新对话那一格）；直接调用（测试）时可以不给，按会话号现取。
    永不抛：失败出声 + 游标不推（"best-effort"）。
    """
    if st is None:
        st = _state.setdefault(session_id, _blank())
    # 后台活的时间跨度（`bg.start` / `bg.end`）—— **`bg_end` 落在既有的
    #   `finally` 上**，所以失败也收口（"有 start 无 end"的孤儿才分得清是
    #   "还在跑"还是"炸了没报"）。落盘之后就能回答「后台活跟哪一轮撞过」。
    from core.events import bg_start, bg_end
    _bg_t0 = bg_start("extract", session_id)
    try:
        # ⑥ 预注入已有清单（节流之后才付这次全库扫描成本）
        from core import memory_store as _store
        manifest = _store.build_memory_index()
        directive = build_extract_prompt(_count_new(messages, st), manifest)

        from core.agent_loop import agent_loop
        # `_n_before` = fork 的出发点。
        #    没有它，`_collect_written` 会把父会话历史里早已成功的
        #    `memory_write` 一起数进来，回执报「已保存 N 条」而磁盘只多几条。
        #    例：父之前写过 3 条，本轮 fork 只写 2 条，事件报 5。
        #    更坏的一面：本会话写过一次之后，哪怕 fork 一条没写，
        #    事件照样报"已保存"，「无新记忆」那一支永远走不到，事件失去判别力。
        _n_before = len(messages)
        _content, out_msgs, _chunks, _tokens = agent_loop(
            user_message=directive,
            provider=provider,
            messages=messages,            # ← 副本（agent_loop 会 append directive + 回执）
            session_chunks=[],
            memory_context="",            # 有 `_system` 时它不参与
            max_turns=EXTRACT_MAX_TURNS,
            session_id=session_id,        # 沿用父的会话号（门禁 / 白名单都按它）
            _tools=tools,                 # 父的整表（保缓存；执行白名单在下面那行）
            _system=system,               # 父那条请求的 system（同上前缀判据）
            _tool_allowlist=EXTRACT_ALLOWLIST,
            # fork 的第五个 cacheSafeParams ——
            # 思考模式进提示缓存键，按子 agent 同一条取"同一个会话的档"。
            thinking=tiers.current_spec(session_id).thinking,
            _emit=None,                   # 后台活不进父的时间线
        )
    except Exception as exc:  # noqa: BLE001 —— 判据：best-effort，绝不把失败报成成功
        _status(status, f"[memory] 后台抽取失败（游标不推，下轮重试）："
                        f"{type(exc).__name__}: {exc}")
        return
    finally:
        st["in_progress"] = False
        bg_end("extract", session_id, _bg_t0)

    # 成功才推游标（失败让消息被下次重看）。
    # 只有"没落在别人后面"才推：下一轮的互斥跳过可能已经推得更远
    #    （抽取线程与下一轮并发）—— 推小了会让老消息被重扫。
    if len(messages) >= int(st.get("cursor_msgs") or 0):
        _advance(messages, st)

    # 只数 fork 之后 那段（见上面 `_n_before` 的注释）——
    #   历史段的成功写入不属于"这一次抽出来的"。
    saved = _collect_written(out_msgs[_n_before:])
    if saved:
        names = saved[:8]
        try:
            from core.events import EVT_MEMORY_SAVED, get_event_bus
            get_event_bus().emit(EVT_MEMORY_SAVED, session_key=session_id,
                                 count=len(saved), names=names)
        except Exception as exc:  # noqa: BLE001 —— 事件发不出去不许影响抽取本身
            print(f"[memory] memory.saved 事件发送失败：{type(exc).__name__}: {exc}")
        _status(status, f"[memory] 后台抽取完成 —— 写入 {len(saved)} 条记忆："
                        f"{'、'.join(names)}")
    else:
        _status(status, "[memory] 后台抽取完成 —— 无新记忆")


def _collect_written(out_messages: List[dict]) -> List[str]:
    """从 fork 的产出里收集写成功的记忆名。

    判据看回执（`memory_write` 的 handler 返回 `{"success": …, "name": …}`，
    被 `agent_loop` 序列化成 `role: tool` 的 JSON）—— 不是看它调用过：
    参数校验失败的调用没有写成任何东西，不能算数（"部分产出不是成功"）。

    契约：传进来的必须是「fork 新增的那一段」 —— 调用方负责切
    （`run_extraction` 用 `out_msgs[_n_before:]`）。本函数分不出新旧：
    它只看"有没有成功的回执"，而父会话历史里同样躺着成功的回执。
    不切，回执报 5 而磁盘只多 2，**"已保存 N 条"不再表示"这一次抽出了
    东西"（「回执/声明比事实更自信**」的又一例）。
    """
    call_ids = set()
    for m in out_messages:
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            if isinstance(tc, dict) and \
                    (tc.get("function") or {}).get("name") == "memory_write":
                cid = tc.get("id")
                if cid:
                    call_ids.add(cid)
    names: List[str] = []
    for m in out_messages:
        if m.get("role") != "tool" or m.get("tool_call_id") not in call_ids:
            continue
        try:
            payload = json.loads(m.get("content") or "")
        except Exception:  # noqa: BLE001 —— 半截 JSON 当"没写成"（这里不致命）
            continue
        if isinstance(payload, dict) and payload.get("success"):
            nm = payload.get("name") or payload.get("slug")
            if nm:
                names.append(str(nm))
    return list(dict.fromkeys(names))     # 去重保序（同一条被写两次算一条）
