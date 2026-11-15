# -*- coding: utf-8 -*-
"""运行时事故账本 —— `#02 组件异常` 的数据源。

## 只收「真的坏了」，不收「不兼容」

    skill 的 import 失败    —— 模块导不进来（缺依赖 / 语法错）
    skill 的 start 失败     —— 它声明的后台服务起不来
    watcher 启动失败        —— 用户自己开了监听，但没在跑
    MCP 被 quarantine       —— 服务器掉线，工具已下线
    数据目录迁移 / 备份失败 —— 旧数据没搬过来，或备份没做成
    `/rewind` 补偿失败      —— 文件停在退回一半的状态，回执里不许写

不收 `compatibility` / `discover`：那两类是"这个包跟本机配不上"，
技能页已经写了原因，体检器也管得着。把它们也弹出来，等于每次启动都喊一遍
同一句用户改不了的话，喊到没人再看横幅为止。

## 为什么要有它

上面四类现在都只 `print` 到终端。用户开了监听、以为在监听，
实际一条文件变更都不会触发 —— 而界面一片安静。
安静和「一切正常」长得一模一样。

## 存储：`data/incidents/YYYY-MM-DD.jsonl`

一行一个事件，追加写。

不放 `logs/`：那个目录整棵在 `DEFAULT_FORBIDDEN_PATHS` 里，模型读不到 ——
而这一格的价值一半是「agent 读得到才修得了」。
不放 `data/runtime/`：那是可重建的缓存，回收器扫掉会把证据一起删。

## 本轮 vs 历史

`all()` 只返回本进程记下的 —— 否则昨天的事故天天弹横幅。
文件是给人和 agent 看的全量记录。
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
from pathlib import Path

__all__ = ["KINDS", "note", "resolve", "all", "clear", "note_data_migration",
           "build_note", "append_incident_note", "path_for"]

#: 四类来源。加新的一类时同时想清楚两件事：
#:   ① 它是不是"用户改不了"（是，则 别加，那是 compatibility 的活）
#:   ② 它有没有一个明确的组件名（没有，则 用户没法据此让谁去修）
KINDS = ("skill_import", "skill_start", "watcher", "mcp_quarantine",
         "data_migration", "rewind_rollback", "compaction",
         #: 通道起不来（token / 代理 / 网络）。此前失败只 print 到终端，
         #:   桌面壳里根本看不到，界面上通道卡只是回到「未运行」，用户不粘贴
         #:   终端日志就不知道是 token、代理还是网络。两条判据都过：用户改得了
         #:   （换代理 / 换 token），组件名明确（通道名）。
         "channel")

#: 本进程记下的事故。会话/进程级 —— 已登记到 `core/state_registry.py`。
_ITEMS: list[dict] = []
_lock = threading.Lock()

_DIR = Path("data") / "incidents"


def path_for(day: str | None = None) -> Path:
    """当天的账本路径。*day* 形如 `2026-10-31`（测试用）。"""
    d = day or _dt.date.today().isoformat()
    return _DIR / ("%s.jsonl" % d)


def note(component: str, kind: str, error: str, detail: str = "",
         *, persist: bool = True,
         component_tpl: str = "", component_vars: dict | None = None,
         error_tpl: str = "", error_vars: dict | None = None) -> None:
    """记一条。不许抛 —— 记账把自己搞挂，方向就反了。

    *component* 是用户能看懂的组件名（skill 名 / watcher 的 id / MCP server 名）。
    *error* 是一句人话原因；*detail* 是原始报错（可空）。
    *persist=False*：只进内存（本次运行看得见），不落盘。给"落盘会把
      别处的判据弄假"的那一种用 —— 数据迁移失败时账本自己就住在
      目标目录里，建它等于替迁移签了个"已完成"。

    *component_tpl* / *error_tpl*（+ 各自的 vars）是给人看的那两句话的模板。
      不传就只存已经拼好的串（旧行为，向后兼容）。
      为什么非有不可：component / error 由后端拼成中文，前端原样渲染，
      英文界面下这两句永远是中文，而 t() 碰不到它们。
      模板加变量能被前端 t() 翻译；去重仍然按拼好的 component 比，
      所以两个不同的 watcher 不会被同一条模板吃掉。
    """
    name = str(component or "").strip()
    if not name:
        return
    item = {
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "code": "02",
        "kind": kind if kind in KINDS else KINDS[0],
        "component": name,
        "error": str(error or "").strip(),
        "detail": str(detail or "").strip()[:2000],
        "componentTpl": str(component_tpl or ""),
        "componentVars": dict(component_vars or {}),
        "errorTpl": str(error_tpl or ""),
        "errorVars": dict(error_vars or {}),
    }
    with _lock:
        # 同一组件同一类只留第一条 —— 后台服务可能反复重试，
        #   而用户要的是"哪个坏了"，不是"坏了几次"。
        for old in _ITEMS:
            if old["component"] == name and old["kind"] == item["kind"]:
                # 升级路径：老条目是在模板字段出现之前写下的（磁盘上的
                #   data/incidents/*.jsonl 里没有这两个键）。那条会一直以中文
                #   渲染，而每次启动都会走回这里。所以新调用带模板时就地补上，
                #   不补的话用户升级后这个横幅永远是中文，
                #   而代码「看起来是修好的」。
                if not old.get("componentTpl") and item["componentTpl"]:
                    old["componentTpl"] = item["componentTpl"]
                    old["componentVars"] = item["componentVars"]
                if not old.get("errorTpl") and item["errorTpl"]:
                    old["errorTpl"] = item["errorTpl"]
                    old["errorVars"] = item["errorVars"]
                return
        _ITEMS.append(item)

    if not persist:
        return
    try:
        _append(item)
    except Exception:                                          # noqa: BLE001
        # 落盘失败只影响"以后能查"，不影响"现在要告诉用户" —— 后者走内存。
        pass


def note_data_migration(report: dict, *, app_root: str = "",
                        data_root: str = "") -> None:
    """把数据目录那一趟的失败记进账本。

    两种失败分成两个组件名：`note()` 按（组件, 类）去重，合成一个名字的话
    第二条会被第一条吃掉 —— 而它们要用户做的事完全不同（排除原因后重启 /
    先手动复制整个数据目录）。

    原因里带源与目标两个路径：模型被问"帮我看看"时，手里得有这两格
    才能动手（它读不到安装目录之外的东西，`build_note()` 把这一段交给它）。
    """
    rep = report or {}
    # 账本住在 `data/incidents/`，而 `data/` 正是这一趟要搬的东西之一：
    #   搬失败时把它建出来，下一次启动会认为"目标已存在 = 迁移完成"，
    #   旧数据就再也不搬了。所以这一种失败只进内存（本次运行看得见），
    #   下一次启动会重试、也会重新记。
    persist = True
    try:
        persist = bool((Path(data_root) / "data").is_dir()) if data_root else True
    except Exception:                                          # noqa: BLE001
        persist = True

    errors = [str(x).strip() for x in (rep.get("errors") or []) if str(x).strip()]
    if errors:
        note("数据目录迁移", "data_migration",
             "旧数据没有搬过来（%s；源 %s → 目标 %s）。排除原因后重启会继续。"
             % ("；".join(errors), app_root or "?", data_root or "?"),
             detail="\n".join(errors), persist=persist)

    ds = rep.get("data_schema") or {}
    if ds.get("compat") and not (ds.get("complete") and ds.get("backup")):
        why = str(ds.get("error") or "")
        if not why:
            failed = ds.get("failed") or []
            why = "；".join(
                "%s: %s" % (x[0], x[1])
                if isinstance(x, (list, tuple)) and len(x) >= 2 else str(x)
                for x in failed[:3])
        note("数据目录备份", "data_migration",
             "数据格式高于本程序（%s > %s），而备份没有做成（%s）。"
             "先手动复制整个数据目录再使用。"
             % (ds.get("found"), ds.get("ours"), why or "未知原因"),
             detail=why)


def _already_today(item: dict) -> bool:
    """今天的这份账里已经有同组件同类的条目吗。

    内存那份去重（`note` 里）只在进程内有效：重启一次就空了，于是
    「仍然坏着」的组件每启动一次就多一行 —— 重启 71 次就是 71 行同一件事，
    而账本没有任何保留期。判据放在盘上这一侧才跨得过重启。

    读不到 / 解析不了 -> 当"没有"：宁可多一行，不丢一条（记账不许吞事）。
    """
    try:
        p = path_for()
        if not p.is_file():
            return False
        with open(p, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    old = json.loads(raw)
                except Exception:                              # noqa: BLE001
                    continue
                if (old.get("component") == item.get("component")
                        and old.get("kind") == item.get("kind")):
                    return True
    except Exception:                                          # noqa: BLE001
        return False
    return False


def _append(item: dict) -> None:
    if _already_today(item):
        return                       # 同一件事今天已经记过了（跨重启去重）
    p = path_for()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(item, ensure_ascii=False) + "\n"
    # 追加写；先建临时文件再 `os.replace` 会把整份重写一遍（账本只增不改）。
    with open(p, "a", encoding="utf-8", newline="") as f:
        f.write(line)


def resolve(component: str, kind: str) -> bool:
    """撤掉一条 —— 那个组件后来真的起来了。

    成对的另一半：用户把目录改回来、重新启用监听之后，界面上那条事故
    必须跟着消失。没有它，事故就成了一条没人能撤的旧账。
    只动内存那份；文件是证据，不删。
    """
    name = str(component or "").strip()
    if not name:
        return False
    with _lock:
        for i, old in enumerate(_ITEMS):
            if old["component"] == name and old["kind"] == kind:
                del _ITEMS[i]
                return True
    return False


def all() -> list[dict]:
    """本进程记下的事故（副本）。"""
    with _lock:
        return [dict(x) for x in _ITEMS]


def build_note() -> str:
    """把本进程的事故拼成一段给模型看的块；没有事故时是空串。

    为什么要有它：横幅只是告诉用户"有东西坏了"，它不说是哪一件
    （那句话是固定文案，翻成英文之后更不能塞变量）。而模型被问
    "帮我看看怎么回事"时，手里没有任何入口 —— 它读不到
    `data/settings.json`（那份在规避清单里），也就看不见监听配的是哪个目录。
    这一块把"哪一个、为什么、账本在哪"直接放到它眼前。

    只在有事故时挂，零事故时一个字节都不占。
    内容是外来文本（组件名来自包作者 / MCP 配置，原因里带用户路径），
    所以逐字段过 `sanitize_text`。
    """
    items = all()
    if not items:
        return ""
    try:
        from core.content_sanitizer import sanitize_text
    except Exception:                                          # noqa: BLE001
        return ""
    # 标签写成字面量，不用 `%` 拼：块普查按闭合标签的字面量找产者，
    # 拼出来的字符串在它眼里等于"这个块不存在"。
    lines = ["<runtime_incidents>",
             "框架本次运行记下了 %d 条组件异常。用户界面只在新建对话页"
             "提一句，他很可能不知道怎么修，也可能没看见。" % len(items)]
    for it in items:
        lines.append("- %s：%s" % (sanitize_text(it.get("component", "")),
                                   sanitize_text(it.get("error", ""))))
    lines.append("完整账本（可直接读）：%s" % path_for().as_posix())
    lines.append("</runtime_incidents>")
    return "\n".join(lines)


def append_incident_note(messages: list, inner: str) -> list:
    """把事故摘要附在最后一条 user 消息后面。

    与技能索引 / 记忆索引 / 引导片段同一套落点（`append_text_block`）：
    不改原列表、不新加一条消息、找不到就原样返回。
    """
    if not inner:
        return messages
    from core.image_input import append_text_block
    return append_text_block(messages, "\n\n" + inner)


def clear() -> None:
    """清空内存里那份。不删文件 —— 那是证据。"""
    with _lock:
        _ITEMS.clear()
