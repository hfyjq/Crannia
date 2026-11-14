#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QQ 机器人联调探针：把通道要走的几条路一次走完，哪一步断在哪一行。

    .venv/Scripts/python.exe scripts/probe_qq_bot.py
    .venv/Scripts/python.exe scripts/probe_qq_bot.py --listen 120
    .venv/Scripts/python.exe scripts/probe_qq_bot.py --app-id 1 --app-secret 2 --sandbox
    .venv/Scripts/python.exe scripts/probe_qq_bot.py --c2c-openid <openid> --text "你好"
    .venv/Scripts/python.exe scripts/probe_qq_bot.py --c2c-openid <openid> --file report.docx

四步读，与 `channels/qq_bot_channel/handler.py` 用的是同一套请求形状：

    1. 换 access_token   —— 验 appId / appSecret
    2. GET /users/@me    —— 验 token 与网络（这一步过了，发消息就不会是 401）
    3. GET /gateway      —— 验网关地址
    4. WS 认证 + 心跳     —— 验 intents 与鉴权帧

`--listen N` 会在第 4 步之后继续挂着 N 秒，把收到的事件类型打出来。
「群里不 @ 也能收吗」这条待验项就是靠它看：不 @ 时说一句话，
这里没有 `GROUP_AT_MESSAGE_CREATE`，就说明平台侧没开。
私聊发一句话，这里打出来的 `C2C_MESSAGE_CREATE` 里就有 `c2c-openid`。

`--c2c-openid` 加两步写（不发就是只读）：

    5. 发一条文本        —— 验 `/v2/users/<openid>/messages` 与主动消息配额
    6. 上传 + 发文件     —— 验素材上传的请求体，再发 `msg_type=7`

第 6 步要 `--file`。上传那条此前只按官方 v2 的形状写过、没在真机上跑通过；
它失败时会把被拒的响应体原样打出来。

凭据默认读 `data/qq_bot_credentials.json`（与通道同一份）。脚本自己不写
任何文件；`--listen` 期间收到的消息与第 5/6 步发出的消息都不进管线。

退出码：0 全过 · 1 某一步失败 · 2 没有凭据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from channels.qq_bot_channel.handler import (          # noqa: E402
    DEFAULT_API_BASE,
    DEFAULT_TOKEN_URL,
    INTENT_GROUP_AND_C2C,
    QQBotChannel,
    _load_credentials,
)

SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"


def _say(step: str, ok: bool, detail: str = "") -> None:
    mark = "[ok]" if ok else "[!!]"
    print("%s %-22s %s" % (mark, step, detail))


async def _probe(args) -> int:
    import aiohttp

    from core import net_proxy as _np
    mode, url = _np.state()
    if mode == "manual":
        proxy = url or None
    elif mode == "direct":
        proxy = None
    else:
        import os
        proxy = os.environ.get("HTTPS_PROXY") or None

    # 三个地址都留了口子：本地假服务端那一趟靠它跑通整条协议，
    #   而不用把真凭据借出来试。
    base = (args.base_url or
            (SANDBOX_API_BASE if args.sandbox else DEFAULT_API_BASE)).rstrip("/")
    token_url = args.token_url or DEFAULT_TOKEN_URL
    print("=" * 66)
    print("QQ bot probe — base=%s proxy=%s" % (base, "yes" if proxy else "no"))
    print("=" * 66)

    async with aiohttp.ClientSession() as session:
        # 1. access_token
        try:
            async with session.post(
                    token_url,
                    json={"appId": args.app_id, "clientSecret": args.app_secret},
                    proxy=proxy, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                data = await resp.json(content_type=None)
        except Exception as exc:                          # noqa: BLE001
            _say("1 access_token", False, "%s: %s" % (type(exc).__name__, exc))
            return 1
        token = str((data or {}).get("access_token") or "")
        _say("1 access_token", bool(token),
             "expires_in=%s" % (data or {}).get("expires_in") if token
             else json.dumps(data, ensure_ascii=False)[:200])
        if not token:
            return 1

        headers = {"Authorization": "QQBot %s" % token}

        # 2. 机器人自己的身份
        try:
            async with session.get(base + "/users/@me", headers=headers,
                                   proxy=proxy, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                me_status = resp.status
                me = await resp.json(content_type=None)
        except Exception as exc:                          # noqa: BLE001
            _say("2 users/@me", False, "%s: %s" % (type(exc).__name__, exc))
            return 1
        ok = me_status == 200
        _say("2 users/@me", ok, "username=%s id=%s" % (
            (me or {}).get("username"), (me or {}).get("id")) if ok
             else "HTTP %d: %s" % (me_status, json.dumps(me, ensure_ascii=False)[:200]))
        if not ok:
            return 1

        # 3. 网关地址
        try:
            async with session.get(base + "/gateway", headers=headers,
                                   proxy=proxy, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                gw_status = resp.status
                gw = await resp.json(content_type=None)
        except Exception as exc:                          # noqa: BLE001
            _say("3 gateway", False, "%s: %s" % (type(exc).__name__, exc))
            return 1
        gw_url = str((gw or {}).get("url") or "")
        _say("3 gateway", gw_status == 200 and bool(gw_url),
             gw_url or "HTTP %d: %s" % (gw_status,
                                        json.dumps(gw, ensure_ascii=False)[:200]))
        if not gw_url:
            return 1

        # 4. WS 认证
        print("\n-- 4 WebSocket --")
        print("   intents = %d（群与私聊那一位）" % INTENT_GROUP_AND_C2C)
        rc = await _ws_probe(session, gw_url, token, proxy, args.listen)
        if rc != 0:
            return rc
        return await _outbound_probe(session, base, token, proxy, args)


async def _post_json(session, url: str, token: str, payload: dict, proxy):
    """POST 一条，返回 (状态码, 响应文本)。超时与网络错都折进状态码的调用方。"""
    import aiohttp

    headers = {"Authorization": "QQBot %s" % token,
               "Content-Type": "application/json"}
    async with session.post(url, json=payload, headers=headers, proxy=proxy,
                            timeout=aiohttp.ClientTimeout(total=60)) as resp:
        return resp.status, await resp.text()


async def _outbound_probe(session, base: str, token: str, proxy, args) -> int:
    """第 5 / 6 步：主动发一条文本，可选地上传一个文件再发出去。

    没有 `--c2c-openid` 就什么都不做（默认仍是只读那一趟）。
    """
    target = str(getattr(args, "c2c_openid", "") or "").strip()
    if not target:
        return 0

    print("\n-- 5 发一条文本（主动消息）--")
    text = str(getattr(args, "text", "") or "") or "Crannia 联调探针：这是一条测试消息"
    url = "%s/v2/users/%s/messages" % (base, target)
    try:
        status, body = await _post_json(
            session, url, token, QQBotChannel.message_body(text), proxy)
    except Exception as exc:                              # noqa: BLE001
        _say("5 发文本", False, "%s: %s" % (type(exc).__name__, exc))
        return 1
    ok = status in (200, 201, 202)
    _say("5 发文本", ok, "HTTP %d %s" % (status, body[:200]))
    if not ok:
        return 1

    file_path = str(getattr(args, "file", "") or "").strip()
    if not file_path:
        return 0

    print("\n-- 6 上传 + 发文件 --")
    path = Path(file_path)
    if not path.is_file():
        _say("6 上传文件", False, "文件不存在：%s" % file_path)
        return 1
    upload_url = "%s/v2/users/%s/files" % (base, target)
    try:
        payload = QQBotChannel.file_upload_payload(path.read_bytes(), path.name)
        status, body = await _post_json(session, upload_url, token, payload, proxy)
    except Exception as exc:                              # noqa: BLE001
        _say("6 上传文件", False, "%s: %s" % (type(exc).__name__, exc))
        return 1
    if status not in (200, 201, 202):
        #: 被拒的响应体原样打出来 —— 这条路此前没在真机上跑通过一次，
        #: 缺的正是这一句平台自己说的话。
        _say("6 上传文件", False, "HTTP %d %s" % (status, body[:400]))
        return 1
    try:
        import json as _json
        file_info = str((_json.loads(body) or {}).get("file_info") or "")
    except Exception:                                     # noqa: BLE001
        file_info = ""
    _say("6 上传文件", bool(file_info),
         "file_info=%s" % (file_info[:40] or "(响应里没有)"))
    if not file_info:
        return 1

    try:
        status, body = await _post_json(
            session, url, token,
            QQBotChannel.media_message_body(file_info), proxy)
    except Exception as exc:                              # noqa: BLE001
        _say("6 发文件消息", False, "%s: %s" % (type(exc).__name__, exc))
        return 1
    ok = status in (200, 201, 202)
    _say("6 发文件消息", ok, "HTTP %d %s" % (status, body[:200]))
    return 0 if ok else 1


async def _ws_probe(session, url: str, token: str, proxy, listen: float) -> int:
    import aiohttp

    seq = None
    identified = False
    ready = False
    heartbeat: asyncio.Task | None = None
    deadline = None

    try:
        ws = await session.ws_connect(url, proxy=proxy, heartbeat=None)
    except Exception as exc:                              # noqa: BLE001
        _say("4 ws connect", False, "%s: %s" % (type(exc).__name__, exc))
        return 1

    print("   [ok] 连上了 %s" % url)

    async def _hb(interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send_str(json.dumps({"op": 1, "d": seq}))
        except asyncio.CancelledError:
            raise
        except Exception:                                 # noqa: BLE001
            pass

    try:
        while True:
            if deadline is not None and time.monotonic() > deadline:
                break
            try:
                frame = await asyncio.wait_for(ws.receive(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if frame.type != aiohttp.WSMsgType.TEXT:
                if frame.type in (aiohttp.WSMsgType.CLOSED,
                                  aiohttp.WSMsgType.ERROR):
                    print("   [!!] 连接被关掉（type=%s）" % frame.type)
                    break
                continue
            try:
                payload = json.loads(frame.data)
            except json.JSONDecodeError:
                continue
            op = payload.get("op")
            data = payload.get("d") or {}

            if op == 10:
                interval = float(data.get("heartbeat_interval") or 30000) / 1000.0
                print("   [ok] hello，心跳间隔 %.1f 秒" % interval)
                await ws.send_str(json.dumps(QQBotChannel.identify_payload(token)))
                identified = True
                heartbeat = asyncio.create_task(_hb(interval))
                if listen > 0:
                    deadline = time.monotonic() + listen
                    print("   [..] 开始监听 %.0f 秒，现在去 QQ 里给机器人发消息" % listen)
                else:
                    # 不收事件时，等 READY 或等够 15 秒就收工。
                    deadline = time.monotonic() + 15
                continue
            if op == 11:
                continue
            if op == 9:
                print("   [!!] 平台判鉴权失败（op 9），token 或 intents 不对")
                return 1
            if op == 7:
                print("   [..] 平台要求重连")
                break
            if op != 0:
                continue

            seq = payload.get("s")
            event = str(payload.get("t") or "")
            if event == "READY":
                ready = True
                _say("4 ws identify", True, "READY")
                if listen <= 0:
                    break
                continue
            if event == "RESUMED":
                _say("4 ws resume", True, "RESUMED")
                continue

            # 其余事件原样报类型，正文只截一小段。
            preview = json.dumps(data, ensure_ascii=False)[:160]
            print("   [msg] %s  %s" % (event, preview))
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
        try:
            await ws.close()
        except Exception:                                 # noqa: BLE001
            pass

    if not identified:
        _say("4 ws identify", False, "没收到 hello，也就没发认证帧")
        return 1
    if not ready:
        _say("4 ws identify", False, "认证帧发了，但没等到 READY")
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="QQ 机器人联调探针（默认只读；给 --c2c-openid 才发消息）")
    ap.add_argument("--app-id", default="")
    ap.add_argument("--app-secret", default="")
    ap.add_argument("--sandbox", action="store_true",
                    help="用沙箱地址 sandbox.api.sgroup.qq.com")
    ap.add_argument("--base-url", default="", help=argparse.SUPPRESS)
    ap.add_argument("--token-url", default="", help=argparse.SUPPRESS)
    ap.add_argument("--listen", type=float, default=0.0,
                    help="认证之后继续挂着多少秒，把收到的事件打出来")
    ap.add_argument("--c2c-openid", default="",
                    help="给这个私聊发一条消息（不给就是只读那一趟）")
    ap.add_argument("--text", default="",
                    help="第 5 步发的内容（默认一句测试消息）")
    ap.add_argument("--file", default="",
                    help="第 6 步要上传并发出去的文件")
    args = ap.parse_args(argv)

    if not (args.app_id and args.app_secret):
        creds = _load_credentials()
        args.app_id = str(creds.get("app_id") or "")
        args.app_secret = str(creds.get("app_secret") or "")

    if not (args.app_id and args.app_secret):
        print("[!!] 没有凭据：data/qq_bot_credentials.json 里没有 app_id / app_secret，"
              "命令行也没给")
        print("     要么先在设置 → 通道 → QQ 里填一次，要么用 --app-id / --app-secret")
        return 2

    try:
        return asyncio.run(_probe(args))
    except KeyboardInterrupt:
        print("\n[..] 手动停了")
        return 0


if __name__ == "__main__":
    sys.exit(main())
