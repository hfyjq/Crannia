"""窗口的多层来源 —— 值和来源一起给。

## 四层与优先级

原则是「`用户显式声明` > `实际(演算)` > `查表` > `保守兜底`」。
本模块把 `manifest` 那一档也放进来（`provider_loader` 今天就是这么写的），
落成的顺序是：

    ① 声明(config.yaml)      用户自己写的 —— 最高，不许被任何一层悄悄改掉
    ② 演算(实际)              这台机器上真发生过的一次请求（只上不下，见下）
    ③ 查表(models.dev)        外部维护的真值表（`providers/window_table.json`）
    ④ 声明(manifest 兜底)     我们给这家 provider 的粗档默认值
    ⑤ 保守兜底(未声明)        250,000（有意高于常见的 200k）

②③④ 的次序不是随手排的：`声明(manifest 兜底)` 这个名字自己就写着它是兜底，
而原则明写「`实际` 赢过 `表`」。表比 manifest 粗档可信，实际比表可信。
若哪天认为 manifest 该排在表前面，改 `_ORDER` 一处即可（其余全跟着走）。

## 为什么"证据只能把窗口调大"（`演算` 层的方向）

实际给的是下界：我们成功发出过 N 个 token，真实窗口 ≥ N（这个方向上不会错）。
反方向（真实窗口 < 某次 400）也能给上界，但不许为了探测故意发 400
（*"那是花钱买上界"*），本模块只收下界，且只上不下。

一次吸收在下一次解析窗口时生效（provider 重建 / 重启）——不在运行期偷移阈值。
那是「三个阈值静默移动」的形状；运行期唯一该做的是出声
（`_check_window_claim` 已经在做）。

## 表怎么来

`providers/window_table.json` 由 `scripts/refetch_window_table.py` 从
`models.dev/api.json` 摘出（只留我们支持的 16 家；786 条 / 28 KB，原表 5.28 MB）。
它是静态数据：测试读仓库里那份，离线；要更新人来跑一次脚本。

## 匹配规则（表查找）

1. 同一 provider 下精确匹配 model 名；
2. 不行，大小写不敏感（`Qwen3.7-PLUS` vs `qwen3.7-plus`）；
3. 再不行，归一化后匹配（小写 + 丢掉最后一个 `/` 之前的部分 ——
   聚合商把别人的模型挂成 `deepseek-ai/DeepSeek-V3.2`）；
   归一化这一步只在候选唯一时成立 —— 多个候选不猜（落回下一层）。
   判据："读不到"与"读到了好几个"都得能落回兜底，不许拿一个随便挑的。

ASCII-only 打印（Windows GBK 控制台）。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

#: 表文件（静态，随仓库走）。按模块位置解析、不按 cwd ——
#: 它是随代码发布的数据（不是运行期数据），用相对路径会在 `sandbox()` 的
#: chdir 之后静默读空表（"读不到"与"没有这一层"长得一模一样）。
#: 测试要换表就传 `load_table(path=…)` 或改这个常量。
TABLE_PATH = Path(__file__).resolve().parent.parent / "providers" / "window_table.json"

#: 兜底窗口：有意高于常见的 200,000。
#: 理由：兜底来源，记忆档会自动降均衡档（`FALLBACK_SOURCES`），
#: 所以宁可给它一个偏大的值（少装一点也比"把真 1M 的模型按 200k 剪"好）。
#: 方向：更晚触发压缩（闸门落在 `250000×0.85 = 212500`）。
FALLBACK_WINDOW = 250_000

#: 哪些来源算"兜底" —— 「来源是"兜底"时，行为要降级」
#: （记忆档自动降到均衡档：宁可少装，不可爆）。`config` 声明与表、实际都不算。
FALLBACK_SOURCES = frozenset({"声明(manifest 兜底)", "保守兜底(未声明)"})

#: 五档来源名。值是给人看的字符串，
#: 所以别在别处重新拼 —— 一律从这里取。
SRC_CONFIG = "声明(config.yaml)"
SRC_MEASURED = "演算(实际)"
SRC_TABLE = "查表(models.dev)"
SRC_MANIFEST = "声明(manifest 兜底)"
SRC_FALLBACK = "保守兜底(未声明)"

#: 优先级（数字越大越优先）—— 见模块 docstring ①–⑤。
_ORDER = {
    SRC_CONFIG: 5,
    SRC_MEASURED: 4,
    SRC_TABLE: 3,
    SRC_MANIFEST: 2,
    SRC_FALLBACK: 1,
}

#: 进程级：`{model: 已成功发出过的最大 prompt_tokens}`（只存下界）。
#: 登记在会话级状态登记表的 `PROCESS_SHARED`（census 会红着提醒）。
_observed: Dict[str, int] = {}
_observed_lock = threading.Lock()

_table_cache: Optional[dict] = None
_table_lock = threading.Lock()


# 表

def load_table(path=None) -> dict:
    """读 `providers/window_table.json`（惰性 + 缓存）。读不出来，空表 + 出声。

    空表 ≠ 没有这一层：空表时 `lookup` 一律返回 `None`，落到下一层
    —— "读不到"要有降级路，不是拿 0 或者拿一个假的顶上。
    """
    global _table_cache
    if path is None and _table_cache is not None:
        return _table_cache
    p = Path(path) if path is not None else TABLE_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        rows = data.get("providers") or {}
        table = rows if isinstance(rows, dict) else {}
    except Exception as exc:  # noqa: BLE001 —— 表坏了只是少一层，不是故障
        print("[window] 窗口表读不出来（%s: %s）—— 本层跳过"
              % (type(exc).__name__, exc))
        table = {}
    if path is None:
        _table_cache = table
    return table


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def _norm_loose(name: str) -> str:
    """归一化：小写 + 丢掉最后一个 `/` 之前的部分（聚合商的前缀）。"""
    return _norm(name).rsplit("/", 1)[-1]


def _row_field(row, key: str):
    """从一条表项里取一个字段。两种形状都认。

    · schema 1（旧）：一条就是个裸整数，那个数是 `context`。
    · schema 2（新）：一条是 `{"context": …, "output": …, "reasoning": {…}}`。

    认两种形状不是兼容姿势，是判据：表是仓库里的一个文件，
    而代码会被单独回退/前进 —— 让"读表"这一步在两种表下都对，
    坏掉的只会是新字段读不到（落到兜底、出声），不是整个窗口层崩掉。
    读不到一律 `None`，不许给 0（读不到 ≠ 0，而 0 像一个结论）。
    """
    if isinstance(row, bool) or row is None:
        return None
    if isinstance(row, int):
        return row if key == "context" else None
    if isinstance(row, dict):
        v = row.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
        return v if key == "reasoning" and isinstance(v, dict) else None
    return None


def _match(provider_dir: str, model: str):
    """按三级规则找那一条：精确 → 大小写不敏感 → 丢聚合商前缀。多候选不猜。

    只此一处 —— `lookup` / `lookup_output` / `lookup_reasoning` 三个查询
    共用它。分三份写的代价不是多写三十行，是三份会漂移，而三个答案
    各自看都对（"写/查/包含性各一处"的同族判据）。
    """
    rows = load_table().get(provider_dir)
    if not isinstance(rows, dict) or not model:
        return None
    if model in rows:
        return rows[model]
    low = _norm(model)
    case_hits = [v for k, v in rows.items() if _norm(k) == low]
    if len(case_hits) == 1:
        return case_hits[0]
    loose = _norm_loose(model)
    loose_hits = [v for k, v in rows.items() if _norm_loose(k) == loose]
    if len(loose_hits) == 1:
        return loose_hits[0]
    return None


def lookup(provider_dir: str, model: str) -> Optional[int]:
    """查上下文窗口（规则见模块 docstring）。多候选不猜，返回 `None`。"""
    return _row_field(_match(provider_dir, model), "context")


def lookup_output(provider_dir: str, model: str) -> Optional[int]:
    """查输出上限（`limit.output`）。读不到，`None`（不是 0）。

    它是 `max_tokens` 的输入：要 `min(它, 64000)` ——
    一刀切 64000 会让 `qwen-max`（实际 8192）当场 400。
    """
    return _row_field(_match(provider_dir, model), "output")


def lookup_reasoning(provider_dir: str, model: str) -> Optional[dict]:
    """查模型自己声明的思考能力（`reasoning_options`）。读不到，`None`。

    它是发送前拦的输入（不声明就不发那个参数）。
    `None` 与 `{"types": []}` 是两件事：前者是"这家没告诉我"（维持现状），
    后者是"这家明确说它没有"（拦）。
    """
    return _row_field(_match(provider_dir, model), "reasoning")


# 输出上限

#: 我们愿意发的输出上限（`64000`）。
#:
#: 它不是"模型的极限"，是我们自己的刹车：真值在表里的 `limit.output`
#: （实际 `deepseek-v4-flash` 393216 —— 按 4096 发只有它的 1/96）。
#: 这个数有出处：常见的截断恢复做法是"先把 `max_tokens` 升到 64_000"
#: （`ESCALATED_MAX_TOKENS`）—— 而那边是升上去的第二步，
#: 我们直接把它当第一步。
MAX_OUTPUT_CEILING = 64_000

#: 表里查不到时发多少（`8192`）。
#:
#: 它是保守方向的数，理由是`qwen-max` 的上限就是 8192
#: （上游 400 原文 + `models.dev` 表逐字一致），拿它当兜底，
#: 任何一家至少不会因为它而 400。比它更小的那些家（若有）会 400 ——
#: 而那是表没抓全的问题（`refetch_window_table.py` 重跑一次就补上），
#: 不是这个数该背的锅。
MAX_OUTPUT_FALLBACK = 8_192


def resolve_max_tokens(provider_dir: str, model: str) -> int:
    """`max_tokens` 该发多少 —— 一处定义。

    ```
    min(表里的 limit.output, MAX_OUTPUT_CEILING)      # 知道就夹取
    MAX_OUTPUT_FALLBACK                               # 不知道就用兜底
    ```

    夹取不是可选项：一刀切 64000 会让 `qwen-max`（8192）当场 400。
    现成的写法是 `Math.min(model.limit.output, OUTPUT_TOKEN_MAX)`。

    读不到 ≠ 无限：查不到就走 8192，不是走 64000
    （拿一个大的顶上，是一家一家地去撞 400）。
    """
    out = lookup_output(provider_dir, model)
    if isinstance(out, int) and out > 0:
        return min(out, MAX_OUTPUT_CEILING)
    return MAX_OUTPUT_FALLBACK


# （下界）

def observe(model: str, prompt_tokens: int) -> None:
    """记一条下界：我们成功发出过这么多 token（`_check_window_claim` 调）。

    只上不下、只收成功那一条路 —— 见模块 docstring。
    """
    if not model or not isinstance(prompt_tokens, int) or prompt_tokens <= 0:
        return
    with _observed_lock:
        if prompt_tokens > _observed.get(model, 0):
            _observed[model] = prompt_tokens


def observed_lower_bound(model: str) -> Optional[int]:
    with _observed_lock:
        return _observed.get(model) or None


def forget_observed() -> None:
    """清空观测（测试用；生产没有调用者 —— 它按进程生灭）。"""
    with _observed_lock:
        _observed.clear()


# 解析（四层合一）

def resolve(*, provider_dir: str, model: str,
            config_value: Optional[int],
            manifest_value: Optional[int]) -> Tuple[int, str]:
    """按优先级给出 `(窗口, 来源)`。永远给得出一个值（最差是兜底）。

    实际只上不下：它不替换任何一个比它大的值 —— 证据不足以把窗口调小。
    """
    candidates = []
    if isinstance(config_value, int) and config_value > 0:
        candidates.append((config_value, SRC_CONFIG))
    if isinstance(manifest_value, int) and manifest_value > 0:
        candidates.append((manifest_value, SRC_MANIFEST))
    table_value = lookup(provider_dir, model)
    if table_value:
        candidates.append((table_value, SRC_TABLE))

    # 只在严格大于当前最优时才算数（见 docstring 的"只上不下"）
    best_value, best_src = (FALLBACK_WINDOW, SRC_FALLBACK)
    for value, src in candidates:
        if _ORDER[src] > _ORDER[best_src]:
            best_value, best_src = value, src

    seen = observed_lower_bound(model)
    if seen and seen > best_value:
        return seen, SRC_MEASURED
    return best_value, best_src


def is_fallback(source: str) -> bool:
    """这一档算不算"兜底"（兜底，记忆档自动降均衡档）。"""
    return source in FALLBACK_SOURCES
