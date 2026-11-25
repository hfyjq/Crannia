"""模型能力差异表 —— 能不能调工具、参数禁区、思考预算、wire 兼容位。

## 它不管什么

上下文窗口与单次输出上限仍由 `window_estimate.py` 决议，缓存与图预算各有自己的表。
这里只装"同一家 provider 下不同模型不一样、而代码不该写 if 判断"的那些格
（抄成熟平台的做法：一字段一能力、静态表 + 显式覆盖 + 回落）。

## 回落顺序

模型名（最长优先的包含匹配）→ provider 的 `_default` → 全局 `_default`。
`unknown` 是有意义的取值：它表示"查不到"，不等于 `false`。
调用方遇到 `unknown` 必须走 `on_unsupported` 里那条路线，不许当 False 直接用。

## 不写回

没有"从 usage / 错误反推然后写回表里"的逻辑：
静态表由生成脚本或人工维护，运行时只做一次性自适应。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

_LOCK = threading.RLock()
#: 路径 -> (mtime, 解析后的 dict)。模块级可变，登记在 core/state_registry.py。
_CACHE: dict[str, tuple[float, dict]] = {}

#: 字段类型：真布尔、未知、缺省。字符串形式的 true/false 一律不认（防手滑）。
_UNKNOWN = object()


def _table_path() -> Path:
    from core import paths
    return Path(paths.asset_dir("providers")) / "model_profiles.json"


def _read_table() -> dict:
    try:
        mtime = _table_path().stat().st_mtime
    except Exception:                                # noqa: BLE001
        return {}
    with _LOCK:
        hit = _CACHE.get(str(_table_path()))
        if hit is not None and hit[0] == mtime:
            return hit[1]
    try:
        data = json.loads(_table_path().read_text(encoding="utf-8"))
    except Exception:                                # noqa: BLE001
        with _LOCK:
            hit = _CACHE.get(str(_table_path()))
        return hit[1] if hit is not None else {}
    if not isinstance(data, dict):
        return {}
    with _LOCK:
        _CACHE[str(_table_path())] = (mtime, data)
    return data


def _tri(value) -> bool | None:
    """表里的一格：True / False / None（unknown）。不把字符串当布尔。"""
    if value is True:
        return True
    if value is False:
        return False
    return None


def _text(value, default: str = "") -> str:
    return str(value) if isinstance(value, str) and value else default


def _bounded_int(value, default: int = 0, high: int = 10_000_000) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if 0 <= n <= high else default


def _merge(base: dict, override: dict) -> dict:
    """逐字段覆盖：dict 递归合并，其它类型直接替换。"""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass(frozen=True)
class ModelProfile:
    """一个 (provider, model) 的能力画像。字段与表一一对应。"""

    provider: str
    model: str
    tool_call: bool | None
    parallel_tool_call: bool | None
    response_schema: bool | None
    system_message: bool | None
    thinking: dict
    parameters: dict
    compat: dict
    aliases: tuple[str, ...]
    deprecated: bool
    status: str
    on_unsupported: dict
    source: str
    matched: str = ""

    def can(self, key: str) -> bool | None:
        """某一能力：True / False / None（查不到）。"""
        return _tri(getattr(self, key, None))

    def parameter(self, name: str) -> bool | None:
        return _tri(self.parameters.get(name))

    def compat_of(self, name: str) -> str:
        return _text(self.compat.get(name))


def resolve(provider: str = "", model: str = "") -> ModelProfile:
    """按模型 → provider 默认 → 全局默认取一份能力画像。不抛异常。"""
    table = _read_table()
    global_default = table.get("_default")
    if not isinstance(global_default, dict):
        global_default = {}

    prov = str(provider or "")
    model = str(model or "")
    providers = table.get("providers")
    provider_row = providers.get(prov) if isinstance(providers, dict) else None
    if not isinstance(provider_row, dict):
        provider_row = {}

    # 模型级匹配：先精确，再按名字长度降序做包含匹配。长的快照名先命中。
    model_row: dict = {}
    matched = ""
    models = provider_row.get("models")
    if isinstance(models, dict) and model:
        keys = sorted((str(k) for k in models), key=len, reverse=True)
        low = model.lower()
        exact = next((k for k in keys if k.lower() == low), "")
        hit = exact or next((k for k in keys if k.lower() in low), "")
        if hit and isinstance(models[hit], dict):
            model_row = models[hit]
            matched = hit

    provider_default = provider_row.get("_default")
    if not isinstance(provider_default, dict):
        provider_default = {}

    row = _merge(_merge(_merge(global_default, provider_default), model_row), {})
    thinking = row.get("thinking")
    parameters = row.get("parameters")
    compat = row.get("compat")
    on_unsupported = row.get("on_unsupported")

    return ModelProfile(
        provider=prov,
        model=model,
        tool_call=_tri(row.get("tool_call")),
        parallel_tool_call=_tri(row.get("parallel_tool_call")),
        response_schema=_tri(row.get("response_schema")),
        system_message=_tri(row.get("system_message")),
        thinking=thinking if isinstance(thinking, dict) else {},
        parameters=parameters if isinstance(parameters, dict) else {},
        compat=compat if isinstance(compat, dict) else {},
        aliases=tuple(str(a) for a in row.get("aliases", ()) if isinstance(a, str)),
        deprecated=row.get("deprecated") is True,
        status=_text(row.get("status"), "unknown"),
        on_unsupported=on_unsupported if isinstance(on_unsupported, dict) else {},
        source="providers/model_profiles.json",
        matched=matched,
    )


def reset_cache() -> None:
    """只给测试用：表被改写后强制重读。"""
    with _LOCK:
        _CACHE.clear()


# --- 消费点：画像要真的影响请求，不能只躺在表里 -------------------------

def accepts_tools(provider: str, model: str) -> bool:
    """能不能给这一家发工具表。

    只有画像明确说 false 才不发。查不到（unknown）时照旧发 ——
    用户自建的中转 / 自定义模型大多不支持能力查询，一上来就清空工具表
    等于把今天能用的模型全弄坏。

    `on_unsupported.tool_call=error` 管的是另一件事：模型报出一个没下发给它
    的工具名时执行侧要拒绝，而不是"不发工具"。那道闸在执行侧，不在这里。
    """
    got = resolve(provider, model).can("tool_call")
    return True if got is None else got


def rejects_unknown_tool(provider: str, model: str) -> bool:
    """幻觉出一个没下发给它的工具名时，要不要拒绝执行。

    接管类工具在能力未知时也拒绝（表里的默认策略是 error）；
    这是执行侧的闸，与"发不发工具表"分开。
    """
    profile = resolve(provider, model)
    if profile.can("tool_call") is False:
        return True
    return profile.on_unsupported.get("tool_call") == "error"


def accepts_parameter(provider: str, model: str, name: str) -> bool:
    """某个采样参数该不该发。没写就按 on_unsupported.parameter 处理。"""
    profile = resolve(provider, model)
    got = profile.parameter(name)
    if got is not None:
        return got
    return profile.on_unsupported.get("parameter") != "drop"


def wire_field(provider: str, model: str, field: str, fallback: str = "") -> str:
    """按画像取 wire 字段名（reasoning_field / max_tokens_field / cache_format）。"""
    got = resolve(provider, model).compat_of(field)
    return got or fallback


def reports_finish_reason(provider: str, model: str) -> bool:
    """这一家会不会在响应里给结束信号。

    只有明确会报的才把"没收到结束信号"当半截流 —— 对不报的那批，
    "读不到"就是读不到，不是断（读不到 ≠ 0 的同族判据）。
    """
    value = resolve(provider, model).compat.get("reports_finish_reason")
    return value is True


def filter_request_params(params: dict, provider: str, model: str) -> dict:
    """按画像删掉目标模型禁发的采样参数，修正输出上限的字段名。

    只删不改：画像说禁的参数一律不发（发了不生效或 400 的都有），
    画像没表态的保持原样。max_tokens 字段名只在画像明确给出另一个名字时换，
    不靠模型名猜 —— 猜错比不发更糟。
    """
    out = dict(params)
    profile = resolve(provider, model)
    for key in ("temperature", "top_p", "top_k", "frequency_penalty",
                "presence_penalty", "stop", "stop_sequences"):
        if key in out and profile.parameter(key) is False:
            out.pop(key)
    field = profile.compat_of("max_tokens_field")
    if field and field != "max_tokens" and "max_tokens" in out:
        out[field] = out.pop("max_tokens")
    return out


def sanitize_for_wire(messages: list[dict], provider: str, model: str) -> list[dict]:
    """按目标模型清洗历史副本，只改副本、不动调用方那份（它还要落库）。

    三件事，都是跨模型切换会踩到的：

    - 目标不收思考字段时剥掉 assistant 的 `reasoning_content`。
      上一家的思考留在库里是对的（那是当时的记录），发给不认它的下一家才是错。
    - 空的 assistant 补一个空格。严格网关拒空 content，而这一位有时还要
      用来对齐 thinking 签名，填空串比整条删掉安全。
    - tool 调用后面缺结果时补一条可见的错误结果，避免悬空的 tool_use
      让上游 400。

    幂等：同一份输入跑两次结果相同；没有需要改的地方就原样返回入参。
    """
    profile = resolve(provider, model)
    # 历史里的字段名固定叫 `reasoning_content`（我们写入时就是这个名字）。
    # 只有当目标模型也认这个名字时才留着；改成别的名字或根本不收时剥掉 ——
    # 改写字段名等于把上一家的思考当成这一家的思考，那是伪造。
    keep_reasoning = profile.compat_of("reasoning_field") == "reasoning_content"
    out: list[dict] | None = None

    def _writable() -> list[dict]:
        nonlocal out
        if out is None:
            # 必须与原列表等长：下面按原下标回写，过滤掉非 dict 元素会让
            # 下标整体前移，改到错误的条目上（坏历史里真的会这样）。
            out = [dict(m) if isinstance(m, dict) else m for m in messages]
        return out

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant" and not keep_reasoning and "reasoning_content" in msg:
            _writable()[i].pop("reasoning_content", None)
        if role == "assistant":
            content = msg.get("content")
            if content == "" or content == []:
                _writable()[i]["content"] = " "

    # 悬空 tool_use：assistant 报了调用、后面却没有对应 tool 结果。
    # 只在本副本里补；落库那份保持原样，便于事后看出是哪一轮漏了。
    # 记下每条挂起调用由哪条 assistant 声明 —— 占位结果必须紧跟在它后面
    # （OpenAI 兼容与 Anthropic 都要求 tool 结果直接跟随那个 tool_call 轮；
    #   一律追加到列表末尾在"后面还有别的消息"时会被上游拒）。
    pending: dict[str, int] = {}
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            calls = msg.get("tool_calls")
            if isinstance(calls, (list, tuple)):
                for tc in calls:
                    if isinstance(tc, dict) and tc.get("id"):
                        pending[str(tc["id"])] = i
        elif role == "tool":
            cid = msg.get("tool_call_id")
            if cid:
                pending.pop(str(cid), None)
    if pending:
        target = _writable()
        by_msg: dict[int, list[str]] = {}
        for cid, idx in pending.items():
            by_msg.setdefault(idx, []).append(cid)
        # 从后往前插：同一轮的多条按 id 排序，用切片一次插入保持顺序。
        for idx in sorted(by_msg, reverse=True):
            block = [{"role": "tool", "tool_call_id": cid,
                      "content": "（这一条工具结果在会话里缺失，已补占位）"}
                     for cid in sorted(by_msg[idx])]
            target[idx + 1:idx + 1] = block
        out = target

    return out if out is not None else list(messages)
