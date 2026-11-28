# -*- coding: utf-8 -*-
"""SSH 客户端工具 —— 远端执行命令 / 传文件 / 探活。

## 这条路的四道闸（ / 出网权限重构 §3、§7）

1. 出网审批 —— 每次调用先过 `core/egress_gate`，subject 是
   `ssh:user@host`（决定 2）；四选项：同意本次 / 本次对话不再问 /
   永久信任（写 `data/allowlist.json` 的 `egress` 格）/ 不同意。
2. 主机密钥严格 —— 默认 `RejectPolicy`：未知主机拒，用户点
   「信任此主机并继续」才把指纹写进框架自己的
   `data/runtime/ssh/known_hosts`（决定 4：不碰 `~/.ssh/known_hosts`）；
   密钥与记录不一致（`BadHostKeyException`）一律拒，不给"警告并继续"。
3. 路径 —— `ssh_upload` 的本地源过 `validate_path(..., "read")`
   （`.env` / `data/settings.json` 读不到）；`ssh_download` 的落点必须
   在工作区（`validate_path(..., "write")`）。
4. 审计 —— 每次调用写一行 `EGRESS channel=ssh …`（`permission` 是
   read 也写：出网的风险与工具权限档无关）。

## 凭据

私钥 / 口令 / 密码只在 `core/ssh_config` 读，绝不进工具参数；
回执只出现条目名与主机。密码认证（决定 13）优先引导公钥，但没有密码
认证就配不了新机器 —— 新树莓派常常只有密码。

`sudo`（决定 14）：命令以 `sudo` 开头、且该条目配了 `sudo_password` 时，
改写成 `sudo -S` 并把密码写进远端 stdin（没有 TTY 时这是唯一的路）。
这是本工具的便利，不是命令解析器 —— 只认行首那一个词。

## 超时与长任务（决定 15）

超过 `timeout_s` 的命令会被中断。`apt upgrade` / 编译这类请自己用
`nohup ... > log 2>&1 &` 起、再用 `ssh_exec("tail -n 50 log")` 轮询；
P3 会把它们挂进后台作业。
"""

from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path
from typing import Any

from core import ssh_config
from core.i18n import t as tr, tr_fmt as trf

__all__ = ["init", "ssh_exec", "ssh_upload", "ssh_download", "ssh_probe"]

MAX_OUTPUT = 64 * 1024
DEFAULT_TIMEOUT = 20.0

#: 由 `bus` 在装载时注入（`init(security_guard)`）。
_guard = None


def init(security_guard=None) -> None:
    global _guard
    _guard = security_guard


def _ok(tool: str, data: dict, t0: float, **extra) -> dict:
    out = {"success": True, "tool": tool, "data": data, "error": "",
           "duration_ms": int((time.monotonic() - t0) * 1000)}
    out.update(extra)
    return out


def _err(tool: str, msg: str, t0: float, **extra) -> dict:
    out = {"success": False, "tool": tool, "data": {}, "error": msg,
           "duration_ms": int((time.monotonic() - t0) * 1000)}
    out.update(extra)
    return out


def _audit(action: str, target: str, grant: str = "", verdict: str = "") -> None:
    """写一行 EGRESS 审计。失败不影响调用（只印一行）。"""
    if _guard is None:
        return
    try:
        _guard.record_egress("ssh", target, action, grant, verdict)
    except Exception as exc:                                    # noqa: BLE001
        print(f"[ssh] audit write failed ({type(exc).__name__}: {exc})")


def _mdns_hint(host: str) -> str:
    """`.local` 的解析失败在 Windows 上极常见 —— 给一句路线，不假装成功。"""
    if str(host or "").lower().endswith(".local"):
        return ("（`.local` 在 Windows 上常常解析不了 —— 改用 IP，"
                "或安装 Bonjour / 用路由器的设备列表找地址。）")
    return ""


def _gate(tool_name: str, host: str, action: str, *, user: str = "",
          key_name: str = "", timeout_hint: str = ""):
    """出网审批。放行返回 `None`；要问返回载荷，调用方直接返回它。

    subject 由 `core.egress_gate.subject_from_args` 算 —— 与自动/完全权限
    预授权那一条同一个函数（写入与匹配同形）。
    """
    from core.egress_gate import subject_from_args, gate_for

    args = {"host": host, "user": user, "key_name": key_name}
    subj = subject_from_args(tool_name, args)
    if not subj:
        return None                     # 空目标在下面由参数校验挡掉
    # 键是模板（`{host}` 这类）—— 先翻再填，英文译文才能改语序。
    body = trf("Crannia 想通过 SSH 连接到 **{host}**（{action}）。\n"
               "目标：`{target}`", host, tr(action), subj)
    if timeout_hint:
        body += "\n" + timeout_hint
    return gate_for(subj, tool_name=tool_name, body=body)


def _hostkey_pending(tool_name: str, subject: str, host: str, port: int,
                     key) -> dict:
    """未知主机的信任载荷（`kind=ssh_hostkey`）。"""
    from core.approval import pending_payload

    line = "%s %s %s" % (host, key.get_name(), key.get_base64())
    fp = "SHA256:" + base64.b64encode(
        hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
    body = trf("Crannia 第一次连接 **{host}**。\n"
               "主机密钥：{keytype}\n指纹：{fp}\n"
               "核对指纹确实是你要连的那台机器，再选择信任。",
               host, key.get_name(), fp)
    return pending_payload(
        "ssh_hostkey",
        body=body,
        footer=True,
        subject=subject,
        detail="%s %s" % (host, fp),
        host=host,
        port=int(port),
        host_key_line=line,
        fingerprint=fp,
        tool=tool_name,
        status="pending_ssh_hostkey_approval",
    )


def _fetch_host_key(host: str, port: int, timeout: float):
    """连一次取主机公钥（不认证）。取不到返回 `(None, 原因)`。"""
    import paramiko
    t = None
    try:
        t = paramiko.Transport((host, int(port)))
        t.start_client(timeout=float(timeout))
        return t.get_remote_server_key(), ""
    except Exception as exc:                                    # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, exc)
    finally:
        if t is not None:
            try:
                t.close()
            except Exception:                                   # noqa: BLE001
                pass


def _connect(host: str, port: int, user: str, key_name: str, timeout: float,
             tool_name: str):
    """建连接。返回 `(client, error_msg, pending_payload)`。

    `pending_payload` 非 `None` 表示"要先问用户"（未知主机密钥）——
    调用方原样把它当回执返回（`agent_loop` 认 `pending_` 前缀）。
    """
    try:
        import paramiko  # noqa: F401
    except ImportError:
        return None, ("缺少 paramiko（LGPL-2.1）。请 `pip install paramiko`，"
                      "或在设置页 → 技能 → SSH 里查看配置。"), None

    entry = ssh_config.resolve_entry(str(key_name or ""))
    u = str(user or entry.get("user") or "").strip()
    key = str(entry.get("key_path") or "").strip()
    ph = str(entry.get("passphrase") or "").strip()
    pw = str(entry.get("password") or "").strip()
    if not key and not pw:
        return None, ("没配 SSH 凭据。去设置页 → 技能 → SSH 填私钥路径或密码"
                      "（不要放在工具参数里）。"), None
    if key:
        p = Path(key).expanduser()
        if not p.is_file():
            return None, "私钥文件不存在：%s（条目 %s）" % (p.name, entry.get("name")), None

    import paramiko
    subj = _subject_of(u, host)
    cli = paramiko.SSHClient()
    # 读两份：用户自己的（他手动连过的直接能用）+ 框架自己的（信任审批写的）。
    try:
        cli.load_system_host_keys()
    except Exception as exc:                                    # noqa: BLE001
        print(f"[ssh] failed to read system known_hosts (treated as empty): {type(exc).__name__}: {exc}")
    try:
        kh = ssh_config.known_hosts_path()
        if kh.exists():
            cli.load_host_keys(str(kh))
    except Exception as exc:                                    # noqa: BLE001
        print(f"[ssh] failed to read framework known_hosts (treated as empty): {type(exc).__name__}: {exc}")
    # 严格：未知主机一律拒（不给"警告并继续"）。
    cli.set_missing_host_key_policy(paramiko.RejectPolicy())

    kwargs: dict[str, Any] = {
        "hostname": host, "port": int(port),
        "username": u or None, "timeout": float(timeout),
        "allow_agent": False, "look_for_keys": False,
    }
    if pw and not key:
        kwargs["password"] = pw
    else:
        kwargs["key_filename"] = str(Path(key).expanduser())
        kwargs["passphrase"] = ph or None

    try:
        cli.connect(**kwargs)
        return cli, "", None
    except paramiko.BadHostKeyException:
        try:
            cli.close()
        except Exception:                                       # noqa: BLE001
            pass
        return None, (
            "主机密钥与已记录的不一致：%s。这可能是中间人攻击，也可能是"
            "机器重装过。请先在终端里确认指纹；确认无误后，把 "
            "data/runtime/ssh/known_hosts 里那一行删掉再重试。"
            % host), None
    except paramiko.SSHException as exc:
        try:
            cli.close()
        except Exception:                                       # noqa: BLE001
            pass
        if "not found in known_hosts" not in str(exc):
            return None, "%s: %s" % (type(exc).__name__, exc), None
        # 未知主机 —— 取指纹，问用户。取不到就给路线（不回退到不校验）。
        hk, why = _fetch_host_key(host, int(port), float(timeout))
        if hk is None:
            return None, ("未知主机 %s，且取不到它的主机密钥（%s）。"
                          "可以先用终端 ssh 连一次，把指纹记下来。" % (host, why)), None
        return None, "", _hostkey_pending(tool_name, subj, host, int(port), hk)
    except Exception as exc:                                    # noqa: BLE001
        try:
            cli.close()
        except Exception:                                       # noqa: BLE001
            pass
        msg = "%s: %s" % (type(exc).__name__, exc)
        return None, msg + _mdns_hint(host), None


def _subject_of(user: str, host: str) -> str:
    from core.egress_gate import ssh_subject
    return ssh_subject(user, host)


# 工具

def ssh_exec(host: str, command: str, port: int = 22, user: str = "",
             key_name: str = "", timeout_s: float = DEFAULT_TIMEOUT) -> dict:
    """在远端执行一条命令，回 stdout/stderr/exit_code（输出有上限）。"""
    t0 = time.monotonic()
    target = str(host or "").strip()
    cmd = str(command or "").strip()
    if not target or not cmd:
        return _err("ssh_exec", "host 与 command 都不能为空", t0)

    gate = _gate("ssh_exec", target, "执行命令", user=str(user),
                 key_name=str(key_name))
    if gate is not None:
        _audit("exec", target, "", "asked")
        return gate

    cli, why, pend = _connect(target, int(port), str(user), str(key_name),
                              float(timeout_s), "ssh_exec")
    if pend is not None:
        _audit("exec", target, "", "asked")
        return pend
    if cli is None:
        _audit("exec", target, "granted", "error")
        return _err("ssh_exec", why, t0)

    entry = ssh_config.resolve_entry(str(key_name or ""))
    sudo_pw = str(entry.get("sudo_password") or "").strip()
    use_sudo_stdin = bool(sudo_pw) and cmd.startswith("sudo ")
    try:
        if use_sudo_stdin:
            # 没有 TTY 时 `sudo` 读 stdin —— `-S` 让它从管道读密码，
            # `-p ''` 免得提示语混进 stderr。
            cmd = "sudo -S -p '' " + cmd[len("sudo "):]
        stdin, stdout, stderr = cli.exec_command(cmd, timeout=float(timeout_s))
        if use_sudo_stdin:
            try:
                stdin.write(sudo_pw + "\n")
                stdin.flush()
            except Exception:                                   # noqa: BLE001
                pass
        out_b = stdout.read(MAX_OUTPUT)
        err_b = stderr.read(MAX_OUTPUT)
        code = stdout.channel.recv_exit_status()
        data = {"host": target, "command": cmd,
                "stdout": out_b.decode("utf-8", "replace"),
                "stderr": err_b.decode("utf-8", "replace"),
                "exit_code": code}
        _audit("exec", target, "granted", "ok")
        return _ok("ssh_exec", data, t0,
                   truncated=len(out_b) >= MAX_OUTPUT or len(err_b) >= MAX_OUTPUT)
    except Exception as exc:                                    # noqa: BLE001
        _audit("exec", target, "granted", "error")
        msg = "%s: %s" % (type(exc).__name__, exc)
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            msg += ("。长任务请用 `nohup <命令> > /tmp/x.log 2>&1 &` 起，"
                    "再用 ssh_exec(\"tail -n 50 /tmp/x.log\") 轮询。")
        return _err("ssh_exec", msg, t0)
    finally:
        try:
            cli.close()
        except Exception:                                       # noqa: BLE001
            pass


def ssh_upload(host: str, local_path: str, remote_path: str,
               port: int = 22, user: str = "", key_name: str = "",
               timeout_s: float = DEFAULT_TIMEOUT) -> dict:
    """本地文件 → 远端（SFTP），远端目录不会自动创建。"""
    t0 = time.monotonic()
    src = Path(str(local_path or ""))
    if not src.is_file():
        return _err("ssh_upload", "本地文件不存在：%s" % src.name, t0)
    # 本地源过路径三级分类：`.env` 一族读不到（FORBIDDEN），
    # 用户划的规避目录也不行 —— 这是"读本地文件外发"的那道闸。
    try:
        if _guard is not None:
            _guard.validate_path(str(src), "read")
    except Exception as exc:                                    # noqa: BLE001
        _audit("upload", str(host or ""), "", "error")
        return _err("ssh_upload", "不能上传这个文件：%s" % exc, t0)

    gate = _gate("ssh_upload", str(host or ""), "上传文件",
                 user=str(user), key_name=str(key_name))
    if gate is not None:
        _audit("upload", str(host or ""), "", "asked")
        return gate

    cli, why, pend = _connect(str(host or ""), int(port), str(user),
                              str(key_name), float(timeout_s), "ssh_upload")
    if pend is not None:
        _audit("upload", str(host or ""), "", "asked")
        return pend
    if cli is None:
        _audit("upload", str(host or ""), "granted", "error")
        return _err("ssh_upload", why, t0)
    try:
        with cli.open_sftp() as sftp:
            sftp.put(str(src), str(remote_path or ""))
        _audit("upload", str(host or ""), "granted", "ok")
        return _ok("ssh_upload", {"host": host, "local": src.name,
                                  "remote": remote_path,
                                  "bytes": src.stat().st_size}, t0)
    except Exception as exc:                                    # noqa: BLE001
        _audit("upload", str(host or ""), "granted", "error")
        return _err("ssh_upload", "%s: %s" % (type(exc).__name__, exc), t0)
    finally:
        try:
            cli.close()
        except Exception:                                       # noqa: BLE001
            pass


def ssh_download(host: str, remote_path: str, local_path: str,
                 port: int = 22, user: str = "", key_name: str = "",
                 timeout_s: float = DEFAULT_TIMEOUT) -> dict:
    """远端文件 → 本地（SFTP）。落点必须在工作区。"""
    t0 = time.monotonic()
    dst = Path(str(local_path or ""))
    if not str(dst):
        return _err("ssh_download", "local_path 不能为空", t0)
    # 相对路径落在本段对话的默认输出目录（`primary_output_dir`），
    # 然后把最终路径过一遍写权限 —— 区外写有它自己的一道审批。
    try:
        from core.workspace import primary_output_dir
        if not dst.is_absolute():
            dst = Path(primary_output_dir()) / dst
        if _guard is not None:
            _guard.validate_path(str(dst), "write")
    except Exception as exc:                                    # noqa: BLE001
        _audit("download", str(host or ""), "", "error")
        return _err("ssh_download", "不能写到这个位置：%s" % exc, t0)

    gate = _gate("ssh_download", str(host or ""), "下载文件",
                 user=str(user), key_name=str(key_name))
    if gate is not None:
        _audit("download", str(host or ""), "", "asked")
        return gate

    cli, why, pend = _connect(str(host or ""), int(port), str(user),
                              str(key_name), float(timeout_s), "ssh_download")
    if pend is not None:
        _audit("download", str(host or ""), "", "asked")
        return pend
    if cli is None:
        _audit("download", str(host or ""), "granted", "error")
        return _err("ssh_download", why, t0)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with cli.open_sftp() as sftp:
            sftp.get(str(remote_path), str(dst))
        _audit("download", str(host or ""), "granted", "ok")
        return _ok("ssh_download", {"host": host, "remote": remote_path,
                                    "local": str(dst),
                                    "bytes": dst.stat().st_size}, t0)
    except Exception as exc:                                    # noqa: BLE001
        _audit("download", str(host or ""), "granted", "error")
        return _err("ssh_download", "%s: %s" % (type(exc).__name__, exc), t0)
    finally:
        try:
            cli.close()
        except Exception:                                       # noqa: BLE001
            pass


def ssh_probe(host: str, port: int = 22, timeout_s: float = 5.0) -> dict:
    """探活：TCP 可达 + SSH banner。不需要凭据（只建 socket）。"""
    t0 = time.monotonic()
    target = str(host or "").strip()
    if not target:
        return _err("ssh_probe", "host 不能为空", t0)
    gate = _gate("ssh_probe", target, "探活")
    if gate is not None:
        _audit("probe", target, "", "asked")
        return gate
    import socket
    try:
        with socket.create_connection((target, int(port)), timeout=float(timeout_s)) as s:
            s.settimeout(float(timeout_s))
            banner = ""
            try:
                banner = s.recv(256).decode("utf-8", "replace").strip()
            except OSError:
                pass
        _audit("probe", target, "granted", "ok")
        return _ok("ssh_probe", {"host": target, "port": int(port),
                                 "reachable": True, "banner": banner[:200]}, t0)
    except Exception as exc:                                    # noqa: BLE001
        _audit("probe", target, "granted", "error")
        return _ok("ssh_probe", {"host": target, "port": int(port),
                                 "reachable": False,
                                 "reason": "%s: %s%s" % (type(exc).__name__, exc,
                                                         _mdns_hint(target))}, t0)


def _bridge(sock_a, sock_b) -> None:
    """在两个 socket-like 之间双向搬字节（任一方向结束就半关）。"""
    import socket as _socket
    import threading as _threading

    def one_way(src, dst) -> None:
        try:
            while True:
                data = src.recv(16384)
                if not data:
                    break
                if hasattr(dst, "sendall"):
                    dst.sendall(data)
                else:
                    dst.send(data)
        except Exception:                                   # noqa: BLE001
            pass
        finally:
            try:
                if hasattr(dst, "shutdown_write"):
                    dst.shutdown_write()
                else:
                    dst.shutdown(_socket.SHUT_WR)
            except Exception:                               # noqa: BLE001
                pass

    t = _threading.Thread(target=one_way, args=(sock_b, sock_a), daemon=True)
    t.start()
    one_way(sock_a, sock_b)


def ssh_forward(host: str, remote_host: str, remote_port: int,
                local_port: int = 0, port: int = 22, user: str = "",
                key_name: str = "", timeout_s: float = 20.0) -> dict:
    """本地端口转发：127.0.0.1:local_port → remote_host:remote_port（经 SSH）。

    工具本身立刻返回，隧道跑在统一任务表里（`task` 工具可查、可取消；
    `/clear` 会停掉它）。寿限跟作业表一致（30 分钟），到点自动收。
    这是 nc / socat 的 relay 那一路的正规替身。
    """
    t0 = time.monotonic()
    target = str(host or "").strip()
    rh = str(remote_host or "").strip()
    if not target or not rh:
        return _err("ssh_forward", "host 与 remote_host 都不能为空", t0)
    try:
        rport = int(remote_port)
        lport = int(local_port or 0)
    except (TypeError, ValueError):
        return _err("ssh_forward", "端口必须是整数", t0)
    if not (1 <= rport <= 65535) or not (0 <= lport <= 65535):
        return _err("ssh_forward", "端口超出范围", t0)

    gate = _gate("ssh_forward", target, "建立隧道", user=str(user),
                 key_name=str(key_name))
    if gate is not None:
        _audit("forward", target, "", "asked")
        return gate

    cli, why, pend = _connect(target, int(port), str(user), str(key_name),
                              float(timeout_s), "ssh_forward")
    if pend is not None:
        _audit("forward", target, "", "asked")
        return pend
    if cli is None:
        _audit("forward", target, "granted", "error")
        return _err("ssh_forward", why, t0)
    transport = cli.get_transport()
    if transport is None or not transport.is_active():
        try:
            cli.close()
        except Exception:                                   # noqa: BLE001
            pass
        return _err("ssh_forward", "SSH 传输层没起来（连接立刻断了）", t0)

    import socket as _socket
    listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", lport))
        listener.listen(16)
        listener.settimeout(0.5)
    except Exception as exc:                                # noqa: BLE001
        try:
            listener.close()
            cli.close()
        except Exception:                                   # noqa: BLE001
            pass
        _audit("forward", target, "granted", "error")
        return _err("ssh_forward", "本地端口用不了：%s（换一个 local_port）" % exc, t0)

    actual = listener.getsockname()[1]
    # 任务列表里显示的就是这一句 —— 先翻模板再填值（同审批正文那两条）。
    label = trf("ssh 隧道 127.0.0.1:{local} -> {remote}:{rport}（经 {host}）",
                actual, rh, rport, target)

    from core import jobs as _jobs
    try:
        from core.request_snapshot import current_session_id
        sid = str(current_session_id() or "")
    except Exception:                                       # noqa: BLE001
        sid = ""

    def _run(job) -> str:
        deadline = time.monotonic() + (_jobs.JOB_MAX_LIFETIME_MS / 1000.0)
        job.out.write(("%s 已开始\n" % label).encode("utf-8"))
        try:
            while not job.cancel_event.is_set() and time.monotonic() < deadline:
                try:
                    conn, peer = listener.accept()
                except _socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    chan = transport.open_channel(
                        "direct-tcpip", (rh, rport), peer, timeout=float(timeout_s))
                except Exception as exc:                    # noqa: BLE001
                    job.out.write(("打开通道失败：%s: %s\n"
                                   % (type(exc).__name__, exc)).encode("utf-8"))
                    conn.close()
                    continue
                import threading as _threading
                _threading.Thread(target=_bridge, args=(conn, chan),
                                  daemon=True).start()
                job.out.write(("连接：%s\n" % (peer,)).encode("utf-8"))
        finally:
            try:
                listener.close()
            except Exception:                               # noqa: BLE001
                pass
            try:
                transport.close()
                cli.close()
            except Exception:                               # noqa: BLE001
                pass
        if job.cancel_event.is_set():
            return "隧道已停止（取消）。"
        return "隧道到寿限（30 分钟）自动停止；要续，重新调用。"

    try:
        job = _jobs.start_thread(_run, kind=_jobs.KIND_SKILL, command=label,
                                 session_key=sid, storage_id=sid,
                                 description=label, notify=False)
    except Exception as exc:                                # noqa: BLE001
        try:
            listener.close()
            cli.close()
        except Exception:                                   # noqa: BLE001
            pass
        return _err("ssh_forward", "起不了后台任务：%s: %s"
                    % (type(exc).__name__, exc), t0)

    _audit("forward", target, "granted", "ok")
    return _ok("ssh_forward", {
        "host": target,
        "local": "127.0.0.1:%d" % actual,
        "remote": "%s:%d" % (rh, rport),
        "task_id": job.id,
        "note": ("隧道在后台跑（30 分钟寿限）。用 task 工具查/停它；"
                 "把 local 地址给需要连它的程序即可。"),
    }, t0, truncated=False)
