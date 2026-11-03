"""代码符号查询：定义 / 引用 / 悬停 / 文件内符号 / 全局符号。

## 它是什么

一个 LSP 客户端 + 一个工具。语言服务器不在本仓：它们由使用方声明，
本模块只负责拉起进程、握手、发请求、把结果整理成纯文本。

## 服务器从哪来（三层，按优先级）

1. 显式配置：项目根 `<工作目录>/lsp.json`；没有项目文件时用 `data/lsp.json`；
2. 技能声明：`skills/<名>/lsp.json`（与常见插件的同形文件）；
3. 目录探测：内置一张常见服务器表，只启用本机装好了的那些（不安装）。

第 1 层里"项目根"那一份默认不读（`lsp_allow_project`，同 `core/hooks.py`）：
那份文件跟着仓库走（clone / 解压 / 同事发来的目录都会带一份），而它写的是
任意命令（`command` / `args` / `env`），读它 = 让仓库作者在你的机器上起进程。
关着时它不读、控制台印一行说明（不是静默忽略）；
`data/lsp.json` 与目录探测不受影响（那两样不在仓库里）。
工作区里的 `node_modules/.bin` 同理，走同一个开关。

形状（与部署配置一致：`servers` 是对象，键就是服务器名）：

```json
{"servers": {"python": {"command": "pyright-langserver",
                        "args": ["--stdio"],
                        "extensionToLanguage": {".py": "python"}}}}
```

- `command` 可以是字符串，也可以是数组（数组形式不再拼 `args`）；
- `extensionToLanguage` 是「扩展名 → 语言 id」，至少一条、扩展名以 `.` 开头；
- 可选：`env`（合并进子进程环境）、`initializationOptions`（原样传给服务器）；
- 更早的形状（`servers` 是列表、用 `extensions` + `language_id`）仍然可用，
  读取时提示一次 —— 那是本仓自己 v0.11.686 那一版写下的格式（`code_intel.py`
  先有、`language_id` 是 v0.11.692 才加的兼容），不是外部演进史。

读不到 / 解不开，当作没有配置（返回空列表），并打一行原因 ——
`lsp.json` 坏掉时让整次启动失败是更坏的方向。

## 工具出现条件

`config_present()` 为真时才注册。**没配服务器的机器，这个工具的 schema
一个字节都不发**（每请求零成本）；配了之后由 `search_tools` 按需发现。

## 边界

- 只读：不写文件、不改工作区。
- 位置参数与编辑器一致：行、列都从 1 起；列在内部换算成 UTF-16 偏移
  （LSP 的口径），中文/emoji 不会错位。
- 服务器没有响应，超时 + 杀掉那个进程，回执里说清"没等到响应"，
  不猜结果（"读不到 ≠ 空结果"）。
- 结果条数有上限；截断时说数量，不是静默砍掉。
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote as _urlquote

from core.shell import clean_env

__all__ = [
    "CONFIG_NAME", "GLOBAL_CONFIG", "SCHEMA", "TOOL_NAME",
    "load_servers", "config_present", "configure", "reset",
    "run_operation", "shutdown_all", "code_intel", "diagnose",
]

#: 项目根的声明文件（相对会话工作目录）
CONFIG_NAME = "lsp.json"
#: 全局兜底
GLOBAL_CONFIG = Path("data/lsp.json")

#: 工作区配置的信任门（`data/settings.json` 的 `lsp_allow_project`）。
#:
#: 判据与 `core/hooks.py` 的 `hooks_allow_project`逐字同一条：
#: 那两份文件都跟着仓库走（clone / 解压 / 同事发来的目录都会带一份），
#: 而它们的内容是任意命令（`command` / `args` / `env`）—— 读它就等于让
#: 仓库作者在你的机器上起进程。默认关：关着时那一份不读，
#: 但出声（console 一行），不静默忽略。
#: 全局那份 `data/lsp.json` 与"本机装好了哪个服务器"的目录探测不受它影响
#:   —— 那两样都不在仓库里。
_SETTINGS = Path("data/settings.json")

#: 已经提示过的工作区配置（同一条只吵一次）。
_warned: set[str] = set()

TOOL_NAME = "code_intel"

_MAX_RESULTS = 100
_DEFAULT_TIMEOUT = 15.0


def _project_allowed() -> bool:
    """工作区里的 `lsp.json` / `node_modules/.bin` 能不能用。只认真布尔 `true`。

    读不到 / 坏 JSON / 缺键，False（同 `core.hooks._project_allowed`）。
    每次现读，不缓存 —— 设置页那一格改完当场生效，不用重启。
    """
    try:
        data = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("lsp_allow_project") is True


def _warn_skipped(path: Path) -> None:
    """工作区配置被信任门挡下时出声（同一条只吵一次）。"""
    key = str(path)
    if key in _warned:
        return
    _warned.add(key)
    print("[code_intel] 工作区里的 %s **没有读**：项目级 LSP 配置要先在"
          "「设置 → 安全设置 → 工作区 LSP 配置」里打开（它配的是任意命令，"
          "而这份文件跟着仓库走）。全局的 `data/lsp.json` 不受影响。" % path)

_LANG_BY_EXT = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescriptreact",
    ".js": "javascript", ".jsx": "javascriptreact", ".mjs": "javascript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".lua": "lua",
    ".sh": "shellscript", ".ps1": "powershell",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".md": "markdown", ".css": "css", ".scss": "scss", ".html": "html",
    ".sql": "sql", ".vue": "vue", ".svelte": "svelte",
}

# 测试 / 嵌入方可以显式给一份服务器表；为空表时走文件。
_override: Optional[list] = None
#: "这台机器配没配服务器"决定工具进不进 `tools=`，而那条路每请求都走。
#:缓存一次结果，但每 2 秒重看一眼文件签名：加/删 `lsp.json` 之后
#: 下一次请求就能生效（代价是几次 `stat`，不是解析）。
_present_cache: Optional[bool] = None
_present_at: float = 0.0
_PRESENT_TTL_S = 2.0

#: 冷启动那一次诊断的宽限：服务器刚起来要建索引，1.5 秒常常不够。
#: 只有这个服务器一份文件都没打开过时才用；之后仍按短超时。
_COLD_DIAG_TIMEOUT_S = 8.0


class CodeIntelError(Exception):
    """可预期的失败（没配置 / 找不到服务器 / 超时）。消息直接给人看。"""


# 配置

#: 常见服务器目录：只在本机装好时启用（探测，不安装）。
#: 每项 `commands` 按顺序试第一个能找到的。
_CATALOG = (
    {"id": "python",
     "commands": (("pyright-langserver", ("--stdio",)), ("pylsp", ())),
     "ext_map": {".py": "python", ".pyi": "python"}},
    {"id": "typescript",
     "commands": (("typescript-language-server", ("--stdio",)),),
     "ext_map": {".ts": "typescript", ".tsx": "typescriptreact",
                 ".js": "javascript", ".jsx": "javascriptreact",
                 ".mjs": "javascript", ".cjs": "javascript"}},
    {"id": "go", "commands": (("gopls", ()),), "ext_map": {".go": "go"}},
    {"id": "rust", "commands": (("rust-analyzer", ()),),
     "ext_map": {".rs": "rust"}},
    {"id": "clangd", "commands": (("clangd", ()),),
     "ext_map": {".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
                 ".hpp": "cpp", ".cxx": "cpp"}},
    {"id": "lua", "commands": (("lua-language-server", ()),),
     "ext_map": {".lua": "lua"}},
    {"id": "ruby", "commands": (("ruby-lsp", ()),),
     "ext_map": {".rb": "ruby"}},
    {"id": "php", "commands": (("intelephense", ("--stdio",)),),
     "ext_map": {".php": "php"}},
    {"id": "bash", "commands": (("bash-language-server", ("start",)),),
     "ext_map": {".sh": "shellscript", ".bash": "shellscript"}},
    {"id": "yaml", "commands": (("yaml-language-server", ("--stdio",)),),
     "ext_map": {".yaml": "yaml", ".yml": "yaml"}},
    {"id": "json",
     "commands": (("vscode-json-language-server", ("--stdio",)),),
     "ext_map": {".json": "json"}},
)

_catalog_cache: dict = {}
_CATALOG_TTL_S = 60.0
_plugin_cache: tuple = ()
_plugin_specs_cache: list = []
_legacy_warned = False


def _norm_ext_map(item) -> dict:
    """`extensionToLanguage`（新）或 `extensions` + `language_id`（旧）。"""
    m = item.get("extensionToLanguage")
    out: dict = {}
    if isinstance(m, dict) and m:
        for k, v in m.items():
            key = str(k or "").strip().lower()
            if not key:
                continue
            out[key if key.startswith(".") else "." + key] = \
                str(v or "").strip() or "plaintext"
        return out
    exts = item.get("extensions")
    if not isinstance(exts, list):
        return {}
    lang_default = str(item.get("language_id") or "").strip()
    for e in exts:
        key = str(e or "").strip().lower()
        if not key:
            continue
        key = key if key.startswith(".") else "." + key
        out[key] = lang_default or _LANG_BY_EXT.get(key, "plaintext")
    return out


def _spec_from(item: dict, ident: str, *, source: str = "") -> Optional[dict]:
    name = str(item.get("name") or ident or "").strip()
    cmd = item.get("command")
    if isinstance(cmd, str):
        argv = [cmd]
    elif isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd):
        argv = list(cmd)
    else:
        print("[code_intel] `%s` 的 command 形状不对，已跳过" % (name or ident))
        return None
    ext_map = _norm_ext_map(item)
    if not ext_map:
        print("[code_intel] `%s` 没有扩展名映射，已跳过" % (name or ident))
        return None
    args = item.get("args")
    if not isinstance(args, list):
        args = []
    env = item.get("env")
    if not isinstance(env, dict):
        env = {}
    return {
        "id": name or ident,
        "name": name or ident,
        "command": argv,
        "args": [str(a) for a in args],
        "ext_map": ext_map,
        "env": {str(k): str(v) for k, v in env.items()},
        "init_options": item.get("initializationOptions"),
    }


def _parse_specs(raw, *, source: str = "") -> list[dict]:
    """收干净一份 `servers`。两种形状都收：

    - 新（推荐）：`{"python": {"command": ..., "args": [...],
      "extensionToLanguage": {".py": "python"}}}`；
    - 旧：`[{"name": ..., "command": ..., "extensions": [".py"]}]`。
    """
    global _legacy_warned
    out: list[dict] = []
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list):
        if not _legacy_warned:
            _legacy_warned = True
            print("[code_intel] 配置用的是旧的列表格式 —— 仍可用，"
                  "建议改成 `servers` 对象（键就是服务器名）")
        items = []
        for i, it in enumerate(raw):
            ident = (str(it.get("name") or ("server-%d" % (i + 1)))
                     if isinstance(it, dict) else ("server-%d" % (i + 1)))
            items.append((ident, it))
    else:
        return out
    for ident, item in items:
        if not isinstance(item, dict):
            print("[code_intel] `%s` 不是对象，已跳过" % ident)
            continue
        spec = _spec_from(item, str(ident), source=source)
        if spec is not None:
            out.append(spec)
    return out


def _read_config_file(path: Path) -> list[dict]:
    try:
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("[code_intel] %s 读不出来（%s: %s）—— 当作没有配置"
              % (path, type(exc).__name__, exc))
        return []
    if not isinstance(data, dict):
        print("[code_intel] %s 顶层不是对象 —— 当作没有配置" % path)
        return []
    return _parse_specs(data.get("servers"), source=str(path))


def _plugin_specs() -> list[dict]:
    """技能目录里的 `skills/<名>/lsp.json`（与常见插件同形）。"""
    global _plugin_cache, _plugin_specs_cache
    from core import paths as _paths
    files = sorted(_paths.asset_dir("skills").glob("*/lsp.json"))
    sig = []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        sig.append((str(p), st.st_mtime_ns, st.st_size))
    sig = tuple(sig)
    if sig == _plugin_cache:
        return list(_plugin_specs_cache)
    out: list[dict] = []
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print("[code_intel] %s 读不出来（%s）—— 跳过" % (p, exc))
            continue
        section = data.get("servers") if isinstance(data, dict) else None
        out.extend(_parse_specs(section, source=str(p)))
    _plugin_cache, _plugin_specs_cache = sig, out
    return list(out)


def _find_binary(name: str, root: Optional[Path]) -> str:
    bases = []
    # 工作区里的 `node_modules/.bin` 也是仓库可控的可执行文件（挑一个叫
    #   `pyright-langserver` 的放进去就行），与 `lsp.json` 同一个信任门。
    if root is not None and _project_allowed():
        bases.append(Path(root) / "node_modules" / ".bin")
    bases.append(Path.cwd() / "node_modules" / ".bin")
    for base in bases:
        for suffix in ("", ".cmd", ".exe"):
            p = base / (name + suffix)
            try:
                if p.is_file():
                    return str(p)
            except OSError:
                pass
    return shutil.which(name) or ""


def _detected(root: Optional[Path]) -> list[dict]:
    """目录探测：本机装好了哪个就启用哪个。不安装任何东西。"""
    #: 信任门进缓存键：否则改完设置页那一格，最多要等 `_CATALOG_TTL_S`
    #:   才认（而那期间"看着开了、实际没开"，正是标签在说谎）。
    key = (str(root or Path.cwd()), _project_allowed())
    now = time.monotonic()
    hit = _catalog_cache.get(key)
    if hit and now - hit[0] < _CATALOG_TTL_S:
        return list(hit[1])
    out: list[dict] = []
    for entry in _CATALOG:
        for cmd, args in entry["commands"]:
            found = _find_binary(cmd, root)
            if found:
                out.append({
                    "id": entry["id"], "name": entry["id"],
                    "command": [found], "args": list(args),
                    "ext_map": dict(entry["ext_map"]),
                    "env": {}, "init_options": None,
                })
                break
    _catalog_cache[key] = (now, out)
    return list(out)


def load_servers(root: Optional[Path] = None) -> list[dict]:
    """合并服务器表。优先级：显式配置 > 技能声明 > 目录探测。

    显式配置里项目根优先（有项目文件就不再看全局那份）。
    同一个 id 只保留优先级最高的那一条。
    """
    if _override is not None:
        return list(_override)
    explicit: list[dict] = []
    if root is not None:
        project = Path(root) / CONFIG_NAME
        # 信任门：那一份跟着仓库走，默认不读（见 `_project_allowed`）。
        #   关着但文件在，出声，否则"我配了却没生效"看起来像文件写错了。
        if _project_allowed():
            explicit = _read_config_file(project)
        elif project.is_file():
            _warn_skipped(project)
    if not explicit:
        explicit = _read_config_file(GLOBAL_CONFIG)
    merged, seen = [], set()
    for spec in explicit + _plugin_specs() + _detected(root):
        if spec["id"] in seen:
            continue
        seen.add(spec["id"])
        merged.append(spec)
    return merged


def config_present(root: Optional[Path] = None) -> bool:
    """有没有可用的服务器（决定工具进不进 `tools=`）。

    每 2 秒重算一次：新增/删除 `lsp.json`、技能声明或装好一个新服务器之后，
    下一次请求就生效，不需要重启框架。
    """
    global _present_cache, _present_at
    if _override is not None:
        return bool(_override)
    now = time.monotonic()
    if _present_cache is None or now - _present_at > _PRESENT_TTL_S:
        _present_cache = bool(load_servers(root))
        _present_at = now
    return _present_cache


def configure(servers: Optional[list]) -> None:
    """显式给一份服务器表（`None` = 回到读文件）。"""
    global _override, _present_cache, _present_at
    _override = None if servers is None else _parse_specs(servers)
    _present_cache = None
    _present_at = 0.0


def reset() -> None:
    """丢掉显式表并关掉所有服务器进程（测试用）。"""
    global _override, _present_cache, _present_at
    global _catalog_cache, _plugin_cache, _plugin_specs_cache
    _override = None
    _present_cache = None
    _present_at = 0.0
    _catalog_cache.clear()
    _plugin_cache, _plugin_specs_cache = (), []
    _warned.clear()
    shutdown_all()


# 工作目录

def _workspace_root() -> Path:
    try:
        from core.workspace import primary_output_dir
        return Path(primary_output_dir()).resolve()
    except Exception:                                  # noqa: BLE001
        return Path.cwd().resolve()


def _uri(path: Path) -> str:
    p = str(Path(path).resolve()).replace("\\", "/")
    if p.startswith("//"):                 # UNC
        return "file:" + _urlquote(p)
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + _urlquote(p)


_DRIVE_URI_RE = re.compile(r"^(file:///)([A-Za-z])(%3A|:)")


def _uri_key(uri: str) -> str:
    """比较 URI 用的键：只把盘符归一成小写。

    Windows 上同一个文件有两种合法写法：我们发的是 `file:///D%3A/...`，
    而语言服务器回来的是 `file:///d%3A/...`。按原文比对，诊断永远对不上号，
    而症状是"诊断没报告"（与"没配服务器"长得一模一样）。
    只动盘符：路径本身在别的系统上大小写敏感，不能一起降。
    """
    text = str(uri or "")
    m = _DRIVE_URI_RE.match(text)
    if m:
        return m.group(1) + m.group(2).lower() + m.group(3) + text[m.end():]
    return text


def _resolve_file(file_path: str, root: Path) -> Path:
    """相对路径落成绝对路径：先按原样（进程 cwd），再试工作区。

    与文件原语五件套同一个顺序。反过来的话，模型刚 `read_file("core/x.py")`
    读到的那个相对路径，交给本工具就变成"找不到" —— 而两句话都对着同一个文件。
    """
    p = Path(file_path)
    if p.is_absolute():
        return p
    if p.is_file():
        return p.resolve()
    return (root / p).resolve()


def _path_of_uri(uri: str) -> Path:
    if not uri.startswith("file:"):
        return Path(uri)
    from urllib.parse import unquote as _unquote
    raw = _unquote(uri[5:])
    if raw.startswith("//"):
        raw = raw[2:]
    if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return Path(raw)


# 服务器进程

def _spawn_argv(spec: dict) -> list[str]:
    """把 `command` 解成能直接起进程的形式。

    Windows 上 npm 一族的命令是 `.cmd` shim，`CreateProcess` 不能直接执行
这一类包一层 `cmd /c`。解析不到的路径原样交给 Popen（让它报错）。
    """
    argv = list(spec["command"]) + list(spec["args"])
    head = argv[0]
    found = shutil.which(head)
    if found:
        argv[0] = found
        head = found
    if os.name == "nt" and head.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", *argv]
    return argv


class _Client:
    """一个 (服务器, 根目录) 的会话。进程懒起，用完由 `shutdown_all` 收。"""

    def __init__(self, spec: dict, root: Path):
        self.spec = spec
        self.root = root
        self.proc = None
        self._id = 0
        self._pending: dict = {}
        self._write_lock = threading.Lock()
        self._opened: dict = {}
        self._closed = False
        self.caps: dict = {}
        #: uri -> (收到时间, 诊断列表)；由服务器主动推过来。
        self._diag: dict = {}
        self._diag_events: dict = {}
        self._diag_lock = threading.Lock()

    # -- 进程 ---------------------------------------------------------
    def start(self) -> None:
        if self.proc is not None:
            return
        argv = _spawn_argv(self.spec)
        try:
            env = clean_env()
            env.update({str(k): str(v)
                        for k, v in (self.spec.get("env") or {}).items()})
            self.proc = subprocess.Popen(
                argv, cwd=str(self.root), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except OSError as exc:
            raise CodeIntelError(
                "语言服务器起不来（%s: %s）。检查 `%s` 这条命令是否装了、"
                "在不在 PATH 上。" % (type(exc).__name__, exc, argv[0]))
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._initialize()

    def _drain_stderr(self) -> None:
        try:
            for _ in iter(self.proc.stderr.readline, b""):
                pass
        except Exception:                              # noqa: BLE001
            pass

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        head = ("Content-Length: %d\r\n\r\n" % len(body)).encode("ascii")
        with self._write_lock:
            self.proc.stdin.write(head + body)
            self.proc.stdin.flush()

    def _read_loop(self) -> None:
        stream = self.proc.stdout
        try:
            while True:
                length = None
                while True:
                    line = stream.readline()
                    if not line:
                        return
                    line = line.strip()
                    if not line:
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                if length is None:
                    continue
                body = stream.read(length)
                try:
                    msg = json.loads(body.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                mid = msg.get("id")
                if mid is not None and ("result" in msg or "error" in msg):
                    q = self._pending.pop(mid, None)
                    if q is not None:
                        q.put(msg)
                    continue
                if msg.get("method") == "textDocument/publishDiagnostics":
                    params = msg.get("params") or {}
                    uri = params.get("uri") or ""
                    items = params.get("diagnostics") or []
                    if uri:
                        key = _uri_key(uri)
                        with self._diag_lock:
                            self._diag[key] = (time.time(), items)
                            ev = self._diag_events.get(key)
                        if ev is not None:
                            ev.set()
        except Exception:                              # noqa: BLE001
            pass
        finally:
            self._closed = True
            for q in list(self._pending.values()):
                q.put({"error": {"message": "语言服务器进程结束了"}})
            self._pending.clear()

    def request(self, method: str, params: dict,
                timeout: float = _DEFAULT_TIMEOUT) -> dict:
        self.start()
        if self._closed:
            raise CodeIntelError("语言服务器进程已经退出。")
        with self._write_lock:
            self._id += 1
            mid = self._id
            q: queue.Queue = queue.Queue(maxsize=1)
            self._pending[mid] = q
        self._send({"jsonrpc": "2.0", "id": mid, "method": method,
                    "params": params})
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            self._pending.pop(mid, None)
            self.kill()
            raise CodeIntelError(
                "语言服务器 %s 秒内没有响应（`%s`）—— 已把它关掉。"
                % (timeout, method))
        if "error" in msg:
            err = msg["error"] or {}
            raise CodeIntelError("语言服务器报错：%s"
                                 % (err.get("message") or err))
        return msg.get("result")

    def notify(self, method: str, params: dict) -> None:
        self.start()
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _initialize(self) -> None:
        root_uri = _uri(self.root)
        params = {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": self.root.name}],
            "capabilities": {
                "textDocument": {
                    "definition": {"linkSupport": True},
                    "references": {},
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                }
            },
        }
        if self.spec.get("init_options") is not None:
            params["initializationOptions"] = self.spec["init_options"]
        result = self.request("initialize", params, timeout=20.0)
        if isinstance(result, dict):
            caps = result.get("capabilities")
            self.caps = caps if isinstance(caps, dict) else {}
        self.notify("initialized", {})

    def open_file(self, path: Path) -> None:
        try:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError as exc:
            raise CodeIntelError("读不到 `%s`（%s）。" % (path, exc))
        old = self._opened.get(str(path))
        if old == key:
            return
        text = path.read_text(encoding="utf-8", errors="replace")
        lang = (self.spec.get("ext_map") or {}).get(path.suffix.lower()) or \
            _LANG_BY_EXT.get(path.suffix.lower(), "plaintext")
        uri = _uri(path)
        if old is None:
            self.notify("textDocument/didOpen", {
                "textDocument": {"uri": uri, "languageId": lang,
                                 "version": 1, "text": text},
            })
        else:
            self.notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": 2},
                "contentChanges": [{"text": text}],
            })
        self._opened[str(path)] = key

    def kill(self) -> None:
        proc = self.proc
        self.proc = None
        self._closed = True
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:                              # noqa: BLE001
            pass
        try:
            proc.wait(timeout=3)
        except Exception:                              # noqa: BLE001
            pass

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            self.request("shutdown", None, timeout=2.0)
            self.notify("exit", None)
        except Exception:                              # noqa: BLE001
            pass
        try:
            proc.wait(timeout=2)
        except Exception:                              # noqa: BLE001
            self.kill()


_clients: dict = {}
_clients_lock = threading.Lock()


def _client_for(spec: dict, root: Path) -> _Client:
    key = (spec["name"], str(root))
    with _clients_lock:
        c = _clients.get(key)
        if c is None:
            c = _Client(spec, root)
            _clients[key] = c
    return c


def shutdown_all() -> None:
    with _clients_lock:
        items = list(_clients.items())
        _clients.clear()
    for _key, client in items:
        try:
            client.stop()
        except Exception:                              # noqa: BLE001
            client.kill()


atexit.register(shutdown_all)


# 位置与结果

def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _position(path: Path, line: int, character: int) -> dict:
    """1 起 → 0 起；列换算成 UTF-16 偏移（LSP 的口径）。"""
    if line < 1:
        raise CodeIntelError("line 从 1 起（收到 %d）。" % line)
    if character < 1:
        raise CodeIntelError("character 从 1 起（收到 %d）。" % character)
    try:
        rows = path.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError as exc:
        raise CodeIntelError("读不到 `%s`（%s）。" % (path, exc))
    if line > len(rows):
        raise CodeIntelError("文件只有 %d 行（line=%d）。" % (len(rows), line))
    prefix = rows[line - 1][:character - 1]
    return {"line": line - 1, "character": _utf16_len(prefix)}


def _rng_text(rng: dict) -> str:
    s = (rng or {}).get("start") or {}
    return "%d:%d" % (int(s.get("line", 0)) + 1, int(s.get("character", 0)) + 1)


def _fmt_locations(locs, root: Path) -> str:
    if not locs:
        return "没有结果。"
    if isinstance(locs, dict):
        locs = [locs]
    rows, seen = [], set()
    for loc in locs:
        if not isinstance(loc, dict):
            continue
        uri = loc.get("uri") or loc.get("targetUri")
        rng = (loc.get("range") or loc.get("targetSelectionRange")
               or loc.get("targetRange") or {})
        if not uri:
            continue
        path = _path_of_uri(uri)
        row = "%s:%s" % (_shown_path(path, root), _rng_text(rng))
        if row in seen:
            continue
        seen.add(row)
        rows.append(row)
        if len(rows) >= _MAX_RESULTS:
            break
    if not rows:
        return "没有结果。"
    tail = ""
    if len(locs) > len(rows) or len(rows) >= _MAX_RESULTS:
        tail = "\n（最多显示 %d 条）" % _MAX_RESULTS
    return "\n".join(rows) + tail


def _shown_path(path: Path, root: Path) -> str:
    """回执里的路径：先相对工作区根，再相对进程 cwd，都不在才给绝对路径。

    模型问的是 `core/x.py`，回的若是 `d:/OPD/.../core/x.py`，同一个文件就有了
    两种写法 —— `read_file` 那边是相对写法，两边对不上。
    """
    #: `root` 可能是 8.3 短名（`C:\Users\RUNNER~1\…`），而文件路径是解析后的长名
    #: —— 两个写法都试一遍，否则要么回绝对路径、要么回错。
    for base in (root, Path.cwd()):
        for candidate in (base, Path(base).resolve()):
            try:
                return path.relative_to(candidate).as_posix()
            except ValueError:
                continue
    return path.as_posix()


_POSITION_HINT = ("（空结果多半是**位置没落在符号上**：`line` / `character` 都从 1 起、"
                  "要指着名字本身。拿不准就先 `symbols` 看这个文件里的名字与行号，"
                  "或用 `workspace_symbols` 按名字查。）")


def _locations_or_route(result, root: Path) -> str:
    """位置类操作的空结果必须带路线 —— 否则它读起来像"这个工具坏了"。"""
    text = _fmt_locations(result, root)
    if text != "没有结果。":
        return text
    return text + "\n" + _POSITION_HINT


def _hover_text(result) -> str:
    if not result:
        return "没有悬停信息。"
    contents = result.get("contents")
    parts = []
    if isinstance(contents, str):
        parts.append(contents)
    elif isinstance(contents, dict):
        parts.append(str(contents.get("value") or ""))
    elif isinstance(contents, list):
        for c in contents:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                parts.append(str(c.get("value") or ""))
    text = "\n".join(p for p in parts if p).strip()
    return text or "没有悬停信息。"


def _fmt_symbols(items, root: Path, *, with_path: bool) -> str:
    if not items:
        return "没有结果。"
    rows: list[str] = []

    def walk(nodes, depth):
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            name = str(node.get("name") or "")
            kind = _kind_name(node.get("kind"))
            loc = node.get("location") or node
            # 位置取 `selectionRange`（名字本身）优先 —— `range` 从 `def` 起，
            # 拿它去调 `definition` 常常差几列。
            rng = node.get("selectionRange") or node.get("range") or {}
            where = ""
            if with_path and isinstance(loc, dict):
                uri = loc.get("uri")
                if uri:
                    where = _shown_path(_path_of_uri(uri), root)
                    rng = loc.get("range") or rng
            pos = _rng_text(rng) if rng else ""
            where_txt = ("%s:%s" % (where, pos) if where
                         else ("  %s" % pos if pos else ""))
            rows.append("  " * depth + ("%s [%s]" % (name, kind) + where_txt))
            walk(node.get("children"), depth + 1)

    walk(items, 0)
    if not rows:
        return "没有结果。"
    tail = ""
    if len(rows) > _MAX_RESULTS:
        rows = rows[:_MAX_RESULTS]
        tail = "\n（最多显示 %d 条）" % _MAX_RESULTS
    return "\n".join(rows) + tail


def _ws_symbols(items, root: Path) -> str:
    """全局符号。空结果要说清它的搜索范围只有服务器的工作区根 ——
    否则"没搜到"会被读成"项目里没有这个东西"。"""
    text = _fmt_symbols(items, root, with_path=True)
    if text != "没有结果。":
        return text
    return ("没有结果。（`workspace_symbols` 只搜语言服务器的工作区根：`%s`；"
            "根之外的文件改用 `symbols` 列本文件符号，再按行号调 `definition`。）"
            % root.as_posix())


def _fmt_hierarchy(items, root: Path) -> str:
    """调用层级条目：`名字 [种类] 路径:行:列`（`from`/`to` 两种结果都收）。"""
    if not items:
        return "没有结果。"
    if isinstance(items, dict):
        items = [items]
    rows, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "")
        kind = _kind_name(it.get("kind"))
        uri = it.get("uri")
        rng = it.get("selectionRange") or it.get("range") or {}
        where = ""
        if uri:
            where = _shown_path(_path_of_uri(uri), root)
        row = "%s [%s]" % (name, kind)
        if where:
            row += "  %s:%s" % (where, _rng_text(rng))
        if row in seen:
            continue
        seen.add(row)
        rows.append(row)
        if len(rows) >= _MAX_RESULTS:
            break
    return "\n".join(rows) if rows else "没有结果。"


_KIND_NAMES = {
    1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class",
    6: "method", 7: "property", 8: "field", 9: "constructor", 10: "enum",
    11: "interface", 12: "function", 13: "variable", 14: "constant",
    15: "string", 16: "number", 17: "boolean", 18: "array", 19: "object",
    20: "key", 21: "null", 22: "enum-member", 23: "struct", 24: "event",
    25: "operator", 26: "type-parameter",
}


def _kind_name(kind) -> str:
    try:
        return _KIND_NAMES.get(int(kind), "symbol")
    except (TypeError, ValueError):
        return "symbol"


# 工具入口

_OPERATIONS = ("definition", "references", "hover", "symbols",
               "workspace_symbols", "implementation",
               "prepare_call_hierarchy", "incoming_calls", "outgoing_calls")


def _pick_server(specs: list[dict], path: Path):
    """按扩展名挑服务器。返回 `(规格, 语言 id)`。"""
    ext = path.suffix.lower()
    for spec in specs:
        lang = (spec.get("ext_map") or {}).get(ext)
        if lang:
            return spec, lang
    want = "、".join("`%s`" % s["name"] for s in specs)
    raise CodeIntelError(
        "没有为 `%s` 配置语言服务器（已配置：%s）。要么在 `%s` 里给这个扩展名"
        "加一条，要么换一个能查的文件。" % (ext or "无扩展名", want, CONFIG_NAME))


def run_operation(operation: str, file_path: str = "", line=1, character=1,
                  query: str = "") -> str:
    """一次符号查询。返回纯文本（错误也是文本，不抛给循环）。"""
    try:
        if operation not in _OPERATIONS:
            return ("错误：operation 只能是 %s。" % " / ".join(_OPERATIONS))
        root = _workspace_root()
        specs = load_servers(root)
        if not specs:
            return ("没有配置语言服务器。在项目根写一份 `%s`，"
                    "或在 `data/lsp.json` 里声明。" % CONFIG_NAME)
        if not file_path:
            # 按名字全局找符号不需要一个具体文件；只有多服务器并存时才无从选择。
            if operation == "workspace_symbols":
                if len(specs) == 1:
                    client = _client_for(specs[0], root)
                    client.start()
                    got = client.request("workspace/symbol",
                                         {"query": str(query or "")})
                    return _ws_symbols(got, root)
                return ("错误：没给 `file_path` 时不知道用哪个语言服务器"
                        "（已配置：%s）—— 给一个该语言的 `file_path`。"
                        % "、".join("`%s`" % s["name"] for s in specs))
            return "错误：这次操作需要 `file_path`。"
        path = _resolve_file(file_path, root)
        if not path.is_file():
            return "错误：找不到 `%s`。" % file_path
        spec, _lang = _pick_server(specs, path)
        client = _client_for(spec, root)
        client.start()
        client.open_file(path)

        try:
            line_i = int(line)
            char_i = int(character)
        except (TypeError, ValueError):
            return "错误：line / character 要是整数。"

        if operation == "workspace_symbols":
            result = client.request("workspace/symbol", {"query": str(query or "")})
            return _ws_symbols(result, root)

        pos = _position(path, line_i, char_i)
        uri = _uri(path)
        if operation == "definition":
            result = client.request("textDocument/definition", {
                "textDocument": {"uri": uri}, "position": pos})
            return _locations_or_route(result, root)
        if operation == "references":
            result = client.request("textDocument/references", {
                "textDocument": {"uri": uri}, "position": pos,
                "context": {"includeDeclaration": True}})
            return _locations_or_route(result, root)
        if operation == "hover":
            result = client.request("textDocument/hover", {
                "textDocument": {"uri": uri}, "position": pos})
            text = _hover_text(result)
            if text == "没有悬停信息。":
                text += _POSITION_HINT
            return text
        if operation == "implementation":
            result = client.request("textDocument/implementation", {
                "textDocument": {"uri": uri}, "position": pos})
            return _locations_or_route(result, root)
        if operation in ("prepare_call_hierarchy", "incoming_calls",
                         "outgoing_calls"):
            items = client.request("textDocument/prepareCallHierarchy", {
                "textDocument": {"uri": uri}, "position": pos})
            if operation == "prepare_call_hierarchy":
                return _fmt_hierarchy(items, root)
            if not items:
                return "没有结果。"
            if not isinstance(items, list):
                items = [items]
            method = ("callHierarchy/incomingCalls"
                      if operation == "incoming_calls"
                      else "callHierarchy/outgoingCalls")
            rows: list = []
            for item in items[:5]:
                if not isinstance(item, dict):
                    continue
                got = client.request(method, {"item": item}) or []
                for row in got:
                    if isinstance(row, dict):
                        rows.append(row.get("from") or row.get("to") or row)
            return _fmt_hierarchy(rows, root)
        result = client.request("textDocument/documentSymbol",
                                {"textDocument": {"uri": uri}})
        return _fmt_symbols(result, root, with_path=False)
    except CodeIntelError as exc:
        return "错误：%s" % exc
    except Exception as exc:                           # noqa: BLE001
        return "错误：符号查询失败（%s: %s）。" % (type(exc).__name__, exc)


_SEVERITY = {1: "错误", 2: "警告", 3: "信息", 4: "提示"}


def _format_diagnostics(path: Path, root: Path, items: list) -> str:
    if not items:
        return "没有错误或警告"
    rows, errors, warnings = [], 0, 0
    shown = _shown_path(path, root)
    for d in items[:20]:
        if not isinstance(d, dict):
            continue
        sev = d.get("severity")
        try:
            sev = int(sev) if sev is not None else 1
        except (TypeError, ValueError):
            sev = 1
        if sev == 1:
            errors += 1
        elif sev == 2:
            warnings += 1
        start = ((d.get("range") or {}).get("start") or {})
        line = int(start.get("line", 0)) + 1
        col = int(start.get("character", 0)) + 1
        msg = str(d.get("message") or "").strip().splitlines()
        msg = msg[0][:200] if msg else ""
        rows.append("%s:%d:%d %s %s" % (shown, line, col,
                                        _SEVERITY.get(sev, "信息"), msg))
    if not rows:
        return "没有错误或警告"
    head = "%d 个错误，%d 个警告" % (errors, warnings)
    if len(items) > len(rows):
        head += "，只列前 %d 条" % len(rows)
    return head + "\n" + "\n".join(rows)


def diagnose(path_like, timeout: float = 1.5):
    """写完文件之后取一次诊断。失败一律返回 None，绝不影响写入。

    返回 `{"reported": True, "text": ..., "count": n}`；
    服务器没配置 / 扩展名没配 / 没在超时内给出报告，`None`
    （"没报告"与"没有错误"是两件事 —— 后者返回的是
    `reported=True` 且正文为"没有错误或警告"）。
    """
    try:
        root = _workspace_root()
        specs = load_servers(root)
        if not specs:
            return None
        path = _resolve_file(str(path_like), root)
        if not path.is_file():
            return None
        spec, _lang = _pick_server(specs, path)
        client = _client_for(spec, root)
        # 这个服务器还一份文件都没打开过，它可能刚起（要建索引），给它一段更长的宽限。
        # 之后的诊断沿用短超时：写入不该因为"等报告"而变慢。
        cold = not client._opened
        wait_s = max(timeout, _COLD_DIAG_TIMEOUT_S) if cold else timeout
        client.start()
        uri = _uri(path)
        key = _uri_key(uri)
        # 顺序承重：先登记事件与时间戳，再发 `didOpen`。
        #   反过来的话，快服务器可能在"发出去"与"记时间戳"之间就把报告推回来了，
        #   而下面那道 `got[0] < since` 会把它当成旧报告丢掉，同步返回 `None`，
        #   症状是"偶尔没有诊断"（单跑几乎不复现，并行负载下才露头）。
        # 每次都换一个新 Event：Event 一旦 `set` 就不会自己复位。
        #   复用同一个的话，第二次 `wait()` 立刻返回、手里还是上一份报告，
        #   被下面那道时间戳检查判掉，同一个文件从第二次写入起永远没有诊断
        #   （回执上就是"没有这个字段"，而模型会把它读成"没有错误"）。
        with client._diag_lock:
            ev = threading.Event()
            client._diag_events[key] = ev
        since = time.time() - 0.001
        client.open_file(path)

        if client.caps.get("diagnosticProvider"):
            try:
                res = client.request("textDocument/diagnostic",
                                     {"textDocument": {"uri": uri}},
                                     timeout=wait_s)
                items = (res or {}).get("items") or []
                return {"reported": True,
                        "text": _format_diagnostics(path, root, items),
                        "count": len(items)}
            except CodeIntelError:
                pass

        ev.wait(wait_s)
        with client._diag_lock:
            got = client._diag.get(key)
        if not got or got[0] < since:
            return None
        items = got[1] or []
        return {"reported": True,
                "text": _format_diagnostics(path, root, items),
                "count": len(items)}
    except CodeIntelError:
        return None
    except Exception:                                  # noqa: BLE001
        return None


def code_intel(operation: str, file_path: str = "", line=1, character=1,
               query: str = "") -> str:
    """工具入口（名字与注册名一致）。实现与校验都在 :func:`run_operation`。"""
    return run_operation(operation, file_path, line, character, query)


SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "name": TOOL_NAME,
    "description": (
        "查代码符号：`definition` 跳定义 · `references` 找引用 · `hover` 看类型/文档 · "
        "`symbols` 列本文件符号 · `workspace_symbols` 全项目按名字搜 · "
        "`implementation` 找实现 · `prepare_call_hierarchy` 取调用层级条目 · "
        "`incoming_calls` 谁在调它 · `outgoing_calls` 它在调谁。"
        "位置用**编辑器的数法**（行、列都从 1 起）。需要项目里已配置语言服务器"
        "（`lsp.json`），没配就用不了。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": list(_OPERATIONS)},
            "file_path": {"type": "string",
                          "description": ("文件路径（相对工作目录或绝对）。"
                                          "除 `workspace_symbols` 外都必填")},
            "line": {"type": "integer",
                     "description": "行号（1 起）；要指着标识符那一行"},
            "character": {"type": "integer",
                          "description": "列号（1 起，默认 1）；要指着名字本身"},
            "query": {"type": "string",
                      "description": "workspace_symbols 的搜索词（空串 = 全列）"},
        },
        "required": ["operation"],
    },
}
