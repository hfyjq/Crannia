"""框架模式预设 —— `Fern` / `Willow` / `Ginkgo`。

## 它是什么

不是第四个存储，是三个既有旋钮的一次性批处理。后台抽取取 `6 抽 1`：

| 预设 | `memory_continuity` | `effort_level` | 后台抽取频率 |
|---|---|---|---|
| `Fern` | `fresh`（更新档） | `2` = `medium` | 6 抽 1（最懒） |
| `Willow` | `balanced`（均衡档） | `3` = `high` | 20 抽 1 |
| `Ginkgo` | `memory`（记忆档） | `5` = `max` | 1 抽 1（最勤） |

「更新档 / 均衡档 / 记忆档」是 `memory_continuity` 的档名，
不是 `/mode` 那根轴（手动 · 自动 · 计划）—— 两条不同的轴，别混。

它与档位表谁在上（不写死这条就会出两套真相源）：
档位表里只有两格随档（子 agent 个数 + 思考模式），
而一个预设同时动三样，预设只能是档位的上层（=「一次改三项」的批处理），
不是替代。它写进 `data/settings.json` 的，和用户在设置页手改的是同三个键
—— 预设没有自己的值，所以不可能与档位表打架。

## 「归为自定义」存的是事实，不是值

规则：**用户选择预设时覆盖对应设置；只要更改过其中一个就转为自定义，
即使再调回预设同值也是自定义。**

「改回同值也算改过」用值推不出来：三项凑巧等于 `Fern` 的手改，
与"选了 `Fern`"在值上完全一样（两个不同的东西长得一样）。
必须存那个事实 —— 落点 = `data/settings.json["framework_preset"]`，
取值是三个预设名之一、或 `"custom"`。

为什么不派生：一个会撒谎的派生显示比多一个字段坏 ——
用户在设置页把三项手动调成正好等于 `Fern`，
界面就会写「当前：`Fern`」，而那不是他选的（「标签在说谎」的形状）。

没选过 / 键不存在，出厂预设 `Willow`。

理由：新装没有 `settings.json`（不播种），所以"没选过"是默认状态；
而那一刻真正生效的那三格就是基准里那三格 —— 它们恰好等于 `Willow`
（`balanced` / `3` / `20`），报 `Willow` 是如实的，不是猜的。

这不是"拿三项值去比"：`preset_of()` 从来不看那三格（下面那条不变量照旧）。
区别在问的是什么：「用户选过什么」用值推不出来；而「从没选过时是什么」是一个已知的常量。
残余（如实标）：用户绕过界面手改 `settings.json` 里那三格、又不写
`framework_preset`，界面仍会显示 `Willow`。走 `POST /api/settings` 的那条路不会漏 ——
它见到受管键就记 `custom`（`web/server.py`）；而那条路是界面唯一的写入口。
"""

from dataclasses import dataclass

#: 落 `data/settings.json` 的那个键。
SETTING_KEY = "framework_preset"

#: 没有预设时的取值。它是一个值，不是一个"空" ——
#: 空串/`None` 会被读成"没读过"，而这两件事在这个键上不一样。
CUSTOM = "custom"


@dataclass(frozen=True)
class Preset:
    """一个预设 = 名字 + 一句人话 + 三个键的值。"""
    name: str
    summary: str
    settings: dict


PRESETS: tuple = (
    #: 「不计」= `0`（不自动抽取，只靠模型自己判断要不要写）。
    Preset("Fern", "更新档 + medium + 不自动抽取（最省）",
           {"memory_continuity": "fresh",
            "effort_level": 2,
            "memory_extract_every_n_turns": 0}),
    #: `effort_level` 取 3(high)：档位每高一级，每条请求的思考开销跟着涨；而且 Willow 的三格
    #:   与今天的出厂默认逐一相等 —— 它可以当出厂预设而不改变任何既有用户的行为
    #:   （见 `DEFAULT_PRESET`）。
    Preset("Willow", "均衡档 + high + 20抽1",
           {"memory_continuity": "balanced",
            "effort_level": 3,
            "memory_extract_every_n_turns": 20}),
    Preset("Ginkgo", "记忆档 + max + 10抽1",
           {"memory_continuity": "memory",
            "effort_level": 5,
            "memory_extract_every_n_turns": 10}),
)

#: 三个预设名。
NAMES: tuple = tuple(p.name for p in PRESETS)

#: 没选过（键不存在 / 空串）时界面上显示的那一个。
#: 它的三格必须与基准 `data/settings.example.json` 逐一相等 ——
#:   否则"从没选过"的界面会写着一个与生效值不符的名字（`test_framework_preset`
#:   里那条「出厂预设 == 出厂默认三格」钉着它）。
DEFAULT_PRESET = "Willow"

#: 预设动的那三个键 —— 一处。`web/server.py` 用它判
#: 「这一次写进来的是不是它们」，从而决定要不要转 `custom`。
#: 它必须与 `PRESETS` 里那三格逐字一致（有读者钉着：多一个少一个都红）。
GOVERNED_KEYS: tuple = ("memory_continuity", "effort_level",
                        "memory_extract_every_n_turns")


def preset_of(settings: dict) -> str:
    """当前是哪个预设；没记过，出厂预设，记了非法值，`custom`。

    这两种"读不出"必须分开：
    * 键不存在 / 空串 = 从没选过，生效的就是出厂那三格，`DEFAULT_PRESET`；
    * 键在、值认不出 = 有人写了脏数据，`custom`（不许冒充某个预设）。

    从不看那三格的值（见模块开头那条不变量）—— 判据是"记没记过"这个事实。
    """
    v = (settings or {}).get(SETTING_KEY)
    if v is None or v == "":
        return DEFAULT_PRESET
    return v if isinstance(v, str) and v in NAMES else CUSTOM


def values_of(name: str) -> dict:
    """`name` 覆盖的那三格（不含 `framework_preset` 自己）。

    非法名，`KeyError` —— 调用方是"用户点了哪个按钮"，
    所以要拒（不许静默落回某个默认预设：那会让界面写着 `Ginkgo`
    而实际是别的值 ——「标签在说谎」的形状）。
    """
    for p in PRESETS:
        if p.name == name:
            return dict(p.settings)
    raise KeyError(name)


def catalog() -> list:
    """给界面用的只读目录（前端只渲染，别再抄一份名单）。"""
    return [{"name": p.name, "summary": p.summary, "settings": dict(p.settings)}
            for p in PRESETS]
