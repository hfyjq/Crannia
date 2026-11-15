// 壳自带的静态投递 + 到后端的请求转发。
//
// 与 dsh 的 `apps/desktop/src/web-document.ts` 对应（MIT，见 THIRD-PARTY.md
// 第 17 节）：两个函数的边界判定次序、前缀越界判据、出向剥头都照着那份重写。
//
// 本文件不 `require("electron")` —— 它只吃 `Request` / `Response`，是纯 Node。
//    拆出来的两个理由：
//      ① 两个职责（serve 静态 / forward 转发）能单独被看见。
//      ② 于是 `tests/test_desktop_protocol.py` 能用真的 node 把它跑起来。
//         证据要追到「哪一层被观察到」，源码 grep 不算。

"use strict";

const { readFile } = require("node:fs/promises");
const { extname, resolve, sep } = require("node:path");

//: 只服务我们真有的那几种类型。
//: 不认识的不猜（给 `application/octet-stream`，让浏览器当场现形）——
//:    猜错 MIME 的代价是"脚本不执行"，而症状看起来像脚本本身坏了。
//: `charset=utf-8` 必须带上：少了它浏览器按 latin-1 解，中文全变乱码，
//:    而那时 CSS/JS 语法照样过 —— 页面只是"字长得不对"。
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".json": "application/json",
  ".woff2": "font/woff2",
  ".png": "image/png",
  ".ico": "image/x-icon",
};

/**
 * 读一块壳自己拥有的静态资源（`crannia-app://shell/*` 与 `crannia-app://app/*`
 * 里那批 dist 文件走同一条）。
 *
 * @param {Request} request 自定义协议交过来的请求。
 * @param {string}  root    资源根目录。
 * @returns {Promise<Response>}
 */
async function serveWebDocument(request, root) {
  if (!["GET", "HEAD"].includes(request.method)) return new Response(null, { status: 405 });
  const url = new URL(request.url);
  let pathname;
  try {
    pathname = decodeURIComponent(url.pathname);
  } catch {
    // `decodeURIComponent` 对半个 `%E4` 会抛 —— 那是客户端给了坏路径，不是我们崩了。
    return new Response(null, { status: 400 });
  }
  const target = resolve(root, "." + (pathname === "/" ? "/index.html" : pathname));
  const directory = resolve(root);
  // 越界就是 403：`resolve` 会老老实实把 `../../` 算出去，所以判据只能落在
  //    "结果还在不在 root 里面"上（靠 `pathname` 里不许有 `..` 是拦不住的 ——
  //    `%2e%2e` 解码之后才变成 `..`）。
  //    `directory + sep` 那个 `sep` 不能省：少了它，`/x/dist-evil` 会被
  //    判成"在 `/x/dist` 里面"（前缀相同 ≠ 在里面）。
  if (!target.startsWith(directory + sep)) return new Response(null, { status: 403 });
  let body;
  try {
    body = await readFile(target);
  } catch (error) {
    if (error && error.code === "ENOENT") return new Response(null, { status: 404 });
    throw error;                       // 别的错（EACCES / 目录）照实抛，别装成 404
  }
  return new Response(request.method === "HEAD" ? null : new Uint8Array(body), {
    headers: { "content-type": MIME[extname(target)] || "application/octet-stream" },
  });
}

/**
 * 出向剥掉的 11 条响应头。理由：
 *
 *   `set-cookie` 会把后端的认证 cookie 交给页面自己的 cookie jar，而那个 jar
 *   属于壳；其余几条描述的是 Node `fetch` 那条连接（编码、长度、逐跳传输），
 *   而 Chromium 根本看不到它。
 *
 * `content-length` 在里面不是笔误：到达渲染进程的响应是流，
 *    长度以 Chromium 自己数的为准；留着上游那个数，一旦中间被改过长度
 *    （`content-encoding` 也正是为此被剥）就是一句对不上的话。
 */
const WITHHELD_RESPONSE_HEADERS = [
  "set-cookie",
  "content-encoding", "content-length",
  "transfer-encoding", "connection", "keep-alive", "te", "trailer", "upgrade",
  "proxy-authenticate", "proxy-authorization",
];

//: 进向删掉再转发的四条。
//: `host` —— 转发目标的 Host 由 `fetch` 按真地址写，留着原样的等于说谎。
//: `origin` / `sec-fetch-site` —— 它们是"给后端看的来客身份"，而后端看到的
//:   来客是壳，不是那个页面。
//: `cookie` —— 我们的凭据不走 cookie（见下面 `X-Crannia-Token` 那段），
//:   留着页面的 cookie 只会把页面的东西塞给后端。
const STRIPPED_REQUEST_HEADERS = ["host", "origin", "cookie", "sec-fetch-site"];

/**
 * 把 `crannia-app://app/*` 里不属于静态资源的那些请求转给后端。
 *
 * 三样必须同时保住，缺一个就有一种"看起来正常"的坏法：
 *    · `duplex:"half"` —— 流式请求体（`/api/chat` 带图那一档是几十 MB）。
 *      不给它，Node 直接抛 `RequestInit: duplex option is required when sending a body`。
 *    · `signal` —— 页面取消（关标签、`AbortController`）要真的传到后端去，
 *      否则那边继续跑、我们这边没人收。
 *    · `redirect:"manual"` —— 不替浏览器跟跳转。跟了的话 `Set-Cookie` 一族
 *      会落在 Node 这一层，而那一层是壳的东西（同 `WITHHELD_RESPONSE_HEADERS` 的理由）。
 *
 * 凭据不是壳注入的，是页面自己带的：应用的令牌走 `X-Crannia-Token` 头
 *    （`web/server.py::_check_token` 同时认 `?token=` 与这个头），
 *    而它由渲染进程从 `/ping` 现取（`packages/ui/src/api.js::ensureToken`）。
 *，这里原样让它过（不在 `STRIPPED_REQUEST_HEADERS` 里），
 *      壳不持有任何后端凭据 —— 少一个"壳里的秘密"就少一处会泄漏的地方。
 *    （让壳持有 cookie 再转发是另一条路 —— 这里不走它。）
 *
 * `fetchImpl` 必须能注入：
 *    默认的 `globalThis.fetch` 是 Node 的 undici，它有一个写死的
 *    `headersTimeout = 300_000` —— 服务端 5 分钟不吐响应头就掐断。
 *    而 `/api/chat` / `/api/approval` 是同步阻塞的（agent 跑完才回包），
 *    一次 384 秒的请求会在第 303.4 秒被掐，前端 `finally` 把
 *    「这一轮结束」当成「POST 回来了」，绿横幅消失、`busy` 打回 false，
 *    而 SSE 那条流照常推送。
 *    报错长这样：`fetch failed | cause: Headers Timeout Error`。
 *，`main.cjs` 传 Electron 的 `net.fetch`（Chromium 网络栈，没有那条上限）。
 *    本文件仍然不 `require("electron")` —— 凭据是注入进来的，
 *      纯 Node 的测试照样能拿真的 `globalThis.fetch` 跑同一套夹具。
 *
 * @param {Request} request         自定义协议交过来的请求。
 * @param {string}  hostUrl         后端基址（如 `http://127.0.0.1:5000`）。
 * @param {string}  expectedOrigin  唯一放行的进向 origin（`crannia-app://app`）。
 * @param {Function} [fetchImpl]    真正发请求的那个（默认全局 `fetch`；`main.cjs` 注入 `net.fetch`）。
 * @returns {Promise<Response>}
 */
async function forwardWebRequest(request, hostUrl, expectedOrigin,
                                 fetchImpl = globalThis.fetch) {
  const source = new URL(request.url);
  const origin = request.headers.get("origin");
  // 进向 `origin` 不对，403。
  //    `null` 是放行的：GET 与顶层导航本来就不带 `Origin` 头 ——
  //    那不是"来客没报家门"，是"这条请求没有那一栏"。
  //    （判据是 `origin !== null && origin !== 允许的那个 origin`。
  //     "不报就拒"会把所有 GET 静态请求一起拒掉 —— 那正是我们自己的
  //     首页与事件流，症状是整页空白，而日志里只有一片 403。）
  if (origin !== null && origin !== expectedOrigin) return new Response(null, { status: 403 });

  const target = new URL(hostUrl);
  target.pathname = source.pathname;
  target.search = source.search;

  const headers = new Headers(request.headers);
  for (const name of STRIPPED_REQUEST_HEADERS) headers.delete(name);

  const init = {
    method: request.method,
    headers,
    signal: request.signal,
    redirect: "manual",
  };
  if (request.body !== null && request.body !== undefined) {
    init.body = request.body;
    init.duplex = "half";
  }

  const response = await fetchImpl(target, init);
  const outgoing = new Headers(response.headers);
  for (const name of WITHHELD_RESPONSE_HEADERS) outgoing.delete(name);
  // `response.body` 原样交出去（不要 `await response.arrayBuffer()`）——
  //    `/api/events` 是 SSE，缓冲一下就等于把事件流变成"断线前什么都收不到"。
  return new Response(response.body, { status: response.status, headers: outgoing });
}

module.exports = {
  MIME,
  WITHHELD_RESPONSE_HEADERS,
  STRIPPED_REQUEST_HEADERS,
  serveWebDocument,
  forwardWebRequest,
};
