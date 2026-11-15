#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探针：微信 iLink 到底能不能"主动开口"。

    # 只打印要发什么（不联网、不发）
    .venv/Scripts/python.exe scripts/probe_wechat_push.py

    # 第一步：sendtyping（对方看不见任何消息）—— 会自动试三种字段名
    .venv/Scripts/python.exe scripts/probe_wechat_push.py --typing

    # 第二步：真发一句（微信里会收到）
    .venv/Scripts/python.exe scripts/probe_wechat_push.py --send

## 为什么要有这个探针

同一个事实有两种说法，而它们不能同时为真：

* 一种说法：iLink 不能主动推送
  —— 必须先由用户发消息触发、回复还要带 `context_token`；
* `channels/wechat_channel/handler.py` 的 `send()`：注释写着
  「proactive messages have no context_token」，即试试看，
  失败了 `_api_post` 照常抛。

谁对，只有对着真实账号跑一遍才知道。这个脚本只做那一件事：拿缓存凭据，按 `_send_text`
的 body 发一次，把 HTTP 结果原样打出来。

## 首跑读数

结论：主动发正文（`sendmessage`）成立 —— HTTP 200，且带回 `message_id`。

两个副产物：

1. `sendtyping` 用 `to_user_id` 发出去，回的是
   `HTTP 200 {"ret":-2,"errmsg":"ilink_user_id required"}`；
   换成 `ilink_user_id` 之后变成 `{"ret":-3,"errmsg":"typing_ticket required"}`。
   所以字段名是 `ilink_user_id`，而且还要一个 `typing_ticket`
   （那东西来自收到的消息），「正在输入」只能在对话窗口内发。
   生产侧据此改成：拿不到 ticket 就不发，不再把错误藏进 `except: pass`。
2. HTTP 200 不等于成功 —— iLink 的业务错误走 `ret` / `errmsg`。
   本探针因此同时看 ret；生产侧已按这条修（`_api_post` 现在判 `ret`）。

## 投递已确认

验证过：消息不只是"发出去"，接收端确实收到了。
仍未测的边界：多久没说过话之后还能发 · 有没有频率限制 · 会不会被判骚扰。

## 三条纪律

1. 默认不联网 —— 不加 `--typing` / `--send` 只做 dry-run。
2. 先 typing 后 message —— sendtyping 对方看不见，是免费试探。
3. 不打印凭据 —— token 只打前后各 4 位。

输出全部 ASCII（Windows GBK 控制台，emoji 会让整个运行崩）。
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DEFAULT_CREDS = _ROOT / "data" / "wechat_credentials.json"
DEFAULT_BASE = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.0.3"

#: 三种字段名写法。A = 我们现在代码里的写法；B/C = iLink 报错里点名的那个。
SHAPES = ("A", "B", "C")


def _mask(s: str) -> str:
    s = str(s or "")
    return s if len(s) <= 10 else "%s...%s" % (s[:4], s[-4:])


def _uin() -> str:
    """与 `handler._random_wechat_uin` 逐字相同（base64 的 uint32）。"""
    return base64.b64encode(secrets.randbits(32).to_bytes(4, "big")).decode("ascii")


def _headers(token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": "Bearer %s" % token,
        "X-WECHAT-UIN": _uin(),
    }


def _typing_body(shape: str, to_user: str) -> dict:
    b: dict = {"context_token": ""}
    if shape == "A":
        b["to_user_id"] = to_user
    elif shape == "B":
        b["ilink_user_id"] = to_user
    else:
        b["ilink_user_id"] = to_user
        b["to_user_id"] = to_user
    return b


def _text_body(shape: str, to_user: str, text: str) -> dict:
    msg: dict = {
        "from_user_id": "",              # iLink 要求空串，省了会静默失败
        "client_id": uuid.uuid4().hex[:16],
        "message_type": 2,               # BOT
        "message_state": 2,              # FINISH
        "item_list": [{"type": 1, "text_item": {"text": text}}],
        "context_token": "",             # 被验的就是这一格
    }
    if shape == "A":
        msg["to_user_id"] = to_user
    elif shape == "B":
        msg["ilink_user_id"] = to_user
    else:
        msg["ilink_user_id"] = to_user
        msg["to_user_id"] = to_user
    return {"msg": msg, "base_info": {"channel_version": CHANNEL_VERSION}}


def _post(base: str, path: str, body: dict, headers: dict, timeout: float) -> dict:
    """发一次。返回 {'http': int|None, 'ok': bool, 'raw': str}。永不抛。"""
    try:
        import httpx
    except Exception as exc:                                  # noqa: BLE001
        print("  [x] no httpx: %s" % exc)
        return {"http": None, "ok": False, "raw": ""}
    url = "%s/%s" % (base.rstrip("/"), path)
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(url, json=body, headers=headers)
    except Exception as exc:                                  # noqa: BLE001
        print("  [x] connect failed: %s: %s" % (type(exc).__name__, exc))
        return {"http": None, "ok": False, "raw": ""}
    text = (r.text or "").strip()
    print("  <- HTTP %s  %s" % (r.status_code, text[:400] if text else "(empty body)"))
    # 200 不等于成功：真正的判据是 ret 字段
    ok = False
    try:
        obj = json.loads(text) if text else {}
        if isinstance(obj, dict):
            ok = (r.status_code == 200 and obj.get("ret", 0) in (0, None)
                  and not obj.get("errmsg"))
    except Exception:                                         # noqa: BLE001
        ok = r.status_code == 200
    return {"http": r.status_code, "ok": ok, "raw": text}


def main() -> int:
    ap = argparse.ArgumentParser(description="wechat ilink proactive push probe")
    ap.add_argument("--creds", default=str(DEFAULT_CREDS))
    ap.add_argument("--to", default="", help="target openid; default = creds.ilink_user_id")
    ap.add_argument("--text", default="[probe] proactive message, no context.")
    ap.add_argument("--typing", action="store_true", help="send typing (invisible)")
    ap.add_argument("--send", action="store_true", help="actually send text")
    ap.add_argument("--shape", default="", choices=("",) + SHAPES,
                    help="field name shape; default = try all")
    ap.add_argument("--timeout", type=float, default=20.0)
    a = ap.parse_args()

    p = Path(a.creds)
    if not p.exists():
        print("[x] no credentials: %s (scan QR in settings first)" % p)
        return 2
    try:
        creds = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:                                  # noqa: BLE001
        print("[x] credentials unreadable: %s: %s" % (type(exc).__name__, exc))
        return 2
    token = creds.get("bot_token") or ""
    if not token:
        print("[x] credentials have no bot_token")
        return 2

    base = creds.get("base_url") or DEFAULT_BASE
    to_user = a.to or creds.get("ilink_user_id") or ""

    print("=" * 62)
    print("wechat ilink proactive push probe")
    print("=" * 62)
    print("  base_url   : %s" % base)
    print("  bot        : %s" % _mask(creds.get("ilink_bot_id")))
    print("  bot_token  : %s" % _mask(token))
    print("  to_user_id : %s" % _mask(to_user))
    if not to_user:
        print("  [!] no target openid -- pass --to")
        return 2

    shapes = (a.shape,) if a.shape else SHAPES
    print("\n-- bodies (shape: field used) --")
    for sh in shapes:
        print("  [%s] typing : %s" % (sh, json.dumps(_typing_body(sh, to_user), ensure_ascii=False)))
        print("  [%s] message: %s" % (sh, json.dumps(_text_body(sh, to_user, a.text), ensure_ascii=False))[:260])

    if not a.typing and not a.send:
        print("\n[dry-run] nothing sent. add --typing to probe, --send to really send.")
        return 0

    headers = _headers(token)
    good = ""
    if a.typing:
        print("\n-- 1. sendtyping (invisible to the recipient) --")
        for sh in shapes:
            print("  ---- shape %s ----" % sh)
            r = _post(base, "ilink/bot/sendtyping", _typing_body(sh, to_user), headers, a.timeout)
            if r["ok"]:
                good = sh
                print("  [ok] shape %s accepted by the API" % sh)
                break
        if not good:
            print("  [x] no shape accepted sendtyping")

    if a.send:
        use = a.shape or good or "A"
        print("\n-- 2. sendmessage (you WILL receive it in WeChat) --")
        print("  using shape %s" % use)
        r = _post(base, "ilink/bot/sendmessage", _text_body(use, to_user, a.text),
                  headers, a.timeout)
        print("  => %s" % ("ACCEPTED (proactive push works)" if r["ok"]
                           else "REJECTED (see raw JSON above)"))

    print("\nHow to read: HTTP 200 alone means nothing -- check ret/errmsg.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
