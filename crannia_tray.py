"""
Crannia System Tray — zero-barrier launcher.

Double-click crannia.exe → tray icon appears → browser opens Web UI.
All configuration and chat happens in the browser.  No terminal needed.

Dependencies: pystray, Pillow (for tray icon)
"""

from __future__ import annotations

import socket
import sys
import threading
import time as _time
import webbrowser
from pathlib import Path

# Ensure project root is importable
_PROJECT_ROOT = Path(__file__).parent.resolve()

# 与 `main.py` 同一条：安装版把数据根迁到用户目录并 chdir。
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from core import paths as _paths
_paths.prepare()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env", override=True)

from core.version import __version__

PORT = 5000


def _is_port_in_use(port: int) -> bool:
    """Return True if *port* is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _kill_old_instance() -> bool:
    """Kill the process holding PORT. Returns True if old instance was found."""
    if not _is_port_in_use(PORT):
        return False
    try:
        import subprocess
        # Find PID of the process listening on PORT
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if f"127.0.0.1:{PORT}" in line and "LISTENING" in line:
                pid = line.strip().split()[-1]
                print(f"[tray] Killing old instance (PID={pid})...")
                subprocess.run(["taskkill", "/F", "/PID", pid],
                               capture_output=True, timeout=5)
                import time
                time.sleep(0.5)
                return True
    except Exception:
        pass
    return False


def _health_check(port: int, interval: float = 10.0, max_fails: int = 3) -> None:
    """Background thread: ping /ping endpoint, restart server if it hangs.

    Runs as a daemon thread alongside the WSGI server.  If the server
    stops responding, kills the old server thread and spawns a new one.
    """
    import urllib.request

    
    fail_count = 0
    while True:
        _time.sleep(interval)
        # Skip health checks during update (process is being replaced)
        try:
            from core.updater import _UPDATING
            if _UPDATING:
                continue
        except ImportError:
            pass
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/ping", method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    if fail_count > 0:
                        print(f"[watcher] Server recovered after {fail_count} failures")
                    fail_count = 0
                    continue
        except Exception:
            pass
        fail_count += 1
        print(f"[watcher] Ping failed ({fail_count}/{max_fails})")
        if fail_count >= max_fails:
            print("[watcher] Server unresponsive — restarting...")
            _restart_server(port)
            fail_count = 0


_server_thread: threading.Thread | None = None
_server_ready: threading.Event | None = None


def _restart_server(port: int) -> None:
    """Kill the old server thread and start a fresh one."""
    global _server_thread, _server_ready
    # Kill old process on the port
    _kill_old_instance()
    # Start fresh
    _server_ready = threading.Event()
    _start_server(port, _server_ready)
    _server_ready.wait(timeout=10)
    print("[watcher] Server restarted")


def _start_server(port: int, ready_event: threading.Event) -> None:
    """Start the waitress WSGI server in a daemon thread."""
    from web.server import app, _LAUNCH_TOKEN
    from waitress import serve

    def run():
        ready_event.set()
        # v1.2.0: 与 web/server.py:start_webui 保持一致 ——
        # SSE 长连接占线程，且 /api/chat 是阻塞 POST。两者必须同步改，
        # 否则托盘启动和命令行启动的行为不一致。
        serve(app, host="127.0.0.1", port=port, threads=12,
              channel_timeout=300,
              _quiet=True,  # suppress waitress startup banner
              )

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # Store token globally for tray menu
    global _token
    _token = _LAUNCH_TOKEN


def _open_webui(port: int) -> None:
    """Open the Web UI in the default browser."""
    global _token
    webbrowser.open(f"http://127.0.0.1:{port}/?token={_token}")


def _create_tray(port: int):
    """Create the system tray icon and menu."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        print("[tray] pystray or Pillow not installed. "
              "Install: pip install pystray Pillow")
        print("[tray] Web UI running — open http://127.0.0.1:"
              f"{port}/?token={_token}")
        return

    # Crannia leaf icon — dark green bg + cream accent
    icon_img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon_img)
    # Sage green circle background (#558048, matches sidebar)
    draw.ellipse([4, 4, 60, 60], fill=(83, 124, 88))
    # Leaf shape in cream (#FBFADA)
    draw.ellipse([14, 10, 50, 54], fill=(191, 126, 67))
    # Center vein line
    draw.line([32, 14, 32, 50], fill=(83, 124, 88), width=2)
    # Stem
    draw.rectangle([30, 48, 34, 58], fill=(191, 126, 67))

    def on_open():
        _open_webui(port)

    def on_quit(icon):
        icon.stop()
        sys.exit(0)

    # 托盘急停 —— 落点就是它。托盘与后端在同一个进程里（waitress
    #    是这里起的线程），直接调 `session.disable()`，不必绕 HTTP。
    def on_stop_takeover(icon, item=None):
        try:
            from core.computer import session as _cu
            owner = _cu.disable("tray")
            if owner:
                try:
                    icon.notify("接管已停止", "Crannia")
                except Exception:                    # noqa: BLE001 - 通知失败不影响停用
                    pass
            print("[tray] 接管已停止" if owner else "[tray] 接管本来就没开")
        except Exception as exc:                     # noqa: BLE001
            print(f"[tray] 停止接管失败：{type(exc).__name__}: {exc}")

    def _takeover_enabled(item=None):
        try:
            from core.computer import session as _cu
            return _cu.is_enabled()
        except Exception:                            # noqa: BLE001
            return False

    menu = pystray.Menu(
        pystray.MenuItem("打开 Web UI", on_open, default=True),
        pystray.MenuItem("启动 Agent", lambda: _open_webui(port)),
        pystray.Menu.SEPARATOR,
        # 急停项：没开接管时置灰（`enabled` 由 pystray 每次展开菜单时问）。
        pystray.MenuItem("停止接管", on_stop_takeover, enabled=_takeover_enabled),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出 Crannia", on_quit),
    )

    icon = pystray.Icon("crannia", icon_img, "Crannia AI Agent", menu)
    icon.run()


# Global token (set by _start_server)
_token: str = ""

# Entry point


def main() -> None:
    import os

    # Prevent duplicate instances
    if _kill_old_instance():
        print("[tray] Old instance terminated.")

    # Suppress console in packaged exe (PyInstaller)
    if getattr(sys, 'frozen', False):
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(
                ctypes.windll.kernel32.GetConsoleWindow(), 0)
        except Exception:
            pass

    print(f"Crannia v{__version__} starting...")

    # Start the web server
    global _server_ready, _server_thread
    _server_ready = threading.Event()
    _start_server(PORT, _server_ready)
    _server_ready.wait(timeout=5)

    # Start heartbeat watcher
    threading.Thread(
        target=_health_check, args=(PORT,),
        daemon=True, name="watcher",
    ).start()
    print("[tray] Watcher started (checking every 10s)")

    # Clean up old update backups (>30 days)
    try:
        from core.updater import UpdateChecker
        UpdateChecker.cleanup_backups()
    except Exception:
        pass

    # Background update check (delayed 30s to not slow startup)
    try:
        from core.updater import background_startup_check
        threading.Timer(30.0, background_startup_check).start()
    except Exception:
        pass

    # Open browser after a brief delay
    threading.Timer(1.0, lambda: _open_webui(PORT)).start()

    # Create tray icon (blocks until quit)
    _create_tray(PORT)


if __name__ == "__main__":
    main()
