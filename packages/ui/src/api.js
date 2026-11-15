// 传输层 —— 两个壳共用同一套（后端只有一个，端点面也只有一套）。
//
// 缝在这里：本模块不知道自己在哪个壳里跑。
//   `baseUrl` 由壳注入 —— dev（渲染进程挂在 vite）传 `""`，走它的同源代理；
//   打包后页面来自 `crannia-app://app`，`/api` 一族由壳同源转给后端
//   所以也是 `""`。（`?api=` 那个口留着兼容旧调用形式，`main.cjs` 已不再传。）
//
// 对外是一个 client 对象 ——
//   因为 SSE 那条路要 token，而 `EventSource` 发不了自定义 header
//   （见下 `sseUrl`）。token 仍是一次取、内存里存，不进 URL 的可见面。

import { t } from "./i18n.js";

export function createClient(baseUrl = "") {
  let token;

  async function ensureToken() {
    if (token) return token;
    // 第一优先：壳注入的那一个（`preload.cjs` → `window.crannia.token`）。
    //    `/ping` 默认不吐令牌（审计第 2 条），桌面壳自己生成、
    //    经环境变量给后端、经 `additionalArguments` 给渲染进程。
    //    浏览器里单独调试后端时没有这座桥，退到 `/ping`，
    //    那一条要求服务端设了 `CRANNIA_ALLOW_PING_TOKEN=1`。
    const injected = (typeof window !== "undefined" && window.crannia
      && typeof window.crannia.token === "string") ? window.crannia.token : "";
    if (injected) return (token = injected);
    const response = await fetch(`${baseUrl}/ping`, { cache: "no-store" });
    // 这两句是直接显示给用户的（`toasts`）—— 要过 i18n。
    if (!response.ok) throw new Error(t("Crannia 后端未启动，请先运行 python -m web。"));
    const match = (await response.text()).match(/token=([^\s]+)/);
    if (!match) {
      // 一个字符串字面量（不许拆两半再拼）：拆了之后扫描器收的是两段，
      //    而 `t()` 查的是拼好的整句，这条报错永远是中文。
      throw new Error(t("无法取得启动令牌：桌面壳会直接注入；浏览器调试请给后端设 CRANNIA_ALLOW_PING_TOKEN=1。"));
    }
    return (token = match[1]);
  }

  async function call(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("X-Crannia-Token", await ensureToken());
    if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    const response = await fetch(`${baseUrl}${path}`, { ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || t("请求失败 ({status})", { status: response.status }));
    return data;
  }

  /** 事件流的 URL。`EventSource` 无法发自定义 header，鉴权只能走查询串
   *  （`web/server.py::_check_token` 同时认 `?token=` 与 `X-Crannia-Token` 头，
   *   WebUI 走的就是这条路）。 */
  async function sseUrl(path) {
    return `${baseUrl}${path}?token=${encodeURIComponent(await ensureToken())}`;
  }

  // ── 事件流：连一次 + 一张订阅表 ──
  //
  // 订阅表放在这里，不放在 App 里逐层传 props：状态横幅要 `turn.end`、
  //   活动块要 `tool.*` / `say` / `delta` —— 它们不是同一个组件，
  //   而 `Context` 之外最省事的就是让 client 自己当总线。
  // 一个 `EventSource`，不是每个组件各开一个 —— 服务端有「≤8 条流」的上限。
  const listeners = new Map();
  let source = null;

  function subscribe(type, fn) {
    if (!listeners.has(type)) listeners.set(type, new Set());
    listeners.get(type).add(fn);
    return () => { const s = listeners.get(type); if (s) s.delete(fn); };
  }

  function dispatch(type, data) {
    const set = listeners.get(type);
    if (set) set.forEach((fn) => fn(data));
  }

  async function connect() {
    if (source) return source;
    const url = await sseUrl("/api/events");
    source = new EventSource(url);
    // 必须逐个 `addEventListener`：`to_sse()` 发的是命名事件
    //   （帧里带 `event: <type>`），浏览器只派发给注册了该类型名的监听器，
    //   `onmessage` 收不到。
    // `error` 必须单独接，不能走上面那一圈：这个名字被两件事共用 ——
    //   ① 浏览器原生的连接错误（`ev.data` 是 `undefined`）；
    //   ② 后端的命名事件 `event: error`（`ev.data` 是一段 JSON）。
    //   混在一起时，`parseFrame(undefined)` 回落成 `{}`，于是每一次断线/
    //   重连都被当成「这一轮出错了」：活动块被就地收口、`busy` 被清、
    //   还弹一句假报错 —— 症状正是「换语言/刷新之后步骤流没了」。
    EVENT_TYPES.forEach((t) => {
      if (t === "error") return;
      source.addEventListener(t, (ev) => dispatch(t, parseFrame(ev)));
    });
    source.addEventListener("open", () => dispatch("stream.state", { connected: true }));
    source.addEventListener("error", (ev) => {
      if (classifyErrorFrame(ev) === "stream") {
        // 判据全在 `classifyErrorFrame` 里（纯函数，node 逐条钉着）。
        dispatch("stream.state", { connected: false });
        return;
      }
      dispatch("error", parseFrame(ev) || {});
    });
    return source;
  }

  /** 后端静态目录里那张资源的完整地址。
   *
   *  判据与 `call` 逐字同一个：本模块不知道自己在哪个壳里跑 ——
   *  dev 走 vite 同源代理，打包后走 `crannia-app://app` 的壳内转发，两边都是 `""`。
   *
   *  前端里不许再出现 `/static/...` 这种斜杠开头的资源路径：
   *  ① 在 `file://` 下，`/static/img/logo.svg` 会解析成
   *     `file:///static/img/logo.svg`（盘根目录），404，图变成裂图。
   *  ② 页面走 `crannia-app://app` 之后它不再致命 —— 那个 scheme
   *     解析得对，而且壳会把它转给后端，图能出来。
   *，这条规矩留着：资源的 base 只许有一处。
   *     散出去的话，下次换壳 / 换协议就得满仓再找一遍 ——
   *     而漏掉的那一张看起来只是"图没出来"，不会报错。
   *  别用 `<img crossorigin>` —— 那会把它变成一次 CORS 请求，
   *  而后端没有也不该有 CORS 头（`main.cjs` 顶上那条注释同理）。
   */
  const asset = (p) => `${baseUrl}${p}`;

  /** 后端 WebUI 那一页的完整地址。
   *
   *  判据与 `asset` 相反：`asset` 要的是这个前端自己的静态资源，
   *  而这里要的是另一个前端（WebUI 那套 Flask 页面，`web/templates/*.html`）。
   *
   *  不许拿 `baseUrl` 当后端源 —— dev 下 `baseUrl === ""`
   *    （渲染进程挂在 vite 上），拼出来的是 vite 自己的页面。由后端自报
   *    （`/api/status.web_url`，取自 `request.host_url`），从 `origin` 传进来。
   *  token 只能进查询串：`window.open` 开的是系统浏览器，
   *    它发不了自定义头（与 `sseUrl` 逐字同一条理由）。
   */
  async function webUrl(path = "/", origin = "") {
    const t = await ensureToken();
    const base = String(origin || baseUrl).replace(/\/+$/, "");
    const sep = path.includes("?") ? "&" : "?";
    return `${base}${path}${sep}token=${encodeURIComponent(t)}`;
  }

  /** 一个产物的完整地址（`GET /api/artifact?id=…`）。
   *
   *  判据与 `sseUrl` 逐字同一条：`<img>` / `<audio>` / `<video>` 发不了
   *  自定义 header，鉴权只能走查询串。`_check_token` 两样都认。
   *  这里只拼 id，不拼路径 —— 端点也只认 id（`core/artifacts.py`
   *  的 `valid_id` 是唯一形状判据）。
   */
  async function artifactUrl(id) {
    const t = await ensureToken();
    return `${baseUrl}/api/artifact?id=${encodeURIComponent(id)}`
      + `&token=${encodeURIComponent(t)}`;
  }

  /** 下载用：不解析 JSON。
   *
   *  与 `call` 分开是有判据的：`call` 那句 `response.json()` 会把二进制体吃掉
   *  （设置包是 JSON，但记忆包是 zip）。返回 `{blob, filename}`，
   *  文件名取自 `Content-Disposition`（后端铸的，前端不自己猜）。
   */
  async function callBlob(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("X-Crannia-Token", await ensureToken());
    if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    const response = await fetch(`${baseUrl}${path}`, { ...options, headers });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || t("请求失败 ({status})", { status: response.status }));
    }
    const cd = response.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="([^"]+)"/);
    return { blob: await response.blob(), filename: m ? m[1] : "download.bin" };
  }

  /** 上传用（`FormData`）。
   *
   *  不许设 `Content-Type` —— 浏览器要自己加 `boundary`，
   *  设成 `multipart/form-data` 会把 boundary 弄丢，后端收不到文件。
   */
  async function callForm(path, form) {
    const headers = new Headers();
    headers.set("X-Crannia-Token", await ensureToken());
    const response = await fetch(`${baseUrl}${path}`, {
      method: "POST", headers, body: form,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || t("请求失败 ({status})", { status: response.status }));
    return data;
  }

  return { call, callBlob, callForm, sseUrl, artifactUrl, asset, webUrl,
           baseUrl, connect, subscribe };
}

export const jsonRequest = (method, payload) => ({ method, body: JSON.stringify(payload) });

/** 事件流里我们要渲染的那些类型。
 *
 * `to_sse()` 发的是命名事件（帧里带 `event: <type>`），浏览器只派发给注册了
 * 该类型名的监听器，`onmessage` 收不到。所以这份名单必须逐个
 * `addEventListener`，加一种事件 = 名单与监听两处都要改。
 *
 * `bg.start` / `bg.end` 刻意不在名单里：它们在 `task.end` 之后到达
 *，若在前端开活动块，没有任何后续事件会去关它。
 * 后端也刻意不注册它们（`core/events.py:153-157`）。
 */
export const EVENT_TYPES = [
  "task.start", "task.end",
  "turn.start", "turn.end",
  "thinking", "say", "delta",
  "tool.start", "tool.end",
  "approval.request", "approval.resolved",
  "context.state", "memory.saved", "memory.organized",
  "turn.queued", "files.changed", "error",
  // 跑着的时候补的那句话被收下了：摘「排队中」· 旧块收口 ·
  //   新开一块。同一条纪律：加事件 = 这份名单 + `App.jsx` 的 `client.subscribe`。
  "steer.delivered",
  // 试跑浮窗：周期任务的一次运行。
  //   必须加进这份名单 —— `to_sse()` 发的是命名事件，浏览器只派发给
  //   注册了该类型名的监听器（`onmessage` 收不到）。加事件 = 两处：
  //   这里 + 订阅方（`TestRunModal.jsx` 的 `client.subscribe`）。
  "cron.start", "cron.end",
  // 一个插件被屏蔽了。
  //   同上面那条：加事件 = 两处（这份名单 + `App.jsx` 的 `client.subscribe`），
  //   少一处就是"开关打开了、流也在跑、用户一个字都看不到"。
  "plugin.blocked",
  // 无主理人群聊的成员工作状态（谁在被派活 / 谁完成了）。
  //   同一条纪律：加事件 = 这份名单 + `App.jsx` 的 `client.subscribe`，两处都要。
  "group.member.start", "group.member.done",
  // 模型连接重试（红色横幅）。
  //   同一条纪律：加事件 = 这份名单 + `App.jsx` 的 `client.subscribe`，两处都要。
  "llm.retry", "llm.retry_ok",
  // 压缩进行中 / 结果：黄横幅 + 挡住发送；失败转红。
  //   同一条纪律：加事件 = 这份名单 + `App.jsx` 的 `client.subscribe`，两处都要。
  "compact.progress",
  // 任务的四种状态：顶栏两格计数 + 两个浮窗的信号。
  //   它们只更新计数，不开活动块 —— 可能落在 `task.end` 之后，开块没人收口。
  //   同一条纪律：加事件 = 这份名单 + `App.jsx` 的 `client.subscribe`。
  "task.started", "task.finished", "task.failed", "task.cancelled",
];

/** 一条 `error` 监听回调属于哪一类（纯函数，node 能直接断言）。
 *
 *  名字 `error` 被两件事共用，而它们的后果完全相反：
 *    ① `"stream"` —— 浏览器原生的连接错误（`ev.data` 不是字符串）
 *       或后端的 `{"error":"too many streams"}`：流的事，这一轮没错；
 *    ② `"event"` —— 后端命名事件 `event: error`（带 `message`）：这一轮出错了。
 *  混成一类时的现场：每次断线都去"收口活动块 + 清 busy + 弹一句报错"
 *，症状是"换语言/刷新之后步骤流整个没了"。
 */
export function classifyErrorFrame(ev) {
  if (!ev || typeof ev.data !== "string") return "stream";
  let env = null;
  try { env = JSON.parse(ev.data); } catch { return "stream"; }
  const body = (env && env.data) || env || {};
  if (body.error && !body.message) return "stream";
  return "event";
}

/** SSE 帧的形状（`core/events.py::to_sse`）：
 *  `event: <type>` + `data: {"type","seq","ts","session","data":{…}}`
 *，载荷在 `data.data`（信封里的 `type`/`ts` 会遮蔽同名键，所以后端刻意不展开）。
 *
 *  信封里那个 `ts` 也要带出来 —— 气泡下面的时间戳靠它。
 *    （`ts` 是 epoch 秒（`float`），`core/events.py:335` `field(default_factory=time.time)`。
 *     前端要 `×1000` 再 `new Date()`。）
 *  后端刻意不展开 data，所以这里手工并一下；键名用 `ts` 不会撞 ——
 *    data 里没有这个键（不然后端展开就会被它遮蔽）。
 */
export function parseFrame(ev) {
  try {
    const env = JSON.parse(ev.data);
    // （试跑浮窗）：把 `session` 也带出来 —— 信封里有它
    //   （`core/events.py::to_sse` 的 payload），而消费方要按会话过滤
    //   （"只显示这一次的步骤"）。后端 data 里没有 `session` 这个键，
    //   所以并进去不会遮蔽（同 `ts` 那条注释）。
    return { ...(env.data || {}), ts: env.ts, session: env.session };
  } catch {
    return {};
  }
}
