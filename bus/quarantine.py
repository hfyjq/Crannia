"""不兼容插件的屏蔽 / 隔离 —— 账本那一半。

判据：一个插件不可用时应当把它屏蔽，而不是等用户去卸载 ——
它不能破坏框架。

## 三个部件，各在哪儿

===================  =========================================================
闸                `bus/registry.py::get_active_tool_names` —— 负向集合在那儿生效
账（本模块）      `data/runtime/quarantine.json` —— 留文件、可逆、记账
触发（三个）      装载期（`bus/core.py` / `reload_skills`）·
                      首次调用（`skill_loader._LazyStub`）·
                      MCP（`mcpx/manager.py` / `mcpx/client.py`）
===================  =========================================================

## 判据：屏蔽 ≠ 卸载，形状照抄 memory 的 tombstone

- 留文件（`skills/<名>/` 一个字节都不动）· 可逆（`release()`）· 记账（记下
  是哪条体检码、什么时候、哪个包）
- 不删、不改用户的包 —— 删了用户就再也修不回来了，而我们要的只是"别跑它"。
  （同族先例：`draft: true` 就是"装了但不跑"，屏蔽是它的强制版 —— 区别是
  draft 是作者自己写的、屏蔽是框架自己判的。）

## 谁算"该屏蔽"—— 这份名单是收窄过的，别放宽

`not diag.ok` 太宽：体检器把"声明不全"（缺 `permission` / 缺 `destructive` /
`output_kind` 写错）也记成 FAIL，而那些包跑得起来。屏蔽它们的代价是
用户真的用不了了 —— 那比"少一格声明"坏得多。

只认下面两层（判据逐条在 `_quarantine_codes` 的注释里）：

  A 会「静默指错」：重名，`register_tool` 拒第二个，模型调 `foo`，
  执行的是先注册那个包的实现，而两个包看起来都装好了。
  这是全表里唯一"跑得通、但跑的是别人"的形状，必须屏蔽。
  B 死包：工具在表里、一调就炸（首次 `import` 也在这一条路上）。
  它不会掀翻这一轮（`agent_loop` 派发外面那层 `except Exception` 兜着，
  见 `:3417`）—— 代价是每一次都失败一次，模型还得把一句 Python 异常读进上下文
  再自己绕。屏蔽买的是"把反复失败换成一句一次性的实话"。

**`draft_not_bool` / `manifest_unparsable` 这类"装不上但不是坏"不进这张表**：
它们压根没注册（屏蔽是空动作），而弹窗那句话（"已被屏蔽"）会说谎 ——
事实是"你的包没装上"。那类要走出声那条路（标签不许说谎）。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

#: 账本落点。运行数据一律进 `data/runtime/`，
#: 不许落在 `skills/` 或 `core/` 下。
LEDGER_PATH = Path("data/runtime/quarantine.json")

#: 只屏蔽这两类（逐条理由见模块 docstring）。
QUARANTINE_CODES: frozenset[str] = frozenset({
    # A 层：会「静默指错」
    "tool_name_collision",          # 跨包重名，后注册的被拒，名字指着先那个包
    "tool_duplicate_in_manifest",   # 同一个包里重名，schema 就地替换，一个被静默顶掉
    # B 层：死包（工具在表里，一调就炸）
    "handler_missing",
    "handler_unreadable",
    "handler_syntax_error",
    "handler_tool_not_found",
    "tools_missing",
    "tool_schema_invalid",
    "tool_not_mapping",
    "tool_no_name",
})

_LOCK = threading.Lock()
_cache: dict[str, dict] | None = None


# ----------------------------------------------------------------------
# 账本读写
# ----------------------------------------------------------------------

def _read() -> dict[str, dict]:
    """读账本。读不到 / 坏了，空账本（不是"全部屏蔽"）。

    方向是有意的：这一格是"屏蔽"的名单 —— 读不到就当没有屏蔽，
    与 `retention.py` 那条"删的动作，真值是不动"同一个方向（宁可少屏蔽，
    不可凭空把用户的工具关掉）。
    """
    try:
        raw = LEDGER_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:                                    # noqa: BLE001
        print(f"[quarantine] 账本读不动（{type(exc).__name__}: {exc}）—— 按空账本走")
        return {}


def _write(data: dict[str, dict]) -> None:
    """整表写回（原子替换）。

    临时文件 + `os.replace`：进程被杀在半路时，宁可留一份旧账本，
    也不留一份半截 JSON（那会被 `_read` 判成"读不动"，屏蔽全丢）。
    """
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(LEDGER_PATH.parent), prefix=".quarantine-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, LEDGER_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sync_registry(data: dict[str, dict]) -> None:
    """把账本推给闸那一侧（`bus/registry._QUARANTINED`）。"""
    from bus.registry import set_quarantined
    set_quarantined(data.keys())


def load() -> dict[str, dict]:
    """启动时调一次：读账本 + 推给闸。返回账本。"""
    global _cache
    with _LOCK:
        data = _read()
        _cache = data
        _sync_registry(data)
    return data


def all_entries() -> dict[str, dict]:
    """当前账本（只读）。没 load 过就先 load。"""
    if _cache is None:
        return load()
    return dict(_cache)


# ----------------------------------------------------------------------
# 写入
# ----------------------------------------------------------------------

def quarantine(owner: str, *, code: str, reason: str = "",
               path: str = "", detail: str = "") -> dict:
    """把一个插件（`skill:<名>` / `mcp:<名>`）记账 + 屏蔽。幂等。

    返回那条记录（调用方拿它去发事件 / 打日志）。
    重复屏蔽同一归属时保留最早那条的 `since`、刷新 `last_seen` 与 `hits`
    —— 判据与 `EVT_CONTEXT_STATE` 那条一模一样：粘住的旧值不许伪装成新事实，
    反过来也一样：一件一直存在的事，它的"从什么时候开始"不许每次重算。
    """
    global _cache
    owner = str(owner or "").strip()
    if not owner:
        raise ValueError("owner 不能为空（它是屏蔽的粒度：skill:<名> / mcp:<名>）")
    now = time.time()
    with _LOCK:
        data = dict(_cache if _cache is not None else _read())
        old = data.get(owner)
        entry = {
            "owner": owner,
            "code": str(code or ""),
            "reason": str(reason or ""),
            "path": str(path or ""),
            "detail": str(detail or "")[:2000],
            "since": (old or {}).get("since", now),
            "last_seen": now,
            "hits": int((old or {}).get("hits", 0)) + 1,
        }
        data[owner] = entry
        _cache = data
        try:
            _write(data)
        except Exception as exc:                                # noqa: BLE001
            # 写不进去也要屏蔽（本进程内是真的）—— 落盘失败只影响"下次启动还认不认"。
            print(f"[quarantine] 账本写不进去（{type(exc).__name__}: {exc}）"
                  f" —— 本次运行**仍然屏蔽** '{owner}'")
        _sync_registry(data)
    # 记账之后才发信号：
    #   「信号必须在"它让你去读的那份 state"可读之后才发」——
    #   于是前端收到事件之后再拉一次 `/api/quarantine`，两边永远一致。
    # 只在第一次发（`hits == 1`）：一个坏包每轮都被体检一次，
    #   每轮都弹一遍就是"弹窗疲劳"的制造法。重复发现记在 `hits` 上。
    if int(entry.get("hits", 1)) <= 1:
        _emit_blocked(entry)
    return entry


def _emit_blocked(entry: dict) -> None:
    """把"刚屏蔽了一个插件"广播出去（弹窗的信号）。它自己不许炸。"""
    try:
        from core.events import EVT_PLUGIN_BLOCKED, get_event_bus
        get_event_bus().emit(
            EVT_PLUGIN_BLOCKED,
            owner=entry.get("owner", ""),
            name=str(entry.get("owner", "")).split(":", 1)[-1],
            code=entry.get("code", ""),
            reason=entry.get("reason", ""),
            path=entry.get("path", ""),
            since=entry.get("since", 0),
        )
    except Exception as exc:                                    # noqa: BLE001
        # 事件发不出去不影响屏蔽本身（账本里那条是真的、闸也已经生效）——
        #    只是这一次没有"实时"那一下，前端下一次拉的时候照样看得到。
        print(f"[quarantine] 事件没发出去（{type(exc).__name__}: {exc}）"
              f" —— 屏蔽本身照旧生效")


def release(owner: str) -> bool:
    """解封（可逆那一半）。返回是不是真的解掉了一个。"""
    global _cache
    owner = str(owner or "").strip()
    with _LOCK:
        data = dict(_cache if _cache is not None else _read())
        if owner not in data:
            return False
        data.pop(owner, None)
        _cache = data
        try:
            _write(data)
        except Exception as exc:                                # noqa: BLE001
            print(f"[quarantine] 账本写不进去（{type(exc).__name__}: {exc}）")
        _sync_registry(data)
    return True


def reset_for_tests() -> None:
    """清内存缓存 + 闸（不碰盘）—— 测试用。"""
    global _cache
    with _LOCK:
        _cache = None
    from bus.registry import set_quarantined
    set_quarantined(set())


# ----------------------------------------------------------------------
# 判据
# ----------------------------------------------------------------------

def should_quarantine(diag) -> tuple[bool, str]:
    """一份 `Diagnosis` 该不该屏蔽？返回 `(该不该, 命中的那段原因)`。

    两个条件必须同时成立（少一个就有一种误伤）：
      ① 有命中名单里的 FAIL 码（`QUARANTINE_CODES`）
      ② 这个包框架真会装载 —— 判据就是它进得了 `discover()`
         （`manifest.yaml` 存在）。少了这一条，`skills/` 下那些
         只有 `SKILL.md` 的指令型技能会被误判（它们本来就不走工具装载那条路）。
    """
    codes = {f.code for f in getattr(diag, "by_severity", lambda _s: ())("fail")}
    hit = sorted(codes & QUARANTINE_CODES)
    if not hit:
        return False, ""
    if not (Path(getattr(diag, "path", "") or "") / "manifest.yaml").is_file():
        return False, ""
    by_code = {f.code: f.message for f in getattr(diag, "findings", ())}
    reason = "；".join(by_code.get(c, c) for c in hit)
    return True, reason


def gate_skill_dir(skill_dir) -> Any:
    """装载之前的一道闸（触发①）。命中，记账 + 屏蔽，返回那份 `Diagnosis`。

    调用方拿到非 `None` 就是"别装它" —— 两个调用点：
    `bus/core.py` 的启动装载、`bus/loaders/skill_loader.py::reload_skills` 的热加载。
    必须是两处都调 —— 只做启动那一处，`/skill approve` 之后热加载进来的
    同一个坏包就绕过去了（而那条路正是"用户自己装的"那一条）。

    `registered_names` 取当下活着的注册表：重名那条判据靠它，
    而这个函数在装载循环里被逐个调用，第二个包看得见第一个已经注册的名字。
    """
    try:
        from core import skill_doctor as SD
        from bus.registry import list_tools
        registered = set(list_tools())
    except Exception as exc:                                    # noqa: BLE001
        print(f"[quarantine] 体检进不去（{type(exc).__name__}: {exc}）—— 这次不拦")
        return None
    try:
        diag = SD.examine(skill_dir, registered_names=registered)
    except Exception as exc:                                    # noqa: BLE001
        # 体检器自己炸了，不拦
        # （体检是给"别人写坏了"用的，它自己出问题不该连累用户）。
        print(f"[quarantine] 体检炸了（{type(exc).__name__}: {exc}）—— 这次不拦")
        return None

    hit, reason = should_quarantine(diag)
    if not hit:
        return None
    entry = quarantine(owner_for_skill(diag.skill), code=_first_code(diag),
                       reason=reason, path=getattr(diag, "path", ""),
                       detail="；".join("%s: %s" % (f.code, f.message)
                                       for f in diag.by_severity("fail")))
    print(describe(entry) + " —— **本次不装载**")
    return diag


def review_static_quarantines(skills_dir: str | Path = "skills") -> list[str]:
    """启动时重判那些被静态判据屏蔽过的包 —— 修好了就放它出来。

    为什么非有不可：账本是持久的，
    而装载闸只看账本，用户把包修好了、重启，它照样跑不了，
    而且不会再有弹窗（`hits` 已经不是 1 了）。一个"修好也没救"的死结，
    而本仓对这类形状的判据是「安全机制不许妨碍把事做完」。

    判据：只看静态那几条码。运行期才发现的那种（`lazy_load_failed` /
    `mcp_*`）不重判 —— 体检器（`ast.parse`）证明不了"import 期不再炸"
    （那是它天生看不见的那一类）。对它们，出路是那条解封端点（或删掉那个包）。

    只放不收：这里一个包都不会新屏蔽（新屏蔽是装载闸的活）。
    """
    from pathlib import Path as _P
    out: list[str] = []
    for owner, entry in sorted(all_entries().items()):
        if not owner.startswith("skill:"):
            continue
        if str(entry.get("code") or "") not in QUARANTINE_CODES:
            continue                                    # 运行期那几条：重判不了
        pkg = _P(skills_dir) / owner.split(":", 1)[1]
        if not pkg.is_dir():
            continue                                    # 包没了，留着那条账（它记的是发生过的事）
        try:
            diag = gate_probe(pkg)
        except Exception as exc:                        # noqa: BLE001
            print(f"[quarantine] 重判 '{owner}' 时出错（{type(exc).__name__}: {exc}）"
                  f" —— 维持屏蔽")
            continue
        if diag is None:                                # 体检过了，放它出来
            release(owner)
            out.append(owner)
            print(f"[quarantine] '{owner}' 这次体检过了 —— **已解封**，本次会正常装载")
    return out


def gate_probe(skill_dir) -> Any:
    """只判、不记账的那一半（`gate_skill_dir` 的判据部分，给重判用）。

    返回 `Diagnosis`（该屏蔽）或 `None`（放行）。与 `gate_skill_dir` **共用
    同一个 `should_quarantine`** —— 两处各写一遍判据就会各漂各的。
    """
    try:
        from core import skill_doctor as SD
        from bus.registry import list_tools
        diag = SD.examine(skill_dir, registered_names=set(list_tools()))
    except Exception as exc:                            # noqa: BLE001
        print(f"[quarantine] 体检进不去（{type(exc).__name__}: {exc}）—— 这次不判")
        return None
    return diag if should_quarantine(diag)[0] else None


def _first_code(diag) -> str:
    """命中的第一个屏蔽码（给记账用；`reason` 里那条更像人话）。"""
    codes = sorted({f.code for f in diag.by_severity("fail")} & QUARANTINE_CODES)
    return codes[0] if codes else ""


def settle_after_release(owner: str, path: str | Path, security_guard=None) -> dict:
    """解封之后当场重体检，按结果只走两条路之一。

    只调 `release(owner)` 的话三件事一件都没做：

      · 不重体检，一个还是坏的包被放行；
      · 不重新装载，装载期被拦的包工具从来没注册过
        （`skill_loader.py` 那道闸排在任何 `register_tool` 之前）
界面说"已解封"而工具依然调不到（标签在说谎，且没有东西会红）；
      · 不重记，修不好的包不会再弹（账本里 `hits` 已经不是 1 了）。

    ## 两条路只走一条，这是本函数最脆的一处

    不许"先 `quarantine()` 再 `reload_skills()`"：`reload_skills` 内部自己会再闸一次
    （`gate_skill_dir`），那样 `hits` 会变成 2，而"还会再弹一次"正靠
    `hits == 1`（`quarantine()` 里那条 `if int(entry.get("hits", 1)) <= 1`）。
    即："再弹"不是新造的机制，是 `release()` 把条目 `pop` 掉之后
    `hits` 重新从 1 起算的副产品 —— 谁先记一笔账，谁就把它掐掉了。

    返回 `{"recheck": str, "message": str}`。`recheck` 四态：

    ================  ==================================================
    `passed`          放行 + 已 `reload_skills()`（工具真的注册回来了）
    `still_bad`       仍坏，已重记，会自动再发一次 `plugin.blocked`
    `path_missing`    账本里那个目录不在了，没体检、没装载（如实说）
    `load_failed`     放行但重载时炸了，如实说，附 `/skill reload` 那条出路
    ================  ==================================================

    "体检器自己炸了"读起来像"放行" —— `gate_probe` 在那条路上也回 `None`
    （它是不拦路的，同 `gate_skill_dir` 与 `_check_skills_changed` 那条判据）。
回执那句话说的是「已放行」，不是「体检通过」—— 别把它写成后者。
    """
    pkg = Path(str(path or ""))
    if not pkg.is_dir():
        return {"recheck": "path_missing",
                "message": "已解封，但账本里记的那个目录找不到了（%s）"
                           "—— 本次没重新体检，也没重新装载。" % (str(path or "") or "（路径缺失）")}

    diag = gate_probe(pkg)
    if diag is None:                                            # 放行，真去装载
        try:
            from bus.loaders.skill_loader import reload_skills
            info = reload_skills(pkg.parent, security_guard)
        except Exception as exc:                                # noqa: BLE001
            return {"recheck": "load_failed",
                    "message": "已解封，但重新装载时出错（%s: %s）"
                               "—— 可试试 /skill reload。" % (type(exc).__name__, exc)}
        if info.get("quarantined"):
            # 罕见：体检放行之后、`reload_skills` 那道闸又把它拦下了
            # （两处判据同源，只有在两跳之间包被改动才会分叉）。如实说。
            return {"recheck": "still_bad",
                    "message": "已解封，但重新装载时它又没通过体检"
                               "—— 兼容性警告会再弹一次。"}
        n = len(info.get("loaded") or [])
        return {"recheck": "passed",
                "message": "已解封并重新装载%s。" % (("（新装 %d 个）" % n) if n else "")}

    # 仍坏，重记。条目已被 `release()` pop 掉，`hits` 从 1 起算
    #`quarantine()` 里那条 `hits <= 1` 自动重发 `plugin.blocked`。
    hit, reason = should_quarantine(diag)
    quarantine(owner, code=_first_code(diag), reason=reason,
               path=str(getattr(diag, "path", "") or pkg),
               detail="；".join("%s: %s" % (f.code, f.message)
                               for f in diag.by_severity("fail")))
    return {"recheck": "still_bad",
            "message": "还没修好（%s）—— 已重新屏蔽，兼容性警告会再弹一次。"
                       % (reason or "体检仍不通过")}


def owner_for_skill(skill_name: str) -> str:
    """技能目录名 → 归属键。与 `skill_loader` 注册时用的那个逐字同形。"""
    return f"skill:{skill_name}"


def owner_for_mcp(server_name: str) -> str:
    """MCP 服务器名 → 归属键。同 `mcpx/client.py::register_external_server`。"""
    return f"mcp:{server_name}"


def reason_for(owner: str) -> str:
    """这个归属为什么被屏蔽（账本里那句人话）。没有就回空串。"""
    return str((all_entries().get(str(owner)) or {}).get("reason", ""))


def blocked_message(tool_name: str, owner: str, reason: str = "") -> str:
    """被屏蔽的工具回给模型的那一句。只此一处（两处会用：首调失败、派发闸）。

    为什么要单独一句、不复用 `_not_offered_message`：
    那条的承诺是「它存在，`search_tools('<name>')` 能把它拉回来」——
    而对一个被屏蔽的工具，那句话是假的（它被排除在活跃集之外，
    搜多少次都回不来）。模型会照着做、再撞一次墙，然后开始换名字试。
    形状一致：拒绝必带路线，而这里的路线是
    「别重试，去告诉用户」——框架侧没有别的出路。
    """
    return (
        "Error: tool '%s' is not available — it belongs to plugin '%s', which this "
        "framework has **blocked** because it failed its compatibility check. "
        "Nothing was executed. Searching will not bring it back; do not retry it. "
        "Tell the user which plugin is blocked so they can fix or remove it.%s"
        % (tool_name, owner, (" Reason: " + reason) if reason else "")
    )


def describe(entry: dict) -> str:
    """一行给人看的账（出事时终端里要有字）。"""
    return ("[quarantine] 已屏蔽 %s（%s）%s"
            % (entry.get("owner"), entry.get("code") or "?",
               ("—— " + entry.get("reason", "")) if entry.get("reason") else ""))
