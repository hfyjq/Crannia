"""外部命令钩子：在工具执行前后、以及准备收尾时运行用户配置的脚本。

支持面（事件名 / 字段 / 退出码 / 哪些不支持）以 README 的「互操作」一节为准 ——
那里只留一份，这里不抄第二份（抄了就会各漂各的）。

## 事件

`PreToolUse` · `PostToolUse` · `PostToolUseFailure` · `UserPromptSubmit` ·
`SessionStart` · `Stop` · `PermissionRequest`。

配置形状与常见做法一致：事件名 → `[{matcher, hooks: [...]}]`，
只执行 `type: "command"` 的条目，其余类型跳过并说一句。

## 输入 / 输出

- 输入：JSON 从 stdin 送进去，字段沿用同一套名字：
  `hook_event_name` / `session_id` / `cwd` / `tool_name` / `tool_input` /
  `tool_use_id` / `tool_response` / `stop_hook_active`。
- 输出：退出码 2 = 拦下，stderr 当理由；其余非零码是非阻塞失败。
  退出 0 时可以回一段 JSON：`hookSpecificOutput.permissionDecision`
  （`allow` / `ask` / `deny`）、`permissionDecisionReason`、`updatedInput`、
  `additionalContext`，以及顶层的 `decision: "block"`。
- `ask` 接进了审批流：框架把它变成一次待审批（kind `hook_ask`），
  用户答"同意"之后由框架重放那次调用（重放不走钩子，不会二次挂起）。
- `PermissionRequest` 在浮窗弹出之前跑，能替人答：
  `allow`，不发问、框架按"同意"把那一步做掉；`deny`，不发问、那一步回一句拒绝。
  `PreToolUse` 的 `allow` 没有这个权力 —— 它跑在调用之前，而框架自己的
  审批闸在工具里面（命令 / 出网 / 跨区 / 删除 …），那一刻还不知道被审批物是什么。
那一条只表示"这个钩子不反对"，不绕过任何闸。

## 异步（`async: true`）

条目上写 `"async": true`，框架不等它：起一条守护线程跑完就结束，
这一次调用按"钩子什么都没说"处理。

- 异步钩子只会弃权，不会做决定：不能拦、不能 `ask`、不能替人答，
  `updatedInput` / `additionalContext` 也送不回来（工具那一步已经过去了）。
它只适合只做事后动作的钩子（记一笔、发通知、跑一次索引）。
  `PreToolUse` 写 `async` 等于放弃拦截 —— 这不是缺陷，是这个词的定义。
- `SessionStart` 强制同步：它的产出（`additionalContext`）是要进模型上下文的，
  异步等于白跑 —— 等它跑完，那一轮已经把请求发出去了。
- 超时仍然适用；卡住的线程是守护线程，进程退出时不阻塞，但**它起的子进程
  可能留下**（同 §3.11 那条残余）。
- 跑完时若它想拦 / 想回话，会出一行声（那些结果在异步这条路上送不回去）
  —— 「没出声」与「没跑」不许同形，尤其是它拦不住东西的时候。

## 配置从哪来

| 位置 | 是否需要信任 |
|---|---|
| `data/hooks.json` | 否 |
| `~/.claude/settings.json` 的 `hooks` 键 | 否 |
| `<工作区>/.crannia/hooks.json` | 是 |
| `<工作区>/.claude/settings.json` / `.claude/hooks.json` | 是 |

项目级配置要 `data/settings.json` 里 `hooks_allow_project` 为 true 才执行 ——
钩子是用户机器上的任意命令，项目目录里的那份不应当自动获得这个权力。

## 边界

- 不是安全边界：钩子能做的事和用户自己敲命令一样多；它也不能绕过
  审批闸（它只在派发前后被调用）。
- 失败不阻塞对话：读不到配置 / 起不来 / 超时 / 非 0/2 的退出码 ——
  都只记一行，动作照常继续。
- 预取在钩子启用时整批关闭：否则只读批会在钩子之前被执行掉。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.shell import bash_path, clean_env

__all__ = [
    "EVENTS", "active", "pre_tool", "post_tool", "stop", "reset",
    "parse_config", "matches", "permission_request", "user_prompt_submit",
    "session_start", "post_tool_failure",
]

EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure",
          "UserPromptSubmit", "UserPromptExpansion", "SessionStart", "Stop",
          "StopFailure", "PermissionRequest")

_SETTINGS = Path("data/settings.json")
_GLOBAL = Path("data/hooks.json")
_USER_SETTINGS = Path("~/.claude/settings.json")
_PROJECT_FILES = (".crannia/hooks.json", ".claude/settings.json",
                  ".claude/hooks.json")

_DEFAULT_TIMEOUT_MS = 60_000
_MAX_TIMEOUT_MS = 600_000

#: 工具名 → 常见钩子配置里用的别名（matcher 两边都试）。
_ALIASES = {
    "run_command": ("Bash",),
    "read_file": ("Read",),
    "write_file": ("Write",),
    "edit_file": ("Edit",),
    "glob": ("Glob",),
    "grep": ("Grep",),
    "run_subagent": ("Task", "Agent"),
    "web_fetch": ("WebFetch",),
    "web_search": ("WebSearch",),
    "todo_write": ("TodoWrite",),
    "code_intel": ("LSP",),
}

_warned: set = set()


def _warn_once(key: str, msg: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    print("[hooks] " + msg)


@dataclass
class _Hook:
    event: str
    matcher: Optional["re.Pattern[str]"]
    command: str
    timeout_ms: int
    #: 后台跑、不等结果（`async` 是 Python 关键字，字段名换个字）。
    is_async: bool = False


# 配置

def _project_allowed() -> bool:
    try:
        data = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("hooks_allow_project") is True


def _plugins_allowed() -> bool:
    """工作区里的插件包能不能贡献钩子。

    判据与实现只有一处：`core/plugin_packages.workspace_plugins_allowed`。
    这里不重写读法 —— 钩子与 MCP 两个消费点共用它，各写一遍迟早一边开一边不开。
    """
    from core.plugin_packages import workspace_plugins_allowed
    return workspace_plugins_allowed()


def _section(raw) -> dict:
    """从一个文件的顶层取事件表；`{"hooks": {...}}` 与裸事件表都认。"""
    if not isinstance(raw, dict):
        return {}
    inner = raw.get("hooks")
    if isinstance(inner, dict):
        return inner
    if any(k in EVENTS or k in ("SessionStart", "UserPromptSubmit",
                                "SubagentStart", "SubagentStop") for k in raw):
        return raw
    return {}


def _substitute(command: str, workspace: Path,
                plugin_root: Path | None = None) -> str:
    out = (command.replace("${CLAUDE_PROJECT_DIR}", str(workspace))
                  .replace("${CRANNIA_PROJECT_DIR}", str(workspace)))
    if plugin_root:
        root = str(plugin_root)
        out = (out.replace("${CLAUDE_PLUGIN_ROOT}", root)
                  .replace("${CRANNIA_PLUGIN_ROOT}", root))
    return out


def parse_config(raw, *, workspace: Path, source: str,
                 plugin_root: Path | None = None) -> list:
    """把一份配置收成可执行的钩子列表。坏条目跳过并出声。

    *plugin_root* 由插件包提供，用来替换 `${CLAUDE_PLUGIN_ROOT}`。
    """
    out: list[_Hook] = []
    events = _section(raw)
    for event, groups in events.items():
        if event not in EVENTS:
            if event in ("SubagentStart", "SubagentStop"):
                _warn_once("unsupported:" + event,
                           "配置里的 %s 暂不支持，已跳过（支持的是 %s）"
                           % (event, " / ".join(EVENTS)))
            continue
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher_raw = group.get("matcher")
            rx = None
            if event != "Stop" and isinstance(matcher_raw, str) \
                    and matcher_raw not in ("", "*"):
                try:
                    rx = re.compile(matcher_raw)
                except re.error as exc:
                    _warn_once("badmatcher:%s:%s" % (source, matcher_raw),
                               "%s 里的 matcher 不是合法正则（%s）：%r"
                               % (source, exc, matcher_raw))
                    continue
            hooks = group.get("hooks")
            if not isinstance(hooks, list):
                continue
            for h in hooks:
                if not isinstance(h, dict):
                    continue
                if str(h.get("type") or "") != "command":
                    _warn_once("type:%s:%s" % (source, h.get("type")),
                               "%s 里有非命令钩子（type=%r），已跳过"
                               % (source, h.get("type")))
                    continue
                cmd = h.get("command")
                if not isinstance(cmd, str) or not cmd.strip():
                    continue
                try:
                    secs = float(h.get("timeout") or 0)
                except (TypeError, ValueError):
                    secs = 0.0
                ms = int(secs * 1000) if secs > 0 else _DEFAULT_TIMEOUT_MS
                out.append(_Hook(
                    event=event, matcher=rx,
                    command=_substitute(cmd, workspace, plugin_root),
                    timeout_ms=max(1000, min(ms, _MAX_TIMEOUT_MS)),
                    #: 只认真布尔（与其余布尔配置同一条：`"true"` 不算）。
                    is_async=h.get("async") is True,
                ))
    return out


#: 配置缓存的签名 → (钩子列表)
_cache_key: tuple = ()
_cache_hooks: list = []


def _package_hook_files() -> list:
    """用户自己放的插件包里的钩子配置。返回 `[(路径, 插件根, 是否来自工作区)]`。

    只扫用户放的三个包根，不扫其他产品的插件缓存：钩子会执行命令，
    来源限定在用户明确放置的目录。工作区里那两个根（`plugins/`、
    `.agents/plugins`）跟着仓库走 —— 它们的贡献要先过
    `plugins_allow_workspace`（见 `_load`）。
    """
    from pathlib import Path as _Path
    roots = [_Path("plugins"),
             _Path(".agents") / "plugins",
             _Path.home() / ".agents" / "plugins"]
    out = []
    try:
        from core import plugin_packages as _pp
        for pkg in _pp.discover_packages(roots):
            for rel in ("hooks/hooks.json", "hooks.json"):
                p = pkg.root / rel
                if p.is_file():
                    try:
                        root = pkg.root.resolve()
                    except OSError:
                        root = pkg.root
                    out.append((p, root, bool(pkg.from_workspace)))
                    break
    except Exception as exc:                                  # noqa: BLE001
        _warn_once("packages", "读取插件包钩子失败（%s: %s）"
                   % (type(exc).__name__, exc))
    return out


def _files(workspace: Path) -> list:
    """`[(路径, 是否项目级, 插件根, 是否来自工作区的插件包)]`。"""
    files = [(_GLOBAL, False, None, False), (_USER_SETTINGS, False, None, False)]
    for rel in _PROJECT_FILES:
        files.append((workspace / rel, True, None, False))
    for path, root, from_ws in _package_hook_files():
        files.append((path, False, root, from_ws))
    return files


def _load(workspace: Path) -> list:
    global _cache_key, _cache_hooks
    allowed = _project_allowed()
    plugin_allowed = _plugins_allowed()
    parts, key = [], []
    for path, project, plugin_root, from_ws in _files(workspace):
        if project and not allowed:
            if path.exists():
                _warn_once("trust:" + str(path),
                           "工作区里的钩子配置没有执行：项目级钩子要先在"
                           "「设置 → 安全设置 → 工作区钩子」里打开")
            continue
        if from_ws and not plugin_allowed:
            if path.exists():
                _warn_once("plugin-trust:" + str(path),
                           "工作区插件包里的钩子没有执行：要先在"
                           "「设置 → 安全设置 → 工作区插件」里打开")
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        key.append((str(path), st.st_mtime_ns, st.st_size))
        parts.append((path, plugin_root))
    key = tuple(key)
    if key == _cache_key:
        return _cache_hooks
    hooks: list = []
    for path, plugin_root in parts:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _warn_once("read:" + str(path),
                       "%s 读不出来（%s）—— 这一份跳过" % (path, exc))
            continue
        if plugin_root is not None:
            print("[hooks] 插件包钩子已加载：%s" % path)
        hooks.extend(parse_config(raw, workspace=workspace, source=str(path),
                                  plugin_root=plugin_root))
    _cache_key, _cache_hooks = key, hooks
    return hooks


def _workspace() -> Path:
    try:
        from core.workspace import primary_output_dir
        return Path(primary_output_dir()).resolve()
    except Exception:                                  # noqa: BLE001
        return Path.cwd().resolve()


def active() -> bool:
    """有没有可执行的钩子（决定是否关闭预取）。"""
    return bool(_load(_workspace()))


def reset() -> None:
    """测试用：丢掉缓存与"已经提醒过"的记账。"""
    global _cache_key, _cache_hooks
    _cache_key, _cache_hooks = (), []
    _warned.clear()
    _started.clear()
    _source_hint.clear()
    #: 只丢引用、不 join —— 它们是守护线程，卡住的那些不该拖住测试。
    _async_threads.clear()


# 匹配与执行

def matches(hook: _Hook, tool_name: str) -> bool:
    if hook.event == "Stop":
        return True
    if hook.matcher is None:
        return True
    subjects = [tool_name, *_ALIASES.get(tool_name, ())]
    return any(hook.matcher.search(s) for s in subjects)


def _run(hook: _Hook, payload: dict, workspace: Path,
         force_sync: bool = False) -> dict:
    """跑一条钩子。返回 `{"blocked","ask","allow",...}`。

    `force_sync=True` 或没写 `async`，现跑现等；否则起线程就返回 `{}`
      （"钩子什么都没说"，见模块头的「异步」那一段）。
    """
    if hook.is_async and not force_sync:
        _spawn_async(hook, payload, workspace)
        return {}
    return _run_now(hook, payload, workspace)


#: 正在后台跑的钩子线程（测试 join 用；守护线程，进程退出不阻塞）。
_async_threads: set = set()


def _spawn_async(hook: _Hook, payload: dict, workspace: Path) -> None:
    """后台跑一条钩子，结果丢掉。绝不抛、绝不等。"""
    def _worker():
        try:
            out = _run_now(hook, payload, workspace)
        except Exception as exc:                           # noqa: BLE001
            print("[hooks] 后台钩子异常（%s：%s）" % (type(exc).__name__, exc))
            return
        finally:
            _async_threads.discard(threading.current_thread())
        if out.get("blocked") or out.get("ask"):
            print("[hooks] 后台钩子想拦下/询问这一步，但它标了 async ⇒ 弃权：%s"
                  % hook.command)
        elif out.get("context") or out.get("args") is not None:
            print("[hooks] 后台钩子回了上下文/参数，但它标了 async ⇒ 送不回去：%s"
                  % hook.command)
    t = threading.Thread(target=_worker, daemon=True)
    _async_threads.add(t)
    t.start()


def _run_now(hook: _Hook, payload: dict, workspace: Path) -> dict:
    """真的起进程、等它跑完（同步那条路，`_run` 的一半）。"""
    bash = bash_path()
    if not bash:
        return {}
    env = clean_env()
    env["CLAUDE_PROJECT_DIR"] = str(workspace)
    env["CRANNIA_PROJECT_DIR"] = str(workspace)
    try:
        proc = subprocess.run(
            [bash, "-c", hook.command], input=json.dumps(payload, ensure_ascii=False),
            cwd=str(workspace), env=env, text=True, encoding="utf-8",
            errors="replace", capture_output=True,
            timeout=hook.timeout_ms / 1000.0,
        )
    except subprocess.TimeoutExpired:
        _warn_once("timeout:" + hook.command,
                   "钩子超时（%d ms）：%s" % (hook.timeout_ms, hook.command))
        return {}
    except OSError as exc:
        _warn_once("spawn:" + hook.command, "钩子起不来（%s）：%s" % (exc, hook.command))
        return {}

    if proc.returncode == 2:
        reason = (proc.stderr or proc.stdout or "").strip()
        return {"blocked": True, "reason": reason or "钩子返回了 2"}
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout or "")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}

    out: dict = {}
    specific = data.get("hookSpecificOutput")
    if isinstance(specific, dict):
        decision = specific.get("permissionDecision")
        if decision == "allow":
            out["allow"] = True
        elif decision == "deny":
            out["blocked"] = True
            out["reason"] = (specific.get("permissionDecisionReason")
                             or "钩子要求拦截")
        elif decision == "ask":
            # 不是拦下 —— 这是"交给用户决定"。上游把它变成一次待审批
            #   （`hook_ask`）；`PermissionRequest` 那一侧则当作"照常弹窗"。
            out["ask"] = True
            out["reason"] = (specific.get("permissionDecisionReason")
                             or "钩子要求人工确认")
        if isinstance(specific.get("additionalContext"), str):
            out["context"] = specific["additionalContext"]
        if isinstance(specific.get("updatedInput"), dict):
            out["updated_input"] = specific["updatedInput"]
    if data.get("decision") == "block":
        out["blocked"] = True
        out["reason"] = data.get("reason") or out.get("reason") or "钩子拦下了这一步"
    if not out.get("context") and isinstance(data.get("additionalContext"), str):
        out["context"] = data["additionalContext"]
    return out


def _base(event: str, session_id: str, workspace: Path) -> dict:
    return {"hook_event_name": event, "session_id": session_id or "",
            "cwd": str(workspace), "timestamp": int(time.time())}


def _collect(event: str, tool_name: str, payload: dict, workspace: Path,
             force_sync: bool = False) -> dict:
    merged: dict = {"blocked": False, "ask": False, "allow": False,
                    "reason": "", "context": "", "args": None}
    for hook in _load(workspace):
        if hook.event != event or not matches(hook, tool_name):
            continue
        out = _run(hook, payload, workspace, force_sync=force_sync)
        if out.get("context"):
            merged["context"] = (merged["context"] + "\n" + out["context"]).strip()
        if out.get("updated_input") is not None:
            merged["args"] = {**(merged["args"] or {}),
                              **out["updated_input"]}
        # 只有 `blocked` / `ask` 会短路 —— 它们改变的是"这一步做不做"。
        #   `allow` 不短路：后一条钩子仍可以说不行（顺序 = 配置里的顺序）。
        if out.get("blocked"):
            merged["blocked"] = True
            merged["reason"] = out.get("reason") or ""
            break
        if out.get("ask"):
            merged["ask"] = True
            merged["reason"] = out.get("reason") or ""
            break
        if out.get("allow"):
            merged["allow"] = True
    return merged


def pre_tool(session_id: str, tool_name: str, tool_args: dict,
             call_id: str = "") -> dict:
    """返回 `{"blocked","ask","reason","args"}`。

    * `blocked`，这一步不许做（回执里说明理由）；
    * `ask`，不许直接做，但可以问用户（框架据此挂起一次 `hook_ask`）；
    * `args`，非 `None` 时用它替换入参（包括 `ask` 那一支：用户同意之后
                  重放用的是改过的参数）。
    """
    ws = _workspace()
    if not _load(ws):
        return {"blocked": False, "ask": False, "reason": "", "args": None}
    payload = {**_base("PreToolUse", session_id, ws),
               "tool_name": tool_name, "tool_input": tool_args,
               "tool_use_id": call_id}
    out = _collect("PreToolUse", tool_name, payload, ws)
    return {"blocked": bool(out["blocked"]), "ask": bool(out["ask"]),
            "reason": out["reason"], "args": out["args"]}


def permission_request(session_id: str, tool_name: str, tool_args: dict, *,
                       kind: str = "", subject: str = "", detail: str = "",
                       call_id: str = "") -> dict:
    """框架正要弹审批浮窗之前问一次。返回 `{"allow","deny","reason"}`。

    * `allow`，不发问：调用方发一次性授权并把那一步做掉；
    * `deny`，不发问：那一步回一句拒绝（带路线）；
    * 两个都假，照常弹出浮窗。

    与 `pre_tool` 的 `allow` 不是同一件事（见模块头那段）：这里框架
       已经知道被审批物（`kind` + `subject`），所以"替人批准"兑现得了。
    这是用户自己配的命令，它说行就是行；但它不能绕过 `deny` 档
       （出网 / 命令的确定性拒绝在闸里，根本走不到弹窗这一步）。
    """
    ws = _workspace()
    if not _load(ws):
        return {"allow": False, "deny": False, "reason": ""}
    payload = {**_base("PermissionRequest", session_id, ws),
               "tool_name": tool_name, "tool_input": tool_args,
               "tool_use_id": call_id,
               "permission": {"kind": kind, "subject": subject,
                              "detail": str(detail or "")[:4000]}}
    out = _collect("PermissionRequest", tool_name, payload, ws)
    return {"allow": bool(out["allow"]), "deny": bool(out["blocked"]),
            "reason": out["reason"]}


def post_tool(session_id: str, tool_name: str, tool_args: dict,
              result_text: str, call_id: str = "") -> dict:
    """返回 `{"blocked","reason","context"}`。"""
    ws = _workspace()
    if not _load(ws):
        return {"blocked": False, "reason": "", "context": ""}
    payload = {**_base("PostToolUse", session_id, ws),
               "tool_name": tool_name, "tool_input": tool_args,
               "tool_use_id": call_id, "tool_response": result_text}
    return _collect("PostToolUse", tool_name, payload, ws)


#: 已经跑过 SessionStart 的存储 id（进程级；`/clear` 会换新 id，自然再来一次）。
_started: set = set()
#: `/clear` 刚铸的 id → 下一次 SessionStart 的 `source`（一次性）。
_source_hint: dict = {}


def note_session_source(storage_id: str, source: str) -> None:
    """`/clear` 之后告诉下一次 `session_start`：来源是 `clear`。"""
    if storage_id:
        _source_hint[str(storage_id)] = str(source)


def take_session_source(storage_id: str) -> str:
    return _source_hint.pop(str(storage_id or ""), "")


def session_start(session_id: str, source: str = "startup") -> dict:
    """会话开始。每个存储 id 只跑一次；返回 `context`（可追加给模型）。

    强制同步：这一个事件的产出（`additionalContext`）要进模型上下文，
      标了 `async` 就等于白跑 —— 等它跑完，那一轮已经把请求发出去了。
    """
    sid = str(session_id or "")
    if sid and sid in _started:
        return {"blocked": False, "reason": "", "context": ""}
    ws = _workspace()
    if not _load(ws):
        return {"blocked": False, "reason": "", "context": ""}
    if sid:
        _started.add(sid)
    payload = {**_base("SessionStart", sid, ws), "source": source}
    return _collect("SessionStart", "", payload, ws, force_sync=True)


def user_prompt_submit(session_id: str, prompt: str) -> dict:
    """用户发来一句话。可以拦下（`blocked`）或追加 `context`。"""
    ws = _workspace()
    if not _load(ws):
        return {"blocked": False, "reason": "", "context": ""}
    payload = {**_base("UserPromptSubmit", session_id, ws),
               "prompt": str(prompt or "")}
    return _collect("UserPromptSubmit", "", payload, ws)


def user_prompt_expansion(session_id: str, original: str, expanded: str,
                          command: str = "") -> dict:
    """斜杠命令展开成提示词之后。可以追加 `context`。

    `command` 是命令名（不含 `/`）；不是命令触发的展开时为空串。
    """
    ws = _workspace()
    if not _load(ws):
        return {"blocked": False, "reason": "", "context": ""}
    payload = {**_base("UserPromptExpansion", session_id, ws),
               "prompt": str(original or ""),
               "expanded_prompt": str(expanded or ""),
               "command": str(command or "")}
    return _collect("UserPromptExpansion", "", payload, ws)


def stop_failure(session_id: str, stage: str = "", error: str = "") -> dict:
    """一轮以错误结束。通知型：`blocked` 被忽略，只收它的 `context`。

    触发条件：只在阶段抛异常时触发，用户主动打断与收尾刹车不算 ——
    那两种是"停下"，不是"坏了"，报成失败会让模型去修一件没坏的事。
    """
    ws = _workspace()
    if not _load(ws):
        return {"blocked": False, "reason": "", "context": ""}
    payload = {**_base("StopFailure", session_id, ws),
               "failed_stage": str(stage or ""),
               "error": str(error or "")}
    return _collect("StopFailure", "", payload, ws)


def post_tool_failure(session_id: str, tool_name: str, tool_args: dict,
                      error, call_id: str = "") -> dict:
    """工具抛异常之后。可以追加 `context`，或用 `blocked` 把错误换成一句话。"""
    ws = _workspace()
    if not _load(ws):
        return {"blocked": False, "reason": "", "context": ""}
    payload = {**_base("PostToolUseFailure", session_id, ws),
               "tool_name": tool_name, "tool_input": tool_args,
               "tool_use_id": call_id,
               "error": {"type": type(error).__name__,
                          "message": str(error)}}
    return _collect("PostToolUseFailure", tool_name, payload, ws)


def stop(session_id: str, stop_active: bool = False) -> dict:
    """返回 `{"blocked","reason","context"}`；`blocked` 时本轮继续。"""
    ws = _workspace()
    if not _load(ws):
        return {"blocked": False, "reason": "", "context": ""}
    payload = {**_base("Stop", session_id, ws),
               "stop_hook_active": bool(stop_active)}
    return _collect("Stop", "", payload, ws)
