"""上游模型报错的用户面 —— 别让用户看到一行 `error`。

## 为什么要有这一份

网络断了一下时，聊天里不该出现：

    (error: Connection error.)

它看起来像模型说的话，既没说"这一轮什么都没产出"，也没说"下一步做什么"；
而 `task.end.ok` 还可能被记成 true（流式那条会把错误吞成正文）。

## 三条判据

1. 说人话 + 说下一步：每一类都给「发生了什么 / 这一轮有没有产出 /
   现在可以做什么」。原始错误放在最后一行（诊断要留，但别当头一句）。
2. 同步与流式走同一个产者：`explain()` 是唯一一处。
   两条路各自内联一份的话，同一个故障会有两种表现。
3. 失败要被记成失败：流式那条在正文前面打一个 `MARK`，由
   `AgentLoopStage` 剥掉并写 `ctx.metadata["error"]`，`task.end.ok=false`。
   标记只在进程内的一跳上存在（stage 之后就没有了），用户看不到它。

## 分类

`classify_api_error` 管三类按报文的（overflow / auth / rate_limit）；
连接与超时按异常类型判（`classify_transport_error`）。两类正交，这里合并。
"""

from __future__ import annotations

__all__ = ["MARK", "HEADLINES", "headline", "explain", "strip_mark", "is_marked"]

#: 进程内标记 —— 不是给用户看的。`AgentLoopStage` 见到它就把正文剥掉、
#: 并把 `ctx.metadata["error"]` 写上（`task.end.ok` 因此是 false）。
MARK = "\x00[LLM_ERROR]\x00"


def is_marked(text) -> bool:
    return str(text or "").startswith(MARK)


def strip_mark(text) -> str:
    s = str(text or "")
    return s[len(MARK):] if s.startswith(MARK) else s


def _host_of(base_url: str) -> str:
    """从 base_url 取一个人能认的主机名（取不到就原样回）。"""
    u = str(base_url or "").strip()
    if not u:
        return ""
    try:
        from urllib.parse import urlparse
        host = urlparse(u).hostname or ""
        return host or u
    except Exception:                                           # noqa: BLE001
        return u


def _upstream_error_message(text: str) -> str:
    """从 400 的原文里抠出 `error.message`。抠不到回空串。

    形状（OpenAI SDK 的 `str(exc)`，DeepSeek 的原话）::

        Error code: 400 - {'error': {'message': 'The supported API model names are
        deepseek-flash, deepseek-v4-pro, but you passed deepseek-v4.1-flash',
        'type': 'invalid_request_error', 'param': None,
        'code': 'invalid_request_error'}}

    只认结构不猜语义：`ast.literal_eval` 解不开就当没抠到（调用方退回旧形状）。
    用户的报错原文是模型之外的人写的，所以这里只做解析、不回显整段。
    """
    import ast
    import re as _re
    m = _re.search(r"\{.*\}", text, _re.S)
    if not m:
        return ""
    try:
        data = ast.literal_eval(m.group(0))
    except Exception:                                            # noqa: BLE001
        return ""
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict) and isinstance(err.get("message"), str):
        return err["message"].strip()
    if isinstance(data.get("message"), str):
        return str(data["message"]).strip()
    return ""


#: 每一类的一句话。横幅与聊天正文共用这张表（一处定义）。
#:
#: 措辞按"用户下一步能做什么"写：充值 / 等一会儿 / 换 key / 压缩 / 换说法。
#: 连接与超时那两句保持原样（横幅上一直是这两句，用户已经认得）。
HEADLINES: dict[str, str] = {
    "balance": "API余额不足，请检查账号资金",
    "request": "请求参数异常",
    "rate_limit": "API请求频率超限，请稍后重试",
    "server_fault": "上游服务器故障",
    "server_busy": "上游服务器繁忙，请稍后重试",
    "server_other": "上游异常",
    "content": "上游拒绝请求：含有非法内容",
    "region": "上游异常：当前地区不支持服务",
    "too_large": "请求错误：消息过大，尝试压缩或重启应用以继续对话",
    "auth": "api密钥错误，请前往端配置页检查您的api可用性",
    "connection": "网络连接异常--请检查宽带互联网连接",
    "timeout": "网络连接超时——请检查网络或代理",
    "context_overflow": "请求过长，超出模型上下文",
}


def kind_of(err, kind: str = "", exc_name: str = "") -> str:
    """归一成一张表里的类别名。认不出回空串。

    判类的地方只有一处（`providers/base.py` 那两个函数）；这里只做兜底，
    好让 `explain()`/`headline()` 在没拿到 kind 时也说得出话。
    """
    k = str(kind or "").strip().lower()
    if k:
        return k
    try:
        from providers.base import classify_api_error, classify_transport_error
    except Exception:                                           # noqa: BLE001
        return ""
    try:
        return (classify_api_error(err)
                or classify_transport_error(err)
                or "")
    except Exception:                                           # noqa: BLE001
        return ""


def headline(err, *, kind: str = "", exc_name: str = "") -> str:
    """横幅上那一句（短、不含原文）。

    认不出的类别不装懂：退到 400 的两种形状，再退到"这一轮出错了"。
    """
    k = kind_of(err, kind=kind, exc_name=exc_name)
    if k in HEADLINES:
        return HEADLINES[k]
    return ""


def _model_name_mismatch(reason: str):
    """上游说的是不是「模型名填错了」？是就回 `(支持的名字, 你填的)`。

    判据用的是上游的原句：``The supported API model names are A, B,
    but you passed C``。这句比任何本地规则都准 —— 它带着上游此刻认的名字。
    """
    import re as _re
    m = _re.search(r"supported API model names are (.+?),?\s*but you passed\s+([^\s,]+)",
                   reason, _re.I)
    if not m:
        return None
    return m.group(1).strip().rstrip(","), m.group(2).strip("'\"`.")


def explain(err, *, kind: str = "", model: str = "", base_url: str = "",
            stage: str = "", exc_name: str = "") -> str:
    """把一次上游/内部异常变成用户能照着做的一段话。

    *err* 可以是异常对象，也可以是 `str`（流式那条拿到的是字符串）。
    *kind* 优先于关键词（`connection` / `timeout` / `context_overflow` /
    `auth` / `rate_limit`）；没给就按原文猜（与 `classify_api_error` 同口径）。
    """
    text = str(err or "")
    name = str(exc_name or "") or type(err).__name__
    #: 判类只有一处（`providers/base.py`）。这里不再抄第二张表 ——
    #: 抄一份就会与它各自漂，而用户看到的正是这第二份。
    k = kind_of(err, kind=kind, exc_name=exc_name)

    where = _host_of(base_url)
    tail = "\n\n原始错误：%s" % (text[:300] if text else "（上游没给原文）")

    if k == "connection":
        head = ("⚠️ **没能连上模型服务**" + ("（%s）" % where if where else "")
                + ("，模型 `%s`" % model if model else "") + "。\n\n"
                "**这一轮没有产生任何结果** —— 没有调用工具，也没有花掉模型额度。\n\n"
                "可能原因：网络断开 / 代理或 VPN / 上游临时故障。")
        steps = ("\n- 直接**重发这条消息**重试（客户端已经自动重试过几轮了）；"
                 "\n- 连续失败：先看网络，或在「端配置」换一条模型。")
        return head + steps + tail

    if k == "timeout":
        head = ("⚠️ **模型服务超时**" + ("（%s）" % where if where else "") + "。\n\n"
                "**这一轮没有产生任何结果。**")
        steps = ("\n- 直接重发一次；连续超时：检查网络 / 代理，"
                 "或在「端配置」换一条模型。")
        return head + steps + tail

    if k == "context_overflow":
        return ("⚠️ **这次的请求太长了** —— 上游拒绝处理（不是网络、也不是配额）。\n\n"
                "**这一轮没有产生结果。** 可以：把需求拆小一点、先 `/compact` "
                "压缩上下文，或换一个窗口更大的模型。" + tail)

    #: 其余每一类都是一句话（`HEADLINES`）+ 原文 —— 那句话就是用户要的
    #:   "下一步"：充值 / 等一会儿 / 换 key / 压缩 / 换个说法。
    if k in HEADLINES:
        return HEADLINES[k] + tail

    if "400" in text or name == "BadRequestError":
        #: 先试着把上游那句话抠出来。整段 JSON 倒给用户是噪音：
        #:   它就是一大坨 `{'error': {...}}`，而真正的原因是
        #:   一句话 ——「模型名填错了，上游只认 deepseek-flash / deepseek-v4-pro」。
        #: 抠不出来就退回旧形状（整段原文 + 那句提醒）——
        #:   那条判据没变：400 的真因也可能是工具表重名，不能一律说成配置问题。
        reason = _upstream_error_message(text)
        if reason:
            _model = _model_name_mismatch(reason)
            if _model:
                supported, passed = _model
                return ("⚠️ **模型名不对** —— 上游只支持 %s，你配置的是 `%s`。\n\n"
                        "到「端配置页」把「模型名」改成上面其中一个，再发一次。"
                        % (supported, passed))
            return "上游拒绝了这次请求（400）：\n%s" % reason[:300]
        #: 原文是「检查模型名称和参数配置」—— 会误导（400 的真因也可能是
        #: 工具表重名）。带上游原文，让真因自己说话。
        return ("模型请求被上游拒绝（400）。上游原文：\n%s\n\n"
                "⚠️ 不一定是配置问题 —— 400 也可能来自请求本身"
                "（例如工具表重名、消息格式）。" % (text[:800] or "（没有原文）"))

    #: 其它 —— 不装懂：说清是哪一层炸的，把原文交出去。
    where2 = (" [%s]" % stage) if stage else ""
    return ("⚠️ 这一轮出错了%s（%s）。\n\n"
            "**这一轮可能没有产生完整结果。** 可以重发一次；"
            "同一症状反复出现就带上下面这段原文反馈。%s"
            % (where2, name or "未知错误", tail))
