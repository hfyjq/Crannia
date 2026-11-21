"""一个数据根只许一个后端：端点文件的读写、判活与清理。

写这份文件是因为"两个都装"（GUI 与 TUI 共用 `data/settings.json` 与同一个
数据根）需要一个双方都认的地址。共享数据根意味着两套安装必须接同一个后端；
而"谁是那个后端"由端点文件回答，不由"谁先启动"猜。

三条判据：

  1. 写与删都只认自己那份。退出时按 pid 比对，文件已经被后来的进程换掉
     就不删，否则后起的后端会失去自己的端点。
  2. 判活以 HTTP 为准，不以 pid 为准。pid 还在不等于它在听；反过来，
     pid 复用会让"活着"误判成"死了"。pid 只用于清理坏文件时解释原因。
  3. 不用 `os.kill(pid, 0)` 判活。Windows 上 `os.kill` 走
     `TerminateProcess`，判活会变成杀进程，即检测器改变了被测对象。

文件位置：`<数据根>/data/runtime/endpoint.json`。走 `data/runtime/` 而不是
数据根直属的 `runtime/`，与运行数据的既有口径一致（开发树里数据根就是仓库，
写 `runtime/` 会往源码目录里加东西）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

NAME = "endpoint.json"

#: 本机地址不走环境变量里的代理 —— 代理通常不该拦 127.0.0.1，
#: 但 `http_proxy` 一设，urllib 默认就会去问它。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def endpoint_path(root: Path) -> Path:
    """端点文件的绝对路径。`root` 是数据根（`core.paths.data_root()`）。"""
    return Path(root) / "data" / "runtime" / NAME


def write(root: Path, *, port: int, token: str, version: str,
          pid: int | None = None) -> Path:
    """原子写入端点文件，返回路径。

    "原子"是给读侧的：TUI 与壳会在本进程写到一半时来读，半截 JSON
    会被判成"没有端点"，于是它们各起一个后端 —— 正是这层要防的事。
    """
    path = endpoint_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "port": int(port),
        "token": str(token),
        "pid": int(pid if pid is not None else os.getpid()),
        "version": str(version),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def read(root: Path) -> dict | None:
    """读端点文件。读不到、坏 JSON、字段不像样 —— 一律返回 None。

    "没有端点"与"端点坏了"在这一层的处置相同：调用方当作没人，
    然后自己起后端；`write()` 会把坏文件覆盖掉。
    """
    try:
        data = json.loads(endpoint_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        port = int(data.get("port") or 0)
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    token = str(data.get("token") or "")
    if not (0 < port < 65536) or not token or pid <= 0:
        return None
    return {
        "port": port,
        "token": token,
        "pid": pid,
        "version": str(data.get("version") or ""),
    }


def clear(root: Path, *, pid: int | None = None) -> bool:
    """删端点文件。给了 `pid` 就只删自己那份（文件里的 pid 对得上才删）。

    返回是否真的删了。删不掉不抛 —— 一个残留的坏文件由下一次 `probe()`
    判死，再被 `write()` 覆盖；为此让进程退出时崩掉不值得。
    """
    path = endpoint_path(root)
    data = read(root)
    if data is None:
        try:
            path.unlink()
            return True
        except OSError:
            return False
    if pid is not None and data["pid"] != int(pid):
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def probe(root: Path, timeout: float = 2.0) -> dict | None:
    """端点文件上真的有人应答吗？活着返回合并后的字典，否则 None。

    返回 `{port, token, pid, version, status}`：`status` 是 `/api/status`
    的响应体，版本握手与"框架起没起"都从它来。

    超时给 2 秒：这是启动路径上的同步调用，宁可判"没活着"也不要卡住；
    真正的连接失败（拒绝 / 超时 / 403）统一按"没人"处理 —— 调用方那时
    会自己起后端，代价只是多一次冷启动。
    """
    data = read(root)
    if data is None:
        return None
    url = f"http://127.0.0.1:{data['port']}/api/status"
    req = urllib.request.Request(
        url, headers={"X-Crannia-Token": data["token"]})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    out = dict(data)
    out["status"] = body
    return out
