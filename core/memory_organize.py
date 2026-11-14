"""记忆巩固 —— 离线巩固长期记忆（跨层搬运那一档）。

## 它是什么

主 agent 每轮结束后，轮末 fire-and-forget 一个巩固器：把这段时间留在
短期痕迹（日报与会话笔记）里的东西搬进长期记忆，再把长期记忆里
已经重复、臃肿或互相矛盾的部分理顺。

## 这套形状从哪儿来

离线巩固不是我们随手打的比方，是人类记忆里被研究得很清楚的一件事：

- 白天留下的是零散、容易被覆盖的短时痕迹（海马里的那一份）；
- 睡眠中这些痕迹被反复重放，挑出还需要的部分沿通路搬到另一处，
  和那里已有的知识接上；
- 同一段时间里，一批没被用到的连接被整体削弱，好让第二天还有余量。
  削减的是冗余，不是记忆本身。

三段各有各的活 —— 搬运、整合、修剪 —— 本模块的分工就是照它切的，
不是拿它当比喻：

| 那边 | 这边 |
|---|---|
| 容量小、随手被覆盖的短时痕迹 | 日报与会话笔记 |
| 容量大、条目之间互相引用的那一处 | `data/memory/facts/` |
| 重放挑出还需要的部分 | 只搬稳定的事实，不把这一轮原样复制 |
| 并列的痕迹收敛成一条 | 几条讲同一件事的并成一条 |
| 新痕迹和旧知识冲突时以后者为准被覆盖 | 依据更新的那条压过旧的，在原处改掉 |

有一处我们与它不同：不做"全局衰减"。那一步在这里没有对应物 ——
让机器替用户把记忆慢慢调淡，是替他做了一个他不一定会做的决定。

## 与抽取的分工：不是快慢，是层次

| | 抽取（`core/memory_extract.py`） | 巩固（本模块） |
|---|---|---|
| 何时 | 每 3 个合格轮，一轮刚结束 | ≥24 小时 + ≥5 个会话之后 |
| 做什么 | 这一轮刚发生的事实就地写进去 | 把散在短期里的搬过来，并把长期理顺 |
| 尺度 | 单轮、当场 | 跨会话、离线 |

写入工具是同一套（记忆三件）—— 巩固没有发明任何新的写机制。

## 闸序（最便宜的先判）

  ① 开关（两道：写记忆总闸 + 巩固自己那把 `memory_organize`）
     —— 写记忆总闸关着就直接 `false`，两道都开才走。
  ② 在飞检查 —— 有巩固正在跑，本轮直接返回
  ③ 时间闸 —— 距上次成功巩固 ≥ `MIN_HOURS`（24 小时）。
     从没巩固过就是 `0.0`，必然通过
  ④ 扫描节流 —— 卡在③与⑤之间：③过了而⑤没过时，"上次巩固时刻"不动，
     于是③每轮都会过；不节流就等于每轮把 sessions 目录整个 stat 一遍
  ⑤ 会话闸 —— 上次巩固之后碰过的会话（不含当前这条，它刚被写过）≥ `MIN_SESSIONS`
  ⑥ 取互斥（见下）；拿不到就返回
  ⑦ 开跑（后台线程）

## 互斥与状态：一个目录 + 一个 json

```
data/memory/.consolidation/
    lock/          ← `os.mkdir` 即互斥量：它本身是原子的，已存在就报错
        holder     ← 一行 `<pid> <since>`；取到之后重读校验
    state.json     ← {"last_round": <ts>}；成功之后才写
```

逐条：

- 用目录不用文件：`mkdir` 的原子性由文件系统给，不需要"先写再读回来"那一套。
- 陈旧线 30 分钟：即使 PID 活着，超过这个时长也回收 —— 防的是 PID 复用
  （死掉的那个号被系统发给了别人）。
- 回收 = 删掉整个 `lock/` 再重建；两个回收者同时动手时，后建成的那个赢，
  输的那个在重读校验里看到 holder 不是自己，退出。
- 失败不推进"上次巩固时刻"：那个数只在成功之后写。
  所以这里没有"回滚时间戳"这回事 —— 原来那套（把时刻编码进文件的 mtime、
  失败再 `utime` 倒回去）有两个自带的问题：Windows 上要额外小心 mtime 精度；
  而且任何外部动作碰一下那个文件，24 小时闸就被悄悄重置了。
  显式字段两个问题都没有。
- 退避不靠重试：一次没跑成，下一轮的③会把时间重新攒起来。

## fork 形状（与抽取逐字同一条路：同前缀吃缓存）

`messages` = 父对话轮末副本 + directive（走 `agent_loop` 的 `user_message=`）；
`_tools` / `_system` 取请求快照；`max_turns` 见 `ORGANIZE_MAX_TURNS`；
白名单执行侧（与抽取同一份名单）；`thinking` 取同会话档（fork 的第五个
cacheSafeParams）。不落库（我们的 fork 本来就不写会话行）。

## 读得比主线宽：`memory_read` / `memory_search` 的巩固模式

巩固要看的两类素材（日报、会话笔记）不在记忆条目里，而刻意
不去加宽 `DEFAULT_FRAMEWORK_READABLE_DIRS`（那是给所有轮的所有工具的全局
声明）。做法：巩固器在 fork 期间置一个 `ContextVar`（`memory_store.organize_read_mode`），
同一对专用工具在这个模式下多认两类只读名字：

    memory_read("logs/YYYY-MM-DD")        → <storage_path>/logs/YYYY-MM-DD.md
    memory_read("sessions/<会话号>")       → <storage_path>/sessions/<会话号>.md

只读、白名单式：名字不许带路径分隔符与 `..`（越界就地拒）。
主线（模式关）行为一个字节不变 —— 默认 `None`，那两行分支根本不进。

## 五条取舍

- 不做删除。被推翻的事实改写它，重复的合并进保留的那条 —— 修剪的是
  「同一件事留下了几份」，不是「把它从记忆里抹掉」。巩固没有删除工具：
  真正的删除是用户动作（tombstone 挂在写路径上，想写回删过的条目会被拒）。
- 不做全局衰减。有一类设计是让整库随时间一起变淡；我们不做 ——
  用户的记忆不该被机器悄悄削弱。要做只能是用户自己动手。
- 占并发名额：巩固是第 4 个会跑的东西，开跑前拿
  `scheduler._turn_slots` 的一个名额（与用户轮次同一个上限，当前是 5），跑完释放。
  代价：巩固在跑时用户轮次的可用名额少一个（这是有意的）。
- `memory_search` 在「巩固模式」下不扫日报/会话笔记：那会是一次无界全扫；
  素材按名字读，名字由 directive 列出来（比盲 grep 更准）。
- 阈值（24h / 5 / 10min）是常量不是旋钮：设置页只暴露「开关」；
  阈值不走用户可调配置，不为它发明旋钮。测试用猴补改常量。

ASCII-only 打印（Windows GBK 控制台；emoji 会让整个运行崩）。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import request_snapshot
from core import tiers
from core.memory_extract import EXTRACT_ALLOWLIST

#: 巩固 fork 的工具白名单 —— 与抽取同一份（一个判据一处定义；
#: 两份各写一遍迟早漂移）。巩固"读得更宽"靠的是 `memory_read` 的巩固模式，
#: 不是多给几个工具名。
ORGANIZE_ALLOWLIST = EXTRACT_ALLOWLIST

#: fork 的轮次上限。这是一条护栏：巩固会连着读好几份素材再写，
#: 但不设上限就可能变成无限探索（它占着并发名额，代价落在用户身上）。
ORGANIZE_MAX_TURNS = 15

#: 巩固用的那一格目录（在记忆目录里）—— 互斥与"上次巩固时刻"都放这里。
STATE_DIR = ".consolidation"

#: 互斥目录名：`mkdir` 成功即持有。
LOCK_DIR = "lock"

#: 持有者那一行的文件名：`<pid> <since>`。
HOLDER_FILE = "holder"

#: 上次成功巩固的时刻（`{"last_round": <ts>}`）。只在成功之后写。
STATE_FILE = "state.json"

#: 陈旧线：即使 PID 活着，超过这个时长也算陈旧（PID 复用护栏）。
#:
#: 30 分钟 = 一次巩固耗时上界的两倍。上界怎么来：`ORGANIZE_MAX_TURNS`
#: 十五轮 × 每轮 60 秒的保守上界 = 15 分钟；双倍留容错，向上取整到 30。
#: 取两倍的理由是一侧代价不对称 —— 太短会把一个还在跑的巩固判成死的，
#: 两个进程同时改同一批条目；太长只是崩掉那次晚一点被回收。
STALE_MS = 30 * 60 * 1000

#: 扫描节流 —— 见模块 docstring ④。
#:
#: 10 分钟 = 一段连续工作的长边。它防的不是 CPU（实际扫一遍 115 份
#: 笔记 6.67 ms，可以忽略），而是"会话闸没过时每轮都白扫一遍"。
#: 量纲因此是"一段连续工作有多长"：95% 的相邻调用间隔 ≤ 357 秒
#: （n=4454），窗口必须长过它，才能做到同一段连续工作里最多扫一遍。
SCAN_INTERVAL_MS = 10 * 60 * 1000

#: 门控：两次巩固之间至少隔 24 小时（一晚一次）；这期间碰过的会话少于 5 个就不值得搬。
#:
#: 24 小时：巩固是离线活，一天一次已经比素材增长快 —— 再密就是在重复搬运。
#: 5 个会话：会话笔记中位 1395 字符（115 份），5 份 ≈ 7000 字节的素材。
#: 这里数"个数"而不是"字节"是有意的：一段对话就是一份笔记，个数能对用户解释
#: （"你最近碰过 5 个会话"），字节数解释不了。量纲换成字节是另一种设计，
#: 换的话连"什么算攒够了"这句话都要重写 —— 今天不改。
MIN_HOURS = 24
MIN_SESSIONS = 5

#: directive 里最多列几条日报（从最新往前）—— 列太多等于把"别通读"这句话作废。
MAX_LOG_NAMES = 14

#: 进程级状态（不是会话级 —— 巩固是进程里至多一个的东西；登记在
#: `core/state_registry.py` 的 `PROCESS_SHARED`，加/改/删都会让 census 当场红）。
#: `last_scan_at` = 上次扫会话的时刻（节流用）；`in_progress` = 在飞标志。
_state: Dict[str, Any] = {"last_scan_at": 0.0, "in_progress": False}


# 开关（两道：写记忆总闸复用 memory_store 那一份；巩固自己这把在本模块）

def _organize_switch() -> bool:
    """「记忆巩固」开关。读不到 / 缺键 / 坏 JSON，默认开
    （与其它开关同一口径：读不到，默认）。"""
    try:
        data = json.loads(Path("data/settings.json").read_text(encoding="utf-8"))
    except Exception:
        return True
    val = data.get("memory_organize", True)
    return val if isinstance(val, bool) else True


def is_organize_enabled() -> bool:
    """两道闸都开才走（写记忆总闸关着就直接 `false`）。

    写记忆关着的时候巩固没有意义 —— 它一个字节都写不进去
    （`memory_store.write_entry` 会在执行侧拒）。
    """
    from core import memory_store as _store
    if not _store._memory_write_enabled():
        return False
    return _organize_switch()


# 互斥与状态 —— 纯文件操作，读者直接测

def state_root(memory_root) -> Path:
    return Path(memory_root) / STATE_DIR


def lock_path(memory_root) -> Path:
    """互斥是目录，不是文件：`mkdir` 的原子性由文件系统给。"""
    return state_root(memory_root) / LOCK_DIR


def _pid_running(pid: int) -> bool:
    """PID 还活着吗。不许用 `os.kill(pid, 0)` —— 在 Windows 上它等于
    `TerminateProcess`（Python 文档：给任何其它信号值都会让进程被无条件杀掉）
会把别人的进程杀掉。
    用 `psutil.pid_exists`（在 `requirements.txt` 里）。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        import psutil
        return bool(psutil.pid_exists(pid))
    except Exception:  # noqa: BLE001 —— 探针坏了当"没有活持有者"（最坏是抢锁）
        return False


def last_round_at(memory_root) -> float:
    """上次成功巩固的时刻。没记录就是 `0.0`（= 从没巩固过，闸必然通过）。

    读的是一格显式字段，不是某个文件的 mtime —— 后者会被任何外部动作
    （备份、同步、编辑器）顺手重置，而 24 小时闸跟着一起重置是查不出来的。
    每轮代价：一次 `read_text`。
    """
    try:
        raw = (state_root(memory_root) / STATE_FILE).read_text(encoding="utf-8")
        return float(json.loads(raw).get("last_round") or 0.0)
    except (OSError, ValueError, AttributeError, TypeError):
        return 0.0


def mark_round_done(memory_root) -> None:
    """记下"这一轮成了"。只在这里写 —— 失败时这个数一个字节都不动，
    下一轮的时间闸自然重新攒够。写临时文件再 `os.replace`：读者永远
    看不到半截 JSON。"""
    root = state_root(memory_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / (STATE_FILE + ".tmp")
        tmp.write_text(json.dumps({"last_round": time.time()}), encoding="utf-8")
        os.replace(tmp, root / STATE_FILE)
    except OSError as exc:
        print("[memory] 巩固：记状态失败（%s）—— 下一轮会再跑一遍" % exc)


def _read_holder(d: Path) -> Tuple[Optional[int], float]:
    """holder 那一行 -> `(pid, since)`；读不懂给 `(None, 0.0)`。"""
    try:
        parts = (d / HOLDER_FILE).read_text(
            encoding="utf-8", errors="replace").split()
        pid = int(parts[0])
        return pid, float(parts[1]) if len(parts) > 1 else 0.0
    except (OSError, ValueError, IndexError):
        return None, 0.0


def hold_round(memory_root) -> bool:
    """取互斥。`True` = 这一轮归我，跑完（成或败）都要 `release_round()`。

    按序三件：`mkdir` 成功即持有；已存在时判陈旧（holder 死掉 / 超时 /
    读不懂又不像刚建的）；回收之后重建。最后一步是重读校验 ——
    两个回收者会同时动手，后建成的赢，输的那个在这里看到 holder
    不是自己，安静退出。
    """
    d = lock_path(memory_root)
    try:
        d.mkdir(parents=True)
    except FileExistsError:
        pid, since = _read_holder(d)
        if pid is None:
            # 读不到 holder：可能是别人刚 mkdir 完、还没写。给它两秒。
            try:
                if (time.time() - d.stat().st_mtime) < 2.0:
                    return False
            except OSError:
                return False
        elif _pid_running(pid) and (time.time() - since) < STALE_MS / 1000.0:
            return False
        shutil.rmtree(d, ignore_errors=True)
        try:
            d.mkdir(parents=True)
        except FileExistsError:
            return False
    except OSError:
        return False
    try:
        (d / HOLDER_FILE).write_text(
            "%d %.6f" % (os.getpid(), time.time()), encoding="utf-8")
        parts = (d / HOLDER_FILE).read_text(encoding="utf-8").split()
        return bool(parts) and parts[0] == str(os.getpid())
    except OSError:
        return False


def release_round(memory_root) -> None:
    """还回去。成功与失败都调它 —— 互斥从来不是"上次巩固时刻"，
    那个数是另一格（见 `mark_round_done`）。"""
    shutil.rmtree(lock_path(memory_root), ignore_errors=True)


def list_sessions_touched_since(sessions_dir, since_ts: float) -> List[str]:
    """`since_ts` 之后被碰过的会话（判 mtime，不判创建时间）。

    我们的"会话碰过" = 会话笔记文件 `<storage_path>/sessions/<stem>.md` 的 mtime
    （`SessionMemory` 每轮写它）。`since_ts == 0`，全部算。

    返回的是磁盘上的文件名主干（`stem`），不是原始的 `session_id` ——
    会话笔记的文件名是洗过的（`:` → `_`）。要比"是不是当前会话"请走
    `sessions_to_review()`，别直接拿 `session_id` 比。
    """
    root = Path(sessions_dir)
    if not root.is_dir():
        return []
    # 归档件（`<键>.<YYYYMMDD-HHMMSS>.md`）不算会话。
    # 判据：`/clear` 一次次归档留下的文件不是"碰过的会话" —— 让它们进这份清单
    # 会把 ≥5 会话闸喂早（用户在一个会话里 clear 5 次 ≠ 碰过 5 个会话），
    # 而"归档件本身"是素材，走 `list_note_archives` 那条独立的路。
    from core.memory.session_memory import SessionMemory
    out: List[str] = []
    for p in sorted(root.glob("*.md")):
        if SessionMemory.is_archive_stem(p.stem):
            continue
        try:
            if p.stat().st_mtime > since_ts:
                out.append(p.stem)
        except OSError:
            continue
    return out


#: 一次巩固最多附几份归档笔记（同 `list_daily_logs` 的 `MAX_LOG_NAMES` 口径：
#: 清单是指路用的，不是把素材塞进提示词）。
MAX_ARCHIVE_NAMES = 20


def list_note_archives(sessions_dir, since_ts: float = 0.0) -> List[str]:
    """被 `/clear` 归档的会话笔记主干（从新到旧）。

    它和 `list_sessions_touched_since` 是两条路，别合：
      · 那一份答"碰过几个会话"，喂 ≥5 会话闸，必须排除归档件
      · 这一份答"有哪些历史素材可读"，喂提示词的素材清单
    合起来写的代价：闸会随 `/clear` 次数膨胀，而没有任何东西会红。
    """
    from core.memory.session_memory import SessionMemory
    root = Path(sessions_dir)
    if not root.is_dir():
        return []
    out: List[Tuple[float, str]] = []
    for p in root.glob("*.md"):
        if not SessionMemory.is_archive_stem(p.stem):
            continue
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if mt > since_ts:
            out.append((mt, p.stem))
    out.sort(reverse=True)
    return [s for _mt, s in out[:MAX_ARCHIVE_NAMES]]


def sessions_to_review(sessions_dir, since_ts: float, session_id: str) -> List[str]:
    """给巩固看的那份会话清单 = `list_sessions_touched_since` 去掉当前会话。

    比较必须在归一化之后（`SessionMemory.note_stem`）：磁盘上的 stem 是洗过的
    （`workspace:private` → `workspace_private`），拿原始 `session_id` 去比
    永远不相等，当前会话会被算进会话闸（它是最容易被算进去的那个，
    因为它的笔记刚刚才被写过）。
    """
    from core.memory.session_memory import SessionMemory
    me = SessionMemory.note_stem(session_id)
    return [s for s in list_sessions_touched_since(sessions_dir, since_ts) if s != me]


def list_daily_logs(logs_dir) -> List[str]:
    """可用的日报名（`YYYY-MM-DD`，从新到旧）。读不成，空表（不是错）。"""
    root = Path(logs_dir)
    if not root.is_dir():
        return []
    names: List[str] = []
    for p in root.glob("*.md"):
        names.append(p.stem)
    names.sort(reverse=True)
    return names[:MAX_LOG_NAMES]


# 提示词

def build_organize_prompt(*, sessions: List[str], log_names: List[str],
                          archives: Optional[List[str]] = None) -> str:
    """巩固 fork 的 directive —— 附在父对话末尾的唯一一段新指令。

    按三类动作组织，不按阶段编号：模型要的是"做什么、什么算做对了"，
    而不是"第几步"。三类都从我们的数据形状长出来 ——
    索引是派生的、没有删除工具、素材是日报与会话笔记。

    每个字都按每次巩固计费（门控 ≥24h + ≥5 会话，一天至多几次），
    但改措辞仍要重量。
    """
    if sessions:
        sess_block = "\n".join(f"- sessions/{s}" for s in sessions)
    else:
        sess_block = "（没有）"
    if log_names:
        log_block = "\n".join(f"- logs/{n}" for n in log_names)
    else:
        log_block = "（没有）"
    # 归档笔记（`/clear` 留下的那批）：另一路素材。
    # 与"会话笔记"的区别：那份是在跑的会话留下的，这份是已经清过的。
    if archives:
        arch_block = "\n".join(f"- sessions/{a}" for a in archives)
    else:
        arch_block = "（没有）"

    return (
        "# 记忆巩固\n\n"
        "把这段时间散在**日报**与**会话笔记**里的东西搬进长期记忆，"
        "再把长期记忆里已经重复、臃肿或互相矛盾的部分理顺。\n"
        "这不是在回复用户 —— 不要对用户说话，最后只回一段简报。\n\n"
        "【你手上的工具】只有三个，其余一律会被拒绝，别试：\n"
        "- `memory_search(query)` —— 检索记忆（名字/描述/正文一起扫）。"
        "空查询 `memory_search(\"\")` = 列出当前全部记忆的索引（这就是你的 ls）\n"
        "- `memory_read(name)` —— 读一条的全文\n"
        "- `memory_write(name, description, content)` —— 新建/更新一条（同名 = 更新那条）\n\n"
        "【`memory_read` 还能读三类只读素材】（它们不是记忆条目，只能读）：\n"
        "- `memory_read(\"logs/YYYY-MM-DD\")` —— 某天的日报（一轮一段的流水）\n"
        "- `memory_read(\"sessions/<会话号>\")` —— 某个会话的笔记（正在跑的那个）\n"
        "- `memory_read(\"sessions/<会话号>.<日期>-<时间>\")` —— 已归档的会话笔记"
        "（用户按过 `/clear` 的那几段；同一段对话的提炼版，比日报信息密度高）\n\n"
        "## 一、搬运\n"
        "短期里绝大多数东西会自己消失，只有还会被用到的才值得搬。\n"
        "判据：它说的是**稳定的事实**（偏好、约定、环境、结论），"
        "而不是「这一轮发生了什么」。\n"
        "- 写的时候把「昨天」「上周」换成具体日期 —— 不然过一阵就没人读得懂。\n"
        "- 优先并进已有的那一条；确实没有对应的，才新开一条。\n\n"
        "## 二、合并\n"
        "几条讲同一件事的，并成一条。\n"
        "判据：并完之后**信息没少**。少了一点那就是改写，不是合并，别硬并。\n"
        "- 顺带的修剪：某条的 `description` 里塞了只有正文才该有的细节，"
        "就把它挪进正文、把那一行缩短。\n"
        "- 索引是从条目现算的（不落盘），你改不了它 —— 要它变短，改条目本身。\n\n"
        "## 三、改写\n"
        "与你现在看到的事实冲突的，**在原处改掉**。\n"
        "判据：你手上的依据比它新，或者它自相矛盾。\n"
        "- 你没有删除工具：删除是用户的动作（用户删过的条目被挡着，你写不回去，"
        "这是有意的）。要让一条陈旧的事实消失，就把它改成现在对的那个版本。\n\n"
        "【素材清单】\n"
        f"日报（从新到旧，最多列 {MAX_LOG_NAMES} 条）：\n{log_block}\n\n"
        f"上次巩固之后碰过的会话（{len(sessions)} 个）：\n{sess_block}\n\n"
        f"被 `/clear` 归档的会话笔记（从新到旧，最多列 {MAX_ARCHIVE_NAMES} 条）：\n"
        f"{arch_block}\n\n"
        "---\n"
        "最后回一段简报：搬了什么、并了什么、改了什么。"
        "什么都没改就直说（记忆已经够紧凑）—— 不必为了有产出而改东西。"
    )


# 闸门（同步、便宜）+ 开线程

def _status(status: Optional[Callable[[str], None]], line: str) -> None:
    if status is not None:
        status(line)
    else:
        print(line)


def _spawn(*, target, kwargs) -> threading.Thread:
    """起后台线程 —— 单独一个函数是为了测试能同步替换它（与抽取同款：
    `_spawn` 猴子补丁）。"""
    t = threading.Thread(target=target, kwargs=kwargs, daemon=True)
    t.start()
    return t


def maybe_organize(*, session_id: str, messages: Optional[List[dict]], provider,
                   memory_root, logs_dir, sessions_dir, storage_id: str = "",
                   status: Optional[Callable[[str], None]] = None) -> None:
    """轮末入口（`MemoryManager.record_turn` 调）。同步部分只做闸门。

    路径三件（`memory_root` / `logs_dir` / `sessions_dir`）由调用方给
    （manager 按 `storage_path` 推导 —— 同 `daily_log` 那条：路径由调用方传进来，
    带 `storage_path` 的测试自动落自己的临时目录）。

    `session_id` 是通道键，
    而 `storage_id` 是存储键 —— 两个都要：
      · `session_id`（通道键）：`_state` 闸门 · 快照比对 · fork 的
        `agent_loop(session_id=)` · 档位 · 事件 —— 全是通道作用域；
      · `storage_id`（存储键）：`sessions_to_review()` 比的是笔记文件名，
        而笔记按存储 id 命名（`SessionMemory.note_stem`）。
    不给 `storage_id` 时落回 `session_id`（兼容手搭的老测试）——
    生产路径是 `MemoryManager.record_turn`，它两个都给。
    """
    if not session_id or provider is None:
        return

    # ① 开关（写记忆总闸 + 巩固自己那把）
    if not is_organize_enabled():
        return

    # ② 在飞，本轮直接返回
    if _state.get("in_progress"):
        return

    # ③ 时间闸（上次成功巩固的时刻；没记录就是 0，必然通过）
    last_at = last_round_at(memory_root)
    hours_since = (time.time() - last_at) / 3600.0
    if hours_since < MIN_HOURS:
        return

    # ④ 扫描节流 —— 必须在会话闸之前：时间闸过了而会话闸没过时，
    #    "上次巩固时刻"不前进，时间闸每轮都过；不节流就是每轮全扫一遍会话文件。
    now = time.time()
    since_scan = now - float(_state.get("last_scan_at") or 0.0)
    if since_scan < SCAN_INTERVAL_MS / 1000.0:
        return
    _state["last_scan_at"] = now

    # ⑤ 会话闸（排除当前会话 —— 它的 mtime 必然很新）
    # 排除的是当前这条线程的笔记（按存储 id 命名），用 `storage_id`。
    session_ids = sessions_to_review(sessions_dir, last_at,
                                     storage_id or session_id)
    if len(session_ids) < MIN_SESSIONS:
        return

    # ⑥ 取互斥
    if not hold_round(memory_root):
        return

    # ⑦ 开跑（deepcopy 在这里 —— 罕见路径，代价可忽略；fork 必须冻在轮末这一刻）
    snap = request_snapshot.get_current()
    if snap is None or snap.session_id != session_id:
        # 拿不到快照，fork 不出"同前缀"，把互斥还回去
        release_round(memory_root)
        _status(status, "[memory] 巩固：没有本会话的请求快照 —— 跳过本轮")
        return

    _status(status, "[memory] 记忆巩固开始 —— %.1f 小时未巩固，%d 个会话待看"
            % (hours_since, len(session_ids)))
    _state["in_progress"] = True
    _spawn(target=run_organize, kwargs=dict(
        session_id=session_id,
        storage_id=storage_id,
        messages=copy.deepcopy(messages) if messages else [],
        provider=provider,
        tools=list(snap.tools),
        system=snap.system,
        memory_root=memory_root,
        logs_dir=logs_dir,
        sessions_dir=sessions_dir,
        last_at=last_at,
        status=status,
    ))


# 后台本体（测试直接调它 —— 不走线程）

def run_organize(*, session_id: str, messages: List[dict], provider,
                 tools, system, memory_root, logs_dir, sessions_dir,
                 last_at: float, storage_id: str = "",
                 status: Optional[Callable[[str], None]] = None) -> None:
    """线程体：占并发名额 → 拼 directive → fork 一把 → 收集改动 → 发事件。

    永不抛。失败只还互斥、不写"上次巩固时刻" —— 下一轮的时间闸
    自然重新攒够 24 小时。成功才盖那一格。
    `session_id` = 通道键，`storage_id` = 存储键（见 `maybe_organize`
    的 docstring：只有"排除当前那条笔记"那一处要存储键）。同样地，不给就落回。
    """
    from core.pipeline import scheduler as _sched

    # 巩固是第 4 个会跑的东西，占并发上限里的一个名额（当前上限是 5）
    #   （与用户轮次同一个信号量；跑完在 finally 里释放）。
    _sched._acquire_turn_slot("memory_organize")
    ok = False
    out_msgs: List[dict] = []
    sessions: List[str] = []
    # 后台活的时间跨度 —— `bg_end` 落在既有的 `finally` 上（失败也收口）。
    from core.events import bg_start, bg_end
    _bg_t0 = bg_start("organize", session_id)
    try:
        sessions = sessions_to_review(sessions_dir, last_at,
                                      storage_id or session_id)
        log_names = list_daily_logs(logs_dir)
        # `/clear` 归档下来的那批：另一路素材，不参与会话闸（见
        # `list_note_archives` 的 docstring）。
        archives = list_note_archives(sessions_dir, last_at)
        directive = build_organize_prompt(
            sessions=sessions, log_names=log_names, archives=archives)

        from core.agent_loop import agent_loop
        from core.memory_store import organize_read_mode
        with organize_read_mode(logs_dir, sessions_dir):
            _content, out_msgs, _chunks, _tokens = agent_loop(
                user_message=directive,
                provider=provider,
                messages=messages,          # ← 父对话的副本（保前缀）
                session_chunks=[],
                memory_context="",          # 有 `_system` 时它不参与
                max_turns=ORGANIZE_MAX_TURNS,
                session_id=session_id,
                _tools=tools,
                _system=system,
                _tool_allowlist=ORGANIZE_ALLOWLIST,
                thinking=tiers.current_spec(session_id).thinking,
                _emit=None,                 # 后台活不进父的时间线
            )
        ok = True
    except Exception as exc:  # noqa: BLE001 —— 判据：best-effort，绝不把失败报成成功
        _status(status, "[memory] 记忆巩固失败（互斥已还，下轮重算时间闸）："
                        "%s: %s" % (type(exc).__name__, exc))
    finally:
        _state["in_progress"] = False
        _sched._turn_slots.release()
        bg_end("organize", session_id, _bg_t0)

    # 成败都要还互斥；差别只在"上次巩固时刻"盖不盖。
    release_round(memory_root)
    if not ok:
        return
    mark_round_done(memory_root)

    # 收集判据只有一处：用抽取那件现成的读者 —— 两处各写一份迟早漂移。
    from core.memory_extract import _collect_written
    changed = _collect_written(out_msgs)
    names = changed[:8]
    # 收尾两条路都得出声。
    #
    # 只在 `changed` 非空时才发 `memory.organized` 是不够的：`_status(...)`
    # 那行走的是控制台，WebUI 看不到，巩固跑完了、锁也取到了、
    # 改了 0 条，用户侧却一片空白。
    #「跑了没改」与「压根没跑」在 UI 上完全同形。
    #
    # 判据："有变化"是结果，"跑过了"是事实 —— 两件事都得说。
    #事件一律发，`count=0` 照发（前端那条回执块本来就渲染成
    #   「记忆巩固：改动 0 条（看了 N 个会话）」，那句话本身就是"它跑过"的证据）。
    try:
        from core.events import EVT_MEMORY_ORGANIZED, get_event_bus
        get_event_bus().emit(EVT_MEMORY_ORGANIZED, session_key=session_id,
                             count=len(changed), names=names,
                             sessions=len(sessions))
    except Exception as exc:  # noqa: BLE001 —— 事件发不出去不许影响巩固本身
        print("[memory] memory.organized 事件发送失败：%s: %s"
              % (type(exc).__name__, exc))
    if changed:
        _status(status, "[memory] 记忆巩固完成 —— 改动 %d 条：%s"
                % (len(changed), "、".join(names)))
    else:
        _status(status, "[memory] 记忆巩固完成 —— 无改动（记忆已紧凑）")
