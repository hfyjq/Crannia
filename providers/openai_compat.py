"""OpenAI 兼容 provider 基类。

DeepSeek、GLM、Qwen、Moonshot 等大量供应商使用 OpenAI chat-completions
API 格式。本模块提供共用实现 —— 具体 provider 只需提供端点、模型名与
tokenizer 配置。

提供 `chat()` / `chat_stream()` 两条请求路径、`KeyRotator` 多 key 轮换，
以及 `RateLimiter` 接入。
"""

import time

from typing import AsyncIterator, Optional

import httpx
from openai import OpenAI

from core import llm_retry, net_proxy, window_estimate
from core.llm_errors import headline
#: tiktoken 只许经这一处（见 `core/tokenizer.py`：随包词表 + 说得清的失败）。
from core.tokenizer import get_encoding as _get_tiktoken_encoding
from providers.base import (
    BaseProvider,
    ChatResponse,
    StreamChunk,
    classify_api_error,      # 错误分类只有一处（两条请求路径共用）
    classify_transport_error,  # 连接 / 超时（与上面三类正交）
    flatten_system,
    image_hint_for_failure,  # 带了图又报错时的上下文提示（不猜错误串）
    log_context_overflow,
    read_cache_hit,
    record_token_usage,
)
from providers.key_rotation import KeyRotator
from providers.rate_limiter import RateLimiter
from core.token_calibrator import TokenCalibrator


def _session_key() -> str:
    """这一轮属于哪个会话（通道键）—— 重试横幅按它找会话。

    取不到就空串：辅助调用 / 测试直驱没有快照，`llm_retry` **不记状态、
      不发事件**，但重试本身照常（重试与界面无关）。
    """
    try:
        from core.request_snapshot import current_session_id
        return str(current_session_id() or "")
    except Exception:                                           # noqa: BLE001
        return ""


#: 不要 key 的端点（`ollama` / LM Studio 那类本地服务）要发的占位串。
#:
#: `OpenAI` SDK 拒收空 key（`OpenAIError: Missing credentials…`），本地
#: 那条根本到不了发请求那一步，而报错看着像"用户填错了"。
#:
#: 判据：本地模型不是新机制，就是一个指向本机、不要 key 的 OpenAI
#:   兼容端点。
#:
#: 发个占位串不会让不该通的通：真需要 key 的上游会回它自己的 401 ——
#:   而那正是更有用的那个错（「简易联通测试」报的就是它）。
#: 它只影响客户端构造，不进任何日志、不进历史、不落盘。
_KEYLESS_PLACEHOLDER = "not-needed"

#: 思考模式下，历史里的 assistant 消息缺 `reasoning_content` 时补的占位文本。
#:
#: DeepSeek 要求回传思考内容本身（空串是否被接受未证实，占位更稳）；
#: Kimi 只要字段存在（那里补空串，见 `_reasoning_fill_value`）。
#: 只在 wire 边界补，不写回 `messages` —— 那段历史要落库，也是缓存前缀。
REASONING_PASSBACK_PLACEHOLDER = (
    "[previous turn's thinking content was not retained by the client]")


# 上游直接产出的媒体（生图 / 生音）
#
# 判据两条：
#   ① 只收内联 base64 / data URL —— `http(s)` 外链不下载
#      （下载它等于绕开出网闸；要的话让模型自己 `web_fetch`）；
#   ② 读不到就是没有 —— 不猜、不把"这家没告诉我"当成"它不会生图"。
#
# SDK 返回的是 pydantic 对象而不是 dict（有些兼容网关又返回裸 dict），
#    `_fld()` 两边都认。别对形状做假设（兼容网关的形状并不统一）。

#: 各家对音频格式的叫法 → mime。认不出就交给 `artifacts` 按字节嗅探
#: （魔数是证据，这里的表只是"它自称是什么"）。
_AUDIO_MIME = {
    "mp3": "audio/mpeg", "mpeg": "audio/mpeg",
    "wav": "audio/wav", "wave": "audio/wav",
    "opus": "audio/ogg", "ogg": "audio/ogg",
    "aac": "audio/mp4", "m4a": "audio/mp4",
}


def _fld(obj, key, default=None):
    """从 dict 或 pydantic 对象上取一个字段（兼容网关两种都见过）。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _image_data_url(item) -> str:
    """一条 `images[]` / 内容块 → data URL；不是内联的，空串。"""
    raw = _fld(item, "image_url")
    url = _fld(raw, "url") if raw is not None and raw is not item else _fld(item, "url")
    if isinstance(item, str):
        url = item
    if isinstance(raw, str):
        url = raw
    if isinstance(url, str) and url:
        if url.startswith("data:"):
            return url
        print("[media] 忽略外链图片（不下载，绕开出网闸）：%s" % url[:80])
        return ""
    b64 = _fld(item, "b64_json")
    if isinstance(b64, str) and b64:
        return "data:image/png;base64," + b64
    return ""


def _audio_data_url(audio, fmt: str = "") -> str:
    """一条音频对象 → data URL。`audio` 可能本身就是 data URL。"""
    if isinstance(audio, str):
        return audio if audio.startswith("data:") else ""
    data = _fld(audio, "data")
    if not isinstance(data, str) or not data:
        return ""
    if data.startswith("data:"):
        return data
    name = str(_fld(audio, "format") or fmt or "wav").lower()
    mime = _AUDIO_MIME.get(name, "audio/wav")
    return "data:%s;base64,%s" % (mime, data)


def _extract_media(msg) -> list | None:
    """从一条 assistant 消息里收媒体；没有，`None`（空 ≠ 没有）。"""
    out: list = []

    # ① 非标准但被多家兼容网关采用的 `message.images[]`
    for item in (_fld(msg, "images") or []):
        url = _image_data_url(item)
        if url:
            out.append({"data_url": url, "name": "image.png"})

    # ② 有的网关把它塞在 `message.audio` 里
    audio = _fld(msg, "audio")
    if audio is not None:
        url = _audio_data_url(audio)
        if url:
            out.append({"data_url": url, "name": "audio"})

    # ③ content 是块列表时（有的生图端点会这么回）
    content = _fld(msg, "content")
    if isinstance(content, (list, tuple)):
        for part in content:
            t = str(_fld(part, "type") or "")
            if t in ("image_url", "image", "output_image"):
                url = _image_data_url(part)
                if url:
                    out.append({"data_url": url, "name": "image.png"})
            elif t in ("audio", "output_audio"):
                url = _audio_data_url(_fld(part, "audio", part))
                if url:
                    out.append({"data_url": url, "name": "audio"})

    return out or None


class OpenAICompatProvider(BaseProvider):
    """任何暴露 OpenAI 兼容 `/v1/chat/completions` 端点的 LLM 的 provider。

    新增 provider 时继承本类并覆盖类级常量。

    接受 api_keys: list[str] 做多 key 轮换；接入 `RateLimiter`；
    原生实现 `chat_stream()`。
    """

    # ------------------------------------------------------------------
    # Subclass overrides
    # ------------------------------------------------------------------

    # These MUST be set by subclasses (or via __init__ kwargs).
    _api_key: str
    _base_url: str
    _model: str
    _temperature: float
    _max_tokens: int
    _context_window: int
    _tokenizer_encoding: str = "cl100k_base"
    """tiktoken 编码名。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.3,
        # 默认值 = 兜底（能力表里查不到时发多少）。
        #   真正的值由 `core/window_estimate.resolve_max_tokens` 现算，
        #   这里只是「没人传」时的最后一层。
        max_tokens: int = 8_192,
        context_window: int = 65536,
        window_source: str = "未知",
        api_keys: Optional[list[str]] = None,
        # 默认 0 = 不启用客户端限流。工厂的 `if rate_limit_cfg:` 对空字典
        # 判假会跳过赋值，从而让这里的默认值生效，两端都必须是 0，
        # 只有显式配置正数才会建限流器。
        rpm: int = 0,
        tpm: int = 0,
        # 价格表是按 `(provider, model)` 查的 —— 光有 `model` 不够
        # （`claude-sonnet-4-5` 在 anthropic 与 azure 下是两个价）。
        # 装载器给的是目录名（= 我们自己的 provider 键，也是价格表的键）。
        provider_name: str = "",
        # 这个模型看不看得见图 —— 默认 `False`
        # （fail-closed，理由在 `providers/base.py::accepts_images`）。
        # 与 `window_source` 同款：必须显式声明这个形参，
        # 落进 `**kwargs` 会被静默吞掉，那份声明就没了。
        supports_vision: bool = False,
        # 收不收音频 —— 默认 `False`（方向与 vision 相反，理由在
        #   `providers/base.py::accepts_audio`）。
        #   必须显式声明这个形参：落进 `**kwargs` 会被静默吞掉（同 vision）。
        supports_audio: bool = False,
    ) -> None:
        # Primary API key (backward compat)
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._provider_name = provider_name
        # 能力声明跟着实例走 —— 同一个 provider 目录下
        # `deepseek-v4-flash` 与 `…-vision-exp` 是两个答案。
        self.supports_vision = bool(supports_vision)
        self.supports_audio = bool(supports_audio)
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._context_window = context_window
        #: 这个窗口是谁给的（`声明(config.yaml)` / `声明(manifest 兜底)` /
        #: `保守兜底(未声明)`）。没有别的探测手段 —— 所以来源必须跟着值走，
        #: 否则声明错了没有任何地方看得出来。
        self._window_source = window_source
        #: 矛盾检测只吼一次（每轮都吼会把日志淹掉）；见 `_check_window_claim`
        self._window_claim_warned = False

        # --- Key 轮换 ---
        self._rotator: Optional[KeyRotator] = None
        if api_keys and len(api_keys) > 1:
            self._rotator = KeyRotator(api_keys, cooldown_seconds=60.0)
        elif api_key and not api_keys:
            self._rotator = KeyRotator([api_key], cooldown_seconds=0.0)
        elif api_keys and len(api_keys) == 1:
            self._rotator = KeyRotator(api_keys, cooldown_seconds=0.0)
        elif api_keys:
            self._rotator = KeyRotator(api_keys, cooldown_seconds=60.0)

        # --- 客户端限流 ---
        self._rate_limiter: Optional[RateLimiter] = None
        if rpm > 0 and tpm > 0:
            self._rate_limiter = RateLimiter(rpm=rpm, tpm=tpm)

        # 用初始 key 造客户端
        # 构造只走 `_build_client` 一处 —— 全仓 `OpenAI(` 这个字面量
        # 只许出现一次（有机械读者钉着）。
        self._client = self._build_client(self._current_key)
        #: 这个 client 是用哪个 key 建的 —— `_ensure_client` 的判据。
        #: 它必须与 `_client` 一起更新，否则判据会与事实分叉
        #: （来源必须跟着值走）。
        self._client_key: str = self._current_key
        #: 这个 client 是在什么代理配置下建的。
        #:   设置页把三档一改，旧客户端里烘着的代理就过期了，必须一起比
        #:   （切成「直连」之后仍然连不上，就是因为它没重建）。
        self._client_proxy: tuple = net_proxy.fingerprint()
        #: 走唯一入口（`core/tokenizer.py`）：它会给 `TIKTOKEN_CACHE_DIR`
        #:   设默认值，让安装版用随包那份词表，不必第一次启动就联网。
        #:   别改回 `tiktoken.get_encoding` —— 那样装出来的包在断网机器上
        #:   会在构造 provider 这一步抛。
        self._tokenizer = _get_tiktoken_encoding(self._tokenizer_encoding)

        # 自校准 token 估算器
        # 从模型名推出一个稳定的 provider 名（如 "deepseek-v4-flash"）
        cal_name = model or base_url.rstrip("/").split("/")[-1] or "unknown"
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

        为什么这是免费的探针：`usage.prompt_tokens` 是上游报的真实输入量，
        它已经在这里了（喂校准器用的就是它）。没有任何其他探测窗口的手段
        （无 `/models`、无 model→window 表、无 tiktoken 的 model_max_length），
        所以"我们声明的窗口比真实的小"这件事，只有这一处能自己暴露出来。

        后果为什么要吼：闸门算的是 `声明的窗口 × 0.85`。声明偏小，压缩触发得
        太早（白白丢上下文）；声明偏大，一路跑到上游报 400 才停。两种都不出声。

        每个 provider 实例只吼一次（每轮都吼会把日志淹掉，反而没人看）。
        代价：这个数继续涨也不会再报；要一直看着就把这行改成带阈值递增的。

        这一处同时是「演算」那一层的入口 ——「我们成功发出过 N 个
        token」是一条下界，与"超没超过声明"无关，`observe` 必须在下面
        那个 early-return 之前。吸收的时机是下一次解析窗口时
        （provider 重建 / 重启）—— 不在运行期偷移阈值（那会让压缩、
        上下文显示等多个阈值静默移动）。
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
    # Key management
    # ------------------------------------------------------------------

    @property
    def _current_key(self) -> str:
        """当前生效的 API key（来自轮换器或直接给出）。"""
        if self._rotator:
            return self._rotator.current()
        return self._api_key

    def _build_client(self, key: str) -> OpenAI:
        """造一个客户端（全仓唯一构造 `OpenAI` 的地方）。"""
        #: 重试由我们自己管（红色横幅 + 1/2/3/5/10/15/30/60s 的节奏）。
        #:   SDK 默认还会在内部重试 2 次 —— 两者叠加的话，界面上的「第几次」
        #:   与实际请求次数就对不上，而且总等待时间是 3 倍。关掉 SDK 那一层。
        #: `http_client=` 必须给：不给的话 SDK 自己造的那个
        #:   `httpx.Client` 是 `trust_env=True`，会静默采纳环境变量里的
        #:   `ALL_PROXY` —— 一条没在听的本地代理就能把整轮对话卡死，
        #:   而界面只会说「请检查宽带互联网连接」。
        #:   这一处是唯一的构造点，改代理只改 `core/net_proxy.py`。
        #: `url=` 必须给：本机模型（ollama / lmstudio）在这一层就判成直连。
        #:   只给探针直连、真请求照旧按 `manual` 走代理的话，症状是
        #:   「联通测试成功、实际对话失败」，而两个数各自都对。
        return OpenAI(api_key=key or _KEYLESS_PLACEHOLDER, base_url=self._base_url,
                      timeout=httpx.Timeout(120.0, connect=10.0), max_retries=0,
                      http_client=net_proxy.httpx_client(url=self._base_url))

    def _ensure_client(self, key: str) -> None:
        """只在 key 真的变了的时候才换客户端。

        每次 LLM 调用都无条件重建客户端，会让重试从头开始 TCP + TLS 握手
        （零复用）。换客户端唯一站得住脚的理由是换了 key（新 key =
        新鉴权头，必须换）；key 没变时重试应当复用连接池。

        不变量：`_client` 与 `_client_key` 一起变 —— 谁改了 `_client`
        而没改 `_client_key`（或反过来），判据就与事实分叉，症状是
        "换了 key 却还在用旧鉴权头"（上游 401，而日志里看不出为什么）。
        """
        fp = net_proxy.fingerprint()
        if (self._client is not None and key == self._client_key
                and fp == self._client_proxy):
            return
        self._client = self._build_client(key)
        self._client_key = key
        self._client_proxy = fp

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
        """Token count via tiktoken (cl100k_base by default)."""
        return len(self._tokenizer.encode(text))

    def _declared_reasoning(self):
        """这家模型自己声明的思考能力（查能力表）。

        `None` 与 `{"reasons": False}` 是两件事：
        · `None`（表里没有这条），这家没告诉我，发送面维持现状；
        · `{"reasons": False}`，这家明确没有思考能力（`qwen-max` 就是）
两样都不发。
        把两者合成一个值，两种都会错一头。
        表读不到（文件坏了）也走 `None`，还是维持现状，不会因此多发什么。
        """
        try:
            from core import window_estimate
            return window_estimate.lookup_reasoning(
                getattr(self, "_provider_name", "") or "", self._model)
        except Exception:
            return None

    @staticmethod
    def _thinking_on(thinking, declared) -> bool:
        """这一条请求到底会不会以"思考模式"发出去 —— 与 `_thinking_kwargs` 同一条判据。

        `_wire_messages` 要用它决定补不补思考回传，而它只有 specs 没有
        kwargs，所以判定单独留一处（两处各写一份，迟早一个说发一个说不发）。
        """
        if thinking is None or not bool(getattr(thinking, "enabled", False)):
            return False
        if isinstance(declared, dict) and declared.get("reasons") is False:
            return False
        return True

    @staticmethod
    def _reasoning_fill_value(model: str) -> "str | None":
        """这家上游缺 `reasoning_content` 时补什么；`None` = 不补。

        DeepSeek 要求回传思考内容本身（占位文本），Kimi 只要求字段存在（空串）。
        其他厂商不补 —— 陌生字段在严格的网关上可能直接 400，而这一条是
        "让本来会过的请求也过"，不该引入新的拒绝面。
        """
        name = str(model or "").lower()
        if "deepseek" in name:
            return REASONING_PASSBACK_PLACEHOLDER
        if "kimi" in name or "moonshot" in name:
            return ""
        return None

    @staticmethod
    def _fill_reasoning_passback(
        messages: list[dict], *, thinking_on: bool, model: str, has_tools: bool,
    ) -> list[dict]:
        """思考模式 + 带工具时，历史里每条 assistant 都要带 `reasoning_content`。

        ## 为什么

        DeepSeek v4 思考模式要求请求历史里每条 assistant 都回传思考内容：
        带 tools 时全部前序轮次都要有，缺一条就 400
        （"The reasoning_content in the thinking mode must be passed back"）。
        上游只在部分请求上校验（与它的上下文缓存状态相关），所以缺块的历史
        表现为间歇性 400 —— 同一段历史，下一次就可能被拒。

        本仓库会产出没有思考的 assistant 消息：自动刹车收尾那段话、用户中断 /
        子任务取消、纯产物回复；模型自己也可能不给。

        ## 补法

        只在 wire 边界补（不改 `messages` 本体）：它要落库，也是缓存前缀，
        在历史里写占位会把"当时真的想了什么"变成一句假话。

        判据只看两点：这条请求真的带着 tools、且真的开着思考。
        纯聊天请求（无 tools）上游不校验，补了反而污染上下文。
        """
        if not has_tools or not thinking_on:
            return messages
        fill = OpenAICompatProvider._reasoning_fill_value(model)
        if fill is None:
            return messages
        out = None
        for i, m in enumerate(messages):
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            #: 空串是"想过但内容为空"（Kimi 的合法形状）—— 那不算缺失；
            #: `None` / 别的类型当作缺失（缺席与"显式给了个 null"对上游是一回事）。
            if isinstance(m.get("reasoning_content"), str):
                continue
            if out is None:
                out = list(messages)
            out[i] = dict(m, reasoning_content=fill)
        return messages if out is None else out

    @classmethod
    def _thinking_kwargs(cls, thinking, declared) -> dict:
        """思考规格 + 能力声明 -> 直接 `kwargs.update()` 进去的那几格。

        一处定义、两条路共用（`chat` / `chat_stream`）—— 与 `_wire_messages`
        逐字同一条理由：只改一条是看不见的（要等有人把流式打开才发现）。

        ## 发送判定

        | 声明 | `extra_body.thinking` | 顶层 `reasoning_effort` |
        |---|---|---|
        | `reasons: false`（这家明确没有） | 不发 | 不发 |
        | `types` 里有 `toggle`（或读不到 types） | 发 enabled/disabled | —— |
        | `types` 里有 `effort` | —— | 发档名 |
        | 表里没这条（读不到） | 发（= 默认形状） | 不发 |

        两样是「一起发」的 —— 上游官方文档与它自家 harness 都是这个形状。

        `values`（上游声明的可选值）不是闸，是文档：`deepseek-v4-flash`
        的表里写着 low/high/max，而**逐档试过去
        none/minimal/low/medium/high/xhigh/max/ultra 全部 ok**
真正的闸只有"这家声明了 effort 吗"这一条。

        `thinking is None`（摘要/标题/压缩这些辅助调用），逐字节等于
        常量 `{"type": "disabled"}` —— 辅助调用一行都不用改。
        """
        on = thinking is not None and bool(getattr(thinking, "enabled", False))
        types = None
        if isinstance(declared, dict):
            if declared.get("reasons") is False:
                return {}
            t = declared.get("types")
            types = set(t) if isinstance(t, list) else None

        out: dict = {}
        if types is None or "toggle" in types:
            out["extra_body"] = {
                "thinking": {"type": "enabled" if on else "disabled"}}
        if on and types is not None and "effort" in types:
            eff = getattr(thinking, "effort", None)
            if eff:
                # 顶层字段（不是 extra_body 里的）—— 官方 SDK 示例的形状：
                #   chat(..., reasoning_effort="high", extra_body={"thinking": {...}})
                out["reasoning_effort"] = str(eff)
        return out

    @staticmethod
    def _wire_messages(
        messages: list[dict],
        system: "str | list[str] | None" = None,
        *,
        thinking_on: bool = False,
        model: str = "",
        has_tools: bool = False,
    ) -> list[dict]:
        """把 (messages, system) 投影成 OpenAI 的 wire 形状。

        Chat Completions 没有顶层 system 字段 —— `system` 只能落在
        `messages[0]`。所以 `system` 在这一层之上始终作为独立请求参数传递，
        到这里（wire 边界）才重新插入，且只在这一处插入。

        刻意只留一个辅助函数：本文件有两条请求路径（`chat` 与
        `chat_stream`），只改一条的修复在流式被打开之前是看不见的。

        三条承重性质，不是附带的：

        1. 只插入 `system` 参数。列表内 `role="system"` 的条目
           原地不动 —— 它们是位置相关的会话状态（恢复历史的标记），
           把它们前移就是"历史伪装成指令"那类故障。
        2. `messages` 只复制、从不修改。调用方那份列表是
           `post_process.record_turn` 落库、`_persist_compaction` 重写时用的
           同一个对象。在这里为 wire 改动它，会静默少写一行到 SQLite，
           而问题要到重启之后才暴露。
        3. 思考回传的占位只补在这一层（`_fill_reasoning_passback`）。
           历史里那条消息在库里还是原样 —— 它是"当时想了什么"的记录。
        """
        messages = OpenAICompatProvider._strip_private_parts(messages)
        messages = OpenAICompatProvider._fill_reasoning_passback(
            messages, thinking_on=thinking_on, model=model, has_tools=has_tools)
        system_text = flatten_system(system)
        if not system_text:
            return messages
        return [{"role": "system", "content": system_text}] + list(messages)

    @staticmethod
    def _strip_private_parts(messages: list[dict]) -> list[dict]:
        """剥掉媒体块上只给框架用的字段（今天只有 `_path` / `_seconds`）。

        音频块需要那两个字段来落库/装回，而它们不该上 wire
          （未知字段在严格网关上可能 400）。
        不改入参（同 `_wire_messages` 第 2 条：调用方那份列表后面还要
          被 `post_process.record_turn` 落库）—— 有私有字段时才复制。
        """
        out = None
        for i, m in enumerate(messages):
            if not isinstance(m, dict):
                continue
            msg = m
            # 消息级的 `artifacts` 也不许上 wire ——
            #   它是"这一轮产出了什么"给界面看的（`core/artifacts.py`），
            #   上游不认识这个键（严格网关会 400），而且模型有工具回执就够了。
            #   与下面那半同一条复制纪律：不改调用方那份列表（它还要落库）。
            if "artifacts" in msg:
                msg = {k: v for k, v in msg.items() if k != "artifacts"}
                if out is None:
                    out = list(messages)
                out[i] = msg
            content = msg.get("content")
            if not isinstance(content, (list, tuple)):
                continue
            parts = []
            changed = False
            for p in content:
                if isinstance(p, dict) and any(
                        str(k).startswith("_") for k in p):
                    parts.append({k: v for k, v in p.items()
                                  if not str(k).startswith("_")})
                    changed = True
                else:
                    parts.append(p)
            if changed:
                if out is None:
                    out = list(messages)
                nm = dict(m)
                nm["content"] = parts
                out[i] = nm
        return out if out is not None else messages

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: "str | list[str] | None" = None,
        thinking=None,
        temperature: "float | None" = None,
    ) -> ChatResponse:
        """Send a chat-completion request and return a ChatResponse."""

        # 限流检查 + 给校准器做预检估算。
        # `system` 与 `tools` 都必须计入：API 的 prompt_tokens（校准器的
        # `actual`）两者都算 —— 见 base.estimate_request_tokens。
        # `tools` 曾在估算里缺失：22 个工具的 wire 集约 4,777 tokens，
        # 估算只有真实值的三分之一，比率（3.3 倍）被钳位并落盘成因子。
        estimated_input = self.estimate_message_tokens(
            messages, system=system, tools=tools
        )
        if self._rate_limiter:
            self._rate_limiter.acquire(expected_tokens=estimated_input)

        declared = self._declared_reasoning()
        kwargs: dict = {
            "model": self._model,
            "messages": self._wire_messages(
                messages, system,
                # 思考回传的占位：判据与 `_thinking_kwargs` 同一条（见 `_thinking_on`）
                thinking_on=self._thinking_on(thinking, declared),
                model=self._model,
                has_tools=bool(tools),
            ),
            # 按次覆盖：`None`，实例默认，逐字节不变
            "temperature": self.resolve_temperature(temperature),
            "max_tokens": self._max_tokens,
            # 默认关闭思考输出：思考链昂贵，且需要仔细的多轮回传 ——
            # 只在显式开启时使用。
            #
            # 形状不变，值由投入档交给 `_thinking_kwargs`；
            # `thinking=None`（辅助调用 / 没接档位）仍然逐字节是
            # `{"type": "disabled"}`。
            # 能力声明 + 发送前拦（不是撞 400 再降级）。
            **self._thinking_kwargs(thinking, declared),
        }
        if tools:
            kwargs["tools"] = tools

        max_retries = self._rotator.healthy_count if self._rotator else 1

        #: 重试节奏（`core/llm_retry.py::step`）：常规 10 次 + 静默 10 次
        #:   与"换 key"那套正交，所以各记各的。
        rstate: dict = {}
        attempts_left = llm_retry.MAX_RETRIES + llm_retry.SILENT_MAX + max_retries

        for attempt in range(attempts_left):
            key = self._current_key
            # 只在 key 变了时换 —— 两条路（sync / stream）都必须走这里
            self._ensure_client(key)

            try:
                response = self._client.chat.completions.create(**kwargs)
                msg = response.choices[0].message

                # Extract token usage from the API response
                usage = response.usage
                prompt_tokens = usage.prompt_tokens if usage else 0
                completion_tokens = usage.completion_tokens if usage else 0
                # 缓存命中：读不到就是 None —— 别退化成 0，
                # 那会把"上游没报"和"一次都没命中"混成一个数。
                cache_hit = read_cache_hit(usage)

                result = ChatResponse(
                    content=msg.content,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cache_hit_tokens=cache_hit,
                    # 同步路径必须填它（字段在 `ChatResponse` 上声明、
                    # 流式那条一直在填），下游分不出"正常说完"与"被上游截断"
                    # （思考吃光输出额度，空回复，而框架把它当成"说完了"）。
                    # 读不到时给 `None`（不是空串）—— 读不到 != 0。
                    finish_reason=getattr(response.choices[0], "finish_reason", None),
                    # 上游直接产出的媒体（没有就是 `None`）。
                    media=_extract_media(msg),
                )
                record_token_usage(prompt_tokens, completion_tokens,
                                   cache_hit=cache_hit,
                                   provider=self._provider_name,
                                   model=self._model)

                # 声明窗口与实际请求的硬矛盾检测
                self._check_window_claim(prompt_tokens)

                # 把上游的真实 token 数喂给校准器
                if self._calibrator is not None and prompt_tokens > 0:
                    prev_factor = self._calibrator.factor
                    ratio = self._calibrator.feed(
                        estimated=estimated_input,
                        actual=prompt_tokens,
                    )
                    self._calibrator.save()
                    if self._calibrator.factor != prev_factor:
                        # ratio 是未钳的原值：钳位只影响 EMA，不该把
                        # 离群本身藏起来（3.3 倍那类偏差就是从这一行看出来的）
                        print(
                            f"[calibrator] Updated: "
                            f"est={estimated_input} actual={prompt_tokens} "
                            f"ratio={ratio:.2f} "
                            f"factor={self._calibrator.factor:.4f} "
                            f"({self._calibrator.samples} samples)"
                        )

                # 收下 reasoning_content（思考模式）
                reasoning = getattr(msg, "reasoning_content", None)
                if reasoning:
                    result.reasoning_content = reasoning

                if msg.tool_calls:
                    result.tool_calls = [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ]

                #: 连上了，横幅变绿（没有状态时 `clear` 是空操作）。
                llm_retry.clear(_session_key())
                return result

            except Exception as e:
                # 分类只有一处（`providers/base.py`）——
                # 这里与下面流式那条共用同一套分类。
                kind = classify_api_error(e)
                # 带了图而错误归不到已知类别，提示"可能是模型不收图"
                #   （它自己会判断该不该说，见 `providers/base.py` 那段）
                image_hint_for_failure(messages, e)

                # 超长先判、且不重试：换 key / 重试对它一点用都没有
                #（不是权限也不是限流，是请求本身太大），重试只会再爆一次。
                #: 与流式那条同一行格式（同一处诊断口径）。
                print(f"[llm] 调用失败（{type(e).__name__}: {str(e)[:200]}）"
                      f" —— model={self._model} base={self._base_url}"
                      f" kind={kind or classify_transport_error(e) or 'unknown'}")
                if kind == "context_overflow":
                    log_context_overflow(self._model, e, where="chat")
                    raise

                # 重试节奏只有一处（`core/llm_retry.step`）：
                #   连接 / 超时 / 5xx 走常规 10 次，再转静默每 120 秒一次；
                #   余额 / 参数 / 认证 / 限流 / 内容审核一律不重试
                #   （重试十次只是把同一个错误再犯十次）。
                transport = classify_transport_error(e)
                eff = kind or transport or ""
                action, delay = llm_retry.step(rstate, eff)
                if action in ("retry", "silent"):
                    n = int(rstate.get("silent") or 0) if action == "silent" \
                        else int(rstate.get("retry") or 0)
                    _head = headline(e, kind=eff)
                    llm_retry.note(_session_key(), n, kind=eff, text=_head,
                                   phase=action)
                    print(f"[llm] {eff or 'unknown'} —— {delay}ms 后重试"
                          f"（{action} {n}）")
                    #: 可打断的等待 —— 用户按了打断就立刻停，
                    #:   把原异常抛回（同步这条路没有"取消"专用分支）。
                    if not llm_retry.wait(_session_key(), delay):
                        print("[llm] 用户打断了这一轮 —— 不再重试")
                        raise
                    continue

                if kind in ("auth", "rate_limit") and self._rotator:
                    self._rotator.mark_fault(key)
                    if attempt < max_retries - 1:
                        continue

                # 不再重试（或次数用尽）：横幅留着说这一句，然后抛回。
                llm_retry.fail(_session_key(), kind=eff, text=headline(e, kind=eff))
                raise

        # Should not reach here, but safety net:
        raise RuntimeError("All API keys exhausted")

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: "str | list[str] | None" = None,
        thinking=None,
        temperature: "float | None" = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat response as async generator of StreamChunks."""

        # 限流检查 + 给校准器做预检估算。
        # 与 chat() 完全一致 —— 两条路必须走同一个投影（见 _wire_messages），
        # 且 `tools` 必须计入估算（理由见 chat()）。
        estimated_input = self.estimate_message_tokens(
            messages, system=system, tools=tools
        )
        if self._rate_limiter:
            self._rate_limiter.acquire(expected_tokens=estimated_input)

        declared = self._declared_reasoning()
        kwargs: dict = {
            "model": self._model,
            "messages": self._wire_messages(
                messages, system,
                # 与 `chat()` 同一条判据 —— 两条路不许各写一份
                thinking_on=self._thinking_on(thinking, declared),
                model=self._model,
                has_tools=bool(tools),
            ),
            # 按次覆盖：`None`，实例默认，逐字节不变
            "temperature": self.resolve_temperature(temperature),
            "max_tokens": self._max_tokens,
            "stream": True,
            # 与 `chat()` 同一处定义（`_thinking_kwargs`）—— 两条路不许各写一份
            # 能力声明 + 发送前拦（不是撞 400 再降级）。
            **self._thinking_kwargs(thinking, declared),
        }
        if tools:
            kwargs["tools"] = tools

        max_retries = self._rotator.healthy_count if self._rotator else 1
        #: 重试节奏（`core/llm_retry.step`）：常规 10 次 + 静默 10 次。
        #:   与"换 key"那套正交，各记各的。
        rstate: dict = {}
        #: 有没有任何一片已经交出去。交出过内容就不能整条重试 ——
        #: 界面已经看到了那半句，重来的话它会看到两遍（而且两次的正文
        #: 未必一样）。只有"一片都没来过"时才重试。
        emitted = False

        attempts_left = llm_retry.MAX_RETRIES + llm_retry.SILENT_MAX + max_retries
        for attempt in range(attempts_left):
            key = self._current_key
            # 只在 key 变了时换 —— 两条路（sync / stream）都必须走这里
            self._ensure_client(key)

            try:
                stream = self._client.chat.completions.create(**kwargs)

                for chunk in stream:
                    emitted = True
                    if chunk.choices and len(chunk.choices) > 0:
                        delta = chunk.choices[0].delta
                        finish = chunk.choices[0].finish_reason

                        # 思考增量
                        #
                        # `StreamChunk.reasoning_content` 必须在这里填，否则
                        # 开了思考的档在流式下拿不回那段正文（sync 那条路
                        # 在 `chat()` 里填的是同一个字段）。
                        # 判据：`if delta.reasoning_content:` —— 空串不产出分片。
                        #   上游第一片常常是个空 `reasoning_content`，空字符串不能开块 ——
                        #   否则会得到一个空块（下游累积器会纠结"这算不算思考"）。
                        # 顺序：思考在正文之前。
                        if getattr(delta, "reasoning_content", None):
                            yield StreamChunk(reasoning_content=delta.reasoning_content)

                        # Text delta
                        if delta.content:
                            yield StreamChunk(content=delta.content)

                        # Media delta：有的生图/生音网关把结果
                        # 放在流分片里（`delta.images` / `delta.audio` /
                        # 块列表），与同步那条同一个解析函数（`_extract_media`），
                        # 形状与 `ChatResponse.media` 逐字相同。
                        # 只收 data URL：外链不下载（否则绕开出网闸）。
                        _media = _extract_media(delta)
                        if _media:
                            yield StreamChunk(media=_media)

                        # Tool call delta
                        if delta.tool_calls:
                            for tc_delta in delta.tool_calls:
                                # 三处都不许归一化。
                                #
                                # 把"没有这个字段"变成空串会让空串
                                # 看起来像一个身份：后续分片（协议上本来就不带
                                # id）会落进累积器的"没有 id，新建一条"分支，
                                # 同一次调用被劈成两条（一条空参数、一条空名字）
                                #`unknown tool ''`，看起来像卡住。
                                #
                                # `function` 本身也可能是 None（只带 id 的分片）
                                #用 `getattr(..., None)`，别直接取属性。
                                fn = getattr(tc_delta, "function", None)
                                yield StreamChunk(
                                    tool_call={
                                        "id": getattr(tc_delta, "id", None),
                                        "type": "function",
                                        "function": {
                                            "name": getattr(fn, "name", None),
                                            "arguments": getattr(fn, "arguments", None),
                                        },
                                    },
                                    # 槽位号：累积的唯一依据（协议保证给）
                                    tool_call_index=getattr(tc_delta, "index", None),
                                )

                        # Final chunk with usage
                        if finish and chunk.usage:
                            pt = chunk.usage.prompt_tokens or 0
                            ct = chunk.usage.completion_tokens or 0
                            ch = read_cache_hit(chunk.usage)
                            record_token_usage(pt, ct, cache_hit=ch,
                                               provider=self._provider_name,
                                               model=self._model)
                            yield StreamChunk(
                                usage={
                                    "prompt_tokens": pt,
                                    "completion_tokens": ct,
                                    "cache_hit_tokens": ch,
                                },
                            )

                        # 收尾那片必须无条件吐出来。
                        #
                        # 上游的收尾片是「`delta` 空、只有 `finish_reason`」——
                        # 而另外三个出口分别挂在 `delta.content` /
                        # `delta.tool_calls` / `chunk.usage` 上，一个都不命中
                        #`finish_reason` 会被丢掉。
                        #
                        # 后果双重：① 下游永远分不出"正常说完"与"被上游截断"
                        # （空回复就是这样被当成"说完了"）；② 我们没发
                        # `stream_options.include_usage`，`chunk.usage` 也常常是
                        # `None`，连最后那条兜底出口都没有。
                        #
                        #判据落成一句：只要上游给了 `finish`，就必须有一片带它出去，
                        # 与那三样东西有没有内容无关。
                        if finish:
                            yield StreamChunk(finish_reason=finish)

                #: 连上了，横幅变绿（没有状态时 `clear` 是空操作）。
                llm_retry.clear(_session_key())
                return  # 流正常结束

            except Exception as e:
                kind = classify_api_error(e)
                transport = classify_transport_error(e)
                #: 出声 —— 只把异常吞成一个裸串时，控制台上除了
                #:   `(error: …)` 什么都没有，事后没法分清"断网 / 超时 /
                #:   上游 5xx"。
                print(f"[llm] 流式失败（{type(e).__name__}: {str(e)[:200]}）"
                      f" —— model={self._model} base={self._base_url}"
                      f" kind={kind or transport or 'unknown'}")
                if kind == "context_overflow":
                    # 流式这条必须认得出超长：否则用户只看到裸的上游原文，
                    # 而盘上一行都没有。现在出声 + 在错误串里带一个**可辨认的
                    # 标记**（`context_overflow`），供上层断路器识别。
                    log_context_overflow(self._model, e, where="chat_stream")
                    yield StreamChunk(error=f"context_overflow: {e}",
                                      error_kind="context_overflow")
                    return

                # 重试节奏与 sync 同一条（`core/llm_retry.step`）。
                # `not emitted` 是硬条件：已经交出去的内容收不回来，
                #   整条重来会让用户看到两遍（见上面 `emitted` 的注释）。
                eff = kind or transport or ""
                action, delay = (("no_retry", 0) if emitted
                                 else llm_retry.step(rstate, eff))
                if action in ("retry", "silent"):
                    n = int(rstate.get("silent") or 0) if action == "silent" \
                        else int(rstate.get("retry") or 0)
                    _head = headline(e, kind=eff)
                    llm_retry.note(_session_key(), n, kind=eff, text=_head,
                                   phase=action)
                    print(f"[llm] {eff or 'unknown'} —— {delay}ms 后重试"
                          f"（{action} {n}）")
                    #: 可打断。用户一按打断就交一片空 chunk，
                    #:   循环顶部的取消检查会接手（那条路会回「已按你的要求中断」）。
                    if not await llm_retry.wait_async(_session_key(), delay):
                        print("[llm] 用户打断了这一轮 —— 不再重试")
                        llm_retry.clear(_session_key())
                        yield StreamChunk(content="")
                        return
                    continue
                if kind in ("auth", "rate_limit") and self._rotator:
                    self._rotator.mark_fault(key)
                    if attempt < max_retries - 1:
                        continue

                # 不再重试（或次数用尽 / 已经交出去过内容）：横幅留着说这一句。
                llm_retry.fail(_session_key(), kind=eff, text=headline(e, kind=eff))

                # 提示也进返回给用户的那句话（流式这条路上
                #   控制台之外没有别的出口 —— 用户看不到 `print`）
                _hint = image_hint_for_failure(messages, e)
                yield StreamChunk(
                    error=str(e) + (("\n\n" + _hint) if _hint else ""),
                    error_kind=kind or transport or "")

        # Should not reach here
        yield StreamChunk(error="All API keys exhausted", error_kind="auth")

    async def test_connection(self) -> dict:
        """Send a minimal ping to verify the API is reachable."""
        try:
            resp = self.chat(
                [{"role": "user", "content": "ping"}],
                tools=None,
            )
            return {"ok": True, "message": f"API reachable (model={self._model})"}
        except Exception as e:
            return {"ok": False, "message": f"Connection failed: {e}"}
