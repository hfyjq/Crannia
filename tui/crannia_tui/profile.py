"""`crannia profile`：列出与切换模型配置。

不启动界面，也不 import `core`。有后端在跑就走它的
`/api/model-configs` —— 那是唯一会把配置写进文件的进程，
在别的进程里直接改文件会让它的内存缓存与磁盘打架；
没有后端才走一次子进程查询 / 调用。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from . import backend
from .doctor import PREPARE, make_data_root_query, query
from .i18n import t

#: 没有后端时的一次性查询：默认条目 + 全部条目的摘要。
_LIST_CODE = PREPARE + (
    "import json; from core import model_configs as mc;"
    "print(json.dumps({'default': mc.default_id(), 'entries': mc.names_payload()},"
    " ensure_ascii=False))")

#: 没有后端时的切换：先试装，装得上才落盘（与 API 那条同一条判据）。
_SWITCH_CODE = PREPARE + (
    "import json; from core import model_configs as mc;"
    "reason = mc.preflight(%r) or '';"
    "if not reason: mc.set_default(%r);"
    "print(json.dumps({'reason': reason}, ensure_ascii=False))")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(ep: dict, path: str, payload: dict | None = None,
               timeout: float = 3.0) -> dict | None:
    """向端点要一份 JSON；GET（payload 为 None）或 POST。失败返回 None。"""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (ep["port"], path), data=data,
        method="POST" if data else "GET",
        headers={"X-Crannia-Token": ep["token"],
                 **({"Content-Type": "application/json"} if data else {})})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def endpoint(python: Path, home: Path) -> dict | None:
    """活着的端点（没有就是 None）。数据根与判活复用接入那一套。"""
    try:
        root = make_data_root_query(python, home)()
    except Exception:                                       # noqa: BLE001
        return None
    ep = backend._read_endpoint(root)
    if ep is None or backend._probe(ep) is None:
        return None
    return ep


def _entries_from_cards(payload: dict) -> list[dict]:
    out: list[dict] = []
    for card in payload.get("cards") or []:
        for row in card.get("configs") or []:
            out.append({
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or row.get("id") or ""),
                "model": str(row.get("model") or ""),
                "is_default": bool(row.get("is_default")),
            })
    return out


def read(python: Path, home: Path, *, ep: dict | None = None,
         query_fn=None) -> tuple[list[dict], str]:
    """返回 `(条目, 默认 id)`。两条路都读得到，形状先统一成一种。"""
    if ep is not None:
        payload = http_json(ep, "/api/model-configs")
        if payload is not None:
            return _entries_from_cards(payload), str(payload.get("default") or "")
    run = query_fn or (lambda code: query(python, home, code))
    data = json.loads(run(_LIST_CODE))
    entries = [{"id": str(e.get("id") or ""),
                "name": str(e.get("name") or e.get("id") or ""),
                "model": str(e.get("model") or ""),
                "is_default": str(e.get("id") or "") == str(data.get("default") or "")}
               for e in data.get("entries") or []]
    return entries, str(data.get("default") or "")


def render(entries: list[dict]) -> list[str]:
    """人看的三列：名字 / 模型 / id，默认那条前面一个星。"""
    if not entries:
        return [t("尚未拥有模型配置 —— 请前往 GUI 端配置内新建。")]
    width = max(len(e["name"]) for e in entries)
    lines = []
    for e in entries:
        mark = "*" if e["is_default"] else " "
        lines.append("%s %-*s  %-24s  (%s)"
                     % (mark, width, e["name"],
                        e["model"] or t("(未填模型)"), e["id"]))
    return lines


def _resolve(entries: list[dict], ref: str) -> tuple[dict | None, str]:
    """按 id、名字或编号找一条；撞名 / 找不到都返回一句带候选的话。

    编号就是完整清单里的序号（从 1 起），与后端 `/model` 同一套口径；
    先精确匹配 id 与名字，找不到才当编号 —— 一条正好叫 `2` 的配置
    不会被编号 2 抢走。
    """
    key = ref.strip().lower()
    if not key:
        return None, ""
    by_id = [e for e in entries if e["id"].lower() == key]
    if by_id:
        return by_id[0], ""
    same = [e for e in entries if e["name"].lower() == key]
    if len(same) == 1:
        return same[0], ""
    if len(same) > 1:
        return None, t("「%s」撞名了，用 id 指定：%s") % (
            ref, " · ".join("%s(%s)" % (e["name"], e["id"]) for e in same))
    if key.isdigit():
        n = int(key)
        if 1 <= n <= len(entries):
            return entries[n - 1], ""
    return None, t("没有「%s」。现有：%s") % (
        ref, " · ".join(e["name"] for e in entries) or t("（空）"))


def switch(python: Path, home: Path, ref: str, *, ep: dict | None = None,
           query_fn=None) -> tuple[bool, str]:
    """把 `ref` 设为默认。返回 `(成功, 给人看的一句话)`。"""
    entries, _ = read(python, home, ep=ep, query_fn=query_fn)
    hit, why = _resolve(entries, ref)
    if hit is None:
        return False, why or t("请提供一个配置名或 id")
    if hit["is_default"]:
        return True, t("「%s」已经是默认了。") % hit["name"]

    if ep is not None:
        payload = http_json(ep, "/api/model-configs/default", {"id": hit["id"]})
        if payload is None:
            return False, t("后端无应答 —— 先用 `crannia doctor` 看它还在不在。")
        if not payload.get("ok"):
            return False, str(payload.get("error") or t("切换失败"))
        return True, str(payload.get("message")
                         or t("已设为默认：%s") % hit["name"])

    run = query_fn or (lambda code: query(python, home, code))
    data = json.loads(run(_SWITCH_CODE % (hit["id"], hit["id"])))
    reason = str(data.get("reason") or "")
    if reason:
        return False, t("此配置暂无法使用，未设为默认。原因：%s") % reason
    return True, (t("已设为默认：「%s」。新对话生效；当前这个需重启。")
                  % hit["name"])


def profile_command(ref: str | None, *, python: Path, home: Path,
                    ep: dict | None = None, query_fn=None) -> int:
    """`crannia profile [名字|id]` 的入口。返回退出码。"""
    if ep is None:
        ep = endpoint(python, home)
    if ref:
        ok, message = switch(python, home, ref, ep=ep, query_fn=query_fn)
        print(message)
        return 0 if ok else 1
    entries, _ = read(python, home, ep=ep, query_fn=query_fn)
    for line in render(entries):
        print(line)
    return 0
