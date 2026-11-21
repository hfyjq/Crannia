"""`crannia channel`：通道账本的列表 / 增删改 / 默认，以及启停与扫码。

账本操作不 import `core`：两个安装各自更新，TUI 里没有那个包 ——
所以一律走一次子进程（与 `profile` 同一条路）。启停与扫码要求后端在跑，
它们是进程内的长活，命令行这边只递一句请求。

私聊与桌面是同一条会话（`workspace:private`），所以这里不建第二个对话视图；
通道跑起来的消息会进 TUI 正在用的那一段。
"""

from __future__ import annotations

import getpass
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .doctor import PREPARE, query
from .i18n import t
from .profile import endpoint

#: 平台名到通道 id。通道 id 是 `bus` 里的注册名，五个各有各的一段；
#: 映射只在这里写一次（界面那边的表各自独立，见 `packages/ui/src/channels-model.js`）。
CHANNEL_IDS: dict[str, str] = {
    "wechat": "wechat_channel",
    "feishu": "feishu_channel",
    "dingtalk": "dingtalk_channel",
    "telegram": "telegram_channel",
    "qq_bot": "qq_bot_channel",
}

#: 列表读的是账本 + 字段表 + 警告（警告文案由后端给，界面不另存一份）。
_LIST_CODE = PREPARE + (
    "import json\n"
    "from core import channel_configs as cc\n"
    "out = {p: {'fields': list(s['fields']), 'secret': list(s['secret']),\n"
    "            'default': cc.default_id(p), 'notice': cc.notice_for(p),\n"
    "            'entries': cc.entries_for(p)}\n"
    "       for p, s in cc.PLATFORMS.items()}\n"
    "print(json.dumps(out, ensure_ascii=False))\n"
)

_CREATE_CODE = PREPARE + (
    "import json\n"
    "from core import channel_configs as cc\n"
    "try:\n"
    "    out = cc.create(%r, %r, json.loads(%r))\n"
    "except Exception as exc:\n"
    "    out = {'error': str(exc)}\n"
    "print(json.dumps(out, ensure_ascii=False))\n"
)

_UPDATE_CODE = PREPARE + (
    "import json\n"
    "from core import channel_configs as cc\n"
    "try:\n"
    "    out = cc.update(%r, name=(%r or None), fields=json.loads(%r))\n"
    "except Exception as exc:\n"
    "    out = {'error': str(exc)}\n"
    "print(json.dumps(out, ensure_ascii=False))\n"
)

_DELETE_CODE = PREPARE + (
    "import json\n"
    "from core import channel_configs as cc\n"
    "try:\n"
    "    out = {'ok': cc.delete(%r)}\n"
    "except Exception as exc:\n"
    "    out = {'error': str(exc)}\n"
    "print(json.dumps(out, ensure_ascii=False))\n"
)

_DEFAULT_CODE = PREPARE + (
    "import json\n"
    "from core import channel_configs as cc\n"
    "try:\n"
    "    e = cc.entry_of(%r)\n"
    "    out = {'ok': False, 'missing': True}\n"
    "    if e:\n"
    "        cc.set_default(e['platform'], %r)\n"
    "        out = {'ok': True, 'platform': e['platform']}\n"
    "except Exception as exc:\n"
    "    out = {'ok': False, 'error': str(exc)}\n"
    "print(json.dumps(out, ensure_ascii=False))\n"
)

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_USAGE = """用法：
  crannia channel list                列出各通道的配置条目与警告
  crannia channel add <平台> [名字]   新建（凭据逐格输入）
  crannia channel config <名字|id>    更改条目（回车以取消更改）
  crannia channel remove <名字|id>    删除条目（若目标为默认条目，则应优先更换默认）
  crannia channel default <名字|id>   设为该平台的默认条目
  crannia channel start <平台>        启动一个通道
  crannia channel stop <平台>         停止一个通道
  crannia channel login wechat        微信扫码登录

平台：feishu / dingtalk / telegram / qq_bot（需要凭据）· wechat（扫码登录）
选项：--yes 直接确认已读接入警告（脚本用）"""


def read_ledger(python: Path, home: Path, *, query_fn=None) -> dict:
    """平台 -> 字段表 / 默认 id / 警告 / 条目。子进程读，不 import core。"""
    run = query_fn or (lambda code: query(python, home, code))
    return json.loads(run(_LIST_CODE))


def _entries(data: dict):
    for platform, spec in data.items():
        for entry in spec.get("entries") or []:
            yield platform, entry


def _resolve(data: dict, ref: str) -> tuple[tuple[str, dict] | None, str]:
    """按 id 或名字在全部平台里找一条。返回 `(平台, 条目)` 或 `(None, 一句话)`。"""
    key = ref.strip().lower()
    if not key:
        return None, t("请提供名字或 id")
    by_id = [(p, e) for p, e in _entries(data) if str(e.get("id") or "").lower() == key]
    if by_id:
        return by_id[0], ""
    same = [(p, e) for p, e in _entries(data) if str(e.get("name") or "").lower() == key]
    if len(same) == 1:
        return same[0], ""
    if len(same) > 1:
        return None, t("「%s」撞名了，用 id 指定：%s") % (
            ref, " · ".join("%s(%s)" % (e.get("name"), e.get("id"))
                            for _p, e in same))
    return None, t("没有「%s」。尝试运行 `crannia channel list`") % ref


def _masked_hint(entry: dict) -> str:
    fields = entry.get("fields") or {}
    for value in fields.values():
        if value:
            return str(value)
    return ""


def render(data: dict, running: dict | None = None) -> list[str]:
    """人看的列表。每条一行；平台那行挂接入警告的短标记。"""
    running = running or {}
    lines: list[str] = []
    for platform, spec in data.items():
        cid = CHANNEL_IDS.get(platform)
        state = t(" · 运行中") if cid and running.get(cid) else ""
        marker = (t(" [群聊未完善]")
                  if (spec.get("notice") or {}).get("key") else "")
        lines.append(t("%s（%s）%s%s")
                     % (platform, spec.get("default") or t("无默认"),
                        state, marker))
        rows = spec.get("entries") or []
        if not rows:
            lines.append("  " + t("（未配置）"))
            continue
        for entry in rows:
            mark = "*" if entry.get("is_default") else " "
            hint = _masked_hint(entry)
            lines.append("  %s %-16s %-12s (%s)"
                         % (mark, entry.get("name") or t("(未命名)"),
                            hint or t("(无凭据)"), entry.get("id") or ""))
    return lines


def _notice_of(data: dict, platform: str) -> dict:
    return (data.get(platform) or {}).get("notice") or {}


def _confirm_notice(notice: dict, assume_yes: bool) -> bool:
    """配置通道前把接入警告摆出来并要一句确认。"""
    if not notice:
        return True
    print(t("注意：") + str(notice.get("text") or ""))
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(t("非交互环境：加 --yes 表示已读并继续。"))
        return False
    answer = _ask(t("是否继续？[y/N] "))
    return str(answer or "").strip().lower() in ("y", "yes", "是")


def _ask(prompt: str) -> str | None:
    """`input` 的一层薄壳：读到 EOF 给 `None` —— 管道里没有输入不是崩溃的事。"""
    try:
        return input(prompt)
    except EOFError:
        print()
        return None


def _ask_fields(spec: dict, *, keep: bool) -> dict | None:
    """逐格问凭据。密钥格不回显；回车 = 不填 / 不改。

    读不到输入给 `None`（与"一格都没填"分开 —— 后者是一份空 dict，
    调用方要说的话不一样）。
    """
    out: dict = {}
    for key in spec.get("fields") or []:
        prompt = (t("%s（回车不改）: ") if keep else "%s: ") % key
        if key in (spec.get("secret") or []):
            if not sys.stdin.isatty():
                print(t("密钥应在终端里输入。"))
                return None
            try:
                value = getpass.getpass(prompt)
            except EOFError:
                print()
                return None
        else:
            value = _ask(prompt)
            if value is None:
                return None
        value = value.strip()
        if value:
            out[key] = value
    return out


def _post(ep: dict, path: str, payload: dict) -> tuple[bool, str, dict]:
    """向端点 POST 一份 JSON。返回 `(成功, 一句话, 响应体)`。"""
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (ep["port"], path),
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"X-Crannia-Token": ep["token"],
                 "Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            body = {}
        return False, str(body.get("error") or "HTTP %s" % exc.code), body
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, t("后端无应答（%s）") % type(exc).__name__, {}
    if not isinstance(body, dict):
        return False, t("后端回复了未知内容"), {}
    if body.get("error"):
        return False, str(body["error"]), body
    return True, str(body.get("message") or ""), body


def _get(ep: dict, path: str) -> dict:
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (ep["port"], path),
        headers={"X-Crannia-Token": ep["token"]})
    try:
        with _OPENER.open(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _need_backend(ep) -> dict | None:
    if isinstance(ep, dict):
        return ep
    print(t("后端未启动"))
    return None


def _login_wechat(ep: dict, *, timeout_s: float = 180.0) -> int:
    ok, why, _body = _post(ep, "/api/wechat/start-login", {})
    if not ok:
        print(why or t("启动微信登录失败"))
        return 1
    printed = False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        state = _get(ep, "/api/wechat/qrcode")
        phase = str(state.get("phase") or "")
        if phase == "qr_ready":
            if not printed:
                art = str(state.get("ascii") or "")
                if art:
                    print(art, end="" if art.endswith("\n") else "\n")
                print(t("请使用手机微信扫码继续…"))
                printed = True
        elif phase == "scanned":
            print(t("已扫码，请在手机上确认…"))
            printed = True
        elif phase == "confirmed":
            print(t("微信已登录。"))
            return 0
        elif phase == "expired":
            print(t("二维码过期，请再次运行 `crannia channel login wechat`。"))
            return 1
        elif phase == "error":
            print(t("登录失败：%s")
                  % (state.get("error_msg") or t("原因未知")))
            return 1
        time.sleep(1.0)
    print(t("等超时了（%s 秒）—— 再执行一次。") % int(timeout_s))
    return 1


#: `channel_command` 的默认端点参数："没传"与"明说没有后端"是两件事 ——
#: 前者才去探一次，后者按"后端没在跑"处理（测试要能钉后一种）。
_UNSET = object()


def channel_command(argv: list[str], *, python: Path, home: Path,
                    query_fn=None, ep=_UNSET) -> int:
    """`crannia channel ...` 的入口。返回退出码。"""
    if not argv:
        print(t(_USAGE))
        return 0
    args = list(argv)
    assume_yes = "--yes" in args
    args = [a for a in args if a != "--yes"]
    action = args[0]
    rest = args[1:]

    if action in ("-h", "--help", "help"):
        print(t(_USAGE))
        return 0

    if action == "list":
        data = read_ledger(python, home, query_fn=query_fn)
        running: dict = {}
        ep = endpoint(python, home) if ep is _UNSET else ep
        if isinstance(ep, dict):
            running = (_get(ep, "/api/channel/status") or {}).get("running") or {}
        for line in render(data, running):
            print(line)
        return 0

    if action == "add":
        if not rest:
            print(t("需要平台名：") + " / ".join(sorted(
                list(CHANNEL_IDS) + ["qq_bot"])))
            return 2
        platform = rest[0]
        name = rest[1] if len(rest) > 1 else t("默认")
        data = read_ledger(python, home, query_fn=query_fn)
        spec = data.get(platform)
        if spec is None:
            print(t("未知平台：%s（有：%s）")
                  % (platform, " / ".join(sorted(data))))
            return 2
        if not _confirm_notice(spec.get("notice") or {}, assume_yes):
            print(t("已取消。"))
            return 1
        fields = _ask_fields(spec, keep=False)
        if not fields:
            print(t("未更改"))
            return 1
        run = query_fn or (lambda code: query(python, home, code))
        row = json.loads(run(_CREATE_CODE % (
            platform, name, json.dumps(fields, ensure_ascii=False))))
        if row.get("error"):
            print(row["error"])
            return 1
        print(t("已新建：「%s」(%s)")
              % (row.get("name") or name, row.get("id") or ""))
        return 0

    if action == "config":
        if not rest:
            print(t("请提供一个名字或 id"))
            return 2
        data = read_ledger(python, home, query_fn=query_fn)
        hit, why = _resolve(data, rest[0])
        if hit is None:
            print(why)
            return 1
        platform, entry = hit
        if not _confirm_notice(_notice_of(data, platform), assume_yes):
            print(t("已取消。"))
            return 1
        fields = _ask_fields(data[platform], keep=True)
        run = query_fn or (lambda code: query(python, home, code))
        row = json.loads(run(_UPDATE_CODE % (
            entry["id"], "", json.dumps(fields, ensure_ascii=False))))
        if row.get("error"):
            print(row["error"])
            return 1
        print(t("已更新：「%s」(%s)")
              % (row.get("name") or entry.get("name"),
                 entry.get("id") or ""))
        return 0

    if action == "remove":
        if not rest:
            print(t("请提供一个名字或 id"))
            return 2
        data = read_ledger(python, home, query_fn=query_fn)
        hit, why = _resolve(data, rest[0])
        if hit is None:
            print(why)
            return 1
        _platform, entry = hit
        if not assume_yes:
            if not sys.stdin.isatty():
                print(t("非交互环境：加 --yes 确认删除。"))
                return 1
            prompt = t("删除「%s」？[y/N] ") % (
                entry.get("name") or entry.get("id"))
            if input(prompt).strip().lower() not in ("y", "yes", "是"):
                print(t("已取消。"))
                return 1
        run = query_fn or (lambda code: query(python, home, code))
        row = json.loads(run(_DELETE_CODE % (entry["id"],)))
        if row.get("error"):
            print(row["error"])
            return 1
        print(t("已删除：%s") % (entry.get("name") or entry.get("id")))
        return 0

    if action == "default":
        if not rest:
            print(t("请提供一个名字或 id"))
            return 2
        data = read_ledger(python, home, query_fn=query_fn)
        hit, why = _resolve(data, rest[0])
        if hit is None:
            print(why)
            return 1
        _platform, entry = hit
        run = query_fn or (lambda code: query(python, home, code))
        row = json.loads(run(_DEFAULT_CODE % (entry["id"], entry["id"])))
        if not row.get("ok"):
            print(t("配置丢失") if row.get("missing")
                  else (row.get("error") or t("失败")))
            return 1
        print(t("已设为默认：「%s」。重启以更换运行中的通道。")
              % (entry.get("name") or entry.get("id")))
        return 0

    if action in ("start", "stop"):
        if not rest:
            print(t("需要平台名：") + " / ".join(sorted(CHANNEL_IDS)))
            return 2
        channel_id = CHANNEL_IDS.get(rest[0])
        if channel_id is None:
            print(t("未知平台：%s（有：%s）")
                  % (rest[0], " / ".join(sorted(CHANNEL_IDS))))
            return 2
        if ep is _UNSET:
            ep = endpoint(python, home)
        ep = _need_backend(ep)
        if ep is None:
            return 2
        path = "/api/channel/%s" % ("start" if action == "start" else "stop")
        ok, why, body = _post(ep, path, {"channel_id": channel_id})
        if not ok:
            print(why or (t("启动失败") if action == "start"
                          else t("停止失败")))
            return 1
        print(str(body.get("message") or body.get("status")
                  or (t("已启动") if action == "start" else t("已停止"))))
        return 0

    if action == "login":
        if not rest or rest[0] != "wechat":
            print(t("目前只有 `crannia channel login wechat`。"))
            return 2
        if ep is _UNSET:
            ep = endpoint(python, home)
        ep = _need_backend(ep)
        if ep is None:
            return 2
        return _login_wechat(ep)

    print(t(_USAGE))
    return 2
