# -*- coding: utf-8 -*-
"""发布链：版本比较 / 渠道 manifest / 产物断言 / 安装方式。

## 钉住什么

1. `_parse_version` 认 `1.1.1-beta`，且预发布排在正式版之前
   （`1.1.1-beta < 1.1.1 < 1.1.2-alpha`）。
2. 两条渠道各自解析 URL；配置可覆盖；认不出的渠道按 stable 算。
3. 产物断言能抓四种真会发生的坏：没有解释器 / 平台写错 / 依赖清单是空的 /
   `resources/app` 缺文件。
4. `make_release.build_manifest` 写出来的东西能被客户端解析回来（回环）——
   否则"发布成功、客户端永远看不到"。
5. 安装改跑 NSIS `/S`：脚本里有 `/S`，且没有任何自替换（`Copy-Item` 到旧 exe）。

用法::

    .venv/Scripts/python.exe tests/test_release_manifest.py

ASCII-only 输出（Windows 控制台默认 GBK）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ROOT / "scripts"))

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
        print("  [FAIL] %s%s" % (label, ("  -- " + str(detail)) if detail else ""))


# 1. 版本比较

def test_beta_sorts_before_the_release():
    from core.updater import _parse_version as p
    check("★ 1.1.1-beta < 1.1.1", p("1.1.1-beta") < p("1.1.1"),
          "%r vs %r" % (p("1.1.1-beta"), p("1.1.1")))
    check("★ 1.1.1 < 1.1.2-alpha", p("1.1.1") < p("1.1.2-alpha"))
    check("★ beta.2 > beta", p("1.1.1-beta.2") > p("1.1.1-beta"))
    check("★ beta.10 > beta.9（按数字，不按字典序）",
          p("1.1.1-beta.10") > p("1.1.1-beta.9"))
    check("★ beta < 旧版本的下一个正式版（1.1.0 < 1.1.1-beta）",
          p("1.1.0") < p("1.1.1-beta"))
    check("★ 认不出 -> (0,)（不当成最新）", p("nope") == (0,), repr(p("nope")))
    check("★ 前缀 v 与 build 元数据都认",
          p("v1.1.1") == p("1.1.1") == p("1.1.1+build5"))


# 2. 渠道 → manifest URL

def test_two_channel_manifest_urls():
    from core.updater import (DEFAULT_MANIFEST_URLS, manifest_url_for,
                             normalise_channel)
    from core.updater import is_configured as U_is_configured
    stable = manifest_url_for("stable")
    beta = manifest_url_for("beta")
    check("★★ 两条渠道是两个**不同**的 URL", stable != beta, "%s / %s" % (stable, beta))
    check("★ 默认值就在 `DEFAULT_MANIFEST_URLS` 里（不是散在别处）",
          stable == DEFAULT_MANIFEST_URLS["stable"] and beta == DEFAULT_MANIFEST_URLS["beta"])
    cfg = {"update": {"manifest_urls": {"stable": "https://a/s.json",
                                        "beta": "https://a/b.json"}}}
    check("★ 配置覆盖默认值",
          manifest_url_for("stable", cfg) == "https://a/s.json"
          and manifest_url_for("beta", cfg) == "https://a/b.json")
    legacy = {"update": {"manifest_url": "https://old/v.json"}}
    check("★ 老的单值配置两条都用它（向后兼容）",
          manifest_url_for("stable", legacy) == "https://old/v.json"
          and manifest_url_for("beta", legacy) == "https://old/v.json")
    check("★ 认不出的渠道 -> stable",
          normalise_channel("nightly") == "stable"
          and normalise_channel(None) == "stable"
          and normalise_channel(" BETA ") == "beta")
    check("★ 认不出的渠道解析到 stable 那条",
          manifest_url_for("nightly") == stable)

    #: 随包的 `config.yaml` 里那两条也得是真地址 —— 那个文件是用户手里的
    #: 第一份配置；它要是还写着占位，新装机器的"每天一查"就是打一个不存在的地址。
    import yaml
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    live_stable = manifest_url_for("stable", cfg)
    live_beta = manifest_url_for("beta", cfg)
    check("★★ 随包 config 的两条都是**真地址**（不是占位）",
          U_is_configured(live_stable) and U_is_configured(live_beta),
          "%s / %s" % (live_stable, live_beta))
    check("★ 而且两条不是同一个（老的单值键没把它顶掉）",
          live_stable != live_beta, "%s / %s" % (live_stable, live_beta))


def test_the_placeholder_source_makes_no_request():
    """[!] 占位域名 = 还没配，不发请求。

    不发与"查过了、没新版本"必须分得开：后者是结论，前者是"没做"。
    """
    from core import updater as U
    check("★ 占位域名不算配置", U.is_configured("https://x.example/s.json") is False)
    check("★ 而默认那两条是**真地址**（2026-10-28 起）",
          U.is_configured(U.DEFAULT_MANIFEST_URLS["stable"])
          and U.is_configured(U.DEFAULT_MANIFEST_URLS["beta"]),
          repr(U.DEFAULT_MANIFEST_URLS))
    check("★ 域名里带 `.example` 一律算占位（宁可少查一次，也不去撞一个假域名）",
          U.is_configured("https://cdn.example-crannia.com/s.json") is False)
    check("★ 普通 https 算配置", U.is_configured("https://releases.crannia.app/s.json"))

    calls = {"n": 0}
    real = U.requests.get

    def _boom(*a, **kw):
        calls["n"] += 1
        raise AssertionError("占位域名不该发请求")
    U.requests.get = _boom
    saved = dict(U._update_state)
    try:
        got = U.UpdateChecker(manifest_url="https://x.example/s.json").check()
    finally:
        U.requests.get = real
        U._update_state.update(saved)
    check("★★ 占位域名 -> 一次请求都没发", calls["n"] == 0, str(calls["n"]))
    check("★ 而且返回 None（不是假装有新版本）", got is None)


# 2b. 渠道不许写反

def test_channel_follows_the_version():
    """预发布版本进 beta，正式版进 stable —— 判据只有 `channel_for_version` 一处。"""
    from core.updater import channel_for_version
    check("1.0.0-beta.1 -> beta", channel_for_version("1.0.0-beta.1") == "beta",
          channel_for_version("1.0.0-beta.1"))
    check("v1.2.3-rc1 -> beta", channel_for_version("v1.2.3-rc1") == "beta",
          channel_for_version("v1.2.3-rc1"))
    check("1.2.3 -> stable", channel_for_version("1.2.3") == "stable",
          channel_for_version("1.2.3"))
    check("1.2.3+build5 -> stable", channel_for_version("1.2.3+build5") == "stable",
          channel_for_version("1.2.3+build5"))
    check("认不出 -> stable", channel_for_version("nope") == "stable",
          channel_for_version("nope"))

    from core.version import __version__
    #: 2026-11-13 起对外发正式版（用户定的：从 v1.0.0 开始），
    #:   所以当前版本必须落在 stable 渠道。此前这里是 beta ——
    #:   那会儿代码线停在 `1.0.0-beta.1`，断言跟着版本走，别写死。
    check("★ 当前版本 %s 走 stable 渠道" % __version__,
          channel_for_version(__version__) == "stable", __version__)


def test_client_refuses_a_manifest_from_the_other_channel():
    """Stable 客户端读到一份 `channel=beta` 的清单 -> 拒绝，且说得出为什么。"""
    from core import updater as U

    class _Resp:
        def __init__(self, data):
            self._d = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._d

    def _run(manifest: dict) -> tuple:
        real_get = U.requests.get
        U.requests.get = lambda *a, **kw: _Resp(manifest)
        saved = dict(U._update_state)
        try:
            got = U.UpdateChecker(
                channel="stable",
                manifest_url="https://releases.crannia.app/stable/version.json").check()
            status = U._update_state.get("status")
            error = U._update_state.get("error") or ""
        finally:
            U.requests.get = real_get
            U._update_state.update(saved)
        return got, status, error

    base = {"version": "9.9.9-beta", "channel": "beta",
            "artifacts": {"windows-exe": {"url": "https://x/9.exe"}}}
    got, status, error = _run({"latest": base})
    check("★★ stable 客户端拒绝声明为 beta 的清单", got is None, repr(got))
    check("★ 状态是 error（不是「已是最新」）", status == "error", repr(status))
    check("★ 错误里点名两个渠道", "beta" in error and "stable" in error, error)

    got2, status2, _ = _run({"latest": {**base, "channel": ""}})
    check("★★ 没声明渠道的清单也拒绝（缺省不是 stable）", got2 is None, repr(got2))
    check("★ 而且同样报 error", status2 == "error", repr(status2))

    #: 未知渠道值不许被"配置回退用的宽松归一"收成 stable —— 那是另一个函数的事。
    for bad in ("stable1", "nightly", "beta2", "stable-v2"):
        got3, status3, _ = _run({"latest": {**base, "channel": bad}})
        check("★★ 未知渠道 %r 被拒绝（不归一成 stable）" % bad,
              got3 is None and status3 == "error",
              "%r/%r" % (got3, status3))


def test_release_scripts_refuse_stable_for_a_prerelease():
    """两个发布脚本都不许把预发布版本写进 stable 渠道。

    夹具把当前版本临时改成预发布：真实版本是正式版（2026-11-13 起），
    而这条钉的是"预发布那一刻不许进 stable"这个判据，不是当前版本号。
    """
    import core.version as CV
    import make_release as MR
    import release_gate as RG

    saved = CV.__version__
    CV.__version__ = "1.0.0-beta.1"
    try:
        check("★ make_release --channel stable 被拒（版本是预发布）",
              MR.main(["--channel", "stable"]) == 1)
        check("★ release_gate --channel stable 被拒（同一个判据）",
              RG.main(["--channel", "stable"]) == 1)
    finally:
        CV.__version__ = saved


# 3. 产物断言

def _fake_release(t: Path, *, py=True, platform="win32", pkgs=None,
                  app_files=("main.py", "config.yaml", "web/server.py",
                             "core/__init__.py"), asar=True):
    """照真产物的布局造夹具（apps/desktop/electron-builder.config.mjs）。

    这一格曾经把壳的 main.cjs / package.json 当成 resources/app 里的文件，而真产物里
    它们在 app.asar、解释器在 primary-runtime/python/ 下 —— 夹具和检查器共用同一份错
    清单，于是验产物对真包永久报缺文件，而测试一直绿。
    """
    rel = t / "release"
    unpacked = rel / "win-unpacked"
    app = unpacked / "resources" / "app"
    for rel_name in app_files:
        f = app / rel_name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}" if rel_name == "package.json" else "x", encoding="utf-8")
    rt = unpacked / "resources" / "primary-runtime"
    rt.mkdir(parents=True, exist_ok=True)
    if py:
        (rt / "python").mkdir(parents=True, exist_ok=True)
        (rt / "python" / "python.exe").write_bytes(b"MZ")
    (rt / "runtime.json").write_text(json.dumps({
        "platform": platform, "arch": "x64",
        "pythonPackages": pkgs if pkgs is not None else {"flask": "3.0"},
    }), encoding="utf-8")
    if asar:
        (unpacked / "resources" / "app.asar").write_bytes(
            b"x" * (300 * 1024))      # 过 asar 的最小体量门槛
    exe = rel / "crannia-1.0.0-win-x64.exe"
    exe.write_bytes(b"")
    with open(exe, "r+b") as fh:
        fh.truncate(21 * 1024 * 1024)          # 过最小体量门槛
    return unpacked, rel


def test_artifact_checks_catch_the_four_real_failures():
    import make_release as MR
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        unpacked, rel = _fake_release(t)
        check("★ 好产物 -> 零问题", MR.check_artifact(unpacked, rel) == [],
              repr(MR.check_artifact(unpacked, rel)))

        unpacked2, rel2 = _fake_release(t / "a", py=False)
        txt = " ".join(d for _w, d in MR.check_artifact(unpacked2, rel2))
        check("★★ 没有解释器 -> 抓得住", "python.exe" in txt, txt)

        unpacked3, rel3 = _fake_release(t / "b", platform="darwin")
        txt = " ".join(d for _w, d in MR.check_artifact(unpacked3, rel3))
        check("★★ 平台写错（那个 else darwin 的老 bug）-> 抓得住", "darwin" in txt, txt)

        unpacked4, rel4 = _fake_release(t / "c", pkgs={})
        txt = " ".join(d for _w, d in MR.check_artifact(unpacked4, rel4))
        check("★★ 依赖清单为空 -> 抓得住（审计原话）", "pythonPackages" in txt, txt)

        unpacked5, rel5 = _fake_release(t / "d", app_files=("main.py",))
        txt = " ".join(d for _w, d in MR.check_artifact(unpacked5, rel5))
        check("★ resources/app 缺文件 -> 抓得住", "web/server.py" in txt, txt)

        unpacked6, rel6 = _fake_release(t / "e", asar=False)
        txt = " ".join(d for _w, d in MR.check_artifact(unpacked6, rel6))
        check("★★ 壳没打进去（缺 app.asar）-> 抓得住", "app.asar" in txt, txt)


def test_manifest_round_trips_through_the_client_parser():
    import make_release as MR
    from core.updater import UpdateManifest
    man = MR.build_manifest(version="1.1.1-beta", channel="beta",
                            url="https://cdn.example/crannia.exe",
                            sha256="ab" * 32, size_bytes=123456)
    got = UpdateManifest.from_json(man)
    check("★★ 客户端解析得出来（回环）", got is not None)
    if got is not None:
        check("★ 版本 / 渠道逐字", got.version == "1.1.1-beta" and got.channel == "beta",
              "%r/%r" % (got.version, got.channel))
        check("★ 下载地址 / 哈希 / 体积逐字",
              got.download_url == "https://cdn.example/crannia.exe"
              and got.sha256 == "ab" * 32 and got.size_bytes == 123456)
    check("★ 渠道名只收两条",
          MR.build_manifest(version="1", channel="beta", url="u",
                            sha256="s", size_bytes=1)["latest"]["channel"] == "beta")


def test_published_at_is_really_utc():
    """`Z` 是 UTC 的声明，值也必须真是 UTC —— 本地时间加 `Z` 会差一个时区。"""
    import re
    from datetime import datetime, timezone
    import make_release as MR
    stamp = MR.build_manifest(version="1.1.1", channel="stable", url="u",
                              sha256="s", size_bytes=1)["latest"]["published_at"]
    check("★ 形状是 ISO 时间 + `Z`",
          bool(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp)), stamp)
    got = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    delta = abs((datetime.now(timezone.utc) - got).total_seconds())
    check("★★ 与真实 UTC 相差不超过 120 秒（差 8 小时 = 拿本地时间冒充 UTC）",
          delta <= 120, "%.0f 秒" % delta)


def test_published_at_can_be_given_explicitly():
    """重发清单时保留当初的发布时刻，而不是写成重发那一刻。"""
    import make_release as MR
    stamp = "2026-11-02T16:54:23Z"
    man = MR.build_manifest(version="1", channel="beta", url="u",
                            sha256="s", size_bytes=1, published_at=stamp)
    check("★ 显式给的时间原样写进清单", man["latest"]["published_at"] == stamp)
    rc = MR.main(["--published-at", "2026-11-02 16:54:23"])
    check("★ 形状不对 -> 拒绝（本地时间的习惯写法也算不对）", rc == 1, "rc=%r" % rc)


# 4. 安装方式

def test_installer_runs_nsis_silently():
    from core import updater as U
    script = U._build_powershell_script(1234, r"C:\x\crannia.exe")
    check("★★ 脚本里跑的是 NSIS 的 `/S`", 'ArgumentList "/S"' in script, "")
    check("★★ 而且**没有**自替换了（不再 Copy-Item 覆盖旧 exe）",
          "Copy-Item" not in script and "Move-Item" not in script, "")
    check("★ 会先等后端退出（装的时候那些文件是占着的）",
          "Get-Process -Id $OldPid" in script, "")
    check("★ 有超时，不无限等", "120" in script, "")

    saved = dict(U._update_state)
    try:
        U.UpdateInstaller.install(Path("nope.exe"))
        err = str(U._update_state.get("error") or "")
    finally:
        U._update_state.update(saved)
    check("★ 源码运行 -> 拒，并给路线（不替用户装）",
          "源码" in err, err)


# 5. 端到端：真发一版，客户端真收得到

def _a_higher_version(current: str) -> str:
    """造一个确定比 *current* 高的版本串（同渠道）。

    不写死 1.0.0-beta.99：那条会随版本号一起烂。真到了 beta.99 那天，
    这条用例就从「证明确有更新」变成「证明没有更新」，而它照样是绿的。
    """
    import re as _re
    m = _re.match(r"^v?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z][0-9A-Za-z.-]*))?$", current)
    if not m:
        return "9.9.9"
    nums, pre = m.group(1), m.group(2)
    if not pre:
        return nums + ".1"
    parts = pre.split(".")
    if parts[-1].isdigit():
        parts[-1] = str(int(parts[-1]) + 1)
    else:
        parts.append("1")
    return "%s-%s" % (nums, ".".join(parts))


def test_a_published_release_reaches_the_client():
    """端到端：本机起一个静态站，按发布脚本自己的形状发一版，
    客户端从 HTTP 读清单，判定有更新，下载，校验哈希，落盘。

    为什么单独立这一条：make_release.build_manifest 写得对、
    UpdateManifest.from_json 解析得对，两件事各自都有读者，
    但「发布出去的东西客户端收得到」发生在中间那一段（真的 HTTP、
    真的 sha256、真的落盘），而它此前一个读者都没有。
    只验到「已是最新」是不够的：那证的是读得到、比得对，
    推得出更新要另外抬一个版本号发一次。
    """
    import hashlib
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    from _sandbox import sandbox
    from core import updater as U
    from core.version import __version__
    import make_release as MR

    newer = _a_higher_version(__version__)
    saved = dict(U._update_state)
    try:
        with sandbox() as tmp:
            site = tmp / "site"
            (site / "beta").mkdir(parents=True)

            blob = b"MZ" + b"\x00" * 8192 + b"crannia-fake-installer"
            (site / "beta" / "crannia.exe").write_bytes(blob)
            digest = hashlib.sha256(blob).hexdigest()

            class _Quiet(SimpleHTTPRequestHandler):
                #: `directory` 必须在实例化时给（Python 3.7+ 的写法）
                def __init__(self, *a, **kw):
                    super().__init__(*a, directory=str(site), **kw)

                def log_message(self, *a):        # 别把访问日志灌进测试输出
                    pass

            srv = ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = "http://127.0.0.1:%d" % port
            try:
                man = MR.build_manifest(
                    version=newer, channel="beta",
                    url=base + "/beta/crannia.exe",
                    sha256=digest, size_bytes=len(blob),
                )
                #: 写下来的东西必须和发布脚本落盘时逐字同形
                (site / "beta" / "version.json").write_text(
                    json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")

                # 1) 客户端真的从 HTTP 读到了，而且判定有更新
                got = U.UpdateChecker(manifest_url=base + "/beta/version.json",
                                      channel="beta").check()
                check("★★ 发出去的清单，客户端读得到并判定「有更新」",
                      got is not None, repr(U._update_state.get("error")))
                if got is not None:
                    check("★ 版本 / 渠道逐字（没有被 URL 参数或解析改写）",
                          got.version == newer and got.channel == "beta",
                          "%r / %r" % (got.version, got.channel))
                    check("★ 下载地址与哈希逐字", got.download_url == base + "/beta/crannia.exe"
                          and got.sha256 == digest, got.download_url)

                # 2) 下载 + 校验 + 落盘
                path = U.UpdateChecker(manifest_url=base + "/beta/version.json",
                                       channel="beta").download(got) if got else None
                check("★★ 下载得下来且哈希对得上（真的走了 HTTP）",
                      path is not None and path.is_file()
                      and path.read_bytes() == blob,
                      repr(U._update_state.get("error")))
                check("★ 收口到 ready（不是下载完了但界面上永远转圈）",
                      U._update_state.get("status") == "ready",
                      repr(U._update_state.get("status")))
                check("★ 没有留下 `.part` 半截文件",
                      not list((tmp / "data" / "updates").glob("*.part")), "")

                # 2b) 同一个包再下一次：不许因为"文件已经在了"而失败
                #     （Windows 上 `os.rename` 覆盖已存在目标会抛 FileExistsError）
                again = U.UpdateChecker(manifest_url=base + "/beta/version.json",
                                        channel="beta")
                p2 = again.download(again.check())
                check("★★ 同一个版本重下一次也成功（装失败后点重试那条路）",
                      p2 is not None and Path(p2).is_file(),
                      repr(U._update_state.get("error")))

                # 3) 负对照 A：另一个版本，哈希写成别人的，应当拒收且不落盘。
                #    版本号必须不同：同版本会落到同一个文件名上，那样这里变绿
                #    可能只是「目标文件已存在」，而不是校验和挡住了。
                bad = json.loads(json.dumps(man))
                bad["latest"]["version"] = _a_higher_version(newer)
                bad["latest"]["artifacts"]["windows-exe"]["sha256"] = "0" * 64
                (site / "beta" / "bad.json").write_text(
                    json.dumps(bad, ensure_ascii=False), encoding="utf-8")
                mb = U.UpdateChecker(manifest_url=base + "/beta/bad.json",
                                     channel="beta").check()
                p_bad = U.UpdateChecker(manifest_url=base + "/beta/bad.json",
                                        channel="beta").download(mb) if mb else "no-manifest"
                check("★★ 哈希不对 -> 拒收（不是下完了就装）", p_bad is None, repr(p_bad))
                check("★ 而且收口到 error（不是静默什么都不发生）",
                      U._update_state.get("status") == "error",
                      repr(U._update_state.get("status")))
                check("★ 拒绝时把 `.part` 抹掉（不留半个 exe 在盘上）",
                      not list((tmp / "data" / "updates").glob("*.part")), "")

                # 4) 负对照 B：清单就是当前这一版 -> 不假装有更新
                same = json.loads(json.dumps(man))
                same["latest"]["version"] = __version__
                (site / "beta" / "same.json").write_text(
                    json.dumps(same, ensure_ascii=False), encoding="utf-8")
                ch = U.UpdateChecker(manifest_url=base + "/beta/same.json",
                                     channel="beta")
                check("★★ 同样是这一版 -> None（读得到 ≠ 有新版本）",
                      ch.check() is None, repr(U._update_state.get("status")))
            finally:
                srv.shutdown()
                srv.server_close()
    finally:
        U._update_state.clear()
        U._update_state.update(saved)


def main() -> int:
    print("=" * 62)
    print("release chain")
    print("=" * 62)
    for fn in (test_beta_sorts_before_the_release,
               test_two_channel_manifest_urls,
               test_the_placeholder_source_makes_no_request,
               test_channel_follows_the_version,
               test_client_refuses_a_manifest_from_the_other_channel,
               test_release_scripts_refuse_stable_for_a_prerelease,
               test_artifact_checks_catch_the_four_real_failures,
               test_manifest_round_trips_through_the_client_parser,
               test_published_at_is_really_utc,
               test_published_at_can_be_given_explicitly,
               test_installer_runs_nsis_silently,
               test_a_published_release_reaches_the_client):
        print("")
        print("-- %s --" % fn.__name__)
        try:
            fn()
        except Exception as exc:                              # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s 没抛异常（抛了 %s）" % (fn.__name__, type(exc).__name__),
                  False, str(exc))
    print("")
    if _FAILED:
        print("FAILED:")
        for x in _FAILED:
            print("  - %s" % x)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
