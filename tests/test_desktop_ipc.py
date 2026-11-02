# -*- coding: utf-8 -*-
"""壳的 IPC 属主校验 —— 两层读者。

## 它从哪来

`main.cjs` 里第一次出现 `ipcMain` 时补了一道最小闸
（`event.sender === splash.webContents`），但三要素只落了第一条。

补齐另外两条：它那个 `mainFrame` 对象 + 自定义协议 URL。
后者必须等页面搬离 `file://`：在那之前，那半边的判据没有东西可判。

## 判据

    ① 同一个 `webContents` 对象  ② 它那个 `mainFrame` 对象  ③ `crannia-app://<host>` URL

三要素是「同时」，而且比的是对象不是字符串 —— 只比 URL 字符串的实现
会被一张同名同值但不同对象的页面放行。本文件把它列成独立的两条用例
（对象换掉 / 帧换掉），因为它们的"坏"不一样。

## 两层

① 行为层（`_NODE_HARNESS`）：`apps/desktop/ipc-guard.cjs` 不 import electron
   （吃鸭子类型的 `event` / `win`），拿假对象在真 node 里跑一遍，看它答什么。
② 结构层：那四道闸跑不了（要 Electron），源级读者钉住
   「每道闸都问了」+「问在副作用之前」+「三要素只有一份（不是每道闸各抄一遍）」。

## 它怎么证明自己不是恒真

`test_the_judgements_can_go_red`：把 `ipc-guard.cjs` 逐条变异（不再比 sender 对象 /
不再比 mainFrame / 用 `includes` 替掉 host 相等 / 不再看协议），每条都必须让
某个具体用例变红；结构层另有坏样本负对照。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_desktop_ipc.py

[!] 输出一律 ASCII 安全字符（Windows 控制台默认 GBK）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))

from test_memory_ui import _strip_js_comments  # noqa: E402

_MAIN = _ROOT / "apps" / "desktop" / "main.cjs"
_GUARD = _ROOT / "apps" / "desktop" / "ipc-guard.cjs"

#: 与 `main.cjs` 顶上那个字面量一致。
_SCHEME = "crannia-app"
_SHELL_URL = "%s://shell/splash.html?shell=1" % _SCHEME
_APP_URL = "%s://app/" % _SCHEME


# 行为层

#: 夹具：只用假对象（鸭子类型），不发真 IPC。
#: 输出一行 JSON：`{"checks": [{"id","ok","detail"}], "fatal": "..."}`。
_NODE_HARNESS = r"""
"use strict";
const modPath = process.argv[2];
const SCHEME = process.argv[3];
const SHELL_URL = process.argv[4];
const APP_URL = process.argv[5];
const checks = [];
let fatal = "";
function check(id, ok, detail) { checks.push({ id: id, ok: !!ok, detail: String(detail === undefined ? "" : detail) }); }
async function safe(id, fn) {
  try { const r = await fn(); check(id, r[0], r[1]); }
  catch (e) { check(id, false, "threw: " + ((e && e.message) || e)); }
}

function fakeWindow(url) {
  const frame = { url: url };
  const wc = { mainFrame: frame };
  return { webContents: wc, isDestroyed: () => false };
}

async function main() {
  const g = require(modPath);
  const reject = g.senderRejection;

  // ── 正样本：真·小窗 ─────────────────────────────────────────────
  await safe("accept_splash", async () => {
    const win = fakeWindow(SHELL_URL);
    const ev = { sender: win.webContents, senderFrame: win.webContents.mainFrame };
    const why = reject(ev, win, ["shell"], SCHEME);
    return [why === null, String(why)];
  });

  // ── 正样本：主窗（host 名单 = app）─────────────────────────────
  await safe("accept_app", async () => {
    const win = fakeWindow(APP_URL);
    const ev = { sender: win.webContents, senderFrame: win.webContents.mainFrame };
    const why = reject(ev, win, ["app"], SCHEME);
    return [why === null, String(why)];
  });

  // ── ★★ 同名同值但**不同对象** ────────────────────────────────
  //    只比 URL 字符串的实现会**放行**。
  await safe("reject_other_webContents_same_url", async () => {
    const win = fakeWindow(SHELL_URL);
    const other = fakeWindow(SHELL_URL);            // URL 逐字一样，对象是另一个
    const ev = { sender: other.webContents, senderFrame: other.webContents.mainFrame };
    const why = reject(ev, win, ["shell"], SCHEME);
    return [why !== null, String(why)];
  });

  // ── ★★ 帧对象不对（URL 一样，但不是那个 mainFrame）───────────
  await safe("reject_foreign_frame_same_url", async () => {
    const win = fakeWindow(SHELL_URL);
    const ev = { sender: win.webContents, senderFrame: { url: SHELL_URL } };  // 另一个对象
    const why = reject(ev, win, ["shell"], SCHEME);
    return [why !== null, String(why)];
  });

  // ── 子 frame（webContents 对、URL 对，但不是主帧）─────────────
  await safe("reject_subframe", async () => {
    const win = fakeWindow(SHELL_URL);
    const ev = { sender: win.webContents, senderFrame: { url: SHELL_URL } };
    win.webContents.mainFrame = { url: SHELL_URL };   // 主帧是**另一个**对象
    const why = reject(ev, win, ["shell"], SCHEME);
    return [why !== null, String(why)];
  });

  // ── host 不在名单里（小窗的闸收到 app 的页面）─────────────────
  await safe("reject_wrong_host", async () => {
    const win = fakeWindow(APP_URL);
    const ev = { sender: win.webContents, senderFrame: win.webContents.mainFrame };
    const why = reject(ev, win, ["shell"], SCHEME);
    return [why !== null, String(why)];
  });

  // ── ★ 同形域名：`shell.evil` 与 `shell` **前缀一样** ─────────
  //    判据必须是 hostname **相等**，一句 `includes` / `startsWith` 就会放行。
  await safe("reject_lookalike_host", async () => {
    const win = fakeWindow(SCHEME + "://shell.evil/splash.html");
    const ev = { sender: win.webContents, senderFrame: win.webContents.mainFrame };
    const why = reject(ev, win, ["shell"], SCHEME);
    return [why !== null, String(why)];
  });

  // ── ★ 协议不对、而 host 恰好也叫 shell（http://shell/）───────
  await safe("reject_wrong_protocol", async () => {
    const win = fakeWindow("http://shell/splash.html");
    const ev = { sender: win.webContents, senderFrame: win.webContents.mainFrame };
    const why = reject(ev, win, ["shell"], SCHEME);
    return [why !== null, String(why)];
  });

  // ── `about:blank` / 空 URL：解析得了但没有 hostname ───────────
  await safe("reject_about_blank", async () => {
    const win = fakeWindow("about:blank");
    const ev = { sender: win.webContents, senderFrame: win.webContents.mainFrame };
    const why = reject(ev, win, ["shell"], SCHEME);
    return [why !== null, String(why)];
  });

  // ── `senderFrame` 为 null（服务端 / 已销毁的帧）───────────────
  await safe("reject_null_frame", async () => {
    const win = fakeWindow(SHELL_URL);
    const ev = { sender: win.webContents, senderFrame: null };
    const why = reject(ev, win, ["shell"], SCHEME);
    return [why !== null, String(why)];
  });

  // ── 窗口没了 ──────────────────────────────────────────────────
  await safe("reject_destroyed_window", async () => {
    const win = fakeWindow(SHELL_URL);
    win.isDestroyed = () => true;
    const ev = { sender: win.webContents, senderFrame: win.webContents.mainFrame };
    const why = reject(ev, win, ["shell"], SCHEME);
    return [why !== null, String(why)];
  });

  await safe("reject_no_window", async () => {
    const win = fakeWindow(SHELL_URL);
    const ev = { sender: win.webContents, senderFrame: win.webContents.mainFrame };
    const why = reject(ev, null, ["shell"], SCHEME);
    return [why !== null, String(why)];
  });
}

main().catch((e) => { fatal = String((e && e.stack) || e); }).then(() => {
  process.stdout.write(JSON.stringify({ checks: checks, fatal: fatal }));
});
"""


def _run_harness(module_path: Path):
    node = shutil.which("node")
    if not node:
        # [!] 必须 raise：正常返回会被印成 PASS
        #在没装 node 的机器上这就是"一条会自己跳过的护栏"。
        raise AssertionError(
            "没有 node —— 这条护栏无法运行，不算通过。"
            "（装 node，或从这里开始就别再靠肉眼看 IPC 那一层）")
    with tempfile.TemporaryDirectory() as tmp:      # 不落进工作区
        js = Path(tmp) / "ipc_harness.cjs"
        js.write_text(_NODE_HARNESS, encoding="utf-8")
        r = subprocess.run(
            [node, str(js), str(module_path), _SCHEME, _SHELL_URL, _APP_URL],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)
    out = (r.stdout or "").strip()
    try:
        payload = json.loads(out)
    except Exception:
        raise AssertionError(
            "夹具没有吐出 JSON（rc=%s）\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (r.returncode, out[-1200:], (r.stderr or "")[-1200:]))
    if payload.get("fatal"):
        raise AssertionError("夹具自己崩了：\n" + payload["fatal"][-1500:])
    return {c["id"]: (c["ok"], c["detail"]) for c in payload["checks"]}


#: 夹具真的在跑（不是"零条检查也算通过"）。
_MUST_BE_PRESENT = (
    "accept_splash", "accept_app", "reject_other_webContents_same_url",
    "reject_foreign_frame_same_url", "reject_subframe", "reject_wrong_host",
    "reject_lookalike_host", "reject_wrong_protocol", "reject_about_blank",
    "reject_null_frame", "reject_destroyed_window", "reject_no_window",
)


def test_the_guard_decides_correctly():
    print("\n-- 1. 真跑一遍：三要素（假对象 + 真 node）--")
    res = _run_harness(_GUARD)
    missing = [k for k in _MUST_BE_PRESENT if k not in res]
    assert not missing, "夹具少了这些检查：%r（夹具被改窄了？）" % (missing,)
    bad = ["%s -> %s" % (k, res[k][1]) for k, v in res.items() if not v[0]]
    assert not bad, "属主判据不对：\n   " + "\n   ".join(sorted(bad))
    print("  [ok]   %d 条（2 放行 + 10 拒，含「同名同值不同对象」两条）" % len(res))


# 结构层

#: 每道闸的副作用（拒绝必须发生在它之前）。
_SIDE_EFFECT = {
    "splash:quit": "app.quit()",
    "splash:retry": "app.relaunch()",
    "splash:boot": "console.log(",
    "splash:quotes": "readQuotes()",
}

_IPCCALL_RE = re.compile(r"ipcMain\.(on|handle)\(")


def _handler_chunks(src_raw: str):
    """切出 `[(kind, channel, body)]` —— 按 `ipcMain.on|handle(` 分段。

    不按大括号配平取函数体：`splash:boot` 那句模板字面量里有 `${tag}`，
    配平器会被字符串里的花括号骗到（区域抽取器抽错了，整批断言会恒真）。
    按下一个 `ipcMain.` 调用切分段，简单且够用。
    """
    src = _strip_js_comments(src_raw)        # 注释里的历史说明不算数
    hits = list(_IPCCALL_RE.finditer(src))
    out = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(src)
        seg = src[m.end():end]
        chan = re.match(r'\s*"([^"]+)"', seg)
        out.append((m.group(1), chan.group(1) if chan else "", seg))
    return out


def _structure_checks(raw: str):
    """返回 `[(label, ok, detail)]` —— 纯函数，负对照拿合成样本打进来。"""
    out = []
    chunks = _handler_chunks(raw)
    guards = [c for c in chunks if c[1] in _SIDE_EFFECT]

    out.append(("四道闸都还在（quit / retry / boot / quotes）",
                sorted(c[1] for c in guards) == sorted(_SIDE_EFFECT),
                repr(sorted(c[1] for c in guards))))

    for _kind, chan, body in guards:
        if chan not in _SIDE_EFFECT:
            continue
        g = body.find("rejectForeign(")
        s = body.find(_SIDE_EFFECT[chan])
        out.append(("`%s`：问了属主，且**问在副作用之前**" % chan,
                    g != -1 and s != -1 and g < s,
                    "guard@%d sideffect@%d" % (g, s)))

    # 三要素只有一份 —— 不许每道闸各抄一遍（那正是三要素只落一条的原因）。
    body_ = _strip_js_comments(raw)
    out.append(("三要素不在 `main.cjs` 里各抄一遍（只有一份，在 `ipc-guard.cjs`）",
                "senderFrame" not in body_ and "mainFrame" not in body_,
                "main.cjs 里出现了 senderFrame/mainFrame"))
    out.append(("`main.cjs` 真的 require 了 `./ipc-guard.cjs`",
                'require("./ipc-guard.cjs")' in raw, ""))
    out.append(("旧那道只比 webContents 的窄闸**已经拆掉**",
                "isTrustedSplashSender" not in raw, ""))
    return out


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


def test_the_wiring_is_structurally_present():
    print("\n-- 2. 接线：每道闸都问了 / 问在副作用之前 / 三要素只有一份 --")
    for label, ok, detail in _structure_checks(_MAIN.read_text(encoding="utf-8")):
        check(label, ok, detail)


def test_the_structure_guard_can_go_red():
    """负对照：合成坏样本，每一条都必须有它自己的红。"""
    print("\n-- 3. 判据本身：坏样本一条都躲不掉 --")
    good = _MAIN.read_text(encoding="utf-8")
    cases = [
        ("`splash:quit` 不问属主就退（副作用在前）",
         good.replace('if (rejectForeign(event, splash, ["shell"], "splash:quit")) return;',
                      'app.quit();', 1),
         "`splash:quit`：问了属主，且**问在副作用之前**"),
        ("`splash:quotes` 干脆不问",
         good.replace('if (rejectForeign(event, splash, ["shell"], "splash:quotes")) '
                      'return [];', '', 1),
         "`splash:quotes`：问了属主，且**问在副作用之前**"),
        ("`main.cjs` 里把三要素又抄了一遍",
         good.replace("function senderRejection(event, win, hosts) {",
                      "function senderRejection(event, win, hosts) {\n"
                      "  if (event.senderFrame !== event.sender.mainFrame) return 'x';\n", 1),
         "三要素不在 `main.cjs` 里各抄一遍（只有一份，在 `ipc-guard.cjs`）"),
        ("旧那道窄闸又回来了",
         good.replace("const guard = require(\"./ipc-guard.cjs\");",
                      "const guard = require(\"./ipc-guard.cjs\");\n"
                      "function isTrustedSplashSender(e) { return !!e.sender; }", 1),
         "旧那道只比 webContents 的窄闸**已经拆掉**"),
    ]
    for name, sample, must_red in cases:
        reds = [lab for lab, ok, _d in _structure_checks(sample) if not ok]
        check("坏样本被抓：%s" % name, must_red in reds, repr(reds[:4]))
    check("好样本（真源码）在结构层全绿",
          all(ok for _l, ok, _d in _structure_checks(good)))


# 变异

#: `(名字, 原文, 换成, 必须变红的检查 id)`。
_MUTATIONS = [
    ("不再比 sender 对象（只认「有没有 sender」）",
     "if (!event || event.sender !== win.webContents) return \"不是我们那个 webContents\";",
     "if (!event || !event.sender) return \"x\";",
     "reject_other_webContents_same_url"),
    ("不再比 mainFrame 对象（子 frame 也放行）",
     "if (frame !== event.sender.mainFrame) return \"不是那个 mainFrame\";",
     "",
     "reject_subframe"),
    ("host 用 `includes` 替掉相等（同形域名漏网）",
     "if (!hosts.includes(url.hostname)) return `host 不对（${url.hostname}）`;",
     "if (!hosts.some((h) => String(url.hostname).includes(h))) return `host 不对（${url.hostname}）`;",
     "reject_lookalike_host"),
    ("不再看协议（`http://shell/` 漏网）",
     "if (url.protocol !== `${scheme}:`) return `协议不对（${url.protocol}）`;",
     "",
     "reject_wrong_protocol"),
]


def test_the_judgements_can_go_red():
    """变异验证：四条判据，四具尸首。先看它红，再看它绿。"""
    print("\n-- 4. 变异：每条判据都得有它自己那一具尸首 --")
    src = _GUARD.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as mtmp:
        for name, old, new, must_red in _MUTATIONS:
            n = src.count(old)
            # 靶子自查（打偏的症状是"全绿"）。
            assert n == 1, "变异靶子打偏了：%r 出现 %d 次（要求恰好 1 次）" % (old[:60], n)
            mut = Path(mtmp) / "mutant.cjs"
            mut.write_text(src.replace(old, new, 1), encoding="utf-8")
            res = _run_harness(mut)
            ok, detail = res.get(must_red, (None, "这条检查压根没跑"))
            check("尸首：%s" % name, ok is False,
                  "预期 `%s` 变红，实得 ok=%r %s" % (must_red, ok, detail))
        check("原模块在同一批检查下全绿",
              all(v[0] for v in _run_harness(_GUARD).values()))


# ----------------------------------------------------------------------

def _main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    for _name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            check(_name, False, "AssertionError: %s" % (exc,))
        except Exception as exc:  # noqa: BLE001
            check(_name, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d/%d passed" % (_PASS, _PASS + _FAIL))
    if _FAILED:
        print("失败项：")
        for x in _FAILED:
            print("  - " + x)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(_main())
