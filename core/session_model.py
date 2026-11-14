"""一段对话用哪个模型配置 —— 三层指向 + 惰性冻结。

三条语义：

  1. 子 agent 默认跟随默认配置，除非用户指定；用户可随时切回「跟随默认」
  2. 工作区与对话同理（默认用已配置的那个）
  3. 工作区/任务对话里一旦已有任意记录，再改默认不再对他们生效
     —— 只影响后续新建（新对话 / 新开的独立工作区集合）

本模块管的是第 2、3 条里「对话」那一半（子 agent 那半在
`core/subagent_model.py`；工作区那半见下面的「 两层没做」）。

## 惰性冻结是什么

改默认那条配置之后，已经在聊的对话不该当场换模型。
判据：只要模型有任意回复就算 ——
于是冻结的时刻落在 `PostProcessStage`（那一轮真的存下了一条 assistant 消息），
而不是"用户发了一句话"（那时还没有任何回复，用户心里这段对话还没开始）。

冻结 = 把生效那一刻的配置 id 抄下来，之后只读抄下来那个。

## 键是存储 id，不是通道键

这一条把「冻结」和「`/clear`」串成了一件自然的事：

    `/clear` 铸一条新存储 id，新线程那一格不存在，自动=跟随默认
    `/resume` 回一条旧线程，它当年冻的那个还在（原样接着用）

不需要在 `/clear` 的 purge 清单里加任何一行（这是一条显性的设计取舍：
它不是内存态，它就是那条线程自己的一行数据 —— 与 `stars` 同族）。

## 两层没做，以及为什么（先读这一行再找它们）

| 层 | 状态 | 为什么 |
|---|---|---|
| 会话 | ✅ 见下 | 有消费者：每轮都问一次 |
| 子 agent | ✅ `core/subagent_model.py` | 有消费者：派子 agent 时问 |
| 工作区 / 集合 | ⛔ 不做 | 现在没有任何消费者 —— 「工作区/任务对话」那一层真正的落点是集合，而集合尚未落地；在那之前，「点左栏那个工作区」只是高亮（`App.jsx` 的 `space` 是纯本地状态，一个字节都不发给后端），现在做它 = 做一个没有任何调用点的字段。这不是漏做，是有意推迟到它有底座的那一批。 |

`resolve()` 是本模块唯一的判定入口 —— 端点 / 调度器 / 记忆层
都不许自己拿 `frozen()` 和 `default_id()` 拼一遍（那就会各自漂）。
"""

from __future__ import annotations

#: 惰性取 `short_term` 单例 —— `core/model_configs` 那条同款理由：
#: 顶层 import 会把整条记忆链拉进每一条 import 路径，而这里的调用是低频的。
_ST = None


def _short_term():
    """拿到 `ShortTermMemory` 单例（拿不到，`None`，调用方各自兜底）。

    走 `bus.registry` 的那一个 —— 与 `MemoryManager` 用的是同一个连接
      （`ShortTermMemory` 是 `check_same_thread=False` + WAL，多连接也不会坏，
      但"同一个真相源"比"另一条能跑的路"重要：两条连接会各有一份事务视图）。
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

    本仓库有两个键：通道键（认通道的）与存储键（存史的）。
      本模块的调用方两头都有 ——
        · 调度器/`ctx` 手上是通道键（`session_key_of_ctx`）；
        · `MemoryManager` 那几条路手上是存储 id。
归一：先当通道键查一次绑定表，查不到就当它本来就是存储 id。
      两种情形各自都对，而且不需要调用方声明自己是哪一种：
        · 真通道键且绑过，绑定表给的那条（正是"这一段对话"）；
        · 真通道键没绑过，查不到，用它自己（那条线程还没建，反正也不会冻过）；
        · 本来就是存储 id，绑定表的主键是 `channel_key`，不会有一行
          拿存储 id 当通道键，查不到，用它自己。
    """
    st = _short_term()
    #: `.strip()` 在这里是判据的一部分（不是好看）：空白的键会真的
    #:   写进一行（`"   "` 是个合法主键），`freeze("   ", cid)` 会成功，
    #:   而它永远读不回来（没人会拿 `"   "` 去问）。
    k = str(key or "").strip()
    if st is None or not k:
        return k
    try:
        return str(st.bound_storage_id(k) or k)
    except Exception:                                           # noqa: BLE001
        return k


def frozen(storage_key: str) -> str:
    """这一段自己冻的那条（`""` = 没冻过）。

    `""` 与"冻了个空的"必须是同一件事（`set_session_model` 拒收空 id）
一个值只说一件事，调用方不必再分情况。
    """
    st = _short_term()
    if st is None:
        return ""
    try:
        return st.session_model(_norm(storage_key))
    except Exception as exc:                                    # noqa: BLE001
        # 读不到，当没冻（fail-open，同 `_stream_enabled` 那条）。
        #   方向是掐出来的：读不到就走默认 —— 而"读不到"最可能的时刻正是
        #   库刚建、或换了一台机器；那时"跟随默认"就是对的答案。
        print(f"[session_model] 读冻结失败（按未冻结处理）："
              f"{type(exc).__name__}: {exc}")
        return ""


def resolve(storage_key: str) -> str:
    """这一段现在该用哪条配置 —— `""` = 跟随默认。

    唯一判定入口。现在只有一项：这段自己冻的。
      将来的「集合」会在这里加第二项（集合冻的那条排在这段自己之后）。
    """
    own = frozen(storage_key)
    if own:
        return own
    #: 这里刻意不返回 `model_configs.default_id()` —— 回 `""`
    #:   让调用方自己去取默认，好处是调用方知道"这是默认"（而不是
    #:   "这是某条被选中的"）。`describe()` 那一路才需要把它填上。
    return ""


def freeze(storage_key: str, config_id: str) -> bool:
    """惰性冻结：没冻过才写。回这次有没有真的冻上。

    判据是「已经冻过的不动」—— 这就是第 3 条语义的全部：
      改了默认之后，已经冻过的那几段照旧。

    先读后写（不是 `INSERT OR REPLACE`）：读一次是为了**让"已经冻过"
      这件事说了算。并发上不构成问题 —— 同一段对话的轮次是串行**的
      （每会话一把锁，`scheduler.execute` 的 docstring），
      而这个函数只在轮末被那一条串行的路调用。
    """
    if not config_id:
        return False
    key = _norm(storage_key)
    if not key:
        return False
    st = _short_term()
    if st is None:
        return False
    try:
        if st.session_model(key):
            return False                    # 已经冻过 —— 不覆盖（第 3 条语义）
        st.set_session_model(key, config_id)
        return True
    except Exception as exc:                                    # noqa: BLE001
        print(f"[session_model] 冻结失败（这一段会跟随默认）："
              f"{type(exc).__name__}: {exc}")
        return False


def set_for(storage_key: str, config_id: str) -> str:
    """用户显式给这一段指定一个配置（覆盖已冻的那个）。

    回写入之后这一段实际用的 id；`config_id=""`，切回「跟随默认」
    （= :func:`follow_default`，两个名字一个动作 —— 用户那两条操作
    「选一条」/「切回跟随默认」在这里是同一格的两种取值）。

    与 :func:`freeze` 的差别只有一条：这个覆盖，那个不覆盖。
      两个动词必须分开 —— 合成一个"写入"会让惰性冻结在第二次轮末
      把用户刚选的那条冲掉（而症状是"我选了 A，聊了一句又变回去了"）。
    """
    st = _short_term()
    key = _norm(storage_key)
    cid = str(config_id or "").strip()
    if st is None or not key:
        return ""
    if not cid:
        follow_default(storage_key)
        return ""
    st.set_session_model(key, cid)
    return cid


def follow_default(storage_key: str) -> bool:
    """把这一段退回「跟随默认」（删掉那一行）。回有没有真的删掉一条。"""
    st = _short_term()
    key = _norm(storage_key)
    if st is None or not key:
        return False
    try:
        existed = bool(st.session_model(key))
        st.clear_session_model(key)
        return existed
    except Exception as exc:                                    # noqa: BLE001
        print(f"[session_model] 切回跟随默认失败（{type(exc).__name__}: {exc}）")
        return False


def describe(storage_key: str) -> dict:
    """给端点 / 界面的一份读数 —— 「这一段现在用的是哪条，为什么」。

    三个字段回答三个不同的问题（别合并）：
      · `config_id` —— 实际会用的那条（`""` = 没有默认条目，退回 config.yaml）
      · `frozen`    —— 这一段自己冻过没有（`False` = 改默认会波及它）
      · `source`    —— 那句话是怎么来的（`"frozen"` / `"default"` / `"none"`）
        少了它，界面只能说"正在用 X"，而说不出 X 是用户选的还是默认的
        （「标签在说谎」的形状：用户点了「跟随默认」之后，
         界面如果还写着某条的名字，他就不知道那个按钮到底生效没有）。
    """
    from core import model_configs as _mc

    own = frozen(storage_key)
    if own:
        return {"config_id": own, "frozen": True, "source": "frozen"}
    dft = _mc.default_id()
    return {"config_id": dft, "frozen": False,
            "source": "default" if dft else "none"}


def reset_for_tests() -> None:
    """清掉那个单例引用。不碰磁盘（同 `model_configs.reset_for_tests`）。

    它存在的理由是测试之间会换 sandbox（`tests/_sandbox.py` 靠 chdir）——
      不清的话第二个用例还指着上一个临时目录里那个库，而它读得到、
      也有表、只是里面的行是上一个用例的（"绿了但测的是别的库"）。
    """
    global _ST
    _ST = None
