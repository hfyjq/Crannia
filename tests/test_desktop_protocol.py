# -*- coding: utf-8 -*-
"""壳的协议层 —— 两层读者，缺一不可。

## 它从哪来

桌面壳从 `file://` 换到壳自己的协议 `crannia-app://`：页面与它的
`fetch` / `EventSource` 由此进同一个 origin（`file://` 的 origin 是 `null`，
而后端没有、也不该有 CORS，那条路上一次都打不出去）。

## 为什么是两层

① 行为层（`_NODE_HARNESS`）—— 拿真的 node 把 `apps/desktop/web-document.cjs`
   跑起来，去观察它的输出：状态码 / 收到的头 / 上游被没被碰过 / 一个字到得多快。
   判据不是"源码里写着 `403`"，是"发一条坏 origin 的请求，看它回什么"。
   （那 129 个文件里一条源码 grep 都没有，全打在运行时可观察的输出上。）
② 结构层 —— `protocol.handle` 那半截跑不了（要 Electron 才能跑）。
   它退一步用源级读者盯住那几条"少一行就坏、而症状在很远的地方"的东西：
   协议注册的时间窗 · 两个 hostname · 503 而不是 404 · 两个窗口都真的走这个协议。

## 它怎么证明自己不是恒真

`test_the_judgements_can_go_red`：把 `web-document.cjs` 逐条变异（收紧包含性
判据 / 关掉 origin 判据 / 不再剥 `set-cookie` / 丢掉 `duplex` / 改成整块缓冲），
每一条都必须让某个具体的检查变红。
变异前先自查 `count(old) == 1` —— 靶子打偏时，症状是"全绿"。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_desktop_protocol.py

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
_MODULE = _ROOT / "apps" / "desktop" / "web-document.cjs"

#: 协议名 —— 与 `main.cjs` 里那个字面量必须一样（它没有别处推导它）。
_SCHEME = "crannia-app"


# 行为层：真跑一遍

#: 交给 node 的夹具。只吃 argv，不写任何文件、不碰任何生产目录。
#: 输出一行 JSON：`{"checks": [{"id","ok","detail"}], "fatal": "..."}`。
_NODE_HARNESS = r"""
"use strict";
const http = require("node:http");
const path = require("node:path");

const ORIGIN = "crannia-app://app";
const modPath = process.argv[2];
const root = process.argv[3];
const evilRoot = process.argv[4];
const checks = [];
let fatal = "";
function check(id, ok, detail) {
  checks.push({ id, ok: !!ok, detail: detail === undefined ? "" : String(detail).slice(0, 200) });
}
async function safe(id, fn) {
  try { const r = await fn(); check(id, r[0], r[1]); }
  catch (e) { check(id, false, "threw: " + ((e && e.message) || e)); }
}

async function main() {
  const m = require(modPath);

  // ── ① 静态投递 ─────────────────────────────────────────────────
  await safe("serve_root_200", async () => {
    const r = await m.serveWebDocument(new Request("crannia-app://app/"), root);
    const body = r.status === 200 ? await r.text() : "";
    check("serve_root_mime", (r.headers.get("content-type") || "").includes("text/html"),
          r.headers.get("content-type"));
    check("serve_root_body", body.includes("ROOT-MARKER"), body.slice(0, 40));
    return [r.status === 200, r.status];
  });
  await safe("serve_index_200", async () => {
    const r = await m.serveWebDocument(new Request("crannia-app://app/index.html"), root);
    return [r.status === 200, r.status];
  });
  await safe("serve_missing_404", async () => {
    const r = await m.serveWebDocument(new Request("crannia-app://app/nope.js"), root);
    return [r.status === 404, r.status];
  });
  await safe("serve_post_405", async () => {
    const r = await m.serveWebDocument(new Request("crannia-app://app/", { method: "POST" }), root);
    return [r.status === 405, r.status];
  });
  // ★ 越界：`%2f` 是**不会**被 URL 规范化掉的斜杠（`%2e%2e` 会被规范化成 `.`，
  //   所以那种探针打不到包含性判据 —— 见 docstring 里"靶子打偏"那段）。
  //   目标文件**真的存在**（在 root 的兄弟目录里）-> 答 403 才说明拦的是"越界"，
  //   而不是"没这个文件"。
  await safe("serve_escape_403", async () => {
    const evil = path.basename(evilRoot);
    const r = await m.serveWebDocument(
      new Request(`crannia-app://app/..%2f${evil}%2fsecret.txt`.replace("#", "%23")), root);
    return [r.status === 403, r.status];
  });
  await safe("serve_bad_percent_400", async () => {
    const r = await m.serveWebDocument(new Request("crannia-app://app/%E4%B4"), root);
    return [r.status === 400, r.status];
  });

  // ── ② 转发（真起一个后端替身）─────────────────────────────────
  const hits = [];
  const server = http.createServer((req, res) => {
    hits.push({ url: req.url, method: req.method, headers: req.headers });
    if (req.url.startsWith("/stream")) {
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.write("data: 1" + String.fromCharCode(10) + String.fromCharCode(10));
      setTimeout(() => {
        res.write("data: 2" + String.fromCharCode(10) + String.fromCharCode(10));
        res.end();
      }, 300);
      return;
    }
    let body = "";
    req.on("data", (c) => { body += c; });
    req.on("end", () => {
      const payload = JSON.stringify({ ok: true, got: body });
      res.writeHead(200, {
        "content-type": "application/json",
        "content-length": String(Buffer.byteLength(payload)),
        "set-cookie": "host_secret=1",
        "x-keep": "yes",
      });
      res.end(payload);
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const base = "http://127.0.0.1:" + server.address().port;

  await safe("fwd_200", async () => {
    const r = await m.forwardWebRequest(new Request(ORIGIN + "/api/x?q=1", {
      headers: {
        cookie: "page=1", "sec-fetch-site": "none", host: "evil.example",
        origin: ORIGIN, "X-Crannia-Token": "tok",
      },
    }), base, ORIGIN);
    const text = await r.text();
    check("fwd_body", JSON.parse(text).ok === true, text.slice(0, 60));
    check("fwd_strips_setcookie", r.headers.get("set-cookie") === null, r.headers.get("set-cookie"));
    check("fwd_strips_length", r.headers.get("content-length") === null, r.headers.get("content-length"));
    check("fwd_keeps_xkeep", r.headers.get("x-keep") === "yes", r.headers.get("x-keep"));
    check("fwd_keeps_ctype", (r.headers.get("content-type") || "").includes("application/json"),
          r.headers.get("content-type"));
    const up = hits[0].headers;
    check("fwd_upstream_url", hits[0].url === "/api/x?q=1", hits[0].url);
    check("fwd_strips_host", up.host !== "evil.example", up.host);
    check("fwd_strips_origin", up.origin === undefined, up.origin);
    check("fwd_strips_cookie", up.cookie === undefined, up.cookie);
    check("fwd_strips_sfs", up["sec-fetch-site"] === undefined, up["sec-fetch-site"]);
    check("fwd_keeps_token", up["x-crannia-token"] === "tok", up["x-crannia-token"]);
    return [r.status === 200, r.status];
  });

  // ★★ 两边一起看：**403** 与 **上游一次都没被碰过**。
  //   只看状态码的话，"先转发、转发完再改成 403"也能过 —— 那是另一回事。
  await safe("fwd_bad_origin_403", async () => {
    const before = hits.length;
    const r = await m.forwardWebRequest(new Request(ORIGIN + "/api/x", {
      headers: { origin: "https://evil.example" },
    }), base, ORIGIN);
    check("fwd_bad_origin_no_upstream", hits.length === before, hits.length - before);
    return [r.status === 403, r.status];
  });

  // ★ 负对照的另一半：**不带 `Origin` 的 GET 必须放行**。
  //   把"没报家门"也拒掉的话，整个首页与事件流会一起 403 —— 而日志里只有一片 403。
  await safe("fwd_null_origin_ok", async () => {
    const r = await m.forwardWebRequest(new Request(ORIGIN + "/api/x"), base, ORIGIN);
    await r.text();
    return [r.status === 200, r.status];
  });

  await safe("fwd_post_body", async () => {
    const r = await m.forwardWebRequest(new Request(ORIGIN + "/api/chat", {
      method: "POST", body: JSON.stringify({ a: 1 }),
      headers: { "content-type": "application/json" },
    }), base, ORIGIN);
    const text = await r.text();
    check("fwd_post_echo", JSON.parse(text).got === '{"a":1}', text.slice(0, 60));
    return [r.status === 200, r.status];
  });

  // ★★：**注入的 fetchImpl 真的被用到** —— 不是摆着好看。
  //    默认那几条（上面）走的是全局 `fetch`（Node 的 undici，`headersTimeout`
  //    写死 300 秒）；`main.cjs` 注入的是 Electron `net.fetch`。
  //    这条用**替身**证明第四个参数接得上，且**没有**偷偷去打上游。
  await safe("fwd_injected_fetch", async () => {
    const before = hits.length;
    let calledWith = "";
    const stub = async (url, init) => {
      calledWith = String(url);
      return new Response("STUB-BODY", {
        status: 201, headers: { "content-type": "text/plain", "set-cookie": "x=1" },
      });
    };
    const r = await m.forwardWebRequest(
      new Request(ORIGIN + "/api/x?q=2"), base, ORIGIN, stub);
    const body = await r.text();
    check("fwd_injected_used", calledWith.endsWith("/api/x?q=2"), calledWith);
    check("fwd_injected_body", body === "STUB-BODY", body.slice(0, 40));
    check("fwd_injected_status", r.status === 201, r.status);
    check("fwd_injected_no_upstream", hits.length === before, hits.length - before);
    return [body === "STUB-BODY", body.slice(0, 20)];
  });

  // ★★ 流式：**第一块必须在"上游还没结束"时就到手**。
  //   测的是时间差，不是"有没有 body" —— 整块缓冲之后 `r.body` 照样存在，
  //   只有时延能分开这两者（`/api/events` 据此要么实时、要么一片空白）。
  await safe("fwd_stream_first", async () => {
    const t0 = Date.now();
    const r = await m.forwardWebRequest(new Request(ORIGIN + "/stream"), base, ORIGIN);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    const first = await reader.read();
    const firstAt = Date.now() - t0;
    const firstText = dec.decode(first.value);
    let rest = "";
    for (;;) { const c = await reader.read(); if (c.done) break; rest += dec.decode(c.value); }
    const total = Date.now() - t0;
    check("fwd_stream_rest", rest.includes("data: 2"), rest.trim());
    check("fwd_stream_not_buffered", firstAt < 150,
          "first=" + firstAt + "ms total=" + total + "ms");
    return [firstText.includes("data: 1"), firstText.trim()];
  });

  // ── ③ 两张清单的规模（钉住"抄了几条"）───────────────────────
  check("withheld_11", m.WITHHELD_RESPONSE_HEADERS.length === 11,
        m.WITHHELD_RESPONSE_HEADERS.length);
  check("stripped_req_4", m.STRIPPED_REQUEST_HEADERS.length === 4,
        m.STRIPPED_REQUEST_HEADERS.length);

  server.close();
}

main().catch((e) => { fatal = String((e && e.stack) || e); }).then(() => {
  process.stdout.write(JSON.stringify({ checks: checks, fatal: fatal }));
});
"""


def _run_harness(module_path: Path, root: Path, evil_root: Path):
    """跑一遍夹具，返回 `{id: (ok, detail)}` + fatal。"""
    node = shutil.which("node")
    if not node:
        # [!] 必须 raise，不能 return：
        #     正常返回会被 `_main` 印成 PASS，在没装 node 的机器上，
        #     这一层就是"一条会自己跳过的护栏"。
        raise AssertionError(
            "没有 node —— 这条护栏无法运行，不算通过。"
            "（装 node，或从这里开始就别再靠肉眼看转发那一层）")
    with tempfile.TemporaryDirectory() as tmp:          # 不落进工作区
        js = Path(tmp) / "harness.cjs"
        js.write_text(_NODE_HARNESS, encoding="utf-8")
        # 被 require 的那个模块从外面传进去，变异版能被换进来跑同一套夹具。
        #    它若与夹具同目录，node 会按 cjs 解析没问题，但我们就换不动它了。
        r = subprocess.run(
            [node, str(js), str(module_path), str(root), str(evil_root)],
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


def _fixture():
    """造一组真目录：一个被服务的 root + 一个前缀相同的兄弟目录。

    兄弟目录是故意的：`root-evil` 与 `root` 的字符串前缀相同，
    判据必须是"`root` + 路径分隔符"而不是光比 `root`（「同名同值但不同对象」
    是同一个形状）。
    """
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    root = base / "root"
    root.mkdir()
    (root / "index.html").write_text(
        "<!doctype html><html><body><div id=\"root\">ROOT-MARKER</div></body></html>",
        encoding="utf-8")
    evil = base / "root-evil"
    evil.mkdir()
    (evil / "secret.txt").write_text("SECRET-MARKER", encoding="utf-8")
    return tmp, root, evil


#: 夹具真的在跑（不是"零条检查也算通过"）。
_MUST_BE_PRESENT = (
    "serve_root_200", "serve_escape_403", "fwd_200", "fwd_bad_origin_403",
    "fwd_bad_origin_no_upstream", "fwd_null_origin_ok", "fwd_post_body",
    "fwd_injected_fetch", "fwd_stream_not_buffered", "withheld_11", "stripped_req_4",
)


def test_the_forwarder_behaves():
    print("\n-- 1. 真跑一遍：静态投递 + 转发（node 驱动真模块）--")
    tmp, root, evil = _fixture()
    try:
        res = _run_harness(_MODULE, root, evil)
    finally:
        tmp.cleanup()
    missing = [k for k in _MUST_BE_PRESENT if k not in res]
    assert not missing, "夹具少了这些检查：%r（夹具被改窄了？）" % (missing,)
    bad = ["%s -> %s" % (k, res[k][1]) for k, v in res.items() if not v[0]]
    assert not bad, "转发/投递的行为不对：\n   " + "\n   ".join(sorted(bad))
    print("  [ok]   %d 条行为检查全过（含 403 时上游一次都没被碰）" % len(res))


# 结构层：`protocol.handle` 那半截跑不了，退回源级读者

#: 源码里 `app.whenReady` 那个位置 —— 用来判"协议注册在它之前"。
_READY_RE = re.compile(r"app\.whenReady\(\s*\)")


def _main_body() -> str:
    return _strip_js_comments(_MAIN.read_text(encoding="utf-8"))


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


def _structure_checks(body: str, raw: str) -> list:
    """返回 `[(label, ok, detail)]` —— 纯函数，好让负对照拿合成样本打进来。

    两个入参不是省事，是必须的：`body` 是剥过注释的（位置、hostname、
    `loadFile` 这些判据必须落在真代码上 —— 注释里写着历史不算数），
    而 `_strip_js_comments` 那条 `//[^\n]*` 会把模板字面量里的 `://` 一起吃掉
URL 那几条只能在 `raw` 上查。
    剥注释的规则没考虑 `http://` 这种东西，而症状是三条 URL 判据恒假
    （看着像"源码写错了"）。
    """
    out = []
    ready = _READY_RE.search(body)

    reg = body.find("protocol.registerSchemesAsPrivileged")
    out.append(("协议注册在 `app.whenReady` 之前",
                reg != -1 and ready is not None and reg < ready.start(),
                "reg=%d ready=%s" % (reg, ready.start() if ready else None)))

    priv_block = body[reg:reg + 700] if reg != -1 else ""
    for priv in ("standard", "secure", "supportFetchAPI", "corsEnabled", "stream", "codeCache"):
        out.append(("权限表里有 `%s`" % priv, priv + ":" in priv_block or priv + " :" in priv_block,
                    priv_block[:0]))

    h = body.find("protocol.handle")
    out.append(("`protocol.handle` 在 whenReady 里、且在 openSplash 之前",
                h != -1 and ready is not None and h > ready.start()
                and 0 <= h < body.find("openSplash()"),
                "handle=%d openSplash=%d" % (h, body.find("openSplash()"))))

    out.append(("两个 hostname 都有分支（shell / app）",
                'url.hostname === "shell"' in body and 'url.hostname === "app"' in body,
                body.count('url.hostname ===')))

    out.append(("后端没起来给 503，且**没有**拿 404 顶替",
                "status: 503" in body and "status: 404" in body
                and body.find("status: 503") < body.find("status: 404"),
                "503@%d 404@%d" % (body.find("status: 503"), body.find("status: 404"))))
    # 上面那条只证"两个码都在、503 在前面"。真正的判据是"未就绪那一支答 503"。
    out.append(("未就绪那一支紧挨着 `backendReady`",
                re.search(r"if\s*\(\s*!\s*backendReady\s*\)\s*return new Response\(null,\s*\{\s*status:\s*503",
                          body) is not None, ""))

    # 协议名只有一处字面量（`const SCHEME = "..."`），两个窗口都从它拼出来 ——
    #   所以这里钉的是"名字对不对"+"两个窗口各拼了哪一段"，而不是三条平行字面量。
    out.append(("协议名就是 `%s`" % _SCHEME,
                re.search(r'const\s+SCHEME\s*=\s*"%s"' % re.escape(_SCHEME), body) is not None, ""))
    out.append(("主窗 `loadURL` 走 `${SCHEME}://app/`",
                "loadURL(`${SCHEME}://app/`)" in raw, ""))
    # 新增 `lang=` 参数，非中文界面不显示引文的中文译文。
    #:   它走查询串而不是 IPC —— 首屏就要用，IPC 是异步的。
    out.append(("小窗 `loadURL` 走 `${SCHEME}://shell/`（并带上界面语言）",
                "loadURL(`${SCHEME}://shell/splash.html?shell=1&lang=${encodeURIComponent(lang)}`)"
                in raw, ""))
    # `loadFile` 整类退场 —— 它就是那个"页面在 `file://` 上"的根。
    #    判据落在去注释之后的源码上：注释里写着历史不算数。
    out.append(("两个窗口都不再 `loadFile`（`file://` 整类退场）",
                body.count(".loadFile(") == 0, body.count(".loadFile(")))
    out.append(("不再给页面传 `?api=`（同源了，baseUrl 该是空串）",
                "query: { api:" not in body and "query: {api:" not in body, ""))
    out.append(("`main.cjs` 真的 require 了那个纯 Node 模块",
                'require("./web-document.cjs")' in body, ""))
    # 转发必须注入 `net.fetch`。
    #    全局 `fetch` 是 Node 的 undici，`headersTimeout` 写死 300 秒 ——
    #    而 `/api/chat` / `/api/approval` 是同步阻塞的，长任务每一轮都会被半路掐断
    #    （症状：绿横幅消失、`busy` 打回 false，而 SSE 照常推）。
    #    这条只证"接线在"；"注入真的被用到"由行为层的 `fwd_injected_fetch` 证。
    out.append(("转发注入的是 Electron `net.fetch`（不是全局 fetch ——）",
                re.search(r"forwardWebRequest\(\s*request,\s*API_BASE,[^;]*?net\.fetch\(input, init\)",
                          body, re.S) is not None, ""))
    out.append(("`net` 从 electron 里 require 进来",
                re.search(r'const\s*\{[^}]*\bnet\b[^}]*\}\s*=\s*require\("electron"\)',
                          body) is not None, ""))
    out.append(("`backendReady` 在 `/ping` 成功那一支置位",
                re.search(r"backendReady = true", body) is not None, ""))
    # 令牌改由壳自己生成（不再从 `/ping` 解析 ——
    #:   审计第 2 条）。这条从"解析那一步"改钉"生成那一步"。
    out.append(("★ 启动令牌由壳自己生成（`crypto.randomBytes`）",
                "crypto.randomBytes" in body, ""))
    return out


def test_the_wiring_is_structurally_present():
    print("\n-- 2. 接线：协议注册的时间窗 / 两个 hostname / 503 / 两个窗口 --")
    raw = _MAIN.read_text(encoding="utf-8")
    for label, ok, detail in _structure_checks(_strip_js_comments(raw), raw):
        check(label, ok, detail)


def test_the_structure_guard_can_go_red():
    """负对照：拿合成坏样本打同一批判据 —— 每一条都必须有它的红。"""
    print("\n-- 3. 判据本身：坏样本一条都躲不掉 --")
    good_raw = _MAIN.read_text(encoding="utf-8")

    def _mk(text: str):
        return _structure_checks(_strip_js_comments(text), text)

    cases = [
        # 造这个坏样本不能靠"给注册那行加个前缀" —— `find` 找的是那个子串，
        #    加前缀位置一个字都不动，于是它照样绿（靶子打偏的又一具标本）。
        #    真正把时间窗挪坏的做法是：在它前面塞一个 `app.whenReady()`。
        ("协议注册挪到 whenReady **之后**",
         "app.whenReady();\n" + good_raw,
         "协议注册在 `app.whenReady` 之前"),
        ("少一个 `standard` 权限",
         good_raw.replace("    standard: true,", "", 1),
         "权限表里有 `standard`"),
        ("`protocol.handle` 挪到 `openSplash()` 之后",
         good_raw.replace("protocol.handle(SCHEME, handleProtocolRequest);", "void 0;", 1)
             + "\nopenSplash(); protocol.handle(SCHEME, handleProtocolRequest);\n",
         "`protocol.handle` 在 whenReady 里、且在 openSplash 之前"),
        ("只有 `app` 一个 hostname（`shell` 没了）",
         good_raw.replace('url.hostname === "shell"', "false", 1),
         "两个 hostname 都有分支（shell / app）"),
        ("未就绪那一支改答 404",
         good_raw.replace("if (!backendReady) return new Response(null, { status: 503 });",
                          "if (!backendReady) return new Response(null, { status: 404 });", 1),
         "未就绪那一支紧挨着 `backendReady`"),
        ("主窗改回 `loadFile`",
         good_raw.replace("else mainWindow.loadURL(`${SCHEME}://app/`);",
                          'else mainWindow.loadFile(path.join(__dirname, "dist/index.html"));', 1),
         "主窗 `loadURL` 走 `${SCHEME}://app/`"),
        ("小窗改回 `loadFile`（只剩查询串那半截）",
         good_raw.replace(
             "splash.loadURL(`${SCHEME}://shell/splash.html?shell=1&lang=${encodeURIComponent(lang)}`);",
             'splash.loadFile(path.join(__dirname, "splash.html"), '
             '{ query: { shell: "1" } });', 1),
         "小窗 `loadURL` 走 `${SCHEME}://shell/`（并带上界面语言）"),
        # 注入那一格被填成 `undefined`，悄悄退回全局 fetch（300 秒上限回来）。
        ("转发不再注入 `net.fetch`（退回全局 fetch）",
         good_raw.replace("(input, init) => net.fetch(input, init)", "undefined", 1),
         "转发注入的是 Electron `net.fetch`（不是全局 fetch ——）"),
    ]
    for name, sample, must_red in cases:
        reds = [lab for lab, ok, _d in _mk(sample) if not ok]
        check("坏样本被抓：%s" % name, must_red in reds, repr(reds[:4]))
    # 好样本不许被误伤（否则上面那批"红"可能只是判据太吵）。
    check("好样本（真源码）在结构层全绿",
          all(ok for _l, ok, _d in _mk(good_raw)))


# 变异：每条判据都得有一具尸首

#: `(名字, 原文, 换成, 必须变成红的检查 id)`。
#: 原文必须逐字出现且只出现一次 —— 见下面 `count != 1` 那条断言。
_MUTATIONS = [
    ("包含性判据去掉 `+ sep`（`root-evil` 会落进 `root`）",
     "!target.startsWith(directory + sep)", "!target.startsWith(directory)",
     "serve_escape_403"),
    ("进向 origin 判据整个失效",
     "if (origin !== null && origin !== expectedOrigin) return new Response(null, { status: 403 });",
     "if (false) return new Response(null, { status: 403 });",
     "fwd_bad_origin_403"),
    ("`set-cookie` 不再剥",
     '"set-cookie",', "", "fwd_strips_setcookie"),
    ("不再声明 `duplex`（带 body 的转发会抛）",
     '    init.duplex = "half";', "", "fwd_post_body"),
    ("转发改成整块缓冲（SSE 会变成一片空白）",
     "return new Response(response.body, { status: response.status, headers: outgoing });",
     "return new Response(await response.arrayBuffer(), "
     "{ status: response.status, headers: outgoing });",
     "fwd_stream_not_buffered"),
    # 注入那一格被绕过，又回到 Node fetch 的 300 秒 headers 上限。
    ("转发不用注入的实现（回到全局 fetch）",
     "const response = await fetchImpl(target, init);",
     "const response = await fetch(target, init);",
     "fwd_injected_fetch"),
]


def test_the_judgements_can_go_red():
    """变异验证：五条判据，五具尸首。先看它红，再看它绿。"""
    print("\n-- 4. 变异：每条判据都得有它自己那一具尸首 --")
    src = _MODULE.read_text(encoding="utf-8")
    tmp, root, evil = _fixture()
    try:
        with tempfile.TemporaryDirectory() as mtmp:
            for name, old, new, must_red in _MUTATIONS:
                # 靶子自查（打偏的症状是"全绿"）。
                n = src.count(old)
                assert n == 1, "变异靶子打偏了：%r 出现 %d 次（要求恰好 1 次）" % (old[:60], n)
                mut = Path(mtmp) / "mutant.cjs"
                mut.write_text(src.replace(old, new, 1), encoding="utf-8")
                res = _run_harness(mut, root, evil)
                ok, detail = res.get(must_red, (None, "这条检查压根没跑"))
                check("尸首：%s" % name, ok is False,
                      "预期 `%s` 变红，实得 ok=%r %s" % (must_red, ok, detail))
            # 反向：没被变异的那份必须绿（否则"变红"可能只是夹具太吵）。
            res = _run_harness(_MODULE, root, evil)
            check("原模块在同一批检查下全绿",
                  all(v[0] for v in res.values()),
                  repr([k for k, v in res.items() if not v[0]][:4]))
    finally:
        tmp.cleanup()


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
