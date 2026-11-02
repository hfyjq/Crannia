#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""启动小窗的排版读者 —— 用真 Electron 量 DOM。

## 它防什么

`quotes.txt` 里可能出现一条很长的原文（例如 Циолковский 的俄文，原文 7 行
+ 译文 4 行）压在状态行上 ——「正在初始化…」那三个珠子叠在译文第三行的字上。

根因不是"某句话写长了"，是两条独立的规则：

* `.status` 钉在棕色地平线上；
* 右栏的高度不可控 —— `quotes.txt` 是用户自己加句子的地方。

短句时两条都对得上（所以不容易发现），长句时必撞。

## 为什么这条测试要真跑渲染器

重叠是像素的事：源码里 grep「状态行」grep 不出它、`test_splash_quotes.py`
管的是文件格式。本仓已有先例（那条测试拿 `node` 跑 `main.cjs` 里真的
`parseQuotes`）—— 这里同理，拿真的 Chromium 量 `getBoundingClientRect`。

它读的是 `quotes.txt` 的每一条（用 `main.cjs` 里那个真解析器取的，
不是这份文件里再抄一个），用户往 txt 里加一句长句，这条会当场红。

`node_modules/electron` 不在，明确跳过，不许静默当通过
（本仓：一条会自己跳过的护栏 = 没有护栏）。

输出一律 ASCII —— GBK 控制台上打中文会让整个运行崩，
而崩掉的输出看起来像测试失败。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ELECTRON = _ROOT / "node_modules" / "electron" / "cli.js"

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(desc: str, ok: bool, note: str = "") -> None:
    global _PASS, _FAIL
    print("  [%s] %s%s" % ("ok" if ok else "!!", desc, ("  -- " + note) if note and not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
        _FAILED.append(desc)


#: 量一遍的脚本。 `parseQuotes` 从 `main.cjs` 里取真身（同 `test_splash_quotes`）——
#: 这样"哪些句子会被显示"与生产同一个答案，这里不抄第二个解析器。
#:
#: NOTE: printed text below is ASCII only. The GBK scanner also flags these
#: `#:` lines, so keep them ASCII too -- Chinese belongs in the docstring
#: (which the scanner reports as harmless).
_PROBE_JS = r"""
const { app, BrowserWindow } = require("electron");
const fs = require("node:fs");
const path = require("node:path");

const src = fs.readFileSync("apps/desktop/main.cjs", "utf8");
const m = src.match(/function parseQuotes\(text\) \{[\s\S]*?\n\}/);
if (!m) { console.error("NO-PARSEQUOTES"); process.exit(2); }
const parseQuotes = new Function("return " + m[0])();
const quotes = parseQuotes(fs.readFileSync("apps/desktop/quotes.txt", "utf8"));

// NOTE: measurement MUST happen inside the page -- the main process has no
// `document`. A first version put `rect()` here and the whole probe HUNG
// (`document is not defined` thrown inside async => `app.quit()` never runs,
// and Python only ever saw a 120s timeout: symptom far from cause).
const MEASURE = `(function(){
  const r = (s) => { const e = document.querySelector(s);
                     const b = e.getBoundingClientRect();
                     return { top: Math.round(b.top), bottom: Math.round(b.bottom),
                              //: ★★ 这一对是**截断**的判据（见 Python 侧第 4 条）——
                              //: 少了它，读者只能看见"没叠"，看不见"字被切掉了"。
                              scroll: e.scrollHeight, client: e.clientHeight }; };
  return JSON.stringify({ status: r('.status'), quote: r('.quote') });
})()`;
const CLEAR = `(function(){
  document.querySelector('.dbg') && (document.querySelector('.dbg').style.display='none');
  document.querySelector('.quote').style.maxHeight = '';
  ['q-orig','q-zh','q-by'].forEach(function(i){ document.getElementById(i).textContent = ''; });
  return 'ok';
})()`;
const setQuoteJs = (q) => `(function(){
  var q = ${JSON.stringify(q)};
  var o = document.getElementById('q-orig');
  o.textContent = q.orig; o.classList.toggle('cjk', !/[A-Za-z]/.test(q.orig));
  var z = document.getElementById('q-zh');
  z.textContent = q.zh || ''; z.style.display = q.zh ? '' : 'none';
  // NOTE: U+2014 x2 written as escapes -- this JS lives inside a PYTHON string
  // literal, and the GBK scanner counts any non-ASCII there (see docstring).
  document.getElementById('q-by').textContent = '\u2014\u2014 ' + q.by;
  return 'ok';
})()`;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

//: ★★ 主题钉成浅色 —— 页面会跟随系统深色，而这里的断言量的是**具体颜色**；
//:   不钉的话，跑测试那台机器的系统主题会决定断言红不红（假红）。
const { nativeTheme } = require("electron");
nativeTheme.themeSource = "light";
app.disableHardwareAcceleration();
app.whenReady().then(async () => {
  try {
    const win = new BrowserWindow({
      width: 820, height: 500, show: false, frame: false,
      backgroundColor: "#e9ebe6", webPreferences: { offscreen: true },
    });
    await win.loadFile(path.resolve("apps/desktop/splash.html"));
    await wait(700);                     // 起手那句兜底 + 首帧

    // baseline: empty text => the status can ONLY land on the horizon
    await win.webContents.executeJavaScript(CLEAR);
    await wait(180);
    const groundTop = JSON.parse(await win.webContents.executeJavaScript(MEASURE)).status.top;

    // ② 逐条量
    const out = [];
    for (let i = 0; i < quotes.length; i++) {
      await win.webContents.executeJavaScript(setQuoteJs(quotes[i]));
      await wait(180);                   // 等 ResizeObserver 那一跳
      const got = JSON.parse(await win.webContents.executeJavaScript(MEASURE));
      out.push({ i, orig: quotes[i].orig.slice(0, 24),
                 status: got.status, quote: got.quote });
    }
    console.log("JSON " + JSON.stringify({ groundTop, n: quotes.length, out }));
  } catch (e) {
    console.error("PROBE-ERR " + (e && e.message));
  }
  app.quit();
});
"""


def _measure() -> dict:
    tmp = _ROOT / "tests" / "_layout_probe.cjs"
    try:
        tmp.write_text(_PROBE_JS, encoding="utf-8")
        _env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
        r = subprocess.run(["node", str(_ELECTRON), str(tmp)], cwd=str(_ROOT),
                           env=_env, capture_output=True, text=True,
                           encoding="utf-8", timeout=150)
    finally:
        tmp.unlink(missing_ok=True)
    line = next((l for l in (r.stdout or "").splitlines() if l.startswith("JSON ")), "")
    if not line:
        raise RuntimeError("no measurement (rc=%s): %s"
                           % (r.returncode, ((r.stderr or r.stdout or "")[-400:]).replace("\n", " | ")))
    return json.loads(line[5:])


def main() -> int:
    print("=" * 62)
    print("Splash layout: the status line must never overlap the quote ")
    print("=" * 62)

    print("\n-- 0. is electron available? --")
    if not _ELECTRON.exists():
        print("  [!] node_modules/electron NOT found -- this check DID NOT RUN (not a pass).")
        print("      Run `npm install` first. The skip is loud, but do not read it as green.")
        return 0
    print("  [ok] found %s" % _ELECTRON.relative_to(_ROOT))

    try:
        data = _measure()
    except Exception as exc:                                  # noqa: BLE001
        check("measurement ran (%s)" % type(exc).__name__, False, str(exc)[:300])
        return 1

    print("\n-- 1. baseline: with empty text the status sits on the horizon -- top=%s"
          % data["groundTop"])

    print("\n-- 2. no quote may overlap the status line -- %d quotes" % data["n"])
    bad = [r for r in data["out"] if r["status"]["top"] < r["quote"]["bottom"]]
    check("all %d quotes: status.top >= quote.bottom (zero overlap)" % data["n"],
          not bad,
          "%d overlapping -> %s" % (len(bad),
                                    [(r["orig"], r["status"]["top"], r["quote"]["bottom"])
                                     for r in bad][:3]))

    print("\n-- 3. the status line NEVER moves: every quote keeps it on the horizon --")
    #: 让文字整体上移，状态行钉死在棕色地平线上
    #: （判据是视觉归属：状态行与那三颗珠子跟左边那棵树是同一套材质，它是左半边的）。
    #:不变量是"每一条都对齐"。
    #: 这一条同时是第 2 条的负对照：没有它，"把文字压扁 / 把状态行推到别的
    #: 地方"也能过第 2 条 —— 而那两样都把视觉归属丢了（够不着才算证明）。
    off = [r for r in data["out"] if abs(r["status"]["top"] - data["groundTop"]) > 2]
    check("all %d quotes: the status line stays ON the horizon (it never moves)"
          % data["n"], not off,
          "%d moved -> %s" % (len(off), [(r["orig"], r["status"]["top"]) for r in off][:3]))
    print("      horizon top = %s; longest quote ends at y=%d"
          % (data["groundTop"], min(r["quote"]["bottom"] for r in data["out"])))

    print("\n-- 4. no quote may be TRUNCATED (moved, not clipped) --")
    #: 这一条是第 2 条的救命条件：
    #: 把"文字上移"停掉（内边距永远是基数）时，`max-height` 会把长句切掉，
    #: 而 `quote.bottom` 报的是切后的底，第 2、3 条照样全绿。
    #:少了这一条，读者只看得见"没叠"，看不见用户那段字被切了半行
    #: （全称断言会因为一个兜底而恒真）。
    cut = [r for r in data["out"] if r["quote"]["scroll"] > r["quote"]["client"] + 1]
    check("all %d quotes: nothing is hidden (scrollHeight <= clientHeight)"
          % data["n"], not cut,
          "%d truncated -> %s" % (len(cut),
                                  [(r["orig"], r["quote"]["scroll"], r["quote"]["client"])
                                   for r in cut][:3]))

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
