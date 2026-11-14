"""工具权限档的声明与判定。

## 它治什么 —— 一个「猜」变成了承重结构

`bus/registry.py::_classify_permission` 按工具名前缀猜三档
（`get_` 是 read · `set_` 是 write · 认不出来一律 `admin`）。它原本只是
拼进 description 给模型看的一个字符串，但有两处真闸拿它当真判据：

* 并发预取 —— `== "read"` 才并发，否则整批退回串行
* 只读子 agent —— `!= "read"` 就拒

一个从未被打算当承重结构的启发式，变成了安全判据，而没人回头重审过。

动工前（`boot_registry()` 后当场数）：132 个工具里
103 个「没人给它定过档」（前缀猜 68 + 兜底 `admin` 35）——
`_EXPLICIT_PERMS` 只有 29 条。而 `admin` 这个档名在撒谎：
它不表示「危险」，它表示「分类器没认出来」——`set_table_column_width`（纯格式）
与 `delete_files`（不可逆）同一档，唯一的共同点是名字不落前缀。

一个必须知道的事实：`admin` 与 `write` 今天行为完全等价。
全仓只有三处读档位，判的都是「是不是 `read`」；两者唯一的差别是
`bus/registry.py::_PERM_LABEL` 拼进 description 的那个字符串。
`admin -> write` 的迁移对闸零影响。

## 修法：档位由 manifest 声明，判定次序写死

```yaml
tools:
  - name: get_todo_items
    permission: read      # read | write | admin
```

判定次序（不许重排）：

```
manifest 声明  ->  _EXPLICIT_PERMS  ->  前缀猜（出声）  ->  admin（出声）
```

## 三条不许回退的规矩

### ① 没声明，兜底，而且两个兜底都要出声

判据：「读不到，默认；读到了但做不到，拒」。
这里的默认是 fail-closed 那一侧（名字不落 read 家族的，`admin`），
但兜底 ≠ 没人管：`source` 分开记，未决的必须可数（棘轮只许降，
见 `is_undecided`）。
这一条不是"给每个工具安一个档位就完事"——那样只是把旧的猜测抄进新文件，
而抄一遍不算做过决定（`scripts/propose_permissions.py` 的整个理由）。

### ② 声明是给老实作者的，不是安全边界

恶意 skill 可以声明 `read` 而实际删文件。但这不是新问题：skill 是可信写的
（人要按 `/skill approve`），而 `draft` / approve 挡的是「没人按过就生效」，
不是「作者是恶意的」。真恶意的 skill `import` 之后本来就是进程内的 Python。
本模块解决的是「让那两处真闸的输入变准」，不是「让 skill 变安全」。
安全性来自三层合起来：声明（准确）· 兜底出声 + 棘轮（可数）· AST 反查（可证伪 ——
「read 档的工具实现里不许有写标记」）。

### ③ 判定次序与来源必须有机械读者

「声明优先」这件事开了一个新面：manifest 从此能改任何一个工具的档位
——包括框架自己的。撞名必须拒（`register_tool` 的 `owner` 那一格），
否则 skill 可以声明一个 `run_command` 的同名工具去顶掉框架那份。

## 第二根轴：`destructive`

与档位正交 —— `delete_files`（write + 不可逆）· `add_block`（write + 可逆）·
`run_command`（admin + 不可逆）。判据是「不可逆」，不是"危不危险"、
也不是"改不改东西"（后者是 `permission` 那一根）。

```yaml
tools:
  - name: send_email
    destructive: true     # 发出去收不回来
```

判定次序同 `permission`（声明 -> 显式表 -> 兜底），但 兜底方向相反：
`resolve` 兜底是 `admin`（fail-closed），`resolve_destructive` 兜底是 `False`。
这是算过账的：运行期闸是「`destructive` 且不在
`_APPROVAL_CAPABLE_TOOLS`，问」，而那张能力表今天只有三个名字
（`run_command` / `delete_files` / `request_cross_zone_search`），
兜底取 `True` 会让闸为 129 个工具弹窗（每一个 `add_block` / `set_*` 都问一次），
那是不可用的。

未声明的不靠兜底承重，靠另外两层：可数（`SOURCE_DEFAULT`，棘轮只许降）+
可证伪（AST 反查：命中不可逆原语却没声明，红）。
代价写在明处：非法值 / 漏声明落的是 fail-open 那一侧
（与 `permission` 的非法值落 fail-closed 恰好相反）
所以两个字段的"拿不准"都要往多付一层代价那边写
（`permission` 写 `write`、`destructive` 写 `true`），而 `destructive` 的非法值
必须由三层挡住（装载出声 · 体检 `FAIL` · 工厂拒），不能只靠兜底。

我们这一根没有现成做法可抄 —— 常见实现把「不可逆」只用于
UI 上色 / `[destructive]` 标注 / SDK 导出 / MCP `destructiveHint` 映射，
权限系统一处都不读；真正进闸的是「是否只读」。

## 两条已知的不对称（写在这里免得将来有人以为我们不知道）

* `declare()` 从不清空（重启框架会在同一进程里
  重注册一遍，所以必须就地覆盖）。一个包里被删掉的工具，它的声明会残留；
  新包若恰好重名，会继承那条档位，看起来"声明过了"。今天无害
  （注册表才是权威），但别据此下"这个工具声明过"的结论。
* ~~今天没有第三根轴~~ —— 补上了：
  第三根轴 `concurrency_safe` 见下面那一节。它接手了当年那句
  「按 `isConcurrencySafe` 分类」里没做的那一半。
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional, Sequence, Tuple

#: 三个档位。只有三个 —— 判据是"它能不能改东西"，不是"它危不危险"
#: （"要不要问用户"是第二根轴 `destructive`，不是这一根）。
PERM_READ = "read"
PERM_WRITE = "write"
PERM_ADMIN = "admin"
VALID_PERMS: Tuple[str, ...] = (PERM_READ, PERM_WRITE, PERM_ADMIN)

#: 判定来源 —— 本模块与 `core/tool_output_kind.py` 的第一处不同。
#:
#: `output_kind` 只有一种兜底（没声明，`prose`）；权限有两种未决，
#: 而它们代价不同：`prefix` 猜错只可能把危险猜成安全（那一侧才是要紧的），
#: `default` 是 fail-closed（猜不出来一律 `admin`，方向是对的）。
#:出声的规格也不同（`prefix` 且 `read` 逐条 print，其余只进汇总行）。
SOURCE_DECLARED = "declared"     # manifest 声明 —— 唯一"有人做过决定"的来源
SOURCE_EXPLICIT = "explicit"     # 代码里的表（`_EXPLICIT_PERMS`）—— 有人做过决定，但不在包里
SOURCE_PREFIX = "prefix"         # 名字前缀猜出来的 —— 未决
SOURCE_DEFAULT = "default"       # 认不出来一律 admin —— 未决
VALID_SOURCES: Tuple[str, ...] = (SOURCE_DECLARED, SOURCE_EXPLICIT,
                                  SOURCE_PREFIX, SOURCE_DEFAULT)

#: 已声明的档位：`tool_name -> PERM_*`。由 loader 在注册时填
#: （`bus/loaders/skill_loader.py` 的两条注册路各一处）。
#:
#: 是运行时可变状态，不是 import 期常量 —— 这正是本模块存在的理由：
#: manifests 是装载时读的，而旧的判定是查名字（谁来都一样的那个猜）。
_declared: Dict[str, str] = {}


def declare(tool_name: str, permission: Optional[str]) -> bool:
    """登记一个工具的声明档位。返回是否被接受。

    非法值不登记（`None` / 拼错 / 大小写不对，返回 `False`）。
    这是 `_PERM_LABEL[perm]` 那记 `KeyError` 的第一道保险 ——
    那里的 KeyError 会让整个 skill 的全部工具凭空消失（`bus/registry.py:189`）。
    第二道保险在 `resolve()`：它只可能返回 `VALID_PERMS` 里的值。

    幂等：与 `register_tool` / `declare`(output_kind) 同规矩 ——
    "重启框架"会在同一进程里重新注册一遍，所以这里是就地覆盖，不是 append。
    """
    if permission in VALID_PERMS:
        _declared[tool_name] = permission  # type: ignore[assignment]
        return True
    return False


def declared_perm(tool_name: str) -> Optional[str]:
    """这个工具声明了什么。没声明（或声明值非法），`None`。

    与 `resolve()` 分开是刻意的：体检器 / 工厂要报"没声明"这件事，
    而它不能从 `resolve()` 读出来（那里已经把 `None` 兜成了某个档）。
判据同 `window_source` / `read_cache_hit`：
    「读不到」与「读到了是某值」必须分得开。
    """
    return _declared.get(tool_name)


def resolve(tool_name: str, *,
            explicit: Mapping[str, str],
            read_prefixes: Sequence[str],
            write_prefixes: Sequence[str]) -> Tuple[str, str]:
    """判定一个工具该算哪一档，返回 `(permission, source)`。

    顺序写死：声明 -> 显式表 -> 前缀猜 -> 兜底。
    任何一次重排都是一次真闸改动，而它不会有任何东西红。

    `core/` 保持叶子：显式表与前缀表由调用方注入（它们是
    `bus/registry.py` 的注册策略）。这里不 import `bus`——那会绕出调用期循环 import，
    而且测试也就能喂合成的表去验顺序。

    计划文件里这个函数叫 `classify`；改名 `resolve` 是为了与
    `bus/registry.py::_classify_permission`（留着的薄壳，签名不变）读起来不混。
    """
    declared = _declared.get(tool_name)
    if declared is not None:
        return declared, SOURCE_DECLARED

    if tool_name in explicit:
        return explicit[tool_name], SOURCE_EXPLICIT

    for prefix in read_prefixes:
        if tool_name.startswith(prefix):
            return PERM_READ, SOURCE_PREFIX
    for prefix in write_prefixes:
        if tool_name.startswith(prefix):
            return PERM_WRITE, SOURCE_PREFIX

    return PERM_ADMIN, SOURCE_DEFAULT


def is_undecided(source: str) -> bool:
    """这个来源是不是「没人给它定过档」。

    这是本模块唯一要拿来打棘轮的量：`undecided = prefix + default`
    （启动时 103 = 68 + 35，两个都只许降）。

    `declared` 与 `explicit` 都算"有人做过决定" —— `_EXPLICIT_PERMS`
    是一条条捞出来的，不是猜的。
    但判据是要"撤销 `explicit`"的：第 4 步把那张表剪到 16 条
    （只剩"没有 manifest 的工具"，即框架级 + MCP），其余全部搬进 manifest。
这条不变量今天允许 `explicit`，迁移完成后它的目标态是 `declared` 一统。
    """
    return source in (SOURCE_PREFIX, SOURCE_DEFAULT)


# 第二根轴：destructive
#
# 判据与档位正交，所以它是另一张表、另一组函数 ——
# 别把 `destructive` 塞进 `_declared`（那是 `name -> read|write|admin` 的单值表）；
# 也别拿 `permission != "read"` 当它的近似（`add_block` 是 write 而完全可逆）。

#: 已声明的「不可逆」：`tool_name -> bool`。由 loader 在注册时填
#: （`bus/loaders/skill_loader.py` 的两条注册路各一处）。
#:
#: 是运行时可变状态，不是 import 期常量 —— 同 `_declared`。
_declared_destructive: Dict[str, bool] = {}


def declare_destructive(tool_name: str, value) -> bool:
    """登记一个工具的 `destructive` 声明。返回是否被接受。

    只收真 `bool`（`isinstance(value, bool)`）—— YAML 的
    `destructive: 0` / `"false"` / `yes`（`yes` 会被 PyYAML 读成 `True`，
    但那是碰巧对，不是保证）一律不登记。

    与 `declare()` 的非法值待遇相反，这一点要紧：
    那边非法值落回 `admin`（fail-closed，安全侧）；
    这里非法值落回兜底的 `False`（fail-open）。
一个拼错的 `destructive: ture` 不会让工具变危险，它会让工具
    静默变回"可逆" —— 所以非法值必须由三层挡住
    （装载出声 · 体检 `FAIL` · 工厂拒），不能只靠兜底。
    返回 `False` 就是给那三层留的信号。

    幂等：与 `register_tool` / `declare` 同规矩 —— 就地覆盖，不是 append。
    """
    if isinstance(value, bool):
        _declared_destructive[tool_name] = value
        return True
    return False


def declared_destructive(tool_name: str) -> Optional[bool]:
    """这个工具声明了什么。没声明，`None`（与 `False` 分得开）。

    这一格比 `declared_perm` 那边更要紧：那边的"没声明"会落进一个非空的档
    （`admin`），一眼看得出不对；这里"没声明"与"声明了可逆"长得一模一样
    （都是 `False`）—— 而"有多少个没人管"正是棘轮要数的那个数
    （`is_undecided` + `SOURCE_DEFAULT`）。判据同 `window_source`。
    """
    return _declared_destructive.get(tool_name)


def resolve_destructive(tool_name: str, *,
                        explicit: Mapping[str, bool]) -> Tuple[bool, str]:
    """判定一个工具算不算不可逆，返回 `(destructive, source)`。

    顺序写死（同 `resolve`，不许重排）：声明 -> 显式表 -> 兜底。
    重排 = 一次静默的策略改动（manifest 从此压不过代码表），而不会有任何东西红。

    兜底是 `False`（fail-open） —— 与 `resolve` 的 `admin`（fail-closed）
    方向相反，理由在模块 docstring 的「第二根轴」一节里（那是一笔算得出来的账：
    兜底取 `True`，闸为 129 个工具弹窗）。承重的是棘轮与AST 反查这两层。

    `core/` 保持叶子：显式表由调用方注入（它是 `bus/registry.py` 的注册策略）。
    测试因此能喂合成的表验顺序。
    """
    declared = _declared_destructive.get(tool_name)
    if declared is not None:
        return declared, SOURCE_DECLARED

    if tool_name in explicit:
        return bool(explicit[tool_name]), SOURCE_EXPLICIT

    return False, SOURCE_DEFAULT


# 第三根轴：concurrency_safe
#
# 判据：这一个调用能不能和同一批里别的调用同时跑。
#
# 它与 `permission` / `destructive` 都正交。常见做法也是两根轴
# （只读 vs 并发安全），而本仓库一开始
# 是借 `read` 档判的 —— 「按 `isConcurrencySafe` 分类，只读并发、
# 其余串行」，实现成了借档。
#
# 为什么非分开不可 —— `run_subagent` 是 `admin`，但它可以并发：
# 它能起一个"能改任何东西"的子 agent（，档必须是 `admin`，也只读子 agent 会拿它拒写），
# 而两个子互不相干，凭什么不能同时跑。借档判的后果是
# 档 6 给 8 个位，也是一个一个跑 —— 而它自己的工具描述逐字写着
# 「②几件真能并行的独立小活」（描述与行为不一致 = 「标签在说谎」）。
#
# 兜底 = 由权限档派生（`permission == "read"`）：
#今天全仓行为一个字节都不变（旧判据正是 `perm == "read"`），
#     而"以后要不要放开某个工具"变成一条声明，不再改判定逻辑。
#   "拿不准"的规矩是写 `false`（fail-closed） —— 与 `destructive` 的
#     "拿不准写 `true`"（fail-open）方向恰好相反。不写清，下一个人会照抄错。
#     理由是一笔算得出来的账：并发的代价是串味（共享态互相踩），
#     串行的代价只是慢。

#: 已声明的「能不能并发」：`tool_name -> bool`。由 loader 在注册时填
#: （`bus/loaders/skill_loader.py` 的两条注册路各一处）。
_declared_concurrency: Dict[str, bool] = {}


def declare_concurrency(tool_name: str, value) -> bool:
    """登记一个工具的 `concurrency_safe` 声明。返回是否被接受。

    只收真 `bool`（同 `declare_destructive`）—— `"true"` / `1` / `yes`
    一律不登记（`yes` 被 YAML 读成 `True` 是碰巧对，不是保证）。
    非法值的去处是兜底派生（`permission == "read"`）：`read` 档的工具落回
    `True`（与声明 `false` 的人本意相反，但方向是"不并发"那一侧），
    非 `read` 的落回 `False`。与 `destructive` 一样，非法值不该只靠兜底，
    由三层挡住（装载出声 · 体检 · 工厂拒）。

    幂等：就地覆盖，不是 append（同 `register_tool` / `declare` 的理由）。
    """
    if isinstance(value, bool):
        _declared_concurrency[tool_name] = value
        return True
    return False


def declared_concurrency(tool_name: str) -> Optional[bool]:
    """这个工具声明了什么。没声明，`None`（与 `False` 分得开）。"""
    return _declared_concurrency.get(tool_name)


def resolve_concurrency_safe(tool_name: str, *,
                             permission: str,
                             explicit: Mapping[str, bool]) -> bool:
    """判定一个工具能不能与同批别的调用并发。

    顺序写死（同 `resolve` / `resolve_destructive`，不许重排）：
    `声明 -> 显式表 -> 由权限档派生`。

    不返回 `source`（与那两根轴不同）—— 判过的：`source` 在那边承重是因为
    有棘轮在数"还有几个没人定过档"；这一根的默认值是派生出来的，
    "没人定过"就是绝大多数（145 个），数它没有意义。
    判据：没有读者的那一维不许挂着。

    `core/` 保持叶子：`permission` 与显式表由调用方注入（它们是
    `bus/registry.py` 的注册策略）。这个函数是纯的，测试能喂合成的表。
    """
    declared = _declared_concurrency.get(tool_name)
    if declared is not None:
        return declared
    if tool_name in explicit:
        return bool(explicit[tool_name])
    return permission == PERM_READ


# 测试夹具：一对函数管三根轴

def snapshot() -> Dict[str, object]:
    """三张声明表的副本 —— 给测试用。

    一对函数管三根轴（`perm` + `destructive` + `concurrency`）是故意的：
    拆成三对的话，一个只声明了一根的测试还原时常常会忘掉另外两张，
    而症状是"同批后跑的某个测试莫名看到一条不属于它的声明" ——
    那正是 `tool_output_kind` 那个坑的另一半（改全局状态要能还回去）。

    同 `tool_output_kind.snapshot()`：不是 `reset()`。
    声明表是进程级全局，清空之后同一批里后跑的测试就看不到任何声明了。
测试的规矩是存了再还原（下一条），不是清空。
    """
    return {"perm": dict(_declared),
            "destructive": dict(_declared_destructive),
            "concurrency": dict(_declared_concurrency)}


def restore(snap) -> None:
    """把三张声明表还原到 `snapshot()` 的那一刻。

    空值（`{}` / `None`）＝ 三张表都清空 —— 旧的 `restore({})` 写法
    （`tests/test_permission.py` 拿它把表清干净再自造靶子）继续有效；
    其余形态必须是 `snapshot()` 的原样返回。
    """
    if not snap:
        _declared.clear()
        _declared_destructive.clear()
        _declared_concurrency.clear()
        return
    _declared.clear()
    _declared.update(snap.get("perm", {}))
    _declared_destructive.clear()
    _declared_destructive.update(snap.get("destructive", {}))
    _declared_concurrency.clear()
    _declared_concurrency.update(snap.get("concurrency", {}))
