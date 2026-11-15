"""Provider 抽象基类、ChatResponse 与 StreamChunk。

所有 LLM provider 都必须实现 BaseProvider。框架从不直接 import 具体
provider —— 它只与 BaseProvider 打交道。

提供 `StreamChunk`、`chat_stream()` 异步生成器，以及用于运行时健康检查的
`test_connection()`；另有一个模块级 token 计数器，供用量统计使用。
"""

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

# 消息 `content` 的纯文本视图与"有几张图" —— 两件事各一处定义，
#   都在 `core/image_input.py`（`providers` → `core` 是本仓库既有的方向：
#   `core.window_estimate` / `core.token_calibrator` 早就这么引了）。
#   那个模块的 Pillow 是惰性 import 的，所以这一行不会把 Pillow
#   拖进每一个 import 了 `providers.base` 的地方。
from core.image_input import (
    audio_token_allowance, content_text, count_image_blocks,
)

# Global token counter
# 跨所有 provider 调用累加。框架重启时清零。
_token_counter: dict[str, int] = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "api_calls": 0,
    # 缓存命中
    #
    # 为什么要有这三个，而不是一个 `cache_hit_tokens` 就够：
    #
    # 「没报」和「报了 0」必须分得开。上游有两种可能：它压根不告诉你
    # 缓存字段（很多 OpenAI 兼容端点如此），或者它告诉你"这次一个都没命中"。
    # 两者都长得像 `cache_hit_tokens == 0`，而 0 看起来像一个结论
    # ——"命中率 0%，所以缓存没生效"。那是错的，真相是"根本没量到"。
    # 同族判据：`window_source`（来源必须跟着值走）。
    #
    # 所以：`cache_reported_calls` = 上游真报了缓存字段的调用次数。
    # 它是分母的守卫 —— 只有它 > 0，命中率才是一个数；等于 0 时命中率是 `None`。
    "cache_hit_tokens": 0,
    "cache_reported_calls": 0,
    #: 只累加报了缓存字段那几次调用的 `prompt_tokens` —— 命中率的分母。
    #: 不能拿全局 `prompt_tokens` 当分母：那是所有调用之和，
    #: 而分子只来自报了的那部分，两者不同源时算出来的比率没有意义。
    "cache_reported_prompt_tokens": 0,
    # 成本（USD）
    #
    # 三个一起看才有意义：`cost_usd` 是算得出来的那些之和，
    # `cost_unpriced_calls` 是算不出来的次数。
    #只有 `cost_unpriced_calls == 0` 时，那个钱数才是全场的；
    # 否则它是下界（读不到 ≠ 0 —— 把没价的那几次当 0 加进去，
    # 总数就从"下界"变成了一个看着像全款的假数）。
    #
    # 用整数微美元累加，不用 float：钱是累加量，浮点累积误差
    # 会让两个不同的和永远对不上。
    "cost_micro_usd": 0,
    "cost_priced_calls": 0,
    "cost_unpriced_calls": 0,
}
_token_counter_started: float = 0.0  # 计数器上次清零时的 time.time()
#: 一次性缓存探针是否已经响过（进程级，`reset_token_counter` 会复位）。
_cache_probe_done: bool = False
#: 每多少次 API 调用打一行累计命中率。见 `record_token_usage` 里那段理由。
#: 别调成 1 —— 那会让每次调用多一行，把整场读数淹掉。
#: （`[calibrator] Updated:` 只在 factor 变化时打，校准一旦收敛就一行都不打了，
#:  所以它不能替代这里的周期性读数。）
_CACHE_REPORT_EVERY: int = 5


def read_cache_hit(usage) -> "int | None":
    """从上游 `usage` 里读「命中前缀缓存的 prompt token 数」。

    读不到返回 `None`，不返回 0。见 `_token_counter` 上面那段 ——
    `None` 是"上游没报"，`0` 是"报了且确实一次都没命中"。这两个是不同的信息，
    而它们看起来一样，所以调用方必须能分辨。

    三个来源，按顺序读：先读 `usage.prompt_tokens_details.cached_tokens`，
    没有再看 `usage.prompt_cache_hit_tokens`：

    ====================  ============================================
    `prompt_tokens_details.cached_tokens`   OpenAI 口径（已装的 openai
                                               2.44.0 里是类型化字段）
    `prompt_cache_hit_tokens`               DeepSeek 扩展（落在
                                               `model_extra`，`getattr` 也取得到）
    `cache_read_input_tokens`               Anthropic 原生
    ====================  ============================================

    三种形状都试，一个 dict/对象同时认。上游的 usage 有的是 SDK 的
    类型化对象、有的是裸 dict（我们自己拼的、或某个兼容端点），
    只认一种会在另一条路上静默返回 None。

    和 `prompt_tokens` 的关系：包含，不是并列。
    DeepSeek 官方口径：
    `prompt_tokens` 等于 `prompt_cache_hit_tokens` + `prompt_cache_miss_tokens`。
    我们把 `prompt_tokens` 原样保留（含命中） —— 它已经喂给校准器、
    压缩闸门、上下文显示，改它的含义会让三个阈值静默移动。
    命中数作为它的子计数附上；要算钱的地方在那一个地方减，不在入口改口径。
    """
    if usage is None:
        return None

    def _pick(container, name):
        if container is None:
            return None
        if isinstance(container, dict):
            v = container.get(name)
        else:
            v = getattr(container, name, None)
        return v if isinstance(v, int) else None

    # ① `prompt_tokens_details.cached_tokens`（OpenAI 口径）
    details = (usage.get("prompt_tokens_details") if isinstance(usage, dict)
               else getattr(usage, "prompt_tokens_details", None))
    hit = _pick(details, "cached_tokens")
    if hit is not None:
        return hit

    # ② DeepSeek 扩展
    hit = _pick(usage, "prompt_cache_hit_tokens")
    if hit is not None:
        return hit

    # ③ Anthropic 原生
    return _pick(usage, "cache_read_input_tokens")


# 成本（USD）
#
# token 计量已由唯一的用量累加器承担，成本这一层只负责
# "按模型乘一张价格表"。
#
# 价格表是「会长毛的数据」（模型一多就过期），两条对策：
#   ① 落成带 `fetched` 日期的生成物（`scripts/refetch_window_table.py` 生成，
#      与窗口表分开两个文件 —— 它们烂的速度不一样）；
#   ② 读不到就是 `None`，不是 0 —— 0 是一个结论（"免费"），
#      而真相是"这张表里没有它"。

#: 价格表的路径（与窗口表并列）。懒加载：只在真算钱时读一次。
_COST_TABLE_PATH = Path(__file__).with_name("cost_table.json")
_cost_table: "dict | None" = None
_cost_lookup: "dict | None" = None


def _load_cost_table() -> tuple[dict, dict]:
    """读一次价格表，建两张索引：`(整张表, {(provider, model): 价})`。

    读坏了 / 文件不在，返回 `({}, {})`（表空 = 一律未定价，
    而不是"一律免费"）。不许因为价格表坏掉让一轮对话失败。
    """
    global _cost_table, _cost_lookup
    if _cost_lookup is not None:
        return _cost_table or {}, _cost_lookup
    table: dict = {}
    lookup: dict = {}
    try:
        table = json.loads(_COST_TABLE_PATH.read_text(encoding="utf-8"))
        for prov, models in (table.get("providers") or {}).items():
            for name, row in (models or {}).items():
                if isinstance(row, dict):
                    lookup[(str(prov), str(name))] = row
    except Exception as exc:  # noqa: BLE001 —— 见上：坏掉只是"一律未定价"
        print(f"[cost] 价格表读不了（一律按未定价处理）：{type(exc).__name__}: {exc}")
        table, lookup = {}, {}
    _cost_table, _cost_lookup = table, lookup
    return table, lookup


def reset_cost_table_cache() -> None:
    """丢掉价格表的进程内缓存（重跑 `refetch_window_table.py` 之后不用重启）。"""
    global _cost_table, _cost_lookup
    _cost_table = _cost_lookup = None


def cost_table_meta() -> dict:
    """价格表的来源 + 日期 —— 让读数能回答"这个价格是什么时候的"。"""
    table, _ = _load_cost_table()
    return {
        "source": table.get("source"),
        "fetched": table.get("fetched"),
        "models": len(_cost_lookup or {}),
    }


def cost_usd(provider: str, model: str, prompt_tokens: int,
             completion_tokens: int,
             cache_hit_tokens: "int | None" = None) -> "float | None":
    """这一次调用的估算美元成本；算不出来就返回 `None``。

    ## 口径（三个别混）

    * `prompt_tokens` 是含命中的总量（`read_cache_hit` 那条注释：DeepSeek
      官方口径 `prompt = hit + miss`），要算钱，就在这里减一次。
      别去改入口的口径：它已经喂给校准器 / 压缩闸门 / 上下文显示，
      改它会让三个阈值静默移动。
    * `cache_hit_tokens is None` = 上游没报（不是 0），那就整段按未命中价算，
      得到一个上界。这是"已知信息下的最诚实答案"，但它不是精确值。
    * 价格单位：USD / 每 100 万 token（models.dev 的口径）。

    ## 返回 `None` 的两种成因（都不许塌成 `0`）

    ① 这张表里没有这个 `(provider, model)`；
    ② 表里有、但缺 `input` 或 `output`（生成器已把这类整条丢掉）。
调用方拿到 `None` 时要把它当成"不知道"，去累加
    `cost_unpriced_calls`，不是加 0 了事 —— 加了 0，那个总数就从
    "下界"变成了"看着像全款的假数"（读不到 ≠ 0）。
    """
    _, lookup = _load_cost_table()
    row = lookup.get((str(provider), str(model)))
    if not row:
        return None
    pin, pout = row.get("input"), row.get("output")
    if not isinstance(pin, (int, float)) or not isinstance(pout, (int, float)):
        return None

    # 两处都夹：命中数不许超过输入数（上游口径变了 / 字段读错了的时候，
    # 不夹会算出比实际输入还贵的钱）。判据同 `ChatResponse.cache_miss_tokens`
    # 那里也 `max(0, …)`：宁可给一个偏高的上界，也不许给一个负数或超发的量。
    total_in = max(0, int(prompt_tokens))
    hit = min(max(0, int(cache_hit_tokens or 0)), total_in)
    miss = total_in - hit
    # 命中那段按 `cache_read` 计价；表里没这一格，退回 `input` 价
    # （这是上界：真实成本只会更低，不会更高）。
    read = row.get("cache_read")
    read_price = read if isinstance(read, (int, float)) else pin
    return (miss * float(pin) + hit * float(read_price)
            + max(0, int(completion_tokens)) * float(pout)) / 1_000_000.0


def record_token_usage(prompt: int, completion: int,
                       cache_hit: "int | None" = None,
                       provider: str = "", model: str = "") -> None:
    """provider 在每次 API 调用后调它来累加用量。

    *cache_hit* 是 :func:`read_cache_hit` 读出的缓存命中数
    （`None` = 上游没报）。在这里累加而不是在展示层求和，
    是为了保持唯一的累加器。

    `provider` / `model`：算钱要那两个。不传，这一次按未定价记
    （`cost_unpriced_calls += 1`），不是记 0 元 —— 见 `cost_usd` 的 docstring。
    """
    global _token_counter_started, _cache_probe_done
    _token_counter["prompt_tokens"] += prompt
    _token_counter["completion_tokens"] += completion
    _token_counter["api_calls"] += 1
    if cache_hit is not None:
        _token_counter["cache_hit_tokens"] += cache_hit
        _token_counter["cache_reported_calls"] += 1
        _token_counter["cache_reported_prompt_tokens"] += prompt

    # ── 成本 ── 累加器只有这一个（不许在展示层自己乘一遍）。
    _c = cost_usd(provider, model, prompt, completion,
                  cache_hit_tokens=cache_hit)
    if _c is None:
        _token_counter["cost_unpriced_calls"] += 1
    else:
        _token_counter["cost_micro_usd"] += int(round(_c * 1_000_000))
        _token_counter["cost_priced_calls"] += 1

    # 缓存的两条报信
    #
    # 为什么要在进程里响，而不是只把数存在 `get_token_usage()` 里：
    # `read_cache_hit` 读不到时返回 `None`，而 `None` 的两种成因长得一样 ——
    # 「上游不报缓存字段」（那这个指标在我们的 provider 上永远不可用）和
    # 「报了我们没读到」（字段名又改了 / 路径写错了）。
    # 前者是结论，后者是我们的 bug，而它们不响就分不开。
    #
    # 只有一次性提示不够：它只回答了"上游报不报"，
    # 没回答"这场的命中率是多少"。还有第二条：
    # 每 `_CACHE_REPORT_EVERY` 次调用一行滚动读数。
    #
    # 为什么是这个频率：一次对话通常每轮 2~6 次 API 调用，
    # 所以 N=5 ≈ 每轮一行 —— 有曲线，又不至于把控制台淹掉。
    #
    # 两条各司其职：
    #   · 一次性：第一次真读到就喊 —— 让"这个 provider 能不能量"尽早有答案
    #     （不必等到第 5 次）
    #   · 滚动：每 N 次一行，带累计命中率 —— 这才是"命中率"那个数
    #
    # 滚动行还带 `本次输入` 字段：只给累计值时，"每次调用的输入"只能对累计
    # 做差；带上它才能直接与逐条 trace 对齐（每条记录 = 一次调用 + 它的输入
    # token 数）。
    # 注意一：打印的是触发这次输出的那一次调用的输入，不是平均。
    # 注意二：这个累加器是 provider 级的 —— 主循环与辅助调用（事实抽取 /
    #   会话笔记 / 压缩）都从这里过，所以第 5、10... 次有可能是辅助调用，
    #   读数会掉一截。对齐曲线时先看它是不是辅助调用，
    #   别把"辅助调用"读成"上下文缩小了"。
    calls = _token_counter["api_calls"]
    if not _cache_probe_done and cache_hit is not None:
        _cache_probe_done = True
        print(f"[cache] 上游报了缓存字段：本次命中 {cache_hit} / 输入 {prompt}"
              f" —— 命中率指标可用")
    if calls % _CACHE_REPORT_EVERY == 0:
        _rp = _token_counter["cache_reported_prompt_tokens"]
        if _rp > 0:
            _ht = _token_counter["cache_hit_tokens"]
            print(f"[cache] 第 {calls} 次调用 · 本次输入 {prompt:,} · 累计命中 "
                  f"{_ht:,}/{_rp:,} = {_ht / _rp:.1%}"
                  f"（{_token_counter['cache_reported_calls']}/{calls} 次上游报了）")
        else:
            print(f"[cache] 第 {calls} 次调用 · 本次输入 {prompt:,} · 上游一次都没报缓存字段"
                  "（prompt_tokens_details.cached_tokens / prompt_cache_hit_tokens /"
                  " cache_read_input_tokens 三个都没有）"
                  " ⇒ 这个 provider 量不到命中率，**不是命中 0**")

    if _token_counter_started == 0.0:
        _token_counter_started = time.time()


def get_token_usage() -> dict:
    """返回当前的 token 用量统计。对展示场景足够线程安全。

    `cache_hit_rate` 在某些调用真的报了缓存数之前是 `None` ——
    `None` 与 `0.0` 意思不同（「没量到」 vs 「量到了，一次没中」），
    展示代码不许把它们塌成一个值。
    """
    reported_prompt = _token_counter["cache_reported_prompt_tokens"]
    rate = (_token_counter["cache_hit_tokens"] / reported_prompt
            if reported_prompt > 0 else None)
    # ── 成本 ── `cost_usd` 那格只在"全场都定得出来价"时才是全场。
    # 在它旁边必须同时给出 `cost_unpriced_calls`；`cost_is_complete` 是
    # 展示层唯一该看的那个布尔（别让一个"下界"看起来像"全款"）。
    unpriced = _token_counter["cost_unpriced_calls"]
    return {
        **_token_counter,
        "total_tokens": _token_counter["prompt_tokens"] + _token_counter["completion_tokens"],
        "cache_hit_rate": rate,
        "cost_usd": _token_counter["cost_micro_usd"] / 1_000_000.0,
        "cost_is_complete": unpriced == 0 and _token_counter["cost_priced_calls"] > 0,
        "cost_table": cost_table_meta(),
        "uptime_seconds": int(time.time() - _token_counter_started) if _token_counter_started else 0,
    }


def reset_token_counter() -> None:
    """Reset the global token counter (called on framework restart)."""
    global _token_counter_started, _cache_probe_done
    for _k in _token_counter:
        _token_counter[_k] = 0
    _cache_probe_done = False
    _token_counter_started = time.time()


# 图片在估算里值多少 token
#
# ## 为什么必须有这个常量
#
# 带图那一轮的消息 `content` 是块列表，里面那张图的 base64 有几十万字符。
# 两条错法都在眼前：
#
#   · 按 base64 数，`count_tokens` 会算出好几万 token（一张 400 KB 的截图
#     base64 后 ~533 K 字符 ≈ 13 万 token）。上游根本不是这么计的
#     （图片按分块/像素计，一张 1568px 的图约一千出头），而压缩闸门
#     会据此当场触发，把一张截图当成"上下文要爆了"。
#   · 算 0，估算系统性偏低，而 `estimate_request_tokens` 的偏差会
#     被校准器吃进 `factor` 并落盘，污染之后所有估算
#     （`est=1881 actual=6292` 时，factor 被推到 1.5466）。
#
#一个额度，按张数乘。出处：Anthropic 公开的近似式是
# `tokens ≈ 宽 × 高 / 750`；`MAX_EDGE_PX = 1568` 下常见横图（1568×882）
# 算出来 ≈ 1844，方图（1568×1568）≈ 3278 —— 但那两条是上界，
# 而多数上游在长边上还要再分块（512 一档）。取 1000 是"量级对"
# 那一条：宁可偏一点，也不要一个会让闸门误触发的数。
# 它是常量不是旋钮 —— 只有明确列入随档名单的项才允许随档变化。
IMAGE_TOKEN_ALLOWANCE = 1000


#: 这个 provider 此刻收不收得下图片 ——
#: 它挂在 `BaseProvider` 上（见那个类里的 `supports_vision`），
#: 而判定在这里，因为"取不到怎么办"是个决定，得有个地方写下它。
def accepts_audio(provider) -> bool:
    """这个 provider 此刻能不能收音频 —— 取不到就是不能。

    ## 方向与 `accepts_images` 相反（这是有意的）

    - 图片：默认 True —— 现代模型绝大多数能看图，且图片输入是常见能力；
      兜底 `False` 的实际效果是"这个功能对绝大多数用户默认是坏的"
      （理由全文见 :func:`accepts_images`）。
    - 音频：默认 False —— 今天能收音频的聊天模型是少数；而把一个
      `input_audio` 部件发给文本模型，最可能的结局是上游 400 打断整轮。
这里没有"常见的好情况"可以保护，只有"少数派才该拿到它"。

    声明位置：`config.yaml` 的 `model.supports_audio: true`（逐模型）。
      声明被装载器判一次，同时进构造函数与注册表元数据。
    """
    return bool(getattr(provider, "supports_audio", False))


def accepts_images(provider) -> bool:
    """这个 provider 此刻能不能收图 —— 取不到就是能。

    ## 方向选择

    * 它是能力轴，判断错了不留任何安全洞：最坏结果是一次失败的上游请求。
    * 兜底 `False` 的实际效果是"这个功能对绝大多数用户默认就是坏的"
      —— 而现代模型绝大多数能看图。那是**为保护一个罕见的坏情况把常见的
      好情况关掉**（安全机制不该妨碍把事做完）。
    * 判错可诊断、可恢复：上游 400 一次，而
      :func:`image_hint_for_failure` 会在那一刻把"这一轮带了图"打在控制台上。

    替身 / 老 provider 没有这个属性，走 `True`，不是 AttributeError
    （`test_connection` 之类的路会传半成品进来）。
    """
    return bool(getattr(provider, "supports_vision", True))


def image_hint_for_failure(messages, err) -> str:
    """这一轮带了图、而请求又失败了，返回一句提示；否则返回空串。

    ## 为什么不按错误串去认

    只认少数几句固定措辞（如 `prompt is too long` /
    `input is too long for requested model`）时，DeepSeek 实际返回的
    报错一个都不含，不压缩、不熔断、不预警，盘上零痕迹。
乱加关键词的代价是"看着像认出来了"，而没认出来的仍然静默。

    我们没有上游"模型不收图"的报错原文，不猜。
    这里做的是上下文提示：我们知道两个事实 ——
    ① 这一轮带了图；② 这个错误归不到任何已知类别。
    "模型可能不收图"是此刻唯一一条我们还不知道的信息，
    而它正好对应一个用户改得动的开关（`supports_vision: false`）。
    """
    try:
        n = sum(count_image_blocks((m or {}).get("content"))
                for m in (messages or ()) if isinstance(m, dict))
    except Exception:  # noqa: BLE001 —— 提示不该因为数不出来而抛
        n = 0
    if not n:
        return ""
    if classify_api_error(err) is not None:
        # 归到了已知类别（超长 / 鉴权 / 限流），那条路有它自己的处置，
        # 别在这里加噪声
        return ""
    hint = ("[image] 这一次请求带了 %d 张图，而上游的报错归不到已知类别 —— "
            "如果它是'这个模型收不了图'，到 `config.yaml` 的 `model` 段写 "
            "`supports_vision: false` 就能让图不再发出去（文字照常过）。"
            "原文: %s" % (n, str(err)[:300]))
    print(hint)
    return hint


def flatten_system(system: "str | list[str] | None") -> str:
    """把 `system` 参数归一成 wire 需要的单个字符串。

    `None` / `[]` → `""`（没有 system prompt）。列表用空行连接 ——
    这与"wire 只接受单个 prompt 时把块数组折回一个字符串"是同一种做法。

    由 provider 调用；调用方不该调 —— 把列表原样往下传，
    让块结构活到真正需要它的那一层。
    """
    if not system:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(part for part in system if part)


@dataclass
class ChatResponse:
    """provider 的 chat() 调用的结构化返回值。"""

    content: str | None = None
    """assistant 的文本回复（有 tool_calls 时为 None）。"""

    tool_calls: list[dict] | None = None
    """函数调用形式的工具调用（如果有）。"""

    prompt_tokens: int = 0
    """输入消耗的 token 数（messages + tools）。来自 API 的 ``usage``。

    ⚠️ **这个数含缓存命中**（DeepSeek 口径：``prompt_tokens`` = 命中 + 未命中）。
    见 :func:`read_cache_hit` 的 docstring。
    """

    completion_tokens: int = 0
    """输出消耗的 token 数。来自 API 的 ``usage``。"""

    cache_hit_tokens: "int | None" = None
    """其中**命中前缀缓存**的部分。``None`` = 上游没报，**不是 0**。

    ★ 只有它非 ``None`` 时，两个数相减才是有意义的 ——
    ``prompt_tokens - cache_hit_tokens`` = 这次真正重新算过的量（= 钱的来源）。
    """

    finish_reason: str | None = None
    """上游说的**这一条为什么结束**（`stop` / `tool_calls` / `length` …）。

    ★★ 判"这一轮**是不是被上游截断**了"**只有这一个依据**。
    `length` = 输出撞上额度上限、**被拦腰截断** —— 那一刻 ``content`` 可能
    整个是空的（思考把输出额度吃光 ⇒ 正文一个字都没剩），
    而下游光看 ``content`` / ``tool_calls`` **分不出它和"模型正常说完了"**。

    ⚠️ 读不到时是 ``None``，**不是空串**（与 ``cache_hit_tokens`` 同一条判据：
    读不到 != 0）。上游没报 ⇒ 下游**不许猜成被截断**。

    ⚠️ 它是 `StreamChunk` 上那个同名字段的**兄弟** —— 两条路各有一份，
    两条都必须填它。
    """

    @property
    def cache_miss_tokens(self) -> "int | None":
        """真正重新算过的 prompt token 数。推出来的，不另读一个字段。

        依据：DeepSeek 官方口径 prompt_tokens = hit + miss（逐字见
        `read_cache_hit`）。少读一个字段就少一处会漂的地方 —— 而且
        `prompt_cache_miss_tokens` 与 `prompt_cache_hit_tokens` 若同时
        读进来还能互相对不上，这里不可能。
        """
        if self.cache_hit_tokens is None:
            return None
        return max(0, self.prompt_tokens - self.cache_hit_tokens)

    reasoning_content: str | None = None
    """DeepSeek 推理模型 / Claude thinking：模型的内部推理链。
    对于要求回传的 provider（DeepSeek、OpenAI o-series），
    **必须**在下一条 assistant 消息里原样带上。"""

    media: "list[dict] | None" = None
    """★ 上游**直接产出的媒体**（生图 / 生音 / 生视频）。

    形状：``[{"data_url": "data:image/png;base64,…", "name": "…"}]`` —— **只用
    data URL**（`http(s)` 外链不下载：那会绕开出网闸）。
    `None` = 这一家/这一轮**没有媒体**，与"有媒体但读不出来"不是一件事
    （后者会在 provider 里打一行日志）。

    流式那条也解析它：分片里的媒体同样落成
    `StreamChunk.media`，累积到本轮产物里，**与同步那条同一个桶、同一个挂载点**。
    各家的形状仍不一致 ⇒ 解析只在**两个 provider 各自那一处**（OpenAI 兼容走
    `_extract_media`，Anthropic 走 `image` 内容块），框架侧只认这一个字段。
    """

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class StreamChunk:
    """流式回复中的一个分片。

    调用方累积 `content` 分片直到 `finish_reason` 出现，并按
    槽位号（见 `tool_call_index`）累积 `tool_call` 分片。

    属性：
        content: 增量的文本内容（可能为空 / None）。
        tool_call: 正在拼装的工具调用片段（由调用方累积）。
        tool_call_index: 协议给的槽位号。
            累积必须按它归并，不能按 id、更不能按"来一片新建一条" ——
            流式协议里后续分片不带 id（OpenAI 只在第一片给，Anthropic 只在
            `content_block_start` 给），按 id 归并会把同一次调用劈成两条。
            两家协议都有这个字段（OpenAI `delta.tool_calls[i].index`；
            Anthropic 事件自带的 `index`）。取不到时保持 `None`，由累积方兜底。
        finish_reason: `"stop"`、`"tool_calls"`、`"length"` 或 None。
        usage: token 用量字典（通常在最后一片）。
        error: 该分片代表失败时的错误字符串。
        reasoning_content: 带扩展思考的模型（Claude 等）的思考内容。
    """

    content: str | None = None
    tool_call: dict | None = None
    tool_call_index: int | None = None
    finish_reason: str | None = None
    usage: dict | None = None
    error: str | None = None
    #: 错误类别（`connection` / `timeout` / `context_overflow` /
    #: `auth` / `rate_limit` / 空 = 认不出）。
    #: 它是给用户面用的（`core/llm_errors.explain`）—— 只有 `error`
    #: 一个裸串时，下游只能靠关键词猜，而"猜"会把不同故障混成同一句话。
    error_kind: str | None = None
    reasoning_content: str | None = None
    #: 这一片里上游直接给的媒体。
    #: 形状与 `ChatResponse.media` 逐字相同（`[{"data_url", "name"}]`，只收
    #: data URL），累积方一个循环就能把它并进本轮产物，不必认识各家协议。
    #: `None` = 这一片没有媒体（空 ≠ 没有：空列表不该出现，出现也当没有）。
    media: list | None = None


# ------------------------------------------------------------------
# 上游错误分类 —— 一个来源，多处引用
# ------------------------------------------------------------------
#
# 一条都不许按单一字面量匹配。只认两句固定措辞
# （`prompt is too long` / `input is too long for requested model`）时，
# DeepSeek 实际返回的是：
#
#     This model's maximum context length is 1048576 tokens.
#     However, you requested 1048974 tokens …
#
# —— 一个都不含，不压缩、不熔断、不预警，盘上没有任何痕迹。
#
# `context_overflow` 与另外两类的处置完全不同：
# 换 key / 重试对它一点用都没有（那不是权限也不是限流，是请求本身太大）。
# 认不出来的代价是：重试只会再爆一次，而盘上一条痕迹都没有。
#
# 这三张表只在这一处定义，同步与流式两条路径共用。

_AUTH_PATTERNS: tuple[str, ...] = (
    "401", "403", "unauthorized", "forbidden",
    "invalid api key", "invalid key", "auth",
)
_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "429", "rate limit", "too many requests",
)
#: 余额 / 欠费 —— 与限流必须分开：一个的下一步是充值，另一个是等一会儿。
#: 判据只认各家的原话；"quota" 放在这里而不是限流表里（OpenAI、Moonshot、
#: 智谱都用它表示"额度用完了"）。
_BALANCE_PATTERNS: tuple[str, ...] = (
    "insufficient_quota", "insufficient quota", "insufficient balance",
    "insufficient_balance", "credit balance", "exceeded your current quota",
    "quota exceeded", "quota", "payment required", "billing",
    "余额", "欠费", "充值",
)
#: 地区 / 国别限制 —— 它长得像 403，但不是 key 错，改 key 一点用都没有。
_REGION_PATTERNS: tuple[str, ...] = (
    "country, region, or territory not supported",
    "unsupported_country", "unsupported country",
    "not supported in your region", "region not supported",
)
#: 内容审核 —— 上游自己拦下来的，重试无用，但用户换个说法能过去。
_CONTENT_PATTERNS: tuple[str, ...] = (
    "content policy", "content_filter", "content filter",
    "safety system", "sensitive", "敏感", "不安全", "内容安全",
    "code': '1301", 'code": "1301', "非法",
)
#: 请求体太大（与"上下文太长"同处置，但措辞不同）。
_TOO_LARGE_PATTERNS: tuple[str, ...] = (
    "request_too_large", "request exceeds the maximum allowed",
)
#: 顺序无关（三类是并列判据），但这张表只会长：新供应商换一个说法就加一行。
_OVERFLOW_PATTERNS: tuple[str, ...] = (
    "maximum context length",            # DeepSeek / OpenAI 兼容系
    "context_length_exceeded",           # OpenAI 的 error.code
    "context length",                    # 宽一档的兜底（覆盖上面两种的变体）
    "prompt is too long",                # Anthropic
    "input is too long for requested model",   # Anthropic 的另一种
    "reduce the length of the messages",       # OpenAI 的建议语
    "maximum number of tokens",
    "request too large",
    "too many tokens",
)


#: 状态码 -> 类别。5xx 按"用户能做什么"分三档：故障 / 繁忙 / 其余。
_BY_STATUS: dict[int, str] = {
    401: "auth",
    402: "balance",
    413: "too_large",
    422: "request",
    429: "rate_limit",
    500: "server_fault",
    503: "server_busy",
}


def _status_of(err: object) -> int:
    """上游的 HTTP 状态码；读不到回 0（读不到不等于 0 —— 0 就是"读不到"）。"""
    code = getattr(err, "status_code", None)
    if isinstance(code, int):
        return code
    resp = getattr(err, "response", None)
    code = getattr(resp, "status_code", None)
    if isinstance(code, int):
        return code
    import re as _re
    m = _re.search(r"error code:\s*(\d{3})", str(err), _re.I)
    return int(m.group(1)) if m else 0


def classify_api_error(err: object) -> str | None:
    """把上游报错分成一类：`context_overflow` / `balance` / `auth` / `region` /
    `content` / `too_large` / `request` / `rate_limit` / `server_fault` /
    `server_busy` / `server_other` / `None`。

    `None` 不等于"没问题" —— 它只是"这些都不是"。
不许用 `if not classify_api_error(e)` 当成功判据
    （读不到 ≠ 0，认不出 ≠ 没出错）。

    优先级（顺序是承重的）：
      1. `context_overflow` 最先 —— 超长报文里常带 `tokens`，
         而且它的处置最特殊（重试无用、要压缩）。
      2. 措辞类（余额 / 地区 / 内容审核 / 请求过大）先于状态码 ——
         同一个 429 在 OpenAI 是余额、在其他厂商是频率，只有原文分得开。
      3. 状态码。
      4. 只剩措辞的兜底（报文里没有 `Error code:` 的走这里）。
    """
    text = str(err).lower()
    if not text:
        return None
    if any(p in text for p in _OVERFLOW_PATTERNS):
        return "context_overflow"
    if any(p in text for p in _BALANCE_PATTERNS):
        return "balance"
    if any(p in text for p in _REGION_PATTERNS):
        return "region"
    if any(p in text for p in _CONTENT_PATTERNS):
        return "content"
    if any(p in text for p in _TOO_LARGE_PATTERNS):
        return "too_large"
    code = _status_of(err)
    if code:
        kind = _BY_STATUS.get(code)
        if kind:
            return kind
        if code == 403:
            return "auth"
        if 500 <= code < 600:
            #: 529（Anthropic 过载）与 502/504 一起归"上游异常"。
            return "server_other"
    if any(p in text for p in _AUTH_PATTERNS):
        return "auth"
    if any(p in text for p in _RATE_LIMIT_PATTERNS):
        return "rate_limit"
    return None


def classify_transport_error(err: object) -> str:
    """连接 / 超时 —— 按异常类型与关键词判，与上面三类正交。

    为什么单列：`classify_api_error` 判的是报文（overflow / auth / limit），
      而"连不上"这一类报文里没有任何业务信息（就是 `Connection error.`）。
只看报文会把它归到 `None`，下游就只能给一句"内部异常"。
    返回 `"timeout"` / `"connection"` / `""`（认不出）。
    """
    name = type(err).__name__.lower()
    text = str(err or "").lower()
    if "timeout" in name or "timed out" in text or "timeout" in text:
        return "timeout"
    if ("connection" in name or "connect" in name
            or "connection error" in text or "connecterror" in text
            or "econnrefused" in text or "econnreset" in text
            or "network is unreachable" in text):
        return "connection"
    return ""


def log_context_overflow(model: str, err: object, *, where: str = "") -> None:
    """上游说「请求超长」时必须出声。

    这条线默认是静默的：认不出来，一切照旧、盘上零痕迹。
    「没有输出 ≠ 什么都没发生」—— 通道轮询死了半小时与闪断一秒，
    不报的话在盘上留下的痕迹完全一样。

    与 `[cache]` / `[calibrator]` 同款，走 `print`，只到 stdout
    （落盘属于另一层的工作，不在这里顺手做）。
    """
    print(f"[overflow] 上游报「请求超长」（model={model}"
          f"{', ' + where if where else ''}）—— "
          f"**重试 / 换 key 对它无效**（不是权限也不是限流），需要压缩或裁剪。"
          f" 原文: {str(err)[:300]}")


def _reply_side_tokens(m: dict, count_tokens) -> int:
    """一条 assistant 消息上不在 `content` 里、却真的在 wire 上的那些。

    为什么要有它：`estimate_request_tokens` 只读 `m["content"]` 会漏掉
    assistant 消息上两样属于同一条请求的东西：

    * `reasoning_content` —— 思考正文。带工具调用的那一轮必须回传它
      （上游官方文档逐字要求）—— 它走历史、走 wire，上游按输入计费；
    * `tool_calls[].function.arguments` —— 模型给出的参数全文。

    按 `cl100k` 同一把尺量 334 条消息：
    `content` 56,262 tokens，`reasoning_content` 152,596 ——
    后者是前者的 2.7 倍。估算器报 `71,638`，provider 报 `243,855`。

    而这不是"显示不准"：压缩闸门（`core/memory/compressor.py`）
    调的是同一个函数，它会拿一个小了 3.4 倍的数在判"什么时候该压"。
    这正是"唯一仪器"最怕的形状 —— 唯一的仪器量错了量纲。

    **口径一改，`TokenCalibrator.ESTIMATOR_SCHEME` 必须 +1**：
    旧样本是另一个量的样本，留着就是拿一把为错误量纲校准的尺子量新数
    （`test_calibration_units` 钉着它）。

    是模块级函数、不是方法：`count_tokens` 由调用方传进来。
    有替身只实现 `count_tokens` + `estimate_request_tokens` 两个口就去调真算术
    —— 加一个 `self.` 方法会让它们当场 `AttributeError`，
    而那看起来像"估算器坏了"，不像"替身少了东西"。

    只认 `str` 的 `reasoning_content`；非字符串一律当没有
    —— 那格本来就不该是别的东西，猜它等于给"读不到"编一个数
    （读不到 ≠ 0）。
    """
    extra = 0
    _r = m.get("reasoning_content")
    if isinstance(_r, str) and _r:
        extra += count_tokens(_r)
    _tc = m.get("tool_calls")
    if _tc:
        try:
            extra += count_tokens(json.dumps(_tc, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            # 参数里有个序列化不了的东西，还是要算（漏算就是上面那个病），
            # 退回 repr 长度。它比真实值粗，但方向仍然是"算进去"。
            extra += count_tokens(str(_tc))
    return extra


class BaseProvider(ABC):
    """Provider 抽象接口。

    每个 LLM 后端（DeepSeek、GLM、OpenAI、Anthropic、Ollama …）都必须
    继承本类并实现抽象成员。

    提供 `chat_stream()` 异步流式接口、`test_connection()` 运行时健康检查，
    以及 `_calibrator` 自校准 token 估算。
    """

    # 由子类设置（或惰性创建）
    _calibrator: object | None = None
    """TokenCalibrator 实例 —— 由具体 provider 创建。"""

    #: 这个 provider 此刻收不收得下图片。
    #:
    #: ## 默认是 `True`
    #:
    #: 它是能力轴，判断错了不留任何安全洞，只会造成一次失败的请求；
    #: 而默认关的实际效果是"这个功能对绝大多数用户默认就是坏的"
    #: —— 现代模型绝大多数能看图。
    #:完整理由见 `providers/base.py::accepts_images`。
    #:
    #: ## 只有一层配置
    #:
    #:   ① `config.yaml` 的 `model.supports_vision`（逐模型）
    #:   ② 否则 `True`
    #: 没有 provider `manifest.yaml` 那一层：能力是逐模型的事实，
    #: 而 manifest 是逐供应商的（粒度不匹配）。
    #: 完整理由在 `bus/loaders/provider_loader.py::_resolve_vision` 的 docstring。
    #: 装载器判一次，结果同时进构造函数与注册表元数据（来源必须跟着值走）。
    #: 想关掉就写 `supports_vision: false`（对文本模型的逃生口）。
    #:
    #: 它是实例属性不是类属性（构造函数里赋值）—— 同一个 provider
    #: 目录下 `deepseek-v4-flash` 与某个纯文本模型是两个答案。
    #: 类上这个是兜底：替身 / 老 provider / 直接实例化的路都走它。
    supports_vision: bool = True

    #: 这个模型收不收音频。
    #: 默认 False，与 `supports_vision` 的方向相反 —— 理由在
    #: :func:`accepts_audio`：能收音频的聊天模型是少数派，发错会 400。
    #: 声明位置是 `config.yaml` 的 `model.supports_audio`；没有 manifest 层
    #: （粒度同 `supports_vision`：能力是逐模型的，manifest 是逐供应商的）。
    supports_audio: bool = False

    # ------------------------------------------------------------------
    # Abstract — must be implemented by every provider
    # ------------------------------------------------------------------

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: "str | list[str] | None" = None,
        thinking=None,
        temperature: "float | None" = None,
    ) -> ChatResponse:
        """把消息发给 LLM，返回结构化响应。

        参数：
            messages: {"role": ..., "content": ...} 字典的列表。
            tools: 可选的 OpenAI 格式工具定义。
            system: 系统提示词，以请求参数形式传递。由调用方每轮重建，
                从不存储、从不落库。它最终落在哪里由 provider 决定：
                OpenAI 兼容端点没有顶层 system 字段，所以
                :class:`OpenAICompatProvider` 把它重新插成第一条 wire 消息；
                Anthropic 有原生 `system` 字段。

                接受普通字符串或块列表。当前 provider 用
                `flatten_system` 把块拍平，但列表形状要为将来的缓存
                断点保留 —— 只接受 `str` 会在缓存落地那天被迫再改一次
                签名（见 `providers/base.py:flatten_system`）。

            thinking: 这一次请求的思考模式规格 —— 传的是
                `core.tiers.ThinkingSpec`（只有 `.enabled` / `.effort`
                两个属性被读，见 `core/tiers.py::ThinkingSpec`），
                `None` = 不改（provider 按自己的默认/配置走，
                所以"没接档位"的调用方 —— 摘要、标题、`side_call` —— 行为不变）。

                不 import `core.tiers`：provider 是下层，读的是鸭子类型
                的两个属性（方向反了会绕出循环 import）。
                它进提示缓存键（思考配置是 API 缓存键的一部分，
                偏离主线程的配置会击穿提示缓存），调用方不许每请求重算，
                只能取会话上冻结的那个档。

                刻意做成可选关键字参数。摘要、事实抽取、会话笔记、
                压缩器自己的摘要调用、计划器等一次性内部调用会各自构造
                一条带独立 system 消息的短列表；把它们变成必填参数会全部打断。

        返回：
            带 content、tool_calls 与 token 数的 ChatResponse。

        说明：
            消息列表里可以合法地出现不是系统提示词的 `role="system"`
            条目 —— 最重要的是压缩摘要（`kind="history_summary"`），
            它是会话状态、必须能跨重启存活。所以 provider 要合并两个来源：
            先放显式的 `system` 参数，再放列表内的 system 内容。
        """
        ...

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: "str | list[str] | None" = None,
        thinking=None,
        temperature: "float | None" = None,
    ) -> AsyncIterator[StreamChunk]:
        """把聊天响应以 StreamChunk 异步生成器的形式流式返回。

        默认实现包装 `chat()` 并只产出一片 —— provider 应当用原生
        流式实现覆盖它。
        """
        # Default: wrap sync chat()
        response = self.chat(messages, tools, system=system, thinking=thinking)
        yield StreamChunk(
            content=response.content,
            finish_reason="stop",
            usage={
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
            },
        )

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        """用本 provider 的 tokenizer（或足够接近的近似）估算 *text* 的 token 数。

        用于预检（压缩阈值、预算）。
        """
        ...

    @property
    @abstractmethod
    def context_window(self) -> int:
        """上下文窗口大小上限，单位 token（例如 DeepSeek 的 65536）。"""
        ...

    # ------------------------------------------------------------------
    # Optional — override for additional capabilities
    # ------------------------------------------------------------------

    async def test_connection(self) -> dict:
        """发一个最小 ping，验证 API 可达、key 有效。
        返回包含 `ok`（bool）与 `message`（str）的字典。

        默认做法：发一条单消息聊天并检查是否有有效响应。
        """
        try:
            resp = self.chat(
                [{"role": "user", "content": "ping"}],
                tools=None,
            )
            return {"ok": True, "message": "API reachable"}
        except Exception as e:
            return {"ok": False, "message": f"Connection failed: {e}"}

    # ------------------------------------------------------------------
    # Concrete helpers
    # ------------------------------------------------------------------

    def resolve_temperature(self, temperature) -> float:
        """按次覆盖采样温度；`None`，用实例默认。

        判据与接 `thinking=` 那条逐字同款：
        `None` = 逐字节等于现状，所有既有调用点（摘要 / 标题 / 压缩 /
        `/btw`）一行都不用改，行为一个字节不变。
        非法值（如 `"abc"`），落回实例默认，
        而不是把它塞到 wire 上让上游 400。
        一处定义、所有 provider 共用（两处各写一份必然漂移）。

        不要在抽象方法那一节下面插普通方法：`@abstractmethod`
        作用于它下面的那一个函数；插错位置会让普通方法变成抽象方法，
        同时让后面的抽象方法丢掉标记，所有 provider 都实例化不了
        （`Can't instantiate abstract class … without an implementation for
        abstract method 'resolve_temperature'`）。
        """
        if temperature is None:
            return self._temperature
        try:
            return float(temperature)
        except (TypeError, ValueError):
            return self._temperature

    def estimate_request_tokens(
        self,
        messages: list[dict],
        system: "str | list[str] | None" = None,
        tools: list[dict] | None = None,
    ) -> dict:
        """预检估算 —— 整条请求的成本，含三个部分与因子。

        返回 {"messages", "system", "tools", "raw", "factor", "total"}。

        一个仪器，一个因子，一处应用。同一个
        「这条请求要花多少 token」只允许在这里加总；分别计算会给出
        互不相同的值，而它们本该是同一个数。代价长这样：
        [calibrator] est=1881 actual=6292 —— 单次偏差 3.3 倍，因子被推到
        1.5466 并落盘，反过来污染之后所有 estimate（含压缩闸门）。
        缺口几乎全部来自 tools（约 4,777）。

        参数：
            messages: 对话轮次。
            system: 本次请求的系统提示词（如果有）。
            tools: 实际要发出去的工具 schema，形状与 `chat()` 收到的
                一致。传 *schema*（或者不传），**绝不传一个预先量好的
                token 数**：在别处量出来的计数是第二台仪器，正是这个签名
                要防的 bug。

        请求里有 `system` / `tools` 就必须传进来。
        `usage.prompt_tokens` 数的是 wire 上的一切；漏掉一部分的估算
        与它不可比，而且这个缺口的后果不会留在本地 —— 校准器会把比率
        持久化到 `data/runtime/calibration.json`，跨重启乘进之后所有估算。
        （判据：预算两端必须同源。）
        """
        parts = {
            "messages": 0,
            "system": 0,
            "tools": 0,
        }
        for m in messages:
            content = m.get("content")
            # 带图那一轮的 `content` 是块列表 —— 必须走
            # `content_text`（全仓唯一一处转换），否则 `count_tokens` 收一个
            # list 会抛（`tiktoken.encode` 不认），而抛的位置在这台
            # "唯一仪器"里面，整轮死在估算上。
            # 不许在这里内联 `isinstance`：那样这个判断就有两份了，
            # 而漂掉的那一份的表现是"某些消息少算几个 token"（看不出来）。
            parts["messages"] += self.count_tokens(content_text(content))
            # 图片按张给额度，不按 base64 长度 —— 见 `IMAGE_TOKEN_ALLOWANCE`。
            parts["messages"] += (count_image_blocks(content)
                                  * IMAGE_TOKEN_ALLOWANCE)
            # 音频按时长给额度（读不到时长走保守常量）——
            #   否则一段语音在闸门眼里是一句话，压缩会来得太晚。
            parts["messages"] += audio_token_allowance(content)
            # 每条消息的粗略开销（role 标记、格式）
            parts["messages"] += 4
            # assistant 消息上不在 `content` 里、却真的在 wire 上的
            # 那两样（思考正文 + 工具参数全文）。见 `_reply_side_tokens`。
            parts["messages"] += _reply_side_tokens(m, self.count_tokens)

        system_text = flatten_system(system)
        if system_text:
            parts["system"] = self.count_tokens(system_text) + 4

        if tools:
            parts["tools"] = self.count_tokens(
                json.dumps(tools, ensure_ascii=False)
            ) + 4

        raw = parts["messages"] + parts["system"] + parts["tools"]

        factor = 1.0
        if self._calibrator is not None:
            factor = self._calibrator.factor  # type: ignore[union-attr]

        return {
            **parts,
            "raw": raw,
            "factor": factor,
            "total": int(raw * factor) if factor != 1.0 else raw,
        }

    def estimate_message_tokens(
        self,
        messages: list[dict],
        system: "str | list[str] | None" = None,
        tools: list[dict] | None = None,
    ) -> int:
        """`estimate_request_tokens(...)["total"]` —— 只要总数时用它。

        保留这个既有调用方都认识的名字；它现在是薄包装，确保加总请求的
        地方只有一处。需要分项读数的调用方（压缩闸门的余量行、
        上下文用量展示）直接调 :meth:`estimate_request_tokens`。
        """
        return self.estimate_request_tokens(
            messages, system=system, tools=tools
        )["total"]
