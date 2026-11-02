"""一段对话的工作目录 —— 语境，不是边界。

先读这一行再往下：这一格管三件事，不管第四件。

| 它管 | 它不管 |
|---|---|
| ① 落点 —— `primary_output_dir()` 指向它 | ✗ 它不给这个目录开权限 |
| ② 语境 —— 系统提示词里那句「项目目录 = `<目录>`」 | |
| ③ 左栏分组 —— `/api/threads` 多一个字段 | |

"能不能碰别的目录"是审批轴的事（手动模式 = 人 · 自动模式 = 审批器），
见 `core/security.py` 那两道门。这一格写进一个路径，不等于放行那个路径
—— 唯一的例外是 `_is_session_workspace` 那条显式旁路（"这一段自己选的目录
写免问"），而它是审批那一格的实现，不是这一格的推论。

## 键是存储 id，不是通道键

与 `core/session_model.py` 逐条同形，理由也一样：

    `/clear` 铸一条新存储 id，新线程那一格不存在，自动 = 用默认工作区
    `/resume` 回一条旧线程，它当年选的那个目录还在（原样接着用）

不需要在 `/clear` 的 purge 清单里加任何一行（它不是内存态，
它就是那条线程自己的一行数据 —— 与 `stars` / `session_models` 同族）。

## 「这一轮的落点」为什么走 `ContextVar`

`primary_output_dir()` 的调用点在 `skills/**`（`file_parser` / `office_parser`），
而工具 handler 的签名是 `handler(**tool_args)` —— **拿不到 `ctx`、也拿不到
`session_id`**，只能走环境。这一条不是新发明：`core/request_snapshot.py`
为同一个理由做了同一件事，本文照它抄。

**`run_in_executor` / 裸 `ThreadPoolExecutor` 不传播 contextvars**
（`run_in_executor(None, fn)` 里读出来是 `<default>`）。
本仓唯一在别的线程上跑 tool handler 的地方是只读并发批，
**它已经在提交时包了 `contextvars.copy_context().run`**（`agent_loop.py`），
这里不需要再做一遍。新开线程跑 handler 的地方要照那一处做。

`contextvars` 按定义不在会话级状态登记表的扫描面上（每任务一份、自洁、
不跨上下文）—— 所以本文不登记，那不是漏了。

## 谁把它放进去：阶段层显式钉一次

`pin()` 由 `AgentLoopStage.process()` 的第一行调用 —— 一轮一次，
值就是那一段选定的目录（查一次库，见下）。

为什么不在这里做"懒解析"（即 `current()` 自己从 `request_snapshot`
问会话号、再查库）：`request_snapshot` 是每次发请求前才设的
（`agent_loop` 里紧挨着 `provider.chat`），拿它当唯一来源，
"这一轮还没发过第一条请求"那一段没有答案。显式钉没有这个缝。

钉的时机不能提前到调度器入口：`ctx.storage_id` 是
`PreProcessStage` 里才赋值的（它自己有 7 处赋值，而**新对话 / `/clear`
正是在那里才铸新 id**），提前钉只会钉到默认工作区，症状是
「新对话选的那个目录，第一条消息不生效」—— 而用户看到的正好是第一条。

只 set、不 reset：一轮 = 一个任务 = 一个上下文（web 是每请求一个
`asyncio.run`；IM 是 `run_coroutine_threadsafe`、各自成 Task），上一轮的值随
任务一起消亡；而每轮都显式钉一次，陈旧值进不来。
"""

from __future__ import annotations

import contextlib
import contextvars
from pathlib import Path
from typing import Optional

#: 惰性取 `short_term` 单例 —— 同 `core/session_model.py` 的理由：
#: 顶层 import 会把整条记忆链拉进每一条 import 路径，而这里的调用是低频的。
_ST = None


def _short_term():
    """拿到 `ShortTermMemory` 单例（拿不到，`None`，调用方各自兜底）。

    走 `bus.registry` 的那一个 —— 与 `MemoryManager` 用的是同一个连接
      （一个口径一个地方；`session_model.py` 逐字同款）。
    """
    global _ST
    if _ST is not None:
        return _ST
    try:
        from bus.registry import get_memory_manager
        mgr = get_memory_manager()
        if mgr is not None:
            _ST = mgr.short_term
    except Exception:                                           # noqa: BLE001
        _ST = None
    return _ST


def _norm(key: str) -> str:
    """把两种入参形状归一到存储 id（一条判据，一处实现）。

    本仓有两个键：通道键（认通道的）与存储键（存史的）。
      调用方两头都有 —— 调度器/`ctx` 手上是通道键，`MemoryManager` 那几条路
      手上是存储 id，归一：先当通道键查一次绑定表，查不到就**当它本来就是
      存储 id**。

    与 `core/session_model.py::_norm` 逐字同形（两份是同一件事，只是键的
      归属不同）。刻意不抽成一个共用函数：抽了就要有一处"谁的 `_ST`"
      的额外约定，而这两份各自只有 12 行、各自被自己的测试钉着。
      若将来要合并，判据是"两份是否还逐字一样" —— 不一样就别合。
    """
    st = _short_term()
    #: `.strip()` 在这里是判据的一部分：空白的键会真的写进一行
    #:    （`"   "` 是个合法主键），写了也永远读不回来。
    k = str(key or "").strip()
    if st is None or not k:
        return k
    try:
        return str(st.bound_storage_id(k) or k)
    except Exception:                                           # noqa: BLE001
        return k


# ----------------------------------------------------------------------
# 存储面（那一格记录本身）
# ----------------------------------------------------------------------

def chosen(storage_key: str) -> str:
    """这一段自己选的那个目录（`""` = 没选过，用默认工作区）。

    `""` 与"选了个空的"必须是同一件事（`set_for` 拒收空路径）
一个值只说一件事，调用方不必再分情况。
    """
    st = _short_term()
    if st is None:
        return ""
    try:
        return st.session_workspace(_norm(storage_key))
    except Exception as exc:                                    # noqa: BLE001
        # 读不到，当没选过（fail-open，同 `session_model.frozen` 那条）。
        #   方向是掐出来的：读不到就走默认工作区 —— 而"读不到"最可能的时刻
        #   正是库刚建、或换了一台机器；那时"用默认"就是对的答案。
        print(f"[session_workspace] 读会话工作目录失败（按未选择处理）："
              f"{type(exc).__name__}: {exc}")
        return ""


def resolve(storage_key: str) -> str:
    """这一段现在该用哪个目录 —— `""` = 用默认工作区。

    唯一判定入口。唯一的一项是：这段自己选的那个。
    这里刻意不回退到 `default_output_dir()` —— 回 `""` 让调用方自己去取
      默认，好处是调用方知道"这是默认"（而不是"这是某段选中的"）。
      `core/workspace.py::primary_output_dir` 那一处负责接上回退。
    """
    return chosen(storage_key)


def set_for(storage_key: str, path: str) -> str:
    """用户显式给这一段指定工作目录。回写入之后这一段实际用的路径。

    `path=""`，切回「用默认工作区」（= :func:`use_default`），
    两个名字一个动作 —— 用户那两条操作（"选一个" / "用默认"）在这里是
    同一格的两种取值。

    写进去的是 `resolve()` 之后的绝对路径 —— 判据在
      :func:`normalize_dir`：存相对路径的话，`classify_path` 的
      `relative_to` 判定会随进程 cwd 漂（而 cwd 会被 `run_command` 的 `cd` 改）。
    """
    st = _short_term()
    key = _norm(storage_key)
    p = str(path or "").strip()
    if st is None or not key:
        return ""
    if not p:
        use_default(storage_key)
        return ""
    ok, _why = check_dir(p)
    if not ok:
        return ""                      # 拒收 —— 端点那侧会先把 `why` 给用户看
    norm = normalize_dir(p)
    st.set_session_workspace(key, norm)
    return norm


def use_default(storage_key: str) -> bool:
    """把这一段退回「用默认工作区」（删掉那一行）。回有没有真的删掉一条。"""
    st = _short_term()
    key = _norm(storage_key)
    if st is None or not key:
        return False
    try:
        existed = bool(st.session_workspace(key))
        st.clear_session_workspace(key)
        return existed
    except Exception as exc:                                    # noqa: BLE001
        print(f"[session_workspace] 退回默认工作区失败（{type(exc).__name__}: {exc}）")
        return False


def normalize_dir(path: str) -> str:
    """把一个目录串规整成绝对路径（存的就是它）。

    不 `strict` —— 目录可能是刚被用户删掉、或者在一台还没挂载的盘上；
      那时 `resolve()` 仍然给一个可比较的绝对路径（`Path.resolve` 在
      `strict=False` 下不要求存在）。存在性由 `check_dir` 判，两件事分开。
    """
    try:
        return str(Path(str(path or "").strip()).expanduser().resolve())
    except Exception:                                           # noqa: BLE001
        return str(path or "").strip()


def check_dir(path: str) -> tuple[bool, str]:
    """这个路径能不能当一个工作目录。回 `(ok, 为什么不行)`。

    判据只有一处：端点、界面提示、将来的设置页都问这里。
    三条都是"说得出理由"的，不是"看着不对就拒"：
      ① 空 · ② 不存在 · ③ 存在但不是目录（是个文件 —— 用户选错了那一下，
         而后果是"每一处写都落在文件下面"，报错要到很远的地方才看得见）。
    """
    p = str(path or "").strip()
    if not p:
        return False, "没有给目录。"
    try:
        q = Path(p).expanduser()
    except Exception as exc:                                    # noqa: BLE001
        return False, "这个路径读不出来：%s: %s" % (type(exc).__name__, exc)
    try:
        if not q.exists():
            return False, "这个目录不存在：%s" % q
        if not q.is_dir():
            return False, "这是一个文件，不是目录：%s" % q
    except OSError as exc:
        return False, "这个目录打不开：%s: %s" % (type(exc).__name__, exc)
    return True, ""


# ----------------------------------------------------------------------
# 运行面：「这一轮的落点」—— 走 `ContextVar`（理由见模块 docstring）
# ----------------------------------------------------------------------

#: `None` = 还没算过 · `""` = 算过了、这一段没有自己的目录。两态不许塌。
_bound: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "crannia_session_workspace", default=None)


def _turn_session_key() -> str:
    """这一轮跑在哪个会话里 —— 从 `request_snapshot` 取（同族的唯一来源）。

    今天只有测试与诊断用它 —— 生产那条路是 `pin()`（阶段层显式钉）。
      留着的理由是"想知道这是谁在问"时不必再去拼一遍
      （同一个量两处各算各的会漂）。

    为什么不让它当主路：`request_snapshot` 是每次发请求前才设的
      （`agent_loop` 里紧挨 `provider.chat`），拿它当唯一来源，
      "这一轮还没发过第一条请求"那一段就没有答案。显式 `pin()` 没有这个缝。
    """
    try:
        from core.request_snapshot import current_session_id
        return current_session_id()
    except Exception:                                           # noqa: BLE001
        return ""


def pin(storage_key: str) -> str:
    """轮首钉住：把这一段的目录放进本任务的上下文。回钉进去的那个值。

    **落点是阶段层（`AgentLoopStage.process` 第一行），不是调度器入口** ——
      `ctx.storage_id` 是 `PreProcessStage` 里才赋值的（它自己有 7 处赋值，
      且新对话 / `/clear` 正是在那里才铸新 id），在调度器入口钉的那一刻
      还没有存储 id，只会钉到默认工作区，而症状是
      「新对话选的那个目录第一条消息不生效」（第二条起才好 —— 用户看到的
      正好是第一条）。

    只 set、不 reset：一轮 = 一个任务 = 一个上下文（web 是每请求一个
      `asyncio.run`；IM 是 `run_coroutine_threadsafe` 各自成 Task），
      上一轮的值随任务一起消亡；而每轮都显式钉一次，陈旧值进不来。
      （同 `request_snapshot.set_current` 的取舍；那一条也是只 set。）
    钉住之后子 agent 天然跟父同目录（`run_child` 在同一个上下文里跑），
      而只读并发批靠 `copy_context()` 带过去。
    """
    v = resolve(storage_key)
    _bound.set(v)
    return v


def current() -> str:
    """这一轮该往哪儿落 —— 这一段选定的目录；`""` = 没有，用默认工作区。

    读不到当"没有"，不抛 —— 同 `chosen()` 那条：取不到不该让工具不可用
      （后台线程 / 测试夹具 / 阶段还没跑到），回默认工作区，那是今天的行为。
    那一格备忘（`None` vs `""`）今天不再需要 —— 没有懒解析就没有
      "查过一次没有"这个中间态。两态合成一态是简化，不是把哨兵值丢了。
    """
    v = _bound.get()
    return v or ""


def current_path() -> Optional[Path]:
    """`current()` 的 `Path` 形状；没有就是 `None`（不是 `Path("")`）。"""
    d = current()
    return Path(d) if d else None


@contextlib.contextmanager
def bind(storage_key: str):
    """`pin()` 的有作用域版本（测试与需要还原的调用方用）。

    用 `Token` 而不是"存一个变量再 set 回去"：`reset(token)` 精确还原，
      中间嵌套多少层、`set` 了多少次都不影响（`request_snapshot.preserved`
      逐字同款）。
    """
    token = _bound.set(resolve(storage_key))
    try:
        yield
    finally:
        _bound.reset(token)


@contextlib.contextmanager
def use_path(path: str):
    """把这一段的落点临时钉成 *path*（有作用域）。

    只动运行期的这一格，不写盘，这一段对话自己的选择不受影响。
    用在子 agent 的隔离工作树上：那个目录不是用户选的，但它就是这一趟的落点
`classify_path` 的 2.4 与 `primary_output_dir()` 同时认它。
    """
    token = _bound.set(str(path) if path else "")
    try:
        yield
    finally:
        _bound.reset(token)


def forget_memo() -> None:
    """丢掉本任务的钉值（测试用）。不碰磁盘。"""
    _bound.set(None)


def describe(storage_key: str) -> dict:
    """给端点 / 界面的一份读数 —— 「这一段现在落在哪，为什么」。

    三个字段回答三个不同的问题（别合并，同 `session_model.describe`）：
      · `path`   —— 这一段选定的那个（`""` = 没选，用默认）
      · `is_set` —— 用户选过没有（`False` = 改默认会波及它）
      · `source` —— 那句话是怎么来的（`"chosen"` / `"default"`）
        少了它，界面只能说"落在 X"，而说不出 X 是用户选的还是默认的
        （「标签在说谎」的形状：用户点了「用默认」之后，界面如果还写着
         某个目录名，他就不知道那个按钮到底生效没有）。
    """
    own = chosen(storage_key)
    if own:
        return {"path": own, "is_set": True, "source": "chosen"}
    return {"path": "", "is_set": False, "source": "default"}


def reset_for_tests() -> None:
    """清掉那个单例引用与备忘。不碰磁盘。

    它存在的理由是测试之间会换 sandbox（`tests/_sandbox.py` 靠 chdir）
      —— 不清的话第二个用例还指着上一个临时目录里那个库，而它读得到、
      也有表、只是里面的行是上一个用例的（"绿了但测的是别的库"）。
      （`session_model.reset_for_tests` 逐字同款。）
    """
    global _ST
    _ST = None
    forget_memo()
