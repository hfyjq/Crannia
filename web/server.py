"""
Crannia Web UI — zero-barrier deployment & management console.

Architecture:
  - Flask server on 127.0.0.1:5000 (localhost only)
  - URL token authentication (random per launch)
  - Single-page dashboard: sidebar nav + content area

Routes:
  GET  /?token=xxx            — Dashboard (default: platform config)
  GET  /chat?token=xxx         — CLI-style chat interface
  GET  /models?token=xxx       — API key & model management
  GET  /security?token=xxx     — Security settings
  POST /api/start              — Start the agent framework
  POST /api/chat               — Send a chat message (CLI mode)
  GET  /api/status             — Framework status
  POST /api/save-config        — Save channel / model config
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
import threading
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Load .env so API calls resolve ${VAR} placeholders
# 两份：先代码根那份，再数据根那份（后者压过前者）。
#    安装目录升级时整棵被换掉，用户填在里面的密钥会丢；
#    数据根（`%APPDATA%\Crannia`）升级不动，才放得住。
from core import paths as _paths
_ENV_FILES = _paths.load_env_files()

from core.version import __version__
from core.session_key import session_key_for
from core import history_tools as history_search_module

from flask import Flask, request, jsonify, render_template, redirect, Response
from queue import Empty as _QueueEmpty, Queue as _Queue
from core.i18n import t as tr, tr_fmt as trf

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = secrets.token_hex(32)

# Version injection (single source: core/version.py)
@app.context_processor
def inject_version():
    """Make app version available in all Jinja templates as {{ app_version }}."""
    try:
        from core.version import __version__, __build__
        build_suffix = f" {__build__}" if __build__ else ""
        return {"app_version": f"v{__version__}{build_suffix}"}
    except Exception:
        return {"app_version": "unknown"}

# Auth
#: 启动令牌：由桌面壳生成并经环境变量传进来（`apps/desktop/main.cjs`）；
#: 直接跑 `python -m web`（开发）时自己铸一个。
#: 壳注入那一版不进 `/ping` —— 见下面 `ping()` 的开关。
_LAUNCH_TOKEN = os.environ.get("CRANNIA_LAUNCH_TOKEN") or secrets.token_urlsafe(16)

#: 内部界面（老 WebUI）的总闸 —— 默认关闭。
#:
#: 产品对外只有桌面 GUI；这六个 Jinja 页面（`/` `/chat` `/models` `/logs`
#: `/security` `/skills`）只留作内部快速验证某个功能。
#:
#: 结论：webui 只做内部快速验证某个功能用，GUI 里彻底封死 ——
#: 一个开关，为否时无论怎么样 webui 都叫不出来，除非有人真去改代码；
#: 这样一来，除了碰得到代码的人，其他人都碰不了。
#:
#: 要动它，先想清楚：打开 = 任何本机程序都能通过 `/ping` 拿到令牌
#: （见下）再进这些页面 —— 那是对外暴露面，不是"调试方便"。
#: 内部验证的推荐姿势：在代码里临时改成 `True` 并重启，验完改回来。
#: **`/api/*` 与 `/static/*` 不受影响** —— 桌面 GUI 全靠它们。
# 修改值以启用 webUI，仅做调试使用。
WEBUI_PAGES_ENABLED = False


def _webui_pages_off():
    """内部界面关闭时，那六个页面统一回一句人话（404，不是 403）。

    用 404 而不是 403：403 等于承认"这里有东西，只是你没权限"，
      而对外它就是不存在的界面（「无论怎么样都叫不出来」）。
    """
    return ("Crannia 只提供桌面应用；这里没有可访问的页面。", 404,
            {"Content-Type": "text/plain; charset=utf-8"})
_FRAMEWORK_RUNNING = False
_FRAMEWORK_INIT_ERROR: str | None = None
#: 供应商没装上（装得上框架、但用不了模型）—— 与 `_FRAMEWORK_INIT_ERROR`
#: 故意分开：后者是"应用打不开"（小窗直接停在报错屏），而这条是"能打开、
#: 但还没法对话"。判据：机器上配了 SOCKS 代理、而随包
#: runtime 缺 `socksio`，构造 provider 抛，旧写法让整个应用起不来，
#: 报的还是一句 `Provider 'deepseek' not registered`（看不出真因）。
_FRAMEWORK_PROVIDER_ERROR: str | None = None
_FRAMEWORK_START_TIME: float = 0.0
_FRAMEWORK_STARTING: bool = False  # Guard against concurrent init  # time.time() when framework started
_AGENT_BUSY: bool = False          # True while agent is processing a request
_AGENT_TASK: str = ""              # Short description of current task
_AGENT_TASK_UPDATED: float = 0.0   # time.time() when task label was last set

# Framework singletons — set by /api/start
_memory_manager = None
_session_pool = None
_scheduler = None
_provider = None
_config: dict = {}

# Log ring buffer (for Web UI terminal)
_LOG_BUFFER: list[str] = []  # list of "[TIME] message" strings
_MAX_LOG_LINES = 500
#: 单行上限。一条超长日志（整段 JSON / 一整屏堆栈 / 一张 base64）会同时
#:   拖垮两处：页面渲染（一行几 MB 的文本节点）与导出（`.txt` 跟着变大）。
#:   所以在写进缓冲那一刻就截断，截断处带标记 —— 页面与导出都只看到这一份。
_MAX_LOG_LINE = 2000
#: 总字符预算（与行数上限互补）：2000 行 × 2000 字符 = 4MB，仍然太大。
#:   超了从最旧的行开始丢，并在回执首行说明丢了多少行。
_MAX_LOG_CHARS = 400_000
#: 启动噪音的终点：Electron 那条安全警告的最后一句。
#:   见到它，它之前的行一律不对外展示（那是启动期噪声，不是运行日志）。
#:   打包版里不会再出现这句，认不出就什么都不截（不做任何猜测）。
_STARTUP_NOISE_END = "once the app is packaged."
# The slice-assignment trim below is not atomic.  With waitress
# threads=12 (raised for SSE) concurrent prints could tear the ring, so
# both the writer and the /api/logs reader go through this lock.
_LOG_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """Append a timestamped message to the ring buffer."""
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    text = msg if len(msg) <= _MAX_LOG_LINE else (
        msg[:_MAX_LOG_LINE] + "…（本行已截断，原长 %d 字符）" % len(msg))
    with _LOG_LOCK:
        _LOG_BUFFER.append(f"[{ts}] {text}")
        if len(_LOG_BUFFER) > _MAX_LOG_LINES:
            _LOG_BUFFER[:] = _LOG_BUFFER[-_MAX_LOG_LINES:]
    # Use real stdout to avoid infinite recursion through _LogCapture
    # NOTE: _real_stdout may be None in PyInstaller --noconsole mode
    # Written outside the lock: it is a different resource and blocking
    # IO inside the lock would serialise every logging thread.
    if _real_stdout is not None:
        _real_stdout.write(msg + "\n")
        _real_stdout.flush()


def _error_brief(exc: Exception, what: str, name: str = "") -> str:
    """把配置类的失败翻成一句人话 —— 只留文件名，不留目录。

    判据：界面那一层能说的只有"哪件事没做成 + 哪个文件"。路径、errno 原文、
    堆栈都属于日志（`_log` 写的环形缓冲，界面的日志页能翻），不属于弹窗。

    返回值从不拼接 `str(exc)` —— 那正是把 `D:\\...\\config.yaml`
    原样带到界面上、再被用户截图带走的那条路。
    """
    base = (name or str(getattr(exc, "filename", "") or "")).replace("\\", "/")
    base = base.rsplit("/", 1)[-1].strip()
    if isinstance(exc, FileNotFoundError):
        return ("%s：找不到 %s" % (what, base)) if base else ("%s：文件不存在" % what)
    if isinstance(exc, PermissionError):
        return (("%s：写不了 %s（被占用或只读）" % (what, base)) if base
                else ("%s：没有权限" % what))
    if isinstance(exc, json.JSONDecodeError) or type(exc).__name__ == "YAMLError":
        return (("%s：%s 的格式坏了" % (what, base)) if base
                else ("%s：配置文件格式坏了" % what))
    return "%s：失败（详情见日志）" % what


# Redirect Python's stdout to capture framework logs
import sys as _sys
_real_stdout = _sys.__stdout__
_real_stderr = _sys.__stderr__
#: 日志不许弄挂业务：Windows 控制台是 GBK，任何一句带 emoji 的 `print`
#:   都会抛 `UnicodeEncodeError` —— 而它是从业务代码里抛出来的
#:   （微信消息处理里那句 `[wechat] 📩 …` 一炸，
#:   `_delayed_flush` 把整条消息当"Flush error"吞掉，发消息没反应）。
#:   所以把真实 stdout/stderr 设成 `errors="replace"`：编不出来的字符变成 `?`，
#:   日志难看一点，业务照跑。（PyInstaller 的 `--noconsole` 下它们是 None。）
try:
    if _real_stdout is not None:
        _real_stdout.reconfigure(errors="replace")   # type: ignore[attr-defined]
    if _real_stderr is not None:
        _real_stderr.reconfigure(errors="replace")   # type: ignore[attr-defined]
except (AttributeError, ValueError, OSError):
    pass

class _LogCapture:
    def write(self, s: str) -> int:
        if s.strip():
            for line in s.strip().splitlines():
                _log(line)
        return len(s)
    def flush(self) -> None:
        pass
    def isatty(self) -> bool:
        return False

_sys.stdout = _LogCapture()  # type: ignore
_sys.stderr = _LogCapture()  # type: ignore

# Framework Init

def _init_framework() -> None:
    """Initialise the agent framework (bus, memory, pipeline).

    Idempotent — safe to call multiple times.  If the framework is
    already running, this is a no-op (prevents duplicate tool
    registration from `init_bus()`).
    """
    global _FRAMEWORK_RUNNING, _FRAMEWORK_INIT_ERROR, _FRAMEWORK_START_TIME
    global _memory_manager, _session_pool, _scheduler, _provider, _config

    if _FRAMEWORK_RUNNING:
        return

    try:
        from bus.core import init_bus, load_config
        from bus.registry import get_provider, get_channel
        from core.memory.manager import MemoryManager
        from core.pipeline.scheduler import PipelineScheduler
        from core.prompt_assembler import set_project_root
        from core.session_pool import SessionPool

        set_project_root(_PROJECT_ROOT)
        _config = load_config(_paths.config_path())
        provider_name = _config.get("model", {}).get("provider", "deepseek")

        # Memory
        if _config.get("memory", {}).get("enabled", True):
            _memory_manager = MemoryManager(_config, provider=None)
            # 把实例登记给注册表 —— `history_search` 靠它拿 `short_term`。
            # 两个入口都要接（这里与 `main.py`），
            # 少一个 = 那条通道上"查历史不可用"。
            from bus.registry import register_memory_manager
            register_memory_manager(_memory_manager)
            print(f"[web] MemoryManager initialised")

        _session_pool = SessionPool(_memory_manager) if _memory_manager else None

        # Bus
        init_bus(_config, session_pool=_session_pool)
        # 供应商装不上 ≠ 应用打不开：能启动就先让用户进得来，
        #    去「端配置」把 key / 地址补上。旧写法在这里裸抛 KeyError，
        #    小窗停在「Agent 初始化失败」——用户没有别的入口可用。
        global _FRAMEWORK_PROVIDER_ERROR
        try:
            _provider = get_provider(provider_name)
        except Exception as _prov_exc:                  # noqa: BLE001
            _provider = None
            _FRAMEWORK_PROVIDER_ERROR = (
                "模型供应商 '%s' 没装上（%s: %s）—— 去「端配置」补一条可用的模型；"
                "其余功能照常。" % (provider_name,
                                     type(_prov_exc).__name__, _prov_exc))
            print("[web] WARNING: " + _FRAMEWORK_PROVIDER_ERROR)
        if _memory_manager:
            _memory_manager._provider = _provider
            # Threshold self-check — same as main.py.
            _memory_manager.report_thresholds()

        # Pipeline
        _scheduler = PipelineScheduler(
            session_pool=_session_pool, provider=_provider, config=_config,
        )
        # Wire busy-state callbacks for the Web UI status bar
        _scheduler.on_task_start = _on_task_start
        _scheduler.on_task_end = _on_task_end
        # Run async init in a thread-safe way
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        loop.run_until_complete(_scheduler.initialize())

        # 周期任务的调度器。失败不许拖垮启动 ——
        #   它是一项附加能力，起不来只该是"定时任务不跑"，不是"框架起不来"。
        #   `configure()` 是幂等的：界面那个「重启框架」会在同一个进程里
        #     再走一遍这里 —— 重用时只换 scheduler，不新起第二条线程。
        try:
            from core.cron import runner as _cron_runner
            _cr = _cron_runner.configure(_scheduler)
            print("[cron] 周期任务调度器已启动（%d 条任务）" % _cr.job_count())
        except Exception as _exc:  # noqa: BLE001
            print("[cron] 起调度器失败（不影响其他功能）：%s: %s"
                  % (type(_exc).__name__, _exc))

        # 外部事件入口：插件投递消息时要用到调度器。
        try:
            from core import inbound as _inbound
            _inbound.configure(_scheduler)
        except Exception as _exc:  # noqa: BLE001
            print("[inbound] 事件入口没接上（不影响启动）：%s: %s"
                  % (type(_exc).__name__, _exc))

        # 后台任务的完成通知：空闲起一轮 / 忙则排队。
        # 失败同样不许拖垮启动 —— 顶多是"后台任务跑完没人告诉模型"。
        try:
            from core import tasks_notify as _tasks_notify
            _tasks_notify.install(_scheduler)
            print("[jobs] 后台任务完成通知已接通")
        except Exception as _exc:  # noqa: BLE001
            print("[jobs] 后台通知没接上（不影响启动）：%s: %s"
                  % (type(_exc).__name__, _exc))

        # `task.started/finished/failed/cancelled` ——
        #   UI 那两格计数与两个浮窗的信号。与上面的通知链分开：
        #   那一条管"跑完要不要起一轮"，这一条只管广播。
        try:
            from core import task_events as _task_events
            _task_events.install()
        except Exception as _exc:  # noqa: BLE001
            print("[jobs] 任务事件没接上（不影响启动）：%s: %s"
                  % (type(_exc).__name__, _exc))

        import time
        # Reset token counter for new session
        try:
            from providers.base import reset_token_counter
            reset_token_counter()
        except Exception:
            pass

        _FRAMEWORK_RUNNING = True
        _FRAMEWORK_INIT_ERROR = None
        # 不清 `_FRAMEWORK_PROVIDER_ERROR`：它正是这一轮写下的
        _FRAMEWORK_START_TIME = time.time()
        # 记一次「上次成功」的配置快照 —— `#00` / `#03` 的共同前提。
        #   落在这里而不是"进程起来了"那一刻：只有初始化做完了才算真的跑起来，
        #   否则会把一堆坏配置存成"好的"，下次拿它去恢复等于把故障固化。
        #   `mark_good()` 自己不许抛；这里再包一层 —— 快照失败不该让启动失败。
        try:
            from core import last_good as _lg
            _lg.mark_good()
        except Exception as exc:                            # noqa: BLE001
            print("[last_good] 记录失败（不影响启动）：%s: %s"
                  % (type(exc).__name__, exc))
        print(f"[web] Framework initialised (provider={provider_name}, "
              f"model={_provider._model if _provider else '?'})")

    except Exception as e:
        _FRAMEWORK_RUNNING = False
        err_name = type(e).__name__
        err_msg = str(e)
        # Make common errors user-friendly
        if "401" in err_msg or "Unauthorized" in err_msg or "auth" in err_msg.lower():
            err_msg = "API Key 无效或未配置。请在「模型 API」页面填入正确的 Key。"
        elif "timeout" in err_msg.lower():
            err_msg = "API 连接超时。请检查网络或更换模型。"
        elif "connect" in err_msg.lower():
            err_msg = "无法连接到 API 服务器。请检查 base_url 配置。"
        _FRAMEWORK_INIT_ERROR = err_msg
        _log(f"[web] Framework init FAILED: {err_name}: {err_msg}")


def _on_task_start(label: str) -> None:
    """Called by PipelineScheduler when a task begins."""
    global _AGENT_BUSY, _AGENT_TASK, _AGENT_TASK_UPDATED
    import time
    _AGENT_BUSY = True
    _AGENT_TASK = label
    _AGENT_TASK_UPDATED = time.time()


def _on_task_end() -> None:
    """Called by PipelineScheduler when a task completes."""
    global _AGENT_BUSY, _AGENT_TASK
    _AGENT_BUSY = False
    _AGENT_TASK = ""


def _check_token() -> bool:
    """Return True if the request carries a valid token."""
    token = request.args.get("token", "") or request.headers.get("X-Crannia-Token", "")
    return token == _LAUNCH_TOKEN


# Routes: Pages

@app.route("/ping")
def ping():
    """Plain-text endpoint to verify HTTP responses work.

    默认不吐令牌（第三方审计第 2 条：`/ping` 把本地 API 的
    Bearer Token 明文给任何本机进程）。桌面壳改成自己生成令牌、用环境变量
    传给后端（`CRANNIA_LAUNCH_TOKEN`），再经 `additionalArguments` 注入渲染进程
    （`preload.cjs` → `window.crannia.token`）。

    只有显式设了 `CRANNIA_ALLOW_PING_TOKEN=1` 才回旧格式 —— 那是给
    浏览器里单独调试用的（`npm run dev:web`），默认谁都不给。
    """
    if os.environ.get("CRANNIA_ALLOW_PING_TOKEN") == "1":
        return f"pong — token={_LAUNCH_TOKEN}", 200, {"Content-Type": "text/plain"}
    return "pong", 200, {"Content-Type": "text/plain"}


@app.route("/")
def dashboard():
    if not WEBUI_PAGES_ENABLED:
        return _webui_pages_off()
    if not _check_token():
        return _forbidden()
    return render_template("dashboard.html", active="platform")


# /setup route removed — template never existed, caused 500 errors.
# First-run guidance is handled by the status bar + settings modal.


@app.route("/chat")
def chat():
    if not WEBUI_PAGES_ENABLED:
        return _webui_pages_off()
    if not _check_token():
        return _forbidden()
    return render_template("chat.html", active="chat")


@app.route("/models")
def models():
    if not WEBUI_PAGES_ENABLED:
        return _webui_pages_off()
    if not _check_token():
        return _forbidden()
    return render_template("models.html", active="models")


@app.route("/logs")
def logs_page():
    if not WEBUI_PAGES_ENABLED:
        return _webui_pages_off()
    if not _check_token():
        return _forbidden()
    return render_template("logs.html", active="logs")


@app.route("/security")
def security():
    if not WEBUI_PAGES_ENABLED:
        return _webui_pages_off()
    if not _check_token():
        return _forbidden()
    return render_template("security.html", active="security")


@app.route("/skills")
def skills_page():
    if not WEBUI_PAGES_ENABLED:
        return _webui_pages_off()
    if not _check_token():
        return _forbidden()
    return render_template("skills.html", active="skills")


@app.route("/api/download")
def api_download():
    """下载工作区里的文件。

    `deliver_file` 在 Web 会话下的出口。
    没有它的话，Web 会话里唯一的交付出口是邮件，而邮件还挂着 SMTP。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403

    raw = (request.args.get("path") or "").strip()
    if not raw:
        return jsonify({"error": "missing path"}), 400

    try:
        from flask import send_file
        from pathlib import Path as _P

        ws_root = _P("workspace").resolve()
        p = _P(raw)
        if not p.is_absolute():
            # 剥掉可能带的 "workspace/" 前缀，再挂到真实工作区下
            rel = str(p).replace("\\", "/")
            while rel.startswith("./"):
                rel = rel[2:]
            if rel.startswith("workspace/"):
                rel = rel[len("workspace/"):]
            p = ws_root / rel
        p = p.resolve()

        # 只允许工作区内的文件 —— 这是唯一的边界检查，不能省
        if p != ws_root and ws_root not in p.parents:
            return jsonify({"error": "forbidden"}), 403
        if not p.is_file():
            return jsonify({"error": "not found"}), 404

        return send_file(str(p), as_attachment=True, download_name=p.name)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/artifact")
def api_artifact():
    """取回一个产物。

    只认 id，不认路径：`?id=a1b2c3d4e5f6a7b8.png`。`resolve()` 只会在
    `data/attachments/` 下找那 16 位十六进制 + 白名单扩展名 —— 调用方给不了
    任何路径（`/api/download` 那种"路径 + 边界检查"在这里不需要存在）。

    400 与 404 分得开：形状不合法是调用方写错了，文件不存在是
    这条记录已经被清了（内容寻址目录不做 GC，但目前也没人保证它永远在）。

    `conditional=True` 让 Flask 自己处理 `Range` / `If-None-Match` ——
    `<video>` 拖动进度条靠的就是它。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403

    rid = (request.args.get("id") or "").strip()
    from core import artifacts as _art

    if not _art.valid_id(rid):
        return jsonify({"error": "invalid id"}), 400
    p = _art.resolve(rid)
    if p is None:
        return jsonify({"error": "not found"}), 404

    try:
        from flask import send_file
        # 必须 resolve 成绝对路径：`send_file` 对相对路径的基准是
        #    `app.root_path`（= `web/`），而我们的产物在进程 cwd 下的
        #    `data/attachments/` —— 不 resolve 时报错 `web\data\attachments\…`。
        resp = send_file(str(p.resolve()), mimetype=_art.mime_for_id(rid),
                         conditional=True, max_age=3600)
        # `nosniff`：浏览器不许拿扩展名去猜类型
        #   （我们能做对的那一半是"声明清楚"，不是"猜它会怎么读"）。
        resp.headers["X-Content-Type-Options"] = "nosniff"
        # `inline`：这些是聊天区里要直接显示/播放的东西，不是下载项。
        resp.headers["Content-Disposition"] = "inline"
        return resp
    except Exception as e:                       # noqa: BLE001
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/logs")
def api_logs():
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    lines = request.args.get("lines", "100")
    try:
        n = min(int(lines), _MAX_LOG_LINES)
    except ValueError:
        n = 100
    return jsonify({"logs": log_snapshot(n)})


def log_snapshot(n: int = 100) -> list:
    """给界面的那一份日志 —— 启动噪音截断 + 行数上限 + 总字符预算。

    三条都在读这一侧做（写那一侧只截单行）：它们是"给谁看"的判据，
      不是"记不记"的判据 —— 缓冲里留着完整时间线，将来要落盘也不用改回去。

    ① 启动噪音：找到 Electron 那条安全警告的结束句，它之前的全部丢掉
       （认不出，一行不截，不做任何猜测）；
    ② 行数：`n`（已按 `_MAX_LOG_LINES` 收过）；
    ③ 总字符：从最新往回累，超预算就停 —— 并在首行说明丢了多少行
       （"没看见"与"被截掉"必须分得开）。
    """
    with _LOG_LOCK:
        buf = list(_LOG_BUFFER)
    cut = -1
    for i, line in enumerate(buf):
        if _STARTUP_NOISE_END in line:
            cut = i
    if cut >= 0:
        buf = buf[cut + 1:]
    tail = buf[-max(1, int(n)):] if n else buf
    out, total = [], 0
    for line in reversed(tail):
        if total + len(line) + 1 > _MAX_LOG_CHARS:
            break
        out.append(line)
        total += len(line) + 1
    out.reverse()
    dropped = len(tail) - len(out)
    if dropped > 0:
        out.insert(0, "…（更早的 %d 行因总篇幅上限被省略）" % dropped)
    return out


# Routes: SSE progress stream (v1.2.0)

#: 心跳间隔。必须 << waitress 的 channel_timeout（默认 120s，
#: `Adjustments().channel_timeout == 120`）—— 空闲连接会被服务端单向掐掉。
_SSE_HEARTBEAT_S = 15

#: 同时最多几条事件流。超了明确拒绝，而不是把整个 UI 拖死 ——
#: waitress 是同步 WSGI，一条 SSE 终身占用一个线程。
_SSE_MAX_STREAMS = 8
_sse_active = 0
_sse_lock = threading.Lock()


@app.route("/api/events")
def api_events():
    """agent 活动的实时事件流（Server-Sent Events）。

    独立端点，刻意不复用 /api/health —— 后者有 chat 和 dashboard 两个
    页面在消费，改它的语义会让 dashboard 静默退化。

    `EventSource` 无法发自定义 header，所以鉴权只能走 `?token=`
    （`_check_token` 本来就同时认 query 与 header，无需改动）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403

    from core.events import get_event_bus

    bus = get_event_bus()
    session_key = request.args.get("session", "")   # 空 = 订阅全部会话

    # 浏览器重连时自动回传 Last-Event-ID 头；curl 用手动参数兜底
    raw = request.headers.get("Last-Event-ID", "") or request.args.get("last_id", "")
    try:
        last_seq = int(raw)
    except (TypeError, ValueError):
        last_seq = 0

    def _gen():
        global _sse_active
        with _sse_lock:
            if _sse_active >= _SSE_MAX_STREAMS:
                yield 'event: error\ndata: {"error":"too many streams"}\n\n'
                return
            _sse_active += 1

        # 顺序很重要：先订阅，再补洞。反过来的话，补洞与实时推送
        # 之间会有一个丢事件的窗口。
        sub = bus.subscribe(session_key)
        try:
            yield "retry: 3000\n\n"          # 断线后浏览器 3s 重连
            if last_seq > 0:
                for ev in bus.replay_after(last_seq, session_key=session_key):
                    yield ev.to_sse()
            # 立即顶出一帧，否则浏览器 onopen 迟迟不触发
            yield ": connected\n\n"

            while True:
                try:
                    ev = sub.q.get(timeout=_SSE_HEARTBEAT_S)
                except _QueueEmpty:
                    # 心跳。注释帧故意不带 id:，否则会把 Last-Event-ID 冲掉。
                    yield ": hb\n\n"
                    continue
                yield ev.to_sse()
        finally:
            bus.unsubscribe(sub)
            with _sse_lock:
                _sse_active -= 1

    return Response(
        _gen(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",       # 无 nginx，但加着防将来挂反代
            # 不要加 "Connection: keep-alive" —— 那是 hop-by-hop 头，
            # WSGI 规范（PEP 3333）禁止应用设置，waitress 会直接抛
            # AssertionError 让整个响应失败。连接生命周期由服务器管。
        },
    )


# Routes: API

@app.route("/api/start", methods=["POST"])
def api_start():
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    global _FRAMEWORK_STARTING
    if _FRAMEWORK_RUNNING:
        return jsonify({"ok": True})
    if _FRAMEWORK_STARTING:
        return jsonify({"ok": True, "status": "starting"})
    _FRAMEWORK_STARTING = True
    try:
        _init_framework()
        if _FRAMEWORK_RUNNING:
            _auto_start_channels_from_settings()
            _maybe_startup_update_check()
    finally:
        _FRAMEWORK_STARTING = False
    return jsonify({
        "ok": _FRAMEWORK_RUNNING,
        "error": _FRAMEWORK_INIT_ERROR,
    })


def _approval_pending_label() -> str:
    """当前这条 web 会话挂着的待审批 —— 公开的 `kind`（没有 = 空串）。

    「挂着一条等你答的审批」也是「有活在跑」 ——
    壳退出前那一问只看 `agent_busy`，而它在"挂起即停车"那一刻就翻回 `False`
    所以有待审批时会一声不响地退（托盘退出没有确认框）。
    所以两个值分开报（同一组标签不许打两个量纲），
    由壳那边合起来判 —— 这里只负责报，不做决定。

    读不到报 `"unknown"`，不许报空串：空串的意思是"确实没有"，
    而那是这里唯一不能凭空下的结论（读不到 ≠ 没有）。
    """
    # 框架没跑，空串，不是 `"unknown"`：那种状态下不可能有待审批
    #    （它是进程内的会话态）。把它报成"读不到"会让壳在每次退出时都弹确认框。
    if not _FRAMEWORK_RUNNING or _scheduler is None:
        return ""
    try:
        pend = _scheduler.pending_approval(session_key_for(_web_session()))
    except Exception:                                              # noqa: BLE001
        return "unknown"
    if not pend:
        return ""
    return str(pend.get("kind") or "?")


def _model_api_ready() -> bool:
    """有没有一条真能发出去的模型配置？

    判据两条，与真正发请求那条路同源：
      ① 账本里有默认条目：本地模型（ollama / lmstudio）不需要 key；
         其余的看 `api_for()` 有没有明文（唯一出口）。
      ② 没有条目，回退 `config.yaml` 的 `model:`：provider 是本地那两家
         可以直接用；否则看 `model.api_key` 解出来是不是非空。

    不能看 `_read_models()` 的 `configured` —— 它把"`.env` 里有这把 key"
      也算配置，而 `build_provider` 的 api_key 只从 `config.yaml` 的 `model:`
      取（`model_cfg.get("api_key", "")`）。两者会分叉：`.env` 有 key、而
      `model:` 段被清掉时，目录说"已配置"、实例其实没有 key。
    """
    try:
        from core import model_configs as _mc
        cid = _mc.default_id()
        if cid:
            entry = _mc.get(cid) or {}
            provider_dir = _mc.CARD_PROVIDER.get(str(entry.get("card") or ""), "")
            if provider_dir in ("ollama", "lmstudio"):
                return True
            return bool(_mc.api_for(cid).strip())
    except Exception:                                          # noqa: BLE001
        pass
    try:
        cfg = _read_config()
        model = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
        provider = str(model.get("provider") or "")
        if provider in ("ollama", "lmstudio"):
            return True
        return bool(str(model.get("api_key") or "").strip())
    except Exception:                                          # noqa: BLE001
        return False


@app.route("/api/incidents")
def api_incidents():
    """`#02` 组件异常 —— 本进程记下的运行时事故。

    只返回本轮的（内存那份）。历史在 `data/incidents/*.jsonl`，
    给人和 agent 查 —— 不进这里，否则昨天的事故天天弹横幅。

    四类来源见 `core/incidents.py::KINDS`。`compatibility` / `discover`
    刻意不在里面：那是"配不上"，技能页已经写了原因、体检器也管得着。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    try:
        from core import incidents
        items = incidents.all()
    except Exception as exc:                        # noqa: BLE001
        return jsonify({"items": [], "count": 0,
                        "error": "%s: %s" % (type(exc).__name__, exc)})
    return jsonify({"items": items, "count": len(items)})


@app.route("/api/status")
def api_status():
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    out = {
        "framework_running": _FRAMEWORK_RUNNING,
        "init_error": _FRAMEWORK_INIT_ERROR,
        #: 与 `init_error` 分开：这条表示"应用能开，但还模型用不了"。
        "provider_error": _FRAMEWORK_PROVIDER_ERROR,
        "provider": _active_model_info()["provider"],
        "model": _active_model_info()["model"],
        #: 正在用的配置条目叫什么（没有默认条目就是空串）。
        "model_config_name": _active_model_info()["config_name"],
        #: 有没有一条真能发出去的模型配置 —— 聊天页那条红横幅与
        #:   发送闸门读的就是这一格（判据见 `_model_api_ready`）。
        "model_api_ready": _model_api_ready(),
        "project_root": str(_PROJECT_ROOT),
        # GUI 侧栏底部那格版本号
        # 取的是代码线 `core/version.py`，不是文档线 `v0.11.x`。
        # 两套版本号并存是已知的、未收敛的 ——
        #   这里只报代码线，不要顺手把文档线也塞进来。
        "version": __version__,
        # 「端配置」那条路要的后端自己的地址。
        #   端配置页是 WebUI 那个 Flask 页面（`/` → `dashboard.html`），
        #   不是这个 SPA 里的一屏，GUI 只能开系统浏览器过去。
        #   不能让前端拿 `baseUrl` 去拼 —— dev 下 `baseUrl === ""`
        #   （渲染进程挂在 vite 5173 上），拼出来的是 vite 自己的页面。
        #   所以后端自报家门：`request.host_url`。dev 走 vite 代理且
        #   `changeOrigin:true`，Host 被改写成目标，这一格在两种壳下都对。
        "web_url": request.host_url,
        # 壳退出前要问的那一格
        # 判据：壳在退出前先问一句「会打断什么」 ——
        #   而"要不要问"取决于此刻有没有活在跑，所以把它摆在这里。
        # 读不到就按"有活在跑"处理（`inspectQuit` 那侧的 fail-safe）：
        #   这条只负责报，不做任何决定。
        "agent_busy": _AGENT_BUSY,
        "agent_task": _AGENT_TASK,
        # 挂着一条等你答的审批 = 有活在跑 ——
        #   `agent_busy` 在"挂起即停车"那一刻就翻回 `False`，而退出
        #   会连那条审批一起丢掉，它必须能被子进程之外的人看见。
        #   与 `agent_busy` 分开两格：合成一个布尔的话，
        #   壳那边的确认框就说不出"在跑什么"（那是它 `reason` 的全部内容）。
        "approval_pending": _approval_pending_label(),
    }
    #: 轨迹开关的当前值 ——
    #:   右缘那一格按钮只在打开时才出现。
    #:   它是只读的（`trace.enabled()` 只读一次文件）；每 5 秒的轮询
    #:   没有副作用（同下面 `frozen_level` 那条判据）。
    try:
        from core import trace as _trace
        out["trace_enabled"] = _trace.enabled()
    except Exception:                       # noqa: BLE001 — 读不到就"不显示"
        out["trace_enabled"] = False
    #: 下好了、等着下次启动装上的那一份 —— 聊天页那句黄字读它。
    #:   `None` = 没有待安装的（正在下载时也是 None，别读成"没有新版本"）。
    try:
        from core.updater import pending_install as _pending_install
        _pend = _pending_install()
        out["update_ready"] = ({"version": _pend["version"]} if _pend else None)
    except Exception:                       # noqa: BLE001
        out["update_ready"] = None
    #: 模型连接重试的当前状态（红色横幅的兜底读数）。
    #:   `None` = 没在重试。事件（`llm.retry` / `llm.retry_ok`）是即时通道；
    #:   这一格管的是"页面重连之后还看得到"（状态是真相，事件只是叫你读）。
    try:
        from core import llm_retry as _lret
        out["llm_retry"] = _lret.get(session_key_for(_web_session()))
    except Exception:                       # noqa: BLE001
        out["llm_retry"] = None
    # 投入档常驻可见
    #
    # 两个值分开报，因为它们真的会不一样（同一组标签不许打两个量纲）：
    #   · `effort_*`         = 设置里此刻写着的（`configured_level`，现读、不冻结）
    #   · `effort_session_*` = 本会话已经冻结的那个 = 这一段对话真正在跑的档
    # 改完设置而这一段还没生效时两者不同，界面必须两句话都说出来，
    # 否则它就是在替用户猜（「标签在说谎」的形状）。
    #
    # 这里绝不能调 `current_level()` —— 它第一次被问就把档钉死，
    #   而本接口是每 5 秒一次的轮询，一个读接口有了副作用：用户还没发过一句话，
    #   档就已经冻在"打开页面那一刻"的设置上。所以走 `frozen_level()`（纯读）。
    try:
        from core import tiers
        try:
            out["effort_level"] = tiers.configured_level()
            out["effort_name"] = tiers.TIERS[out["effort_level"]].name
        except Exception as exc:          # 「读到了但做不到」，出声，不静默消失
            out["effort_level"] = None
            out["effort_name"] = None
            out["effort_error"] = str(exc)
        _frozen_lvl = tiers.frozen_level(session_key_for(_web_session()))
        out["effort_session_level"] = _frozen_lvl        # 键 —— 界面 chip 要的是它
        out["effort_session_name"] = (tiers.TIERS[_frozen_lvl].name
                                      if _frozen_lvl is not None else None)
    except Exception:
        pass
    # 模式轴：与投入档同一套规矩、同一个理由
    #   · `mode_name`         = 设置里此刻写着的（`configured_mode`，现读、不冻结）
    #   · `mode_session_name` = 本会话已经冻结的那个（`frozen_mode`，纯读）
    # 同上：绝不能调 `current_mode()` —— 每 5 秒一次的读接口不许有副作用。
    try:
        from core import modes
        try:
            _cfg_mode = modes.configured_mode()
            #: 配置里那个也报一个"键"（理由同下面 `mode_session`）。
            #: 输入框那枚 chip 在"这一段还没冻结"时要显示它将要用的那一个，
            #: 而它要的是 key —— `mode_name` 只是中文名，前端拿它选不了项。
            #: 否则第一轮之前 chip 会一律显示「手动」，而设置里写的是「自动」
            #: （标签在说谎）。
            out["mode_configured"] = _cfg_mode
            out["mode_name"] = modes.MODE_NAMES.get(_cfg_mode)
        except Exception as exc:
            out["mode_configured"] = None
            out["mode_name"] = None
            out["mode_error"] = str(exc)
        _frozen_mode = modes.frozen_mode(session_key_for(_web_session()))
        #: 多报一个"键"。只给 `mode_session_name`（中文名）是不够的：
        #: 输入框那枚 chip 要的是 key（`manual`/`auto`/`plan`），它只好去读
        #: `settings.default_mode`，于是显示的是"默认值"而不是"这一段在跑的"
        #: （以为在自动、其实一直手动）。
        out["mode_session"] = _frozen_mode
        out["mode_session_name"] = (modes.MODE_NAMES.get(_frozen_mode)
                                    if _frozen_mode is not None else None)
    except Exception:
        pass
    return jsonify(out)


@app.route("/api/quarantine")
def api_quarantine():
    """被屏蔽的插件 —— 现读。

    它是"状态"那一侧，事件只是"叫你来读"那一下。
      前端两条路都要走：开机拉一次（补发掉线/关机期间发生的屏蔽）+ 收到
      `plugin.blocked` 之后再拉一次（对账）—— 与审批浮窗那两条路同族
      （`approval.request` 叫你读，`GET /api/approval` 给正文）。

    它只读账本，不自己去体检（体检是 `skill_doctor` 的活）——
      两个来源会各漂各的。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    try:
        from bus.quarantine import all_entries
        entries = all_entries()
        return jsonify({"blocked": [entries[k] for k in sorted(entries)]})
    except Exception as e:                                       # noqa: BLE001
        # 与 `/api/context` 同一条判据：取不到就出声，不回一张空表 ——
        #   "没有屏蔽"与"读不到"长得一样，而前者会被读成一个结论。
        return jsonify({"blocked": [],
                        "error": "%s: %s" % (type(e).__name__, e)}), 503


@app.route("/api/quarantine/release", methods=["POST"])
def api_quarantine_release():
    """解封 = 放出来 + 当场重体检（可逆的那一半）。屏蔽 ≠ 卸载 ——
    用户的文件一个字节没动过。

    只 `release(owner)` 是不够的：判据与各条路
      （`passed` / `still_bad` / `path_missing` / `load_failed`）都在
      `bus/quarantine.py::settle_after_release` —— 这一层只管接线。

    账本条目必须在 `release()` 之前取：它会把条目 `pop` 掉
      （`bus/quarantine.py`），而重体检要用里面那个 `path`。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    owner = str(data.get("owner") or "").strip()
    if not owner:
        return jsonify({"ok": False, "error": tr("缺少 owner")}), 400
    try:
        from bus.quarantine import all_entries, release, settle_after_release
        entry = dict(all_entries().get(owner) or {})
        released = release(owner)
        out = {"ok": True, "released": released, "recheck": "skipped",
               "message": "已解封" if released else "它本来就不在屏蔽名单里"}
        # 只有 `skill:` 那一族有"重体检"这回事（`mcp:` 的屏蔽由 MCP 自己那半管，
        #   它的放行判据不在这里）。
        if released and owner.startswith("skill:"):
            out.update(settle_after_release(owner, entry.get("path", ""),
                                            _release_guard()))
        return jsonify(out)
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500


def _release_guard():
    """解封后重新装载时用的安全核 —— 与 `/skill reload` 同一套取法
    （`core/pipeline/stages/pre_process.py::_security_guard_or_none`）。

    拿不到就回 `None`，不因为取不到就不装（那条判据在那个函数的 docstring 里）。
    """
    try:
        from core.pipeline.stages.pre_process import _security_guard_or_none
        return _security_guard_or_none()
    except Exception:                                            # noqa: BLE001
        return None


@app.route("/api/skills/uninstall", methods=["POST"])
def api_skill_uninstall():
    """卸载一个技能 —— 只许目录内的（「来源」是真实信息）。

    判据：

      · 目录内（`source == "repo"` 且不是框架自带）-> 挪到系统回收站（可恢复）
      · 外部来源 -> 不卸，回一句
        「这是外部应用的组件，暂不提供卸载。路径 __」

    [!] 路径只从扫描结果取（`_scan_skills()`），请求里那个 `name` 只当键——
      用户传 `../core` 之类在这里查不到 -> 404 -> 构造不出路径。
      这是本仓库对 `skills/` 写操作唯一安全的形状（`skills/` 对通用写工具是只读的，
      见 `core/security.py`；真正能动的只有框架自己这条路，同 `skill_factory` 的先例）。
    [!] 卸了必须把工具摘掉（`bus.registry.unregister_owner`）—— `register_tool`
      是就地替换、从不删除，只挪目录的话界面会说「已卸载」而工具还能被调
      （标签在说谎，而且没有任何东西会红）。
    [!] 回收站降级要出声：`RecycleOutcome.degraded` 为真 = 没真挪走
      （或退回永久删）-> 回执必须把它说清楚，不能只说"已卸载"。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403

    #: 名字走 body（不是 URL 段）—— 与兄弟端点 `/api/quarantine/release` 同形。
    #:   这不是风格问题：`tests/test_stars_ui.py` 有一条两边对账的读者，
    #:   要求前端源码里出现的那个字面路径本身就是一条路由。
    #:   `"/api/skills/" + 名字 + "/uninstall"` 那种拼法抽出来的是 `/api/skills/`，
    #:   而那不是路由。
    name = str((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": tr("缺少 name")}), 400

    target = next((s for s in _scan_skills() if s.get("name") == name), None)
    if target is None:
        # 查不到就是查不到 —— 不拿请求里的名字去拼路径。
        return jsonify({"ok": False, "error": tr("没有这个技能：%r") % name}), 404
    if target.get("source") != "repo":
        return jsonify({"ok": False, "external": True, "path": target.get("path", ""),
                        "error": tr("这是外部应用的组件，暂不提供卸载。路径 %s")
                                 % (target.get("path") or "（未提供）")}), 400
    if target.get("builtin"):
        return jsonify({"ok": False, "error": tr("这是框架自带的技能，暂不提供卸载")}), 400

    skill_dir = Path(target["path"])
    try:
        from core.recycle import explain, send_to_recycle_bin
        outcome = send_to_recycle_bin([skill_dir])
        moved = bool(outcome.moved)
        message = explain(outcome)
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500

    removed_tools = 0
    if moved:
        try:
            from bus.registry import unregister_owner
            removed_tools = unregister_owner("skill:" + name)
        except Exception:                                        # noqa: BLE001
            pass
        try:
            # `/名字` 得当场消失（指令型那条）。
            from bus.loaders.skill_md import invalidate_command_map
            invalidate_command_map()
        except Exception:                                        # noqa: BLE001
            pass
        try:
            # 包都没了，那条屏蔽账不该留着（否则用户会看到一条点不到的"已屏蔽"）。
            from bus.quarantine import release
            release("skill:" + name)
        except Exception:                                        # noqa: BLE001
            pass

    return jsonify({"ok": moved, "moved": moved, "tools_removed": removed_tools,
                    "degraded": bool(getattr(outcome, "degraded", False)),
                    "message": message})


@app.route("/api/skills")
def api_skills():
    """Return loaded skill metadata for the Skills page."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    skills = _scan_skills()
    # Check which skills have saved configs
    settings = _read_settings()
    saved_configs = settings.get("skill_configs", {})
    # If framework is running, enrich with registration status
    if _FRAMEWORK_RUNNING:
        from bus.registry import list_tools
        for s in skills:
            s["loaded"] = True
            s["tool_count"] = s.get("tool_count", 0)
    # Mark configured skills
    for s in skills:
        s["configured"] = s["name"] in saved_configs
    # 启用状态（技能页右下角的按钮）
    try:
        from bus import skill_state
        _states = skill_state.states()
    except Exception:                                         # noqa: BLE001
        _states = {}
    for s in skills:
        s["state"] = _states.get(s["name"], "on")
    # 装载结果（失败阶段与原因），没记录的不带这一格。
    try:
        from core import skill_load_report
        _load = skill_load_report.all()
    except Exception:                                         # noqa: BLE001
        _load = {}
    for s in skills:
        s["load"] = _load.get(s["name"])
    # 后台服务的当前状态（只有声明了 start 的技能才有这一格）。
    try:
        from core import services as _services
        _svc = _services.status_all()
    except Exception:                                         # noqa: BLE001
        _svc = {}
    for s in skills:
        st = _svc.get(s["name"])
        if st:
            s["service"] = st
    # 外部程序（`manifest.yaml` 的 `external:`）—— 当场查，不缓存：
    # 用户刚装完 ffmpeg，技能页下一次刷新就该变绿。
    try:
        from core import externals as _ext
    except Exception:                                         # noqa: BLE001
        _ext = None
    for s in skills:
        specs = s.get("external") or []
        if not specs or _ext is None:
            s.pop("external", None)
            continue
        try:
            status = _ext.check(specs, root=s.get("path"))
            status["text"] = _ext.describe(status)
        except Exception as e:                                # noqa: BLE001
            status = {"ok": True, "total": 0, "present": [], "missing": [],
                      "text": "", "error": "%s: %s" % (type(e).__name__, e)}
        s["external"] = status
    return jsonify({"skills": skills})


@app.route("/api/skills/state", methods=["POST"])
def api_skill_state():
    """启用或停用一个技能。名字与状态都走 body。

    停用的效果：工具型技能的工具不进 tools=、也不能执行；指令型技能从模型索引
    与 `/` 命令里消失。启用立即生效，不需要重启。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    state = str(data.get("state") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": tr("要 name")}), 400
    try:
        from bus import skill_state
        got = skill_state.set_state(name, state)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:                                  # noqa: BLE001
        return jsonify({"ok": False,
                        "error": "%s: %s" % (type(exc).__name__, exc)}), 500
    return jsonify({"ok": True, "name": name, "state": got})


@app.route("/api/skills/install", methods=["POST"])
def api_skill_install():
    """把用户选的一个目录装成技能（复制到用户技能目录）。

    与卸载那条同一条纪律的两个方向：那条的路径只从扫描结果取；
    这条必须收用户给的路径（他要装的东西还没被扫到），所以只做三件事：

      1. 只读源：路径必须存在、是目录、且含 `SKILL.md` 或 `manifest.yaml`
         —— 不是这个形状的目录不当技能；
      2. 目标固定：一律写到 `paths.user_skills_dir()`（数据根下，升级不动），
         不覆盖已有的同名技能；
      3. 做完就热加载（否则用户装完还要重启，"装上了"就成了空话）。

    `symlinks=True`：不跟随源里的符号链接 —— 跟随的话一个指向上层的链接
    会把整棵树复制进来（而 `~/.claude/skills` 那 27 个全是链接）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403

    import shutil as _shutil
    body = request.get_json(silent=True) or {}
    raw = str(body.get("path") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": tr("缺少 path")}), 400

    try:
        src = Path(raw).expanduser().resolve()
    except OSError as exc:
        return jsonify({"ok": False, "error": tr("路径读不了：%s") % exc}), 400
    if not src.is_dir():
        return jsonify({"ok": False, "error": tr("不是一个目录")}), 400
    if not ((src / "SKILL.md").is_file() or (src / "manifest.yaml").is_file()):
        return jsonify({"ok": False,
                        "error": tr("这个目录里既没有 SKILL.md 也没有 manifest.yaml")}), 400

    name = src.name
    if not name or name.startswith((".", "_", "$")):
        return jsonify({"ok": False,
                        "error": tr("目录名不能作为技能名（以 . _ $ 开头）")}), 400

    dst_root = _paths.user_skills_dir()
    dst = dst_root / name
    if dst.exists():
        return jsonify({"ok": False,
                        "error": tr("已经有同名技能：%s") % name}), 409

    try:
        dst_root.mkdir(parents=True, exist_ok=True)
        _shutil.copytree(src, dst, symlinks=True)
    except OSError as exc:
        return jsonify({"ok": False, "error": tr("复制失败：%s") % exc}), 500

    # 装完当场让它可用（两半：工具型 + 指令型）。
    reloaded = False
    try:
        from bus.loaders.skill_loader import reload_skills
        from bus.loaders.skill_md import invalidate_command_map
        reload_skills("skills", None)
        invalidate_command_map()
        reloaded = True
    except Exception as exc:                                  # noqa: BLE001
        print("[skills] 装好了但热加载失败（重启后生效）：%s: %s"
              % (type(exc).__name__, exc))

    return jsonify({"ok": True, "name": name, "path": str(dst),
                    "reloaded": reloaded})


def _skill_config_payload(name: str, reveal: bool = False) -> dict:
    """某个技能已保存的配置（值打码，见 github 那一支）。

    `reveal=True`（`?reveal=1`）：声明式技能返回明文 —— 那是界面上
      那颗「眼睛」用的（「点了怎么还是 4 个星号」）。
      端点本来就要 `X-Crannia-Token` / `?token=`，而这份配置是用户自己的
      所以他明确点开看一眼，不该被挡住（`security-must-not-block-the-task`）。
      老的两个特例（mail / github）不支持 reveal —— 它们的凭据另有出口
      （`core/github_config`），别在这里开第二个口子。
    """
    # 声明式优先 —— 只要 manifest 里写了 `settings`
    #   （schema + storage），就走通用那条：读那个文件、按 schema 打码。
    #   老的两个特例（mail_parser / github）保持原样，一个字节不变。
    try:
        from core import skill_settings as _ss
        if _ss.available(name):
            cfg = _ss.load(name) if reveal else _ss.public(name)
            return {"name": name, "config": cfg, "schema": _ss.schema_for(name)}
    except Exception as e:                                # noqa: BLE001
        # 这条闸自己不许拦路：读不出来就退回老路（下面那些分支）
        print(f"[skill-config] 声明式配置读不出来（{type(e).__name__}: {e}）"
              f" —— '{name}' 走旧路径")
    settings = _read_settings()
    saved = settings.get("skill_configs", {}).get(name, {})
    # Auto-migrate old single-account mail config to multi-account structure
    if name == "mail_parser":
        saved = _normalize_mail_config(saved)
    # 凭据不回显：GitHub 的令牌在读出时打码 ——
    #   界面只需要知道"有哪些条目、哪个是默认"，不需要看到值。
    #   修改时必须重新输入（与"不支持静默覆盖"同一条判据）。
    if name == "github":
        from core import github_config as _gc
        table, active = _gc.entries()
        saved = {"active": active,
                 "entries": {k: {"token": "****" if (v or {}).get("token") else ""}
                             for k, v in table.items()}}
    # 凭据不回显（mail 那条）：只打码 `password`，其余字段照原样给
    #   （修改别的字段时不用把邮箱地址也重敲一遍）。
    if name == "mail_parser" and isinstance(saved, dict):
        accts = saved.get("accounts")
        if isinstance(accts, dict):
            saved = {"active": saved.get("active", ""),
                     "accounts": {k: {**v, "password": "****" if (v or {}).get("password") else ""}
                                  if isinstance(v, dict) else v
                                  for k, v in accts.items()}}
    return {"name": name, "config": saved}


@app.route("/api/skills/<name>/config", methods=["GET"])
def api_get_skill_config(name: str):
    """Return saved configuration for a specific skill."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    return jsonify(_skill_config_payload(name))


@app.route("/api/skills/config", methods=["GET", "POST"])
def api_get_skill_config_by_name():
    """同义，名字走 query 或 body —— 前端那条路径必须是字面路由
    （`tests/test_stars_ui.py` 按字面路径对账）。

    界面那条表驱动的配置卡只用这一个字面路径做读写 ——
      前端写不出 `/api/skills/<名字>/config` 这种动态串（对账的扫描器按
      字面量抽路径，动态串会被抽成 `/api/skills/`，当场红）。

    名字两处都认：旧式技能卡把 `{name, config}` 放在 body 里，表驱动卡把名字
      放在 query 里。同一个 URL 上注册两条 `POST` 规则时，后一条会被先注册的
      那条遮蔽（永远不可达）—— 所以这里只留一条。`config` 有就拆开，
      没有就把整份 body 当配置。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    name = str(request.args.get("name") or data.get("name") or "").strip()
    if not name:
        return jsonify({"error": tr("缺少 name")}), 400
    if request.method == "POST":
        cfg = data.get("config")
        if "config" in data and not isinstance(cfg, dict):
            return jsonify({"ok": False, "error": tr("要 name 和 config")}), 400
        return _save_skill_config(name, cfg if isinstance(cfg, dict) else data)
    reveal = str(request.args.get("reveal") or "") in ("1", "true", "yes")
    return jsonify(_skill_config_payload(name, reveal=reveal))


@app.route("/api/skills/<name>/config", methods=["POST"])
def api_save_skill_config(name: str):
    """Save configuration for a specific skill.

    For mail_parser with multi-account support, the body should be the full
    accounts structure: {active, accounts: {id: {label, ...}}}.
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    return _save_skill_config(name, request.get_json() or {})


def _save_skill_config(name: str, data: dict):
    """存一个技能的配置（两条路由共用这一处）。"""
    # 声明式技能走通用那条（校验 + 打码换回 + 原子写）。
    #   校验失败要 400 且说清哪一格，不是默默存一半。
    try:
        from core import skill_settings as _ss
        if _ss.available(name):
            err = _ss.save(name, data)
            if err:
                return jsonify({"error": err}), 400
            return jsonify({"ok": True, "name": name})
    except Exception as e:                                # noqa: BLE001
        print(f"[skill-config] 声明式配置写不进去（{type(e).__name__}: {e}）"
              f" —— '{name}' 走旧路径")

    settings = _read_settings()
    if "skill_configs" not in settings:
        settings["skill_configs"] = {}

    # 打码值回写：界面读到的凭据是 `****`，它原样送回来时不是新值。
    #   不换回旧值 = 把用户的密码写成四个星号，而界面还会说「已保存」。
    #   挂在唯一写入口上，不另起一条路由。
    old = (settings.get("skill_configs") or {}).get(name) or {}
    data = _keep_masked_secrets(name, data, old if isinstance(old, dict) else {})

    # For mail_parser, store the normalized accounts structure as-is
    if name == "mail_parser" and "accounts" in data:
        settings["skill_configs"][name] = {
            "active": data.get("active", ""),
            "accounts": data.get("accounts", {}),
        }
    else:
        settings["skill_configs"][name] = data

    _write_settings(settings)
    return jsonify({"ok": True, "name": name})


#: 前端拿到的凭据是打码的 —— 它原样送回来时不是新值。
_MASK = "****"


def _keep_masked_secrets(name: str, new_cfg: dict, old_cfg: dict) -> dict:
    """把新配置里值为 `` 的凭据换回旧值（不静默覆盖，也不静默抹掉）。

    为什么必须有这一条：界面不回显凭据（`GET` 打码），
      所以「改了一个账号的服务器地址、其他账号原封不动保存」时，
      那些账号的凭据是 `****`。不换回去 = 把用户的密码写成四个星号。
    只覆盖 `accounts` / `entries` 里同名字段；新条目（旧配置里没有）保持原样。

    读者：`tests/test_skills_install.py` 的掩码那两条。
    """
    if name not in ("github", "mail_parser"):
        return new_cfg
    for key in ("accounts", "entries"):
        new_map, old_map = new_cfg.get(key), old_cfg.get(key)
        if not isinstance(new_map, dict) or not isinstance(old_map, dict):
            continue
        for entry_name, entry in new_map.items():
            old_entry = old_map.get(entry_name)
            if not isinstance(entry, dict) or not isinstance(old_entry, dict):
                continue
            for field, value in list(entry.items()):
                if value == _MASK and old_entry.get(field) not in (None, "", _MASK):
                    entry[field] = old_entry[field]
    return new_cfg


@app.route("/api/skills/<name>/test-and-save", methods=["POST"])
def api_test_and_save_skill_config(name: str):
    """Test mail config by sending a test email, then save if successful."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403

    data = request.get_json() or {}
    provider_key = data.get("_provider", "qq")
    email_address = data.get("email_address", "").strip()
    password = data.get("password", "").strip()

    if not email_address or not password:
        return jsonify({"ok": False, "error": tr("请填写邮箱地址和授权码")}), 400

    # Load provider presets from mail_parser manifest
    import yaml
    manifest_path = _PROJECT_ROOT / "skills" / "mail_parser" / "manifest.yaml"
    presets = {}
    if manifest_path.exists():
        try:
            with open(manifest_path, encoding="utf-8") as f:
                m = yaml.safe_load(f)
            presets = (m.get("config") or {}).get("provider_presets", {})
        except Exception:
            pass

    preset = presets.get(provider_key, {})
    defaults = preset.get("defaults", {})
    smtp_server = data.get("smtp_server", "").strip() or defaults.get("smtp_server", "smtp.qq.com")
    smtp_port = int(data.get("smtp_port", 0) or defaults.get("smtp_port", 465))

    if not smtp_server:
        return jsonify({"ok": False, "error": tr("无法确定 SMTP 服务器地址")}), 400

    # Attempt to send a test email
    import smtplib
    import ssl
    import email.utils
    from email.mime.text import MIMEText
    from email.header import Header

    try:
        ctx = ssl.create_default_context()
        # Build the test message first
        msg = MIMEText(
            "这是一封来自 Crannia AI Agent 的测试邮件。\n\n"
            "如果您收到此邮件，说明邮箱配置成功！\n\n"
            f"配置信息：\n"
            f"  邮箱：{email_address}\n"
            f"  提供商：{preset.get('label', provider_key)}\n"
            f"  SMTP：{smtp_server}:{smtp_port}\n\n"
            f"— Crannia v{__version__}",
            "plain", "utf-8"
        )
        msg["Subject"] = Header("Crannia 邮件测试", "utf-8")
        msg["From"] = email_address
        msg["To"] = email_address
        msg["Date"] = email.utils.formatdate(localtime=True)

        # Gmail/Outlook use STARTTLS on 587, QQ/163 use SSL on 465
        if smtp_port == 587:
            with smtplib.SMTP(smtp_server, smtp_port, timeout=15) as server:
                server.starttls(context=ctx)
                server.login(email_address, password)
                server.sendmail(email_address, [email_address], msg.as_string())
        else:
            with smtplib.SMTP_SSL(smtp_server, smtp_port, context=ctx, timeout=15) as server:
                server.login(email_address, password)
                server.sendmail(email_address, [email_address], msg.as_string())

    except smtplib.SMTPAuthenticationError:
        return jsonify({
            "ok": False,
            "error": tr("SMTP 认证失败，请检查邮箱地址和授权码（非登录密码）。")
        })
    except smtplib.SMTPConnectError:
        return jsonify({
            "ok": False,
            "error": trf("无法连接到 SMTP 服务器 {smtp_server}:{smtp_port}，请检查服务器地址和端口", smtp_server, smtp_port)
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": trf("邮件发送失败：{type(e).__name__}: {e}", type(e).__name__, e)
        })

    # Save config on success — into multi-account structure
    import time
    settings = _read_settings()
    if "skill_configs" not in settings:
        settings["skill_configs"] = {}

    # Normalize existing config (handles old → new migration)
    existing = _normalize_mail_config(settings.get("skill_configs", {}).get(name, {}))
    accounts = existing.get("accounts", {})

    # Determine account ID: use provided, or generate new
    account_id = data.get("account_id", "").strip()
    if not account_id or account_id not in accounts:
        account_id = f"acc_{time.time_ns()}"  # ns precision, no collision

    # Build account entry from form data
    account_entry = {k: v for k, v in data.items() if k != "account_id"}
    if "label" not in account_entry or not account_entry["label"]:
        account_entry["label"] = email_address.split("@")[0]
    accounts[account_id] = account_entry

    # If this is the first account or no active set, make it active
    active = existing.get("active", "")
    if not active or active not in accounts:
        active = account_id

    settings["skill_configs"][name] = {
        "active": active,
        "accounts": accounts,
    }
    _write_settings(settings)

    # 回显走 `_skill_config_payload` 那条 —— 它按技能名打码凭据
    #   （mail 的 `password`、github 的 `token`）。这里直接回
    #   `settings["skill_configs"][name]` 会把刚存的授权码原样送回前端，
    #   与同一份数据在 GET 那条路上打码的做法不一致。
    return jsonify({
        "ok": True,
        "message": trf("测试邮件已发送至 {email_address}，请检查收件箱（含垃圾邮件箱）。配置已保存。", email_address),
        "accounts": _skill_config_payload(name).get("config", {}),
    })


# MCP management APIs

_mcp_installs: dict[str, dict] = {}


@app.route("/api/mcp/tools", methods=["GET"])
def api_mcp_tools():
    """Return registered MCP tools and configured servers."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    tools = []
    external_servers = {}
    config_servers = {}
    if _FRAMEWORK_RUNNING:
        try:
            from mcpx.manager import get_manager, _load_external_config
            mgr = get_manager()
            from bus.registry import list_tools as _reg_tools
            for name in _reg_tools():
                if name.startswith("mcp__"):
                    parts = name.split("__", 2)
                    tools.append({
                        "name": name, "server": parts[1] if len(parts) > 1 else "?",
                        "tool": parts[2] if len(parts) > 2 else name,
                    })
            for sname, srv in mgr._external_servers.items():
                external_servers[sname] = {
                    "connected": True,
                    "tool_count": len(srv._tools) if hasattr(srv, "_tools") else 0,
                }
            for cfg in _load_external_config():
                name = cfg.get("name", "?")
                if name not in external_servers:
                    config_servers[name] = {
                        "connected": False,
                        "command": cfg.get("command", ""),
                        "args": cfg.get("args", []),
                    }
        except Exception as e:
            _log("[web] 读取 MCP 服务列表失败：%r" % (e,))
            return jsonify({"error": _error_brief(
                e, "读取 MCP 服务列表", "mcp_servers.json")}), 500
    # 这个数据实数，不写死 ——
    #   判据只有一个：`mcpx.servers.BUILTIN_SERVERS` 那几条的 owner 底下
    #   挂着几个工具。框架没起时就是 0（那时确实一个都没注册），
    #   任何写死的值在那种时候都是假的。
    return jsonify({
        "tools": tools, "builtin_count": _builtin_mcp_tool_count(),
        "external_connected": external_servers,
        "external_configured": config_servers,
    })


def _builtin_mcp_tool_count() -> int:
    """内置 MCP 现在真的注册了几个工具。判据：owner ∈ `BUILTIN_SERVERS` 那几个。"""
    try:
        from bus.registry import get_tool_owner, list_tools
        from mcpx.servers import BUILTIN_SERVERS, owner_of
        owners = {owner_of(s) for s in BUILTIN_SERVERS}
        return sum(1 for n in list_tools() if get_tool_owner(n) in owners)
    except Exception:                                            # noqa: BLE001
        return 0


def _mcp_server_rows() -> list[dict]:
    """内置 + 外部 MCP 服务器各自的现状（MCP 管理浮窗的数据源）。

    "内置"的判据只有一处：`mcpx.servers.BUILTIN_SERVERS`。
    一条服务器名字只当键 —— 工具名从注册表现数，不在别处再抄一份。
    """
    from bus.mcp_disabled import all_entries as _disabled
    from bus.registry import get_tool_owner, list_tools
    from mcpx.manager import _load_external_config, get_manager
    from mcpx.servers import BUILTIN_SERVERS, owner_of

    try:
        disabled = _disabled()
    except Exception:                                            # noqa: BLE001
        disabled = {}
    mgr = get_manager()
    all_tools = list(list_tools())

    def _tools_of(owner: str) -> list[str]:
        return sorted(n for n in all_tools if get_tool_owner(n) == owner)

    rows = []
    for name in BUILTIN_SERVERS:
        owner = owner_of(name)
        rows.append({"name": name, "owner": owner, "builtin": True,
                     "connected": True, "disabled": owner in disabled,
                     "tools": _tools_of(owner)})
    connected = set(mgr._external_servers)
    try:
        configured = {c.get("name", "") for c in _load_external_config() if c.get("name")}
    except Exception:                                            # noqa: BLE001
        configured = set()
    for name in sorted(connected | configured):
        owner = "mcp:" + name
        rows.append({"name": name, "owner": owner, "builtin": False,
                     "connected": name in connected, "disabled": owner in disabled,
                     "tools": _tools_of(owner)})
    for r in rows:
        r["tool_count"] = len(r["tools"])
    return rows


def _known_mcp_server(name: str) -> bool:
    """这个名字是不是已知的 MCP 服务器（内置 / 已配的外部）。

    同 `uninstall` 那条判据：名字只当键 —— 不在这里凭空拼一个 owner
      就往账本里写（`../` 之类构造不出东西，因为它压根不在扫描结果里）。
    """
    if not name:
        return False
    return any(r["name"] == name for r in _mcp_server_rows())


@app.route("/api/mcp/servers")
def api_mcp_servers():
    """MCP 服务器一览 —— 内置 + 外部 + 已停用。

    内置的后面带一个「内置」标记；内置与外部都支持停用。

    读不到（框架没起），照实回空表 + `error`，不冒充"没有服务器"
      （同 `/api/quarantine` 那条判据：空表会被读成一个结论）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    if not _FRAMEWORK_RUNNING:
        return jsonify({"servers": [], "status": None,
                        "error": tr("框架没在跑 —— 现在读到的是空的，不代表没有服务器")})
    try:
        from mcpx.manager import get_manager
        return jsonify({"servers": _mcp_server_rows(),
                        "status": get_manager().get_status()})
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"servers": [], "status": None,
                        "error": "%s: %s" % (type(e).__name__, e)}), 503


@app.route("/api/mcp/disable", methods=["POST"])
def api_mcp_disable():
    """停用一个 MCP 服务器（内置 / 外部都行）。

    判据：停用 = 只改筛子，不摘注册表，「启用」是真的可逆的
      （为什么这么定：`bus/mcp_disabled` 的 docstring）。
      不能走 `unregister_owner` 那条路去停用内置服务器 ——
      它的工具归属是 `framework`，而 `unregister_owner` 明确拒摘 `framework`。

    名字只当键：不在名单里的名字，404（构造不出 owner）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": tr("缺少 name")}), 400
    if not _known_mcp_server(name):
        return jsonify({"ok": False, "error": tr("没有这台 MCP 服务器：%s") % name}), 404
    try:
        from bus.mcp_disabled import disable
        entry = disable("mcp:" + name)
        return jsonify({"ok": True, "disabled": True, "owner": entry["owner"],
                        "message": tr("已停用 %s —— 它的工具当场不再发给模型。") % name})
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500


@app.route("/api/mcp/enable", methods=["POST"])
def api_mcp_enable():
    """启用（撤销停用）。与 `disable` 成对 —— 这半条就是"可逆"本身。"""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": tr("缺少 name")}), 400
    try:
        from bus.mcp_disabled import enable
        ok = enable("mcp:" + name)
        return jsonify({"ok": True, "enabled": ok,
                        "message": ("已启用 %s。" % name) if ok
                                   else ("%s 本来就没在停用名单里。" % name)})
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500


@app.route("/api/groups")
def api_groups():
    """多 agent 群聊的名册。

    这一层只到"建得出来、存得下、读得回"为止 —— 它不派活、不跑模型。

    判据与校验全在 `core/groups.py`（纯函数，能逐条断言）；
      这一条只是把它接上网，一个字都不自己判（一处定义）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    try:
        from core import groups as _groups
        rows = sorted(_groups.all_groups().values(),
                      key=lambda g: g.get("created_at") or 0)
        for g in rows:
            g["host"] = _groups.host_of(g)        # 派生一格给界面用，不落盘
        #: 成员模型下拉的选项随这个载荷一起下发 ——
        #:   与设置页那个子 agent 下拉同源（`model_configs.names_payload()`），
        #:   界面不必再单独拉一次 `/api/model-configs` 自己拍平。
        try:
            from core import model_configs as _mc
            model_options = _mc.names_payload()
        except Exception as _e:                                 # noqa: BLE001
            print(f"[groups] 模型选项拉不到（{type(_e).__name__}: {_e}）—— 空表下发")
            model_options = []
        return jsonify({"groups": rows, "count": len(rows),
                        #: 上限由后端给 —— 前端不自己写死一个 5 / 一个 8 / 一个 40
                        #:   （写死就是第二份真相源，而用户的 5 是"暂时"限制）
                        "max_members": _groups.MAX_MEMBERS,
                        "max_groups": _groups.MAX_GROUPS,
                        "max_name": _groups.MAX_NAME,
                        "max_role": _groups.MAX_ROLE,
                        #: 群级 brief 的上限也由后端给。
                        "max_brief": _groups.MAX_BRIEF,
                        "model_options": model_options})
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"groups": [], "error": "%s: %s" % (type(e).__name__, e)})


@app.route("/api/groups/create", methods=["POST"])
def api_groups_create():
    """建群。不合规，400 + 说清是哪一条判据，一个字节都不落盘
    （同 `skill_factory` 那条"先体检、后落盘"）。

    成员的判据（至多 8 个 / 主理人至多 1 个 / 名字不重复 / 名字缺省 workerN）
      都在 `core/groups.normalize_members` 里 —— 端点不重判一遍
      （两处各判一次就是会各自漂的形状）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    try:
        from core import groups as _groups
        entry = _groups.create(name=data.get("name") or "",
                               members=data.get("members"),
                               # 群级背景 + 圆桌开关。
                               brief=data.get("brief") or "",
                               roundtable=data.get("roundtable") if "roundtable" in data else False)
        return jsonify({"ok": True, "group": entry,
                        "session": _groups.session_key_of(entry["id"]),
                        "message": tr("群聊已创建（%d 个成员）。") % len(entry["members"])})
    except ValueError as e:
        # `GroupError` 继承 `ValueError`，这里一条就够，不必 import 那个类
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500


@app.route("/api/groups/update", methods=["POST"])
def api_groups_update():
    """改群名 / 改成员表（「建后可以改」）。

    只改传上来的那几格 —— `name` / `members` 缺席 = 不动它。
      别写成"没传就当空"（那会把群名清成 `未命名群聊`，而用户只是改了个成员）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    gid = str(data.get("id") or "").strip()
    if not gid:
        return jsonify({"ok": False, "error": tr("缺少 id")}), 400
    try:
        from core import groups as _groups
        entry = _groups.update(gid,
                               name=data.get("name") if "name" in data else None,
                               members=data.get("members") if "members" in data else None,
                               # 缺席 = 不动（`brief` 传空串 = 清空）。
                               brief=data.get("brief") if "brief" in data else None,
                               roundtable=(data.get("roundtable")
                                           if "roundtable" in data else None))
        return jsonify({"ok": True, "group": entry, "message": tr("已保存。")})
    except KeyError:
        return jsonify({"ok": False, "error": tr("没有这个群聊：%s") % gid}), 404
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500


@app.route("/api/groups/member", methods=["POST"])
def api_groups_member():
    """只改一个成员的职责/模型。

    与 `/api/groups/update` 分工不同：
      · `update` = 整表保存（会走 `normalize_members` —— 无主理人时
        全部职责必填）；
      · `member` = 逐个填（只判这一格，允许"先填一个、其余还空着"的中间态）。
      所以少了这条，无主理人的群永远填不完（第一次填任何一个都会被拒）。
    两条判据都在 `core/groups.py` 里 —— 端点一个字不自己判。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    gid = str(data.get("id") or "").strip()
    if not gid:
        return jsonify({"ok": False, "error": tr("缺少 id")}), 400
    if "index" not in data:
        return jsonify({"ok": False, "error": tr("缺少 index")}), 400
    if "role" not in data and "model" not in data:
        # 两格都没给 = 这次调用什么都不会改 —— 别回"已保存"
        return jsonify({"ok": False, "error": tr("role / model 至少要给一个")}), 400
    try:
        from core import groups as _groups
        entry = _groups.update_member(
            gid, data.get("index"),
            name=data.get("name") or "",
            role=data.get("role") if "role" in data else None,
            model=data.get("model") if "model" in data else None)
        idx = data.get("index")
        who = ""
        if isinstance(idx, int) and not isinstance(idx, bool):
            members = entry.get("members") or []
            if 0 <= idx < len(members):
                who = str(members[idx].get("name") or "")
        return jsonify({"ok": True, "group": entry,
                        "message": tr("已保存%s的配置。") % ("「%s」" % who if who else "该成员")})
    except KeyError:
        return jsonify({"ok": False, "error": tr("没有这个群聊：%s") % gid}), 404
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500


@app.route("/api/groups/delete", methods=["POST"])
def api_groups_delete():
    """删群（删的是群这个名册条目）。

    它不删那条会话的消息行 —— "群没了"之后 `history_search` 照样搜得到
      它说过的话（同 `/clear` 的留档判据）。回执要照实说清删了什么，
      别写成"聊天记录已删除"（那是「标签在说谎」）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    gid = str(data.get("id") or "").strip()
    if not gid:
        return jsonify({"ok": False, "error": tr("缺少 id")}), 400
    try:
        from core import groups as _groups
        ok = _groups.delete(gid)
        return jsonify({"ok": True, "deleted": ok,
                        "message": ("群聊已解散（它说过的话仍在历史库里）。" if ok
                                    else "没有这个群聊：%s" % gid)})
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500


@app.route("/api/model-configs")
def api_model_configs():
    """模型的「配置条目」 —— 端配置页读的那一份。

    绝不回 api 明文：每条只带 `has_api` + `api_tail`（打码在
      `core/model_configs.masked` 里，一处定义）。

    判据与校验全在 `core/model_configs.py`（纯函数，能逐条断言）；
      这一条只是把它接上网，一个字都不自己判（一处定义）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    try:
        from core import model_configs as _mc
        return jsonify(_mc.cards_payload())
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"cards": [], "error": "%s: %s" % (type(e).__name__, e)})


@app.route("/api/model-configs/create", methods=["POST"])
def api_model_configs_create():
    """建一条配置。不合规 => 400 + 说清是哪一条判据，一个字节都不落盘。"""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    try:
        from core import model_configs as _mc
        entry = _mc.create(card=data.get("card"), name=data.get("name"),
                           model=data.get("model"), base_url=data.get("base_url") or "",
                           api=data.get("api") or "", window=data.get("window"),
                           capabilities=data.get("capabilities"))
        return jsonify({"ok": True, "entry": entry,
                        "message": tr("配置已保存：%s") % entry["name"]})
    except ValueError as e:

        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500


@app.route("/api/model-configs/update", methods=["POST"])
def api_model_configs_update():
    """改一条。只改传上来的那几格 —— `api` 缺席 = 不动凭据（别写成"没传就当空"）。"""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    cid = str(data.get("id") or "").strip()
    if not cid:
        return jsonify({"ok": False, "error": tr("缺少 id")}), 400
    kw = {}
    for key in ("name", "model", "base_url", "api", "window", "capabilities"):
        if key in data:
            kw[key] = data[key]
    try:
        from core import model_configs as _mc
        entry = _mc.update(cid, **kw)
        return jsonify({"ok": True, "entry": entry, "message": tr("已保存。")})
    except KeyError:
        return jsonify({"ok": False, "error": tr("没有这条配置：%s") % cid}), 404
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500


@app.route("/api/model-configs/delete", methods=["POST"])
def api_model_configs_delete():
    """删一条。默认那条不许删（拒 + 带路线），见 `model_configs.delete`。"""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    cid = str(data.get("id") or "").strip()
    if not cid:
        return jsonify({"ok": False, "error": tr("缺少 id")}), 400
    try:
        from core import model_configs as _mc
        ok = _mc.delete(cid)
        return jsonify({"ok": True, "deleted": ok,
                        "message": "配置已删除。" if ok else "没有这条配置：%s" % cid})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500


@app.route("/api/model-configs/default", methods=["POST"])
def api_model_configs_default():
    """设为「默认启用」。

    它不热插拔正在跑的那个 provider —— `config.yaml` 那一套的既有语义
      就是"改了要重启"（`_switch_model` 的 docstring 逐字记着这条），
      而 `MemoryManager._provider` 是实时读的 + 并发上限 3
      所以运行期换实例会让跑着的那几轮半新半旧（压缩阈值与估算同时换口径）。
      本进程的接法在 `PipelineScheduler.initialize()`（`_apply_configured_model`），
      `/api/restart` 会重跑它，回执里明说这一点，别让用户以为按了就换了。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    cid = str(data.get("id") or "").strip()
    if not cid:
        return jsonify({"ok": False, "error": tr("缺少 id")}), 400
    try:
        from core import model_configs as _mc
        # 先试装、装得上才落盘。
        #   只写账本是不够的：某张卡背后的 SDK 可能在这个环境里没装，
        #   而那样界面会回"已设为默认"、模型一个字节没变，
        #   失败只躺在日志里（「标签在说谎」）。
        reason = _mc.preflight(cid)
        if reason:
            return jsonify({"ok": False,
                            "error": tr("此配置暂无法使用，未设为默认。原因：%s")
                                     % reason}), 400
        entry = _mc.set_default(cid)
        return jsonify({"ok": True, "entry": entry, "default": cid,
                        "message": tr("已设为默认：「%s」。新对话生效；当前这个需重启。")
                                   % entry["name"]})
    except KeyError:
        return jsonify({"ok": False, "error": tr("没有这条配置：%s") % cid}), 404
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500


def _probe_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _http_probe(url: str, headers: dict, timeout: int = 15,
                method: str = "GET", payload: bytes | None = None):
    """一次请求，不跟随重定向。回 `(status, body_text)`。

    不跟随是刻意的：跳转由调用方自己走，好让每一跳都过出网地板 ——
      与 `mcpx/servers/web_search.py::_http_get_once` 同一条判据
      （`urllib` 默认跟随 -> 只查第一个 URL 等于把围栏装在旁边那扇门上）。

    客户端由 `core.net_proxy.httpx_client()` 造 —— 与 provider 是同一台。
      光"也走代理"不够：真请求走 httpx（它认 `ALL_PROXY`），而裸用 urllib
      只认 `http_proxy` / `https_proxy`。两条路对"当前代理是谁"的答案不同，
      于是会出现「这里报通、真请求连不上」—— 而这个端点存在的意义正是
      回答"这条配置能不能用"。测试与真请求必须是同一条路。

    本机 / 内网目标一律直连（本地模型就在那儿；被代理接走会回一句
      「上游回了 HTTP 502」，用户看到的是"本机模型连不上"）。
    """
    from core import net_proxy as _np

    try:
        import httpx                                          # noqa: F401
    except Exception as exc:                                    # noqa: BLE001
        #: 不退回 urllib：那条路与真请求不同源，会把"测不通"说成"通"。
        return 0, ("连接失败：没有 httpx（%s）——联通测试与真请求共用同一客户端，"
                   "安装后重试" % (type(exc).__name__,))

    #: 本机 / 内网目标在这一层判直连 —— 判据在 `core/net_proxy`（与 provider
    #:   共用同一个函数，不是两段等价逻辑）：只给探针直连、真请求照旧走代理的话，
    #:   症状正是「联通测试成功、实际对话失败」。
    try:
        with _np.httpx_client(url=url, follow_redirects=False,
                              timeout=timeout) as client:
            r = client.request(method, url, headers=headers, content=payload)
            return r.status_code, r.content[:8192].decode("utf-8", "replace")
    except Exception as e:                                      # noqa: BLE001
        # 连不上也是"有结论"：域名解析不了 / 端口没开 / 超时 / 代理死了。
        #   `status = 0` 是这里唯一的"没有 HTTP 状态"表示 —— 调用方据此
        #   说人话（"没连上"），而不是把它当成一个 500 抛出去。
        return 0, "连接失败：%s: %s" % (type(e).__name__, str(e)[:300])


@app.route("/api/model-configs/test", methods=["POST"])
def api_model_configs_test():
    """简易联通测试 —— 真的发一次最小请求。

    优先 `GET {base_url}/models`（免费、只是列模型）；它 404/405 才退回
      一次 `max_tokens=1` 的对话。报的是上游原话，不是我们的复述。

    凭据绝不回显：错误里只带上游返回的正文，`Authorization` /
      `x-api-key` 永远不进响应、也不进日志 —— 上游 401 的正文常常把 key
      的前缀/尾段原样回显，所以正文要先过 `sanitize_output` 再截断。

    出网地板的边界（如实记）：本仓库的地板（`core/security.check_url_egress`）
      挡的是模型自己选的目的地；这里的地址是用户在设置页填的、存在配置条目里
      的，与"用户自己加一把 key"同一类。而本机 / 内网地址正是本地模型
      （`ollama` / `lmstudio`）与局域网中转站的正常位置，照发，
      但回执里说清它在本机/内网（不装作没有这回事）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    cid = str(data.get("id") or "").strip()
    try:
        from core import model_configs as _mc
        entry = _mc.get(cid) if cid else None
        if cid and not entry:
            return jsonify({"ok": False, "error": tr("没有这条配置：%s") % cid}), 404
        if entry:
            card = entry.get("card")
            base_url = entry.get("base_url") or ""
            model = entry.get("model") or ""
            api_key = _mc.api_for(cid)
        else:
            card = _mc.card_of(data.get("card"))
            base_url = str(data.get("base_url") or "")
            model = str(data.get("model") or "")
            api_key = str(data.get("api") or "")
        provider_dir = _mc.provider_dir_of(card)
        if not base_url:
            base_url = _manifest_base_url(provider_dir)
        if not base_url:
            #: `claude` 卡的 manifest 里 `base_url` 是故意空着的
            #:   （Anthropic 那侧由 SDK 自带默认），这里补上官方那个，
            #:   而不是回一句"你没填地址"（那会让用户以为必须填）。
            base_url = _PROTOCOL_DEFAULT_BASE.get(provider_dir, "")
        if not base_url:
            return jsonify({"ok": False, "window": None,
                            "error": tr("这条配置未填请求地址，服务商也没有提供默认地址，请填写地址。")}), 400

        from urllib.parse import urlsplit
        scheme = (urlsplit(base_url).scheme or "").lower()
        if scheme not in ("http", "https"):
            return jsonify({"ok": False, "error": "请求地址要以 http:// 或 "
                                                  "https:// 开头"}), 400

        scope, scope_note = "public", ""
        try:
            from core.security import check_url_egress
            check_url_egress(_probe_url(base_url, "/models"))
        except Exception as exc:                                # noqa: BLE001
            # 不是"拒绝"，是"这是本机/内网目标" —— 本地模型与局域网中转站
            #   就在那儿，照发，但说清楚。
            scope = "private"
            scope_note = ("这个地址在本机 / 内网（%s）—— 本地模型与局域网中转站"
                          "本来就在那儿，测试照发。" % str(exc)[:80])

        if provider_dir == "anthropic":
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        else:
            headers = {"Authorization": "Bearer %s" % api_key} if api_key else {}
        headers["Accept"] = "application/json"

        #: 两条协议拼不同的路径。
        #:   判据是地址的语义：Anthropic 那条按约定不带 `/v1`
        #:   （SDK 自己补 `/v1/messages`），探针得自己补 `/v1`；
        #:   而 OpenAI 兼容那条的地址已经到 API 根（`/v1` 或 `/api/v3` 之类）
        #:   所以直接接 `/models`。
        #:   两条都拼 `/models` 的话 Claude 卡会永远 404，
        #:   而用户看到的是一句"上游回了 HTTP 404"（看着像对方的问题）。
        if provider_dir == "anthropic":
            models_path, chat_path = "/v1/models", "/v1/messages"
        else:
            models_path, chat_path = "/models", "/chat/completions"

        url = _probe_url(base_url, models_path)
        status, body = _http_probe(url, headers)
        tried = "GET %s" % url
        if status in (404, 405, 400) and chat_path:
            # 这一家没有"列模型"的接口，退回一次最小对话
            # （`max_tokens: 1` —— 既然要"真发一次"，那就发最小的那次）
            import json as _json2
            payload = _json2.dumps({
                "model": model or "probe",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            }).encode("utf-8")
            h2 = dict(headers)
            h2["Content-Type"] = "application/json"
            url = _probe_url(base_url, chat_path)
            status, body = _http_probe(url, h2, method="POST", payload=payload)
            tried = "POST %s" % url

        ok = 200 <= int(status) < 400
        excerpt = _sanitize_probe_body(body)
        return jsonify({
            "ok": ok, "status": int(status), "tried": tried,
            "scope": scope, "scope_note": scope_note,
            "model": model, "base_url": base_url,
            "body": excerpt[:600],
            #: `status == 0` = 没有 HTTP 状态（连不上：解析 / 端口 / 超时 /
            #:   代理死了）。不许说成「上游回了 HTTP 0」—— 那是一句看了
            #:   更糊涂的话，而它恰恰是"真请求也连不上"的那种情形。
            "message": ("已联通（HTTP %d）" % status) if ok
                       else ((excerpt[:200] or tr("连接失败")) if int(status) == 0
                             else ("上游回了 HTTP %d" % status)),
        })
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500


#: manifest 里没写、但 SDK 自带的那几个默认地址。
#:
#: 今天只有 `anthropic` 一个 —— 它的 `manifest.yaml` 里 `base_url` 是故意空着的
#: （`anthropic/client.py` 不传 `base_url` 时 `Anthropic()` 自己用官方地址）。
#: 只补"查到确实有默认"的那些 —— 别在这里替供给方猜（猜错 = 把用户的
#: 联通测试打到别人的服务器上）。
_PROTOCOL_DEFAULT_BASE = {
    "anthropic": "https://api.anthropic.com",
}


def _manifest_base_url(provider_dir: str) -> str:
    """那家 provider 在 manifest 里写的默认地址（配置条目留空时用它）。"""
    try:
        import yaml as _yaml
        p = _PROJECT_ROOT / "providers" / provider_dir / "manifest.yaml"
        m = _yaml.safe_load(p.read_text(encoding="utf-8"))
        return str(((m or {}).get("config") or {}).get("base_url") or "")
    except Exception:                                           # noqa: BLE001
        return ""


def _sanitize_probe_body(body: str) -> str:
    """上游正文回给用户之前先打码。

    为什么必须：上游 401 的正文常常把 key 的前缀/尾段原样回显 ——
      上游回什么我们挡不住，但转发给用户之前必须过一道手。
      `SecurityGuard.sanitize_output` 原来只有 `post_process` 一处调它，
      而 HTTP 端点的 JSON body 完全不过那一道。
    打码之外还限长（`_http_probe` 已经只读 8192 字节，调用方再截 600）。
    """
    text = str(body or "")
    try:
        import re as _re
        # `sk-…` / `rk-…` 一族的 key 字面量
        text = _re.sub(r"\b(sk|rk|pk|ak)-[A-Za-z0-9_\-]{6,}", r"\1-***", text)
        # 长的不带分隔符的串（base64 / hex 老长的那些）
        text = _re.sub(r"\b[A-Za-z0-9_\-]{32,}\b", "***", text)
    except Exception:                                           # noqa: BLE001
        pass
    return text


@app.route("/api/session-model")
def api_session_model_get():
    """这一段对话用的是哪条模型配置。

    入参：`?storage_id=`（会话右侧那一段的存储 id；缺省，用当前绑定的那段）。

    判据全在 `core/session_model.py`（`describe` / `list_names`）——
      这一条只是把它接上网，一个字都不自己判。
    不碰 `short_term` 的写侧：这是一条纯读口（界面每次开浮窗都会问它）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    sid = str(request.args.get("storage_id") or "").strip()
    try:
        from core import model_configs as _mc
        from core import session_model as _sm
        opts = [{"id": cid, "name": name, "card_label": label, "model": model}
                for name, cid, label, model in _mc.list_names()]
        info = _sm.describe(sid) if sid else {"config_id": "", "frozen": False,
                                              "source": "none"}
        cur = _mc.get(info["config_id"]) if info["config_id"] else None
        dft = _mc.get(_mc.default_id())
        return jsonify({
            "ok": True,
            "storage_id": sid,
            "config_id": info["config_id"],
            "frozen": info["frozen"],
            "source": info["source"],
            #: 名字与模型名一起给 —— 界面那一行要写"正在用「便宜的」/ qwen3.8-max"，
            #:   只给 id 的话它得自己再查一次（而那就是第二份真相源）。
            "name": (cur or {}).get("name") or "",
            "model": (cur or {}).get("model") or "",
            "card_label": _mc.CARD_LABEL.get((cur or {}).get("card") or "", ""),
            "default_name": (dft or {}).get("name") or "",
            "options": opts,
        })
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "options": [],
                        "error": "%s: %s" % (type(e).__name__, e)})


@app.route("/api/session-model", methods=["POST"])
def api_session_model_set():
    """把这一段换成某条配置（`config_id` 空串 = 切回「跟随默认」）。

    先试装、后落盘（`preflight`）—— 装不上就不写：
      写了的话这一段的每一轮都会"解析到 A、跑 B"，而用户改默认也救不回来
      （标签在说谎）。这同「设为默认」那条判据。
    它是用户显式选择，走 `set_for`（覆盖已冻的那个），
      而不是轮末那条不覆盖的惰性冻结（两个动词，见 `core/session_model.py`）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    sid = str(data.get("storage_id") or "").strip()
    cid = str(data.get("config_id") or "").strip()
    if not sid:
        return jsonify({"ok": False, "error": tr("缺少 storage_id")}), 400
    try:
        from core import model_configs as _mc
        from core import session_model as _sm

        #: 归属核对（判据只有一处：`_desktop_thread` / `thread_channel`）——
        #:   端点收到的是客户端传来的 id，不核对的话这条路能给任意一段
        #:   写一行（包括别的通道的）。与 `/api/messages` 那两条只读口
        #:   用的同一个辅助函数，不在这里另写一份。
        mm = _mem()
        st = getattr(mm, "short_term", None) if mm is not None else None
        if st is None:
            return jsonify({"ok": False,
                            "error": tr("记忆库未连接：框架未启动。")}), 503
        ok, why = _desktop_thread(st, sid)
        if not ok:
            return jsonify({"ok": False, "error": why}), 404

        if cid:
            if not _mc.get(cid):
                return jsonify({"ok": False,
                                "error": tr("没有这条配置：%s") % cid}), 404
            reason = _mc.preflight(cid)
            if reason:
                return jsonify({"ok": False, "error": (
                    tr("此配置暂无法使用，未切换。原因：%s") % reason)}), 400
            _sm.set_for(sid, cid)
            e = _mc.get(cid) or {}
            msg = "已改用「%s」，下一条消息起生效" % (e.get("name") or cid)
        else:
            _sm.follow_default(sid)
            msg = "已切回跟随默认，下一条消息起生效。"
        return jsonify({"ok": True, "config_id": cid, "message": msg})
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500


@app.route("/api/session-workspace")
def api_session_workspace_get():
    """这一段对话的工作目录。

    入参：`?storage_id=`（那一段的存储 id；缺省，用当前绑定的那段）。

    判据全在 `core/session_workspace.py`（`describe`）—— 这一条只是把它
      接上网，一个字都不自己判。
    不碰写侧：这是一条纯读口。
    `default_dir` 与 `path` 分开给 —— 界面要说得出"你这个是选的还是
      默认的"（用户点了「用默认」之后，界面如果还写着某个目录名，
      他就不知道那个按钮到底生效没有）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    sid = str(request.args.get("storage_id") or "").strip()
    try:
        from core import session_workspace as _sw
        from core.workspace import default_output_dir
        info = _sw.describe(sid) if sid else {"path": "", "is_set": False,
                                              "source": "default"}
        return jsonify({
            "ok": True,
            "storage_id": sid,
            "path": info["path"],
            "is_set": info["is_set"],
            "source": info["source"],
            #: 没选时要落在哪儿 —— 界面那行字要写得出一个真路径，
            #:   而不是空白（"没选"与"落在默认工作区"是两件事，用户要看后者）。
            "default_dir": str(default_output_dir()),
        })
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})


@app.route("/api/session-workspace", methods=["POST"])
def api_session_workspace_set():
    """把这一段的工作目录设成某个目录（`path` 空串 = 切回「用默认工作区」）。

    先校验、后落盘 —— 校验不过就不写：写进去的话，这一段每一处写
      都会指着一个不存在的目录，而症状要到很远的地方才看得见
      （标签在说谎）。判据只有一处：`session_workspace.check_dir`。

    归属核对（与 `/api/session-model` 用的是同一个 `_desktop_thread`）——
      端点收到的是客户端传来的 id，不核对的话这条路能给任意一段写一行。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    sid = str(data.get("storage_id") or "").strip()
    path = str(data.get("path") or "").strip()
    if not sid:
        return jsonify({"ok": False, "error": tr("缺少 storage_id")}), 400
    try:
        from core import session_workspace as _sw
        mm = _mem()
        st = getattr(mm, "short_term", None) if mm is not None else None
        if st is None:
            return jsonify({"ok": False,
                            "error": tr("记忆库未连接：框架未启动。")}), 503
        ok, why = _desktop_thread(st, sid)
        if not ok:
            return jsonify({"ok": False, "error": why}), 404

        if not path:
            _sw.use_default(sid)
            return jsonify({"ok": True, "path": "", "is_set": False,
                            "message": tr("已切回默认工作区")})
        good, why2 = _sw.check_dir(path)
        if not good:
            return jsonify({"ok": False, "error": why2}), 400
        norm = _sw.set_for(sid, path)
        return jsonify({"ok": True, "path": norm, "is_set": True,
                        "message": tr("工作目录已改为 %s，下一条消息起生效。")
                                   % norm})
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500


@app.route("/api/mcp/install", methods=["POST"])
def api_mcp_install():
    """Run an MCP install command in background subprocess.

    SECURITY: Only whitelisted package-manager patterns are permitted.
    Every command is validated against a strict regex — anything that
    doesn't match exactly is rejected.  Dangerous operations (file
    access, copy, delete, privilege escalation, network tools) are
    blocked at the pattern level.
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403

    data = request.get_json() or {}
    command = data.get("command", "").strip()
    if not command:
        return jsonify({"ok": False, "error": tr("请输入安装指令")}), 400

    # Strict validation: only exact package-manager patterns
    error = _validate_install_command(command)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    import subprocess, secrets, threading
    task_id = secrets.token_hex(6)
    _mcp_installs[task_id] = {"status": "running", "output": "", "command": command}
    def _run():
        try:
            p = subprocess.run(command, shell=True, capture_output=True, text=True,
                               timeout=300, cwd=str(_PROJECT_ROOT))
            if p.returncode == 0:
                _mcp_installs[task_id] = {"status": "done", "output": p.stdout[-1000:] or "OK", "command": command}
            else:
                _mcp_installs[task_id] = {"status": "error", "output": p.stderr[-500:] or f"exit={p.returncode}", "command": command}
        except subprocess.TimeoutExpired:
            _mcp_installs[task_id] = {"status": "error", "output": "超时(5分钟)", "command": command}
        except Exception as e:
            _mcp_installs[task_id] = {"status": "error", "output": str(e), "command": command}
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id, "status": "running"})


@app.route("/api/mcp/install/<task_id>", methods=["GET"])
def api_mcp_install_status(task_id: str):
    """Poll install task status."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    info = _mcp_installs.get(task_id)
    if info is None:
        return jsonify({"error": tr("任务不存在")}), 404
    return jsonify(info)


@app.route("/api/mcp/auto-import", methods=["POST"])
def api_mcp_auto_import():
    """Parse install command → write to data/mcp_servers.json."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json() or {}
    install_cmd = data.get("install_command", "").strip()

    # Validate with same strict rules as install endpoint
    if install_cmd:
        error = _validate_install_command(install_cmd)
        if error:
            return jsonify({"ok": False, "error": error}), 400
    server_name = data.get("server_name", "").strip()
    entry_cmd = data.get("entry_command", "").strip()
    entry_args = data.get("entry_args", [])

    if not entry_cmd and install_cmd:
        parsed = _parse_mcp_install_command(install_cmd)
        if not parsed:
            return jsonify({"ok": False, "error": tr("无法解析安装指令")}), 400
        entry_cmd = parsed["command"]
        entry_args = parsed["args"]
        if not server_name:
            server_name = parsed["name"]
    if not entry_cmd:
        return jsonify({"ok": False, "error": tr("无法确定 MCP 启动命令")}), 400
    if not server_name:
        return jsonify({"ok": False, "error": tr("无法确定服务器名称")}), 400

    # Pre-flight: verify the package is actually installed
    verified = _verify_package_installed(entry_cmd, entry_args)
    if not verified:
        return jsonify({
            "ok": False,
            "error": (
                trf("  包未安装。请先使用「安装指令」安装，或确认 '{' '.join([entry_cmd] + entry_args)}' 可执行。", ' '.join([entry_cmd] + entry_args))
            ),
        }), 400

    import json as _json
    cfg_path = Path("data/mcp_servers.json")
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg.setdefault("mcpServers", {})[server_name] = {
        "command": entry_cmd, "args": entry_args, "env": {},
    }
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(_json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({
        "ok": True,
        "message": trf("'{server_name}' 已加入配置文件，重启框架后生效。", server_name),
        "server_name": server_name,
        "entry": {"command": entry_cmd, "args": entry_args},
    })


def _parse_mcp_install_command(cmd: str) -> dict | None:
    """Extract MCP server metadata from a package manager install command."""
    import re
    # npm install -g <pkg>
    m = re.match(r"npm\s+(?:install|i|add)\s+(?:-g|--global)\s+(.+)", cmd)
    if m:
        pkg = m.group(1).strip()
        return {"name": _extract_mcp_name(pkg), "command": "npx", "args": ["-y", pkg]}
    # npx -y <pkg> / npx --yes <pkg>
    m = re.match(r"npx\s+(?:-y|--yes)\s+(.+)", cmd)
    if m:
        pkg = m.group(1).strip()
        return {"name": _extract_mcp_name(pkg), "command": "npx", "args": ["-y", pkg]}
    # npx <pkg>
    m = re.match(r"npx\s+(.+)", cmd)
    if m:
        pkg = m.group(1).strip()
        return {"name": _extract_mcp_name(pkg), "command": "npx", "args": ["-y", pkg]}
    # uvx <pkg>
    m = re.match(r"uvx\s+(.+)", cmd)
    if m:
        pkg = m.group(1).strip()
        return {"name": _extract_mcp_name(pkg), "command": "uvx", "args": [pkg]}
    # pip install <pkg>
    m = re.match(r"pip(?:3)?\s+install\s+(?:--user\s+)?(.+)", cmd)
    if m:
        pkg = m.group(1).strip().rstrip(".")
        return {"name": _extract_mcp_name(pkg), "command": "python", "args": ["-m", pkg.replace("-", "_")]}
    return None


def _verify_package_installed(command: str, args: list[str]) -> bool:
    """
    Pre-flight check: run <command> <args> --version to verify
    the MCP package is actually installed before writing to config.
    """
    import subprocess
    try:
        test_args = [command] + list(args) + ["--version"]
        proc = subprocess.run(
            test_args, capture_output=True, text=True,
            timeout=15, cwd=str(_PROJECT_ROOT),
        )
        return proc.returncode == 0
    except Exception:
        return False


def _validate_install_command(cmd: str) -> str | None:
    """
    Strict whitelist-only validation for MCP install commands.

    Returns an error message string if invalid, None if the command
    passes all checks.
    """
    import re

    # Tier 1：在任何位置都拒的东西 —— 拼接符、重定向、路径穿越。
    # 它们本身就是把两条命令缝起来、或把内容搬出去的工具，出现在哪里都算危险。
    _BLACKLIST_ANY = (
        # Shell metacharacters (injection vectors)
        ";", "&&", "||", "|", "`", "$(", "${", "(", ")",
        # I/O redirection
        ">", "<", ">>", "<<",
        # Path traversal
        "..", "/etc/", "C:\\", "\\windows\\",
    )

    # Tier 1b：命令名 —— 只在命令位（整条的第一个词）判。
    # 逐字符判会把包名和子命令一起打中：`dd ` 命中 `add `（于是
    # `npm add -g` / `pnpm add -g` / `yarn global add` 这三条白名单里
    # 承诺的形状一条都装不上）、`mkdir` 命中 `mkdirp`、`kill` 命中 `kill-port`。
    # 而分隔符全在上面那张表里，第二条命令拼不进来 —— 命令位只有整条开头。
    _BLACKLIST_CMDS = frozenset((
        # File operations
        "rm", "del", "rmdir", "copy", "cp", "mv", "move",
        "cat", "type", "touch", "mkdir", "chmod", "chown", "dd", "tee",
        # Network tools
        "curl", "wget", "nc", "telnet", "ssh", "scp", "ftp", "rsync",
        # Process / system
        "kill", "pkill", "taskkill", "shutdown", "reboot",
        "sudo", "su", "runas", "eval",
        # Disk
        "format", "fdisk", "mkfs",
        # Environment / registry
        "export", "set", "unset", "reg",
        # Data exfiltration
        "echo", "base64", "xxd",
    ))

    cmd_lower = cmd.lower()
    for token in _BLACKLIST_ANY:
        if token in cmd_lower or token in cmd:
            return f"❌️ 检测到禁止的操作: {token.strip()}"
    head = cmd_lower.split()[:1]
    if head and head[0] in _BLACKLIST_CMDS:
        return f"❌️ 检测到禁止的操作: {head[0]}"

    # Tier 2: Only allow exact package-manager patterns
    # Each pattern captures: <prefix> <package-name>
    # Package name allows: @scope/name, name-with-dashes, name_with_underscores
    _PKG = r"([@\w][\w\-_./]*)"

    _ALLOWED = [
        # npm install -g <pkg>  /  npm i -g <pkg>  /  npm add --global <pkg>
        rf"^npm\s+(?:install|i|add)\s+(?:-g|--global)\s+{_PKG}$",
        # npx -y <pkg>  /  npx --yes <pkg>
        rf"^npx\s+(?:-y|--yes)\s+{_PKG}$",
        # pip install <pkg>  /  pip3 install --user <pkg>
        rf"^pip(?:3)?\s+install\s+(?:--user\s+)?{_PKG}$",
        # uvx <pkg>
        rf"^uvx\s+{_PKG}$",
        # uv tool install <pkg>
        rf"^uv\s+tool\s+install\s+{_PKG}$",
        # pnpm add -g <pkg>  /  pnpm install -g <pkg>
        rf"^pnpm\s+(?:add|install|i)\s+(?:-g|--global)\s+{_PKG}$",
        # yarn global add <pkg>
        rf"^yarn\s+global\s+add\s+{_PKG}$",
    ]

    for pattern in _ALLOWED:
        if re.match(pattern, cmd):
            return None  # Passed

    return "️ 请使用正确的安装指令。支持: npm install -g / npx -y / pip install / uvx / pnpm add -g / yarn global add"


def _extract_mcp_name(pkg: str) -> str:
    """mcp-server-time → time, @scope/mcp-doc → mcp-doc"""
    if "/" in pkg:
        pkg = pkg.split("/")[-1]
    for prefix in ("mcp-server-", "mcp-", "server-"):
        if pkg.startswith(prefix):
            pkg = pkg[len(prefix):]
            break
    return pkg.strip().split()[0]


@app.route("/api/commands")
def api_commands():
    """框架命令目录，给"输入 `/` 弹候选"用。

    只读、零参数、零状态：它读的是 `core/pre_process.py` 里那一份
    名单（`_FRAMEWORK_COMMANDS`）与挂在它旁边的说明表。

    为什么要有这个端点（而不是让前端自己抄一份）：
    命令的成员资格与说明都在后端，前端抄一份就是两套真相源 ——
    加一条命令时两边都会漂，而两边看着都对。
    边界：判定必须在服务端做，不能依赖客户端补全列表，
    否则「选了补全」与「没选」行为不同。

    `implemented: false` 的那几条是如实标的（`/cancel` `/done` 声明了但
    `process()` 里没有分支）—— 不许承诺兑现不了的东西。
    序号/顺序是呈现的事，前端要按使用频率重排就自己排。
    """
    try:
        from core.pipeline.stages.pre_process import framework_command_catalog
        return jsonify({"ok": True, "commands": framework_command_catalog()})
    except Exception as exc:  # noqa: BLE001
        # 判据同 `/api/context`：拿不到就出声，别回一个空列表 ——
        # "没有命令"与"读不到"长得一样，而前者会被读成一个结论。
        return jsonify({"ok": False,
                        "error": "%s: %s" % (type(exc).__name__, exc),
                        "commands": []}), 503


@app.route("/api/context")
def api_context():
    """会话构成面板 —— "本会话现在装了什么、各多大、和上次比变了多少"。

    只读：它不碰 `_pending_memory_inject` ——
    唯一的现算入口 `get_context()` 会把标志吃掉。唯一的写是给它自己的
    「与上次比」快照记一条，那份状态挂在 `core.context_report` 上、`/clear` 时清。

    会话键由 `_web_session()` 构造 —— 不许再写字面量
    （写字面量会让标志置在没人读的键上，而接口回 `ok: true` 谎报成功 ——
    见那个函数的 docstring）。

    框架还没起来时回 503，不回一张空格子的面板 ——
    "没有数据"与"有数据且全是 0"看起来一样，而后者会被读成一个结论
    （读不到一律别伪装成一个数）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    if _session_pool is None:
        return jsonify({"error": "framework not started (no SessionPool)"}), 503
    try:
        return jsonify(_session_pool.context_breakdown(session_key_for(_web_session())))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/todos")
def api_todos():
    """本会话的任务清单 —— 给 UI 面板用的数据源。

    它是只读的第二个读者：清单的写者只有一个 —— `todo_write` 工具
      （`core/todos.py` 判据 ①）。这一条不写、不建、不碰任何状态，
      它和 `/clear` 一样只是读。

    会话键由 `_web_session()` 推导 —— 与 `/api/context`、`/api/stop` 同一处，
      不许再写字面量（见 `_web_session` 的 docstring）。
      它必须和 agent 循环里那个键逐字相同，否则面板会永远显示空
      —— 而"空清单"看起来就是一个结论。

    清单不落盘（它活在工具回执里），重启之后这里就是空的，
      那是对的，不是故障。所以这一条不回 503 —— 它没有"读不到"那一档。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    try:
        from core import todos as _todos
        key = session_key_for(_web_session())
        items = _todos.get(key)                      # 已经是副本
        return jsonify({"session": key, "todos": items, "count": len(items),
                        "rendered": _todos.render(items)})
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/file-diff")
def api_file_diff():
    """右侧栏 diff —— 点文件名的那个落点。

    它是一道有边界的读取器，不是任意文件读取：内容只来自
    `core/file_history`（本会话在写权限校验那一刻留下的改前副本），
    路径与版本对不上留档，回 `kind="missing"`、正文为空。
    「改后」那一侧才读磁盘，而它读的仍是那条留过档的路径。

    框架没起来 / 没有留档时回一条说明（`note`），不回一张空白面板
    —— "没有数据"与"有数据但是空的"看起来一样（读不到 ≠ 0）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    path = str(request.args.get("path", "") or "")
    # 会话键由服务端推（照 `?group=` 那条规矩，同 `text` 别处）——
    # 客户端传什么不代表它能读哪一段的留档。
    try:
        session = session_key_for(_web_session(str(request.args.get("group", "") or "")))
    except Exception:                                    # noqa: BLE001
        session = ""
    try:
        version = int(request.args.get("version", "0") or 0)
    except (TypeError, ValueError):
        version = 0
    if not path or version <= 0:
        return jsonify({"ok": False, "kind": "missing", "diff": "", "before": "",
                        "after": "", "text": "",
                        "note": tr("缺少 path / version —— 这个文件没有留档")})
    try:
        from core.file_history import diff_for
        return jsonify(diff_for(session, path, version))
    except Exception as e:                               # noqa: BLE001
        return jsonify({"ok": False, "kind": "missing", "diff": "", "before": "",
                        "after": "", "text": "",
                        "note": trf("读取失败：{type(e).__name__}: {e}", type(e).__name__, e)})


@app.route("/api/file-history/prune", methods=["POST"])
def api_file_history_prune():
    """「立即清理」—— 按当前保留期与总空间上限各清一次。

    回收复用 `core/file_history.prune` → `core/retention.prune_generic`
    （不在这里另写一套"按时间删"）。
    读不到某一个值，只关掉那一半（与 `core/retention.py` 同一条方向：
    一个"删"的动作，读不到值时的真值不是"默认"，是"不动"）。
    不许早退 —— `if days is None: return …` 会让「保留期读不出来」
    顺手把空间上限那半也短路（而它们读的是两个键）。
    所以两半各报各的实情。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    try:
        from core.file_history import configured_days, configured_max_mb, prune
        days = configured_days()
        cap_mb = configured_max_mb()
        if days is None and cap_mb is None:
            return jsonify({"ok": False, "removed": 0, "days": None,
                            "max_mb": None,
                            "note": tr("保留期和空间上限读取失败，未删除任何文件")})
        # 传 `days=days`（可能是 None）：`prune` 自己按"一半一半"处理，
        #    不在这里替它决定（那正是上面那个早退 bug 的形状）。
        removed = prune(days=days)
        return jsonify({"ok": True, "removed": removed, "days": days,
                        "max_mb": cap_mb,
                        "note": None if days is not None
                        else "保留期读不出来 ⇒ 这一次只按空间上限削"})
    except Exception as e:                               # noqa: BLE001
        return jsonify({"ok": False, "removed": 0,
                        "note": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/health")
def api_health():
    """Return framework health + basic system metrics for the status bar."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    import time
    uptime = time.time() - _FRAMEWORK_START_TIME if _FRAMEWORK_START_TIME else 0

    # Memory usage — try psutil first, fall back to Win32 psapi.dll
    mem_mb = 0
    try:
        import psutil
        mem_mb = psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        try:
            import ctypes
            from ctypes import wintypes
            # Load psapi.dll explicitly — ctypes.windll.psapi may not be auto-loaded
            _psapi = ctypes.WinDLL("psapi.dll", use_last_error=True)
            _kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)

            class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            _GetProcessMemoryInfo = _psapi.GetProcessMemoryInfo
            _GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD,
            ]
            _GetProcessMemoryInfo.restype = wintypes.BOOL

            _kernel32.GetCurrentProcess.restype = wintypes.HANDLE

            mc = _PROCESS_MEMORY_COUNTERS_EX()
            mc.cb = ctypes.sizeof(mc)
            if _GetProcessMemoryInfo(
                _kernel32.GetCurrentProcess(),
                ctypes.byref(mc),
                ctypes.sizeof(mc),
            ):
                # WorkingSetSize = physical RAM; PrivateUsage = Task Manager "Private Working Set"
                mem_mb = mc.WorkingSetSize / (1024 * 1024)
        except Exception:
            pass

    # Tool count
    tool_count = 0
    if _FRAMEWORK_RUNNING:
        try:
            from bus.registry import list_tools
            tool_count = len(list_tools())
        except Exception:
            pass

    # Token usage
    token_stats = {}
    try:
        from providers.base import get_token_usage
        token_stats = get_token_usage()
    except Exception:
        pass

    # 投入档常驻可见
    #
    # 两个值分开报，因为它们真的会不一样（同一组标签不许打两个量纲）：
    #   · `effort_name`         = 设置里此刻写着的（`configured_level`，现读、不冻结）
    #   · `effort_session_name` = 本会话已经冻结的那个 = 这一段真正在跑的档
    # 改完设置而这一段还没生效时两者不同，状态条两个都得说（标签不许说谎）。
    #
    # 这里绝不能调 `current_level()` —— 它第一次被问就把档钉死，
    #   而状态条是每 5 秒轮一次本接口，一个读接口有了副作用：用户还没发过一句话，
    #   档就已经冻在"打开页面那一刻"的设置上了。所以走 `frozen_level()`（纯读）。
    effort_info = {}
    try:
        from core import tiers
        try:
            _lvl = tiers.configured_level()
            effort_info["effort_level"] = _lvl
            effort_info["effort_name"] = tiers.TIERS[_lvl].name
        except Exception as exc:          # 「读到了但做不到」，出声，不静默消失
            effort_info["effort_level"] = None
            effort_info["effort_name"] = None
            effort_info["effort_error"] = str(exc)
        _fl = tiers.frozen_level(session_key_for(_web_session()))
        effort_info["effort_session_name"] = (
            tiers.TIERS[_fl].name if _fl is not None else None)
    except Exception:
        pass
    # 模式轴：同一条规矩、同一个理由（读接口无副作用）
    mode_info = {}
    try:
        from core import modes
        try:
            mode_info["mode_name"] = modes.MODE_NAMES[modes.configured_mode()]
        except Exception as exc:
            mode_info["mode_name"] = None
            mode_info["mode_error"] = str(exc)
        _fm = modes.frozen_mode(session_key_for(_web_session()))
        mode_info["mode_session_name"] = (
            modes.MODE_NAMES.get(_fm) if _fm is not None else None)
    except Exception:
        pass

    # ICE：本会话的保护降级事实。
    #
    #   放这里，是因为本接口每 5 秒被轮一次（状态条），徽标自动保持正确。
    #   少了它，刷新页面徽标就没了，而用户会以为保护回来了 ——
    #      「『没有读数』不等于『可以清空』」的反面：
    #      这里清空等于说了一句假话。
    #   纯读（同 `frozen_level` 那条判据）：`downgraded()` 只查一个集合，
    #      不改任何东西 —— 一个每 5 秒轮一次的接口不许有副作用。
    try:
        from core.ice import reveal as _ice_reveal
        _ice_down = bool(_ice_reveal.downgraded(str(_web_session())))
    except Exception:  # noqa: BLE001
        _ice_down = False

    return jsonify({
        "framework_running": _FRAMEWORK_RUNNING,
        "ice_downgraded": _ice_down,
        "uptime_seconds": round(uptime),
        "memory_mb": round(mem_mb, 1),
        "tool_count": tool_count,
        "provider": _active_model_info()["provider"],
        "model": _active_model_info()["model"],
        "model_config_name": _active_model_info()["config_name"],
        "busy": _AGENT_BUSY,
        "task": _AGENT_TASK,
        "tokens_total": token_stats.get("total_tokens", 0),
        "tokens_prompt": token_stats.get("prompt_tokens", 0),
        "tokens_completion": token_stats.get("completion_tokens", 0),
        "api_calls": token_stats.get("api_calls", 0),
        # 缓存命中。 `cache_hit_rate` 是 None 直到有一次调用
        # 真报了缓存字段 —— None 与 0.0 意思不同（"没量到" vs "量到了没中"），
        # 前端必须照样区分，别用 `|| 0` 把它塌成 0。
        "cache_hit_tokens": token_stats.get("cache_hit_tokens", 0),
        "cache_reported_calls": token_stats.get("cache_reported_calls", 0),
        "cache_hit_rate": token_stats.get("cache_hit_rate"),
        # 成本。三格必须一起看：
        #   · `cost_usd` 是算得出来的那几次之和；
        #   · `cost_unpriced_calls` 是算不出来的次数（表里没这个模型）；
        #   · `cost_is_complete` 才是"这个数是不是全场的"（渲染层只许看它）。
        # 所以前端不许只显示 `cost_usd`：那是一句可对可错的话
        #   （读不到 ≠ 0，0 看起来像一个结论）。
        "cost_usd": token_stats.get("cost_usd", 0.0),
        "cost_priced_calls": token_stats.get("cost_priced_calls", 0),
        "cost_unpriced_calls": token_stats.get("cost_unpriced_calls", 0),
        "cost_is_complete": token_stats.get("cost_is_complete", False),
        "cost_table": token_stats.get("cost_table") or {},
        # 投入档（`effort_name` / `effort_session_name` / `effort_error`）
        **effort_info,
        # 模式轴（`mode_name` / `mode_session_name` / `mode_error`）
        **mode_info,
        "startup_seconds": _get_startup_seconds(),
    })


@app.route("/api/config", methods=["GET"])
def api_get_config():
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    config = _read_config()
    channels = _detect_channels(config)
    models_cfg = _read_models(config)
    return jsonify({
        "channel": config.get("channel", {}).get("default", "cli_channel"),
        # 这三格问的都是"现在用哪个"，走同一处判定
        #   （默认那条配置条目优先，没有才退回 config.yaml 的顶层 `model:`）。
        "provider": _active_model_info()["provider"],
        "model_name": _active_model_info()["model"],
        "model_config_name": _active_model_info()["config_name"],
        "channels": channels,
        "models": models_cfg,
    })


@app.route("/api/config", methods=["POST"])
def api_save_config():
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json() or {}
    section = data.get("section", "")
    # 这两支改道。
    #   它们改的是 `config.yaml` 的顶层 `model:`，而
    #   "用哪个模型"由默认那条配置条目说了算 —— `config.yaml` 只剩
    #   "没有配置条目时的兜底"这一个用途，老页面上那个「设为当前」会变成
    #   按了有回执、模型一个字节没变的假按钮（标签在说谎）。
    #   所以与其让它静默失效，不如说清改去哪了（拒要带路线）。
    if section in ("provider", "model_select"):
        try:
            from core import model_configs as _mc
            if _mc.default_id():
                return jsonify({
                    "ok": False,
                    "error": tr("模型在「端配置」页统一设置，由「默认启用」决定"),
                }), 409
        except Exception:                                       # noqa: BLE001
            pass
    try:
        if section == "channel":
            _save_channel_config(data)
        elif section == "model":
            _save_model_config(data)
        elif section == "provider":
            # Switch active provider + model
            provider = data.get("provider", "")
            model_name = data.get("model_name", "")
            if provider:
                _switch_provider(provider, model_name)
        elif section == "model_select":
            # Switch model within current provider
            provider = data.get("provider", "")
            model_name = data.get("model_name", "")
            if provider and model_name:
                _switch_model(provider, model_name)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _yaml_scalar(value) -> str:
    """把值写成 YAML 标量 —— 该加引号的加引号，其余不加（保持文件原样）。"""
    s = str(value)
    needs_quote = (
        s == ""
        or s != s.strip()
        or s[0] in "-?:,[]{}#&*!|>'\"%@`"
        or ": " in s
        or " #" in s
        or s.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~")
    )
    if needs_quote:
        return "'" + s.replace("'", "''") + "'"
    return s


def _replace_yaml_scalar(text: str, key: str, value, section: str = "") -> str:
    """在 YAML 文本里只改那一行，其余字节原样保留 —— 取代 `yaml.dump` 的整文件重写。

    ## 为什么

    `yaml.safe_load` → 改一个键 → `yaml.dump` 会抹掉文件里所有注释并重排键序。
    PyYAML 保不住注释，它没有那个能力。切一轮 provider，
    `config.yaml` 从 187 行 / 44 行注释变成 141 行 / 0 行注释 ——
    值一个没变，文档全没了。而「`rate_limit:` 必须显式写 0、
    不能删这个键」的道理正写在那些注释里。

    它不是"值写错了"，是"把文档删了" —— 所以任何只看值的检查都发现不了
    （`git diff` 里那一片红只有人眼看得见）。

    ## 形状

    与 `_update_config_default_channel`（`re.sub(r"default:\\s*\\S+", …)`）同族，
    只是加了 `section` 范围限定、避免打到同名的别的键。

    只改不增：找不到 `key` 就原样返回并吼一行，绝不静默改文件。
    """
    import re as _re

    lines = text.splitlines(keepends=True)
    lo, hi = 0, len(lines)

    if section:
        header = None
        for i, ln in enumerate(lines):
            body = ln.rstrip("\r\n")
            # 必须是顶层且恰好叫 section：`models:` 不能匹配 `model`
            if _re.match(r"^" + _re.escape(section) + r"\s*:\s*$", body):
                header = i
                break
        if header is None:
            print(f"[config] 找不到 `{section}:` 段 —— 文件未改（不猜）")
            return text
        lo = header + 1
        for j in range(lo, len(lines)):
            body = lines[j].rstrip("\r\n")
            if body and not body[0].isspace() and not body.lstrip().startswith("#"):
                hi = j
                break

    pat = _re.compile(r"^(\s+)" + _re.escape(key) + r"\s*:")
    for i in range(lo, hi):
        m = pat.match(lines[i])
        if m:
            eol = "\r\n" if lines[i].endswith("\r\n") else "\n"
            before = lines[i]
            lines[i] = f"{m.group(1)}{key}: {_yaml_scalar(value)}{eol}"
            if lines[i] != before:
                print(f"[config] {section + '.' if section else ''}{key} -> "
                      f"{_yaml_scalar(value)}（只改了这一行）")
            return "".join(lines)

    if not section:
        print(f"[config] 没有 `{key}:` 这一行且未给 section —— 文件未改（不猜）")
        return text
    eol = "\r\n" if "\r\n" in text else "\n"
    lines.insert(lo, f"  {key}: {_yaml_scalar(value)}{eol}")
    print(f"[config] {section}.{key} 不存在，已插入一行")
    return "".join(lines)


def _switch_provider(provider: str, model_name: str) -> None:
    """Update config.yaml: provider, model, base_url, api_key.

    行级写入，不是 `yaml.safe_load` + `yaml.dump` —— 后者会把注释全抹掉。
    """
    cfg_path = _paths.config_path()
    if not cfg_path.exists():
        raise FileNotFoundError("config.yaml not found")
    import yaml
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    updates = [("provider", provider)]
    if model_name:
        updates.append(("model_name", model_name))

    # Also update base_url and api_key from the new provider's manifest
    env_map = _build_env_map()
    prov_dir = _PROJECT_ROOT / "providers" / provider
    mf = prov_dir / "manifest.yaml"
    if mf.exists():
        with open(mf, encoding="utf-8") as f:
            pm = yaml.safe_load(f)
        new_base = pm.get("config", {}).get("base_url", "")
        if new_base:
            updates.append(("base_url", new_base))
        env_key = env_map.get(provider, "")
        if env_key:
            updates.append(("api_key", "${" + env_key + "}"))

    text = cfg_path.read_text(encoding="utf-8")
    for k, v in updates:
        text = _replace_yaml_scalar(text, k, v, section="model")
    cfg_path.write_text(text, encoding="utf-8")


def _switch_model(provider: str, model_name: str) -> None:
    """Update config.yaml model_name within the active provider.

    同上：行级写入。这里读 `config.yaml` 只是判断该不该动
    （写路径不走 `yaml.dump`）。

    已知边界：它只改 config.yaml、不重建 provider 实例
    所以真正生效要重启框架。
    """
    cfg_path = _paths.config_path()
    if not cfg_path.exists():
        return
    import yaml
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    current = cfg.get("model", {}).get("provider", "")
    if current != provider:
        return

    text = cfg_path.read_text(encoding="utf-8")
    new_text = _replace_yaml_scalar(text, "model_name", model_name, section="model")
    if new_text != text:
        cfg_path.write_text(new_text, encoding="utf-8")


# Channel management

_channel_threads: dict[str, threading.Thread] = {}
_channel_running: dict[str, bool] = {}
#: 微信登录线程自己的事件循环 —— 强制重扫要能在另一个线程里停掉它。
#:   没有这一格时，`channel.terminate()` 这个协程没有地方可投递
#:   所以旧轮询循环停不下来，重扫会变成两个线程抢同一个 bot。
#:   （只有微信那条路需要它：别的通道没有"重新登录"这件事。）
_channel_loops: dict[str, object] = {}


@app.route("/api/channel/start", methods=["POST"])
def api_channel_start():
    """Start an IM channel in a background thread.

    Multiple channels can coexist — each has its own polling loop and
    feeds events to the shared PipelineScheduler which processes them
    sequentially.  Session isolation is handled by SessionPool keyed
    on platform+user.
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    if not _FRAMEWORK_RUNNING:
        return jsonify({"error": tr("请先在 CLI 聊天页启动框架")}), 400

    data = request.get_json() or {}
    channel_id = data.get("channel_id", "")
    if not channel_id:
        return jsonify({"error": "missing channel_id"}), 400

    if _channel_running.get(channel_id):
        return jsonify({"ok": True, "status": "already_running"})

    # Log if another channel is already active (multi-channel is supported)
    other_channels = [c for c in _channel_running if _channel_running[c] and c != channel_id]
    if other_channels:
        _log(f"[web] Starting {channel_id} alongside already-running: {', '.join(other_channels)}")

    try:
        from bus.registry import get_channel
        channel = get_channel(channel_id)
        if not channel:
            return jsonify({"error": trf("通道 '{channel_id}' 未找到", channel_id)}), 404

        #: 微信不走这条通用启动路：`WeChatChannel.run()` 在
        #:   没有缓存凭据时会阻塞在 `do_login()` 的扫码上，而这条路上没有
        #:   扫码回调，线程挂在一个谁也看不见的二维码上，界面只会一直"启动中"。
        #:   所以没凭据就先拒，把用户送回扫码卡片（有凭据时 `run()` 自己 load，
        #:      进来就能长轮询）。
        if channel_id == "wechat_channel":
            _cred = Path(str(getattr(channel, "_credential_path",
                                     "data/wechat_credentials.json")))
            if not _cred.exists():
                return jsonify({"error": tr("微信还没登录，请先用二维码扫码登录")}), 400

        # Inject scheduler/pool (same as main.py does)
        channel._scheduler = _scheduler
        channel._pool = _session_pool

        def _run_channel():
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            #: 登记循环 —— 「重新登录」要从 Flask 线程把 `terminate()` 投递进来
            #:   （`_channel_loops`）。不登记时那一格是 `None`，覆盖重连直接报
            #:   「无法安全停止，请重启应用后再试」，而重启后自动启动又把它拉起，死循环。
            _channel_loops[channel_id] = loop
            try:
                _channel_running[channel_id] = True
                _log(f"[channel] Starting {channel_id}...")
                loop.run_until_complete(channel.run())
            except Exception as e:
                _log(f"[channel] {channel_id} error: {e}")
            finally:
                _channel_running[channel_id] = False
                if _channel_loops.get(channel_id) is loop:
                    _channel_loops.pop(channel_id, None)
                loop.close()

        t = threading.Thread(target=_run_channel, daemon=True, name=f"ch-{channel_id}")
        t.start()
        _channel_threads[channel_id] = t
        _log(f"[web] Channel {channel_id} started in background")
        return jsonify({"ok": True, "status": "started"})

    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


def _stop_channel(channel_id: str, *, wait_s: float = 5.0,
                  hard_wait_s: float = 3.0) -> dict:
    """停一条通道 —— 卡死的循环也要停得住。

    ## 为什么需要它

    Telegram 的轮询循环停了，而 `/api/channel/status` 仍然报 `running: true`
    （那个 flag 只在 `_run_channel` 的 `finally` 里才翻）。界面上没有停止按钮
    所以用户唯一的出路是把整个应用掐掉。

    ## 两段

    ① 温和：把 `terminate()` 投递到那条通道自己的事件循环
       （`_channel_loops` 现成有），等它自己退；
    ② 硬停：等不到就 `loop.call_soon_threadsafe(loop.stop)` ——
       `run_until_complete` 会抛 `RuntimeError`，`finally` 照常翻 flag。
       卡在 await 里的循环只有这一步救得了。

    硬停也救不回来时如实说 `stuck`，并且不动那个 flag ——
      假装停了会让下一次 `start` 起第二条线程抢同一个 bot（Telegram 会 409）。
    """
    import asyncio
    import time as _time

    if not _channel_running.get(channel_id):
        return {"ok": True, "status": "not_running", "channel_id": channel_id}

    loop = _channel_loops.get(channel_id)
    channel = None
    try:
        from bus.registry import get_channel
        channel = get_channel(channel_id)
    except Exception as exc:                                     # noqa: BLE001
        _log(f"[channel] {channel_id} 取通道实例失败（{type(exc).__name__}: {exc}）")

    # ① 温和
    if loop is not None and channel is not None:
        async def _terminate() -> None:
            try:
                await channel.terminate()
            except Exception as exc:                             # noqa: BLE001
                print(f"[channel] {channel_id} terminate 抛了"
                      f"（{type(exc).__name__}: {exc}）")
        try:
            asyncio.run_coroutine_threadsafe(_terminate(), loop)
        except Exception as exc:                                 # noqa: BLE001
            _log(f"[channel] {channel_id} 温和停止投递失败"
                 f"（{type(exc).__name__}: {exc}）")

    deadline = _time.time() + wait_s
    while _time.time() < deadline:
        if not _channel_running.get(channel_id):
            _log(f"[channel] {channel_id} 已停止（温和）")
            return {"ok": True, "status": "stopped", "channel_id": channel_id}
        _time.sleep(0.1)

    # ② 硬停
    if loop is not None:
        try:
            loop.call_soon_threadsafe(loop.stop)
            _log(f"[channel] {channel_id} 温和停不下来，已硬停事件循环")
        except Exception as exc:                                 # noqa: BLE001
            _log(f"[channel] {channel_id} 硬停失败（{type(exc).__name__}: {exc}）")

    deadline = _time.time() + hard_wait_s
    while _time.time() < deadline:
        if not _channel_running.get(channel_id):
            _log(f"[channel] {channel_id} 已停止（硬停）")
            return {"ok": True, "status": "forced", "channel_id": channel_id}
        _time.sleep(0.1)

    _log(f"[channel] {channel_id} 停不下来（线程没有退出）—— 需要重启应用")
    return {"ok": False, "status": "stuck", "channel_id": channel_id,
            "error": tr("这条通道的线程没有退出，停不下来；请重启应用。")}


@app.route("/api/channel/stop", methods=["POST"])
def api_channel_stop():
    """停一条通道（见 `_stop_channel` 的两段与那条"卡死"的现场）。"""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    channel_id = str(data.get("channel_id") or "").strip()
    if not channel_id:
        return jsonify({"error": "missing channel_id"}), 400
    return jsonify(_stop_channel(channel_id))


@app.route("/api/channel/status")
def api_channel_status():
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    running_map = dict(_channel_running)
    #: 「在跑」与「真的在收」必须分得开（Telegram：循环停了，
    #:   而这里只报 `true`）。`last_event` 是进过管线的时间，不是心跳 ——
    #:   一个健康但没人说话的通道也会"安静"，读数要跟未取队列一起看。
    try:
        from core import channel_health as _chh
        last = _chh.snapshot()
    except Exception:                                            # noqa: BLE001
        last = {}
    #: 上次启动失败的原因，来自事故账本里 kind == "channel" 那几条。
    #:   通道层失败时只 print 到终端，而桌面壳里没有终端，卡片只会回到「已配置」，
    #:   用户不知道是 token、代理还是网络。
    #:   真相仍只有一处（账本），这里只做一份投影，成功启动时通道自己 resolve()。
    try:
        from core import incidents as _inc
        errors = {
            it.get("component", ""): {
                "error": it.get("error", ""),
                "errorTpl": it.get("errorTpl", ""),
                "errorVars": it.get("errorVars", {}),
            }
            for it in _inc.all() if it.get("kind") == "channel"
        }
    except Exception:                                            # noqa: BLE001
        errors = {}
    return jsonify({
        "running": running_map,
        "running_count": sum(1 for v in running_map.values() if v),
        "last_event": last,
        "errors": errors,
    })


def _channel_entry_error(exc) -> tuple:
    """把配置条目那层的拒绝翻成 HTTP。消息原样给用户看（它带路线）。"""
    return jsonify({"error": str(exc)}), 400


@app.route("/api/channel/entry/create", methods=["POST"])
def api_channel_entry_create():
    """给一个通道新建一条命名凭据。"""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core import channel_configs as _cc

    data = request.get_json() or {}
    platform = _channel_platform(data.get("channel_id", ""))
    if platform not in _cc.PLATFORMS:
        return jsonify({"error": "这个通道没有凭据表单"}), 400
    try:
        row = _cc.create(platform, data.get("name", ""), data.get("fields") or {})
    except _cc.ChannelConfigError as exc:
        return _channel_entry_error(exc)
    return jsonify({"ok": True, "entry": row})


@app.route("/api/channel/entry/update", methods=["POST"])
def api_channel_entry_update():
    """改一条：名字 / 凭据各改各的，没给的那半不动。"""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core import channel_configs as _cc

    data = request.get_json() or {}
    cid = str(data.get("id") or "")
    if not cid:
        return jsonify({"error": "missing id"}), 400
    name = data.get("name") if "name" in data else None
    try:
        row = _cc.update(cid, name=name, fields=data.get("fields") or None)
    except _cc.ChannelConfigError as exc:
        return _channel_entry_error(exc)
    return jsonify({"ok": True, "entry": row})


@app.route("/api/channel/entry/delete", methods=["POST"])
def api_channel_entry_delete():
    """删一条。默认那条会被拒，并说明先做什么。"""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core import channel_configs as _cc

    data = request.get_json() or {}
    cid = str(data.get("id") or "")
    if not cid:
        return jsonify({"error": "missing id"}), 400
    try:
        ok = _cc.delete(cid)
    except _cc.ChannelConfigError as exc:
        return _channel_entry_error(exc)
    return jsonify({"ok": bool(ok)})


@app.route("/api/channel/entry/default", methods=["POST"])
def api_channel_entry_default():
    """把某一条设为该通道的默认。运行中的通道要重启才换过去。"""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core import channel_configs as _cc

    data = request.get_json() or {}
    platform = _channel_platform(data.get("channel_id", ""))
    cid = str(data.get("id") or "")
    if platform not in _cc.PLATFORMS or not cid:
        return jsonify({"error": "参数不全"}), 400
    try:
        _cc.set_default(platform, cid)
    except _cc.ChannelConfigError as exc:
        return _channel_entry_error(exc)
    return jsonify({"ok": True, "default": cid})


@app.route("/api/channel/set-default", methods=["POST"])
def api_channel_set_default():
    """Update config.yaml channel.default — called when user clicks a
    platform chip in the dashboard.  This is separate from saving channel
    credentials (_save_channel_config) so that configuring a channel does
    not accidentally change the default."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json() or {}
    channel_id = data.get("channel_id", "")
    if not channel_id:
        return jsonify({"error": "missing channel_id"}), 400
    try:
        _update_config_default_channel(channel_id)
        return jsonify({"ok": True, "default": channel_id})
    except Exception as e:
        _log("[web] 设置默认通道失败：%r" % (e,))
        return jsonify({"error": _error_brief(
            e, "设置默认通道", "config.yaml")}), 500


WEB_SESSION_ID = "web:cli:default"


def _web_session(group: str = ""):
    """Web 通道的会话对象 —— 唯一的构造点。

    存在的理由是一个真实事故：`/api/stop` 若自己写一个
    字面量 `"web:cli:default"` 当会话键，而调度器与 agent 循环用的是
    `str(event.session)` = `"web:private:web:cli:default"`（`MessageSession.__str__`
    是 `platform:type:session_id`）。标志置在没人读的键上：那一轮照跑满 30 轮，
    而接口回 `{"ok": true}` 谎报成功（「说一套做一套」）。

    所以会话对象只能从这里构造，键只能由 `str()` 得出 —— 谁都不许再写字面量。

    多了一个 *group*（群 id）。
      给了它，这条会话是那个群的会话，键是 workspace:group:<群 id>
      —— 与 `core.groups.session_key_of(gid)` 逐字同形（那是"我们铸的群是哪条会话"，
      这里是"这条 Web 会话属于哪个群"，同一个口径的两面）。
      仍然只有这一个构造点 —— 别在 `/api/chat` 里另外拼一个群会话。
    """
    from channels.base import MessageSession, MessageType

    gid = str(group or "").strip()
    if gid:
        return MessageSession(platform="web", message_type=MessageType.GROUP,
                              session_id=gid)
    return MessageSession(
        platform="web", message_type=MessageType.PRIVATE,
        session_id=WEB_SESSION_ID,
    )


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """中断正在跑的那一轮（刹车 1）。

    Web 是唯一能真打断一轮的入口，因为它是并发的（waitress threads=12）：
    这个请求在另一个线程里跑，置上标志，agent 循环下一次迭代就会看到。

    IM 通道做不到：整轮管线是同步无 await 的
    （`PreProcessStage`/`PostProcessStage` 零 `await`，`AgentLoopStage` 调的是同步
    `agent_loop`），所以单事件循环里第二个消息进不来 —— 那边的 `/stop` 只能等这一轮
    跑完才被处理。终端 CLI 同理（输入被阻塞）。

    也只保证"下一次调用模型之前停下" —— `provider.chat()` 不可打断，
    详见 `core/cancellation.py`。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403

    from core.cancellation import request_cancel

    # 键必须与 /api/chat 那次事件的推导完全一致 —— 同一个 session_key_for()
    #: 群里那一轮也得停得下来 —— 所以 `/api/stop`
    #:   收同一个 `?group=`（不给 = 私聊那条，今天的行为一个字节不变）。
    #:   两处必须从同一个 `_web_session(group)` 推键 —— 键错一位的症状是
    #:   "按了打断、那一轮照跑"，而这正是 `_web_session` docstring 里那个旧事故。
    session_key = session_key_for(_web_session(request.args.get("group") or ""))
    fresh = request_cancel(session_key)
    #: 多报一格"收没收"（`fresh=False` = 这一会话本来就有一条
    #: 还没被读到的打断）—— 对应"按一次、出两条「已打断」"这种尚未复现的症状，
    #: 这一格是留给下一次复现时观测的仪器（先观测、再定论）。
    return jsonify({
        "ok": True,
        "requested": fresh,
        "reason": "" if fresh else "already_requested",
        "session_key": session_key,
        "note": ("已请求中断" if fresh else "那一轮已经在中断中了"),
    })


@app.route("/api/steer", methods=["POST"])
def api_steer():
    """跑着的时候补的那句话 —— 排进会话收件箱，下一步的边界注入。

    与 `/api/stop` 同一类侧信道：不拿会话锁（那一轮正持有它），
    只往 `core/steer.py` 的收件箱里放一条，然后由 agent 循环在迭代开头
    （上一次工具批全部回填之后、下一次请求之前）取走。

    四条拒绝，一条都不许省（每一条都是一种"插进去就出事"）：
      · `command` —— `/` 开头：命令在 `pre_process` 解析，注入进循环里没人执行；
      · `awaiting_approval` —— 有待审批：那时用户的话就是审批回答，
        注入进循环等于把那句话吞掉；
      · `attachments` —— 带图/附件：注入侧要重走 `core/image_input` 的块拼装，
        v1 不做（形状错了 = 整轮 400），让前端回落成排队；
      · `idle` —— 没有一轮在跑：没有"下一个边界"，前端走普通 `/api/chat`。

    accepted 不等于"已经进上下文" —— 真正的时刻由 `steer.delivered`
      事件说（带着前端给的 `client_msg_id`）；没赶上就落到 `task.end.steer_leftover`。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    if _scheduler is None:
        return jsonify({"accepted": False, "reason": "not_running"}), 503

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:                                    # noqa: BLE001
        data = {}
    client_id = str(data.get("client_msg_id") or "").strip()
    #: 撤单：前端在轮末兜底接管了这一条（用同一条气泡接着跑一轮），
    #:   先把它从收件箱摘掉，否则下一轮的边界会把它再注入一次
    #:   （同一个问题被回答两遍，而第二条用户气泡根本不存在）。
    #:   它排在"空文本"那道检查之前 —— 撤单本来就没有正文。
    if data.get("cancel"):
        from core import steer as _steer0
        key0 = session_key_for(_web_session(request.args.get("group") or ""))
        n = _steer0.drop(key0, client_id)
        return jsonify({"cancelled": n, "session_key": key0})

    text = str(data.get("text") or "").strip()
    if not text:
        return jsonify({"accepted": False, "reason": "empty"}), 400
    if data.get("images"):
        return jsonify({"accepted": False, "reason": "attachments"})

    # 键与 `/api/chat` / `/api/stop` 同一处推导（`_web_session`）——
    #   自己拼一个字面量就会注入到没人读的键上（`_web_session` docstring 的旧事故）。
    session_key = session_key_for(_web_session(request.args.get("group") or ""))
    if text.startswith("/"):
        return jsonify({"accepted": False, "reason": "command",
                        "session_key": session_key})
    try:
        if _scheduler.pending_approval(session_key) is not None:
            return jsonify({"accepted": False, "reason": "awaiting_approval",
                            "session_key": session_key})
    except Exception:                                    # noqa: BLE001
        pass
    if not _scheduler.is_busy(session_key):
        return jsonify({"accepted": False, "reason": "idle",
                        "session_key": session_key})

    from core import steer as _steer
    _steer.push(session_key, {"id": client_id, "text": text})
    return jsonify({"accepted": True, "reason": "", "session_key": session_key,
                    "pending": _steer.pending(session_key)})


@app.route("/api/background", methods=["POST"])
def api_background():
    """用户 Ctrl+B：把这条会话正在前台跑的任务转到后台。

    转不转得动由 `core/jobs.py` 说了算（`request_background` 只给
      真的登记过的前台执行体置事件）—— 界面不许自己猜。

    `count == 0`，`ok: false`（"没有可转的任务"不是成功）——
      假装成功的话，用户下次按 Ctrl+B 会以为它坏了。
    键仍由 `_web_session(group)` 推导（同 `/api/stop` 那条；
      写一个字面量就会"置在没人读的键上"，而接口回 ok —— 那个旧事故的 §注）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403

    from core import jobs

    session_key = session_key_for(_web_session(request.args.get("group") or ""))
    n = jobs.request_background(session_key)
    return jsonify({
        "ok": n > 0,
        "count": n,
        "session_key": session_key,
        "note": ("已把 %d 个任务转到后台" % n) if n
                else "现在没有可转的前台任务",
    })


@app.route("/api/tasks")
def api_tasks():
    """本会话正在跑的任务（顶栏两格 + 两个浮窗的数据源）。

    只读、现拉：真值在 `core/jobs.py` 的表里，事件只是"叫你来读"的信号。
    与 `/api/context`、`/api/stop` 同一个会话键推导 —— 不给 `?group=` 就是私聊那条。
    只回 `running` 的：已结束的留在表里是为了 `task(action="output")`，
      界面上那两格是状态（有才显示），不是历史列表。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403

    from core import jobs

    key = session_key_for(_web_session(request.args.get("group") or ""))
    running = jobs.running_for(key)
    bg = [t for t in running if t["kind"] == jobs.KIND_COMMAND]
    subs = [t for t in running if t["kind"] == jobs.KIND_SUBAGENT]
    # 技能的后台任务单独一组，不与命令或子 agent 合并。
    skills = [t for t in running if t["kind"] == jobs.KIND_SKILL]
    return jsonify({
        "ok": True,
        "session": key,
        "tasks": running,
        "background": bg,
        "subagents": subs,
        "skills": skills,
        "counts": {"background": len(bg), "subagents": len(subs),
                   "skills": len(skills)},
        # 服务器侧的同口径读数（排障用；界面那行提示走 `tool.start`）。
        "foreground": jobs.foreground_count(key),
    })


#: `/api/chat` 请求体的上限。见 `api_chat` 里那段理由：
#: 它管的是"在解析之前就拒绝"，逐张 / 逐轮的限额在下游（`core/image_input.py`）。
#:
#: 跟着下游那一组数一起对齐：
#: = 下游的每轮合计 40 MiB 走 base64 之后（×4/3 ≈ 53.3 MB）+ JSON 余量。
#: 所以前端放行的任何一轮，这里一定放行；这条闸只挡"不是这一轮该有的体量"。
#: 它仍然不替代下游：逐张（5 MB base64 / 20 MiB 源）与逐轮（40 MiB）的
#: 判据都在 `core/image_input.py` 一处。
_CHAT_BODY_MAX_BYTES = 56 * 1024 * 1024


def _user_visible(text: str) -> str:
    """显示侧：把框架拼在用户消息尾巴上的附注截掉。

    模型那条路不经过这里 —— `messages` / `load_session` 必须看到全文，
      否则它下一轮不知道自己少看了一张图。这里只服务两个给人看的出口：
      `GET /api/messages`（气泡）与 `GET /api/threads` 的标题
      （能力闸那句说明曾同时泄漏进对话框和会话标题）。
    """
    try:
        from core.image_input import strip_notes
        return strip_notes(text)
    except Exception:                                            # noqa: BLE001
        return text


#: 附件标记的几种形状 —— 与产者同源：
#: 图片 / 音频指针来自 `core/image_input.content_text`，
#: 文件那一行来自 `Composer.attachmentPreamble`（编号可选 —— 更早产出的
#: 消息是 `[附件]`，现在是 `[附件N]`；后跟一行"这个文件以…"的说明）。
#: 内联文本的正文夹在一对 `[附件正文#id 开始]` / `[附件正文#id 结束]` 之间，
#: 整段都是给模型看的，标题只认用户自己说的话。
_ATTACH_IMAGE_LINE = re.compile(r"^\[图片\s*\d+(?:\s*——\s*原图在\s*[^\]]*)?\]$")
_ATTACH_AUDIO_LINE = re.compile(r"^\[音频\s*\d+(?:\s*——\s*原音频在\s*[^\]]*)?\]$")
_ATTACH_FILE_LINE = re.compile(r"^\[附件\d*\]\s")
_ATTACH_FILE_NOTE = "这个文件以"
_ATTACH_BODY_OPEN = re.compile(r"^\[附件正文#\S+ 开始\]$")
_ATTACH_BODY_CLOSE = re.compile(r"^\[附件正文#\S+ 结束\]$")

#: 标题截断长度 —— 与 `first_user_text` 的默认值同一个数。
_TITLE_LIMIT = 40


def _thread_title(raw: str) -> str:
    """会话标题：只留真正的正文；一条只有附件的消息落成 `【图片】` / `【文件】`。

    判据必须在截断之前做：附件指针那一行本身就有 45+ 个字符，
      先截再判的话它会被切成半截（`[图片 1 —— 原图在 data/atta…`），
      既认不出来、又把内部路径当成标题露给用户。
    认不出的形状，退回"折叠空白后截断"，不猜（与 `preview` 的契约一致）。
    """
    text = _user_visible(raw or "")
    if not text.strip():
        return ""
    has_image = has_audio = has_file = False
    keep: list[str] = []
    skip_note = False
    in_body = False
    for line in text.splitlines():
        s = line.strip()
        if in_body:
            if _ATTACH_BODY_CLOSE.match(s):
                in_body = False
            continue
        if _ATTACH_BODY_OPEN.match(s):
            #: 内联文本的正文整段是给模型看的（标题里出现一屏粘贴物没有意义）。
            in_body = True
            skip_note = False
            continue
        if _ATTACH_BODY_CLOSE.match(s):
            continue
        if _ATTACH_IMAGE_LINE.match(s):
            has_image = True
            skip_note = False
            continue
        if _ATTACH_AUDIO_LINE.match(s):
            has_audio = True
            skip_note = False
            continue
        if _ATTACH_FILE_LINE.match(s):
            has_file = True
            skip_note = True
            continue
        if skip_note and s.startswith(_ATTACH_FILE_NOTE):
            skip_note = False
            continue
        skip_note = False
        keep.append(line)
    body = " ".join(" ".join(keep).split())
    if body:
        return body[:_TITLE_LIMIT] + ("…" if len(body) > _TITLE_LIMIT else "")
    tags = []
    if has_image:
        tags.append(tr("【图片】"))
    if has_audio:
        tags.append(tr("【音频】"))
    if has_file:
        tags.append(tr("【文件】"))
    if tags:
        return "".join(tags)
    return " ".join(text.split())[:_TITLE_LIMIT]


def _thread_title_of(st, storage_id: str) -> str:
    """名册里那一格 —— 拿全文再判（`limit=None`），判完才截断。"""
    try:
        return _thread_title(st.first_user_text(storage_id, limit=None))
    except Exception:                                            # noqa: BLE001
        return ""


def _image_components(images) -> list:
    """`images` 请求字段 → `Image` 组件列表。

    收三种形状：
       · `{"data_url": "data:..."}` —— 文档里的契约（下面 docstring 写着它）；
       · `{"data": "<b64>", "media_type": ...}`；
       · 裸字符串 —— 桌面端 `Composer.jsx` 一直发的就是它
         （`images.map((i) => i.url)`）。
    少认一种的代价是每一张粘贴的图都被静默丢掉：
       用户贴截图问"帮我看看"，模型只收到文字，转头去翻工作区，
       翻到一张旧图就着它点评了一通 —— 用户以为「模型没看见」。
    认不出的条目出声，不静默：图没发出去必须留下痕迹。
      `prepare_*` 认得 `data:` 头的 URL，所以 URL 那支用 `url=`；
        裸 base64 那支才靠 `base64=` + `mime_type`（魔数判断不在这里做）。
    """
    from channels.message import Image

    out: list = []
    for item in (images or ()):
        if isinstance(item, str):
            item = {"data_url": item}
        if not isinstance(item, dict):
            print("[web] images 里有一个认不出的条目（%s），已跳过"
                  % type(item).__name__)
            continue
        url = str(item.get("data_url") or "")
        if url:
            out.append(Image(url=url))
        elif item.get("data"):
            out.append(Image(base64=str(item["data"]),
                             mime_type=str(item.get("media_type")
                                           or "image/png")))
        else:
            print("[web] images 条目既没有 data_url 也没有 data，已跳过")
    return out


# ---------------------------------------------------------------------------
# Web 侧的常驻事件循环池
#
# 为什么不能每请求 `new_event_loop()` + `run_until_complete`：审批倒计时
#   （`core/approval_timeout.arm`）挂在当时正在跑的那个 loop 上
#   （`scheduler._sync_approval_timer` 读 `asyncio.get_running_loop()`）。
#   请求一结束那个 loop 就停了 -> `call_later` 永远不响，`auto` /
#   `full_access` 档的挂起会一直等下去。IM 侧没这个问题：`core/channel_turns`
#   的 worker 循环是常驻的。
#
# 为什么是池而不是一条循环：`execute()` 里几乎没有 `await`（管线五个阶段
#   都是同步工作），一条循环同一时刻只能跑一轮 —— 一条就是"所有 Web 会话排队"。
#   waitress 是 `threads=12`，池子同宽；池满时请求线程等一条回来（与等一个
#   waitress 线程等价）。
#
# 循环从不关闭：daemon 线程，进程退出即散。`/clear` 也不碰它们
#   （碰了就把挂在上面的审批倒计时一起掐了）。

_AGENT_LOOP_POOL_SIZE = 12
_agent_loops_free: "_Queue[asyncio.AbstractEventLoop]" = _Queue()
_agent_loops_lock = threading.Lock()
_agent_loops_made = 0


def _spawn_agent_loop() -> asyncio.AbstractEventLoop:
    """起一条常驻循环（线程 + `run_forever`），返回它。"""
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run_forever() -> None:
        asyncio.set_event_loop(loop)
        #: 用 `call_soon` 放行，而不是"线程一起来就 set" —— 后者会在循环
        #:   真正开始处理之前就放行（`core/async_loop.py` 同一条理由）。
        loop.call_soon(ready.set)
        loop.run_forever()

    threading.Thread(target=_run_forever, name="web-agent-loop", daemon=True).start()
    if not ready.wait(10.0):
        loop.close()
        raise RuntimeError("Web 的常驻事件循环没能在 10s 内启动")
    return loop


def _acquire_agent_loop() -> asyncio.AbstractEventLoop:
    """借一条常驻循环（请求线程用）。用完必须 `_release_agent_loop` 还回去。"""
    global _agent_loops_made
    try:
        return _agent_loops_free.get_nowait()
    except _QueueEmpty:
        pass
    with _agent_loops_lock:
        if _agent_loops_made < _AGENT_LOOP_POOL_SIZE:
            _agent_loops_made += 1
            return _spawn_agent_loop()
    #: 池满：等一条回来。等不到的只可能是"12 轮同时卡住"，那与 waitress
    #:   的 12 条线程打满是同一件事。
    return _agent_loops_free.get()


def _release_agent_loop(loop: asyncio.AbstractEventLoop) -> None:
    if loop is not None and not loop.is_closed():
        _agent_loops_free.put(loop)


def _run_chat_turn_full(user_text: str, images: list | None = None,
                        segment: dict | None = None, group: str = "",
                        workspace: str = ""):
    """跑一轮对话，返回 `{"reply", "group_replies"}`。

    无主理人群聊的成员回复不是一句 `reply`，
      而是几条独立的可见消息（`group_replies`）。`reply` 那个口留给
      私聊 / 有主理人的群 / 审批重放 —— 行为一个字节不变。
      需要旧形状的调用方用下面的 `_run_chat_turn` 薄壳。

    抽出来是为了让浮窗的结构化回传复用同一条管线：
    `POST /api/approval` 把 `choice`/`text` 变成一行文本喂进这里，
    管线里仍然只有一个解析器（`pre_process._dispatch_approval`）。
    各写一份 = 两套语义，而"两套语义"正是这次重做要消灭的东西。

    *images*：`[{"data_url": "data:image/png;base64,…"}]`
    或 `[{"data": "<base64>", "media_type": "image/png"}]`。

    *segment*：这一轮该在哪一段对话里跑。
      · `{"new": True}`，开一段新的
      · `{"thread": "<存储 id>"}`，接着那一段
      · `None`，追加到当前绑定的那段（今天的行为）
    它是一个"轮的属性"，不是一次单独的对话轮 —— 所以这里只把它挂到
      `event.extras` 上（`MessageEvent.extras` 本来就是"阶段间扩展格"，
      调度器自己往它写 `response`），真正的动作在
      `pre_process.process()` 里、加载会话之前执行。
      所以今天的形状是前端先发一条真的 `/clear` 聊天轮、再发用户那句话：
        两次往返，而且那条 `/clear` 轮会照常进事件流（开一个活动块、
        把回执照落成一个气泡）。一个轮一个字段，这些杂散产物就都没有了。

    本函数一个字节都不解码 —— 它只把图装进 `MessageChain`，
    真正的校验/转码/限额在 `core/image_input.py`（唯一一处）。
    在这里先解析一遍 = 第二个判定点，而两个判定点会漂。
    """
    from channels.base import (
        MessageEvent, MessageType, Message as ChannelMessage, ChannelMetadata,
    )
    from channels.message import MessageChain, Plain

    # 由 _web_session() 构造，键由 str() 得出 —— 不许再写字面量。
    # 直接写 `session_key = "web:cli:default"` 是错的（那其实是 session_id，
    # 真正的键是 "web:private:web:cli:default"）—— 若 `/api/stop` 照抄这个名字，
    # 就会置一个没人读的标志。见 _web_session() 的 docstring。
    #: 给了 *group*，这一轮跑在那个群的会话里。
    #:   `message_type` 也必须跟着换 —— 会话键由它推（`session_key_for`），
    #:   两处不一致的后果是"消息落进了 A 会话、模型读的是 B 会话"。
    session = _web_session(group)
    comps: list = [Plain(text=user_text)]
    # 形状解析放在 `_image_components`（一处，可单测）——
    #   内联循环只认 dict 的话，桌面端发的裸字符串会让每张图都被静默丢掉。
    comps.extend(_image_components(images))
    chain = MessageChain(comps)
    msg = ChannelMessage(
        sender_id="web_user",
        message_chain=chain,
        sender_name="User",
        message_type=(MessageType.GROUP if str(group or "").strip()
                      else MessageType.PRIVATE),
        session=session,
    )
    event = MessageEvent(
        session=session,
        message=msg,
        platform_meta=ChannelMetadata(
            name="web_channel",
            description="Web UI CLI chat",
            platform_type="web",
        ),
        # 这一轮该在哪一段对话里跑（见上面 *segment* 那段）。
        #   不给就是空 dict，`pre_process` 那侧按 `.get(...)` 读，
        #   与今天逐字节同形（这是有意的：这条特性默认不改变任何老路径）。
        # *workspace* 走同一格（同一个理由：它是"这一轮的
        #   属性"，而且 `pre_process` 要拿它去写刚铸出来的那个存储 id）。
        extras=({**({"segment": segment} if segment else {}),
                 **({"workspace": workspace} if workspace else {})}),
    )

    async def _run():
        return await _scheduler.execute(event)

    loop = _acquire_agent_loop()
    try:
        reply = asyncio.run_coroutine_threadsafe(_run(), loop).result()
    finally:
        _release_agent_loop(loop)
    # 调度器把成员回复写进 `event.extras`（不是 `ctx.response`）。
    #   没有它，`None`（私聊那条路一个字节不变）。
    group_replies = (event.extras or {}).get("group_replies") or None
    # 这一轮产出的产物（图/音/视频）。
    #   与 `group_replies` 同一条来源（调度器写、这里读）—— 没有就是 `None`。
    artifacts = (event.extras or {}).get("artifacts") or None
    #: 这一轮停在等审批上（模型挂起、没话说）。前端据此不画空气泡。
    awaiting = bool((event.extras or {}).get("awaiting_approval"))
    #: 这一轮是用户打断收尾的，`reply` 是框架的回执，不是模型说的话。
    #:   前端据此给它一个回执身份（发下一句时撤掉、不抢"最后一条消息"）。
    cancelled = bool((event.extras or {}).get("cancelled"))
    #: 这一轮没赶上的 steer：`[{id, text}]` —— 前端用
    #:   同一条气泡接着跑一轮（不许丢、不许留给下一轮）。空列表是事实。
    steer_leftover = list((event.extras or {}).get("steer_leftover") or [])
    return {"reply": reply, "group_replies": group_replies,
            "artifacts": artifacts, "awaiting_approval": awaiting,
            "cancelled": cancelled, "steer_leftover": steer_leftover}


def _chat_reply_text(reply: str, group_replies, awaiting: bool) -> str:
    """这一轮该显示哪句话。

    空回复有两种，不许同形：
    - 挂起等审批，留空（面板本身就是信号，模型没哑）
    - 真的什么都没说，`(无回复)`（让"它没答"看得见）
    """
    if reply:
        return reply
    if group_replies or awaiting:
        return ""
    return "(无回复)"


def _run_chat_turn(user_text: str, images: list | None = None,
                   segment: dict | None = None, group: str = "",
                   workspace: str = ""):
    """旧形状（只回一句 `reply`）—— `/api/approval` 与既有调用方用。"""
    return _run_chat_turn_full(
        user_text, images=images, segment=segment,
        group=group, workspace=workspace,
    )["reply"]


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """CLI-mode chat: send a message through the real Agent Loop."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    if not _FRAMEWORK_RUNNING:
        return jsonify({"error": tr("框架未启动。请先在端配置页启动 Agent。")}), 503

    # 请求体先过一道闸。
    #
    # 图片是 base64 进 JSON 的 —— 一张 4 MB 的图在 body 里是 ~5.3 MB。
    # Flask 默认不限长，一个手滑（或一次误拖整个文件夹）会先把几百 MB
    # 读进内存，然后才轮到 `image_input` 那些限额说话。
    # 这道闸不替代 `MAX_SOURCE_BYTES` / `MAX_IMAGES_PER_TURN`
    #    （那是逐张、逐轮的判据，在下游）；它管的是"在解析之前就拒绝"。
    # 给的是一个余量很大的数：4 张 4 MB 的图 ≈ 21 MB body。
    if (request.content_length or 0) > _CHAT_BODY_MAX_BYTES:
        return jsonify({"error": "这一轮的图片太大了（请求体 %d 字节，上限 %d）—— "
                                 "请少发几张，或先把图裁小。"
                                 % (request.content_length, _CHAT_BODY_MAX_BYTES)}), 413

    data = request.get_json() or {}
    user_text = data.get("text", "").strip()
    images = data.get("images") or []
    if not isinstance(images, list):
        return jsonify({"error": "images 必须是一个列表"}), 400
    # 只有图、一个字不打的粘贴是最常见的用法，不许按"空消息"拒掉。
    #    判据是「这一轮有没有内容」，不是「文本空不空」
    #    （`core/pipeline/stages/pre_process.py::_attach_turn_images` 那条同一个）。
    if not user_text and not images:
        return jsonify({"error": "empty message"}), 400

    # 这一轮该在哪一段对话里跑
    #
    # 桌面端的会话列表要三个动作：「开一段新的」（`new`）·「接着某一段」
    # （`thread`）·「追加到当前这段」（两个都不给）。
    # 它们不是三次请求 —— 它们是这一轮的属性，所以落在请求体里，
    # 由 `pre_process` 在加载会话之前一次做完（见 `_run_chat_turn` 的
    # *segment* 那段）。
    #
    # 两个都给，`new` 赢，但要出声：那是调用方写错了，
    #   静默挑一个会让"我点的明明是那条"变成一个查不出来的谜
    #   （标签在说谎同族）。
    # 这里不校验那个 id —— 校验只有一处，在 `pre_process`
    #   （它才是真正下手切会话的地方，`_handle_resume` 那条警告
    #   「喂错就是一个凭空建出来的空会话」说的就是那里）。
    segment = None
    _thread_id = str(data.get("thread") or "").strip()
    if data.get("new"):
        segment = {"new": True}
        if _thread_id:
            # 这一行一律 ASCII 箭头（`->`），不许写 `，`：
            #   它是控制台输出，而本机是 GBK —— 那个字符会让整个
            #   `/api/chat` 在这一行抛 UnicodeEncodeError，
            #   而症状是"测试挂了"，看起来像被测代码的问题
            #   （崩掉的输出看起来像失败）。`scripts/check_gbk_safe.py`
            #   默认只扫 `tests/` 与 `scripts/`，扫不到这里。
            print("[chat] gave both new and thread -> following new"
                  " (thread=%s ignored)" % _thread_id[:24])
    elif _thread_id:
        segment = {"thread": _thread_id}

    #: 这一轮跑在群里吗（群 id）。空 = 私聊那条（今天的行为）。
    #:   不在这里校验群存不存在 —— 校验只有一处（`core.groups.get`），
    #:   而它会给一个带路线的拒绝。这里多判一次 = 第二个判定点。
    _group = str(data.get("group") or "").strip()
    #: 这一段对话的工作目录（草稿态选的那个）。
    #:   空 = 不给（这一格一个字节不动）—— 与 `segment` 那条同款：
    #:   不给就是今天的行为。校验只有一处（`session_workspace.check_dir`），
    #:   在 `pre_process` 读到它的地方；这里只搬运。
    _workspace = str(data.get("workspace") or "").strip()
    try:
        full = _run_chat_turn_full(user_text, images, segment, group=_group,
                                   workspace=_workspace)
        reply = str(full.get("reply") or "")
        # 无主理人群聊回的是多条成员消息，不是一句 reply。
        #   这时 `reply` 必须留空 —— 否则前端会再画一条"主持人总结"。
        group_replies = full.get("group_replies") or []
        # 停在等审批上时不要编一句「(无回复)」：模型不是没回答，
        #   是挂起等确认 —— 那句话会被读成"它哑了"，而且紧跟着的下一条回复
        #   会让用户看到"双回复"（一条空壳 + 一条真话）。
        #   判据是 scheduler 写下的 `awaiting_approval`（取自权威 state）。
        awaiting = bool(full.get("awaiting_approval"))
        reply = _chat_reply_text(reply, group_replies, awaiting)

        # 带上本轮交付的文件，供前端渲染下载按钮。
        # 没有这一层的话，用户在 Web 会话里拿不到任何出口。
        try:
            from skills.file_parser.handler import take_delivered
            delivered = take_delivered()
        except Exception:
            delivered = []
        return jsonify({"reply": reply, "tokens": 0,
                        "files": delivered,
                        # 这一轮的产物记录（前端据此拼 `/api/artifact?id=`）
                        "artifacts": full.get("artifacts") or [],
                        "group_replies": group_replies,
                        #: 空回复是"挂起等审批"时，前端不画那条气泡
                        "awaiting_approval": awaiting,
                        #: 这一轮是用户打断收尾的（`reply` 是回执，不是回答）
                        "cancelled": bool(full.get("cancelled")),
                        #: 这一轮没赶上的 steer（前端用同一条气泡兜底）
                        "steer_leftover": full.get("steer_leftover") or []})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/btw", methods=["POST"])
def api_btw():
    """`/btw` 的旁路入口 —— 不进调度器、不排队、不进历史。

    为什么单独一条端点：`/api/chat` 走 `PipelineScheduler.execute`，
      同一会话有一轮在跑时，新消息只能排在它后面。而 `/btw` 的语义正是
      "主线程在跑，我顺带问一句" —— 排队就把它变成了普通消息
      （用户看到的正是"打了 /btw 却排队去了"）。
    这一条只做一次 `provider.chat`（`core/side_call`）：
      不写 messages、不派发工具、不发事件、不落库。
    """
    if not _check_token():
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    if not _FRAMEWORK_RUNNING:
        return jsonify({"ok": False,
                        "error": tr("框架未启动。请先在端配置页启动 Agent。")}), 503

    data = request.get_json(silent=True) or {}
    question = str(data.get("question") or "").strip()
    if not question:
        return jsonify({"ok": False, "error": tr("请在 /btw 后面带上要问的话")}), 400
    group = str(data.get("group") or "").strip()
    key = session_key_for(_web_session(group))
    try:
        from core import side_call
        #: 与这一轮主对话同一个 provider（这一段冻结的那条，或默认那条）——
        #: 旁路问句不改变任何冻结状态。
        provider = _scheduler._resolve_turn_model(key)[1]
        _sid, msgs = _scheduler._session_pool.get_or_create(key, provider)
        #: `list(msgs)` 是快照：主线程可能正在往后追加，旁路这一问不许
        #: 跟着变（它问的是"此刻为止"）。
        answer = side_call.run(provider, list(msgs), session_id=key,
                               directive=side_call.BTW_DIRECTIVE % question,
                               header="#btw:", tag="btw")
        return jsonify({"ok": True, "answer": answer})
    except Exception as e:                                       # noqa: BLE001
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}), 500


@app.route("/api/trace")
def api_trace():
    """逐条轨迹 —— 参数全文 / 思考 / 结果实质。

    按需拉取（与 `/api/context` 同一条）：不进事件流，
      所以不必改 `EVT_TYPES`；刷新 / 重连只是再拉一次。
    默认关（`data/settings.json` 的 `trace_full`）：关着时返回
      `enabled:false` + 空表 —— 界面据此说"先去设置页打开"，而不是显示一个
      空列表（"没打开"与"没有记录"必须分得开）。
    这些字永远不回灌给模型；它们只是给用户看的。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    try:
        from core import trace as _trace
        since = request.args.get("since", "0")
        try:
            since_n = int(since)
        except (TypeError, ValueError):
            since_n = 0
        return jsonify(_trace.snapshot(session_key_for(_web_session()),
                                       since=since_n))
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"enabled": False, "records": [], "dropped": 0,
                        "last_seq": 0, "error": "%s: %s" % (type(e).__name__, e)})


@app.route("/api/approval", methods=["GET"])
def api_approval_get():
    """当前挂着的待审批 —— 浮窗的渲染源。

    从 state 出，不从事件重建（`core/events.py` 规矩 3）。所以：
    刷新 / 重连 / 进程重启都只是重取一次 —— 没有幽灵浮窗，也不会漏。

    收 `?group=` —— 待审批是按会话存的（`_PENDING_STORES` 的键
      是 `session_key_for(会话)`）。群聊那一轮用的是 `workspace:group:<id>`
      所以不带 group 就会去私聊那条会话里找，永远找不到：
      宿主 agent 调了 `ask_user`、聊天里只有「(无回复)」，而提问面板
      一次都不出现。
      不给 = 私聊那条（今天的行为一个字节不变）。
    非 Web 的会话在这里返回 `null`（它们走文本菜单），这是对的。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    group = request.args.get("group") or ""
    try:
        pending = _scheduler.pending_approval(
            session_key_for(_web_session(group)))
    except Exception as e:
        # 失败返回 `null` 而不是 500：前端把这个端点当轮询用，
        # 偶发失败不该在界面上弹一个错误。
        return jsonify({"pending": None, "error": f"{type(e).__name__}: {e}"})
    return jsonify({"pending": pending})


#: 同一条回答可能被接两遍（连点两次「确认」，模型回了两遍）。
#:
#: 根因是 TOCTOU：`/api/approval` 先读一次「现在还挂着什么」（`pending_approval`），
#: 再 `_run_chat_turn` —— 而结算发生在那一轮的 pre_process 里（晚一步），
#: 于是两个并发 POST 都能通过校验，跑出两轮。
#: 所以这一格按通道键记「正在跑那一轮」，检查与占用在同一把锁里做。
_APPROVAL_INFLIGHT: set[str] = set()
_APPROVAL_INFLIGHT_LOCK = threading.Lock()


def _claim_approval(key: str) -> bool:
    """占住「这条通道正在跑一次审批回答」—— 占到了回 `True`。

    检查与占用在同一把锁里：分两次做（先 `in` 再 `add`）就是这里
      要防的那个 TOCTOU —— 两个并发 POST 都会看到"没人在跑"。
    """
    with _APPROVAL_INFLIGHT_LOCK:
        if key in _APPROVAL_INFLIGHT:
            return False
        _APPROVAL_INFLIGHT.add(key)
        return True


def _release_approval(key: str) -> None:
    with _APPROVAL_INFLIGHT_LOCK:
        _APPROVAL_INFLIGHT.discard(key)


@app.route("/api/approval", methods=["POST"])
def api_approval_post():
    """浮窗的回传。把它变成一行文本喂进同一条管线。

    这里不结算、不解析选项 —— 只做两件事：校验身份、拼出那行文本。
    真正的解析仍然只有 `pre_process._dispatch_approval` 一处。

    与 GET 收同一个 `?group=`（理由见 GET 的 docstring）：
      读待审批、占在途格、跑回答那一轮，三者必须用同一个会话键。
      只改其中一处 = 浮窗看得见却答不上（或答到别的会话里），
        两种坏法都比"看不见"更难查。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    if not _FRAMEWORK_RUNNING:
        return jsonify({"error": tr("框架未启动。请先在端配置页启动 Agent。")}), 503

    group = request.args.get("group") or ""
    data = request.get_json() or {}
    request_id = str(data.get("request_id") or "")
    choice = str(data.get("choice") or "").strip()
    text = str(data.get("text") or "").strip()

    from core.approval import REPLY_OK, REPLY_STALE, reply_decision

    try:
        cur = _scheduler.pending_approval(
            session_key_for(_web_session(group)))
    except Exception as e:
        return jsonify({"ok": False, "reason": "error",
                        "error": f"{type(e).__name__}: {e}"}), 500
    if not cur:
        # 已经没有待审批了 —— 多半是双击的第二下，或者已经在别处答过
        return jsonify({"ok": False, "reason": REPLY_STALE,
                        "error": tr("这条已经回答过了（或正在处理），不用再点。")}), 409

    verdict, payload_text = reply_decision(
        request_id=request_id,
        current_request_id=str(cur.get("request_id") or ""),
        choice=choice, text=text,
        kind=str(cur.get("kind") or ""),
        ask_index=int(cur.get("ask_index") or 1),
        # 提问面板的选项表是动态的（模型给的）——
        #   校验必须对着这次真给出的那张表，不是 `_TABLES`（`find_option`）。
        options=cur.get("options") or None,
    )
    if verdict != REPLY_OK:
        # stale = 对不上（双击/双发/刷新后重放）；bad_choice = 不在这次给出的选项里
        return jsonify({
            "ok": False, "reason": verdict,
            #: 说人话：界面上弹的是 `error`，而 `409` 本身对用户没有信息量。
            "error": ("这条已经回答过了，不用再点。" if verdict == REPLY_STALE
                      else "这个选项不在这次给出的选项里。"),
        }), 409

    # 转发的是 `choice` 或 `text` 的原文，不是拼好的中文 ——
    # 让管线那个唯一的解析器去认（`reply_decision` 已经校验过它在选项里）。
    #
    # 占用这一格再跑（检查与占用在同一把锁里）——
    #   结算发生在那一轮的 pre_process 里，只靠上面那次 `pending_approval`
    #   挡不住并发/双击的第二下（连点两次，模型回了两遍）。
    _key = session_key_for(_web_session(group))
    if not _claim_approval(_key):
        return jsonify({"ok": False, "reason": REPLY_STALE,
                        "error": tr("这条已经回答过了（或正在处理），不用再点。")}), 409
    try:
        reply = _run_chat_turn(payload_text, group=group)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "reason": "error",
                        "error": f"{type(e).__name__}: {e}"}), 500
    finally:
        _release_approval(_key)
    return jsonify({"ok": True, "reply": reply or "", "sent": payload_text})


# Launch

def start_webui(port: int = 5000, open_browser: bool = True) -> None:
    """Start the WSGI server (blocking)."""
    url = f"http://127.0.0.1:{port}/?token={_LAUNCH_TOKEN}"
    import sys
    print(f"\n{'=' * 60}", flush=True)
    print(f"  Crannia Web UI", flush=True)
    print(f"  {url}", flush=True)
    print(f"{'=' * 60}\n", flush=True)
    if open_browser:
        webbrowser.open(url)
    from waitress import serve
    # threads 12，并显式设置 channel_timeout。
    #
    # 原因：waitress 是同步 WSGI，一条 SSE 连接终身占用一个线程；而
    # /api/chat 是阻塞 POST，一次对话同样占满一个线程直到 agent 跑完。
    # 旧值 4 时"1 个聊天标签页 + 1 次在途对话 + 几个轮询端点"就会打满，
    # 整个 WebUI 卡死。
    #
    # channel_timeout 默认 120s：空闲连接会被服务端单向掐掉。SSE 每 15s
    # 发一次心跳本来就能喂饱它，这里显式写出来是为了 (a) 让这个约束在代码里
    # 可见可搜，(b) 给 GC 停顿 / 系统休眠恢复留余量。
    # 退出时说清楚"为什么退"
    # 控制台最后一行常常是 `WARNING:waitress:1 thread(s) still
    # running`，而这句话查不出任何东西。
    #
    # 读 waitress 源码得到的确定结论（`waitress/server.py`）：
    #
    #     def run(self):
    #         try:
    #             self.asyncore.loop(...)
    #         except (SystemExit, KeyboardInterrupt):   # <- 只有这两个
    #             self.close()
    #
    # 而 `close()` -> `task_dispatcher.shutdown(timeout=5)` 会等满 5 秒
    # 才打那句 WARNING。那句 WARNING 是"正在收尾"的产物，不是崩溃的原因，
    # 而且它出现时，真正的触发已经过去 5 秒了。
    # 全仓没有任何地方在主线程里调 `sys.exit()`（`main.py`/`crannia_tray.py`
    # 有，但 `python -m web` 不 import 任何一个），走到这里基本只有一种可能：
    # 那个控制台收到了 Ctrl+C。这不是崩溃，是按设计退出。
    #
    # 那个 `1 thread` 通常是 SSE 连接（`/api/events`，开着一个聊天页就
    # 终身占一个线程）。它不是凶手，只是 5 秒内没停下来，所以被点名了。
    try:
        serve(app, host="127.0.0.1", port=port,
              threads=12, channel_timeout=300)
    except KeyboardInterrupt:
        # 只打控制台，不进 UI / 不落盘 —— 这是排障信息，不是功能。
        # 干净退出（不 re-raise）：traceback 只会是 serve -> run -> asyncore.loop，
        # 一条有用信息都没有，反而盖住上面那行结论。
        print("\n[web] 收到 Ctrl+C -- WebUI 主动退出（不是崩溃）。", flush=True)
        print("[web] 注：Ctrl+C 发给整个控制台进程组；"
              "若别的程序也跑在这个窗口里，它会一起收到。", flush=True)
        return
    except Exception:
        print("\n[web] WebUI 异常退出 -- 这次是真的崩了，原因见下：", flush=True)
        raise


def _maybe_startup_update_check() -> None:
    """框架起来时排一次自动更新检查。

    判据：设置页有「自动更新」这一格，而它此前只有托盘版
    （`crannia_tray.py`）会触发 —— 桌面 GUI 跑的是 `python -m web`，
    从来不调 `background_startup_check()`，那个开关在桌面版里是死的
    （「标签在说谎」）。

    三件事都不在这里做：认不认这个开关（`update_auto_check`）、
    多久一次（`update_last_check` + `check_interval_hours`）、
    失败怎么办 —— 三条全在 `core.updater.background_startup_check` 里，
    这里只负责"有人叫它"。延迟 30 秒与托盘版一致（别和启动抢带宽）。
    """
    try:
        import threading
        from core.updater import background_startup_check
        threading.Timer(30.0, background_startup_check).start()
    except Exception as e:                             # noqa: BLE001
        _log(f"[web] startup update check skipped: {e}")


def _auto_start_channels_from_settings() -> list[str]:
    """If settings.auto_start_channel is true, start the currently selected
    channel (config.yaml channel.default).  Returns list of started channel IDs."""
    settings = _read_settings()
    if not settings.get("auto_start_channel"):
        return []
    config = _read_config()
    cid = config.get("channel", {}).get("default", "")
    if not cid or cid == "cli_channel":
        return []  # CLI doesn't need a daemon thread
    if _channel_running.get(cid):
        return [cid]  # Already running, nothing to do
    try:
        from bus.registry import get_channel
        channel = get_channel(cid)
        if not channel:
            return []
        channel._scheduler = _scheduler
        channel._pool = _session_pool
        def _run():
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            #: 登记循环 —— 「重新登录」要从 Flask 线程把 `terminate()` 投递进来
            #:   （`_channel_loops`）。不登记时那一格是 `None`，覆盖重连直接报
            #:   「无法安全停止，请重启应用后再试」，而重启后自动启动又把它拉起，死循环。
            _channel_loops[cid] = loop
            try:
                _channel_running[cid] = True
                _log(f"[channel] Auto-starting {cid}...")
                loop.run_until_complete(channel.run())
            except Exception as e:
                _log(f"[channel] {cid} error: {e}")
            finally:
                _channel_running[cid] = False
                if _channel_loops.get(cid) is loop:
                    _channel_loops.pop(cid, None)
                loop.close()
        t = threading.Thread(target=_run, daemon=True, name=f"ch-{cid}")
        t.start()
        _channel_threads[cid] = t
        _log(f"[web] Channel {cid} auto-started")
        return [cid]
    except Exception as e:
        _log(f"[web] Failed to auto-start {cid}: {e}")
        return []


# Onboarding (first-run wizard)

_ONBOARDING_DONE = Path("data/.onboarding-done")


def _is_onboarding_done() -> bool:
    return _ONBOARDING_DONE.exists()


@app.route("/api/onboarding/status")
def api_onboarding_status():
    """Check whether the first-run wizard has been completed."""
    return jsonify({"done": _is_onboarding_done()})


@app.route("/api/onboarding/workspace", methods=["POST"])
def api_onboarding_workspace():
    """Save the agent workspace directory."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(force=True) or {}
    path = str(data.get("path", "")).strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    p = Path(path).expanduser().resolve()
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return jsonify({"error": trf("无法创建目录: {e}", e)}), 400
    settings = _read_settings()
    settings["workspace"] = str(p)
    _write_settings(settings)
    # 落盘只是第一步：活着的 WorkspaceManager / 安全核要当场知道这件事。
    #   不然引导里选的那个目录要等下一次启动框架才进允许目录 ——
    #   期间每一次写入都是 "outside every workspace"，而界面上写着"已保存"。
    try:
        _apply_settings_side_effects(settings, {"workspace"})
    except Exception as exc:                                  # noqa: BLE001
        _log("[web] onboarding workspace side effects failed: %s" % exc)
    return jsonify({"ok": True, "workspace": str(p)})


@app.route("/api/onboarding/complete", methods=["POST"])
def api_onboarding_complete():
    """Mark the first-run wizard as finished."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    _ONBOARDING_DONE.parent.mkdir(parents=True, exist_ok=True)
    _ONBOARDING_DONE.touch()
    _log("[web] Onboarding marked complete")
    return jsonify({"ok": True})


@app.route("/api/wechat/qrcode")
def api_wechat_qrcode():
    """Return the current WeChat login state (Web-mode shared state).

    This reads from the shared WebLoginState singleton — the QR code
    is generated by the actual WeChat channel thread, not fetched
    independently.
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from channels.wechat_channel.auth import get_web_login_state
    state = get_web_login_state()
    snap = state.snapshot()
    return jsonify({"ok": True, **snap})


@app.route("/api/wechat/start-login", methods=["POST"])
def api_wechat_start_login():
    """Start the WeChat channel in the background for QR code login.

    If the framework is not yet running, it is initialised first
    (without auto-starting the default channel — only WeChat is
    launched).  Multiple channels can coexist; starting WeChat
    alongside a running Feishu/DingTalk channel is supported.

    The channel thread calls do_login() with a web_callback that
    writes QR state (including a generated QR code PNG as a data
    URI) to the shared WebLoginState.  The frontend polls
    /api/wechat/qrcode to track progress.

    `{"force": true}` = 覆盖重连：先把正在跑的那条微信通道停掉
    （投递 `channel.terminate()` 到它自己的事件循环，停掉长轮询），
    再删掉旧的 `data/wechat_credentials.json`，然后重新走扫码。
    旧线程没退出就不许开新线程 —— 两个线程用同一个 bot 会抢消息。
    停不下来时照实回 500 + 路线，不假装开始。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))

    global _FRAMEWORK_RUNNING

    # Ensure framework is running
    if not _FRAMEWORK_RUNNING:
        _log("[web] WeChat login: framework not running, initialising…")
        try:
            _init_framework()
        except Exception as e:
            _log(f"[web] Framework init failed: {e}")
            return jsonify({"error": trf("框架初始化失败: {e}", e)}), 500
        # NOTE: _auto_start_channels_from_settings() is intentionally
        #       skipped — the user explicitly wants WeChat, not the
        #       configured default channel.

    try:
        from bus.registry import get_channel
        from channels.wechat_channel.auth import get_web_login_state, reset_web_login_state

        channel = get_channel("wechat_channel")
        if not channel:
            return jsonify({"error": tr("微信通道未加载")}), 500

        creds_path = Path("data/wechat_credentials.json")

        # 覆盖重连：先停旧线程，再删旧凭据
        if force:
            th = _channel_threads.get("wechat_channel")
            if th and th.is_alive():
                loop = _channel_loops.get("wechat_channel")
                if loop is None:
                    return jsonify({"error": (
                        tr("旧微信通道还在运行，无法安全停止，请重启应用后再试。")
                    )}), 500
                try:
                    fut = asyncio.run_coroutine_threadsafe(channel.terminate(), loop)
                    fut.result(timeout=8)
                except Exception as e:                          # noqa: BLE001
                    #: 超时那一支要把那个 Future 撤掉：它若在旧线程
                    #:   刚登完时才开始执行，会把正在跑的 `_running`
                    #:   翻成 False（等于把刚连上的通道又关掉）。
                    try:
                        fut.cancel()
                    except Exception:                           # noqa: BLE001
                        pass
                    return jsonify({"error": (
                        tr("旧微信通道停止失败：%s: %s。旧连接仍在运行，请重启应用后再试。")
                        % (type(e).__name__, e)
                    )}), 500
                th.join(timeout=8)
                if th.is_alive():
                    #: 强制收口：`terminate()` 在长轮询里排队时，
                    #:   8 秒可能不够。再等下去就永远是「请重启应用」——
                    #:   而重启之后自动启动又把它拉起来（那两个坑合起来就是
                    #:   死循环）。直接把循环停掉。
                    try:
                        loop.call_soon_threadsafe(loop.stop)
                    except Exception:                        # noqa: BLE001
                        pass
                    th.join(timeout=3)
                if th.is_alive():
                    return jsonify({"error": (
                        trf("旧微信通道仍未停下（{name}）。请先关掉「自动启动通道」"
                            "（设置 → 其他设置）再点重试；或者完全退出应用后"
                            "把 data/wechat_credentials.json 删掉再开。",
                            name="ch-wechat")
                    )}), 500
            _channel_running["wechat_channel"] = False
            #: 删旧凭据放在停稳之后：先删再停的话，旧线程万一没停下，
            #:   它手上那份凭据还在内存里，而盘上已经没了 —— 下次启动会以为没配过。
            try:
                creds_path.unlink(missing_ok=True)
            except OSError as e:
                return jsonify({"error": tr("删除旧微信凭据失败：%s") % e}), 500
            _log("[web] WeChat force re-login: old channel stopped, credentials removed")

        # Already running?（真在跑，只报 running，什么都不做）
        if _channel_running.get("wechat_channel"):
            return jsonify({"ok": True, "phase": "running"})

        # Already configured?
        #: "配过" ≠ "在跑"：以前这里一看凭据存在就回 `already_configured`
        #:   然后直接返回，通道明明没在跑（上次启动失败 / 刚被停掉 / 重启后
        #:   没自动拉起），界面却写着"已连接"，用户连点哪儿都找不着
        #:   （「扫码连上了，发消息没反应」，而进程里
        #:   一条到 iLink 的连接都没有）。凭据存在就顺手把它跑起来
        #:   （`run()` 会自己 load 缓存凭据并进长轮询）。
        configured_bot = ""
        if not force and creds_path.exists():
            from channels.wechat_channel.auth import load_credentials
            creds = load_credentials(creds_path)
            if creds:
                configured_bot = (creds.get("ilink_bot_id", "") or "")[:16] + "..."

        # Reset shared state and wire up web callback
        state = get_web_login_state()
        reset_web_login_state()

        def _web_callback(phase: str, data: dict) -> None:
            # When iLink returns QR data, generate an actual QR code image
            # (qrcode_img_content is the URL to ENCODE, not an image URL)
            if phase == "qr_ready":
                qr_data = data.get("img_url", "") or data.get("qrcode_key", "")
                qr_b64 = _make_qr_data_uri(qr_data) if qr_data else ""
                data["qr_data_uri"] = qr_b64
            state.set_phase(phase, **data)

        if hasattr(channel, "set_web_login_callback"):
            channel.set_web_login_callback(_web_callback)

        # Inject scheduler/pool
        channel._scheduler = _scheduler
        channel._pool = _session_pool

        # Start channel in background thread
        def _run_wechat_login():
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            #: 登记循环 —— 「覆盖重连」要从 Flask 线程把 `terminate()`
            #:   投递进来。没这一格就只能干等 40 秒的长轮询自己返回。
            _channel_loops["wechat_channel"] = loop
            try:
                state.set_phase("starting")
                _channel_running["wechat_channel"] = True
                _log("[web] WeChat channel starting (web login mode)...")
                loop.run_until_complete(channel.run())
            except Exception as e:
                _log(f"[web] WeChat login error: {e}")
                state.set_phase("error", error_msg=str(e))
            finally:
                _channel_running["wechat_channel"] = False
                if _channel_loops.get("wechat_channel") is loop:
                    _channel_loops.pop("wechat_channel", None)
                loop.close()

        t = threading.Thread(target=_run_wechat_login, daemon=True, name="ch-wechat-login")
        t.start()
        _channel_threads["wechat_channel"] = t

        _log("[web] WeChat login flow started in background")
        return jsonify({"ok": True, "phase": "starting",
                        **({"bot_id": configured_bot} if configured_bot else {})})

    except Exception as e:
        _log(f"[web] WeChat start-login failed: {e}")
        return jsonify({"error": str(e)}), 500


# QR image generation

def _make_qr_data_uri(data: str, size: int = 240) -> str:
    """Generate a QR code PNG as a base64 data URI from *data*.

    Uses the `qrcode` package (same as the terminal display path).
    Returns a string suitable for use as an <img src="...">.
    """
    import base64
    import io
    try:
        import qrcode as qr_module
        qr = qr_module.QRCode(box_size=6, border=2)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except ImportError:
        # Fallback: use an external QR API
        from urllib.parse import quote
        return f"https://api.qrserver.com/v1/create-qr-code/?size={size}x{size}&data={quote(data)}"


@app.route("/api/wechat/qrcode/status")
def api_wechat_qrcode_status():
    """Poll the scan status of a WeChat QR code directly from iLink.

    Query params: qrcode=<key> (the qrcode value returned by /api/wechat/qrcode)
    Returns: {ok, status: "wait"|"scaned"|"confirmed"|"expired", ...}
    On "confirmed", also returns bot_token (masked) info.
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    qrcode_key = request.args.get("qrcode", "")
    if not qrcode_key:
        return jsonify({"error": "missing qrcode param"}), 400
    try:
        from channels.wechat_channel.auth import poll_qrcode_status
        status = poll_qrcode_status(qrcode_key)
        state = status.get("status", "")
        result = {"ok": True, "status": state}
        if state == "confirmed":
            result["bot_id"] = status.get("ilink_bot_id", "")[:16] + "..."
        return jsonify(result)
    except Exception as e:
        _log(f"[web] WeChat QR status poll failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/has-api-key")
def api_has_api_key():
    """Check if at least one API key is configured."""
    # 与 `/api/env-keys` 同一族的守门。
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    config = _read_config()
    models = _read_models(config)
    for m in models:
        if m.get("configured"):
            return jsonify({"has_key": True})
    return jsonify({"has_key": False})


# Multi-Key Store (per-provider API key vault)

_API_KEYS_PATH = Path("data/api_keys.json")


def _load_api_keys() -> dict:
    """Load the multi-key store. Migrates .env keys on first access."""
    if _API_KEYS_PATH.exists():
        try:
            return json.loads(_API_KEYS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    # First access — migrate from .env
    store: dict = {}
    env_path = _PROJECT_ROOT / ".env"
    env_map = _build_env_map()
    rev_map: dict[str, str] = {}
    for provider_key, env_var in env_map.items():
        rev_map[env_var] = provider_key
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            provider_key = rev_map.get(k, "")
            if not provider_key:
                continue
            if provider_key not in store:
                store[provider_key] = {"active": "", "keys": {}}
            kid = uuid.uuid4().hex[:8]
            store[provider_key]["keys"][kid] = {
                "label": provider_key,
                "key": v,
                "created": datetime.now(timezone.utc).isoformat(),
            }
            store[provider_key]["active"] = kid
    _save_api_keys(store)
    return store


def _save_api_keys(store: dict) -> None:
    """Persist the multi-key store to disk."""
    _API_KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _API_KEYS_PATH.write_text(
        json.dumps(store, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _provider_active_key(provider: str) -> str:
    """Return the active API key for a provider (for .env sync)."""
    store = _load_api_keys()
    prov = store.get(provider, {})
    active_id = prov.get("active", "")
    if active_id:
        return prov.get("keys", {}).get(active_id, {}).get("key", "")
    return ""


def _sync_active_key_to_env(provider: str) -> None:
    """Write the active key for *provider* to .env and os.environ."""
    key_value = _provider_active_key(provider)
    env_map = _build_env_map()
    key_name = env_map.get(provider, f"{provider.upper()}_API_KEY")
    if key_value:
        _write_env({key_name: key_value})
        os.environ[key_name] = key_value
    else:
        _remove_env_key(key_name)


def _remove_env_key(env_var: str) -> None:
    """Remove a key from .env and os.environ."""
    env_path = _PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(env_var + "=") and not stripped.startswith("#"):
            continue
        new_lines.append(line)
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    if env_var in os.environ:
        del os.environ[env_var]


# API Key management endpoints

@app.route("/api/env-keys")
def api_env_keys():
    """Return saved API keys from the multi-key store.

    Query params:
      provider  — filter to a specific provider (e.g. ?provider=deepseek)
    """
    # 这一条必须守门。
    #   它回的是每一把 key 的「前 4 位…后 4 位」—— 而 `web_token` 本身是明文
    #   写在 `/ping` 的应答里的（本机进程都拿得到），不守门就等于把凭据的
    #   头尾发给任何能连上这个端口的进程。旁边三条都有守门，唯独这条不能漏。
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    provider_filter = request.args.get("provider", "")
    store = _load_api_keys()
    result: list[dict] = []
    for prov_key, prov_data in store.items():
        if provider_filter and prov_key != provider_filter:
            continue
        active_id = prov_data.get("active", "")
        for kid, kd in prov_data.get("keys", {}).items():
            raw = kd.get("key", "")
            masked = raw[:4] + "****" + raw[-4:] if len(raw) > 8 else "****"
            result.append({
                "id": kid,
                "env_var": prov_key,  # provider key used as grouping key
                "provider": prov_key,
                "label": kd.get("label", prov_key),
                "masked": masked,
                "created": kd.get("created", ""),
                "active": kid == active_id,
            })
    return jsonify({"keys": result})


@app.route("/api/env-keys/delete", methods=["POST"])
def api_env_keys_delete():
    """Delete an API key from the store by provider + key id."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json() or {}
    provider = data.get("provider", "")
    kid = data.get("id", "")
    if not provider or not kid:
        return jsonify({"ok": False, "error": tr("缺少服务商或配置 ID")}), 400
    store = _load_api_keys()
    prov = store.get(provider, {})
    if kid in prov.get("keys", {}):
        was_active = prov.get("active") == kid
        del prov["keys"][kid]
        if was_active:
            # Pick another key as active, or clear
            remaining = list(prov["keys"].keys())
            prov["active"] = remaining[0] if remaining else ""
        _save_api_keys(store)
        _sync_active_key_to_env(provider)
    return jsonify({"ok": True})


@app.route("/api/env-keys/activate", methods=["POST"])
def api_env_keys_activate():
    """Switch the active key for a provider."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json() or {}
    provider = data.get("provider", "")
    kid = data.get("id", "")
    if not provider or not kid:
        return jsonify({"ok": False, "error": tr("缺少服务商或配置 ID")}), 400
    store = _load_api_keys()
    prov = store.get(provider, {})
    if kid in prov.get("keys", {}):
        prov["active"] = kid
        _save_api_keys(store)
        _sync_active_key_to_env(provider)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Key 不存在"}), 404


@app.route("/api/env-keys/rename", methods=["POST"])
def api_env_keys_rename():
    """Rename a key's display label."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json() or {}
    provider = data.get("provider", "")
    kid = data.get("id", "")
    new_label = data.get("label", "")
    if not provider or not kid or not new_label:
        return jsonify({"ok": False, "error": tr("缺少参数")}), 400
    store = _load_api_keys()
    prov = store.get(provider, {})
    if kid in prov.get("keys", {}):
        prov["keys"][kid]["label"] = new_label
        _save_api_keys(store)
    return jsonify({"ok": True})


@app.route("/api/key-labels")
def api_key_labels():
    """Return custom labels from the multi-key store (for backward compat)."""
    # 它回的是多 key 仓库里的 key id 与标签
    #   —— 别的 key 端点（activate / delete / rename）都按 id 认，给它守门。
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    store = _load_api_keys()
    labels: dict = {}
    for prov_key, prov_data in store.items():
        for kid, kd in prov_data.get("keys", {}).items():
            labels[kid] = kd.get("label", prov_key)
    return jsonify(labels)


# Settings

_SETTINGS_PATH = Path("data/settings.json")


def _read_settings() -> dict:
    """Read the settings JSON file, or return defaults."""
    if _SETTINGS_PATH.exists():
        try:
            return json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "auto_start_framework": False,
        "auto_start_channel": False,
        "boot_on_startup": False,
    }


def _normalize_mail_config(raw_config: dict) -> dict:
    """Migrate old flat mail config to new multi-account structure.

    Old: {email_address, password, _provider, smtp_server, ...}
    New: {active: "acc_...", accounts: {acc_id: {label, email_address, ...}}}
    """
    if not raw_config:
        return {"active": "", "accounts": {}}
    # Already new format
    if "accounts" in raw_config:
        return raw_config
    # Old flat format — wrap into a single account
    if "email_address" in raw_config:
        import time
        acc_id = f"acc_{time.time_ns()}"  # ns precision, no collision
        label = raw_config.get("email_address", "").split("@")[0]
        return {
            "active": acc_id,
            "accounts": {
                acc_id: {**raw_config, "label": label}
            }
        }
    # Empty or unknown
    return {"active": "", "accounts": {}}


def _write_settings(data: dict) -> None:
    """Persist settings to JSON."""
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _apply_settings_side_effects(settings: dict, written) -> None:
    """把刚落盘的设置推进那些活着的东西（环境变量 / 缓存 / 单例 / 注册表）。

    一处写，三条路共用：`POST /api/settings` · 「恢复默认」· 「导入设置包」。
      此前「恢复默认」只做了工作区与 ICE 两格 —— 而它同样会把 `boot_on_startup`
      写回 `false`，设置说关了、注册表还在开机自启（「标签在说谎」）。
      导入更险：它会写 `proxy_*` 与 `boot_on_startup`，只落文件就是同一个病。

    两个参数不许合并：`settings` 是合并后的整份（工作区管理器要的是整份，
      喂它一个请求体片段会把没提的键重置成兜底）；`written` 是这一轮写了哪些键
      （判据与 `POST /api/settings` 原来逐字一致：看请求体里有没有它）。
    """
    data = settings
    written = set(written or ())
    # 代理三档改了，立刻落到环境变量（只认 env 的 SDK 立刻跟上）。
    #    判据见 `core/net_proxy.apply_env_policy`：飞书 `lark_oapi` 建 WS 时
    #    裸调 `requests.post`，不落 env 的话它照样走那个可能已经死掉的老代理。
    if any(k in data for k in ("proxy_mode", "proxy_url")):
        try:
            from core import net_proxy as _NP
            _changed = _NP.apply_env_policy()
            if _changed:
                _log("[proxy] 「%s」档已落到环境变量：%s"
                     % (_NP.mode(), ", ".join(_changed)))
        except Exception as e:                                    # noqa: BLE001
            _log(f"[proxy] 环境变量口径没落下去：{type(e).__name__}: {e}")

    # 界面语言：清掉进程里的读数缓存 —— 语言包 / 设置文件都按 mtime 失效，
    #   但同一秒内改写（本轮的写完立刻读）可能 mtime 不变，显式清一次。
    if "interface_language" in data:
        try:
            from core import i18n as _I18N
            _I18N.reload()
        except Exception as e:  # noqa: BLE001
            _log(f"[settings] i18n reload failed: {e}")

    # ICE：把新值推进那个进程级单例 —— 它是构造时读一次的。
    #   不推的话，用户在设置页改完、界面回一句"已保存"，而实际行为要等重启才变
    #   —— 那正是「标签在说谎」。`_ice_on()` 每次现读 `guard.enabled`，
    #   所以推进去就当场生效（与 `stream_output` 那条"每次请求现读"同一取向）。
    if "ice" in data:
        try:
            from core.ice import reload_enabled
            reload_enabled()
        except Exception as e:  # noqa: BLE001
            _log(f"[settings] ICE reload failed: {e}")

    # Sync to live WorkspaceManager if running
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        if wm:
            wm.load_from_settings(data)
    except Exception:
        pass

    # Handle boot-on-startup registry change
    if "boot_on_startup" in data and sys.platform == "win32":
        try:
            _set_windows_startup(data["boot_on_startup"])
        except Exception as e:
            _log(f"[settings] Failed to set startup: {e}")


def _explorer_select_cmd(path: str) -> str:
    """Windows 资源管理器「选中一个文件」的命令行。

    只给路径加引号，不给整个 `/select,path` 加：
    传 list 给 `Popen` 时 `subprocess` 会自动把含空格的整个参数包成
    `"/select,C:\\…\\my project\\x.svg"`，而 Explorer 不认那种形状 ——
    它会退回去打开「文档」。正确写法：`explorer /select,"C:\\…\\x.svg"`。
    Windows 文件名不允许 `"`，这里不需要再转义。
    """
    return 'explorer /select,"%s"' % path


@app.route("/api/reveal", methods=["POST"])
def api_reveal():
    """把回复里那个路径在资源管理器里定位（文件）或打开那个文件夹（目录）。

    落点是回复里的 Markdown 链接（`[工作区 → 项目一 → 作业.docx](真实路径)`）——
    让模型自己声明哪个是路径，前端不猜（它写的是「工作区 → …」这种人话，
      纯靠正则认不出来；而 href 里那个串是它自己给的，是声明不是猜测）。

    只定位、不执行（`showItemInFolder` 那一档）。判据是一条真实的风险：
    回复是模型写的文本，href 里也可能是任意串 —— 若这里调"用默认程序打开"，
    它写一个 `.exe` / `.bat` 的路径就变成用户一点就执行。
    所以文件只选中、不打开；目录只打开那个文件夹（打开文件夹不执行任何东西）。

    解析用已有的口径（"查"那一类：`lookup_roots()`）——
    相对路径按"默认落点在前"依次试，不新发明一套解析。
    不限制在允许的根里：用户会想点"桌面"上那份（回复里就有），
    把它挡在外面等于这个功能对他没用；而动作本身只是"跳到那个位置"。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    raw = str(data.get("path") or "").strip().strip("`").strip('"').strip("'")
    if not raw:
        return jsonify({"ok": False, "error": tr("没给路径")}), 400

    import subprocess as _sp
    from pathlib import Path as _P

    #: 候选：绝对路径就用它自己；相对路径按 `lookup_roots()` 依次试。
    try:
        from core.workspace import lookup_roots
        roots = lookup_roots()
    except Exception:                                  # noqa: BLE001
        roots = []
    def _candidates(value: str):
        return ([_P(value)] if _P(value).is_absolute()
                else [r / value for r in roots] + [_P(value)])

    cands = _candidates(raw)
    hit = next((c for c in cands if c.exists()), None)
    # 模型可能把路径写成 URL 编码的样子 ——
    #   `[pelican_bike.svg](D:/…/my%20project/workspace/pelican_bike.svg)`。
    #   于是点击报"这个路径不存在"，而文件其实就在那儿（`%20` 不是目录名的一部分）。
    #   判据：先按原样找（文件名里真带 `%20` 的情况优先），找不到再按 URL 解码
    #   解一次。只解一次、只在原样失败时解；结果照样要过存在性检查，
    #   动作仍然只是"定位/打开文件夹"（不执行文件）。
    if hit is None and "%" in raw:
        from urllib.parse import unquote as _unquote
        decoded = _unquote(raw)
        if decoded != raw:
            cands = _candidates(decoded)
            hit = next((c for c in cands if c.exists()), None)
            if hit is not None:
                print(f"[reveal] 路径是 URL 编码的 —— 按解码后的解析：{decoded}")
                raw = decoded
    if hit is None:
        #: 如实说（别让用户以为点了）——
        #: 顺便把"试过哪儿"说一句，那是唯一能让他自己纠的地方。
        tried = "、".join(str(c) for c in cands[:3])
        return jsonify({"ok": False,
                        "error": tr("这个路径不存在：%s（找过：%s）") % (raw, tried)}), 404

    try:
        if os.name == "nt":
            if hit.is_dir():
                os.startfile(str(hit))                 # 打开文件夹（不执行任何东西）
            else:
                # 必须传一个字符串，且只给路径加引号。
                #   传 list 时 `subprocess` 会把整个 `/select,C:\…\my project\…`
                #   当成一个含空格的参数加引号（`"/select,C:\…"`），而
                #   Explorer 的 `/select,` 开关不认这种形状，它退回去打开
                #   「文档」（用户看到的就是这个）。`explorer /select,"C:\…"`
                #   才是它认识的写法。
                _sp.Popen(_explorer_select_cmd(str(hit)))
        elif sys.platform == "darwin":
            _sp.Popen(["open"] + (["-R", str(hit)] if hit.is_file() else [str(hit)]))
        else:
            _sp.Popen(["xdg-open", str(hit.parent if hit.is_file() else hit)])
    except Exception as exc:                           # noqa: BLE001
        return jsonify({"ok": False, "error": tr("打不开：%s") % exc}), 500
    return jsonify({"ok": True, "path": str(hit), "dir": hit.is_dir()})


@app.route("/api/mode", methods=["POST"])
def api_mode_post():
    """把模式切到"当前这一段会话" —— 输入框那枚 chip 的入口。

    只改 `/api/settings {default_mode}` 是不够的 —— 那只写"以后新会话的默认值"
    （设置页自己的小字就写着「已冻结的会话保持原设置」），而 chip 若又本地乐观更新、
    立刻显示成已切，用户以为切了、这一段会话根本没变。
    症状：在"自动模式"下每一个动作都被问，因为会话其实一直冻在 `manual`。
    这是「标签在说谎」的原形。

    落点就是 `/mode` 命令同一个 setter（`core.modes.set_mode`）：
    当场生效、并返回一句"这是冷启动"的话。两条路不许有两套语义。
    不落盘 —— 与 `/effort` 同一条判据（`/effort` 只改这一段，
    `/clear` / 重启即回设置页那个默认档）。要改默认走设置页。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    key = str(data.get("mode") or "")
    try:
        from core import modes
        msg = modes.set_mode(session_key_for(_web_session()), key)
    except Exception as exc:                       # 非法值 / 未实现的档，当场拒、出声
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "mode": key, "message": msg})


@app.route("/api/computer-use/status")
def api_computer_use_status():
    """接管状态（全局单例 —— 一台机器只有一个输入桌面）。

    界面不许从本地状态猜它开着没有：这是唯一真相源
    （「标签在说谎」同族 —— 横幅和按钮都读这一格）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core.computer import session as _cu
    return jsonify(_cu.status())


@app.route("/api/computer-use/enable", methods=["POST"])
def api_computer_use_enable():
    """启用接管。只允许「完全权限」会话。

    界面那边按钮在这条模式下才可点；这里是第二道 ——
    界面坏了 / 有人直接打这个端点，都不能绕过模式校验。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    sid = session_key_for(_web_session())
    try:
        from core import modes
        # 用 `frozen_mode`（纯读）而不是 `current_mode`：后者读的时候会
        #   顺手把模式冻结下来 —— 而那是个副作用，`test_modes` 钉着
        #   "读接口不许有副作用"。`frozen_mode` 返回 None = 这段还没定过模式
        #   所以也就不可能已经到了完全权限（那一档必须由用户亲手切）。
        if modes.frozen_mode(sid) != "full_access":
            return jsonify({"ok": False,
                            "error": tr("接管只在「完全权限」模式下可用")}), 400
    except Exception as exc:                       # noqa: BLE001
        return jsonify({"ok": False, "error": tr("读不到当前模式：%s") % exc}), 500
    from core.computer import session as _cu
    ok, msg = _cu.enable(sid)
    if not ok:
        return jsonify({"ok": False, "error": msg, **_cu.status()}), 409
    print("[computer] takeover enabled for %s" % sid)
    return jsonify({"ok": True, **_cu.status()})


@app.route("/api/computer-use/disable", methods=["POST"])
def api_computer_use_disable():
    """停用接管（按钮 / 横幅 / 托盘都走这里）。幂等：没开着也返回 ok。"""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core.computer import session as _cu
    owner = _cu.disable("user")
    if owner:
        print("[computer] takeover disabled by user (%s)" % owner)
    return jsonify({"ok": True, "was_owner": bool(owner), **_cu.status()})


@app.route("/api/window/focus", methods=["POST"])
def api_window_focus():
    """把 Crannia 主窗拉回前台。

    Edge 全屏播视频时一轮结束，Electron 的
    `show()/moveTop()/focus()` 只让窗口在任务栏亮黄闪烁 —— 那是
    Windows 前台锁在工作（`SetForegroundWindow` 返回 FALSE）。这里用
    `win32.activate()` 的 `AttachThreadInput` 再拉一次（没有 uiAccess
    的进程能做到的上限）。

    只由壳在 `task.end` + 接管开启时调；不是给模型用的能力
    （`list_windows` 仍然把自家窗口剔掉）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    try:
        from core.computer import win32
        w = win32.pick_main_window(win32.enum_windows(), win32.our_pids())
        if w is None:
            return jsonify({"ok": False, "error": tr("找不到 Crannia 主窗口")}), 404
        ok, why = win32.activate(w.hwnd)
        return jsonify({"ok": bool(ok), "error": why or ""})
    except Exception as exc:                       # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/effort", methods=["POST"])
def api_effort_post():
    """投入档的当场生效入口 —— 与 `/api/mode` 同一条判据。

    同一个病：chip 从前只写 `settings.effort_level`（= 以后新会话的默认），
    而界面显示成已切。真正"把本会话钉到某一档"的是 `core.tiers.set_level`
    —— `/effort` 命令走的就是它。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    try:
        lvl = int(data.get("effort"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "effort 要给一个数字"}), 400
    try:
        from core import tiers
        tiers.set_level(session_key_for(_web_session()), lvl)
    except Exception as exc:                       # 越界 / 未实现，当场拒、出声
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "effort": lvl})


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    settings = _read_settings()
    # 把生效值一并带给前端 —— 键不在 `settings.json` 里时
    # 前端也要显示真实生效的那个值，否则界面上的开关与真实行为对不上
    # （标签在说谎：标签写着"自动"，实际是手动）。
    try:
        from core.modes import configured_mode
        settings.setdefault("default_mode", configured_mode())
    except Exception:                             # noqa: BLE001
        pass
    try:
        from core.approval_timeout import seconds as _timeout_seconds
        settings.setdefault("approval_timeout_seconds", _timeout_seconds())
    except Exception:                             # noqa: BLE001
        pass
    # 记忆接续档：键不在 `settings.json` 里时，生效值是出厂基准那一份。
    # 不回填的话，新装（还没写过 `settings.json`）的界面上那一格是空的 ——
    # 而 `POST` 是收这个键的，于是"按了没反应的开关"。
    try:
        from core.settings_baseline import baseline as _baseline
        if "memory_continuity" not in settings:
            _v = (_baseline() or {}).get("memory_continuity")
            if _v is not None:
                settings["memory_continuity"] = _v
    except Exception:                             # noqa: BLE001
        pass
    # 回答语言 —— 三层里的②：未设 = null（跟随系统）
    # 别用 `setdefault(..., "zh")` 之类的兜底：那就没有了「没设过」这个事实，
    #    界面上「跟随系统」会变成一句假话（同一条：存事实，不存值）。
    try:
        from core import response_language as _RL
        settings.setdefault("response_language", _RL.configured_language())
        settings["response_language_effective"] = _RL.current("")
        settings["response_languages"] = [
            {"id": k, "name": _RL.LANG_NAMES[k]} for k in _RL.LANGS]
    except Exception:                             # noqa: BLE001
        pass
    # 界面语言 —— 与「回答语言」两条轴，互不影响
    # 下拉项自动发现（`locales/*.json` 里放一个 `ja.json` 就多一个选项）；
    #   `zh` 是源码语言，永远在第一个。
    try:
        from core import i18n as _I18N
        # 未设 = `None`（跟随系统），判据同上面「回答语言」那一段：
        #    `setdefault(..., current())` 会把生效值顶上去冒充"用户设过"，
        #    于是界面上那个「跟随系统」永远选不回去（存事实，不存值）。
        settings.setdefault("interface_language", _I18N.configured())
        settings["interface_language_effective"] = _I18N.current()
        settings["interface_languages"] = [
            {"id": k, "name": n} for k, n in _I18N.available()]
        settings["interface_language_system"] = _I18N.system_language()
    except Exception:                             # noqa: BLE001
        pass
    # 出网代理三档 + 一行「当前实际生效」
    # 判据同「回答语言」：未设 = 默认档（system），不往 `settings.json` 里写。
    # `proxy_url` 只给打码的那一份（它可能带 `user:pass@`，是凭据）；
    #    写回侧是「留空 = 不改」，所以前端拿到打码值也不会把它存回去。
    try:
        from core import net_proxy as _NP
        _pst = _NP.describe()
        settings.setdefault(_NP.SETTING_MODE, _pst["mode"])
        settings[_NP.SETTING_URL] = _pst["url"]
        settings["proxy_state"] = _pst
        #: 下拉框里只留名字，不写括号解释 —— 解释归每一档下面那行说明。
        settings["proxy_modes"] = [
            {"id": _NP.MODE_SYSTEM, "name": tr("跟随系统")},
            {"id": _NP.MODE_DIRECT, "name": tr("直连")},
            {"id": _NP.MODE_MANUAL, "name": tr("指定")},
        ]
    except Exception:                             # noqa: BLE001
        pass
    #: 数据目录的真实位置 —— 设置页那一行拿它给按钮用。
    #:   只读：它不经 `POST /api/settings` 写回（那边按载荷里的键写），
    #:   所以不会把路径腌进 `settings.json`。
    #:   判据是"用户要能在卸载之前把东西捞走"，而路径只有服务端知道
    #:   （安装版里它是 `<安装目录>\resources\app\data`，开发版是仓根 `data/`）。
    settings.setdefault("data_dir", str(_SETTINGS_PATH.parent))
    # 数据格式那一格：兼容模式要一直看得见，
    #    判据同 ICE 的「保护已降级」—— 不是弹一次就走的一次性提示。
    try:
        settings["data_schema"] = _paths.data_schema_status()
    except Exception:
        pass
    # Merge workspace data if framework is running
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        if wm:
            ws_data = wm.to_settings_dict()
            settings.update(ws_data)
    except Exception:
        pass
    # 规避目录：只有"从没配过"才给默认那一批。
    #
    # 配过就照用户那份原样交出去 —— 包括空表。此前是每次 GET 都把默认并回来，
    # 于是界面上删掉任何一条与默认重合的目录，下一次拉取它又回来了；
    # 而且每存一次，默认那批就被写进 `settings.json`，那一份从此不再是"用户的表"。
    try:
        from core.workspace import resolve_default_excludes
        if settings.get("forbidden_directories") is None:
            settings["forbidden_directories"] = resolve_default_excludes()
    except Exception:
        pass
    # ICE：把两把开关补齐成 UI 能直接用的形状
    # 缺的键填兜底值本身（不是另写一个 False）—— 兜底值住在
    #   `core.ice.guard.ICE_DEFAULT_ON`，只有那一份；这里抄一份字面量的话，
    #   将来"默认关，默认开"翻面时，界面会永远显示那个旧值。
    # 顺带给 `ice_effective` = 「总闸 且 信息层」的生效值 —— 界面要它来显示
    #   黄色横条（保护被降级时界面必须一直看得见，否则用户以为还护着）。
    try:
        from core.ice import ICE_DEFAULT_ON, ICE_SWITCH_KEYS, get_guard
        tree = settings.get("ice")
        tree = dict(tree) if isinstance(tree, dict) else {}
        for _k in ICE_SWITCH_KEYS:
            if not isinstance(tree.get(_k), bool):
                tree[_k] = ICE_DEFAULT_ON
        settings["ice"] = tree
        settings["ice_effective"] = bool(get_guard().enabled)
        # ICE 网络侧：金丝雀值原样交出去 —— 设置页要显示它
        #   （用户得知道自己该把哪一串抄进文件里，否则那条 token 的用法是空的）。
        #   不许让界面抄一份字面量：那是第二套真相源，改了常量界面会撒谎
        #   （与上面 `ICE_DEFAULT_ON` 逐字同一条规矩）。
        from core.ice import CANARY_VALUES
        settings["ice_canary"] = list(CANARY_VALUES)
    except Exception:
        pass
    # 改前留档的保留期
    # 与上面 `ICE_DEFAULT_ON` 同一条规矩：缺的键填兜底值本身，
    #   界面不抄字面量；顺带把生效值（可能因为坏值而是 `None`）交出去。
    try:
        from core.file_history import DEFAULT_DAYS, SETTING_KEY, configured_days
        if not isinstance(settings.get(SETTING_KEY), int):
            settings[SETTING_KEY] = DEFAULT_DAYS
        settings["file_history_effective_days"] = configured_days()
    except Exception:
        pass
    # 设置页要用到的那些键，缺的补成生效值
    #
    # 判据（照上面 ICE / file_history 那两块，一字不差）：键不在 `settings.json`
    #    里时，界面也要显示真实生效的那个值 —— 否则开关与真实行为对不上
    #    （标签在说谎：标签写着"自动"，实际是手动）。
    # 界面不许抄字面量兜底（那是第二套真相源：改了默认值界面会撒谎）——
    #    所以由这里补，而补的值就取自各读取端自己的常量。
    # 补的都是没有独立读取端常量的那几个（有常量的都单列在各自那一块里）——
    #    这里给的是"这个键没写过时的语义默认"，与读取端 `settings.get(k, 默认)` 同值。
    try:
        from core.file_history import restore_enabled as _rewind_restore
        settings.setdefault("rewind_restore_files", _rewind_restore())
    except Exception:
        pass
    try:
        from core.memory_store import _memory_write_enabled as _mw
        from core.memory_store import _auto_seal_enabled as _mas
        from core.memory_organize import is_organize_enabled as _mo
        settings.setdefault("memory_write", _mw())
        settings.setdefault("memory_organize", _mo())
        #: 「删除时自动封条」—— 记忆库页顶栏那个开关读它。
        settings.setdefault("memory_auto_seal", _mas())
    except Exception:
        pass
    #: `work_groups_enabled` 已从这张表里拿掉（那个开关退场了）。
    #: 留在这里的话，`setdefault` 会让每一次 GET /api/settings 都把它重新写出来
    #: 所以用户手改文件删掉它，下一轮又回来（「标签在说谎」的形状）。
    #: `cross_zone_enabled` 的兜底 = true，与出厂基准、`config.yaml` 和
    #: `WorkspaceManager.load_from_settings` 四处同值。
    for _k, _v in (("cross_zone_enabled", True), ("auto_approve", False),
                   ("cascade_enabled", False),
                   ("cascade_notify", True), ("cascade_auto_start", True),
                   # 「权限解禁」—— 只影响完全权限模式；GET 给生效值。
                   ("full_access_unlock", False),
                   ("relax_command_tier", False), ("no_ask_readonly", False),
                   ("stream_output", False), ("context_sliding_window", True),
                   ("boot_on_startup", False), ("auto_start_channel", True),
                   ("update_auto_check", True), ("recap_enabled", False),
                   # 「50 条存储提醒」那个不再提醒。
                   #   与 `SafetyNotice` 那条存 `localStorage` 的相反 ——
                   #   这一条要跨重启，所以它必须有一个看得见的撤回口
                   #   （= 设置页那一格）。
                   ("thread_notice_off", False),
                   ("delete_batch_limit", 35),
                   # 逐条轨迹的开关。默认关
                   #   （与逐字输出同一条判据：口碑五五开，别替用户决定）。
                   ("trace_full", False),
                   # 工作区钩子的信任门。默认关 ——
                   #   那几份配置跟着仓库走，打开 = 别人写的脚本会在本机跑。
                   #   读侧是 `core/hooks._project_allowed()`（`is True`，现读）。
                   ("hooks_allow_project", False),
                   # 工作区 `lsp.json` 的信任门。
                   #   与上面那条同一条判据：那份文件跟着仓库走，而它写的是
                   #   任意命令，默认关。读侧 `core.code_intel._project_allowed()`
                   #   （`is True`，现读；关着但文件在会印一行说明）。
                   ("lsp_allow_project", False),
                   # 工作区插件包的信任门（Hooks 与 `.mcp.json`）。默认关 ——
                   #   包跟着仓库走，而这两个组件都是"在你的机器上执行东西"。
                   #   用户自己机器上的包根（`~/.agents/plugins` 等）不看这个开关。
                   #   读侧 `core/plugin_packages.workspace_plugins_allowed()`
                   #   （`is True`，现读；关着但文件在会印一行说明）。
                   ("plugins_allow_workspace", False)):
        settings.setdefault(_k, _v)
    for _k, _v in (("memory_extract_every_n_turns", 20), ("recap_after_minutes", 30),
                   ("sliding_window_gap_minutes", 90),
                   ("approval_timeout_seconds", 0)):
        settings.setdefault(_k, _v)
    #: 两份「全部放行」名单的当前状态：`"*" in list_for(kind)` 算出来的
    #:  （`core/allowlist.py` 判据 ②）—— 界面不自己算，也不用另存一个布尔。
    try:
        from core.allowlist import list_for as _al_list
        settings["allowlist_egress_all"] = "*" in _al_list("egress")
        settings["allowlist_command_all"] = "*" in _al_list("command")
    except Exception:
        pass
    #: 自动审查的长期规则（`auto_review_rules`）—— 三段：允许 / 禁止 / 环境。
    #: 没写过就回三段空表（界面按三段渲染）。不在这里补兜底规则：出厂默认
    #: 就是"没有规则"，那套默认住在 `core/auto_review.py` 的常量里。
    #: 存量坏值（旧形状 / 段名拼错），回三段空表；读取那一侧会出声一次。
    try:
        from core.auto_review import normalize_rules as _norm_rules
        settings["auto_review_rules"] = _norm_rules(
            settings.get("auto_review_rules") or {})
    except Exception:
        settings["auto_review_rules"] = {
            "allow": [], "deny": [], "environment": []}
    # 总空间上限（设置页的「备份空间大小」）
    # 与上面那条同形：缺的键填兜底值本身，生效值由后端算好交出去
    #   （界面不自己判 —— 判两份就会漂）。
    try:
        from core.file_history import (DEFAULT_TOTAL_MB, TOTAL_SETTING_KEY,
                                       configured_max_mb)
        if not isinstance(settings.get(TOTAL_SETTING_KEY), int):
            settings[TOTAL_SETTING_KEY] = DEFAULT_TOTAL_MB
        settings["file_history_effective_max_mb"] = configured_max_mb()
    except Exception:
        pass
    # 投入档 —— 设置页那张卡片要的整张档表
    # 不许让界面抄一份档名（那会是第二套真相源）：这里把 `core/tiers.py::TIERS`
    #   原样交出去，界面只负责画。加一档时这里自动跟着长。
    # 与 ICE 那两块同一条规矩：派生的值（`effort_effective`）由后端算好，
    #   界面不自己判 —— 判两份就会漂。
    # `configured_level()` 会抛 `TierUnsupported`（"读到了但做不到"那条），
    #   而这里是读接口，不许让 500 冒出去，把话原样带给界面（"拒，不静默"）。
    try:
        from core import tiers
        settings["effort_levels"] = [
            {"level": tiers.TIERS[i].level,
             "name": tiers.TIERS[i].name,
             "implemented": tiers.TIERS[i].implemented}
            for i in sorted(tiers.TIERS)]
        try:
            settings["effort_level"] = tiers.configured_level()
            settings["effort_error"] = ""
        except Exception as exc:          # TierUnsupported 及其它
            settings["effort_level"] = None
            settings["effort_error"] = str(exc)
    except Exception:
        pass
    # 模式轴：同一条规矩 —— 不许让界面抄一份模式名
    # 把 `core/modes.py` 的表原样交出去，界面只负责画。 `implemented` 也交出去
    # （未实现的档要在界面上禁用）。
    try:
        from core import modes
        settings["modes"] = [
            {"key": m.key, "name": m.name, "desc": m.desc,
             "implemented": m.implemented,
             # 设置页那个「默认档」不许选完全权限
             #   （只许 `/mode` 按会话开）—— 判据在后端，界面只负责过滤。
             "default_allowed": m.default_allowed}
            for m in modes.MODES.values()]
        try:
            settings["mode"] = modes.configured_mode()
            settings["mode_error"] = ""
        except Exception as exc:
            settings["mode"] = None
            settings["mode_error"] = str(exc)
    except Exception:
        pass
    # 框架模式预设 —— 同一条规矩，界面不抄名单
    # 交出去两样：目录（画四张卡）与当前是哪个（判据在后端，
    #   见 `core/framework_preset.py`）。界面不许自己拿三项值去比 ——
    #   那正是"会撒谎的派生显示"（手改成正好等于 `Fern`，界面写 `Fern`）。
    try:
        from core import framework_preset as _fp
        settings["framework_presets"] = _fp.catalog()
        settings["framework_preset"] = _fp.preset_of(settings)
        settings["framework_preset_custom"] = _fp.CUSTOM
    except Exception:
        pass
    # 项目指令文件的大小 —— 设置页那一行
    # 软告警有两处出口：「设置页一行 + 启动日志一行」；日志那半
    #   超阈值 `print` 一行、进 `/api/logs`，这一半是这条。
    # 读不到 ≠ 0：`leaf_md_status()` 在本进程还没装载过时
    #   返回 `chars=0`，那看起来像"0 字符的文件"，多带一个 `loaded` 让界面分得开
    #   （「没有读数」不等于「可以清空」）。
    # 判据（超没超）由后端给（`over`）—— 界面不自己拿两个数去比（判两份就会漂）。
    try:
        from core.prompt_assembler import leaf_md_status
        _leaf = leaf_md_status()
        _leaf["loaded"] = bool(_leaf.get("files"))
        settings["leaf_md"] = _leaf
    except Exception:
        pass
    # 子 agent 用哪个模型 —— 当前值 + 可选项
    # 可选项由后端给（`core.subagent_model.available()`），界面不自己扫目录 ——
    #   判两份就会漂（同上面 ICE / 档表那几条）。
    # 可选项就是端配置页里那些配置条目
    #   （`[{id, name, card_label, model}]`，不是 `providers/` 的目录名）——
    #   与子 agent 派发时那条（`model_configs.instance_for`）同一份账本
    #   （一个口径一个地方）。
    # 外加一句 `subagent_model_legacy`：老版本存的是目录名，今天解析不到 ——
    #   静默忽略一个用户改过的键比不许改更坏（`core/tiers.py` 那条），
    #   所以把它说出来（非空 = 该提示；界面据此显示一行警告）。
    try:
        from core.subagent_model import available as _sub_avail
        from core.subagent_model import configured_model as _sub_cur
        from core.subagent_model import legacy_note as _sub_legacy
        _cur = _sub_cur(settings)
        settings["subagent_model"] = _cur
        settings["subagent_model_options"] = _sub_avail()
        settings["subagent_model_legacy"] = _sub_legacy(_cur)
    except Exception:                                           # noqa: BLE001
        pass
    return jsonify(settings)


@app.route("/api/i18n", methods=["GET"])
def api_i18n():
    """界面语言包 —— 前端 `t()` 查这一份。

    形状 `{ok, lang, pack}`，`pack` = `{中文原文: English}`。
    读不到 / 当前就是源码语言，`pack: {}` —— 前端 `t()` 回落原文，
      与后端 `core/i18n.t()` 逐字同一条判据（永不显示裸键）。
    不缓存（没有 ETag）：语言包几 KB、只在页面加载时拉一次，
      而加一层缓存就要回答"设置改了谁去失效它"——现在答案是不需要。
    """
    if not _check_token():
        return _forbidden()
    from core import i18n as _I18N
    lang = _I18N.current()
    return jsonify({"ok": True, "lang": lang, "pack": _I18N.pack_for(lang)})


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json() or {}
    settings = _read_settings()

    # 框架模式预设 —— 「转自定义」的唯一一处
    #
    # 规则：选择预设时覆盖对应设置；只要更改过其中一个就转为自定义，
    # 即使再调回预设同值也是自定义。
    #
    # 判据是事实不是值 —— 三项凑巧等于 `Fern` 的手改，与"选了 `Fern`"
    #    在值上完全一样（两个不同的东西长得一样），必须存那个事实。
    #    所以所以这里的规则是结构性的，不是比较：
    #      · 载荷里有 `framework_preset`，覆盖那三格 + 记下那个名字；
    #      · 载荷里有三格中的任意一格、而没有 `framework_preset`，记 `custom`。
    #    第二条不看值（这就是"调回同值也算改过"的落法）。
    from core.framework_preset import (CUSTOM, GOVERNED_KEYS, NAMES,
                                       SETTING_KEY as _PRESET_KEY, values_of)
    _preset_applied = False
    if _PRESET_KEY in data:
        _want = data[_PRESET_KEY]
        if _want not in NAMES:
            #: 界面上那排只列得出合法值，这条正常点不出来（
            #:   「这条通知不要了」），人话只留一句，收到的值进控制台。
            print("[settings] framework_preset 不合法：%r（可选 %s）"
                  % (_want, " / ".join(NAMES)))
            return jsonify({"ok": False, "error": tr("预设无效")}), 400
        settings.update(values_of(_want))
        settings[_PRESET_KEY] = _want
        _preset_applied = True

    # 投入档
    #
    # 它不进下面那个通用白名单：那一圈是"照单收下"，而档位是唯一一根
    # 进提示缓存键的设置（那一次 45x 缓存尖峰），非法值必须当场拒，
    # 不许落盘、不许静默降级（`validate_level` 是唯一判定处，见 `core/tiers.py`）。
    # 改档不会动当前这一段对话（`current_level` 冻结在会话上）——
    # 冷启动提示是 UI 的活，这里只负责"值本身要么对、要么拒"。
    if "effort_level" in data:
        from core.tiers import TierUnsupported, validate_level
        try:
            settings["effort_level"] = validate_level(data["effort_level"])
        except TierUnsupported as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    # 模式轴
    # 同一条规矩：非法值当场拒（400），不许落盘让读取端去猜。
    # 未实现的档也拒 —— 存进去就等于界面写着"自动"而它什么都不做，
    #    那正是「标签在说谎」。列得出、设不了。
    if "default_mode" in data:
        from core.modes import (SETTING_KEY as _MODE_KEY, MODE_NAMES,
                                ModeUnsupported, is_implemented, mode_of)
        try:
            _mode_key = mode_of(data["default_mode"])
        except ModeUnsupported as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not is_implemented(_mode_key):
            return jsonify({"ok": False, "error":
                            tr("「%s」还没做好，暂时不能选。")
                            % MODE_NAMES.get(_mode_key, _mode_key)}), 400
        # 完全权限只能 `/mode` 按会话开，
        #   不许当默认档 —— 默认档会跟着"新会话 / 重启"静默生效，
        #   而"不问而放"最不该静默。判据在后端（界面只是不列它）。
        from core.modes import is_default_allowed
        if not is_default_allowed(_mode_key):
            return jsonify({"ok": False, "error":
                            "「%s」不能设为默认模式。请在对话里用 /mode %s，"
                            "仅对当前对话开启"
                            % (MODE_NAMES.get(_mode_key, _mode_key), _mode_key)}), 400
        settings[_MODE_KEY] = _mode_key

    # 出网代理
    # 与档位同一条规矩：非法值当场拒（400），不许落盘让读取端去猜。
    # `proxy_url` 留空 = 不改（与端配置的 api 那一格同一条）——
    #    GET 给的是打码值，把它原样存回去就会把真凭据换成 `***@host`。
    if "proxy_mode" in data:
        from core import net_proxy as _NP
        _pm = str(data.get("proxy_mode") or "").strip().lower()
        if _pm not in _NP.MODES:
            return jsonify({"ok": False, "error": (
                "proxy_mode 只能是 %s（收到 %r）" % (" / ".join(_NP.MODES), _pm))}), 400
        #: 「指定」却没有地址，拒（哪怕旧地址是空的、也没在同一批里给）。
        #:   放过去的话，`httpx_kwargs()` 会回落到跟随系统，而界面上写着
        #:   「指定」—— 那又是一处"标签与行为不一致"。宁可在这里把话说清楚。
        if _pm == _NP.MODE_MANUAL:
            _u = _NP.normalize_url(str(data.get(_NP.SETTING_URL)
                                       or settings.get(_NP.SETTING_URL) or ""))
            if not _u:
                return jsonify({"ok": False, "error": (
                    tr("指定格式下需要代理地址，否则保持跟随系统"))}), 400
            _why = _NP.validate_url(_u)
            if _why:
                return jsonify({"ok": False, "error": _why}), 400
        settings[_NP.SETTING_MODE] = _pm
    if "proxy_url" in data:
        from core import net_proxy as _NP
        #: `127.0.0.1:7890` 与 `http://127.0.0.1:7890` 都收（WinINET 的
        #:   `ProxyServer` 本来就不带 scheme，用户照抄那个形状最省事）。
        _pu = _NP.normalize_url(str(data.get("proxy_url") or ""))
        if _pu:
            why = _NP.validate_url(_pu)
            if why:
                return jsonify({"ok": False, "error": why}), 400
            settings[_NP.SETTING_URL] = _pu

    # 后台记忆抽取的节流旋钮
    # 与档位同一条规矩：非法值当场拒（400），不许落盘让读取端去猜。
    # `isinstance(True, int)` 是 True，bool 必须单独拒。
    if "memory_extract_every_n_turns" in data:
        from core.memory_extract import THROTTLE_MAX, THROTTLE_MIN
        v = data["memory_extract_every_n_turns"]
        if isinstance(v, bool) or not isinstance(v, int) \
                or not (THROTTLE_MIN <= v <= THROTTLE_MAX):
            return jsonify({"ok": False, "error": (
                f"memory_extract_every_n_turns 要是 {THROTTLE_MIN}–{THROTTLE_MAX} "
                f"之间的整数（收到 {v!r}）")}), 400
        settings["memory_extract_every_n_turns"] = v

    # 上下文滑窗的冷闸
    # 开关收 bool（坏类型拒，不许落盘让读取端去猜）；闸值与节流同一条规矩。
    if "context_sliding_window" in data:
        v = data["context_sliding_window"]
        if not isinstance(v, bool):
            return jsonify({"ok": False, "error": (
                f"context_sliding_window 要是 true / false（收到 {v!r}）")}), 400
        settings["context_sliding_window"] = v
    if "sliding_window_gap_minutes" in data:
        from core.memory.manager import (COLD_WINDOW_GAP_MAX_LIMIT,
                                         COLD_WINDOW_GAP_MIN_LIMIT)
        v = data["sliding_window_gap_minutes"]
        if isinstance(v, bool) or not isinstance(v, int) \
                or not (COLD_WINDOW_GAP_MIN_LIMIT <= v <= COLD_WINDOW_GAP_MAX_LIMIT):
            return jsonify({"ok": False, "error": (
                f"sliding_window_gap_minutes 要是 "
                f"{COLD_WINDOW_GAP_MIN_LIMIT}–{COLD_WINDOW_GAP_MAX_LIMIT} "
                f"之间的整数（收到 {v!r}）")}), 400
        settings["sliding_window_gap_minutes"] = v

    # 回来时的「我们到哪了」
    # 开关 + 触发时间，与上面那条（滑窗冷闸）同一条规矩：
    # 坏类型当场拒（400），不许落盘让读取端去猜。
    # 读取端（`core/session_recap.py`）对"读不到 / 坏值"一律回默认
    #    （默认关）—— 那是给"文件坏了"兜底用的，不是给设置页用的：
    #    设置页送什么就是什么，送错就拒。
    if "recap_enabled" in data:
        v = data["recap_enabled"]
        if not isinstance(v, bool):
            return jsonify({"ok": False, "error": (
                f"recap_enabled 要是 true / false（收到 {v!r}）")}), 400
        settings["recap_enabled"] = v
    # 「50 条存储提醒」的不再提醒。
    #   它是一条持久化的"不许"，必须有一个看得见的撤回口
    #   （设置页那一格，`packages/ui/src/settings-view.js`）——
    #   `persistent-deny-needs-a-review-path`：不许写成"存了就再也回不来"。
    if "thread_notice_off" in data:
        v = data["thread_notice_off"]
        if not isinstance(v, bool):
            return jsonify({"ok": False, "error": (
                f"thread_notice_off 要是 true / false（收到 {v!r}）")}), 400
        settings["thread_notice_off"] = v
    if "recap_after_minutes" in data:
        from core.session_recap import (MAX_MINUTES as _R_MAX,
                                        MIN_MINUTES as _R_MIN)
        v = data["recap_after_minutes"]
        if isinstance(v, bool) or not isinstance(v, int) \
                or not (_R_MIN <= v <= _R_MAX):
            return jsonify({"ok": False, "error": (
                f"recap_after_minutes 要是 {_R_MIN}–{_R_MAX} 之间的整数"
                f"（收到 {v!r}）")}), 400
        settings["recap_after_minutes"] = v

    # 记忆的两把开关
    # 与上面两个开关同一条规矩：坏类型当场拒（400），不许落盘让读取端去猜。
    # 只有 `bool` 合法 —— `"true"` / `1` 一律拒（读取端把非 bool 当"默认"，
    #    落盘一个字符串等于让设置页显示的和实际生效的对不上）。
    # `memory_write` 的读取端与工具拒绝话术都要有对应入口，
    #   否则工具会说"去设置页打开"，而设置页上没有那个控件。
    # `/rewind` 要不要连工作区文件一起退。
    #   同一条规矩：只收真 bool（读取端 `file_history.restore_enabled`
    #   把非 bool 当"默认开"，落盘一个字符串会让设置页显示的和实际生效的对不上）。
    if "rewind_restore_files" in data:
        _v = data["rewind_restore_files"]
        if not isinstance(_v, bool):
            return jsonify({"ok": False, "error": (
                f"rewind_restore_files 要是 true / false（收到 {_v!r}）")}), 400
        settings["rewind_restore_files"] = _v

    for _mem_key in ("memory_write", "memory_organize", "memory_auto_seal"):
        if _mem_key in data:
            _v = data[_mem_key]
            if not isinstance(_v, bool):
                return jsonify({"ok": False, "error": (
                    f"{_mem_key} 要是 true / false（收到 {_v!r}）")}), 400
            settings[_mem_key] = _v

    # ICE：本地安全模块
    # 它是本文件里第一格嵌套的设置（`ice.enabled` 总闸 / `ice.info_protection` 信息层）
    #   —— 目标形态里 ICE 就是一个带子开关的总闸。
    # 与上面几把开关同一条规矩：只收真 bool，坏类型当场拒（400），
    #   不许落盘让读取端去猜（读取端把非 bool 当"兜底"，设置页显示的和实际生效的对不上）。
    # 收哪几个键从 `core.ice.ICE_SWITCH_KEYS` 读 —— 不在这里另抄一份（漏抄一个键
    #   的后果是"界面上能改、落盘不了"，而两边都不报错）。
    if "ice" in data:
        incoming = data["ice"]
        if not isinstance(incoming, dict):
            return jsonify({"ok": False, "error": (
                f"ice 要是一个对象（收到 {incoming!r}）")}), 400
        from core.ice import ICE_SWITCH_KEYS
        tree = settings.get("ice")
        tree = dict(tree) if isinstance(tree, dict) else {}
        for _ice_key in ICE_SWITCH_KEYS:
            if _ice_key not in incoming:
                continue
            _v = incoming[_ice_key]
            if not isinstance(_v, bool):
                return jsonify({"ok": False, "error": (
                    f"ice.{_ice_key} 要是 true / false（收到 {_v!r}）")}), 400
            tree[_ice_key] = _v
        settings["ice"] = tree

    # 改前留档的保留期
    # 单独一处校验（不并进下面那个"照单全收"的循环）：它是一个数
    #    且越界要拒 —— 而 `isinstance(True, int)` 是 `True`，布尔必须单独挡
    #    （`effort_level: true` 会被当成档 1）。
    if "file_history_days" in data:
        _fh = data["file_history_days"]
        if isinstance(_fh, bool) or not isinstance(_fh, int) or not (1 <= _fh <= 3650):
            return jsonify({"ok": False, "error": (
                f"file_history_days 要是 1~3650 的整数（收到 {_fh!r}）")}), 400
        settings["file_history_days"] = _fh

    # 总空间上限（单位 MiB）
    # 与上面那条同一套理由（一个数、要拒越界、布尔要单独挡）。
    # 上限取 `core.file_history` 里那两个常量，不在这里再抄一遍数字
    #    （抄了就是第二套真相源，而它们会漂）。
    if "file_history_max_mb" in data:
        from core.file_history import MAX_TOTAL_MB, MIN_TOTAL_MB
        _fm = data["file_history_max_mb"]
        if (isinstance(_fm, bool) or not isinstance(_fm, int)
                or not (MIN_TOTAL_MB <= _fm <= MAX_TOTAL_MB)):
            return jsonify({"ok": False, "error": (
                f"file_history_max_mb 要是 {MIN_TOTAL_MB}~{MAX_TOTAL_MB} 的整数"
                f"（单位 MiB，收到 {_fm!r}）")}), 400
        settings["file_history_max_mb"] = _fm

    # 审批超时秒数
    #
    # 处理的是"用户离开了，等待中的一轮却老是断掉"。
    # 与档位/节流同一条规矩：非法值当场 400 并说清，不静默夹取。
    # `default_mode` 不在这里 —— 它上面早就有一块（模式轴那节，
    #   校验更严：`mode_of` + `is_implemented`）。
    #   只 grep 下面那张表、没看函数别处，就会误以为它没有被收
    #   所以"我没查到"不等于"没有"。
    #   再加一块就成了同一件事两处声明 —— 那正是本仓库反复吃亏的形状。
    if "approval_timeout_seconds" in data:
        _at = data["approval_timeout_seconds"]
        if (isinstance(_at, bool) or not isinstance(_at, int)
                or not (0 <= _at <= 3600)):
            #: 用户拿到一句人话；收到的值进控制台（排障要它，界面不要它）。
            print("[settings] approval_timeout_seconds 不合法：%r" % (_at,))
            return jsonify({"ok": False, "error":
                            tr("审批等待时间为 0 到 3600 之间的整数秒")}), 400
        settings["approval_timeout_seconds"] = _at

    # 回答语言
    # 取值只有三种：`"zh"` / `"en"` / 空串（= 清掉、跟随系统）。
    # 与档位/模式同一条规矩：认不出当场 400，不许落盘让读取端去猜。
    # 它不影响本会话（本会话由 `/language` 覆盖）—— 这里写的是
    #    「以后新会话的默认」，与 `/effort` 的分工逐字相同。
    if "response_language" in data:
        from core import response_language as _RL
        _raw = data["response_language"]
        _want = str(_raw or "").strip()
        if _want == "":
            settings["response_language"] = None      # 事实：没设过
        else:
            _lang = _RL.normalize(_want)
            if _lang is None:
                return jsonify({"ok": False, "error": (
                    "response_language 只认 zh / en，或者空串（跟随系统）；"
                    "收到 %r" % (_raw,))}), 400
            settings["response_language"] = _lang

    # 界面语言 —— 只收发现得到的包，认不出就拒（不静默夹取）
    # 空串 / `system` = 「跟随系统」。判据同「回答语言」：
    #    存的是事实（没设过），把键删掉，别存 `"system"` 那种伪值
    #    （否则以后改了"跟随"的定义，老设置里那一笔就成了历史包袱）。
    if "interface_language" in data:
        from core import i18n as _I18N
        _raw = data["interface_language"]
        _want = str(_raw or "").strip().lower()
        if not _want or _want == _I18N.SYSTEM:
            settings.pop("interface_language", None)
        else:
            _known = {c for c, _n in _I18N.available()}
            if _want not in _known:
                return jsonify({"ok": False, "error": (
                    tr("interface_language 只认 %s（或留空 = 跟随系统）；收到 %r")
                    % ("、".join(sorted(_known)), _raw))}), 400
            settings["interface_language"] = _want

    for key in ("auto_start_framework", "auto_start_channel", "boot_on_startup",
                 "memory_continuity",
                 # `freenote_mode` 从这份白名单里摘掉了
                 # —— 它随 freenote 拦截层一起剥出，框架里再没有读者
                 # （留着它 = 一个改了什么都不会发生的设置项）。
                 # 逐字输出的开关。`AgentLoopStage` 每次请求现读
                 # 所以改完当场生效，不需要重启。
                 "stream_output",
                 # 逐条轨迹。`trace.enabled()` 每次请求现读
                 #   所以改完当场生效，不需要重启（与 `stream_output` 同一取向）。
                 "trace_full",
                 "update_auto_check",
                 # `update_channel` 从这份白名单里摘掉了
                 # —— `core/updater.py` 从来没读过它（它的 `channel` 读
                 # `config.yaml`，而那条兜底又被构造默认值 `"stable"` 遮蔽），
                 # 留着它 = 一个存进去什么都不会发生的设置项。
                  "update_ignored_version", "update_last_check"):
        if key in data:
            settings[key] = data[key]
    # Workspace-related keys (v2)
    #: `work_groups_enabled` 不在这一列里了 ——
    #: 它退场了，而放行一个没人再读的键就是"写进去也不生效"。
    #: `work_groups` 留着（列表恒生效，设置页还在编辑它）。
    for ws_key in ("cross_zone_enabled", "auto_approve",
                    "work_groups",
                    "cascade_enabled", "cascade_auto_start", "cascade_notify",
                    "watchers", "forbidden_directories",
                    # 设置页「操作设置」那个危险档降级开关。
                    # 落 `settings.json`；`wm.load_from_settings`（本函数结尾会调）
                    # 把它同步到 SecurityGuard 上，当场生效。
                    "relax_command_tier",
                    # 设置页「只读命令免问」。同一条路
                    # —— 落 `settings.json`，`wm.load_from_settings` 同步到
                    # SecurityGuard 上，当场生效。默认 `false`（照常问）。
                    "no_ask_readonly"):
        if ws_key not in data:
            continue
        # `null` = 恢复默认：不留这个键，下一次 GET 会把默认那一批重新给出来。
        if ws_key == "forbidden_directories" and data[ws_key] is None:
            settings.pop("forbidden_directories", None)
            continue
        settings[ws_key] = data[ws_key]

    # 工作区钩子的信任门
    #
    # 只收真布尔：`core/hooks._project_allowed()` 判的是 `is True`
    #    —— 字符串 `"true"` 不算。放行一个非布尔值等于让开关看着开着、
    #    实际不执行（「标签在说谎」）；而这一格说的正是"你的仓库里的脚本
    #    可以跑"，它不许猜。
    # 权限解禁
    #
    # 只收真布尔：`core/unlock.py::_read()` 判的是 `value is True`
    #    —— `"true"` / `1` 一律当没写。放行非布尔值等于让开关
    #    "看着开着、实际不解禁"（「标签在说谎」）；而这一格说的是
    #    "完全权限下不再有任何阻力"，它不许猜。
    if "full_access_unlock" in data:
        _fu = data["full_access_unlock"]
        if not isinstance(_fu, bool):
            return jsonify({"ok": False, "error": (
                f"full_access_unlock 要是 true / false（收到 {_fu!r}）")}), 400
        settings["full_access_unlock"] = _fu

    if "hooks_allow_project" in data:
        _hp = data["hooks_allow_project"]
        if not isinstance(_hp, bool):
            return jsonify({"ok": False, "error": (
                f"hooks_allow_project 要是 true / false（收到 {_hp!r}）")}), 400
        settings["hooks_allow_project"] = _hp

    # 工作区 LSP 配置的信任门（与上面那条同一个形状：只收真布尔）。
    if "lsp_allow_project" in data:
        _lp = data["lsp_allow_project"]
        if not isinstance(_lp, bool):
            return jsonify({"ok": False, "error": (
                f"lsp_allow_project 要是 true / false（收到 {_lp!r}）")}), 400
        settings["lsp_allow_project"] = _lp

    # 工作区插件包的信任门（Hooks / MCP）。同一个形状：只收真布尔。
    if "plugins_allow_workspace" in data:
        _pw = data["plugins_allow_workspace"]
        if not isinstance(_pw, bool):
            return jsonify({"ok": False, "error": (
                f"plugins_allow_workspace 要是 true / false（收到 {_pw!r}）")}), 400
        settings["plugins_allow_workspace"] = _pw

    # 自动审查的长期规则（用户写在设置页）
    #
    # 只验形状（列表 / 字符串 / 条数 / 长度 / 单行）：规则的内容不管 ——
    #   写什么、指向哪里是用户自己的授权决定（填错由填写的人负责）。
    # 拒，不静默砍：静默丢掉一条 = 让用户以为写进去了。
    # 它管不到限位闸（出网命令族 / 受保护路径）—— 那句写在
    #   `core/auto_review.py::_USER_RULES_TEMPLATE` 里，分类器会看见。
    if "auto_review_rules" in data:
        from core.auto_review import normalize_rules
        try:
            settings["auto_review_rules"] = normalize_rules(data["auto_review_rules"])
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    # 批量删除的阈值（0 = 关掉这个闸）
    #
    # 必须验型，而且理由与 `file_history_max_mb` 那条不同：
    #    `_batch_delete_limit()` 自己的兜底是"坏值，回 30"（安全侧，对的），
    #    但在设置页这一层静默回 30 更坏 —— 用户以为自己关掉了（写了 0
    #    或写了个字符串），而实际还拦着，且没有任何东西会说（标签在说谎）。
    #    所以边界上拒，把话说到用户面前。
    if "delete_batch_limit" in data:
        # 键名从生产代码取，不在这里抄第二份（一个口径一个地方）。
        from skills.file_parser.handler import BATCH_DELETE_LIMIT_KEY
        _bl = data["delete_batch_limit"]
        if isinstance(_bl, bool) or not isinstance(_bl, int) or _bl < 0:
            return jsonify({"ok": False, "error": (
                f"delete_batch_limit 要是 >= 0 的整数（0 = 关闭批量删除的强制确认，"
                f"收到 {_bl!r}）")}), 400
        settings[BATCH_DELETE_LIMIT_KEY] = _bl

    # 子 agent 用哪个模型（用户可配）
    # 与档位同一条规矩：非法值当场拒（400），不许落盘让读取端去猜。
    # 空串是合法值（= 沿用会话那个模型，也是默认）—— 别写成"必须非空"。
    # 判据（认不认识这个名字）只有一处：`core.subagent_model.ids()`，
    #    与子 agent 派发时那条（`model_configs.instance_for`）用的是同一份账本。
    # 收的是配置条目 id（`m` + 8 位），
    #   不再是供应商目录名 —— 老值在这里会被拒（而不是静默存下去），
    #   而拒的话术里带了可选清单，用户当场就能改对。
    if "subagent_model" in data:
        from core.subagent_model import ids as _sub_ids
        from core.subagent_model import legacy_note as _sub_legacy2
        _sm = str(data["subagent_model"] or "").strip()
        _ids = _sub_ids()
        if _sm and _sm not in _ids:
            _hint = _sub_legacy2(_sm) or ""
            return jsonify({"ok": False, "error": (
                "子代理模型要留空（沿用当前模型），或选一条已有配置"
                "（可选：%s；收到 %r）%s"
                % ("、".join(_ids) or "一个都没有", _sm,
                   ("\n" + _hint) if _hint else ""))}), 400
        settings["subagent_model"] = _sm

    # 收尾：动了那三格里的任意一格，转 `custom`
    # 放在所有写入分支之后、`_write_settings` 之前 —— 它必须在
    #    "这一轮到底写了什么"已经定下来的时候判（记结果，不只记决定）。
    # 不看值：这正是"调回同值也算改过"的落法（见上面那段判据）。
    # 它不在那两个白名单循环里 —— 那两个是"照单收下"，而这是一条规则。
    if not _preset_applied and any(k in data for k in GOVERNED_KEYS):
        settings[_PRESET_KEY] = CUSTOM

    _write_settings(settings)

    # 收尾一处（三条路共用）：环境变量 / i18n 缓存 / ICE 单例 / 工作区 / 注册表。
    #    此前这段是内联的，于是「恢复默认」少做了两格（`boot_on_startup` 与 proxy）
    #    —— 见 `_apply_settings_side_effects` 的 docstring。
    _apply_settings_side_effects(settings, set(data))

    return jsonify({"ok": True})


@app.route("/api/settings/restore-default", methods=["POST"])
def api_restore_default_settings():
    """「恢复默认设置」—— 恢复成出厂基准那一份。

    基准 = `data/settings.example.json`（已跟踪的一份文件）。
      为什么要有它、以及它今天覆盖到哪儿，全在 `core/settings_baseline.py`。

    两段式，不许一键静默重置：
      · `{"dry_run": true}`，只报计划（将要改变哪几格 / 跳过哪几格），
        一个字节都不写 —— 界面拿它先把话说清楚
      · 不给、或 `false`，真写回

    有一类键永远不动（`core/settings_baseline.NEVER_RESTORE`）：
      登录令牌 · 邮箱账户与口令 · 工作组 · 监听器 · 规避目录 · 工作区路径。
      它们是用户数据/凭据，不是偏好 —— 恢复默认把它们抹掉，用户读到的
      是「点了一下『恢复默认』，我的东西没了」。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    body = request.get_json(silent=True) or {}
    dry = bool(body.get("dry_run"))
    try:
        from core.settings_baseline import plan_restore, restorable
    except Exception as e:                                     # noqa: BLE001
        return jsonify({"ok": False, "error": trf("读不到出厂基准：{e}", e)}), 500

    new, changed, skipped = plan_restore(_read_settings())
    out = {"ok": True, "dry_run": dry,
           "changed": changed,          # {键: [现在, 基准]} —— 只列真的会变的
           "skipped": skipped,          # 基准里有、但永远不许动的
           "baseline_keys": len(restorable())}
    if dry or not changed:
        out["applied"] = False
        return jsonify(out)

    _write_settings(new)
    # 与 `POST /api/settings` 同一条收尾（`_apply_settings_side_effects`）：
    #   新值要推进活着的那些单例，否则界面回一句"已恢复"，而实际行为要等重启才变
    #   —— 「标签在说谎」。这条收尾现在也管 `boot_on_startup`
    #   （此前恢复默认写回了 `false` 却不改注册表，开机照旧自启）。
    _apply_settings_side_effects(new, set(changed))
    out["applied"] = True
    return jsonify(out)


# 设置包：导出 / 导入
#
# 判据全在 `core/settings_transfer.py` 的 docstring 里（包里有什么 / 谁永不进包 /
#   机器相关项 / 口令）。这里只做三件事：鉴权 · 读写盘 · 收尾。
# 与「恢复默认」同一条安全轴：默认 `dry_run`（不传就是只看计划）。

@app.route("/api/settings/export", methods=["POST"])
def api_export_settings():
    """导出一个设置包（JSON，前端负责存成文件）。

    默认不含凭据；勾了就必须给口令 —— 口令丢了没有找回通道
      （scrypt 派生，包里没有可逆材料），这句话由界面负责说给用户听。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    body = request.get_json(silent=True) or {}
    include = bool(body.get("include_credentials"))
    password = str(body.get("password") or "")
    if include and not password:
        return jsonify({"ok": False, "error": (
            "当前会携带凭据导出，请设置口令")}), 400
    try:
        from core import settings_transfer as _ST
        bundle = _ST.build_bundle(
            _read_settings(),
            include_credentials=include,
            password=password,
            gui=body.get("gui") if isinstance(body.get("gui"), dict) else None,
            app_version=__version__,
        )
    except Exception as exc:                                   # noqa: BLE001
        return jsonify({"ok": False, "error": trf(
            "导出失败：{e}", "%s: %s" % (type(exc).__name__, exc))}), 500
    return jsonify({"ok": True, "bundle": bundle})


@app.route("/api/settings/import", methods=["POST"])
def api_import_settings():
    """导入一个设置包。两段式（默认 `dry_run=True`）。

    请求体：
      `{"bundle": {...}, "dry_run": true, "password": "", "selections": [...],
        "with_credentials": false, "with_allowlist": true, "with_model_configs": true}`

    `changed` 的形状与「恢复默认」逐字一致 —— 界面拿它一格格说清楚
      （`auto_approve` / `relax_command_tier` 也在里面，不许静默整表替换）。
    `selections` 只对机器相关项（`settings_transfer.MACHINE_KEYS`）生效，
      默认一项都不选：那是"这台机器"的事，其中 `boot_on_startup` 还会改注册表。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    body = request.get_json(silent=True) or {}
    bundle = body.get("bundle")
    dry = bool(body.get("dry_run", True))          # 不传 = 只看计划（安全轴）
    password = str(body.get("password") or "")
    selections = body.get("selections") or []
    if not isinstance(selections, list):
        selections = []
    with_credentials = bool(body.get("with_credentials"))
    with_allowlist = bool(body.get("with_allowlist", True))
    with_model_configs = bool(body.get("with_model_configs", True))

    try:
        from core import settings_transfer as _ST
    except Exception as exc:                                   # noqa: BLE001
        return jsonify({"ok": False, "error": trf(
            "读不到导入模块：{e}", "%s: %s" % (type(exc).__name__, exc))}), 500

    current = _read_settings()
    kw = dict(password=password, selections=selections,
              with_credentials=with_credentials,
              with_allowlist=with_allowlist,
              with_model_configs=with_model_configs)
    plan = _ST.plan_import(bundle, current, **kw)
    plan["dry_run"] = dry
    if not plan.get("ok"):
        plan["applied"] = False
        return jsonify(plan), 400
    if dry:
        plan["applied"] = False
        return jsonify(plan)

    try:
        result = _ST.apply_import(bundle, plan, current, **kw)
    except Exception as exc:                                   # noqa: BLE001
        return jsonify({"ok": False, "applied": False, "error": trf(
            "导入失败：{e}", "%s: %s" % (type(exc).__name__, exc))}), 500

    new = result.get("settings") or current
    written = (result.get("applied") or {}).get("written") or []
    if new != current:
        _write_settings(new)
        # 与另外两条路同一条收尾（注册表 / 代理 / ICE / i18n / 工作区）。
        _apply_settings_side_effects(new, set(written))
    plan["applied_detail"] = result.get("applied") or {}
    plan["applied"] = True
    return jsonify(plan)


# 持久白名单
#
# 这两个端点不持有任何状态。它们只是 `core/allowlist.py` 那一个文件的
# 读写口 —— 自定义条目可以来自对话时批准的、用户在 UI 加的、
# 或直接改文件来的。三处来源一个真相源就是那个文件
# （见 `core/allowlist.py` 的 docstring）。
#
# **`"*"` 不在这里做特殊处理** —— 它就是一个普通条目，加它就是"全部放行"，
# 删它就是"关掉"。UI 把它渲染成一行「全部」+ 一个开关，但开关的状态是从
# 列表算出来的（`"*" in list`），不是另存一个布尔 —— 否则同一件事有两个表示。

def _allowlist_state() -> dict:
    """给 UI 的完整状态：两个域 + 危险档开关。唯一的读取路径。"""
    from core.allowlist import KINDS, list_for
    state = {k: list_for(k) for k in KINDS}
    # 框架没在跑的时候也要给对：那个开关的真值在 guard 上
    # （`run_command` 每次现读它），框架没起来时 guard 不存在 ——
    # 那时读文件。两个来源指的是同一个设置，只是一前一后：
    # 有 guard 时 guard 是活的真相，没有时文件是唯一真相。
    relax = False
    no_ask = False
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        guard = getattr(wm, "security", None) if wm else None
        if guard is not None:
            relax = bool(getattr(guard, "relax_command_tier", False))
            # 同一个来源（guard 是活的真相）
            no_ask = bool(getattr(guard, "no_ask_readonly", False))
        else:
            relax = bool(_read_settings().get("relax_command_tier", False))
            no_ask = bool(_read_settings().get("no_ask_readonly", False))
    except Exception:
        relax = False
        no_ask = False
    # 从列表算出来，不另存 —— 见上面那段注释
    state["all_egress"] = "*" in state.get("egress", [])
    state["all_command"] = "*" in state.get("command", [])
    state["relax_command_tier"] = relax
    state["no_ask_readonly"] = no_ask
    # 批量删除的阈值 —— 走生产那个读函数，不在这儿重算一遍
    # （兜底方向、`0 = 关` 这些判据都住在它里面；重算就是第二套口径）。
    try:
        from skills.file_parser.handler import (
            _batch_delete_limit, BATCH_DELETE_LIMIT_KEY)
        state[BATCH_DELETE_LIMIT_KEY] = _batch_delete_limit()
    except Exception:
        state["delete_batch_limit"] = None      # 界面显示"读不到"，不假装是 0
    return state


@app.route("/api/allowlist", methods=["GET"])
def api_get_allowlist():
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    return jsonify(_allowlist_state())


@app.route("/api/allowlist", methods=["POST"])
def api_save_allowlist():
    """加一条 / 删一条。幂等（加已有的、删没有的都返回成功 + 新状态）。

    *subject* 不做任何规范化 —— 域名的 `www.`、命令的空白，都按用户输入的原样
    存（用户在对话里批准时写进去的是什么，这里就该是什么，
    否则"列表里看得见的那条"和"判定时比的那条"会不是同一个串）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json() or {}
    kind = str(data.get("kind") or "")
    action = str(data.get("action") or "")
    subject = str(data.get("subject") or "").strip()

    from core.allowlist import KINDS, add, remove
    if kind not in KINDS:
        return jsonify({"error": f"unknown kind: {kind!r}"}), 400
    if not subject:
        return jsonify({"error": "empty subject"}), 400
    if action not in ("add", "remove"):
        return jsonify({"error": f"unknown action: {action!r}"}), 400

    try:
        changed = add(kind, subject) if action == "add" else remove(kind, subject)
    except Exception as e:
        _log(f"[allowlist] {action} {kind}={subject!r} failed: {e}")
        return jsonify({"error": f"write failed: {e}"}), 500
    return jsonify({"ok": True, "changed": bool(changed), **_allowlist_state()})


@app.route("/api/cron", methods=["GET"])
def api_cron_list():
    """周期任务的列表视图（面板与测试按钮都用它）。

    只用 `cron_list` 那一个函数（解释规则只许在 `core/cron/expr.py` 一处）——
    界面自己再解释一遍规则就是第二套真相。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core.cron.tools import cron_list
    out = cron_list()
    out.pop("note", None)
    return jsonify(out)


@app.route("/api/cron/run", methods=["POST"])
def api_cron_run():
    """「立即执行一次」 —— 那颗测试按钮的后端。

    判据 ①：结果回到发起测试的这里（HTTP 响应 + 面板内联），
    不是任务配置的目标 —— 否则 `deliver="new"` 的任务点下去什么都看不见。
    所以这里注入一个空投递器，把正文原样返回给调用方。

    同步：与 `/api/chat` 同一个形状（那条也是同步跑完一整轮）。
    每一条判据（不推 `next_run`、标 `manual`、不累计退避）都在
    `CronRunner.run_now` 里，这里只是接线。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    job_id = str((request.get_json(silent=True) or {}).get("job_id") or "")
    if not job_id:
        return jsonify({"ok": False, "error": tr("要给 job_id")}), 400
    from core.cron.runner import get_cron_runner
    # 判据 ①：注入空投递器 —— 结果原样回这个响应，不发进任务的通道
    #   （否则 `deliver="new"` 的任务点下去什么都看不见）。
    # `isolate=True`：试跑不许碰用户的对话 ——
    #   它跑在框架自己铸的一段里，`storage_id` 回给浮窗，关窗时整段丢掉。
    out = get_cron_runner().run_now(job_id, deliverer=lambda _s, _t: None,
                                    isolate=True)
    return jsonify(out), (200 if out.get("ok") else 409)


@app.route("/api/cron/run/close", methods=["POST"])
def api_cron_run_close():
    """关掉试跑浮窗，把那次试跑的那一段整个丢掉。

    只删"我们造的"那一段 —— `storage_id` 是客户端给的，
    没有 `is_test_segment` 那道闸，这行接口就等于
    「凭一个字符串删掉用户的一段对话」。不认，拒（fail-closed）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    sid = str((request.get_json(silent=True) or {}).get("storage_id") or "")
    if not sid:
        return jsonify({"ok": False, "error": tr("要给 storage_id")}), 400
    from core.cron.runner import get_cron_runner
    runner = get_cron_runner()
    if not runner.is_test_segment(sid):
        return jsonify({"ok": False,
                        "error": tr("此对话不是试跑产生的，无法删除。")}), 403
    try:
        _session_pool.discard_thread(sid)
    except Exception as exc:                         # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}), 500
    runner.forget_test_segment(sid)
    return jsonify({"ok": True, "storage_id": sid})


@app.route("/api/cron/toggle", methods=["POST"])
def api_cron_toggle():
    """暂停 / 恢复一条任务（不删）。`cron_update` 工具不做 ——
    这件事归面板。"""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    body = request.get_json(silent=True) or {}
    job_id = str(body.get("job_id") or "")
    enabled = 1 if body.get("enabled") else 0
    if not job_id:
        return jsonify({"ok": False, "error": tr("要给 job_id")}), 400
    from core.cron.store import get_cron_store
    if not get_cron_store().update(job_id, enabled=enabled):
        return jsonify({"ok": False, "error": tr("没有这条任务：%s") % job_id}), 404
    return jsonify({"ok": True, "job_id": job_id, "enabled": bool(enabled)})


@app.route("/api/cron/preview", methods=["POST"])
def api_cron_preview():
    """面板的校验器 + 中文预览。

    判据：前端一个字节都不重写规则语义 —— 合法性、那句中文、下次是什么时候，
    全部出自 `core/cron/expr.py`（`preview_rule` 就是它的转发）。
    所以附带好处：表单上那句错误消息与工具收到的是同一句（带路线的那句）。

    `ok=true` 而 `next_run=null` 不是错误（`once` 的时刻过去了）——
    "下一次是什么时候"与"规则合不合法"是两个问题。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core.cron.tools import preview_rule
    body = request.get_json(silent=True) or {}
    out = preview_rule(body.get("rule"))
    return jsonify(out), (200 if out.get("ok") else 400)


@app.route("/api/cron/create", methods=["POST"])
def api_cron_create():
    """面板建任务。

    渠道从这儿来：面板不是一个 agent 轮次，请求快照上什么都没有
    所以工具那条路（`current_channel()`）在这里必然取不到。
    所以端点自己把渠道交给 `create_job(..., channel=...)`，而校验/话术/落库
    全都是同一份实现（两个入口不是两套逻辑）。

    `channel_key_of(_web_session())` —— 不许写字面量（`_web_session` 的
    docstring 记着那次真事故：标志置在没人读的键上，接口还谎报成功）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core.cron.tools import create_job
    from core.im_routing import channel_key_of
    body = request.get_json(silent=True) or {}
    out = create_job(title=body.get("title"), prompt=body.get("prompt"),
                     rule=body.get("rule"),
                     deliver=body.get("deliver") or "here",
                     channel=channel_key_of(_web_session()))
    return jsonify(out), (200 if out.get("success") else 400)


@app.route("/api/cron/delete", methods=["POST"])
def api_cron_delete():
    """面板删任务。走的是同一个 `cron_delete`（工具与端点不分家）。"""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core.cron.tools import cron_delete
    body = request.get_json(silent=True) or {}
    out = cron_delete(body.get("job_id"))
    return jsonify(out), (200 if out.get("success") else 404)


@app.route("/api/memory", methods=["GET"])
def api_memory_list():
    """记忆库的列表视图 —— 条目 + 删除账本。

    判据："让用户自己看/改"比"给开关"强 —— 开关是全有或全无，
    而记忆真正的问题是"这一条记错了"。
    列表不带正文（正文最大 4,000 字符/条）—— 点开某一条再单独取
    （`GET /api/memory/entry?name=…`）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core import memory_store as MS
    entries = []
    for e in MS.list_entries():
        try:
            st = e["path"].stat()
            updated = int(st.st_mtime)
        except OSError:
            updated = 0
        entries.append({
            "slug": e["slug"],
            "name": e["name"],
            "description": e["description"],
            "chars": len(e["body"]),
            "updated": updated,
        })
    return jsonify({
        "ok": True,
        "entries": entries,
        "tombstones": MS.list_tombstones(),
        "limits": {"name": MS.MAX_NAME_CHARS, "description": MS.MAX_DESC_CHARS,
                   "body": MS.MAX_BODY_CHARS},
    })


@app.route("/api/memory/entry", methods=["GET"])
def api_memory_entry():
    """读一条的全文（列表点开时才拉）。"""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core import memory_store as MS
    name = str(request.args.get("name") or "").strip()
    entry = MS.read_entry(name) if name else None
    if entry is None:
        return jsonify({"ok": False, "error": trf("没有名为「{name}」的记忆", name)}), 404
    return jsonify({"ok": True, "slug": entry["slug"], "name": entry["name"],
                    "description": entry["description"], "content": entry["body"]})


@app.route("/api/memory/entry", methods=["POST"])
def api_memory_write():
    """新建 / 更新一条。超限一律拒（`memory_store` 纪律 ⑤：不静默砍）。"""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core import memory_store as MS
    data = request.get_json() or {}
    result = MS.write_entry(
        str(data.get("name") or ""),
        str(data.get("description") or ""),
        str(data.get("content") or ""),
    )
    # 关着写记忆开关时 `write_entry` 会给一条"去设置页打开"的路线 ——
    #    原样透出去（这里不替用户绕过开关）。
    code = 200 if result.get("success") else 400
    return jsonify({"ok": bool(result.get("success")), **result}), code


@app.route("/api/memory/delete", methods=["POST"])
def api_memory_delete():
    """删一条（用户动作）：记 tombstone + 删文件。

    不可逆（内容不保留）—— 界面那边必须先问一次（`confirm`）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core import memory_store as MS
    data = request.get_json() or {}
    result = MS.delete_entry(str(data.get("name") or ""))
    code = 200 if result.get("success") else 400
    return jsonify({"ok": bool(result.get("success")), **result}), code


@app.route("/api/memory/clear", methods=["POST"])
def api_memory_clear():
    """清空记忆库（用户动作）：全部删 + 全部落封条。

    与逐条删同一条语义（`MS.delete_entry` 的那两步），只是成批 ——
    见 `MS.delete_all` 里那条"为什么不循环调 `delete_entry`"。
    落封条是这件事的一半：只删文件的话，那批名字下一条后台抽取
    还能重生（tombstone 就是防这个的），用户看到的是
    「我清空了、它又回来了」。
    不可逆（内容不保留）。后路是逐条 `POST /api/memory/restore` 解除封条
    —— 但那只解除"这个名字不许再写"，内容不会回来。
    界面那边必须先问一次（5 秒倒计时），这里不代它做决定。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core import memory_store as MS
    result = MS.delete_all()
    code = 200 if result.get("success") else 400
    return jsonify({"ok": bool(result.get("success")), **result}), code


@app.route("/api/memory/restore", methods=["POST"])
def api_memory_restore():
    """解除封条（撤销一次误删）。它不恢复内容（内容删除时就没了）。"""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    from core import memory_store as MS
    data = request.get_json() or {}
    result = MS.remove_tombstone(str(data.get("name") or ""))
    code = 200 if result.get("success") else 400
    return jsonify({"ok": bool(result.get("success")), **result}), code


@app.route("/api/memory/export", methods=["POST"])
def api_memory_export():
    """导出一个记忆包（zip）—— 记忆库子页顶栏那两个按钮之一。

    默认带：长期事实 + 会话笔记 + 删除账本（不带它，删掉的事实会在新机器复活）。
    `include_history=true` 才带 `short_term.db`（聊天历史，含工具结果原文）。
    ❌ 日报（`logs/`）不带 —— 新机器自己生成。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    body = request.get_json(silent=True) or {}
    try:
        from core import memory_transfer as MT
        data = MT.build_zip(include_history=bool(body.get("include_history")),
                            app_version=__version__)
    except Exception as exc:                                   # noqa: BLE001
        return jsonify({"ok": False, "error": trf(
            "导出失败：{e}", "%s: %s" % (type(exc).__name__, exc))}), 500
    import time as _time            # 本文件不全局 import time（只在少数几处用）
    resp = Response(data, mimetype="application/zip")
    resp.headers["Content-Disposition"] = (
        'attachment; filename="crannia-memory-%s.zip"'
        % _time.strftime("%Y%m%d-%H%M%S"))
    return resp


@app.route("/api/memory/import", methods=["POST"])
def api_memory_import():
    """导入一个记忆包（multipart：字段 `file`）。

    两段式：`dry_run` 默认真（不传就是只看计划）——
      返回 `conflicts`（逐条列名字：facts 是提示注入面，不许只给一个总数）。
    真导入必须显式给 `on_conflict`：`overwrite`（覆盖）或 `skip`（跳过冲突 = 保留本地）。
      取消在界面那一侧（根本不发这个请求）。
    `with_history=true` 才把包里的聊天历史并行进本机（按主键去重，保留本地）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    upload = request.files.get("file")
    data = upload.read() if upload is not None else b""
    if not data:
        return jsonify({"ok": False, "error": "需要上传（.zip）"}), 400
    dry = (request.form.get("dry_run") or "1").strip().lower() not in (
        "0", "false", "no", "off")
    on_conflict = (request.form.get("on_conflict") or "").strip().lower()
    with_history = (request.form.get("with_history") or "").strip().lower() in (
        "1", "true", "yes", "on")
    try:
        from core import memory_transfer as MT
    except Exception as exc:                                   # noqa: BLE001
        return jsonify({"ok": False, "error": trf(
            "读不到导入模块：{e}", "%s: %s" % (type(exc).__name__, exc))}), 500
    if dry:
        out = MT.inspect_zip(data)
        out["dry_run"] = True
        return jsonify(out), (200 if out.get("ok") else 400)
    out = MT.apply_zip(data, on_conflict=on_conflict, with_history=with_history)
    out["dry_run"] = False
    return jsonify(out), (200 if out.get("ok") else 400)


# 历史 —— 三个只读端点

# 它们的存在理由：桌面端要「右侧竖排栏 → 历史面板」，
# 而没有这些端点，界面没有任何一条路能读到存着的对话 ——
# 「重启后对话记录就消失了」是前端/端点的缺口，不是数据没了
# （库里 1,344 行、19 条线程，每行都有 `created_at`）。
#
# 三条写在案的判据：
#   ① 游标是 `id` 一个数（不是 `(created_at, id)`）—— 见 `load_message_rows`。
#   ② 不用 `load_session` —— 那条路按预算裁剪、跳过 tool 消息，
#      是「装进模型上下文」的口径；界面要的是存着的全部行。
#      也不受 `memory_continuity` 管辖（那是装载预算，不是存储）。
#   ③ 模型永远不知道用户翻过历史 —— 这三条路只读库、
#      一个字节都不进 `messages`、不触发压缩、不发事件。


def _mem():
    """端点取记忆管理器的唯一口。

    走注册表（`bus.registry.get_memory_manager`）而不是模块全局 ——
      因为 `history_search` 走的就是注册表，同一个来源
      （两处各拿各的 = 「一个口径两个地方」）。
      全局只作兜底：框架没起来时注册表是空的。
    """
    from bus.registry import get_memory_manager
    return get_memory_manager() or _memory_manager


def _desktop_thread(st, storage_id: str):
    """这个 storage_id 是不是本通道的线程 → `(ok, 说明)`。

    名册已经只会给出本通道的 id，但端点收到的是客户端传来的 id ——
      不核对的话，「会话只列桌面端的」就只管住了那一屏、管不住这条读的路。
    名册外的 id（`thread_channel` 给 `None`）不放行、也不用「默认放行」：
      放行的代价是这条路能读到别的通道的行，而它换不来任何东西 ——
      界面手上的每个 id 都来自名册。
    """
    key = session_key_for(_web_session())
    ch = st.thread_channel(storage_id)
    if ch is None:
        return False, "这条会话不在名册里（旧会话，或这条线程还没登记）。"
    if ch != key:
        return False, "这条会话不属于这个通道。"
    return True, ""


@app.route("/api/threads", methods=["GET"])
def api_threads():
    """会话名册（新 → 旧）—— 桌面端"历史"那一栏的第一层。

    只列本通道：桌面端不跑 IM，别通道的线程
      在这儿既列不出也用不上。键由 `session_key_for(_web_session())` 推
      （不许再写字面量）。
    如实记一条键的性质：私聊在所有通道上是同一条键
      （`workspace:private`），这张名册在语义上是
      「工作区的私聊线程」，今天恰好等于「桌面端的」。IM 一旦真在跑，
      它的私聊也会落进这张表 —— 那不是 bug，是那条设计。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    mm = _mem()
    if mm is None or not getattr(mm, "short_term", None):
        return jsonify({"ok": False, "error": tr("记忆库未连接：框架未启动。")}), 503
    st = mm.short_term
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        return jsonify({"ok": False, "error": "limit 要是整数"}), 400
    key = session_key_for(_web_session())
    # 每条线程多两格 —— 这一段的工作目录。
    #   左栏要按目录分组，而这件事前端判不了：名册里原来
    #   根本不知道"这段对话用哪个目录"。后端给值，分组在前端做
    #   （同一个理由：后端算好的分组会随这次拉取一起过期）。
    #   判据只有一处（`session_workspace.describe`）—— 这里只搬运。
    from core import session_workspace as _sw
    threads = []
    for t in st.list_threads(key, limit=limit):
        sid = t.get("storage_id") or ""
        try:
            _ws = _sw.describe(sid)
            #: 实际落点：选了就是选的那个，没选就是默认工作区
            #: （没选目录的对话，它们归到"默认工作区"那个条目下）
            _ws["dir"] = _ws["path"] or str(_sw.resolve(sid) or "")
        except Exception:                                        # noqa: BLE001
            _ws = {"path": "", "is_set": False, "source": "default", "dir": ""}
        threads.append({
            "storage_id": sid,
            "started_at": t.get("started_at"),
            "ended_at": t.get("ended_at"),
            "turn_count": int(t.get("turn_count") or 0),
            "is_current": bool(t.get("is_current")),
            #: 标题同样只取用户说的话（框架附注截掉）——
            #:   否则会话名会变成「帮我看看这一轮附了 1 张图片…」。
            "title": _thread_title_of(st, sid),
            #: 同上：预览也是给人看的（可能是只有附注的那一条）。
            "preview": _user_visible(st.preview(sid)),
            "message_count": st.count_messages(sid),
            #: 左栏按目录分组要的那一格（`dir` = 落点，
            #:   `is_set` = 是选的还是默认的 —— 界面要说得出差别）。
            "workspace": _ws,
            # 「回到当前对话」那个按钮要判活跃
            #   （窗口 = 30 分钟）。名册里那两个时间字段都不回答这件事
            #   （见 `short_term.last_message_at` 的 docstring）。
            #   这里只给值、不给判断 —— 窗口在前端算。给一个后端算好的
            #   布尔，它会随那次拉取一起过期：界面 40 分钟不动，那个按钮
            #   就还亮着，而它本该消失（别把"那一刻的答案"
            #   当成"这个问题的答案"）。
            #   空线程（铸出来还没说过话），`None`，不是 0。
            "last_at": st.last_message_at(sid),
        })
    # 总数单给一格 —— 那一页 `limit=30`
    #   推不出总数（拿到 30 条就说"有 30 条"会是假话；报数就报真数）。
    #   拿不到就是 `None`，不是 0（读不到 ≠ 空）——
    #   前端据此不说话，而不是说一句"你有 0 条"。
    try:
        _total = st.count_threads(key)
    except Exception:                                            # noqa: BLE001
        _total = None
    # 置顶那一格跟着名册一起给 ——
    #   左栏那一棵树要按它排序，而它是前端排序（同一份名册一趟取回来，
    #   免得"名册到了、置顶还没到"那一瞬间列表跳一下）。
    #   读不到就给空表（不是"给个假的顺序"）—— 空 = 谁都不置顶 = 今天的行为。
    try:
        _pt = st.pinned("thread")
    except Exception:                                            # noqa: BLE001
        _pt = []
    try:
        _pg = st.pinned("group")
    except Exception:                                            # noqa: BLE001
        _pg = []
    return jsonify({"ok": True, "channel_key": key, "threads": threads,
                    "total": _total, "limit": limit,
                    "pinned_threads": _pt, "pinned_groups": _pg})


@app.route("/api/threads/delete", methods=["POST"])
def api_threads_delete():
    """删掉一段对话。

    它是全仓唯一一条「删用户对话」的路 —— 所以判据收得很紧：

      ① 这个 id 必须在本通道的名册里（`thread_channel(sid) == 本通道键`）
         —— 不是"客户端随便给个字符串就能删"。与
         `/api/cron/run/close` 的白名单同一条道理：
         能不能删，由服务端说了算，不由请求体说了算。
      ② 与 `/clear` 正相反（如实记，别混）：`/clear` 是留档 ——
         旧行原样留着、只换指针；这里是真删
         （消息 + 会话行 + 名册行，一条不留）。用户要的就是这个。
      ③ 删的是"当前那一段"，立刻铸一段新的并绑上。否则这个通道会指向
         一段已经不存在的东西，而下一次说话会用同一个 id 重新长出一个空会话
         （那种"看着正常、其实刚才那段没了"的状态最难查）。

    界面那一侧必须先确认一次（它是不可逆的）—— 服务端不替用户确认。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    mm = _mem()
    if mm is None or not getattr(mm, "short_term", None):
        return jsonify({"ok": False, "error": tr("记忆库未连接：框架未启动。")}), 503
    body = request.get_json(silent=True) or {}
    sid = str(body.get("storage_id") or "")
    if not sid:
        return jsonify({"ok": False, "error": tr("要给 storage_id")}), 400

    st = mm.short_term
    key = session_key_for(_web_session())
    try:
        owner = st.thread_channel(sid)
    except Exception as exc:                          # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}), 500
    if not owner or owner != key:
        return jsonify({"ok": False,
                        "error": tr("此对话不属于当前通道，无法删除。")}), 403

    was_current = (st.bound_storage_id(key) == sid)
    try:
        _session_pool.discard_thread(sid)
    except Exception as exc:                          # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}), 500

    fresh = ""
    if was_current:
        # 见 docstring ③：不铸新的，这个通道就指向一段不存在的东西。
        try:
            fresh, _msgs = _session_pool.new_session(key)
        except Exception as exc:                      # noqa: BLE001
            return jsonify({"ok": True, "storage_id": sid, "error":
                            tr("删掉了，但新建那一段失败：%s") % exc}), 200
    return jsonify({"ok": True, "storage_id": sid,
                    "was_current": was_current, "new_storage_id": fresh})


@app.route("/api/pins", methods=["POST"])
def api_pins_set():
    """置顶 / 取消置顶（「置顶此对话 / 此集」）。

    入参：`{kind: "thread"|"group", key, on: bool}` · 读侧在 `GET /api/threads`
    （`pinned_threads` / `pinned_groups`）—— 同一份名册一趟取回来，
    免得"名册到了、置顶还没到"那一瞬间列表跳一下。

    归属核对照抄 `/api/threads/delete` 那条判据（同一个道理，别再写第二份）：
      · `kind="thread"`，`key` 必须在本通道的名册里
        （`thread_channel(key) == 本通道键`）。少了它，客户端给个字符串
        就能置顶别的通道的一段对话。
      · `kind="group"`，`key` 是目录路径 —— 它是左栏呈现层的分组键，
        没有归属可言（任何通道看到的都是同一棵树），只校验"非空 + 不是超长"。
    置顶不是权限、不是内容，所以不需要审批；但它落库（跨重启）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    mm = _mem()
    if mm is None or not getattr(mm, "short_term", None):
        return jsonify({"ok": False, "error": tr("记忆库未连接：框架未启动。")}), 503
    body = request.get_json(silent=True) or {}
    kind = str(body.get("kind") or "")
    key = str(body.get("key") or "")
    on = bool(body.get("on"))
    if kind not in ("thread", "group"):
        return jsonify({"ok": False, "error": tr("kind 只能是 thread / group")}), 400
    if not key:
        return jsonify({"ok": False, "error": tr("要给 key")}), 400
    if len(key) > 4096:
        return jsonify({"ok": False, "error": tr("key 太长了")}), 400

    st = mm.short_term
    if kind == "thread":
        try:
            owner = st.thread_channel(key)
        except Exception as exc:                                 # noqa: BLE001
            return jsonify({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}), 500
        if not owner or owner != session_key_for(_web_session()):
            return jsonify({"ok": False,
                            "error": tr("此对话不属于当前通道，无法置顶。")}), 403
    try:
        now = st.set_pin(kind, key, on)
    except Exception as exc:                                     # noqa: BLE001
        return jsonify({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}), 500
    return jsonify({"ok": True, "kind": kind, "key": key, "on": now})


@app.route("/api/messages", methods=["GET"])
def api_messages():
    """一段会话的尾部一页消息，往上翻用 `before`。

    `?storage_id=<id>&before=<id>&limit=<n>`
    `?group=<群 id>&before=<id>&limit=<n>`   ← 群会话的读口（见下）

    返回升序（`id` 小 → 大），界面直接 prepend 到头顶；
      `has_more=true` 才让「往上翻」那个按钮活着。

    多了一条 `group` 寻址 ——
      进群之后看得到这个群说过的话。前端只传群 id，键与存储 id
      都由服务端推（`core/groups.session_key_of` + `short_term.bound_storage_id`），
      前端一个字节都不拼。
      没说过话的群，空历史 + `empty_reason`（不是 404、不是白屏 ——
      「没有读数」不等于「可以清空」）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    mm = _mem()
    if mm is None or not getattr(mm, "short_term", None):
        return jsonify({"ok": False, "error": tr("记忆库未连接：框架未启动。")}), 503
    st = mm.short_term
    gid = (request.args.get("group") or "").strip()
    sid = (request.args.get("storage_id") or "").strip()
    try:
        limit = int(request.args.get("limit", str(st.DEFAULT_PAGE)))
    except ValueError:
        return jsonify({"ok": False, "error": "limit 要是整数"}), 400
    if gid:
        # 群会话的读口。
        #   键由服务端推 —— 与 `/api/chat` 的 `body.group`、
        #   `core/groups.session_key_of` 是同一处口径（前端不拼键）。
        #   归属核对与私聊那条不同：私聊问"这条线程属不属于本通道"；
        #   这里问"这个群是不是我们账本里真有的群"（群 id 由 `groups.new_id` 铸）。
        from core import groups as _groups
        if not _groups.get(gid):
            return jsonify({"ok": False, "error": tr("没有这个群聊：%s") % gid}), 404
        # `bound_storage_id` 是纯查询（不铸新，同那条契约）：别让一次读
        #   凭空建出一条会话。没绑过 = 这个群还没说过话，空历史，不是错误。
        sid = st.bound_storage_id(_groups.session_key_of(gid)) or ""
        if not sid:
            return jsonify({"ok": True, "storage_id": "", "group": gid,
                            "messages": [], "has_more": False,
                            "empty_reason": "这个群还没说过话",
                            "oldest_id": None, "newest_id": None, "limit": limit})
    elif sid:
        ok, why = _desktop_thread(st, sid)
        if not ok:
            return jsonify({"ok": False, "error": why}), 404
    else:
        return jsonify({"ok": False, "error": tr("storage_id 或 group 必填")}), 400
    raw_before = (request.args.get("before") or "").strip()
    before = None
    if raw_before:
        try:
            before = int(raw_before)
        except ValueError:
            return jsonify({"ok": False, "error": "before 要是整数行号"}), 400
    # 默认给对话视图：只 `user`/`assistant` 且正文非空
    #    （界面只显示回复，不显示工具调用）。
    #    `?scope=all` 才要原始行（排查用 —— 比如"这一轮到底调了什么工具"）。
    #    判据不是这里现写的：它就是 `history_tools.is_conversation_row`
    #       那一条的 SQL 版（搜索能搜什么与界面显示什么必须是同一条，
    #       否则会出现「列表里有、搜不到」）。两边由 `tests/test_history_api.py`
    #       按集合相等钉着。
    #    筛在 SQL 里是为了让"一页"名副其实：50 条原始行里可能只剩 8 条对话行，
    #       那样的「加载更早」点一次几乎没动静、而 `has_more` 还是 true。
    conv_only = (request.args.get("scope") or "").strip().lower() != "all"
    rows, has_more = st.load_message_rows(sid, before_id=before, limit=limit,
                                          conversation_only=conv_only)
    #: 用户气泡只显示用户说的话 —— 框架附注
    #:   （"图被拦下了""这个文件只有路径"）从 `NOTE_HEADER` 起截掉。
    #:   就地改的是这一份返回给界面的副本，库里的全文一个字节不动
    #:   （模型与 `history_search` 还要用）。
    for _r in rows:
        if (_r.get("role") or "") == "user":
            _r["content"] = _user_visible(_r.get("content") or "")
    #: 压缩摘要行换成一句人话再给界面。
    #:   它是给模型的背景块（`<historical_context>` + 路标 + 逐字原话），
    #:   重启之后按"对话行"读回来，会在聊天记录里长出一个带内部标记的气泡。
    #:   行还在（分页、搜索、集合相等判据都不变），只改这一份返回副本。
    if conv_only:
        for _r in rows:
            if _r.get("kind") == "history_summary":
                _r["content"] = tr("（更早的对话已压缩成摘要，可在会话搜索里找回原文。）")
    return jsonify({
        "ok": True, "storage_id": sid, "messages": rows,
        "has_more": has_more,
        "scope": "conversation" if conv_only else "all",
        "oldest_id": rows[0]["id"] if rows else None,
        "newest_id": rows[-1]["id"] if rows else None,
        "limit": limit,
        # 群寻址时回带群 id，让界面确认读回来的是它要的那个群
        #   （私聊那一支恒为 `""`）。
        "group": gid,
    })


@app.route("/api/search", methods=["GET"])
def api_search():
    """会话内搜索（第 7 项那个搜索框接的就是它）。

    `?storage_id=<id>&q=<词>&turns=<n>`

    它没有另写一套搜索 —— 调的是 `core.history_tools.search_history`，
      也就是 `history_search` 那个工具的核。
      那个核只认「请求快照里的当前会话」，而端点上没有「当前」
      所以直接调只会永远收到「拿不到当前会话号」，看起来像「这段对话没内容」。
    返回结构化（含每条命中的 `id`）—— 界面靠 `id` 跳回那一行；
      中文那一段是工具壳的话术，不在这里返回（历史与实时
      在前端用同一套渲染）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    mm = _mem()
    if mm is None or not getattr(mm, "short_term", None):
        return jsonify({"ok": False, "error": tr("记忆库未连接：框架未启动。")}), 503
    st = mm.short_term
    sid = (request.args.get("storage_id") or "").strip()
    q = (request.args.get("q") or "").strip()
    if not sid:
        return jsonify({"ok": False, "error": tr("storage_id 必填")}), 400
    ok, why = _desktop_thread(st, sid)
    if not ok:
        return jsonify({"ok": False, "error": why}), 404
    try:
        turns = int(request.args.get("turns", str(history_search_module.DEFAULT_TURNS)))
    except ValueError:
        return jsonify({"ok": False, "error": "turns 要是整数"}), 400
    result = history_search_module.search_history(sid, q, turns)
    result = dict(result)
    result["storage_id"] = sid
    return jsonify(result), (200 if result.get("ok") else 400)


# 收藏夹（星标）
#
# 与上面三条结构性不同：那三条是只读的
#    （用户看得见而 agent 看不见，也不把它们带进上下文），而这三条要写。
#    写的是另一张表（`stars`），`messages` 一个字节都不动 ——
#    所以「模型永远不知道用户翻过历史」那条不变量原样成立
#    （`tests/test_history_api.py::test_endpoints_are_read_only` 照旧绿）。
#
# 归属核对（`_desktop_thread`）与那三条逐字同一套：端点收到的是
#   客户端传来的 id，不核的话「只动本通道的」就只管住了列表那一屏。

#: 收藏夹一页多少条。它有上限不是性能问题，是"界面一屏"问题 ——
#: 但不许因为翻页就把账报错：`total`/`has_more` 一定带上（报数，不报字符）。
_STARS_PAGE = 100


@app.route("/api/stars", methods=["GET"])
def api_stars_list():
    """收藏夹列表（新 → 旧）。

    "源没了就不显示"不是这里写的 —— 它是 `list_stars` 那两个 `JOIN`
      的结果（级联免费）。这里既不过滤也不兜底。
    每条带 `message_id` + `storage_id` —— 那是「点击跳过去」的抓手
      （跳转 = 切到那段会话 + 滚到那一行）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    mm = _mem()
    if mm is None or not getattr(mm, "short_term", None):
        return jsonify({"ok": False, "error": tr("记忆库未连接：框架未启动。")}), 503
    st = mm.short_term
    try:
        limit = int(request.args.get("limit", str(_STARS_PAGE)))
    except ValueError:
        return jsonify({"ok": False, "error": "limit 要是整数"}), 400
    key = session_key_for(_web_session())
    stars = st.list_stars(key, limit=limit)
    total = st.count_stars(key)
    return jsonify({
        "ok": True, "channel_key": key, "stars": stars,
        "total": total, "has_more": total > len(stars), "limit": limit,
    })


@app.route("/api/stars", methods=["POST"])
def api_stars_add():
    """给一轮打星标。体：`{"message_id": <那一轮提问的行号>}`。

    幂等：已经收藏过也回 200（重复点一下不该报错）。
    `message_id` 必须是那条 `user` 行 —— 判据在 `anchor_of` 里
      （执行侧、唯一一处），这里只负责把它转成整数、把归属核一遍。
    不收 `storage_id`：见下面那一段注释（服务端自己解析）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    mm = _mem()
    if mm is None or not getattr(mm, "short_term", None):
        return jsonify({"ok": False, "error": tr("记忆库未连接：框架未启动。")}), 503
    st = mm.short_term
    body = request.get_json(silent=True) or {}
    try:
        mid = int(body.get("message_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "message_id 要是整数行号"}), 400
    # `storage_id` 不入参 —— 那一行属于哪段会话是库自己知道的事实
    #    （`anchor_of` 解析）。让客户端传一遍 = 多一个能说谎的来源，而且
    #    聊天区根本不知道自己的 storage_id（它只知道刚拿到的行号）。
    #    所以归属核对照样做，核的是解析出来的那个。
    a = st.anchor_of(mid)
    if not a.get("ok"):
        return jsonify({"ok": False, "error": a.get("why") or "这条不能收藏"}), 400
    ok, why = _desktop_thread(st, a.get("storage_id") or "")
    if not ok:
        return jsonify({"ok": False, "error": why}), 404
    r = st.star_turn(mid)
    if not r.get("ok"):
        return jsonify({"ok": False, "error": r.get("why") or "存不进去"}), 400
    return jsonify({"ok": True, "storage_id": a.get("storage_id"),
                    "message_id": mid})


@app.route("/api/stars/delete", methods=["POST"])
def api_stars_remove():
    """取消一颗星标。体：`{"message_id": <行号>}`。

    本来就没有也回 200（`removed: false`）—— 取消是幂等的，报错只会
      让界面在"连点两下"时报一句没意义的红字。
    但归属要核：先查出这颗星属于哪一段，再走 `_desktop_thread`
      —— 删的是别人通道的收藏时回 404，不是静默删掉。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    mm = _mem()
    if mm is None or not getattr(mm, "short_term", None):
        return jsonify({"ok": False, "error": tr("记忆库未连接：框架未启动。")}), 503
    st = mm.short_term
    body = request.get_json(silent=True) or {}
    try:
        mid = int(body.get("message_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "message_id 要是整数行号"}), 400
    owner = st.star_storage_id(mid)
    if owner is None:
        return jsonify({"ok": True, "message_id": mid, "removed": False})
    ok, why = _desktop_thread(st, owner)
    if not ok:
        return jsonify({"ok": False, "error": why}), 404
    return jsonify({"ok": True, "message_id": mid, "removed": st.unstar_turn(mid)})


@app.route("/api/browse-folder", methods=["POST"])
def api_browse_folder():
    """Open a native folder-picker dialog and return the selected path.

    Since the Web UI runs on localhost, we can use OS-native dialogs
    that are not available to remote web pages.  Falls back gracefully
    if no GUI stack is available (headless mode).
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    try:
        import tkinter.filedialog
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = tkinter.filedialog.askdirectory(title="选择目录")
        root.destroy()
        if path:
            # Normalise backslashes for the frontend
            return jsonify({"ok": True, "path": path.replace("\\", "/")})
        else:
            return jsonify({"ok": False, "error": tr("未选择目录")})
    except Exception as e:
        return jsonify({"ok": False, "error": trf("无法打开文件夹选择器: {e}", e)})


def _graceful_exit() -> None:
    """壳请后端退场时，真正结束进程的那一下。

    单独一个函数是为了它可被换掉：`tests/test_desktop_lifecycle.py` 要打
      `/api/shutdown` 那一路，但不能让测试进程自己退出，它把这个名字换成假的。
      （这不是"为了测试而拆" —— 它与 `_on_task_start` 是同一族的缝。）
    """
    import os
    import sys as _sys
    try:
        _sys.stdout.flush()
        _sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """壳请后端自己退 —— 三级降级里的第一级。

    为什么要有这一档 —— 壳直接 `kill` 是"被人杀了"，而这一档是
    "自己退"。判据：退出码 0 但没 ack shutdown 也算不干净
    —— 没有这个 ack，那两种死法看起来一模一样（都是 `exitCode 0`），
    而"更新交接"那一档（`requireGraceful`）要的正是这个区别。
    为什么是 HTTP 而不是 IPC：Python 子进程没有 `process.send`
    （那是 Node 的 IPC fd），"优雅"那一档只能落在两边都已经有的
    那条通道上，而壳与后端之间本来就只有 HTTP。
    次序：先回执、后收摊 —— 回执发不出去的话，壳只能走到第二级（SIGTERM），
    于是"到底谁没做到"就分不清了。
    它不等待在途的那一轮："有没有活在跑"是壳那一侧的判据
    （`inspectQuit`），不是这里的；这里只负责"答应了就退"。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    global _FRAMEWORK_RUNNING
    _FRAMEWORK_RUNNING = False
    try:
        from core import services as _services
        _services.stop_all()
    except Exception:  # noqa: BLE001
        pass
    _log("[shutdown] 壳要求退出 —— 回执已发，0.3 秒后结束进程")
    import threading
    threading.Timer(0.3, _graceful_exit).start()
    return jsonify({"ok": True, "ack": True})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    """Restart the agent framework and optionally re-start channels."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    global _FRAMEWORK_RUNNING, _FRAMEWORK_INIT_ERROR, _AGENT_BUSY, _AGENT_TASK

    # Stop running channels
    for cid in list(_channel_running.keys()):
        _channel_running[cid] = False
        _log(f"[restart] Stopped channel {cid}")

    # 重启前停掉技能的后台服务（新一批在重新装载时启动）。
    try:
        from core import services as _services
        _stopped = _services.stop_all()
        if _stopped:
            _log("[restart] Stopped skill services: %s" % ", ".join(_stopped))
    except Exception as _exc:  # noqa: BLE001
        _log("[restart] 停止技能服务失败：%s: %s"
             % (type(_exc).__name__, _exc))

    _FRAMEWORK_RUNNING = False
    _AGENT_BUSY = False
    _AGENT_TASK = ""

    # Re-init
    _init_framework()

    if not _FRAMEWORK_RUNNING:
        return jsonify({"ok": False, "error": _FRAMEWORK_INIT_ERROR or "启动失败"}), 500

    # Re-start channels that were set to auto-start
    started = _auto_start_channels_from_settings()

    return jsonify({
        "ok": True,
        "restarted_channels": started,
    })


# Workspace v2 API — work groups, cross-zone, path index

def _get_wm():
    """Get the live WorkspaceManager, or None if framework not started."""
    try:
        from bus.registry import get_workspace_manager
        return get_workspace_manager()
    except Exception:
        return None


@app.route("/api/workspaces")
def api_list_workspaces():
    """List all configured work groups."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    #: 默认工作区的绝对路径 —— 走 `default_output_dir()` 那唯一一处
    #:   三层回退（显式 primary → 用户设的 workspace → ./workspace）。
    #:   界面（首启引导的工作区步骤）要显示完整路径，而不是 `settings.json`
    #:   里可能写着的相对名（`workspace`）—— 相对名对用户不是地址。
    try:
        from core.workspace import default_output_dir
        default_ws = str(default_output_dir())
    except Exception:                                          # noqa: BLE001
        default_ws = ""
    wm = _get_wm()
    if not wm:
        # Fallback: read from settings
        s = _read_settings()
        return jsonify({"workspaces": s.get("work_groups", []), "default": default_ws})
    return jsonify({
        "workspaces": [
            {
                "id": wg.id, "name": wg.name, "path": wg.path,
                "permissions": sorted(wg.permissions), "primary": wg.primary,
            }
            for wg in wm.workspaces
        ],
        "default": default_ws,
    })


@app.route("/api/workspaces", methods=["POST"])
def api_add_workspace():
    """Add a new workspace."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json() or {}
    path = data.get("path", "").strip()
    name = data.get("name", "").strip()
    permissions = data.get("permissions",
        ["copy", "delete", "move", "rename", "write"])
    primary = data.get("primary", False)
    if not path:
        return jsonify({"ok": False, "error": tr("缺少路径")}), 400

    wm = _get_wm()
    if wm:
        wg = wm.add_workspace(path, name, permissions, primary)
        _persist_workspaces(wm)
        return jsonify({"ok": True, "workspace": {
            "id": wg.id, "name": wg.name, "path": wg.path,
            "permissions": sorted(wg.permissions), "primary": wg.primary,
        }})
    else:
        # Offline mode: save to settings directly
        s = _read_settings()
        import uuid as _uuid
        wg = {"id": _uuid.uuid4().hex[:8], "name": name or Path(path).name,
              "path": str(Path(path).resolve()),
              "permissions": permissions, "primary": primary}
        wgs = s.get("work_groups", [])
        if primary:
            for w in wgs:
                w["primary"] = False
        wgs.append(wg)
        s["work_groups"] = wgs
        #: 不再写 `work_groups_enabled` —— 开关没了，
        #: 而"新建一个工作区"这件事本身就说明列表要生效（恒生效）。
        #: 继续写它只会让 `data/settings.json` 里多一个没人读的键。
        _write_settings(s)
        return jsonify({"ok": True, "workspace": wg})


@app.route("/api/workspaces/<ws_id>", methods=["DELETE"])
def api_delete_workspace(ws_id):
    """Remove a workspace (does NOT delete files on disk)."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    wm = _get_wm()
    if wm:
        ok = wm.remove_workspace(ws_id)
        if ok:
            _persist_workspaces(wm)
        return jsonify({"ok": ok})
    else:
        s = _read_settings()
        wgs = s.get("work_groups", [])
        s["work_groups"] = [w for w in wgs if w.get("id") != ws_id]
        _write_settings(s)
        return jsonify({"ok": True})


@app.route("/api/workspaces/<ws_id>/permissions", methods=["PUT"])
def api_update_workspace_permissions(ws_id):
    """Update workspace permissions."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json() or {}
    perms = data.get("permissions")
    name = data.get("name")
    wm = _get_wm()
    if wm:
        ok = wm.update_workspace(ws_id, name=name, permissions=perms)
        if ok:
            _persist_workspaces(wm)
        return jsonify({"ok": ok})
    return jsonify({"ok": False, "error": "framework not running"}), 503


@app.route("/api/workspaces/<ws_id>/primary", methods=["PUT"])
def api_set_primary_workspace(ws_id):
    """Set a workspace as primary."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    wm = _get_wm()
    if wm:
        ok = wm.set_primary(ws_id)
        if ok:
            _persist_workspaces(wm)
        return jsonify({"ok": ok})
    return jsonify({"ok": False, "error": "framework not running"}), 503


@app.route("/api/workspace-index")
def api_workspace_index():
    """Return path index statistics."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    wm = _get_wm()
    if wm:
        return jsonify({"stats": wm.index_stats})
    return jsonify({"stats": {"file_count": 0, "total_size_mb": 0, "last_built": ""}})


@app.route("/api/workspace-index/rebuild", methods=["POST"])
def api_rebuild_index():
    """Rebuild the path index."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    wm = _get_wm()
    if wm:
        count = wm.build_index()
        return jsonify({"ok": True, "stats": wm.index_stats})
    return jsonify({"ok": False, "error": "framework not running"}), 503


@app.route("/api/workspace-search")
def api_workspace_search():
    """Search files across workspaces (and cross-zone if enabled)."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    q = request.args.get("q", "*")
    scope = request.args.get("scope", "workspaces")
    max_results = int(request.args.get("max", "200"))
    wm = _get_wm()
    if wm:
        # `search_files` 返回三元组 (results, notice, action)。
        # 只解包两个，只要这个端点被调用就 `ValueError` 500。
        # 全仓目前没有任何调用方（前端也没有），所以一直没有暴露 ——
        # 留着一个一定崩的端点不写注释，下一个人接上它就会以为是新 bug。
        results, notice, _action = wm.search_files(
            q, scope=scope, max_results=max_results)
        return jsonify({
            "results": [
                {"name": r.name, "path": r.path, "ext": r.ext,
                 "size": r.size, "modified": r.modified}
                for r in results
            ],
            "notice": notice,
            "count": len(results),
        })
    return jsonify({"results": [], "notice": None, "count": 0})


def _persist_workspaces(wm) -> None:
    """Save workspace manager state to settings.json."""
    s = _read_settings()
    ws_data = wm.to_settings_dict()
    for key, value in ws_data.items():
        s[key] = value
    _write_settings(s)


# Watcher API (Phase C stubs)

@app.route("/api/watchers")
def api_list_watchers():
    """List all configured file watchers."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    wm = _get_wm()
    if wm:
        return jsonify({"watchers": [
            {
                "id": wc.id, "watch_path": wc.watch_path,
                "target_workspace_id": wc.target_workspace_id,
                "sync_documents": wc.sync_documents,
                "sync_images": wc.sync_images,
                "sync_media": wc.sync_media,
                "sync_archives": wc.sync_archives,
                "custom_extensions": wc.custom_extensions,
                "notify_on_sync": wc.notify_on_sync,
                "auto_start": wc.auto_start,
                "enabled": wc.enabled,
                "exclude_subdirs": wc.exclude_subdirs,
                "snapshot_mode": wc.snapshot_mode,
            }
            for wc in wm.watchers
        ]})
    return jsonify({"watchers": []})


@app.route("/api/watchers", methods=["POST"])
def api_add_watcher():
    """Add a new file watcher."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json() or {}
    from core.workspace import WatcherConfig
    wc = WatcherConfig(
        id=data.get("id", ""),
        watch_path=data.get("watch_path", ""),
        target_workspace_id=data.get("target_workspace_id", ""),
        sync_documents=data.get("sync_documents", True),
        sync_images=data.get("sync_images", False),
        sync_media=data.get("sync_media", False),
        custom_extensions=data.get("custom_extensions", []),
        notify_on_sync=data.get("notify_on_sync", True),
        auto_start=data.get("auto_start", True),
        enabled=data.get("enabled", False),
    )
    wm = _get_wm()
    if wm:
        wm.add_watcher(wc)
        _persist_workspaces(wm)
    return jsonify({"ok": True, "watcher": {"id": wc.id}})


@app.route("/api/watchers/<watcher_id>", methods=["DELETE"])
def api_delete_watcher(watcher_id):
    """Remove a file watcher."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    wm = _get_wm()
    if wm:
        wm.remove_watcher(watcher_id)
        _persist_workspaces(wm)
    return jsonify({"ok": True})


@app.route("/api/watchers/<watcher_id>", methods=["PUT"])
def api_update_watcher(watcher_id):
    """Update watcher configuration."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json() or {}
    wm = _get_wm()
    if wm:
        wm.update_watcher(watcher_id, **data)
        _persist_workspaces(wm)
    return jsonify({"ok": True})


@app.route("/api/watchers/<watcher_id>/toggle", methods=["PUT"])
def api_toggle_watcher(watcher_id):
    """Enable or disable a watcher."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json() or {}
    enabled = data.get("enabled", False)
    wm = _get_wm()
    if wm:
        wm.toggle_watcher(watcher_id, enabled)
        _persist_workspaces(wm)
        # 开了却起不来时把原因带回去 —— 界面拿它提示用户。
        # 「ok: true」在这里是假话：目录不存在时它正是"开着但一条都不会触发"。
        if enabled:
            _why = wm.watcher_error(watcher_id)
            if _why:
                return jsonify({"ok": False, "error": _why})
    return jsonify({"ok": True})


@app.route("/api/watchers/status")
def api_watchers_status():
    """Return watcher running status."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    wm = _get_wm()
    if wm:
        return jsonify({"status": wm.get_watcher_status()})
    return jsonify({"status": []})


@app.route("/api/watchers/events")
def api_watcher_events():
    """Return recent watcher sync events (for frontend notification polling).

    这个队列是排空式的（`poll_watcher_events` 消费即删），全站
    只许有一个轮询者，否则每 3 秒两组请求各抢到一半事件。
    所以母版那一份（`base.html`）是唯一的；设置页那份已删。

    顺带把 `notify` 一起发回去 —— 「是否启用通知」那个开关若只让设置页
    自己读得到（`security.html` 的 `_wsSettings`），别的页面就拿不到
    所以母版那句 `// Check if notification is enabled` 说了要做、代码没做，
    于是开关在别处关了照弹（标签在说谎）。放在这里是因为它本来就
    每 3 秒被拉一次 —— 零额外请求，而且改完当场生效（不用重开页面）。
    """
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    notify = _read_settings().get("cascade_notify")
    notify = True if notify is None else bool(notify)
    wm = _get_wm()
    if wm:
        events = wm.poll_watcher_events() if hasattr(wm, 'poll_watcher_events') else []
        return jsonify({"events": events, "notify": notify})
    return jsonify({"events": [], "notify": notify})


#  Auto-Update API

@app.route("/api/update/status")
def api_update_status():
    """Return current version + update availability + download progress."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    try:
        from core.updater import _update_state
        from core.version import __version__
        return jsonify({
            "current": __version__,
            "latest": _update_state.get("latest_version"),
            "update_available": _update_state.get("latest_version") is not None,
            "status": _update_state.get("status", "idle"),
            "progress": _update_state.get("progress", 0.0),
            "error": _update_state.get("error"),
        })
    except Exception as e:
        return jsonify({"current": "0.10.1", "update_available": False, "error": str(e)})


@app.route("/api/update/check", methods=["POST"])
def api_update_check():
    """Force an immediate update check."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    try:
        # 手动检查两条都查（stable + beta），取版本更高的那条。
        #    自动检查只查 stable —— beta「出现在可更新列表、不主动提醒」。
        from core.updater import check_all_channels, _update_state

        found = check_all_channels()
        manifest = found["best"]

        if manifest is not None:
            return jsonify({
                "update_available": True,
                "version": manifest.version,
                "changelog": manifest.changelog,
                "size_bytes": manifest.size_bytes,
                "mandatory": manifest.mandatory,
                "release_notes_url": manifest.release_notes_url,
                "channel": manifest.channel,
                #: 两条各自读到什么（`None` = 那条没新版本或没查成）。
                "channels": {c: (found[c].version if found[c] else None)
                             for c in ("stable", "beta")},
                "errors": found["errors"],
            })
        else:
            error = _update_state.get("error") or (
                "; ".join("%s: %s" % kv for kv in found["errors"].items()) or None)
            return jsonify({"update_available": False, "error": error,
                            "errors": found["errors"]})
    except Exception as e:
        return jsonify({"update_available": False, "error": str(e)})


@app.route("/api/update/download", methods=["POST"])
def api_update_download():
    """Start downloading the update in a background thread."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    try:
        from core.updater import UpdateChecker, _update_state

        # Use the latest manifest from the last check
        manifest_data = _update_state.get("latest_manifest")
        if not manifest_data:
            return jsonify({"error": "No update available. Run check first."}), 400

        from core.updater import UpdateManifest
        manifest = UpdateManifest.from_json(
            {"latest": manifest_data} if "latest" not in manifest_data else manifest_data
        )
        if manifest is None:
            return jsonify({"error": "Invalid manifest"}), 400

        checker = UpdateChecker()

        def _download_thread():
            checker.download(manifest)

        task_id = secrets.token_hex(8)
        _update_state["_task_id"] = task_id
        threading.Thread(target=_download_thread, daemon=True, name="update-download").start()

        return jsonify({"ok": True, "task_id": task_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/update/progress")
def api_update_progress():
    """Poll download progress."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    try:
        from core.updater import _update_state
        return jsonify({
            "status": _update_state.get("status", "idle"),
            "progress": _update_state.get("progress", 0.0),
            "error": _update_state.get("error"),
            "downloaded_path": _update_state.get("downloaded_path"),
        })
    except Exception as e:
        return jsonify({"status": "idle", "error": str(e)})


@app.route("/api/update/apply", methods=["POST"])
def api_update_apply():
    """Verify the downloaded update and trigger the self-replace install."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403

    # Gate: don't update while agent is busy
    if _AGENT_BUSY:
        return jsonify({"error": "Agent is busy — cannot update now."}), 409

    try:
        from core.updater import _update_state, UpdateInstaller

        downloaded = _update_state.get("downloaded_path")
        if not downloaded:
            return jsonify({"error": "No downloaded update available."}), 400

        from pathlib import Path
        installer = UpdateInstaller()
        installer.install(Path(downloaded))

        # install() calls os._exit(0) on success — we never reach here
        return jsonify({"ok": True, "message": "Update started."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/update/dismiss", methods=["POST"])
def api_update_dismiss():
    """Skip/dismiss the current update (ignore this version)."""
    if not _check_token():
        return jsonify({"error": "unauthorized"}), 403
    try:
        from core.updater import _update_state

        version = _update_state.get("latest_version")
        if not version:
            return jsonify({"error": "No update to dismiss."}), 400

        settings = _read_settings()
        settings["update_ignored_version"] = version
        _write_settings(settings)

        _update_state["latest_version"] = None
        _update_state["latest_manifest"] = None

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _set_windows_startup(enable: bool) -> None:
    """在 Windows 启动项里加/删一条 —— 只写 HKCU，不需要管理员。

    关掉自启时 `Run` 键本身可能不存在（干净的 Windows 用户从没设过），
    那时 `OpenKey` 自己抛 `FileNotFoundError`。原来那个 try 只套在
    `DeleteValue` 外面，保护不到它 —— 于是「关掉自启」在从没开过自启的
    机器上必然失败；而调用方把异常吃掉只留一行日志，症状是开关切过去了、
    注册表没动、下次开机照旧不启动。
    """
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    tray_path = _PROJECT_ROOT / "crannia_tray.py"
    # 打包成 exe 时用 exe 自己；源码运行时用解释器 + 托盘脚本
    if getattr(sys, 'frozen', False):
        command = f'"{sys.executable}"'
    else:
        command = f'"{sys.executable}" "{tray_path}"'

    if enable:
        # 用 CreateKeyEx 而不是 OpenKey：`Run` 键不存在时顺手建出来
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "Crannia", 0, winreg.REG_SZ, command)
        return

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "Crannia")
    except FileNotFoundError:
        # 键或值本来就不在 —— 「不在启动项里」这个目标状态已经成立
        pass


def _get_startup_seconds() -> float:
    """Return the time init_bus() took, or 0 if unknown."""
    try:
        from bus.core import get_startup_stats
        return get_startup_stats().get("total_seconds", 0)
    except Exception:
        return 0


# Helpers

def _forbidden():
    return "<h1>403 Forbidden</h1><p>Invalid or missing token.</p>", 403


def _read_config() -> dict:
    """Read config.yaml, resolve env vars."""
    from bus.core import load_config
    try:
        return load_config(_paths.config_path())
    except Exception:
        return {}


def _active_model_info() -> dict:
    """现在真正在发的那个 provider 与模型名。

    判据：`model_name` 取自 provider 实例
      （`_model` = wire 上真发的那个串），不读 `config.yaml` —— 运行时切换
      模型只写 config.yaml、不重建实例，两个源会分叉。

    多了第二层：默认启用那条配置条目。它一在，用户在界面上
      看到的名字就必须是那条的名字 —— 否则 `/api/status` 会一直显示
      `deepseek-v4-flash`，而请求已经走到他新配的中转站上（那是
      「标签在说谎」的形状，而且用户会拿它当"设默认没生效"的证据）。

    回 `{"provider", "model", "config_name", "config_id"}`；两个源都没有，空串。
    """
    out = {"provider": "", "model": "", "config_name": "", "config_id": ""}
    try:
        from core import model_configs as _mc
        act = _mc.active_summary()
        if act:
            out["config_id"] = act.get("id") or ""
            out["config_name"] = act.get("name") or ""
            out["model"] = act.get("model") or ""
            out["provider"] = act.get("provider") or ""
    except Exception:                                           # noqa: BLE001
        pass
    # 模型名以实例为准（它是 wire 上真发的那个串）—— 实例在就盖过配置条目
    #   里那个名字（正常情况下两者相等；不等就是"装的不是你以为的那条"）。
    live = getattr(_provider, "_model", "") if _provider is not None else ""
    if live:
        out["model"] = live
        out["provider"] = getattr(_provider, "_provider_name", "") or out["provider"]
    if not out["model"]:
        out["model"] = _config.get("model", {}).get("model_name", "")
        out["provider"] = out["provider"] or _config.get("model", {}).get("provider", "")
    return out


def _read_models(config: dict) -> list[dict]:
    """
    Return the provider catalog for the UI — from framework registry if
    running, otherwise auto-discovered from providers/ on disk.
    """
    import json as _json
    import yaml

    active_provider = config.get("model", {}).get("provider", "")
    active_model = config.get("model", {}).get("model_name", "")
    env_map = _build_env_map()

    # 不再读 `creators/models.json` ——
    #   那份硬编码目录退场。模型名一律来自
    #   用户自己建的配置条目；一条都没有时只剩 manifest 那个默认。
    #   动机：① 模型列表要跟着上游持续更新，硬编码那份一改就过时；
    #   ② 中转站填不进来。"用户填"才是这份目录该有的形状。
    by_dir: dict[str, list[dict]] = {}
    active_by_dir: dict[str, str] = {}
    try:
        from core import model_configs as _mc
        for _e in _mc.all_entries()["configs"].values():
            _d = _mc.CARD_PROVIDER.get(str(_e.get("card") or ""))
            if not _d:
                continue
            by_dir.setdefault(_d, []).append({
                "id": _e.get("model") or "",
                "label": _e.get("name") or _e.get("model") or "",
                "context_window": _e.get("window") or 0,
            })
        _act = _mc.active_summary()
        if _act:
            active_by_dir[_act.get("provider") or ""] = _act.get("id") or ""
    except Exception:                                           # noqa: BLE001
        pass

    # Prefer framework registry when available (after startup)
    result: list[dict] = []
    seen: set[str] = set()

    if _FRAMEWORK_RUNNING:
        from bus.registry import get_provider_metadatas
        metas = get_provider_metadatas()
        for name, meta in sorted(metas.items()):
            seen.add(name)
            available_models = list(by_dir.get(name, []))
            is_gateway = (name in ("openrouter", "ollama"))
            if not available_models and not is_gateway:
                available_models.append({
                    "id": meta.get("default_model", ""),
                    "label": meta.get("default_model", ""),
                    "context_window": meta.get("context_window", 0),
                })
            # "当前用哪个"改由配置条目说了算 —— 有默认条目且
            #   它在这家目录上，就是它（`meta["model"]` 是条目那个模型名）；
            #   没有默认条目才退回"目录名 == config.yaml 的 provider"。
            act_id = active_by_dir.get(name, "")
            current = (meta.get("model") or "") if act_id else (
                active_model if name == active_provider and active_model
                else meta.get("default_model", ""))
            env_key = env_map.get(name, "")
            result.append({
                "key": name, "provider": name,
                "label": meta.get("description", name).split("—")[0].strip().split("(")[0].strip(),
                "base_url": meta.get("base_url", ""),
                "website": meta.get("website", ""),
                "current_model": current,
                "models": available_models,
                "active": bool(act_id) or name == active_provider,
                # 同上："配好了没有"问的是有没有配置条目（或老 .env 里那把）
                "configured": bool(by_dir.get(name)) or bool(
                    env_key and os.environ.get(env_key)),
                "env_key": env_key,
                "is_local": meta.get("base_url", "").startswith("http://localhost"),
                "is_gateway": is_gateway,
                "note": "",
            })
        return result

    # Fallback: read from disk
    providers_dir = _PROJECT_ROOT / "providers"
    if not providers_dir.is_dir():
        return result

    for mf in sorted(providers_dir.glob("*/manifest.yaml")):
        try:
            with open(mf, encoding="utf-8") as f:
                m = yaml.safe_load(f)
        except Exception:
            continue
        if m.get("kind") != "provider":
            continue
        name = m["name"]
        cfg = m.get("config", {})
        env_key = env_map.get(name, "")
        base_url = cfg.get("base_url", "")
        is_local = base_url.startswith("http://localhost")
        is_active = name == active_provider
        #: 目录来自用户自己建的配置条目（`by_dir`）—— 硬编码那份
        #:   `creators/models.json` 已经退场（见上面那段），这里原来还引着
        #:   那位已删掉的变量 -> 走到这条磁盘兜底就 `NameError`。
        available_models = list(by_dir.get(name, []))
        is_gateway = (name in ("openrouter", "ollama"))
        if not available_models and not is_gateway:
            available_models.append({
                "id": cfg.get("default_model", ""),
                "label": cfg.get("default_model", ""),
                "context_window": cfg.get("context_window", 0),
            })
        current = active_model if is_active and active_model else cfg.get("default_model", "")
        result.append({
            "key": name, "provider": name,
            "label": m.get("description", name).split("—")[0].strip().split("(")[0].strip(),
            "base_url": base_url,
            "website": cfg.get("website", ""),
            "current_model": current,
            "models": available_models,
            "active": is_active,
            "configured": bool(env_key and os.environ.get(env_key)),
            "env_key": env_key,
            "is_local": is_local,
            "is_gateway": is_gateway,
            "note": cfg.get("note", ""),
        })

    return result


def _env_var_for(model: dict) -> str:
    """Guess the env var name from the model's api_key template."""
    ak = model.get("api_key", "")
    if "${" in ak:
        return ak.split("${")[1].split("}")[0]
    return ""


def _fingerprint(value: str, secret: bool = False) -> str:
    """已保存值给界面看的指纹 —— 只说明"这一格有值"，不回显值本身。

    配置弹窗的输入框本来一律是空的（留空 = 不改），于是已配置和
    没配置在界面上长得一样。这一格是那句话的补偿：有值时显示
    `cli_a1…f0e1`（密钥一律八颗点）。
    """
    s = str(value or "")
    if not s:
        return ""
    if secret:
        return "•" * 8
    if len(s) <= 8:
        return "•" * len(s)
    return s[:4] + "…" + s[-4:]


def _qq_credentials() -> dict:
    """QQ 机器人的凭据文件。读不到回空字典，与通道那边同一份口径。"""
    path = Path("data/qq_bot_credentials.json")
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_qq_credentials(fields: dict) -> None:
    """把 QQ 的 appId / appSecret 写进凭据文件。

    留空表示不改那一格 —— 与其它通道同一条规矩：界面上的输入框
    在已配置时是空的，把空串写回去等于把凭据清掉而用户看不出来。
    """
    path = Path("data/qq_bot_credentials.json")
    current = _qq_credentials()
    for key in ("app_id", "app_secret"):
        value = str((fields or {}).get(key) or "").strip()
        if value:
            current[key] = value
    if not current:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        print("[channel] 写 QQ 凭据失败（%s: %s）" % (type(exc).__name__, exc))


#: 通道 id ↔ 配置条目里的平台名。表外的一律没有条目（微信走扫码、CLI 不需要）。
_CHANNEL_PLATFORMS = {
    "feishu_channel": "feishu",
    "dingtalk_channel": "dingtalk",
    "telegram_channel": "telegram",
    "qq_bot_channel": "qq_bot",
}


def _channel_platform(channel_id: str) -> str:
    return _CHANNEL_PLATFORMS.get(str(channel_id or ""), "")


def _detect_channels(config: dict) -> list[dict]:
    """可用通道与它们的配置状态。

    有凭据表单的那四个（飞书 / 钉钉 / Telegram / QQ）走配置条目账本：
    `entries` 是该通道已存的几套凭据（打码），`fields` 沿用旧语义 ——
    默认那条的指纹。微信走扫码、CLI 不需要配置，这两条不进账本。
    """
    from core import channel_configs as _cc

    ch_cfg = config.get("channel", {})
    default = ch_cfg.get("default", "cli_channel")

    def _with_entries(row: dict, platform: str) -> dict:
        entries = _cc.entries_for(platform)
        row["platform"] = platform
        row["entries"] = entries
        row["configured"] = bool(entries)
        row["fields"] = entries[0]["fields"] if entries else {}
        return row

    channels = [
        _with_entries({"id": "feishu_channel", "name": "飞书",
                       "icon": "<img src='/static/img/channel-feishu.png' class='channel-icon-svg'>"},
                      "feishu"),
        _with_entries({"id": "dingtalk_channel", "name": "钉钉",
                       "icon": "<img src='/static/img/channel-dingtalk.png' class='channel-icon-svg'>"},
                      "dingtalk"),
        {"id": "wechat_channel", "name": "微信",
         "icon": "<img src='/static/img/channel-wechat.svg' class='channel-icon-svg'>",
         "configured": Path("data/wechat_credentials.json").exists()},
        _with_entries({"id": "telegram_channel", "name": "Telegram",
                       "icon": "<img src='/static/img/channel-telegram.svg' class='channel-icon-svg'>"},
                      "telegram"),
        _with_entries({"id": "qq_bot_channel", "name": "QQ",
                       "icon": "<img src='/static/img/channel-qq.svg' class='channel-icon-svg'>"},
                      "qq_bot"),
        {"id": "cli_channel", "name": "CLI 终端", "icon": "🖥",
         "configured": True, "always_available": True},
    ]
    for ch in channels:
        ch["active"] = ch["id"] == default
    return channels


def _save_channel_config(data: dict) -> None:
    """保存一套凭据 —— 写到配置条目账本的默认那条上。

    这条路径是旧界面用的，语义是"改默认那条"。有表单的四个通道进账本，
    微信走扫码、CLI 不需要配置，两者不进。
    `.env` 不再从界面上写：账本是唯一写入方，读的时候才回退到它。
    """
    from core import channel_configs as _cc

    channel_id = str(data.get("channel_id") or "")
    platform = _channel_platform(channel_id)
    if platform not in _cc.PLATFORMS:
        print("[channel] %s 没有凭据表单，不保存" % (channel_id or "?"))
        return
    fields = data.get("fields") or {}
    cid = _cc.default_id(platform)
    if cid:
        _cc.update(cid, fields=fields)
    else:
        _cc.create(platform, "默认", fields)
    # NOTE: channel.default 不在这里改 —— 保存凭据不该顺带换默认通道，
    #   那要走 /api/channel/set-default。


def _build_env_map() -> dict[str, str]:
    """Auto-discover env var names from provider manifests."""
    import yaml
    env_map: dict[str, str] = {}
    providers_dir = _PROJECT_ROOT / "providers"
    if providers_dir.is_dir():
        for mf in providers_dir.glob("*/manifest.yaml"):
            try:
                with open(mf, encoding="utf-8") as f:
                    m = yaml.safe_load(f)
                if m.get("kind") == "provider":
                    name = m["name"]
                    env_keys = m.get("env_keys", [])
                    if env_keys:
                        env_map[name] = env_keys[0]
                    else:
                        env_map[name] = f"{name.upper()}_API_KEY"
            except Exception:
                pass
    return env_map


def _save_model_config(data: dict) -> None:
    """Save model API key to .env AND multi-key store."""
    provider = data.get("key", data.get("provider", ""))
    api_key = data.get("api_key", "")
    label = data.get("label", "")
    if api_key and provider:
        # Sync to .env + os.environ immediately
        env_map = _build_env_map()
        key_name = env_map.get(provider, f"{provider.upper()}_API_KEY")
        _write_env({key_name: api_key})
        os.environ[key_name] = api_key
        # Also save to multi-key store
        store = _load_api_keys()
        if provider not in store:
            store[provider] = {"active": "", "keys": {}}
        kid = uuid.uuid4().hex[:8]
        store[provider]["keys"][kid] = {
            "label": label or provider,
            "key": api_key,
            "created": datetime.now(timezone.utc).isoformat(),
        }
        store[provider]["active"] = kid
        _save_api_keys(store)


def _write_env(updates: dict[str, str]) -> None:
    """Write key-value pairs to .env, preserving existing content."""
    env_path = _PROJECT_ROOT / ".env"
    existing = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                existing[k] = v
    existing.update(updates)
    lines = [f"{k}={v}" for k, v in existing.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _update_config_default_channel(channel_id: str) -> None:
    """Update channel.default in config.yaml."""
    cfg_path = _paths.config_path()
    if not cfg_path.exists():
        return
    text = cfg_path.read_text(encoding="utf-8")
    import re
    text = re.sub(r"default:\s*\S+", f"default: {channel_id}", text, count=1)
    cfg_path.write_text(text, encoding="utf-8")


def _home_abbrev(path, home) -> str:
    """把 *path* 里的 *home* 前缀缩写成 `~` —— 给人看的那一份根路径。

    集合那一行的标题就是路径（「零发明」—— 显示什么，就是从哪儿读的）。
    绝对路径一屏写不下，所以家目录那一段缩成 `~`。

    这一格必须在服务端算：只有这里知道 `Path.home()`。前端自己拼会漂，
    而且它压根不知道家目录在哪（一个口径一个地方）。
    不在家目录下的（仓内那个相对根 `skills`）原样返回 —— 不硬塞一个 `~`：
    那是伪造。分组键用的是机器可读的 `root`，不是这一格。
    """
    try:
        rel = Path(path).resolve().relative_to(Path(home).resolve())
    except (ValueError, OSError):
        return str(path)
    return "~/" + rel.as_posix()


def _scan_skills() -> list[dict]:
    """扫全部技能根，返回「技能页」要的元数据。

    根从一个（`_PROJECT_ROOT/skills`）变成 `core.skill_sources.skill_roots()`
      （仓内 + 共享目录），而且两种都列：
      工具提供者（有 `manifest.yaml`）+ 指令型（只有 `SKILL.md`）。
      [!] 两源分叉的教训：这里若用 `_PROJECT_ROOT / "skills"`（绝对），
      而加载链用 `Path("skills")`（cwd 相对）-> cwd≠仓根时
      页面看到的和真加载的不是同一批。两边必须同一个口径。

    每条多带几格：
      · `source` —— `repo`（目录内）/ `external`（外部来源）
      · `builtin` —— 框架自带（那份名单在 `core.skill_sources.BUILTIN_SKILLS`；
        技能页不显示它们）
      · `kind` —— `tool` / `prompt`
      · `path` —— 目录（外部来源那条「暂不提供卸载」要把它念给用户听）
      · `root` / `root_display` —— 这个包来自哪个根：技能页把同一个
        外部来源下的 ≥3 个包收成一个「集合」，分组键取 `root`（机器可读）；
        `root_display` 是它给人看的那一份（家目录缩写成 `~`）
      · `blocked` / `blocked_code` / `blocked_reason` —— 屏蔽账本（画红标用）

    [!] `blocked` 只是"画不画红标"；权威在 `GET /api/quarantine`（那条读不到回 503）。
    """
    import yaml
    from core.skill_sources import is_builtin_skill, skill_root_specs, source_of

    #: 账本读不到 -> 空。不冒充"没有屏蔽"：这一格只影响画不画红，
    #: 而 `/api/quarantine` 那条才是权威（读不到它回 503）。
    ledger: dict = {}
    try:
        from bus.quarantine import all_entries
        ledger = all_entries() or {}
    except Exception:
        ledger = {}

    #: 家目录只算一次（下面每个包都要用它缩写路径）。
    _home = Path.home()

    result = []
    for spec in skill_root_specs():
        root = spec.path
        if not root.is_dir():
            continue
        src = source_of(root)

        def _decorate(entry: dict, name: str, path) -> dict:
            """新格 —— 一处算（工具型与指令型共用）。

            `root` / `root_display`：技能页把同一个外部来源
            下的 ≥3 个包收成一个「集合」。分组键取这一格，
            前端不在 `dirname(path)` 上重推（两套真相源会漂）。
            """
            hit = ledger.get("skill:" + name) or {}
            entry.update({
                "source": src,
                # 更细的来源（给界面分类用）：repo / agents / claude /
                #   codex / workbuddy —— 由根规格给，不从内容猜。
                "origin": spec.origin,
                "builtin": is_builtin_skill(name),
                "path": str(path),
                "root": str(root),
                "root_display": _home_abbrev(root, _home),
                "blocked": bool(hit),
                "blocked_code": hit.get("code", ""),
                "blocked_reason": hit.get("reason", ""),
            })
            return entry

        # 工具提供者
        for manifest_path in sorted(root.glob("*/manifest.yaml")):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if data and data.get("kind") == "skill":
                    tools = data.get("tools") or []
                    cfg = data.get("config") or {}
                    # 声明式配置（`settings:`）也是"要用户填东西"
                    #   所以技能页必须显示它、并给那颗「配置」按钮。
                    #   判据仍是"manifest 说了算"，不是按技能名写死。
                    declarative = bool(data.get("settings"))
                    name = data.get("name", manifest_path.parent.name)
                    entry = {
                        "name": name,
                        "version": data.get("version", "0.0.0"),
                        "description": data.get("description", ""),
                        # 技能页卡片上那句短简介（
                        #   「github连接器 / 邮箱连接器 / 图视频拓展」）。
                        #   与 `description` 分开：那一句还喂给技能索引
                        #   （每请求的税 + 模型的检索面），短简介不许把它吃掉。
                        "summary": data.get("summary", ""),
                        "tool_count": len(tools),
                        "draft": data.get("draft", False),
                        "has_config": bool(cfg) or declarative,
                        "declarative": declarative,
                        "kind": "tool",
                        # 这个 skill 有没有"发信人白名单"那一格。
                        # 判据是 manifest 里声明了 `send_email`（不是写死 skill 名）——
                        # 前端据此决定画不画那块 UI，而"哪个 skill 会发邮件"这件事
                        # 只有 manifest 说了算（两套真相源会漂）。
                        "recipient_gate": any(
                            isinstance(t, dict) and t.get("name") == "send_email"
                            for t in tools),
                        # 声明的外部程序（原样带出去，`/api/skills` 当场查可用性）。
                        #   这里不查 —— 扫描是同步路径，查询放在渲染那一侧。
                        "external": data.get("external") or [],
                    }
                    if cfg:
                        entry["config"] = {
                            "provider_presets": cfg.get("provider_presets", {}),
                            "fields": cfg.get("fields", []),
                            "setup_guide": cfg.get("setup_guide", ""),
                            # 条目列表那几个键 —— 漏了它们，
                            #   界面就退化成"只有一个空表单"，而配置其实一直在
                            #   （现场：mail_parser 有两个账号，浮窗里一个都看不见）。
                            "named": bool(cfg.get("named")),
                            "entries_key": cfg.get("entries_key", ""),
                            "label_key": cfg.get("label_key", ""),
                        }
                    result.append(_decorate(entry, name, manifest_path.parent))
            except Exception:
                pass

        # 指令型（只有 `SKILL.md`、没有 `manifest.yaml`）
        # 必须列（否则用户刚放进去的 skill
        #   在技能页上看不到 —— 现在大家最常放的就是这一种）。
        # [!] 这里逐根调（把这一根传进去），不是 `None` —— 免得在外层循环里
        #   把同一批根扫 N 遍。
        try:
            from bus.loaders.skill_md import discover_instruction_skills
            for s in discover_instruction_skills([spec]):
                if any(r["name"] == s.name for r in result):
                    continue                    # 同名（跨根撞名那条已由命令面拒）
                result.append(_decorate({
                    "name": s.name,
                    "version": "",
                    "description": s.description or "",
                    "tool_count": 0,
                    "draft": False,
                    "has_config": False,
                    "kind": "prompt",
                }, s.name, s.directory))
        except Exception:
            pass

    # MCP tools (synthetic entry)
    mcp_count = 0
    mcp_external = 0
    try:
        from mcpx.manager import get_manager
        mgr = get_manager()
        mcp_count = mgr._tools_registered
        mcp_external = len(mgr._external_servers)
    except Exception:
        pass

    # Always show MCP entry — built-in web_search always has 2 tools
    # even before framework start.  If framework is running, use the
    # actual registered count.
    if mcp_count == 0:
        mcp_count = 2  # web_search: web_search + web_fetch
    if mcp_count > 0:
        result.append({
            "name": "_mcp_builtin_",
            # MCP 不是一种 skill —— 它自己那一栏（浮窗）走
            #   `GET /api/mcp/servers`。这里仍然挂着它是因为旧 WebUI 那页
            #   （`web/templates/skills.html`）还在用它；新前端按 `kind` 跳过。
            #   [!] MCP 浮窗落地后把这一条摘掉，别两处都列。
            "kind": "mcp",
            "source": "mcp",
            "builtin": True,
            "path": "",
            "blocked": False,
            "blocked_code": "",
            "blocked_reason": "",
            "version": "1.0.0",
            "description": (
                f"MCP 协议工具 — {mcp_count} 个工具"
                + (f"（{mcp_external} 个外部服务器）" if mcp_external else "（内置）")
            ),
            "tool_count": mcp_count,
            "draft": False,
            "has_config": True,
            "config": {
                "provider_presets": {},
                "fields": [],
                "setup_guide": (
                    "### 🔌 MCP 工具\n\n"
                    "MCP (Model Context Protocol) 工具通过标准协议连接到外部服务器。\n\n"
                    + (f"**已注册**: {mcp_count} 个工具\n\n" if mcp_count else "")
                    + "**配置外部 MCP 服务器**: 编辑 `data/mcp_servers.json`，"
                      "每项包含 `command`、`args`、`env` 字段。\n\n"
                    "**查看状态**: 在聊天中输入 `/mcp`。"
                ),
            },
        })

    return result
