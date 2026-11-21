"""`crannia setup`：首次引导 —— 配一条能用的模型 API。

引导按这个顺序走：

  1. 选提供商卡（决定默认地址；回车 = 「其他」/ OpenAI 兼容，选它才问地址）
  2. 配置名称 → 模型名 → API Key（掩码输入）
  3. 自动联通测试（一次最小请求，短超时）
     成功：「已确认」；失败：保留已填值，只让人改错的那一项

不 import `core`：与 `profile` / `channel` 同一条路，账本操作走一次子进程。
这条命令要终端（密钥要从键盘读），非交互环境直接拒。
"""

from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path

from .doctor import PREPARE, query
from .i18n import t

#: 每张卡的默认值（名称 / 地址 / 默认模型）全在后端读 manifest ——
#: 界面不抄一份（抄了就是第二套真相，卡片一改就漂）。
_CARDS_CODE = (
    "import json\n"
    "from core import model_configs as mc\n"
    "print(json.dumps(mc.card_defaults(), ensure_ascii=False))\n")

_STATE_CODE = PREPARE + (
    "import json\n"
    "from core import model_configs as mc\n"
    "cid = mc.default_id()\n"
    "e = mc.get(cid) if cid else None\n"
    "print(json.dumps({'default': cid,"
    " 'name': (e or {}).get('name', ''),"
    " 'model': (e or {}).get('model', '')}, ensure_ascii=False))\n")

_CREATE_CODE = PREPARE + (
    "import json\n"
    "from core import model_configs as mc\n"
    "try:\n"
    "    row = mc.create(card=%r, name=%r, model=%r, base_url=%r, api=%r)\n"
    "    out = {'ok': True, 'id': row.get('id')}\n"
    "except Exception as exc:\n"
    "    out = {'ok': False, 'error': str(exc)}\n"
    "print(json.dumps(out, ensure_ascii=False))\n")

_UPDATE_CODE = PREPARE + (
    "import json\n"
    "from core import model_configs as mc\n"
    "try:\n"
    "    mc.update(%r, **json.loads(%r))\n"
    "    out = {'ok': True}\n"
    "except Exception as exc:\n"
    "    out = {'ok': False, 'error': str(exc)}\n"
    "print(json.dumps(out, ensure_ascii=False))\n")

_DEFAULT_CODE = PREPARE + (
    "import json\n"
    "from core import model_configs as mc\n"
    "try:\n"
    "    mc.set_default(%r)\n"
    "    out = {'ok': True}\n"
    "except Exception as exc:\n"
    "    out = {'ok': False, 'error': str(exc)}\n"
    "print(json.dumps(out, ensure_ascii=False))\n")

_PROBE_CODE = PREPARE + (
    "import json\n"
    "from core import model_configs as mc\n"
    "ok, kind, why = mc.probe(%r)\n"
    "print(json.dumps({'ok': ok, 'kind': kind, 'error': why},"
    " ensure_ascii=False))\n")

#: 联通测试给多长。上游默认超时是 120 秒（`providers/openai_compat.py` 的
#: httpx 客户端），引导里等不了那么久 —— 子进程整体掐在这里。
PROBE_TIMEOUT = 25.0

#: 有后端在跑时走它的端点（`model_configs` 有内存镜像，在别的进程里直接改文件
#: 会让它的镜像与磁盘打架 —— `profile` 早就按这条判据走了，本命令跟上）。
def _live_endpoint(python: Path, home: Path):
    try:
        from .profile import endpoint
        return endpoint(python, home)
    except Exception:  # noqa: BLE001 - 探测失败就当没有
        return None


def _post(ep, path: str, payload: dict) -> dict:
    from .profile import http_json

    got = http_json(ep, path, payload)
    if not isinstance(got, dict):
        return {"ok": False, "error": t("后端未响应，未更改任何配置")}
    return got


def _create_config(python: Path, home: Path, ep, values: dict, *, query_fn=None) -> dict:
    if ep is not None:
        got = _post(ep, "/api/model-configs/create", values)
        entry = got.get("entry") or {}
        if got.get("ok") and entry.get("id"):
            return {"ok": True, "id": str(entry["id"])}
        return {"ok": False, "error": got.get("error") or t("配置设置失败")}
    return _run(python, home, _CREATE_CODE % (
        values["card"], values["name"], values["model"],
        values["base_url"], values["api"]), query_fn=query_fn)


def _update_config(python: Path, home: Path, ep, cid: str, fields: dict,
                   *, query_fn=None) -> dict:
    if ep is not None:
        return _post(ep, "/api/model-configs/update",
                     {"id": cid, **fields})
    return _run(python, home, _UPDATE_CODE % (
        cid, json.dumps(fields, ensure_ascii=False)), query_fn=query_fn)


def _set_default(python: Path, home: Path, ep, cid: str, *, query_fn=None) -> dict:
    if ep is not None:
        return _post(ep, "/api/model-configs/default", {"id": cid})
    return _run(python, home, _DEFAULT_CODE % (cid,), query_fn=query_fn)

#: 失败类别 -> (给人看的标签, 建议先改哪一项)。
#: 类别来自 `core.model_configs.classify_probe_error`（认证 / 限流那一套判据
#: 复用 `providers/base.py`，全仓只有一份）。
_ERROR_HINTS: dict[str, tuple[str, str]] = {
    "auth": ("密钥被拒绝", "3"),
    "balance": ("账户余额不足", "3"),
    "rate_limit": ("API已被限流，请稍后重试", "0"),
    "region": ("地区受限", "1"),
    "content": ("请求被内容审核拦下", "0"),
    "too_large": ("请求体被拒", "2"),
    "context_overflow": ("上下文超长", "2"),
    "request": ("请求被拒", "2"),
    "server_fault": ("上游故障", "0"),
    "server_busy": ("上游繁忙", "0"),
    "server_other": ("上游异常", "0"),
    "timeout": ("连接错误", "1"),
    "connection": ("连接错误", "1"),
    "model": ("模型名错误", "2"),
    "address": ("地址错误", "1"),
    "install": ("配置错误", "0"),
    "unknown": ("未知错误", "1"),
}


def _hint(kind: str) -> tuple[str, str]:
    label, item = _ERROR_HINTS.get(kind, _ERROR_HINTS["unknown"])
    return t(label), item


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        print()
        return ""


def _secret(prompt: str) -> str:
    try:
        return getpass.getpass(prompt)
    except EOFError:
        print()
        return ""


def _run(python: Path, home: Path, code: str, *, timeout: float = 25.0,
         query_fn=None) -> dict:
    run = query_fn or (lambda c, timeout=timeout: query(python, home, c,
                                                        timeout=timeout))
    return json.loads(run(code, timeout=timeout))


def needs_setup(python: Path, home: Path, *, query_fn=None) -> bool:
    """还没有默认模型配置 —— 进界面前该先走引导。

    查不出来就给 `False`（不挡启动；`doctor` 会把真实原因说出来）。
    """
    try:
        state = _run(python, home, _STATE_CODE, query_fn=query_fn)
    except Exception:                                # noqa: BLE001
        return False
    return not bool(state.get("default"))


def _pick_card(cards: list[dict], ask) -> dict:
    """选卡。回车 = 「其他」（OpenAI 兼容）。"""
    default_id = "other" if any(c["id"] == "other" for c in cards) else \
        (cards[0]["id"] if cards else "")
    while True:
        answer = (ask(t("输入对应编号以开始配置（直接回车 = 其他 / OpenAI 兼容）: "))
                  or "").strip()
        if not answer:
            for c in cards:
                if c["id"] == default_id:
                    return c
            return cards[0] if cards else {}
        if answer.isdigit():
            n = int(answer)
            if 1 <= n <= len(cards):
                return cards[n - 1]
        for c in cards:
            if answer.lower() in (c["id"].lower(), str(c["label"]).lower()):
                return c
        print(t("没有「%s」。请填写编号或直接回车。") % answer)


def _probe(python: Path, home: Path, cid: str, *, query_fn=None) -> dict:
    try:
        return _run(python, home, _PROBE_CODE % (cid,),
                    timeout=PROBE_TIMEOUT, query_fn=query_fn)
    except Exception as exc:                         # noqa: BLE001
        return {"ok": False, "kind": "unknown",
                "error": t("测试未完成：%s") % exc}


def _fix_loop(python: Path, home: Path, cid: str, *, ask, secret,
              ep=None, query_fn=None, suggest: str = "1") -> bool:
    """联通测试失败后"只改错的那一项"，直到通过或用户选择跳过。

    *suggest* 是第一次的默认项（由失败类别来）；之后每失败一次都按新类别更新。
    """
    rep: dict | None = None
    while True:
        if rep is not None:
            #: 与第一次失败**同一个呈现** —— 不另起一种"还是不通"的口吻。
            label, suggest = _hint(str(rep.get("kind") or ""))
            print(t("API 异常（%s）：%s")
                  % (label, rep.get("error") or t("未知错误")))
        choice = (ask(
            t("选择更改条目（直接回车以更改推荐条目）  "
              "1 地址 · 2 模型 · 3 密钥 · 0 保留并退出 [%s]: ") % suggest)
            or "").strip() or suggest
        if choice == "0":
            return False
        fields: dict = {}
        if choice == "1":
            value = (ask(t("新的请求地址: ")) or "").strip()
            if value:
                fields["base_url"] = value
        elif choice == "2":
            value = (ask(t("新的模型名: ")) or "").strip()
            if value:
                fields["model"] = value
        elif choice == "3":
            fields["api"] = secret(t("新的 API Key: "))
        else:
            continue
        if not fields:
            continue
        try:
            upd = _update_config(python, home, ep, cid, fields,
                                 query_fn=query_fn)
        except Exception as exc:                     # noqa: BLE001
            print(t("未更改：%s") % exc)
            continue
        if not upd.get("ok"):
            print(upd.get("error") or t("未更改"))
            continue
        rep = _probe(python, home, cid, query_fn=query_fn)
        if rep.get("ok"):
            return True


def setup_command(*, python: Path, home: Path, query_fn=None,
                  ask=None, secret=None, interactive: bool | None = None) -> int:
    """`crannia setup` 的入口。返回退出码。"""
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive and ask is None:
        print(t("setup 需要在终端中启用，"
                "请在 Windows Terminal 里执行 `crannia setup`。"))
        return 2
    ask_ = ask or _ask
    sec_ = secret or _secret
    ep = _live_endpoint(python, home)

    try:
        state = _run(python, home, _STATE_CODE, query_fn=query_fn)
    except Exception as exc:                         # noqa: BLE001
        print(t("模型配置读取失败：%s") % exc)
        return 2

    if state.get("default"):
        print(t("默认：「%s」（模型 %s）")
              % (state.get("name") or state["default"],
                 state.get("model") or t("？（未填写）")))
        if (ask_(t("重新配置？[y/N] ")) or "").strip().lower() not in (
                "y", "yes", "是"):
            return 0

    try:
        cards = _run(python, home, _CARDS_CODE, query_fn=query_fn)
    except Exception as exc:                         # noqa: BLE001
        print(t("供应商读取错误：%s") % exc)
        return 2
    if not isinstance(cards, list) or not cards:
        print(t("供应商错误，此程序遇到了一些问题，请先运行 `crannia doctor`尝试修复"))
        return 2

    print(t("欢迎！看起来这是您的首次使用，让我们从配置您的首个API开始"))
    ask_(t("按下enter以继续…"))
    print(t("模型列表："))
    for i, c in enumerate(cards, 1):
        #: 空地址 = 由 SDK 决定（Anthropic 原生 SDK 自己补
        #: `https://api.anthropic.com` + `/v1/messages`）；留空会看起来像坏了。
        #: 「其他」不显示地址 —— 它的地址在选中之后才问。
        if c.get("base_url"):
            suffix = "  " + c["base_url"]
        elif c["id"] == "other":
            suffix = ""
        else:
            suffix = t("（地址由 SDK 决定）")
        print("   %2d. %s%s" % (i, c["label"], suffix))
    card = _pick_card(cards, ask_)

    name = (ask_(t("配置名称 [eg：%s]: ") % card["label"]) or "").strip() \
        or card["label"]
    #: 「其他」没有自己的 manifest —— 它的默认值是 OpenAI 那一份，拿它当提示
    #: 会把人往错的方向带（那家的模型名与地址都不同）。
    hint_model = "" if card["id"] == "other" else card.get("default_model", "")
    #: 例子前面写 `eg：`，否则方括号里的字看起来像"只能填这个"。
    #: 「其他」没有例子可给，就不摆一对空括号。
    model_prompt = (t("模型名 [eg：%s]: ") % hint_model
                    if hint_model else t("模型名: "))
    model = (ask_(model_prompt) or "").strip() or hint_model
    if not model:
        print(t("模型名不能为空"))
        return 1

    base = str(card.get("base_url") or "")
    if card["id"] == "other":
        base = (ask_(t("请求地址（例如 https://…/v1）: "))
                or "").strip()
        if not base:
            print(t("「其他」需要一个请求地址。"))
            return 1

    api = (sec_(t("API Key（此字段不可见，请直接粘贴；本地模型可直接回车跳过）: "))
           or "").strip()

    try:
        created = _create_config(
            python, home, ep,
            {"card": card["id"], "name": name, "model": model,
             "base_url": base, "api": api},
            query_fn=query_fn)
    except Exception as exc:                         # noqa: BLE001
        print(t("配置失败：%s") % exc)
        return 1
    if not created.get("ok"):
        print(created.get("error") or t("配置设置失败"))
        return 1
    cid = created["id"]

    print(t("最小请求联通测试…"))
    rep = _probe(python, home, cid, query_fn=query_fn)
    if not rep.get("ok"):
        label, suggest = _hint(str(rep.get("kind") or ""))
        print(t("API 异常（%s）：%s")
              % (label, rep.get("error") or t("未知错误")))
        if not _fix_loop(python, home, cid, ask=ask_, secret=sec_, ep=ep,
                         query_fn=query_fn, suggest=suggest):
            print(t("已保留，联通测试未通过"))

    try:
        done = _set_default(python, home, ep, cid, query_fn=query_fn)
        if not done.get("ok"):
            print(t("更改默认失败：%s") % (done.get("error") or t("原因未知")))
            return 1
    except Exception as exc:                         # noqa: BLE001
        print(t("更改默认失败：%s") % exc)
        return 1

    print(t("已确认。运行 `crannia` 开始使用。"))
    return 0
