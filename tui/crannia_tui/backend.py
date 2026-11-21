"""后端子进程的生命周期。

TUI 自己不实现 agent，它启动 crannia 的 web 后端并连上去。所以这里
管的是"怎么起、怎么确认起来了、怎么保证死干净"。

三条不变量：

  1. **token 由本进程生成**。后端只认 `CRANNIA_LAUNCH_TOKEN`，不认"谁先连上谁算数"——
     本机其他进程也能连 127.0.0.1，没有 token 就是公开的。
  2. **端口由本进程挑**。默认 5000 很容易被别的实例占着，占了就会连到别人的会话。
     让系统分配一个空闲端口，比"探测 5000 是否可用"更没竞态。
  3. **异常路径也要收尸**。用户 Ctrl-C、后端自己崩、Python 抛异常——
     三条路都不能留下孤儿进程。所以 stop() 是幂等的，并且在 finally 里调。
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

from .doctor import query as _query
from .i18n import t


#: 启动前要问后端解释器两件事：数据根（端点文件在它下面）与代码版本（握手）。
#: 与 `doctor` 同一条口径：TUI 不 import `core`，一律子进程问。
_FACTS_CODE = (
    "import json; from core.paths import data_root; "
    "from core.version import __version__; "
    "print(json.dumps({'root': str(data_root()), 'version': __version__}))")

#: 端点文件的位置。形状与读写口径的权威在 `core/endpoint.py`；
#: 这里只读、不写 —— TUI 只可能是接入方，写端点的是后端自己。
_ENDPOINT_REL = Path("data") / "runtime" / "endpoint.json"

#: 本机地址不走环境变量里的代理（与 `core/endpoint.py` 同一个理由）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _read_endpoint(root: Path) -> dict | None:
    """读端点文件；读不到 / 字段不像样一律 None。"""
    try:
        data = json.loads((Path(root) / _ENDPOINT_REL).read_text(encoding="utf-8"))
        port = int(data.get("port") or 0)
        pid = int(data.get("pid") or 0)
        token = str(data.get("token") or "")
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not (0 < port < 65536) or not token or pid <= 0:
        return None
    return {"port": port, "token": token, "pid": pid,
            "version": str(data.get("version") or "")}


def _probe(ep: dict, timeout: float = 2.0) -> dict | None:
    """端点上的 `/api/status` 应答；连不上 / 令牌不对都返回 None。"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{ep['port']}/api/status",
        headers={"X-Crannia-Token": ep["token"]})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _free_port() -> int:
    """让系统给一个空闲端口。

    拿到端口和真正 listen 之间有一个窗口，别的进程可能抢走。这个窗口
    在单机上窄到可以忽略；相比之下"固定 5000 再探测"的竞态更宽。
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BackendError(RuntimeError):
    """后端没能起来。消息里带最后几行输出 —— 起不来的原因几乎总在那里面。"""


class Backend:
    def __init__(self, crannia: Path, python: Path,
                 port: int | None = None, token: str | None = None,
                 data_root: Path | None = None,
                 expect_version: str | None = None) -> None:
        self.crannia = crannia
        self.python = python
        self.port = port or _free_port()
        self.token = token or secrets.token_urlsafe(24)
        self.base = f"http://127.0.0.1:{self.port}"
        #: 接入判定用的两个口径。默认自己去问（子进程），测试与特殊部署可注入。
        self.data_root = data_root
        self.expect_version = expect_version
        self._proc: subprocess.Popen | None = None
        #: 后端 stdout/stderr 的尾巴。正常时不看，报错时是唯一线索；
        #: 有界是因为它会一直涨（日志是流式写的）。
        self._tail: deque[str] = deque(maxlen=80)
        self._pump: threading.Thread | None = None

    # -- 起 -------------------------------------------------------

    def _attach(self) -> dict | None:
        """有活的端点就接上去（返回 port/token），没有返回 None。

        版本必须一致：两套安装各自更新，版本错开时"能连上"不代表
        "能一起用"。握手失败是硬错，不是"退回自己起" —— 同一个数据根上
        再起一个后端，正是这条要防的事。
        """
        root, expect = self.data_root, self.expect_version
        if root is None or expect is None:
            try:
                facts = json.loads(_query(self.python, self.crannia, _FACTS_CODE))
                root = Path(facts["root"]).expanduser()
                expect = str(facts["version"])
            except Exception:                    # noqa: BLE001
                # 问不出来（解释器不对 / 树不全）就不接。照常 spawn 会撞上
                # 后端的让位闸，那句"已有服务在跑"比这里猜更准。
                return None
        ep = _read_endpoint(root)
        if ep is None:
            return None
        status = _probe(ep)
        if status is None:
            return None
        got = str(status.get("version") or "")
        if not got or got != expect:
            raise BackendError(
                t("版本不一致：TUI 是 %s，当前后端 %s。请优先升级后重试。")
                % (expect, got or t("未知")))
        return {"port": ep["port"], "token": ep["token"]}

    def start(self) -> None:
        if self._proc is not None:
            return
        live = self._attach()
        if live is not None:
            # 接上去：端口与令牌都来自端点文件；本进程没有子进程，
            # `stop()` 因此不会去杀别人的后端。
            self.port, self.token = live["port"], live["token"]
            self.base = f"http://127.0.0.1:{self.port}"
            return
        env = dict(os.environ)
        env.update({
            "CRANNIA_LAUNCH_TOKEN": self.token,
            "CRANNIA_API_PORT": str(self.port),
            # 不设这一格，后端会去开系统浏览器 —— 用户点一次"起 TUI"
            # 却弹出一个网页，这一格就是那个差别。
            "CRANNIA_DESKTOP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        })
        self._proc = subprocess.Popen(
            [str(self.python), "-m", "web"],
            cwd=str(self.crannia), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self._pump = threading.Thread(target=self._drain, daemon=True)
        self._pump.start()

    def _drain(self) -> None:
        """把子进程输出收进环形缓冲。

        不直接打到终端：TUI 占着屏幕，backend 的启动日志会把界面冲掉。
        留尾巴是为了"起不来"时能原样贴出来。
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self._tail.append(line)

    # -- 就绪 -----------------------------------------------------

    def wait_ready(self, timeout: float = 90.0) -> dict:
        """轮询 health 直到起来。

        超时给到 90 秒：后端要装 bus（providers / skills / channels）和
        记忆库，冷启动十几秒是常态；给短了会把"慢"误判成"坏"。
        """
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise BackendError(
                    t("后端进程已退出（code=%s）。\n") % self._proc.returncode
                    + self.tail_text())
            try:
                return self.get_json("/api/health", timeout=3)
            except Exception as exc:      # 连接被拒是预期内的，它还没 listen
                last = exc
                time.sleep(0.4)
        raise BackendError(
            t("后端 %ss 内未就绪（最后错误：%s）。\n")
            % (f"{timeout:.0f}", last) + self.tail_text())

    def start_framework(self) -> None:
        """让后端把 agent 框架启起来。

        不调这一步，`/api/chat` 会回 503 —— 那个端点检查的是
        `_FRAMEWORK_RUNNING`，它只在 `/api/start` 之后才为真。
        """
        code, body = self.post_json("/api/start", {})
        if code != 200:
            raise BackendError(t("/api/start 失败（%s）：%s")
                               % (code, body[:300]))

    # -- HTTP -----------------------------------------------------

    def _request(self, path: str, data: bytes | None = None,
                 timeout: float = 30.0):
        req = urllib.request.Request(
            self.base + path, data=data,
            method="POST" if data is not None else "GET",
            headers={"X-Crannia-Token": self.token,
                     **({"Content-Type": "application/json"} if data else {})},
        )
        return urllib.request.urlopen(req, timeout=timeout)

    def get_json(self, path: str, timeout: float = 10.0) -> dict:
        with self._request(path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def post_json(self, path: str, payload: dict,
                  timeout: float = 300.0) -> tuple[int, str]:
        """POST。超时给得长：一轮工具调用可能跑几分钟。

        用 300 秒而不是不设上限 —— TCP 断了之后没有超时的话，
        这个调用会一直挂着，用户看到的是"永远转圈"。
        """
        body = json.dumps(payload).encode("utf-8")
        try:
            with self._request(path, data=body, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")

    # -- 收 -------------------------------------------------------

    def tail_text(self, n: int = 12) -> str:
        lines = list(self._tail)[-n:]
        return ("\n".join("  " + ln for ln in lines) if lines
                else "  " + t("（没有输出）"))

    def stop(self) -> None:
        """终止后端。幂等 —— 正常退出和异常退出都会走到这里。"""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            # 8 秒还不退：它可能卡在某个不可中断的调用里。
            # 这时候只能强杀 —— 留着它比杀掉更糟（下次启动会抢端口/锁数据）。
            proc.kill()
        except Exception:
            proc.kill()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
