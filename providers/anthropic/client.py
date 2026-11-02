"""AnthropicProvider —— 原生 Anthropic Messages API provider。

使用 `anthropic` Python SDK，以完整使用 Claude 的原生能力：
扩展思考、tool_choice、提示缓存。

OpenAI ↔ Anthropic 的消息格式翻译在
:meth:`_to_anthropic_messages` 中完成。
"""

import json
from typing import AsyncIterator, Optional

from core import window_estimate
#: tiktoken 只许经这一处（见 `core/tokenizer.py`：随包词表 + 说得清的失败）。
from core.tokenizer import get_encoding as _get_tiktoken_encoding
from providers.base import (
    BaseProvider,
    ChatResponse,
    StreamChunk,
    classify_api_error,
    classify_transport_error,
    flatten_system,
    read_cache_hit,
    record_token_usage,
)


def _session_key() -> str:
    """这一轮属于哪个会话（通道键）—— 重试横幅按它找会话。"""
    try:
        from core.request_snapshot import current_session_id
        return str(current_session_id() or "")
    except Exception:                                           # noqa: BLE001
        return ""


def _create_with_retry(client, kwargs):
    """把一次 `messages.create` 按统一节奏重试。

    节奏与 OpenAI 兼容那条同源（`core/llm_retry.step`）：连接 / 超时 / 5xx
    走常规 10 次，再转静默每 120 秒一次；余额 / 参数 / 认证 / 限流 /
    内容审核 / 请求过大一律不重试。
    """
    from core import llm_retry
    from core.llm_errors import headline
    rstate: dict = {}
    while True:
        try:
            resp = client.messages.create(**kwargs)
            llm_retry.clear(_session_key())
            return resp
        except Exception as e:                                  # noqa: BLE001
            kind = classify_api_error(e) or classify_transport_error(e) or ""
            action, delay = llm_retry.step(rstate, kind)
            if action in ("retry", "silent"):
                n = int(rstate.get("silent") or 0) if action == "silent" \
                    else int(rstate.get("retry") or 0)
                llm_retry.note(_session_key(), n, kind=kind,
                               text=headline(e, kind=kind), phase=action)
                print(f"[llm] {kind or 'unknown'} —— {delay}ms 后重试"
                      f"（{action} {n}）")
                #: 可打断 —— 用户按了打断就立刻把原异常抛回。
                if not llm_retry.wait(_session_key(), delay):
                    raise
                continue
            llm_retry.fail(_session_key(), kind=kind,
                           text=headline(e, kind=kind))
            raise
from core.token_calibrator import TokenCalibrator
from core.image_input import content_text


# OpenAI 形状的 `content` -> Anthropic 形状
#
# 这是两条线唯一的翻译处（`_to_anthropic_messages` 是 Anthropic 唯一的
# 消息投影，正如 `_wire_messages` 是 OpenAI 那条路唯一的）。
#
# | | 文本块 | 图片块 |
# |---|---|---|
# | OpenAI | `{"type":"text","text":…}` | `{"type":"image_url","image_url":{"url":"data:…"}}` |
# | Anthropic | 同左（逐字一样） | `{"type":"image","source":{"type":"base64","media_type":…,"data":…}}` |
#
# 文本块形状不动 —— 两家逐字相同，所以只有图片需要翻译。
# 认不出的块不静默丢（"不许静默改坏"）：换成一个
# 可见的文本块，写清它是什么形状。丢掉它会让模型以为"这条消息就这些"。

def _to_anthropic_content(content):
    """消息 `content` -> Anthropic 接受的形式。

    `str` / `None`，原样（`None` 变 `""`）。
    块列表，逐块翻译；文本块原样、图片块换形状、其余变成一句实话。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        # 既不是串也不是块列表 —— 交给 `content_text` 取它能取的部分，
        # 而不是把认不出的东西原样发上去（上游会 400，且报错看不出是我们的锅）
        return content_text(content)

    out: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            if part:
                out.append({"type": "text", "text": str(part)})
            continue
        kind = part.get("type")
        if kind == "text":
            out.append({"type": "text", "text": str(part.get("text") or "")})
        elif kind == "image_url":
            block = _image_block(part)
            if block is not None:
                out.append(block)
        else:
            print(f"[anthropic] 认不出的 content 块 type={kind!r} —— "
                  f"换成一个可见的说明（不静默丢）")
            out.append({"type": "text",
                        "text": f"[这块内容（{kind}）Anthropic 这条线不支持]"})
    # 空列表上游会 400（content 不能是空数组），补一个空文本块。
    return out or [{"type": "text", "text": ""}]


def _image_block(part: dict) -> Optional[dict]:
    """`image_url` part -> Anthropic 的 `image` block；解不开就返回 `None` 并出声。

    只认 `data:` URL：Anthropic 的 `source` 有 `base64` / `url` / `file`
    三种，而 `url` 那种要求它自己去取（等于给上游开了一条我们看不见的出网路，
    而那条出网闸是我们自己的）。远端图片不在这一层解决，
    让它走不进来（`core/image_input.py::prepare_*` 只产 `data:`）。
    """
    # 延迟 import：正常路径（`base64`）用不到它，而 `base64` 是标准库。
    import base64 as _b64

    url = (part.get("image_url") or {}).get("url") or ""
    if not url.startswith("data:"):
        print(f"[anthropic] 图片块不是一个 data: URL（{url[:40]!r}）—— "
              f"Anthropic 那条路只收内嵌的字节，这一块被丢掉了")
        return None
    head, _, payload = url.partition(",")
    media = head[5:].split(";")[0].strip().lower() or "image/png"
    if not payload:
        print("[anthropic] 图片块的 data: URL 里没有 payload —— 丢掉了")
        return None
    # 重编码成 base64 一次（`data:` 里本来就是 base64，但别假设 ——
    #   一个 `;base64,` 都没有的 data URL 是百分号编码的）。
    if "base64" in head[5:]:
        data = payload
    else:
        from urllib.parse import unquote_to_bytes
        data = _b64.b64encode(unquote_to_bytes(payload)).decode("ascii")
    return {"type": "image",
            "source": {"type": "base64", "media_type": media, "data": data}}


#: Anthropic 对 `budget_tokens` 的硬下限（上游会拒，不是我们能选的）：
#: 小于它 · 或者不小于 `max_tokens`，整个请求 400。`_thinking_kwargs`
#: 靠这两条把档位的预算夹进合法区间（并出声）。
_MIN_THINKING_BUDGET = 1024


def _image_block_media(block) -> list:
    """Anthropic 的 `image` 内容块 → `media` 列表。同步与流式共用这一处。

    只收 `source.type == "base64"`；`url` 来源不下载（出网闸）。
    """
    src = getattr(block, "source", None)
    if getattr(src, "type", "") == "base64" and getattr(src, "data", ""):
        return [{
            "data_url": "data:%s;base64,%s" % (
                getattr(src, "media_type", "image/png"), src.data),
            "name": "image",
        }]
    print("[media] 忽略非 base64 的 image 块"
          "（url 来源不下载，绕开出网闸）")
    return []


def map_stream_event(event) -> list[StreamChunk]:
    """把一个 Anthropic 流事件映射成 0..1 个 `StreamChunk`（纯函数，可测）。

    抽出来的理由不是"整洁"，是要有机械读者：
    本机没装 anthropic SDK（`AnthropicProvider.__init__` 会直接抛
    "anthropic SDK required"），而"工具的**身份与名字只在 `content_block_start`
    里"这条判据必须**有东西钉住 —— 否则它只能靠"读一遍代码觉得对"。
    现在 `tests/test_stream_accumulator.py` 直接喂事件进来验它（不需要 SDK）。

    必须处理 `content_block_start`：不接它，Anthropic 走流式时
    `name` 永远是空，累积器得到 `unknown tool ''`。
    """
    et = getattr(event, "type", None)

    if et == "content_block_start":
        block = getattr(event, "content_block", None)
        btype = getattr(block, "type", None)
        # 媒体块：与同步那条同一个收法（`_image_block_media`），
        #   只在 `content_block_start` 里有 —— 图片不走增量分片。
        if btype == "image":
            media = _image_block_media(block)
            return [StreamChunk(media=media)] if media else []
        if btype != "tool_use":
            return []
        return [StreamChunk(
            tool_call={
                "id": getattr(block, "id", None),
                "type": "function",
                # 名字就在这里，且只有这里有（参数增量分片不带它）
                "function": {"name": getattr(block, "name", None), "arguments": ""},
            },
            # 同一个槽位号贯穿 start 与后续的 json 增量
            tool_call_index=getattr(event, "index", None),
        )]

    if et == "content_block_delta":
        delta = getattr(event, "delta", None)
        dt = getattr(delta, "type", None)
        if dt == "text_delta":
            return [StreamChunk(content=getattr(delta, "text", None))]
        if dt == "input_json_delta":
            # 只有参数增量：身份/名字一律给 `None`（累积器的"首个非空胜出"
            # 不会让 None 把已知的值顶掉）
            return [StreamChunk(
                tool_call={
                    "id": None,
                    "type": "function",
                    "function": {"name": None,
                                 "arguments": getattr(delta, "partial_json", None)},
                },
                tool_call_index=getattr(event, "index", None),
            )]
        return []

    if et == "message_delta":
        return [StreamChunk(
            finish_reason=getattr(getattr(event, "delta", None), "stop_reason", None),
            usage={"output_tokens": getattr(
                getattr(event, "usage", None), "output_tokens", 0)},
        )]

    return []


class AnthropicProvider(BaseProvider):
    """Anthropic 原生 Messages API 的 provider。

    直接使用 `anthropic` SDK（不是 OpenAI 兼容代理），
    因此能使用 Claude 的原生能力：扩展思考与提示缓存。
    """

    _api_key: str
    _model: str
    _temperature: float
    _max_tokens: int
    _context_window: int
    _thinking_budget: int = 0  # 0 = 关闭

    #: 与其他 provider 使用同一个编码（`cl100k_base`），
    #: 让记忆层的测量在不同 provider 之间可比 —— 见下面的 count_tokens。
    _tokenizer_encoding: str = "cl100k_base"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        temperature: float = 0.3,
        max_tokens: int = 8192,
        context_window: int = 200000,
        thinking_budget: int = 0,
        base_url: str = "",
        # 必须显式声明：落在 `**kwargs` 里会被静默吞掉，来源就丢了
        window_source: str = "未知",
        # 价格表按 `(provider, model)` 查 —— 见
        # `providers/openai_compat.py` 的同名字段。必须显式声明：
        # 落进 `**kwargs` 会被静默吞掉（同 `window_source` 那条）。
        provider_name: str = "",
        # 看不看得见图。必须显式声明 ——
        # 落进 `**kwargs` 会被静默吞掉，那份声明就没了（同 `window_source`）。
        # 默认 `False` 的理由在 `providers/base.py::accepts_images`。
        supports_vision: bool = False,
        # Anthropic 线没有音频部件 —— 这个形参收下但永远置 False，
        #   并在显式声明 `true` 时出声（静默忽略一个写过的声明比拒绝更坏）。
        supports_audio: bool = False,
        **kwargs,  # 接受统一装载器传来的 rpm/tpm/api_keys
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._provider_name = provider_name
        self.supports_vision = bool(supports_vision)
        # 这条路没有音频部件 —— 永远 False，声明了也要出声。
        if supports_audio:
            print("[anthropic] config 声明了 supports_audio，但这条线没有音频部件 "
                  "—— 仍按不支持处理（语音会以可见说明的形式交给模型）")
        self.supports_audio = False
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._context_window = context_window
        #: 这个窗口是谁给的（见 `providers/openai_compat.py` 的同名字段）
        self._window_source = window_source
        self._window_claim_warned = False
        self._thinking_budget = thinking_budget
        #: 档位取代配置那次只说一遍（每请求刷屏没有信息量）
        self._tier_supersede_warned = False
        #: 与 `openai_compat` 同一个入口（见 `core/tokenizer.py`）：
        #:   安装版用随包词表，断网也构造得出来。
        self._tokenizer = _get_tiktoken_encoding(self._tokenizer_encoding)

        try:
            from anthropic import Anthropic
        except ImportError:
            raise ImportError(
                "anthropic SDK required. Install: pip install anthropic"
            )

        # `http_client=` 必须给：不给的话 SDK 造的
        #    `httpx.Client` 是 `trust_env=True`，会静默采纳环境变量里的
        #    `ALL_PROXY` —— 一条没在听的本地代理能把整轮对话卡死。
        from core import net_proxy

        #: `url=` 让指向本机 / 内网中转站的自定义 `base_url` 直连
        #:   （判据在 `core/net_proxy.is_local_target`，与设置页探针共用）。
        _http = net_proxy.httpx_client(url=base_url or None)
        # Anthropic SDK doesn't support custom base_url in the same way,
        # but we support it for proxies
        if base_url:
            self._client = Anthropic(
                api_key=api_key,
                base_url=base_url,
                http_client=_http,
            )
        else:
            self._client = Anthropic(api_key=api_key, http_client=_http)

        # v1.1.0: Self-calibrating token estimator
        cal_name = model or "claude"
        self._calibrator = TokenCalibrator(cal_name)
        if self._calibrator.load():
            print(f"[calibrator] Loaded {cal_name}: "
                  f"factor={self._calibrator.factor:.4f}, "
                  f"samples={self._calibrator.samples}")
        else:
            print(f"[calibrator] Fresh start for {cal_name} "
                  f"— will auto-calibrate after ~3 turns")

    # ------------------------------------------------------------------
    # 窗口声明的矛盾检测
    # ------------------------------------------------------------------

    def _check_window_claim(self, prompt_tokens: int) -> None:
        """上游报的输入量超过声明的窗口，声明值一定是错的，吼一声。

        与 `providers/openai_compat.py` 的同名方法同款（那边有完整注释：
        没有别的探测手段，这是唯一的免费探针；每个实例只吼一次）。

        这里同时是「演算」那一层的入口（`window_estimate.observe`）
        —— 它必须在 early-return 之前（下界与"超没超过声明"无关）。
        """
        window_estimate.observe(self._model, prompt_tokens)
        if self._window_claim_warned or prompt_tokens <= self._context_window:
            return
        self._window_claim_warned = True
        print(
            f"[window] prompt={prompt_tokens:,} 已超过声明窗口 "
            f"{self._context_window:,} —— 声明值错了"
            f"（来源={self._window_source}，model={self._model}）。"
            f"压缩闸门按声明值算（{int(self._context_window * 0.85):,}），"
            f"会永远够不着。"
        )

    # ------------------------------------------------------------------
    # BaseProvider interface
    # ------------------------------------------------------------------

    @property
    def context_window(self) -> int:
        return self._context_window

    @property
    def window_source(self) -> str:
        """这个窗口是谁给的 —— 没有别的探测手段，所以来源必须跟着值走。"""
        return getattr(self, "_window_source", "未知")

    def count_tokens(self, text: str) -> int:
        """用 tiktoken（`cl100k_base`）计数，与其他 provider 同一把尺。

        不要改用 SDK 的 token 计数：`client.count_tokens(text)`
        这个接口并不存在（SDK 提供的是
        `client.messages.count_tokens(messages=...)`），退回 len(text) // 3
        会得到约 0.33 tokens/字符，而中文约 0.53（1.9 字符/token）——
        低估约 37%。

        方向很重要：记忆层的预检估算走这个方法（经 `estimate_request_tokens`），
        再与非 tiktoken 计数的窗口比较。这个 provider 上出现第二种
        静默不同的单位，两端就会对不上（判据：预算两端必须同源）。

        共用 `cl100k_base` 编码，保证跨 provider 只有一把尺。
        """
        return len(self._tokenizer.encode(text))

    def _thinking_kwargs(self, thinking) -> dict:
        """这一次请求的 `thinking` 参数 —— 一处定义、两条路共用。

        `chat` 与 `chat_stream` 必须走同一段逻辑 —— 只改一条是看不见的，
        要等有人把流式打开才发现。

        四条（每条都有理由或代价）：
        ① `thinking is None`，这一格不参与，走构造函数那个
           `thinking_budget`（= 既有行为，逐字不变）；
        ② 档位说 开，发 `adaptive`（见下）；
        ③ 档位说 关，不发；配置里设过值的话打一行
           （静默忽略一个改过的键比不许改更坏）；
        ④ Anthropic 的硬约束（上游会拒，不是我们能选的）：
           `budget_tokens >= 1024` 且 `budget_tokens < max_tokens`。
           越界就夹到合法区间并出声 —— 不发是白丢这一档的能力，
           硬发是整个请求 400（那一轮直接没了）。
        """
        if thinking is None:
            # 辅助调用（摘要 / 标题 / 压缩）：逐字保留既有行为 ——
            #   配置里没设预算就是什么都不发。
            budget = self._thinking_budget
        elif getattr(thinking, "enabled", False):
            # 档位说开，发 `adaptive`，不再发预算。
            #
            #   理由有三条，都不是口味：
            #   ① `budget_tokens` 这一格已经删除 —— 可 Anthropic 的
            #      `enabled` 形状必须带一个预算，没有预算就只能二选一：
            #      编一个，或者用 `adaptive`；
            #   ② `adaptive` = 模型自己决定想多久，档位只改一个字符串；
            #   ③ 编一个预算就是第二根随档的旋钮，而"手段随档增长"没有先例。
            #   这一格未在真实 Anthropic 端点上验证过
            #      （本机没配 anthropic 的 key）。枚举本身经过探针验证
            #      （`bogus` 会报错，上游硬校验
            #      `{adaptive, enabled, disabled}`）。
            return {"thinking": {"type": "adaptive"}}
        else:
            budget = 0
            if self._thinking_budget > 0 and not self._tier_supersede_warned:
                self._tier_supersede_warned = True
                print(f"[thinking] 投入档这一档是「不思考」—— config.yaml 的 "
                      f"anthropic.thinking_budget={self._thinking_budget} 被它取代"
                      f"（档位是唯一来源）")
        if budget <= 0:
            return {}
        if budget < _MIN_THINKING_BUDGET:
            print(f"[thinking] 预算 {budget} 低于 Anthropic 的下限 "
                  f"{_MIN_THINKING_BUDGET} —— 按 {_MIN_THINKING_BUDGET} 发")
            budget = _MIN_THINKING_BUDGET
        if budget >= self._max_tokens:
            if self._max_tokens <= _MIN_THINKING_BUDGET:
                print(f"[thinking] max_tokens={self._max_tokens} 容不下思考预算 "
                      f"（要 >{_MIN_THINKING_BUDGET}）—— 这一次**不带思考**"
                      f"（硬发会被上游 400，整轮都没了）")
                return {}
            print(f"[thinking] 预算 {budget} >= max_tokens {self._max_tokens} "
                  f"—— 夹到 {self._max_tokens - 1}（上游硬约束）")
            budget = self._max_tokens - 1
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: "str | list[str] | None" = None,
        thinking=None,
        temperature: "float | None" = None,
    ) -> ChatResponse:
        """向 Claude 发一条同步消息。"""
        system_prompt, api_messages = self._to_anthropic_messages(
            messages, system=system
        )
        api_tools = self._to_anthropic_tools(tools)

        # 给校准器做预检估算 —— 计入 `system` 与 `tools`，
        # 与 API 的 usage.input_tokens 对齐（见
        # BaseProvider.estimate_request_tokens）。
        #
        # `tools` 传的是 OpenAI 形状，不是投影后的 `api_tools`：
        # 压缩闸门量的是注册表里的 schema，校准器必须与闸门用同一台仪器，
        # 否则因子拟合的量与它要修正的量不是同一个。
        # 这条 wire 上形状差异只是几个 key 名的 token。
        estimated_input = self.estimate_message_tokens(
            messages, system=system, tools=tools
        )

        kwargs: dict = {
            "model": self._model,
            "messages": api_messages,
            "max_tokens": self._max_tokens,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if api_tools:
            kwargs["tools"] = api_tools
        kwargs.update(self._thinking_kwargs(thinking))
        # 按次覆盖：`None` 时一个字都不加 ——
        # 本 client 默认不发 temperature。
        # 判据同接 `thinking=` 那条：`None` = 逐字节等于现状。
        #它只在调用点显式传的时候才改变 wire，
        # 不顺手改掉所有 anthropic 调用的采样温度。
        if temperature is not None:
            kwargs["temperature"] = self.resolve_temperature(temperature)

        response = _create_with_retry(self._client, kwargs)

        # Extract text and tool calls
        content = ""
        tool_calls = []
        #: assistant 内容里的 image 块（base64）。
        #: 只收 `source.type == "base64"`；`url` 来源不下载（出网闸）。
        media: list = []

        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input),
                    },
                })
            elif block.type == "image":
                # 与流式那条同一个收法（`_image_block_media`）：
                #   两处各写一份，迟早一处改了、另一处漏（`A11`）。
                media.extend(_image_block_media(block))

        it = response.usage.input_tokens
        ot = response.usage.output_tokens
        # 缓存命中：Anthropic 走 `cache_read_input_tokens`，
        # 同一个 reader 认三种字段名，这里不用特判。
        cache_hit = read_cache_hit(response.usage)
        record_token_usage(it, ot, cache_hit=cache_hit,
                           provider=self._provider_name, model=self._model)

        # 声明窗口与实际请求的硬矛盾检测（与 openai_compat 同款）
        self._check_window_claim(it)

        # 把上游的真实 token 数喂给校准器
        if self._calibrator is not None and it > 0:
            prev_factor = self._calibrator.factor
            ratio = self._calibrator.feed(estimated=estimated_input, actual=it)
            self._calibrator.save()
            if self._calibrator.factor != prev_factor:
                print(
                    f"[calibrator] Updated: "
                    f"est={estimated_input} actual={it} "
                    f"ratio={ratio:.2f} "
                    f"factor={self._calibrator.factor:.4f} "
                    f"({self._calibrator.samples} samples)"
                )

        return ChatResponse(
            content=content or None,
            tool_calls=tool_calls if tool_calls else None,
            prompt_tokens=it,
            completion_tokens=ot,
            cache_hit_tokens=cache_hit,
            # 这一轮 assistant 直接产出的图（没有就是 `None`）。
            media=media or None,
        )

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: "str | list[str] | None" = None,
        thinking=None,
        temperature: "float | None" = None,
    ) -> AsyncIterator[StreamChunk]:
        """流式接收 Claude 的回复。"""
        system_prompt, api_messages = self._to_anthropic_messages(
            messages, system=system
        )
        api_tools = self._to_anthropic_tools(tools)

        kwargs: dict = {
            "model": self._model,
            "messages": api_messages,
            "max_tokens": self._max_tokens,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if api_tools:
            kwargs["tools"] = api_tools
        # 流式这条路也必须接思考参数 —— 只改 `chat` 一条是看不见的。
        kwargs.update(self._thinking_kwargs(thinking))
        # 按次覆盖：`None` 时一个字都不加 ——
        # 本 client 默认不发 temperature。
        # 判据同接 `thinking=` 那条：`None` = 逐字节等于现状。
        #它只在调用点显式传的时候才改变 wire，
        # 不顺手改掉所有 anthropic 调用的采样温度。
        if temperature is not None:
            kwargs["temperature"] = self.resolve_temperature(temperature)

        #: 连接 / 超时按固定节奏重试
        #:   （与 OpenAI 兼容那条同一条判据）。已经交出去的内容不能重来
        #:   —— 那会让用户看到两遍（`emitted`）。
        from core import llm_retry
        from core.llm_errors import headline
        rstate: dict = {}
        while True:
            emitted = False
            try:
                with self._client.messages.stream(**kwargs) as stream:
                    for event in stream:
                        for _chunk in map_stream_event(event):
                            emitted = True
                            yield _chunk
                llm_retry.clear(_session_key())
                return
            except Exception as e:
                transport = classify_transport_error(e)
                kind = classify_api_error(e) or transport or ""
                action, delay = (("no_retry", 0) if emitted
                                 else llm_retry.step(rstate, kind))
                if action in ("retry", "silent"):
                    n = int(rstate.get("silent") or 0) if action == "silent" \
                        else int(rstate.get("retry") or 0)
                    llm_retry.note(_session_key(), n, kind=kind,
                                   text=headline(e, kind=kind), phase=action)
                    print(f"[llm] {kind or 'unknown'} —— {delay}ms 后重试"
                          f"（{action} {n}）")
                    #: 可打断（同 OpenAI 兼容那条）。
                    if not await llm_retry.wait_async(_session_key(), delay):
                        llm_retry.clear(_session_key())
                        yield StreamChunk(content="")
                        return
                    continue
                #: 与 OpenAI 兼容那条同一条诊断口径 —— 错误类别跟着 chunk 走
                #: （用户面 `core/llm_errors.explain` 要用），并且出声。
                print(f"[llm] 流式失败（{type(e).__name__}: {str(e)[:200]}）"
                      f" —— model={self._model}")
                llm_retry.fail(_session_key(), kind=kind,
                               text=headline(e, kind=kind))
                yield StreamChunk(
                    error=str(e),
                    error_kind=kind)
                return

    async def test_connection(self) -> dict:
        """ping 一次 Claude，验证 API key。"""
        try:
            resp = self._client.messages.create(
                model=self._model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return {"ok": True, "message": f"Claude reachable (model={self._model})"}
        except Exception as e:
            return {"ok": False, "message": f"Connection failed: {e}"}

    # ------------------------------------------------------------------
    # Message format translation
    # ------------------------------------------------------------------

    # (helper `_to_anthropic_content` lives at module level — see below)

    def _to_anthropic_messages(
        self, messages: list[dict], system: "str | list[str] | None" = None
    ) -> tuple[str, list[dict]]:
        """把 OpenAI 格式的消息转成 Anthropic 格式。

        Anthropic 有原生顶层 `system` 字段，所以这个类是系统提示词
        唯一不必伪装成消息的地方。`system` 参数以 `kwargs["system"]`
        发出，别的都不走那里。

        **列表内的 `role="system"` 消息原地降级为 `user`，
        不被提升到顶部。理由：

        * 提升会把历史挪到指令之上。恢复历史的标记是位置相关的：
          `<history_start>` … 轮次 … `<history_end>` 只有在原地才有意义。
          拼成一个字符串放到顶部，模型会把一段旧对话当成当前指令 ——
          "历史伪装成指令"那类故障只是换到了 provider 里。
        * 降级为 `user` 是唯一遇到同样约束时的做法：只有一个 system 槽位，
          没占上槽位的 system 消息折进 user 消息以保持顺序。
        * Anthropic Messages API 会在服务端合并相邻的同角色轮次，
          所以把标记折进相邻的 `user` 轮是安全的。

        `system` 接受普通字符串或块列表；块用 :func:`flatten_system`
        拍平（这里还没有 Anthropic 侧的缓存断点支持 —— 将来有时就在这里长，
        这也是参数不标注成 `str` 的原因）。

        返回：
            (system_text: str, api_messages: list[dict])
        """
        system_text = flatten_system(system)
        api_messages = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content")
            # 带图的 user 轮是块列表，而
            # Anthropic 的块形状与 OpenAI 不同：
            #   OpenAI    {"type": "image_url", "image_url": {"url": "data:…"}}
            #   Anthropic {"type": "image", "source": {"type": "base64",
            #                                          "media_type": …, "data": …}}
            # 不翻译就直接透传，上游 400，而报错说的是
            # "content.0.type: Input tag 'image_url' …"，看不出是我们的锅。
            # 翻译只在这一处（本函数是 Anthropic 唯一的消息投影），
            # 与 `_wire_messages` 在 OpenAI 那条路上是同一条判据。
            content = _to_anthropic_content(content)

            if role == "system":
                api_messages.append({"role": "user", "content": content})
                continue

            if role == "user":
                api_messages.append({"role": "user", "content": content})
            elif role == "assistant":
                entry: dict = {"role": "assistant", "content": content}
                # Handle tool_calls if present
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    tool_blocks = []
                    for tc in tool_calls:
                        name = tc["function"]["name"]
                        # 历史里那条 `tool_calls` 是模型的原样输出，
                        # 坏 JSON（以及流式无参调用的空串）会一路留在会话里。
                        # 直接 `json.loads`，一旦撞上，**整个请求崩在
                        # provider 内部**：上游看不到，我们只看到一句
                        # JSONDecodeError，从它反推"是历史坏了"要绕一大圈。
                        #判据是"历史必须始终可重发"：坏的就按无参回填并出声。
                        # 不改历史 —— 修在消费端（一处），不在 N 个生产端。
                        raw_args = tc["function"]["arguments"]
                        try:
                            args = json.loads(raw_args)
                        except (json.JSONDecodeError, TypeError) as exc:
                            args = {}
                            print(f"[anthropic] 历史里 '{name}' 的 arguments 不是合法 "
                                  f"JSON（{type(exc).__name__}: {exc}）—— 按无参回填"
                                  f"（历史本身不动）")
                        tool_blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": name,
                            "input": args,
                        })
                    entry["content"] = tool_blocks
                api_messages.append(entry)
            elif role == "tool":
                # Anthropic: tool results go in a user message
                tc_id = msg.get("tool_call_id", "")
                api_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tc_id,
                            "content": content,
                        }
                    ],
                })

        return (system_text, api_messages)

    def _to_anthropic_tools(
        self, tools: list[dict] | None
    ) -> list[dict] | None:
        """把 OpenAI 格式的工具定义转成 Anthropic 格式。"""
        if not tools:
            return None

        result = []
        for tool in tools:
            fn = tool.get("function", {})
            result.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {}),
            })
        return result
