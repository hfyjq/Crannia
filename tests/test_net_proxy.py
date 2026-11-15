# -*- coding: utf-8 -*-
"""出网代理（`core/net_proxy.py`）的机械读者。

## 钉住四件事

1. 三档各自造出什么（`system` / `direct` / `manual`）——
   默认（读不到设置）必须是 `system` = 今天的行为。
2. 地址校验与打码（`validate_url` / `redact`）——
   代理地址是凭据（可能带 `user:pass@`），不许原样进日志/载荷。
3. `describe()` 说的是真话：它报的 `effective` 必须与
   `httpx_kwargs()` 实际会用的那个一致（标签不许说谎）。
4. 反查：全仓造 HTTP 客户端的地方不许绕过它 ——
   这一条是防止"半年后又有人自己 `httpx.Client()` 造一个"。

ASCII-only 输出（Windows 控制台是 GBK）。

用法：
    .venv/Scripts/python.exe tests/test_net_proxy.py
"""

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


class _Sandbox:
    """临时 cwd —— `core/net_proxy.py` 的路径是相对的。"""

    def __init__(self):
        self.tmp = None
        self.old = None
        self.old_env = {}

    def __enter__(self):
        #: 开发机上 `ALL_PROXY` 等变量是真的存在的（本仓库踩过）-> 先摘干净，
        #:   否则"默认档读环境变量"这一条会读到机器自己的代理，测试变成环境依赖。
        for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                  "ALL_PROXY", "all_proxy"):
            if k in os.environ:
                self.old_env[k] = os.environ.pop(k)
        #: `_OURS` 是我们自己写进 env 的那几格的账 —— 跨测试留着会让下一个
        #:   测试把上一轮写进去的地址当成"机器上本来就有的"。
        try:
            _np()._OURS.clear()
            _np()._reach_cache.clear()
        except Exception:                              # noqa: BLE001
            pass
        self.old = Path.cwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        Path("data").mkdir()
        return Path(self.tmp.name)

    def __exit__(self, *exc):
        os.chdir(self.old)
        #: 先全清再还原：测试自己写进去的六个变量（`manual` 那一条会把六个都写）
        #:   如果不在 `old_env` 里，原来就留在了进程里，下一个测试读到的
        #:   是上一个测试留下的代理。
        for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                  "ALL_PROXY", "all_proxy"):
            os.environ.pop(k, None)
        for k, v in self.old_env.items():
            os.environ[k] = v
        try:
            _np()._OURS.clear()
        except Exception:                              # noqa: BLE001
            pass
        self.tmp.cleanup()
        return False


def _settings(**kw):
    Path("data/settings.json").write_text(json.dumps(kw), encoding="utf-8")


def _np():
    from core import net_proxy
    return net_proxy


#: 「跟随系统」现在会读 Windows 系统代理 —— 测试不许依赖跑测试那台机器的
#:   注册表与环境（`_Sandbox` 已经摘掉环境变量）。这两把夹具把两件事钉死：
#:   WinINET 读出什么、某个代理地址连不连得上。
import contextlib                                          # noqa: E402


@contextlib.contextmanager
def _wininet(url=""):
    np = _np()
    old = np._wininet_proxy
    np._wininet_proxy = lambda: url
    try:
        yield
    finally:
        np._wininet_proxy = old


@contextlib.contextmanager
def _reachable(ok):
    np = _np()
    old = np._reachable
    np._reachable = lambda _u: ok
    try:
        yield
    finally:
        np._reachable = old


# 1. 三档

def test_default_is_system():
    with _Sandbox(), _wininet(""):
        np = _np()
        check("没设置 => mode=system（= 今天的行为）", np.mode() == "system", np.mode())
        check("system 的 httpx 参数 = trust_env 开",
              np.httpx_kwargs() == {"trust_env": True}, repr(np.httpx_kwargs()))
        check("system 的 requests 参数 = 什么都不传（None）",
              np.requests_proxies() is None, repr(np.requests_proxies()))
        #: curl_cffi 不认 `{"http": None}`（实测：照旧读 `ALL_PROXY`）->
        #:    `direct` 必须是 `{"all": ""}`。这一条就是那个现场 bug 的读者。
        check("★ system 的 curl 参数 = 什么都不传（None）",
              np.curl_proxies() is None, repr(np.curl_proxies()))
        check("system 且没有环境变量 => effective 是 direct",
              np.effective() == "direct", np.effective())


def test_direct_ignores_env():
    with _Sandbox():
        np = _np()
        _settings(proxy_mode="direct")
        check("direct => httpx trust_env 关",
              np.httpx_kwargs() == {"trust_env": False}, repr(np.httpx_kwargs()))
        check("direct => requests 给**空串**（不是 None —— 见下面那条）",
              np.requests_proxies() == {"http": "", "https": ""},
              repr(np.requests_proxies()))
        check("★★ direct => curl 给 `{\"all\": \"\"}`（空串才是显式直连）",
              np.curl_proxies() == {"all": ""}, repr(np.curl_proxies()))
        check("★★ 两张表**在 direct 下必须不同** —— 照抄就是那个 bug",
              np.curl_proxies() != np.requests_proxies(),
              "%r vs %r" % (np.curl_proxies(), np.requests_proxies()))

        # 行为判据：
        #:   `{"http": None}` 在 `ALL_PROXY` 存在时不是直连 ——
        #:   `requests.sessions.merge_setting` 会把值为 None 的键整条删掉，
        #:   然后 `resolve_proxies` 把环境里的 `all` 补回来 -> 显式直连走成了代理。
        #:   这一条走的是生产同一套 API（`merge_environment_settings` +
        #:   `select_proxy`），不联网。
        os.environ["ALL_PROXY"] = "socks5://127.0.0.1:1"
        try:
            import requests as _rq
            sess = _rq.Session()
            merged = sess.merge_environment_settings(
                "https://example.com", dict(np.requests_proxies()),
                None, None, None)["proxies"]
            picked = _rq.utils.select_proxy("https://example.com", merged)
            check("★★ direct 下**选出来的代理必须为空**（否则就是走死代理）",
                  not picked, "picked=%r merged=%r" % (picked, merged))
        finally:
            os.environ.pop("ALL_PROXY", None)
        os.environ["ALL_PROXY"] = "socks5://127.0.0.1:7897"
        try:
            check("★★ 环境变量里有代理，direct 仍然报 direct",
                  np.effective() == "direct", np.effective())
        finally:
            os.environ.pop("ALL_PROXY", None)


def test_manual_uses_the_url():
    with _Sandbox():
        np = _np()
        _settings(proxy_mode="manual", proxy_url="http://127.0.0.1:7890")
        kw = np.httpx_kwargs()
        check("manual => 显式 proxy",
              kw.get("proxy") == "http://127.0.0.1:7890", repr(kw))
        check("★★ manual 也会关 trust_env（否则 NO_PROXY 会偷偷旁路掉它）",
              kw.get("trust_env") is False, repr(kw))
        check("manual => requests 两个键都指向它",
              np.requests_proxies() == {"http": "http://127.0.0.1:7890",
                                        "https": "http://127.0.0.1:7890"},
              repr(np.requests_proxies()))
        check("manual => curl 也给同一个地址",
              np.curl_proxies() == {"all": "http://127.0.0.1:7890"},
              repr(np.curl_proxies()))
        check("manual 但地址是空的 => 退回 system 的行为（不静默当成直连）",
              _manual_without_url(np) == {"trust_env": True},
              repr(_manual_without_url(np)))


def _manual_without_url(np):
    Path("data/settings.json").write_text(
        json.dumps({"proxy_mode": "manual", "proxy_url": ""}), encoding="utf-8")
    return np.httpx_kwargs()


# 1b. 本机 / 内网目标：三档都直连

def test_local_targets_bypass_every_mode():
    """本地模型（ollama / lmstudio）与本地 MCP 服务器一律直连。

    判据必须是同一条：设置页的联通测试与真请求共用一个 `is_local_target`。
    只给探针直连、真请求照旧走 `manual` 的代理，症状是「联通测试成功、
    实际对话失败」—— 而两个数各自都"对"。
    """
    np = _np()

    check("127.0.0.1 是本机", np.is_local_target("http://127.0.0.1:11434"))
    check("localhost 是本机", np.is_local_target("http://localhost:1234/v1"))
    check("[::1] 是本机", np.is_local_target("http://[::1]:8000"))
    check("192.168.x 是内网", np.is_local_target("http://192.168.1.9:8080"))
    check("公网域名不是", not np.is_local_target("https://api.deepseek.com"))
    check("空串不是", not np.is_local_target(""))
    check("None 不是", not np.is_local_target(None))

    with _Sandbox(), _wininet("http://proxy.example:8080"):
        _settings(proxy_mode="manual", proxy_url="http://10.1.2.3:3128")
        check("manual 档：公网目标仍用那个代理",
              np.httpx_kwargs("https://api.deepseek.com")
              == {"trust_env": False, "proxy": "http://10.1.2.3:3128"},
              repr(np.httpx_kwargs("https://api.deepseek.com")))
        for u in ("http://127.0.0.1:11434/v1", "http://localhost:1234/v1",
                  "http://192.168.1.9:8080", "http://[::1]:8000"):
            check("★ manual 档：本机/内网目标直连 %s" % u,
                  np.httpx_kwargs(u) == {"trust_env": False, "proxy": None},
                  repr(np.httpx_kwargs(u)))

    with _Sandbox(), _wininet("http://proxy.example:8080"), _reachable(True):
        check("system 档：本机目标同样直连",
              np.httpx_kwargs("http://127.0.0.1:11434")
              == {"trust_env": False, "proxy": None},
              repr(np.httpx_kwargs("http://127.0.0.1:11434")))


def test_provider_and_probe_share_the_local_rule():
    """Provider 造客户端时把 base_url 交进去；探针用同一个函数。

    两条都钉：只改一边（探针直连 / 真请求走代理）就是那个「测通、用不通」的现场。
    """
    np = _np()
    from providers import openai_compat as oc

    got: dict = {}

    def _fake(module=None, *, url=None, **kw):
        got["url"] = url
        raise RuntimeError("到此为止，不真建客户端")

    old = np.httpx_client
    np.httpx_client = _fake
    try:
        #: 构造器自己就建客户端（`_build_client` 是唯一构造点），
        #:   所以桩在构造这一步就会抛 —— 在那之前记下的 `url` 正是要断言的东西。
        try:
            oc.OpenAICompatProvider(
                api_key="k", base_url="http://127.0.0.1:11434/v1", model="local")
        except RuntimeError:
            pass
    finally:
        np.httpx_client = old

    check("★ Provider 把 base_url 交给了 httpx_client",
          str(got.get("url") or "").startswith("http://127.0.0.1:11434"),
          repr(got))

    #: 读源码而不是 import `web.server` —— 那个模块一 import 就建 Flask app。
    src = (Path(__file__).resolve().parent.parent / "web" / "server.py"
           ).read_text(encoding="utf-8")
    check("★ 探针走同一条判据（不再自带一份 _is_local_target）",
          "_is_local_target" not in src, "web/server.py 里仍有本地副本")
    check("探针把 url= 交给 httpx_client",
          "httpx_client(url=url" in src, "没找到 httpx_client(url=url")


# 2. 校验与打码

def test_validate_and_redact():
    np = _np()
    check("合法 http 地址", np.validate_url("http://127.0.0.1:7897") == "")
    check("合法 socks5 地址", np.validate_url("socks5://127.0.0.1:7897") == "")
    check("空串 => 拒", np.validate_url("") != "")
    #: 裸 `host:port` 也收（WinINET 的 `ProxyServer` 本来就不带 scheme，
    #: 照抄那个形状最省事）—— 补成 `http://`，不是拒。
    check("★ 裸 host:port => 补成 http:// 而不是拒",
          np.validate_url("127.0.0.1:7897") == ""
          and np.normalize_url("127.0.0.1:7897") == "http://127.0.0.1:7897",
          np.normalize_url("127.0.0.1:7897"))
    check("★ 不认识的 scheme => 拒", np.validate_url("ftp://h:1") != "")
    check("端口越界 => 拒", np.validate_url("http://h:99999") != "")
    check("没有主机名 => 拒", np.validate_url("http://:7897") != "")
    got = np.redact("socks5://user:pass@127.0.0.1:7897")
    check("★★ 打码：凭据不许出现在明文里",
          "user" not in got and "pass" not in got and "127.0.0.1:7897" in got, got)
    check("没凭据的地址原样返回",
          np.redact("http://127.0.0.1:7897") == "http://127.0.0.1:7897")


# 3. describe 说的是真话

def test_describe_matches_reality():
    with _Sandbox():
        np = _np()
        _settings(proxy_mode="manual", proxy_url="socks5://u:p@127.0.0.1:7897")
        d = np.describe()
        check("★★ describe 报的就是 httpx 会用的那个（打码后）",
              d["effective"] == np.redact("socks5://u:p@127.0.0.1:7897"), repr(d))
        check("describe 的 url 字段也是打码的",
              "u:p" not in d["url"], repr(d["url"]))
        _settings(proxy_mode="direct")
        check("direct 的 describe 说 direct", np.describe()["effective"] == "direct")

        os.environ["HTTPS_PROXY"] = "http://10.0.0.1:8888"
        try:
            os.remove("data/settings.json")   # 回默认档
            with _wininet(""), _reachable(True):
                d = np.describe()
                check("★★ 默认档 + 有环境变量（连得上）=> 报出**那个变量名**",
                      d["source"] == "HTTPS_PROXY" and "10.0.0.1" in d["effective"],
                      repr(d))
            with _wininet(""), _reachable(False):
                d = np.describe()
                check("★★ 同一条环境变量**连不上** => 按直连走，并说出来",
                      d["effective"] == "direct" and d["hint"] == "env_dead",
                      repr(d))
        finally:
            os.environ.pop("HTTPS_PROXY", None)


# 4. 反查：不许绕过它自己造客户端

#: 允许不经过 `net_proxy` 的构造点。每条都要写清为什么。
_ALLOWED = {
    # 本模块自己就是造客户端的那一处。
    "core/net_proxy.py",
}

_FLAGS = ("httpx.Client(", "httpx.AsyncClient(", "OpenAI(", "AsyncOpenAI(",
          "urlopen(", "requests.get(", "requests.post(", ".post(", ".get(")


def _calls(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        try:
            text = ast.unparse(n)
        except Exception:                                  # noqa: BLE001
            continue
        head = text.split("(")[0]
        if head.endswith(("httpx.Client", "httpx.AsyncClient", "OpenAI",
                          "AsyncOpenAI", "requests.get", "requests.post",
                          "urllib.request.urlopen", "urlopen")):
            out.append((n.lineno, head))
    return out


def test_no_client_bypasses_the_module():
    roots = ("core", "providers", "mcpx", "skills", "channels", "bus", "web")
    offenders = []
    for root in roots:
        for p in (_ROOT / root).rglob("*.py"):
            rel = p.relative_to(_ROOT).as_posix()
            if rel in _ALLOWED:
                continue
            src = p.read_text(encoding="utf-8", errors="replace")
            if "net_proxy" not in src:
                calls = _calls(p)
                if calls:
                    offenders.append((rel, calls[:3]))
                continue
            #: 文件里提到 net_proxy 还不够 —— 逐个调用点看这一行附近有没有用它。
            for n in ast.walk(ast.parse(src)):
                if not isinstance(n, ast.Call):
                    continue
                try:
                    text = ast.unparse(n)
                except Exception:                          # noqa: BLE001
                    continue
                head = text.split("(")[0]
                if not head.endswith(("httpx.Client", "httpx.AsyncClient", "OpenAI",
                                      "AsyncOpenAI", "requests.get", "requests.post",
                                      "urllib.request.urlopen", "urlopen")):
                    continue
                if "net_proxy" in text or "_np." in text:
                    continue
                offenders.append((rel + ":%d" % n.lineno, [head]))
    check("★★ 全仓造 HTTP 客户端的地方**都经过 `core/net_proxy`**"
          "（漏一处 = 那一处不受设置页管）", not offenders,
          "%d 处：%r" % (len(offenders), offenders[:5]))


def test_curl_cffi_call_sites_use_the_curl_table():
    """现场 bug 的读者：`web_fetch` 那条 GET 以前根本没传代理。

 现场：设置页是「直连」，搜索却报 `127.0.0.1:7897 连不上`。    两个原因叠在一起：
      ① `direct` 给的是 `{"http": None}` —— curl_cffi 把它解析成"不设代理"
         -> libcurl 照旧读 `ALL_PROXY`；
      ② `_http_get_once` 的 `_cr.get(...)` 一个 proxies 都没传。
    """
    src = (_ROOT / "mcpx" / "servers" / "web_search.py").read_text(encoding="utf-8")
    check("★ curl 的表来自 `curl_proxies()`，且**两条路都传了**",
          src.count("_np.curl_proxies()") == 2 and
          src.count('"proxies": _curl_proxies') == 2,
          "curl_proxies=%d proxies=%d" % (src.count("_np.curl_proxies()"),
                                          src.count('"proxies": _curl_proxies')))


def test_system_follows_the_system_proxy():
    """「跟随系统」= WinINET 优先，环境变量只在连得上时兜底。

    两份配置可以互相矛盾：代理软件退出时把 WinINET 的 `ProxyEnable` 改回 0，
    而用户级环境变量里的 `ALL_PROXY` 还挂着（那是持久的，没人负责清理）。
    把决定权交给各库时，httpx 认那条环境变量、urllib 还看注册表 —— 一处报通、
    一处连不上。解析成"一个地址"之后两边才有同一个答案。
    """
    with _Sandbox():
        np = _np()
        _settings()                                     # 默认档 = system
        with _wininet("http://127.0.0.1:7890"), _reachable(False):
            check("★ WinINET 开着 => 就用它（环境变量怎么说都不看）",
                  np.system_proxy() == ("http://127.0.0.1:7890", "wininet"),
                  repr(np.system_proxy()))
            np.refresh_system_env()
            check("而且它被投影到环境里（只认 env 的 SDK 也吃到）",
                  (os.environ.get("ALL_PROXY") or "") == "http://127.0.0.1:7890",
                  repr(os.environ.get("ALL_PROXY")))

        os.environ["ALL_PROXY"] = "socks5://127.0.0.1:7897"
        try:
            with _wininet(""), _reachable(True):
                check("★ WinINET 关着 + 环境变量连得上 => 用环境变量",
                      np.system_proxy() == ("socks5://127.0.0.1:7897", "env:ALL_PROXY"),
                      repr(np.system_proxy()))
            with _wininet(""), _reachable(False):
                check("★★ WinINET 关着 + 环境变量连不上 => 直连（不再撞死在它身上）",
                      np.system_proxy() == ("", "env:ALL_PROXY"),
                      repr(np.system_proxy()))
                np.refresh_system_env()
                check("★★ 而那条死变量被摘掉了 —— 只认 env 的库也不会再撞上去",
                      "ALL_PROXY" not in os.environ,
                      repr(os.environ.get("ALL_PROXY")))
        finally:
            os.environ.pop("ALL_PROXY", None)


def test_env_policy_reaches_env_only_sdks():
    """只认环境变量的 SDK 也要吃三档 —— 飞书 `lark_oapi` 的现场反馈。

 设置页选了「直连」，而系统环境里
    `ALL_PROXY=socks5://127.0.0.1:7897`（代理没开）-> 飞书 SDK 建 WS 时
    裸调 `requests.post`（不传 `proxies=`）-> 每 2 分钟重连一次、每次都
    `[WinError 10061]`，用户侧看到的是「发消息没反应」。
    """
    with _Sandbox():
        np = _np()
        os.environ["ALL_PROXY"] = "socks5://127.0.0.1:7897"
        try:
            _settings(proxy_mode="direct")
            changed = np.apply_env_policy()
            check("direct -> ALL_PROXY 被**删掉**（SDK 才会走直连）",
                  "ALL_PROXY" not in os.environ, repr(os.environ.get("ALL_PROXY")))
            check("direct 确实报了改动", "ALL_PROXY" in changed, repr(changed))

            _settings(proxy_mode="manual", proxy_url="http://127.0.0.1:7890")
            np.apply_env_policy()
            check("manual -> 六个变量都指向那个地址",
                  os.environ.get("ALL_PROXY") == "http://127.0.0.1:7890",
                  repr(os.environ.get("ALL_PROXY")))

            _settings(proxy_mode="system")
            #: system 不再"放回 import 时的快照"——那份快照里可能正是一条
            #: 没人清理的死代理，放回去等于把病因重新装回去。现在写的是
            #: 解析结果：WinINET 没开、环境里那条连不上，于是直连（删掉）。
            os.environ["ALL_PROXY"] = "socks5://127.0.0.1:7897"
            with _wininet(""), _reachable(False):
                np.apply_env_policy()
                check("★★ system + 死环境变量 -> 删掉（不是还原）",
                      "ALL_PROXY" not in os.environ,
                      repr(os.environ.get("ALL_PROXY")))
            with _wininet("http://127.0.0.1:7890"), _reachable(False):
                np.apply_env_policy()
                check("★★ system + WinINET 开着 -> 六个变量都指向它",
                      os.environ.get("ALL_PROXY") == "http://127.0.0.1:7890",
                      repr(os.environ.get("ALL_PROXY")))
        finally:
            if "ALL_PROXY" in np._ENV_SNAPSHOT:
                os.environ["ALL_PROXY"] = np._ENV_SNAPSHOT["ALL_PROXY"]
            else:
                os.environ.pop("ALL_PROXY", None)

def test_the_probe_uses_the_same_client_as_the_provider():
    """模型配置的「联通测试」必须与真请求同一台客户端。

    两个客户端对"当前代理是谁"的定义不同（urllib 还看 WinINET 注册表、
    不认 `ALL_PROXY`；httpx 只看环境变量），于是可能出现测试报通、
    真请求连不上。判据从"探针也读三档"升级成"与 provider 共用
    `net_proxy.httpx_client()`"—— 同一个工厂就不会各自漂。
    """
    import httpx

    np = _np()
    orig_client = httpx.Client
    orig_state = np.state
    captured: dict = {}

    class _Resp:
        status_code = 200
        content = b'{"ok":true}'

    class _FakeClient:
        def __init__(self, **kw):
            captured.clear()
            captured.update(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, headers=None, content=None):
            return _Resp()

    try:
        httpx.Client = _FakeClient
        import web.server as WS

        def _kw(url):
            captured.clear()
            WS._http_probe(url, {})
            return dict(captured)

        np.state = lambda: ("system", "")
        with _wininet(""), _reachable(False):
            kw = _kw("https://api.example.com/v1/models")
        check("system -> trust_env=True（与 provider 同一份环境变量口径）",
              kw.get("trust_env") is True, repr(kw))
        check("★ 不跟随重定向（每一跳都要自己过出网地板）",
              kw.get("follow_redirects") is False, repr(kw))

        np.state = lambda: ("direct", "")
        kw = _kw("https://api.example.com/v1/models")
        check("direct -> trust_env=False", kw.get("trust_env") is False, repr(kw))

        np.state = lambda: ("manual", "http://127.0.0.1:9911")
        kw = _kw("https://api.example.com/v1/models")
        check("manual -> 显式 proxy 且关掉 trust_env",
              kw.get("proxy") == "http://127.0.0.1:9911"
              and kw.get("trust_env") is False, repr(kw))

        kw = _kw("http://127.0.0.1:11434/v1/models")
        check("本机模型：即使 manual 也强制直连（proxy=None + trust_env=False）",
              kw.get("proxy") is None and kw.get("trust_env") is False, repr(kw))
    finally:
        httpx.Client = orig_client
        np.state = orig_state


def main() -> int:
    for fn in (test_default_is_system,
               test_direct_ignores_env,
               test_manual_uses_the_url,
               test_local_targets_bypass_every_mode,
               test_provider_and_probe_share_the_local_rule,
               test_validate_and_redact,
               test_describe_matches_reality,
               test_no_client_bypasses_the_module,
               test_curl_cffi_call_sites_use_the_curl_table,
               test_env_policy_reaches_env_only_sdks,
               test_system_follows_the_system_proxy,
               test_the_probe_uses_the_same_client_as_the_provider):
        print("\n-- %s --" % fn.__name__)
        try:
            fn()
        except Exception as exc:                           # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s 没抛异常（抛了 %s）" % (fn.__name__, type(exc).__name__),
                  False, str(exc))

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
