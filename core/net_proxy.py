"""出网代理—— 一处口径，三档。

## 为什么要有这个模块

根因是 HTTP 客户端的默认值：`httpx` 的 `trust_env=True`、`requests` 的
`Session.trust_env=True` —— 它们会静默采纳环境变量里的
`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`。而 Windows 上那条变量是用户级的，
一次"忘了关代理"或"代理换了端口"就会同时卡住模型请求和所有联网工具，
而界面上一个字节都看不见（用户看到的是
「网络连接异常--请检查宽带互联网连接」，实际是环境里那条 `socks5://127.0.0.1:<端口>` 没在听）。

这是这一类工具共同的坑，而共同点就是那个默认值。

## 三档

| 档 | 含义 | 造客户端时 |
|---|---|---|
| `system`（默认） | 真正的「跟随系统」= 先读 Windows 系统代理（WinINET），没开才看环境变量（且必须连得上） | 解析结果投影到环境，客户端照旧 `trust_env` |
| `direct` | 显式忽略系统代理（自救路） | `trust_env=False` |
| `manual` | 全部出网 HTTP 都用这一个地址 | 显式 `proxy=`（并关掉 trust_env） |

默认必须是 `system`：有一类用户只能靠代理上网，改默认会把他们弄坏。
`direct` 是给"系统代理配错了"的人一条不必去改 Windows 环境变量的路。

把决定权交给每个库是行不通的：httpx 只读环境变量，requests / urllib
  还看 WinINET，curl 只看环境变量 —— 同一个下拉框会读出三种结果。
  后果是「某一处报通、另一处连不上」，而界面上看不出差别。
  所以由本模块解析出一个地址，再投影到环境：只有一个口径。

## 覆盖到谁，覆盖不到谁（别读成"全部"）

覆盖：模型 provider（openai 兼容 / anthropic）· `web_search` / `web_fetch` ·
Telegram 通道 · 只认环境变量的第三方 SDK（飞书 `lark_oapi`
建 WS 时裸调 `requests.post`）—— 后者靠 `apply_env_policy()` 把三档落到
`os.environ` 上，在框架启动与用户改设置两处调用。

⛔ 管不到：`mail_parser` 的 IMAP/SMTP（不是 HTTP）· stdio 型 MCP。
界面文案写「所有出网 HTTP」，不写「所有工具」。

## 凭据

`proxy_url` 允许带 `user:pass@`（很多人就是这么用公司代理的），
它当凭据对待：`GET /api/settings` 走 `masked()`、日志里走 `redact()`。
`data/settings.json` 本来就在 `DEFAULT_FORBIDDEN_PATHS` 里（模型读不到）。
"""

from __future__ import annotations

import json
import os
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "SETTING_MODE", "SETTING_URL", "MODE_SYSTEM", "MODE_DIRECT", "MODE_MANUAL",
    "MODES", "PROXY_SCHEMES", "state", "mode", "proxy_url", "httpx_kwargs",
    "httpx_client", "is_local_target", "validate_url", "fingerprint",
    "requests_proxies", "curl_proxies", "apply_env_policy", "redact", "describe", "effective",
]

#: `data/settings.json` 里的两个键（分开存：模式是一个枚举，地址是一段文本）。
SETTING_MODE = "proxy_mode"
SETTING_URL = "proxy_url"

MODE_SYSTEM = "system"
MODE_DIRECT = "direct"
MODE_MANUAL = "manual"

#: 合法的三档（顺序 = 界面上拉的顺序）。
MODES: tuple[str, ...] = (MODE_SYSTEM, MODE_DIRECT, MODE_MANUAL)

#: 相对路径：`tests/_sandbox.py::sandbox()` 靠 chdir 生效（同 `core/i18n.py`）。
_SETTINGS = Path("data/settings.json")

#: 会读的代理环境变量（只用于如实报告，不参与决策）。
_ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
             "ALL_PROXY", "all_proxy")


def _raw() -> dict:
    try:
        data = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def state() -> tuple[str, str]:
    """`(mode, url)`。读不到 / 坏值，`(system, "")` —— 不是抛异常。

    读不到 ≠ manual：设置文件坏了就回默认档，那正是"什么都不做"。
    """
    raw = _raw()
    code = str(raw.get(SETTING_MODE) or "").strip().lower()
    if code not in MODES:
        code = MODE_SYSTEM
    url = normalize_url(str(raw.get(SETTING_URL) or "")) if code == MODE_MANUAL else ""
    return code, url


def normalize_url(url: str) -> str:
    """`127.0.0.1:7890` 这种写法补成 `http://127.0.0.1:7890`。

    判据：用户填"端口"是常态 —— WinINET 的 `ProxyServer` 本身就不带 scheme，
      而界面上照抄那个形状最省事。已经有 scheme 的原样返回，不改别的。
    """
    s = str(url or "").strip()
    if not s or "://" in s:
        return s
    return "http://" + s


def mode() -> str:
    return state()[0]


def proxy_url() -> str:
    """生效的代理地址（`manual` 之外一律空串）。"""
    return state()[1]


# ── 「跟随系统」到底指哪一份配置 ───────────────────────────────────────
#
# Windows 把代理放在两处：
#   ① WinINET —— `HKCU\...\Internet Settings` 的 `ProxyEnable` / `ProxyServer`。
#      代理软件每次开关都会改写它（关掉时改回 0），所以它代表"此刻的选择"。
#   ② 进程环境变量 —— `ALL_PROXY` 一族。常是 `setx` 或代理软件的"环境变量"
#      选项写进去的，软件退出时不会清理，关掉代理之后它还挂着。
#
# 判据：① 优先；② 只作兜底，而且必须连得上才算数。
#   于是"开代理走它、关代理直连"是读 ① 的自然结果，不需要重试魔法。
#   两处都在运行时读，不写死任何端口。
# 覆盖不到：PAC（`AutoConfigURL`）不解析 —— 只配了 PAC 的机器按直连走并出声。

_WININET_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
_WININET_TTL_S = 2.0
#: [读的时刻, 地址]。不用 `lru_cache` —— 它不能过期，而代理软件随时会改这一格。
_wininet_cache: list = [0.0, ""]

#: 可达性探测的结果：`{url: (时刻, 通不通)}`。
_REACH_TTL_S = 3.0
_reach_cache: dict = {}

#: 我们亲手写进 `os.environ` 的那几格，值是这样。
#: 判据：读"环境里有什么"时跳过自己写的那份，否则会把上游结果当成机器配置，
#:   而且一旦写坏（把死的写成活的）就再也看不出真相。
_OURS: dict = {}


def _from_proxy_server(server: str) -> str:
    """WinINET `ProxyServer` 的两种写法 -> 一个能直接用的地址。

    · `host:port`                       —— 所有协议同一台
    · `http=host:port;https=host:port;socks=host:port`

    WinINET 的 `https=` 指的是"给 https 用的代理"，那个代理本身仍然是
      HTTP CONNECT，所以 scheme 记 `http`；只有 `socks=` 才换成 `socks5`。
    """
    s = str(server or "").strip()
    if not s:
        return ""
    if "=" not in s:
        return "http://" + s
    parts = {}
    for item in s.split(";"):
        k, _, v = item.partition("=")
        if k.strip() and v.strip():
            parts[k.strip().lower()] = v.strip()
    if "socks" in parts:
        return "socks5://" + parts["socks"]
    for k in ("https", "http"):
        if k in parts:
            return "http://" + parts[k]
    return "http://" + next(iter(parts.values()))


def _wininet_proxy() -> str:
    """Windows 系统代理开着的那个地址；没开 / 读不到 / 非 Windows 一律 `""`。"""
    now = time.time()
    if now - _wininet_cache[0] < _WININET_TTL_S:
        return _wininet_cache[1]
    url = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WININET_KEY) as k:
            enable = int(winreg.QueryValueEx(k, "ProxyEnable")[0] or 0)
            if enable:
                url = _from_proxy_server(
                    winreg.QueryValueEx(k, "ProxyServer")[0])
    except Exception:                                    # noqa: BLE001
        url = ""
    _wininet_cache[0], _wininet_cache[1] = now, url
    return url


def _env_proxy() -> tuple[str, str]:
    """机器上（不是我们写的）那个代理变量。回 `(url, 变量名)`，没有就 `("", "")`。

    顺序照 `HTTPS_PROXY` -> `HTTP_PROXY` -> `ALL_PROXY`：前两个更具体。
    """
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy"):
        v = (os.environ.get(key) or "").strip()
        #: 按大写比对：Windows 的 `os.environ` 大小写不敏感，`https_proxy`
        #:   与 `HTTPS_PROXY` 是同一格 —— 用原键记账会把"自己写的"漏成"别人的"。
        if v and _OURS.get(key.upper()) != v:
            return v, key
    return "", ""


def _reachable(url: str) -> bool:
    """TCP 连得上吗（不看协议，不做握手）。结果缓存几秒 —— 代理随时会被关掉。"""
    import socket
    key = str(url or "").strip()
    if not key:
        return False
    now = time.time()
    hit = _reach_cache.get(key)
    if hit and now - hit[0] < _REACH_TTL_S:
        return hit[1]
    ok = False
    try:
        parts = urlsplit(key)
        host = parts.hostname or ""
        port = parts.port or (1080 if (parts.scheme or "").startswith("socks")
                              else 8080)
        if host:
            with socket.create_connection((host, port), timeout=0.35):
                ok = True
    except Exception:                                    # noqa: BLE001
        ok = False
    _reach_cache[key] = (now, ok)
    return ok


def system_proxy() -> tuple[str, str]:
    """「跟随系统」的解析结果：`(url, source)`。

    `source`：`wininet` · `env:<变量名>`（可能不可达）· `none`。
    返回空 url 且 source 以 `env:` 开头 = 机器上有那么一条环境变量代理，
      但它连不上 —— 调用方按直连走，并把这句话说给用户听
      （`describe()` 的 `hint` 就是它）。
    """
    w = _wininet_proxy()
    if w:
        return w, "wininet"
    url, key = _env_proxy()
    if url:
        return (url if _reachable(url) else ""), "env:" + key
    return "", "none"


def _write_env(url: str) -> list[str]:
    """把"这一次该走的代理"投影到环境变量上（空串 = 删掉）。

    为什么投影而不是"直接给客户端传参"：`NO_PROXY` 与"本机模型直连"
      这两条语义只有环境变量那条路才有；显式 `proxy=` 会把 127.0.0.1
      也塞进代理（本地 ollama / lmstudio 直接坏掉）。
    """
    changed: list[str] = []
    for key in _ENV_KEYS:
        if not url:
            if key in os.environ:
                os.environ.pop(key, None)
                _OURS.pop(key.upper(), None)
                changed.append(key)
            continue
        if os.environ.get(key) != url:
            os.environ[key] = url
            _OURS[key.upper()] = url
            changed.append(key)
    return changed


def refresh_system_env() -> list[str]:
    """`system` 档下把解析结果落到环境变量；其它档不动。回改了哪几个变量。

    调用点：三个客户端 getter（每次出网前一次，命中缓存时只是几次字符串比较）
      与 `apply_env_policy()`。只有 `system` 档会来这里 —— 另外两档是用户显式选的。
    """
    if mode() != MODE_SYSTEM:
        return []
    url, _src = system_proxy()
    return _write_env(url)


# 造客户端时用的那一组参数（唯一一处）

def is_local_target(url: str | None) -> bool:
    """环回 / 私网 / localhost —— 这类目标一律直连。

    本机模型（ollama / lmstudio 在 `127.0.0.1:11434`）与本地 MCP 服务器
    被代理接走时，代理连不上目标，回来一句「上游回了 HTTP 502」，而用户
    看到的是「本机模型连不上」。

    判据放在这里（不是 `web/server.py`）：设置页的联通测试与真请求必须
    用同一条规则 —— 只给探针加直连、真请求照旧走 `manual` 的代理，症状是
    「测试通过、对话失败」，而两个数都"对"。
    """
    import ipaddress
    from urllib.parse import urlsplit as _split

    host = (_split(str(url or "")).hostname or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "::1") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def httpx_kwargs(url: str | None = None) -> dict:
    """`httpx.Client(...)` / `httpx.AsyncClient(...)` 的代理那几格。

    *url* 是这一次要访问的目标：给了且是本机 / 内网，一律直连 —— 三档
    （`system` / `direct` / `manual`）都成立。`manual` 档尤其要在这儿拦：
    显式 `proxy=` 之后 `NO_PROXY` 已经不起作用（见下），本机模型会被送进
    用户填的那个代理。
    """
    if url and is_local_target(url):
        #: 显式 `proxy: None` 而不是只靠 `trust_env=False`：后者只关掉
        #:   环境变量，管不住上面 `manual` 那一支显式给的地址。
        return {"trust_env": False, "proxy": None}
    code, _url = state()
    if code == MODE_DIRECT:
        return {"trust_env": False}
    if code == MODE_MANUAL and _url:
        # 显式给 `proxy` 之后仍关掉 `trust_env`：否则环境变量里的
        #    `NO_PROXY` 会悄悄旁路掉用户自己填的代理（"我填了它却不走"）。
        return {"trust_env": False, "proxy": _url}
    #: `system`：先把解析结果投影到环境，再交给 httpx 自己读 ——
    #:   这样 `NO_PROXY`（本机模型直连）照旧有效，而环境里已经是"该走的那个"。
    refresh_system_env()
    return {"trust_env": True}


def httpx_client(module=None, *, url: str | None = None, **kwargs):
    """造一个 httpx 客户端并把代理参数合进去。

    *module* 传 `httpx` 或 `httpx.AsyncClient` —— 由调用方给，**本模块不 import
    httpx**（它是可选依赖：不装 httpx 的路径不该因为 import 本模块而崩）。

    *url* 是这一次要访问的目标：本机 / 内网目标一律直连（见 `httpx_kwargs`）。
    调用方知道目标就必须给 —— 不给等于只按全局代理档走，而本机模型正是
    「全局档对、这一条错」的那一类。
    """
    import httpx                                    # noqa: F401  (可选依赖，见上)

    cls = module or httpx.Client
    merged = dict(httpx_kwargs(url))
    merged.update(kwargs)
    return cls(**merged)


def fingerprint() -> tuple:
    """当前代理配置的指纹 —— 给"客户端缓存"当判据。

    为什么需要它：`providers/openai_compat.py` 会复用 `OpenAI` 客户端
      （复用连接池 = 不重复 TCP/TLS 握手）。而客户端里烘着代理配置，
      设置页把「跟随系统」切成「直连」之后，旧的客户端还在走代理，
      用户看到的是"改了没反应"（切了直连仍然连不上）。
判据从"key 变没变"扩成"(key, 代理配置) 变没变"。

    只放模式与地址，不放时间戳：指纹一样就该不重建（否则等于没有缓存）。

    `system` 档还要带上解析结果：代理软件开关 WinINET 时（Clash 打开 /
      关掉），客户端得重建，否则它会一直拿着开关那一刻的环境变量
      —— 那正是"开了代理没反应、关了还报错"的来源。
    """
    code, url = state()
    if code == MODE_SYSTEM:
        return (code, url, system_proxy()[0])
    return (code, url, "")


@lru_cache(maxsize=1)
def _socks_env_without_pysocks() -> bool:
    """「环境给的是 SOCKS 代理」且「这台机器没装 PySocks」——进程内只判一次。

    `lru_cache` 而不是模块级布尔：那张会话态登记表会把
      「模块级、且被 `global` 写过」的名字当成状态抓出来（它确实是，只是
      不是会话状态），用一个没有副作用的缓存，省掉一条登记。
    用 `find_spec` 不 import —— 那条"产品代码 import 的包都要在锁里"的
      读者按 AST 数 `import` 语句，而 PySocks 现在不在锁里（见 `v0.11.757`）。
    """
    import importlib.util
    vals = [os.environ.get(k, "") for k in
            ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
             "HTTPS_PROXY", "https_proxy")]
    if not any(str(v).lower().startswith("socks") for v in vals):
        return False
    return importlib.util.find_spec("socks") is None


def warn_if_socks_unsupported() -> None:
    """环境里是 SOCKS 代理、而 `requests` 没有 SOCKS 支持，一次出声。

    `ALL_PROXY=socks5://127.0.0.1:<端口>`，而 venv 里没有 PySocks
任何 `requests` 调用抛 `InvalidSchema: Missing dependencies for SOCKS support`，
    而那句话看不出该装什么（钉钉的回复 / 主动推送 / 附件下载都在这条线上）。
    只提醒一次（每次出网都打一行就是噪声）。
    """
    if _socks_env_without_pysocks():
        print("[net_proxy] 环境里给的是 SOCKS 代理，但这台机器没装 PySocks ⇒ "
              "requests 系调用会报 Missing dependencies for SOCKS support。"
              "装一下：pip install PySocks（或把代理地址写成 http://）。")


def requests_proxies() -> dict | None:
    """`requests` / `urllib` 的 `proxies=`（curl_cffi 用 `curl_proxies()` —— 语义不同）。

    * `None`，什么都别传（跟随环境变量，= 今天的行为）
    * `{"http": "", "https": ""}`，显式直连（见下面那段实际）
    * 否则两个键都指向同一个地址

    别改回 `{"http": None, "https": None}`（那是 requests 文档的写法，
    但在 `ALL_PROXY` 存在时不管用）。本机（requests 2.x，
    `ALL_PROXY=socks5://127.0.0.1:<端口>` 且代理没开）：

        proxies={"http": None, "https": None}  -> 走 SOCKS，ConnectionError
        proxies={"http": "",  "https": ""}     -> 直连成功

    根因在 `requests.sessions.merge_setting` 的最后一步：**值等于 `None` 的键会被
    整条删掉**；删掉之后 `resolve_proxies` 再把环境里的 `all`（= `ALL_PROXY`）
    setdefault 回来，"显式直连"反而变成了走代理。
    空串不会被删，且 `select_proxy` 返回空串（假值），适配器认作直连。
    与 `curl_proxies()` 那条 bug 同一族（那边是 `{"all": ""}`）——
    两张表在 direct 下必须不同，照抄就是 bug。
    """
    code, url = state()
    if code == MODE_DIRECT:
        return {"http": "", "https": ""}
    if code == MODE_MANUAL and url:
        return {"http": url, "https": url}
    #: `system`：先把解析结果投影到环境，再让 requests 自己读（同上一条判据）。
    refresh_system_env()
    warn_if_socks_unsupported()
    return None


#: 会被我们改写/删除的代理环境变量（大小写都算 —— Windows 与 *nix 两套写法）。
_ENV_KEYS: tuple[str, ...] = (
    "ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY",
    "all_proxy", "http_proxy", "https_proxy",
)

#: import 那一刻的快照（之后只读）—— `system` 档用它把变量放回去。
#: 刻意不做成可变状态：census 会把
#:   「import 之后被写过」的模块级名字扫出来（这里只在 `apply_env_policy`
#:   里读，写的是 `os.environ`）。
_ENV_SNAPSHOT: dict[str, str] = {
    k: os.environ[k] for k in _ENV_KEYS if k in os.environ
}


def apply_env_policy() -> list[str]:
    """把三档落到 `os.environ` 上 —— 只给"只认环境变量"的 SDK 用。

    为什么非做不可：飞书 SDK `lark_oapi` 建 WebSocket 时
      裸调 `requests.post`（不传 `proxies=`），requests 回退到环境变量。
      设置页选了「直连」，而系统环境里 `ALL_PROXY=socks5://127.0.0.1:<端口>`
      且那个代理没开，飞书每 2 分钟重连一次、每次都
      `SOCKSHTTPSConnectionPool … [WinError 10061]`，用户侧看到的是发消息没反应。
      （这条与模块开头那段"静默采纳环境变量"是同一个病，只是不在我们自己的调用点上。）

    判据（与 `requests_proxies()` 同一张三档表）：
      · `direct`，删掉六个变量
      · `manual`，六个都写成那个地址
      · `system`，写成解析结果（`system_proxy()`），解析成直连就删掉 ——
        不再"放回 import 时的快照"：那份快照里可能正是一条没人清理的死代理，
        放回去等于把病因重新装回去。
    返回被改动的变量名（给日志用；值打码，别把带凭据的地址写进日志）。

    这是进程级副作用，只许在「框架启动」与「用户改设置」两处调用
    （外加每次出网前那一次 `refresh_system_env()` —— 它只看 `system` 档）。
    """
    code, url = state()
    if code == MODE_SYSTEM:
        return _write_env(system_proxy()[0])
    if code == MODE_MANUAL and url:
        return _write_env(url)
    return _write_env("")


def curl_proxies() -> dict | None:
    """`curl_cffi`（libcurl）的 `proxies=` —— 与 `requests_proxies()` 不是同一张表。

    条件（curl_cffi 0.15.0，`ALL_PROXY=http://127.0.0.1:1`）：

        _cr.get(url)                                   -> 走了环境代理（失败）
        _cr.get(url, proxies={"http": None, "https": None}) -> 仍然走环境代理（失败）
        _cr.get(url, proxies={"all": ""})              -> 直连（成功）

`{"http": None, ...}` 在 curl_cffi 里解析成 `proxy=None`，**根本不下发
    CURLOPT_PROXY**，libcurl 照旧读 `ALL_PROXY`。`direct` 档等于没生效
    （症状：设置页显示「直连」，搜索仍然报 `127.0.0.1:<端口> 连不上`）。
    只有空串才是"显式把代理置空"。

    * `None`，什么都不传（跟随环境变量 = `system` 档）
    * `{"all": ""}`，显式直连
    * `{"all": url}`，指定代理
    """
    code, url = state()
    if code == MODE_DIRECT:
        return {"all": ""}
    if code == MODE_MANUAL and url:
        return {"all": url}
    refresh_system_env()
    return None

# 给人看的那一行（设置页 + 排障）

#: 允许的代理协议。不收别的（`ftp://` / 裸 `host:port` 都不收）——
#: 一个填错的 scheme 会在第一次请求时才炸，而那时用户看到的是"网络异常"。
PROXY_SCHEMES: tuple[str, ...] = ("http", "https", "socks5", "socks5h", "socks4")


def validate_url(url: str) -> str:
    """`""` = 合法；否则返回一句给用户看的拒绝理由。

    只做结构校验（scheme + host + 端口范围）—— 它不连那个代理：
       "通不通"要等真正发请求才知道，而这里判的是"这个串长得像不像代理地址"。
    """
    s = normalize_url(url)
    if not s:
        return "代理地址不能为空"
    try:
        parts = urlsplit(s)
    except ValueError:
        return "代理地址异常：%s" % s
    if parts.scheme.lower() not in PROXY_SCHEMES:
        return "代理地址异常：%s（协议只支持 %s）" % (s, " / ".join(PROXY_SCHEMES))
    if not parts.hostname:
        return "代理地址异常：%s（缺少主机名）" % s
    try:
        port = parts.port
    except ValueError:
        return "代理地址异常：%s（端口不是数字）" % s
    if port is not None and not (1 <= port <= 65535):
        return "代理地址异常：%s（端口需在 1–65535）" % s
    return ""


def redact(url: str) -> str:
    """把 `user:pass@` 打掉，只留 `scheme://***@host:port`。"""
    s = str(url or "").strip()
    if not s:
        return ""
    try:
        parts = urlsplit(s)
    except ValueError:
        return "***"
    if parts.username or parts.password:
        host = parts.hostname or ""
        netloc = "***@" + host + (":%d" % parts.port if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
    return s


def effective() -> str:
    """此刻真正走的是什么 —— 不含"设置里写的是什么"。"""
    code, url = state()
    if code == MODE_DIRECT:
        return "direct"
    if code == MODE_MANUAL and url:
        return redact(url)
    u, _src = system_proxy()
    return redact(u) if u else "direct"


def describe() -> dict:
    """设置页那一行 + `GET /api/settings` 的载荷。

    返回 `{mode, url, effective, source, hint}`：
      * `url` —— 用户在输入框里看到的（打码）
      * `effective` —— 当前实际生效的那个（打码）
      * `source` —— 它是从哪儿来的（设置 / 哪个环境变量 / 无）
      * `hint` —— 一句话（给界面直接显示；不在这里做 i18n 判断）
    """
    code, url = state()
    env_key = _env_proxy()[1]
    if code == MODE_DIRECT:
        source = "setting"
        hint = "" if not env_key else "env_ignored"
    elif code == MODE_MANUAL and url:
        source = "setting"
        hint = "manual"
    else:
        #: `system`：报的是解析结果，不是"环境里碰巧有什么"。
        #:   `env_dead` = 机器上有那么一条环境变量代理，但它连不上，按直连走
        #:   （界面据此说「环境里的代理 X 连不上，本次直连」）。
        u, src = system_proxy()
        if u:
            source = "wininet" if src == "wininet" else src.split(":", 1)[-1]
            hint = "wininet" if src == "wininet" else "env"
        elif src.startswith("env:"):
            source = src.split(":", 1)[-1]
            hint = "env_dead"
        else:
            source = ""
            hint = "none"
    return {
        "mode": code,
        "url": redact(url),
        "effective": effective(),
        "source": source,
        "hint": hint,
        "env_keys": [k for k in _ENV_KEYS if (os.environ.get(k) or "").strip()],
    }
