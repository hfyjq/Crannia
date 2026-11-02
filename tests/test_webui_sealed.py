# -*- coding: utf-8 -*-
"""老 WebUI（六个 Jinja 页面）默认是关的，以及 `/ping` 不再吐令牌。

## 为什么

第三方审计第 2 条：`/ping` 把本地 API 的 Bearer Token 明文
回给任何本机进程）拍板：

> 「webui 只做内部快速验证某功能用，GUI 里彻底封死，甚至可以很激进的做一个
> 开关在代码里，如果为否，无论怎么样 webui 都叫不出来，除非有人改成真 ——
> 这样一来除了碰得到代码的其他人都碰不了」

两件事一起落：
  1. `web/server.py::WEBUI_PAGES_ENABLED = False`（代码里的开关）——
     六个页面统一 404；`/api/*` 与 `/static/*` 照旧（桌面 GUI 靠它们）。
  2. `/ping` 默认只回 `pong`；令牌改由桌面壳生成：
     环境变量 `CRANNIA_LAUNCH_TOKEN` → 后端；
     `additionalArguments` → `preload.cjs` → `window.crannia.token` → `api.js`。

## 钉住的五件事

  1. 六个页面默认 404（带不带令牌都一样 —— 对外它就是不存在）
  2. `/ping` 默认不含 token；只有显式 `CRANNIA_ALLOW_PING_TOKEN=1` 才回旧格式
  3. 后端认 `CRANNIA_LAUNCH_TOKEN`（壳注入的那个），不再自己铸
  4. 壳三条线都在：生成 → 环境变量 → `additionalArguments`
  5. 渲染进程第一优先用 `window.crannia.token`（不是先去 `/ping`）

用法：
    .venv/Scripts/python.exe tests/test_webui_sealed.py

ASCII-only 输出（Windows 控制台 GBK）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
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


def _boot():
    import web.server as S

    S.app.config["TESTING"] = True
    return S, S.app.test_client(), S._LAUNCH_TOKEN


def test_pages_are_404_by_default() -> None:
    print("\n-- 1. 六个页面默认 404（带令牌也一样）--")
    S, c, tok = _boot()
    check("★ 开关的默认值是 False", S.WEBUI_PAGES_ENABLED is False, "")
    pages = ["/", "/chat", "/models", "/logs", "/security", "/skills"]
    for p in pages:
        r = c.get(p + "?token=" + tok)
        check("%-10s -> 404（不是 200，也不是 403）" % p, r.status_code == 404,
              str(r.status_code))
        body = (r.get_data(as_text=True) or "")
        check("%-10s 回的是人话（不是模板）" % p,
              "只提供桌面应用" in body, body[:80])
    #: `/api/*` 与 `/static/*` 不受影响 —— 桌面 GUI 全靠它们
    r = c.get("/api/status?token=" + tok)
    check("★ `/api/status` 照旧 200（GUI 不被误伤）", r.status_code == 200,
          str(r.status_code))
    r = c.get("/static/img/logo.svg")
    check("★ `/static/*` 照旧可读（GUI 的音频/图标走这里）",
          r.status_code == 200, str(r.status_code))


def test_ping_no_longer_leaks_the_token() -> None:
    print("\n-- 2. `/ping` 默认不含 token；显式开关才回旧格式 --")
    S, c, tok = _boot()
    os.environ.pop("CRANNIA_ALLOW_PING_TOKEN", None)
    r = c.get("/ping")
    body = r.get_data(as_text=True) or ""
    check("默认 /ping -> 200", r.status_code == 200, str(r.status_code))
    check("★★ 而且**没有 token**（审计第 2 条）",
          "token" not in body.lower(), body)
    check("★ 仍然是一句 pong（壳用它判后端起没起）", body.strip() == "pong", body)

    os.environ["CRANNIA_ALLOW_PING_TOKEN"] = "1"
    try:
        body = c.get("/ping").get_data(as_text=True) or ""
        check("显式开关 -> 回旧格式（浏览器调试那条退路）",
              ("token=" + tok) in body, body)
    finally:
        os.environ.pop("CRANNIA_ALLOW_PING_TOKEN", None)


def test_shell_generates_and_injects() -> None:
    print("\n-- 3. 壳：生成 -> 环境变量 -> additionalArguments --")
    main = (_ROOT / "apps" / "desktop" / "main.cjs").read_text(encoding="utf-8")
    pre = (_ROOT / "apps" / "desktop" / "preload.cjs").read_text(encoding="utf-8")
    api = (_ROOT / "packages" / "ui" / "src" / "api.js").read_text(encoding="utf-8")
    srv = (_ROOT / "web" / "server.py").read_text(encoding="utf-8")

    check("★ 壳自己生成令牌（`randomBytes`）",
          "crypto.randomBytes" in main and "let _token = crypto.randomBytes" in main, "")
    check("★ 经环境变量给后端（`CRANNIA_LAUNCH_TOKEN`）",
          "CRANNIA_LAUNCH_TOKEN: _token" in main, "")
    check("★ 经 `additionalArguments` 给渲染进程",
          "additionalArguments" in main and "--crannia-token=" in main, "")
    check("★ 壳**不再**从 /ping 解析令牌",
          'body.match(/token=' not in main, "")
    check("★ preload 把它暴露成 `window.crannia.token`",
          "token: LAUNCH_TOKEN" in pre and "process.argv" in pre, "")
    check("★ 渲染进程**第一优先**用注入的那个（再去 /ping）",
          "window.crannia.token" in api
          and api.index("window.crannia.token") < api.index("/ping"), "")
    check("★ 后端认环境变量（没有才自己铸）",
          'os.environ.get("CRANNIA_LAUNCH_TOKEN")' in srv, "")


# ------------------------------------------------------------------

def _main() -> int:
    for fn in (test_pages_are_404_by_default,
               test_ping_no_longer_leaks_the_token,
               test_shell_generates_and_injects):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check("%s 没抛异常" % fn.__name__, False,
                  "%s: %s" % (type(exc).__name__, exc))
    print("\n" + "=" * 60)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - " + f)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(_main())
