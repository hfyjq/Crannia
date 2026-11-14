const { app, BrowserWindow, Menu, Tray, shell, ipcMain, protocol, dialog, screen, net, nativeTheme } = require("electron");
const { spawn } = require("node:child_process");
const http = require("node:http");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
// 后端子进程的生命周期。不 import electron ——
// 只接收"像 child 的对象"与"能查状态的函数"，因此可以在真实子进程上单独测试。
const life = require("./backend-lifecycle.cjs");
// 壳自带的静态投递 + 到后端的请求转发。不 import electron ——
// 因此可以用纯 Node 单独运行与测试。
const { serveWebDocument, forwardWebRequest } = require("./web-document.cjs");
// IPC 属主校验。不 import electron —— 只接收鸭子类型的 event / win，
// 因此可以用纯 Node 加假对象单独运行与测试。
const guard = require("./ipc-guard.cjs");
// 「渲染进程没了」时在窗口里出声的文案与判据。不 import electron ——
// 文案与判定逻辑可以由纯 Node 测试直接钉住。
const gone = require("./renderer-gone.cjs");
let mainWindow; let tray; let quitting = false; let backend;
let backendStopped = false;  // 子进程收过尾了（`will-quit` 只许走一次）
let quitChecked = false;     // "现在退会打断什么"问过了（`before-quit` 只许走一次）
let quitAsking = false;      // 那个确认框正开着（再按一次「退出」= 直接走）
//: 「我们 spawn 的那个后端」答应过 —— 判据是带令牌的 `/api/status` 回 200，
//:   不是 `/ping` 回 200（那个不验令牌，端口上坐着别人时也会答应）。
let backendReady = false;
let splash = null;          // 启动小窗
let splashReadyAt = 0;      // 主进程喊过"就绪"的时刻 —— 用来算最小停留
let splashState = null;     // 已经决定过的状态（见 `tellSplash` 的竞态说明）
let startupFailed = false;  // 已经报过错，别让后面的路径再动小窗
//: 启动令牌由壳自己生成（审计第 2 条：`/ping` 曾把它明文
//: 回给任何本机进程）。两条去向：
//:   ① 环境变量 `CRANNIA_LAUNCH_TOKEN` → 后端起服务时就用它（`web/server.py`）；
//:   ② `additionalArguments` → 渲染进程（`preload.cjs` 暴露成 `window.crannia.token`）
//:      → `api.js` 的 `ensureToken()` 直接用它，不再去 `/ping` 取。
let _token = crypto.randomBytes(24).toString("base64url");
//: `render-process-gone` 的「这个框已经说过了」名单 —— 键是 `reason:exitCode`。
//:   同一种故障连续出现时（例如 `launch-failed` / `exitCode 18` 重复两次），
//:   弹两次是噪声，不是信息。
//:   只压对话框；终端那行 `[<tag> gone]` 一个字都不压 —— 它是原始读数，
//:   而「发生了几次」本身就是要看得见的事实。
const _goneNoticed = new Set();
const API_PORT = Number(process.env.CRANNIA_API_PORT || 5000);
const API_BASE = `http://127.0.0.1:${API_PORT}`;

// 协议层

//: 壳自己的协议名。只能由小写字母 / 数字 / `+` / `-` / `.` 组成（没有下划线）。
//: 它是唯一的字面量，没有别处推导它；改应用名时要连这一行一起改。
const SCHEME = "crannia-app";

//: 必须在 `app.ready` 之前调、且只能调一次（判据：Electron 的
//:   `registerSchemesAsPrivileged` 只认"ready 之前"这一个时间窗，窗口过了就直接抛）。
//:   所以它在模块顶层，不在任何 `then` 里。
//:
//: 六个权限各管什么：
//:   · `standard` —— 按 RFC 3986 那样解析（像 http）。不开它：相对 URL
//:     （`./assets/index.js`）解析不了 · web storage 全废（localStorage /
//:     sessionStorage / IndexedDB / cookie），要替代 http 就必须开。
//:   · `secure` —— 按 https 待遇（安全上下文）
//:   · `supportFetchAPI` —— 页内 `fetch()` 打不了这个 scheme
//:   · `corsEnabled` —— 不开 CORS
//:   · `stream` —— `<video>`/`<audio>` 默认按缓冲处理；SSE 也吃它
//:   · `codeCache` —— V8 code cache（只在 `standard` 也开时才有效）
protocol.registerSchemesAsPrivileged([{
  scheme: SCHEME,
  privileges: {
    standard: true,
    secure: true,
    supportFetchAPI: true,
    corsEnabled: true,
    stream: true,
    codeCache: true,
  },
}]);

//: 哪些路径算「前端 dist 自己的」，由壳端上来，不转发。
//: 这张清单是壳自己的决定，不是"文件系统里恰好有什么"——
//: 往里多加一条，就等于把某个本该走后端的路径从后端手里抢走
//: （症状：那个接口永远 404，而壳这边一切正常）。
const _APP_OWNED = new Set(["/", "/index.html", "/favicon.svg", "/manifest.webmanifest"]);

function isAppOwnedPath(pathname) {
  return _APP_OWNED.has(pathname) || pathname.startsWith("/assets/");
}

/** 两个 hostname 的分工。
 *
 *     crannia-app://shell/*   → 壳自己的页面（启动小窗）—— 永不经过后端
 *     crannia-app://overlay/* → 接管期间的顶层横幅 —— 同样永不经过后端
 *     crannia-app://app/*     → ① 前端 dist（`/` · `/index.html` · `/assets/*`…）
 *                                ② 其余一切转发给后端
 *     crannia-app://<其他>    → 404
 *
 *  为什么自己注册一个协议，而不是继续用 `file://`：
 *    `file://` 的 origin 是 `null`，而应用的令牌走 `X-Crannia-Token` 头
 *    （后端没有、也不该有 CORS），从 `file://` 打 `http://127.0.0.1:5000`
 *    是一次跨源请求，浏览器按同源策略拦掉。
 *    自定义协议把「页面」与「它的 `fetch` / `EventSource`」放进同一个 origin
 *，无端口、无 CORS、无 cookie 域问题。
 *    同时整类消除 `file://` 下的相对路径问题：那个 scheme 下
 *    `/x` 与 `./x` 都解析得对。
 */
function handleProtocolRequest(request) {
  const url = new URL(request.url);
  if (url.hostname === "shell") return serveWebDocument(request, __dirname);
  // 接管横幅是壳自己的页面（同样的 `__dirname`，同样的"不经后端"）。
  if (url.hostname === "overlay") return serveWebDocument(request, __dirname);
  if (url.hostname === "app") {
    if (isAppOwnedPath(url.pathname)) return serveWebDocument(request, path.join(__dirname, "dist"));
    // 后端还没起来，503，不是 404。
    //    404 说的是"这个地址不存在"—— 那是一句假话，它会把排查推到
    //    "路径写错了"那个方向；503 说的是"我现在没法伺候你"，而那正是此刻的事实。
    if (!backendReady) return new Response(null, { status: 503 });
    // 必须传 `net.fetch`，不能用全局 `fetch`。
    //    全局 fetch 是 Node 的 undici，`headersTimeout` 写死 300 秒；
    //    而 `/api/chat` / `/api/approval` 是同步阻塞的（agent 跑完才回包）——
    //    一次 384 秒的请求会在第 303.4 秒被掐断，绿横幅消失、`busy` 打回 false，
    //    而 SSE 照常推送。
    //    `net.fetch` 走 Chromium 网络栈，没有那条上限（流式请求体与 SSE 都正常）。
    return forwardWebRequest(request, API_BASE, `${SCHEME}://app`,
                             (input, init) => net.fetch(input, init));
  }
  return new Response(null, { status: 404 });
}

//: 后端子进程的有界诊断缓冲（64KB，留尾部）。
const backendDiag = life.createDiagnostics();

//: 小窗尺寸 —— 必须与 `splash.html` 里的 `--win-w/--win-h` 一致。
//: 两处写同一个数是有意的（那个页面在浏览器里也要能独立打开），
//: 但谁改谁得改两处：症状是"窗口比画布大一圈、右下角一片白"。
const SPLASH_W = 688, SPLASH_H = 386;

//: 「准备就绪」至少停留这么久才切主窗（0.5 秒）。
//:   ⏱ 它从"喊了就绪"那一刻算，而不是从"后端起来"算 ——
//:   否则主窗建得快时，那句「准备就绪」会一闪而过、等于没有。
const MIN_HOLD_MS = 500;

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on("second-instance", () => { if (mainWindow) { mainWindow.show(); mainWindow.focus(); } });
  app.whenReady().then(async () => {
    // 第一件事就把协议接上：下面 `openSplash()` 里那个 `loadURL` 立刻就要用它，
    //    晚一行就是"小窗打不开"，而那时终端里只有一句 `ERR_UNKNOWN_URL_SCHEME`。
    protocol.handle(SCHEME, handleProtocolRequest);
    // 在开小窗之前把用户上次选的主题装回去 —— 小窗比主窗先开，
    //    它是靠 `prefers-color-scheme` 判深浅的，而那个值由 `themeSource` 定。
    try {
      nativeTheme.themeSource = loadThemeMode();
    } catch (err) {
      console.warn(`[theme] 读取上次的主题失败：${err && err.message ? err.message : err}`);
    }
    // 先开小窗，再等后端 —— 顺序是这一句的全部意义。
    //    若先 `await startBackend()` 再 `createWindow()`，从点图标到后端起来
    //    这段时间屏幕上什么都没有（窗口压根还没建），用户看到的只是"干等"。
    openSplash();
    //: 上一次下好的更新：先装、后起后端（那些文件正被后端占着）。
    //:   只在安装版里做 —— 开发树没有安装目录，装安装包没有意义。
    if (app.isPackaged) {
      const pend = pendingUpdate();
      if (pend) { applyPendingUpdate(pend); return; }
    }
    try {
      await startBackend();
      await startFramework();
    } catch (err) {
      // 分层：先判是不是"压根装错了"（`#00`），不是就试着救一次（`#03`）。
      //   两种都报在小窗里 —— "静默消失"那条路早就封了。
      //   救活了就继续往下建主窗；救不动时 `recoverOrExplain` 已经报过。
      if (!(await recoverOrExplain(err))) return;
    }
    createWindow();
    createTray();
    app.on("activate", () => mainWindow?.show());
  });
}

// 启动小窗

/** 把渲染进程的话接到终端。
 *
 *  白屏最难查的地方就是它什么都不说（React 一崩就整棵树卸载）。
 *  这三条把三类死法都接到 stdout：
 *    · `console-message`     —— 页面里的报错、`console.error` 全走这里
 *    · `preload-error`       —— preload 自己炸了
 *    · `render-process-gone` —— 整个渲染进程没了（比页面崩更狠的一档）
 *  `console-message` 的签名在 Electron 新旧版本之间变过（新的是 details 对象）
 *，两种都接，别假定一种。
 *  小窗同样要接这三条：小窗一旦出故障（动画停止、状态文字不再推进），
 *    那是只有渲染进程才知道的事；漏接就一个字都到不了终端，只能靠猜。
 *    仪表漏装的那一格，正是出事的那一格。
 */
/** 渲染进程没了的时候，在窗口里说人话。
 *
 *  为什么用原生对话框，而不是往页面里灌一段最小 HTML ——
 *  这一条是关键取舍：`launch-failed` 的判据就是「渲染进程压根没启动起来」
 *  （见 `renderer-gone.cjs` 抬头），也就是说页面没有可渲染的地方，那正是它没起来的原因。
 *  往 `webContents` 里 `loadURL` 一段 HTML 是在赌"这一次能起来"；赌输了它会
 *  再发一次 `render-process-gone`，「报错」变成「无限报错」，而那比白屏更难查。
 *  `dialog` 由浏览器进程画，不经过渲染进程，它恰恰是这一档还能用的那件工具。
 *
 *  去重按 `reason:exitCode`（进程级，不是每个窗口一份）—— 逐字相同的两条读数
 *  应当只让用户看到一个框。终端那行不去重（见 `_goneNoticed`）。
 *
 *  @param {object} win      出事的那个 BrowserWindow（可能已经没了）。
 *  @param {unknown} details Electron 给的 `details`（可能是 `undefined`）。
 *  @param {string} tag      日志前缀（`renderer` / `splash`）。
 *  @returns {Promise<unknown>|null} 没弹（窗口没了 / 说过了 / 对话子系统坏了）时 `null`。
 */
function showRendererGoneNotice(win, details, tag) {
  const d = (details && typeof details === "object") ? details : {};
  // 窗口已经没了，只留终端那行：框没有意义，而且此刻 `setTitle` 会抛。
  if (!win || win.isDestroyed()) return null;
  const key = String(d.reason) + ":" + String(d.exitCode);
  if (_goneNoticed.has(key)) return null;
  _goneNoticed.add(key);

  // 窗口标题也是「界面在说话」的一部分：默认的 `Electron` 标题
  //   —— 一个白框配一个不说明任何事的标题，等于界面在说谎。
  //   只许吞掉自己的异常：这一段跑在一条已经出事的路上。
  let notice;
  try {
    notice = gone.noticeText(d);
    win.setTitle(notice.title);
  } catch (err) {
    console.error(`[${tag} gone] 准备那句话时出错：${err && err.message ? err.message : err}`);
    return null;
  }

  try {
    // 刻意不挂父窗口（`dialog.showMessageBox(win, opts)` 那个重载）：
    //   主窗此刻很可能还没 `show()` —— 它的 `ready-to-show` 永远不会来了
    //   （渲染进程没起来），而挂在一个隐藏窗口上的模态框会跟着一起看不见
    //   所以那就是「加了报错、用户还是看不到」，正是要避免的形状。
    return dialog.showMessageBox({
      type: "error", buttons: ["知道了"], defaultId: 0,
      title: notice.title, message: notice.message, detail: notice.detail,
    });
  } catch (err) {
    // 对话子系统本身坏了（无显示环境…），照旧出声：上面终端那行就是全部读数。
    console.error(`[${tag} gone] 弹框失败（${err && err.message ? err.message : err}）`
      + " —— 终端里那行就是全部读数");
    return null;
  }
}

/** 渲染进程崩溃后的自动恢复页。
 *
 *  ## 为什么不是"在原页面上加个遮罩"
 *
 *  `render-process-gone` 意味着渲染进程已经死了 —— 页面没了，画不出任何东西。
 *  所以先 `loadURL` 一张内联恢复页（它会重建渲染进程），3 秒后再 `loadURL`
 *  回原来那个地址。倒计时写在恢复页自己的 JS 里，因为跑它的已经是新进程。
 *
 *  ## 为什么 `launch-failed` 不走这条路
 *
 *  `launch-failed` = 渲染进程压根没启动（官方逐字 *"Process never
 *  successfully launched"*）。环境不让它起来，重载一次结果一样 ——
 *  在它上面赌只是把白屏延长 3 秒。那一类仍旧走 `showRendererGoneNotice`。
 */
function _recoverPageHtml(reasonText, seconds) {
  const esc = (s) => String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  return "<!doctype html><meta charset=\"utf-8\"><title>正在恢复</title><style>"
    + "html,body{height:100%;margin:0;background:#1f1f23;color:#e8e8ea;"
    + "font:14px/1.6 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
    + "display:flex;align-items:center;justify-content:center}"
    + ".box{max-width:560px;text-align:center;padding:24px}"
    + "h1{font-size:16px;font-weight:600;margin:0 0 4px}"
    + "#cd{font-size:30px;font-variant-numeric:tabular-nums;margin:18px 0;color:#f0c040}"
    + "pre{text-align:left;white-space:pre-wrap;word-break:break-all;background:#2a2a30;"
    + "border-radius:6px;padding:10px;font-size:12px;color:#b9b9c0;max-height:180px;overflow:auto}"
    + "</style><div class=\"box\">"
    + "<h1>" + esc(tr("#01 渲染组件异常，需要重新启动。",
                      "#01 Renderer component failed; a restart is needed.")) + "</h1>"
    + "<h1>" + esc(tr("我们正在尝试解决，请稍后",
                      "We are trying to fix it, please wait")) + "</h1>"
    + "<div id=\"cd\">" + seconds + "</div>"
    + "<pre>" + esc(reasonText) + "</pre></div>"
    + "<script>let n=" + seconds + ";var el=document.getElementById('cd');"
    + "var t=setInterval(function(){n=Math.max(0,n-1);el.textContent=n;"
    + "if(!n){clearInterval(t);}},1000);</script>";
}

/** 崩了先自恢复一次，第二次才叫人。
 *
 *  上限是判据不是洁癖：可复现的崩溃（某个页面一渲染就崩）如果每次都自动
 *  reload，用户看到的是窗口一直闪，比白屏还难查。所以第一次自动，
 *  第二次退回 `showRendererGoneNotice`（那里有 `_goneNoticed` 去重）。
 *
 *  @returns 恢复页能不能装上 —— 装不上时调用方仍只管终端那行。
 */
function recoverRenderer(win, details, tag, lastGoodUrl, used) {
  if (!win || win.isDestroyed()) return false;
  const d = (details && typeof details === "object") ? details : {};
  if (d.reason === "launch-failed") return false;   // 见上面那段：重载没用
  if (used.n >= 1) return false;                    // 已经自动过一次
  const back = String(lastGoodUrl || "");
  if (!back) return false;                          // 不知道回哪，别赌

  used.n += 1;
  const reasonText = "reason=" + String(d.reason) + "  exitCode=" + String(d.exitCode);
  try {
    win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(_recoverPageHtml(reasonText, 3)));
  } catch (err) {
    console.error(`[${tag} gone] 恢复页装不上：${err && err.message ? err.message : err}`);
    return false;
  }
  setTimeout(() => {
    try { if (!win.isDestroyed()) win.loadURL(back); } catch (err) {
      console.error(`[${tag} gone] 回不到原地址：${err && err.message ? err.message : err}`);
    }
  }, 3000);
  return true;
}

function bridgeRenderer(win, tag) {
  //: 这个窗口上一次真的加载成功的地址。崩溃后要靠它回去 ——
  //:   `render-process-gone` 那一刻 `getURL()` 可能已经拿不到了。
  //:   恢复页自己也是 `data:`，不能让它把这格覆盖掉。
  let lastGoodUrl = "";
  const recoverUsed = { n: 0 };

  win.webContents.on("console-message", (...args) => {
    const d = args.find((a) => a && typeof a === "object" && "message" in a);
    const msg = d ? d.message : args[2];
    if (msg) console.log(`[${tag}] ${msg}`);
  });
  win.webContents.on("preload-error", (_e, file, err) => {
    console.error(`[${tag} preload] ${file}\n${err && err.stack ? err.stack : err}`);
  });
  // 记"上一次真的加载成功"的地址 —— 崩溃那一刻 `getURL()` 可能已经拿不到。
  //   恢复页自己是 `data:`，不许它把这格覆盖掉。
  win.webContents.on("did-finish-load", () => {
    try {
      const u = win.webContents.getURL();
      if (u && !u.startsWith("data:")) lastGoodUrl = u;
    } catch (err) {
      // 记不住就记不住：恢复时那条路会自己退回"别赌"。
    }
  });
  win.webContents.on("render-process-gone", (_e, details) => {
    // `launch-failed` 这一类的读数：`{"reason":"launch-failed","exitCode":18}`
    //   —— 现象是白屏。
    //   读这行时的判据：`launch-failed` = 渲染进程压根没启动起来
    //   （官方文档逐字 *"Process never successfully launched"*，而且此时
    //   `exitCode` 不是进程退出码、是平台侧的"启动失败"码）。
    //   所以页面、JS、任何前端代码都还没轮到被执行，它不是页面 bug，
    //     去改 `App.jsx` / `dist/` 是白费功夫。
    //   已观察到的疑凶是内核级反作弊驱动（只有一个样本，尚未排除替代解释）；
    //   排查顺序：先查近期运行过的反作弊游戏，再查 Windows 更新与核心隔离 / VBS，
    //   最后换时间点重试。
    //
    // 顺序承重：先落终端、再弹框。反过来的话，弹框自己抛异常时
    //   终端里一行读数都不会有，又回到"白屏 + 什么都不说"。
    console.error(`[${tag} gone] ${JSON.stringify(details)}`);
    // 先试着自恢复（只对可重试的那一类）；赌不了才弹框。
    // 顺序承重：终端那行在 `recoverRenderer` 之前 —— 它自己抛异常时
    //   至少还有一行读数（同白屏那条判据：白屏也要有声）。
    if (!recoverRenderer(win, details, tag, lastGoodUrl, recoverUsed)) {
      // 白屏也要在窗口里出声：只留终端一行时，用户看到的仍是一片白。
      showRendererGoneNotice(win, details, tag);
    }
  });
}

// 启动小窗的随机句

/** 后端那一套源码（`core/` `bus/` `web/` …）的根目录。
 *
 *  打包之后 `__dirname` 在 asar 里面
 *    `path.resolve(__dirname, "../..")` 算出来是个不存在的路径，
 *    而 `spawn(..., { cwd:不存在 })` 会当场抛（不是"起不来"，是抛）。
 *，打包时取 `process.resourcesPath/app` —— `electron-builder.config.mjs` 的
 *    `extraResources` 把 Python 那套放在那儿，asar 之外
 *    （解释器读不了 asar：它不是一个真目录）。
 *  两处必须对上：这一行与那份配置里的 `{ from: ROOT, to:"app" }`。
 *    `tests/test_desktop_package.py` 钉着它们。
 */
function backendRoot() {
  if (app.isPackaged) return path.join(process.resourcesPath, "app");
  return path.resolve(__dirname, "../..");
}

/** 数据根 —— 与 `core/paths.py::data_root()` 同一套判据。
 *
 *  安装版是 `%APPDATA%\\Crannia`（后端 `prepare()` 会 chdir 过去），开发树是仓根。
 *  壳读 `settings.json` / 待安装标记都在这里：`backendRoot()` 是代码根，
 *    安装版里那份 `data/settings.json` 是打包时带进来的模板，不是用户改过的那份
 *    （读错的症状：小窗语言永远跟着模板走）。
 */
function dataRoot() {
  if (!app.isPackaged) return backendRoot();
  return path.join(app.getPath("appData"), "Crannia");
}

/** 版本号只比数字段（`1.0.0` 与 `1.0.1`）。读不出的段按 0 ——
 *  它只用来回答"这一份比我现在新吗"，读不到就不装（fail-closed 那一侧）。 */
function versionParts(v) {
  return String(v || "").replace(/^v/i, "").split(/[-+]/)[0]
    .split(".").map((x) => parseInt(x, 10) || 0);
}

function versionIsNewer(a, b) {
  const A = versionParts(a), B = versionParts(b);
  for (let i = 0; i < Math.max(A.length, B.length); i += 1) {
    const x = A[i] || 0, y = B[i] || 0;
    if (x !== y) return x > y;
  }
  return false;
}

/** 下好、等着这次启动装上的那一份（后端下载完写的 `data/updates/pending.json`）。
 *
 *  为什么由壳读磁盘、而不是问后端：装更新必须在起后端之前
 *  （那些文件正被后端占着），那一刻还没有任何接口可问。
 *  三条都要真的成立才认：文件在、安装包在、版本比当前新。
 */
function pendingUpdate() {
  const file = path.join(dataRoot(), "data", "updates", "pending.json");
  try {
    const raw = JSON.parse(fs.readFileSync(file, "utf8"));
    const version = String(raw.version || "").trim();
    const installer = String(raw.path || "").trim();
    if (!version || !installer || !fs.existsSync(installer)) return null;
    if (!versionIsNewer(version, app.getVersion())) return null;
    return { marker: file, version, installer };
  } catch (e) {
    return null;
  }
}

/** 静默装上那一份，然后把自己重新拉起来。
 *
 *  顺序是有意的：小窗先换成「正在更新」并停 1.6 秒（用户要知道发生了什么），
 *  再退出本进程 —— 装的时候我们的文件不能还被占着 —— 交给一个脱离的
 *  PowerShell 等两秒、`/S` 装、装完 `Start-Process` 拉起新版本。
 *  `/D=` 必须放在参数串最后且不加引号（NSIS 的规矩）；所以整串交给
 *  `-ArgumentList` 一个字符串，不让 PowerShell 再替我们加引号。
 *
 *  为什么要一个"中转" PowerShell（`relay`）而不是直接 spawn 脚本：
 *    真机实测（2026-11-13）—— 直接 `spawn(powershell -File …)` 然后立刻
 *    `app.exit(0)`，子进程会被一起收掉（脚本一次都没跑起来）。改成
 *    先起一个 PowerShell，让它用 `Start-Process` 把真正的脚本甩出去
 *    （Start-Process 起的进程不挂在我们的进程树上），等它交接完（2 秒）
 *    我们再退。
 */
const UPDATE_HOLD_MS = 1600;

function applyPendingUpdate(p) {
  console.log(`[update] 待安装 v${p.version}：${p.installer}`);
  tellSplash({ updating: true });
  const dir = path.dirname(process.execPath);
  const script = [
    "param($Installer, $Dir, $Exe)",
    '$log = Join-Path $env:TEMP "crannia_update.log"',
    'Add-Content $log "$(Get-Date -Format o) start: $Installer -> $Dir"',
    "Start-Sleep -Seconds 2",
    "try {",
    "  $p = Start-Process -FilePath $Installer -ArgumentList ('/S /D=' + $Dir) -Wait -PassThru",
    '  Add-Content $log "$(Get-Date -Format o) installer exit $($p.ExitCode)"',
    "  if ($p.ExitCode -eq 0) {",
    "    Start-Process -FilePath $Exe",
    "    Remove-Item -LiteralPath $Installer -Force -ErrorAction SilentlyContinue",
    "  }",
    '} catch { Add-Content $log "$(Get-Date -Format o) failed: $($_.Exception.Message)" }',
  ].join("\r\n");
  const ps = path.join(app.getPath("temp"), "crannia_apply_update.ps1");
  setTimeout(() => {
    try { fs.unlinkSync(p.marker); } catch (e) { /* 清不掉也别拦住重启 */ }
    try {
      // BOM：Windows PowerShell 5.1 读不带 BOM 的 UTF-8 会按本地代码页解，
      //   路径里的中文会解坏 —— 那正是"装完打不开"的一种。
      fs.writeFileSync(ps, "﻿" + script, "utf8");
      const psq = (s) => "'" + String(s).replace(/'/g, "''") + "'";
      const relay = "Start-Process powershell.exe -ArgumentList @(" + [
        "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
        "-File", ps, "-Installer", p.installer, "-Dir", dir,
        "-Exe", process.execPath,
      ].map(psq).join(",") + ") -WindowStyle Hidden";
      spawn("powershell.exe", ["-NoProfile", "-WindowStyle", "Hidden", "-Command", relay],
            { stdio: "ignore", windowsHide: true }).unref();
    } catch (e) {
      console.error(`[update] 起安装脚本失败：${e && e.message ? e.message : e}`);
    }
    // 给中转 PowerShell 两秒交接（它自己马上就退，真正干活的是被 甩出去 的那个）。
    setTimeout(() => app.exit(0), 2000);
  }, UPDATE_HOLD_MS);
}

//: 随机句的文件 —— 桌面壳的内容，跟着版本走。
//: 路径相对后端根：开发时是仓库根，打包后是 `resources/app`。
//: 打包白名单里必须有这一条（`electron-builder.config.mjs` 的 `BACKEND_TREE`），
//: 否则它在包里静默不存在。
const QUOTES_FILE = path.join(backendRoot(), "apps", "desktop", "quotes.txt");

/** 解析 `quotes.txt`。
 *
 *  格式（"怎么写"写在文件自己头上，那边才是用户看的地方）：
 *  块之间空行隔开，块里 `orig:` / `zh:` / `by:` 三行，顺序随意，`#` 是注释。
 *
 *  键名是 `orig` 不是 `en`：原文可能是俄文、日文、中文
 *，叫 `en` 就是说谎。`orig` = 原文（任何语言）。
 *  `zh` 可省：原文本身就是中文的那几条，写了就是同一句话显示两遍。
 *
 *  坏块只跳过、不作废整个文件，而且要出声（`[quotes] 跳过第 N 块`）：
 *  用户会自己往里加句子，写漏一个字段是常态；静默丢掉那一条，
 *  只会看到"我加的句子怎么不出现"，而终端里一个字都没有。
 *  这里的解析故意极简（只切 `键:值`），不认引号、不认续行 ——
 *  多一条规则就多一种"用户以为能写、其实写不出"的形状。
 */
function parseQuotes(text) {
  const out = [];
  const blocks = String(text).replace(/\r\n/g, "\n").split(/\n\s*\n/);
  blocks.forEach((raw, bi) => {
    const lines = raw.split("\n").map((l) => l.trim())
      .filter((l) => l && !l.startsWith("#"));
    if (!lines.length) return;                       // 纯注释块，正常
    const q = {};
    for (const line of lines) {
      const m = line.match(/^([A-Za-z]+)\s*[:：]\s*(.+)$/);
      if (!m) continue;
      q[m[1].toLowerCase()] = m[2].trim();
    }
    if (q.orig && q.by) out.push({ orig: q.orig, zh: q.zh || "", by: q.by });
    else console.warn(`[quotes] 跳过第 ${bi + 1} 块（缺 orig / by）：`
      + JSON.stringify(q).slice(0, 120));
  });
  return out;
}

function readQuotes() {
  try {
    return parseQuotes(fs.readFileSync(QUOTES_FILE, "utf8"));
  } catch (e) {
    // 读不到不是启动失败 —— 那只是一句话显示不出来，不该拦着用户用应用。
    console.warn(`[quotes] 读不到 ${QUOTES_FILE}（${e.code || e.message}）`
      + " —— 小窗会用内置的那两句");
    return [];
  }
}

//: 界面语言（`data/settings.json` 的 `interface_language`）。
//: 只用来决定启动小窗那句引文要不要带中文译文 —— 非中文用户看中文译文
//:   是纯噪声，所以不翻译、只留原文。
//: 读不到 / 坏 JSON / 没这个键，按中文（源码语言，也是默认值）。
//:   这里不上抛：小窗早于后端起来，为了一个字号问题拦着启动不值得。
//: 走查询串给小窗（`?lang=…`），不走 IPC —— 它是首屏就要用的值，
//:   而 IPC 是异步的，会先按中文画一帧再改，看起来像"闪了一下"。
const SETTINGS_FILE = path.join(dataRoot(), "data", "settings.json");
/** 系统语言标签（`en-US` / `zh-CN` …）。必须在 `whenReady()` 之后调。
 *
 *  读不到就回空串 —— 让调用方走"什么都没有发生过"那条路，别在这里猜。
 */
function systemLocale() {
  try { return String(app.getLocale() || "").toLowerCase(); } catch (e) { return ""; }
}

function readInterfaceLanguage() {
  let saved = "";
  try {
    const raw = JSON.parse(fs.readFileSync(SETTINGS_FILE, "utf8"));
    saved = String(raw.interface_language || "").toLowerCase();
  } catch (e) {
    saved = "";                       // 没设过 / 文件还没有：都一样
  }
  // 没设过 = 跟随系统。
  //    小窗是第一屏，这一刻后端还没起来，所以这里必须自己判一次。
  if (saved && saved !== "system") return saved;
  const tag = systemLocale();
  return tag.startsWith("zh") ? "zh" : (tag ? "en" : "zh");
}

/** 引擎侧那几句文案走哪种语言。
 *
 *  小窗不在 `/api/i18n` 的覆盖范围里 —— 它先于后端起来，读不到翻译接口，
 *  所以静态节点在 `splash.html` 的 `T_EN` 里翻，而引擎铸的句子
 *  （`#00` / `#03` / `#01` 那几句）只能在这里翻一次。
 *  两处各管一半，都不许再抄对方那一半。
 */
let splashLang = "";
function tr(zh, en) {
  const lang = splashLang || readInterfaceLanguage();
  return String(lang).startsWith("zh") ? zh : en;
}

function openSplash() {
  splash = new BrowserWindow({
    width: SPLASH_W, height: SPLASH_H, show: true, frame: false,
    transparent: true, resizable: false, movable: false, minimizable: false,
    maximizable: false, fullscreenable: false, skipTaskbar: true, alwaysOnTop: true,
    backgroundColor: "#00000000",
    webPreferences: {
      preload: path.join(__dirname, "splash-preload.cjs"),
      nodeIntegration: false, contextIsolation: true, sandbox: true,
      // 必须关掉。Electron 默认 `backgroundThrottling: true`，窗口被判定为
      //    遮挡/非前台时，`requestAnimationFrame` 根本不触发。而小窗里那三件事
      //    （逐帧动画 / 文本推进 / 把就绪画出来）全挂在同一个 rAF 循环上
      //    所以一个原因、三个症状。
      //    为什么"第一次正常、后面不正常"：第一次是用户手动启动的，
      //    小窗一出来就在前台；后面那次常常是刚建出来、还没被系统算作可见。
      //    主窗刻意不关它 —— 主窗收进托盘时就该被节流（省电）。
      backgroundThrottling: false,
    },
  });
  // `?shell=1` 是判据：小窗页面靠它知道"底色要透明、别挂调试条、
  //    别自己跑假的 demo"（见 `splash.html` 顶上那段）。
  // 小窗也要接仪表 —— 它的故障只有渲染进程知道（见 `bridgeRenderer` 的）。
  bridgeRenderer(splash, "splash");
  // 走壳自己的协议（`crannia-app://shell/*`），不再是 `loadFile` 的
  //    `file://` —— 判据有两个：
  //      ① IPC 属主的第三个要素是"自定义协议 URL" —— 页面在 `file://` 上时，
  //         那半边的判据没有东西可判。
  //      ② 与主窗同一个 scheme，少一种"这个页面在哪一层"的分叉。
  //    `?shell=1` 一个字不变（`splash.html` 靠它知道"底色透明、别挂调试条"）——
  //       `loadURL` 保留查询串，`serveWebDocument` 也只吃 `pathname`。
  //: `lang` 决定那句引文要不要带中文译文（见 `readInterfaceLanguage`）。
  //:   走查询串是首屏同步可读的；`serveWebDocument` 只吃 `pathname`，不影响取文件。
  const lang = readInterfaceLanguage();
  splashLang = lang;                 // 引擎那几句（#00 / #03 / #01）跟着它走
  splash.loadURL(`${SCHEME}://shell/splash.html?shell=1&lang=${encodeURIComponent(lang)}`);
  // `-3` 是 `ERR_ABORTED` —— 正常的导航取消（同一页再 load 一次），不是故障。
  //    别的码就是"这一页压根没上来"，而那种情况下连 console 都没有
  //    （脚本还没开始跑），必须在这一层接住。
  splash.webContents.on("did-fail-load", (_e, code, desc, url) => {
    if (code === -3) return;
    console.error(`[splash] 页面没加载上：${code} ${desc} ${url}`);
  });
  // 补发那个"可能来得太早"的状态 —— 见 `tellSplash` 的 。
  splash.webContents.on("did-finish-load", () => {
    if (splashState && splash && !splash.isDestroyed()) {
      splash.webContents.send("splash:state", splashState);
    }
  });
  splash.on("closed", () => {
    splash = null;
    // 用户在小窗里点了「退出」（`window.close()`），整个应用跟着退。
    if (!mainWindow && !startupFailed) app.quit();
  });
}

/** 主 → 小窗的唯一通道。`{ok:true}` 就绪 / `{ok:false, reason}` 失败。
 *
 *  失败可能来得比"小窗加载完"还早 —— python 不存在时 `spawn` 立刻
 *  报错，那时页面还没 `did-finish-load`，`send` 丢在一个没人监听的 webContents 上，
 *  而症状是小窗一直转、永远不报错（比"静默消失"更糟：它假装还在努力）。
 *，状态存一份，`did-finish-load` 时补发（见 `openSplash`）。
 */
function tellSplash(state) {
  splashState = state;
  if (!splash || splash.isDestroyed()) return;
  if (splash.webContents.isLoading()) return;        // 加载完会补发
  splash.webContents.send("splash:state", state);
}

function failSplash(err) {
  startupFailed = true;
  const reason = (err && err.message) ? err.message : String(err);
  console.error(`[splash] 启动失败：${reason}`);
  tellSplash({ ok: false, reason });
}

/** `#00` 与 `#03` 的判据。返回 `"00"` 或 `"03"`。
 *
 *  ## 为什么 `00` 的判据必须窄
 *
 *  `00` 那句话是「存在不可修复的错误，请更换安装包」——
 *  判错的代价是让用户白重装一遍，而重装完还是同样的问题，
 *  体验比现在的"报个错"更差。
 *
 *  所以只认两个硬信号：
 *    · `ENOENT` —— 解释器根本不存在（`spawn` 当场抛）
 *    · stderr 里有 `ModuleNotFoundError` / `No module named` —— 包缺了
 *
 *  其余一律当 `03`（先试着救一次）。多试一次的代价是几秒钟，
 *  判错的代价是一次白折腾。
 */
function classifyFailure(err, diag) {
  const msg = String((err && err.message) || "");
  const text = String(diag || "");
  if (/ENOENT/.test(msg)) return "00";
  if (/ModuleNotFoundError|No module named|ImportError/.test(text)) return "00";
  return "03";
}

/** 用一个独立的 python 进程恢复配置。
 *
 *  ## 为什么不能走 HTTP API
 *
 *  要恢复的场景恰恰是"后端没起来"—— 那时 `/api/*` 一个都调不到。
 *  所以这里 spawn 一个一次性 python，只做 `last_good.restore()` 就退。
 *  它不依赖 Flask、不占端口、失败也不影响主进程。
 */
function restoreConfig() {
  return new Promise((resolve) => {
    let out = "";
    let child;
    try {
      child = spawn(pythonExe(), ["-c",
        "import sys,json; sys.path.insert(0,'.');"
        + "from core import last_good;"
        + "print(json.dumps(last_good.restore(), ensure_ascii=False))",
      ], { cwd: root, env: process.env, windowsHide: true });
    } catch (err) {
      return resolve({ ok: false, reason: String(err && err.message) });
    }
    child.stdout.on("data", (c) => { out += c; });
    child.on("error", (err) => resolve({ ok: false, reason: String(err && err.message) }));
    child.on("exit", () => {
      const last = String(out).trim().split("\n").pop() || "";
      try { resolve(JSON.parse(last)); }
      catch { resolve({ ok: false, reason: "恢复进程没有给出可解析的结果" }); }
    });
  });
}

//: `#03` 那句话至少显示这么久（毫秒）。失败常常是秒级的 —— 不设下限时
//:   它一闪而过，用户只看到最后的 `#00`，看起来像"03 从来没出现过"。
const MIN_03_MS = 2500;

/** 给用户看的那句"到底是什么坏了"。
 *
 *  后端"进程退出了（code=1）"这种话等于没说 —— 真正的原因在它的 stderr 里。
 *  所以把诊断尾巴的最后一行一起带上（端口被占时那一行就是
 *  `PermissionError: [WinError 10013] …`，而"code=1"指不到任何地方）。
 *  已经被 message 包含时不重复。
 */
function _failureDetail(err) {
  const msg = String((err && err.message) || err || "").trim();
  let tail = "";
  try {
    const text = String((backendDiag && backendDiag.text && backendDiag.text()) || "");
    const lines = text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    if (lines.length) tail = lines[lines.length - 1];
  } catch (e) {
    tail = "";
  }
  if (!tail || (msg && msg.indexOf(tail) >= 0)) return msg;
  return msg ? msg + "\n" + tail : tail;
}

/** 启动失败之后：先分类，能救就救一次。`true` = 救活了，可以继续建主窗。
 *
 *  ## 这一层 try/catch 是承重的，不是保险
 *
 *  恢复这条路自己挂掉时，症状不是"报个错"—— 调用方 `whenReady` 的
 *  catch 里 `await recoverOrExplain(err)` 再抛一次，就成了一条没人接的
 *  Promise 拒绝，什么都不显示，小窗永远停在「正在初始化」。
 *
 *  所以它对外承诺"永远不抛"：真出了意外就报 #00，至少让用户看得见。
 */
async function recoverOrExplain(err) {
  try {
    return await _recoverOrExplain(err);
  } catch (e) {
    console.error(`[startup] 恢复流程自身出错：${e && e.stack ? e.stack : e}`);
    try {
      failSplash(new Error("#00 应用程序异常 —— 存在不可修复的错误，请更换安装包"));
    } catch (e2) {
      // 小窗可能已经没了。终端那行已经落过，不能连它也一起丢。
    }
    return false;
  }
}

async function _recoverOrExplain(err) {
  // `backendDiag` 是 `createDiagnostics()` 造的对象（`push` / `text` / `dropped`），
  //   不是数组。写 `.join("")` 会当场抛，而这条支路只在启动失败时才走。
  //   读者 `test_desktop_lifecycle` 按它的真 API 反查。
  const kind = classifyFailure(err, backendDiag.text());
  console.error(`[startup] 失败归类 ${kind} —— ${err && err.message ? err.message : err}`);

  // 文案后面接上真实那句。原先只有一句"请更换安装包"，
  //   而端口被占、后端进程退码这些都会走到这里 —— 那句会把人指到错的方向。
  //   真实原因不替代那句话，只是跟在下面一行（小窗把它当正文显示）。
  const raw = _failureDetail(err);

  if (kind === "00") {
    failSplash(new Error(
      tr("#00 应用程序异常 —— 存在不可修复的错误，请更换安装包",
         "#00 Application error — unrecoverable. Please reinstall the app.")
      + (raw ? "\n" + raw : "")));
    return false;
  }

  // `03`：正计时塞进 `reason` —— 小窗本来就会显示这一串，不必改页面。
  //   每秒一次 IPC 只在"已经出事"的那条路上，正常启动一个字节都不多。
  const t0 = Date.now();
  const showElapsed = () => {
    const secs = Math.round((Date.now() - t0) / 1000);
    return tellSplash({
      ok: false,
      reason: tr(
        "#03 应用程序异常，我们正在尝试解决，请稍后（" + secs + "s）",
        "#03 Application error — trying to fix it, please wait (" + secs + "s)"),
    });
  };
  showElapsed();
  const tick = setInterval(showElapsed, 1000);

  try {
    const r = await restoreConfig();
    console.error(`[startup] 配置恢复：${JSON.stringify(r)}`);
    try { await stopBackend(); } catch (e) { /* 它本来就没起来，正常 */ }
    await startBackend();
    await startFramework();
    clearInterval(tick);
    console.error("[startup] 恢复后启动成功");
    return true;
  } catch (err2) {
    // `#03` 必须被人看见一次。
    //   后端进程一退，`startBackend` 就 reject —— 整段恢复可能只有几百毫秒，
    //   那句话一闪而过，用户只记住最后的 `#00`，看起来像 03 根本没出现。
    //   下限只管失败这条支路：救活了就赶紧把主窗给他，不在这里拖。
    const left = MIN_03_MS - (Date.now() - t0);
    if (left > 0) await new Promise((r) => setTimeout(r, left));
    clearInterval(tick);
    console.error(`[startup] 恢复后仍然失败：${err2 && err2.message ? err2.message : err2}`);
    const raw2 = _failureDetail(err2);
    failSplash(new Error(
      tr("#00 应用程序异常 —— 存在不可修复的错误，请更换安装包",
         "#00 Application error — unrecoverable. Please reinstall the app.")
      + (raw2 ? "\n" + raw2 : "")));
    return false;
  }
}

/** IPC 的来客必须验过身份才许办事。
 *
 *  不验来客的 IPC 处理器，任何渲染进程都能喊「退出应用」。
 *
 *  身份由三要素同时成立来判定：同一个 `webContents` 对象（对象同一性，
 *  不是比 URL 字符串）· 它那个 `mainFrame` 对象 · 自定义协议 URL。
 *，判据与"为什么是三个 `===` 而不是一句 `startsWith`"全在
 *  `apps/desktop/ipc-guard.cjs`（那个文件不 import electron，真 node 跑得起来）。
 */
function senderRejection(event, win, hosts) {
  return guard.senderRejection(event, win, hosts, SCHEME);
}

/** 拒了要出声，但只出声一次一人 —— 这条闸本该永远不响，响了就是有人在敲门。 */
function rejectForeign(event, win, hosts, tag) {
  const why = senderRejection(event, win, hosts);
  if (why) {
    console.warn(`[ipc] 拒了 ${tag} 的来客：${why}`);
    return true;
  }
  return false;
}

//: 小窗上那两个按钮 —— 渲染进程 → 主进程这一向走 IPC + preload。
//: 与 `tellSplash` 那一向不对称是有意的：那一向只要"能喊话"，
//:    `webContents.send` 就够；这一向是用户的动作，要有个能点的东西
//:    接回主进程（`executeJavaScript` 是单向的，做不了这个）。
//: 四道闸的次序一律是「先问、后动手」 —— `app.quit()` / `app.relaunch()`
//:    就是这里的"副作用"，而拒绝必须发生在它们之前。
ipcMain.on("splash:quit", (event) => {
  if (rejectForeign(event, splash, ["shell"], "splash:quit")) return;
  quitting = true; app.quit();
});
ipcMain.on("splash:retry", (event) => {
  if (rejectForeign(event, splash, ["shell"], "splash:retry")) return;
  // 先收尾、再重启 —— 不要写成裸的 `app.relaunch(); app.exit(0);`：
  //    `app.exit()` 跳过 `will-quit`，后端子进程变成孤儿，
  //    新起的那一份会撞端口（它绑不上 5000，于是新壳也起不来，用户看到的
  //    是"重试按了没反应"，而两次失败的原因完全不同）。
  quitting = true;
  stopBackend()
    .catch((err) => console.error(`[retry] 收尾时出错（照常重启）：${err && err.message ? err.message : err}`))
    .finally(() => { app.relaunch(); app.exit(0); });
});
//: 启动轨迹（见 `splash-preload.cjs` 的 `boot`）—— 只打日志，什么都不改。
ipcMain.on("splash:boot", (event, tag) => {
  if (rejectForeign(event, splash, ["shell"], "splash:boot")) return;
  console.log(`[splash] boot: ${tag}`);
});
//: 小窗来取随机句（拉，不是推 —— 页面自己决定什么时候要，主进程不必知道它加载到哪了）。
ipcMain.handle("splash:quotes", (event) => {
  // 拒的那一支返回空数组（不是抛）：调用方本来就备着"读不到"这条路
  //    （`splash.html` 有内置的兜底两句），抛出去只会把一次越权变成启动时的一屏报错。
  if (rejectForeign(event, splash, ["shell"], "splash:quotes")) return [];
  return readQuotes();
});

// 接管横幅
//
// 接管后屏幕顶层渲染一个横幅「操作接管-esc退出」。
//    它必须同时满足三条 —— 每一条都在这个窗口的选项里：
//      ① 不抢焦点 —— `focusable:false`（等价于 `WS_EX_NOACTIVATE`）：
//         抢走焦点 = 下一条 `SendInput` 打进横幅而不是目标窗口；
//      ② 不被自己的截图拍到 —— `setContentProtection(true)`（Windows 上
//         映射到 `WDA_EXCLUDEFROMCAPTURE`）；
//      ③ 一直可见 —— `alwaysOnTop` 用 `screen-saver` 这一档（盖住普通置顶窗）。
// 它不接收键盘（Esc 由 Python 那边的低级钩子处理 —— 那个才分得清
//    "物理按的"与"agent 注入的"）。这个窗口上的「退出」按钮走 IPC 回后端。
let takeoverOverlay = null;

function setTakeoverOverlay(on) {
  if (!on) {
    if (takeoverOverlay && !takeoverOverlay.isDestroyed()) takeoverOverlay.destroy();
    takeoverOverlay = null;
    return;
  }
  if (takeoverOverlay && !takeoverOverlay.isDestroyed()) return;
  const { workArea } = screen.getPrimaryDisplay();
  const W = 344, H = 46;
  takeoverOverlay = new BrowserWindow({
    width: W, height: H,
    x: Math.round(workArea.x + (workArea.width - W) / 2),
    y: workArea.y + 10,
    frame: false, transparent: true, resizable: false, movable: false,
    minimizable: false, maximizable: false, fullscreenable: false,
    skipTaskbar: true, focusable: false, hasShadow: false, show: false,
    alwaysOnTop: true,
    webPreferences: {
      preload: path.join(__dirname, "overlay-preload.cjs"),
      nodeIntegration: false, contextIsolation: true, sandbox: true,
    },
  });
  takeoverOverlay.setAlwaysOnTop(true, "screen-saver");
  takeoverOverlay.setContentProtection(true);      // WDA_EXCLUDEFROMCAPTURE
  takeoverOverlay.loadURL(`${SCHEME}://overlay/overlay.html`);
  takeoverOverlay.once("ready-to-show", () => {
    if (takeoverOverlay && !takeoverOverlay.isDestroyed()) takeoverOverlay.showInactive();
  });
  takeoverOverlay.on("closed", () => { takeoverOverlay = null; });
}

//: 主题模式要落盘：`lv.theme` 在主窗的 localStorage 里，主进程读不到；
//:   而启动小窗比主窗先开（那时渲染进程还没说话），不落盘的话，
//:   每次启动小窗都只能跟系统走（用户选了浅色、系统是深色，小窗一片黑）。
//:   存在 `userData` 下，不进安装目录（升级不丢，也不碰用户数据根）。
function themeModeFile() {
  return path.join(app.getPath("userData"), "ui-theme.json");
}

function loadThemeMode() {
  try {
    const m = String(JSON.parse(fs.readFileSync(themeModeFile(), "utf8")).mode || "");
    return (m === "light" || m === "dark" || m === "system") ? m : "system";
  } catch {
    return "system";
  }
}

function saveThemeMode(mode) {
  try {
    const dir = path.dirname(themeModeFile());
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(themeModeFile(), JSON.stringify({ mode }), "utf8");
  } catch (err) {
    console.warn(`[theme] 落盘失败：${err && err.message ? err.message : err}`);
  }
}

//: 主题模式（由主窗告诉我们 —— `lv.theme` 在主窗的 localStorage 里，
//:   主进程读不到）。改 `nativeTheme.themeSource` 之后，三处一起跟着走：
//:   主窗的 `prefers-color-scheme`、启动小窗（`shell` origin）、窗口底色。
//:   只收那三个值；别的值当作没收到（不做任何猜测）。
ipcMain.on("theme:mode", (event, mode) => {
  if (rejectForeign(event, mainWindow, ["app"], "theme:mode")) return;
  const m = String(mode || "");
  if (m !== "light" && m !== "dark" && m !== "system") {
    console.warn(`[theme] 不认识的主题模式：${m}`);
    return;
  }
  try {
    nativeTheme.themeSource = m;
    saveThemeMode(m);
    _syncWindowTheme();
  } catch (err) {
    console.warn(`[theme] 设置失败：${err && err.message ? err.message : err}`);
  }
});

ipcMain.on("takeover:overlay", (event, on) => {
  if (rejectForeign(event, mainWindow, ["app"], "takeover:overlay")) return;
  setTakeoverOverlay(!!on);
});

//: 横幅上的「退出」按钮 —— 与前端那个按钮走同一个后端端点（不另开一套语义）。
ipcMain.on("takeover:stop", (event) => {
  if (rejectForeign(event, takeoverOverlay, ["overlay"], "takeover:stop")) return;
  api("POST", "/api/computer-use/disable", 3000).catch((err) => {
    console.error(`[takeover] 停止失败：${err && err.message ? err.message : err}`);
  });
});

//: 接管期间一轮结束，把主窗拉回前台。
//: 用户在别的应用里等结果，回复落在我们窗口里而窗口在后台 —— 看不到。
//: 只由渲染进程在 `task.end` 且接管开启时调用；它不是给模型用的能力。
//: 用户装技能时选一个目录 —— 只回一个绝对路径（空串 = 用户取消了）。
//:   不在渲染进程里拼路径：那里拿不到真实的文件系统语义。
ipcMain.handle("dialog:pick-skill-dir", async (event) => {
  if (rejectForeign(event, mainWindow, ["app"], "dialog:pick-skill-dir")) return "";
  const win = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
  try {
    const r = win
      ? await dialog.showOpenDialog(win, {
          title: "选择技能目录",
          properties: ["openDirectory"],
        })
      : await dialog.showOpenDialog({
          title: "选择技能目录",
          properties: ["openDirectory"],
        });
    if (r.canceled || !r.filePaths || !r.filePaths.length) return "";
    return r.filePaths[0];
  } catch (err) {
    console.warn(`[skills] 选择目录失败：${err && err.message ? err.message : err}`);
    return "";
  }
});

ipcMain.on("window:focus", (event) => {
  if (rejectForeign(event, mainWindow, ["app"], "window:focus")) return;
  if (!mainWindow || mainWindow.isDestroyed()) return;
  try {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.moveTop();
    mainWindow.focus();
  } catch (err) {
    console.warn(`[window] 拉回前台失败：${err && err.message ? err.message : err}`);
  }
  // Edge 全屏播视频时，上面那串只让窗口在任务栏
  //   亮黄闪烁 —— Windows 前台锁（`SetForegroundWindow` 返回 FALSE）。
  //   Electron 没有暴露 AttachThreadInput，让后端再拉一次（它手上有 ctypes）。
  //   仍然失败就只出声，不假装成功。
  api("POST", "/api/window/focus", 3000).then((r) => {
    if (!r || !r.json || r.json.ok) return;
    console.warn(`[window] 后端拉回前台没成功：${r.json.error || "未知原因"}`);
  }).catch((err) => {
    console.warn(`[window] 后端拉回前台失败：${err && err.message ? err.message : err}`);
  });
});

// 后端

/** 用哪个 python 跑后端。
 *
 *  优先项目自己的 `.venv`。PATH 上的 `python` 可能不是这个项目的，
 *  拿它跑 `-m web` 会缺依赖。
 *  这条改动的判据不是"更整洁"，是症状对不上病因：缺依赖时后端起不来，
 *  而表现只是"点了图标没反应"（现在会报在小窗里）。
 *  `CRANNIA_PYTHON` 仍然最优先 —— 那是「自带 Python 运行时」要接的口。
 */
function pythonExe() {
  if (process.env.CRANNIA_PYTHON) return process.env.CRANNIA_PYTHON;
  // 随包分发的那份排在项目 `.venv` 之前。
  //   判据是"两级互不打扰"：打包之后没有 `.venv`（源码与运行时都在 resources 里）
  //   所以只剩这一级；开发时 `resourcesPath` 下根本没有这个目录
  //   所以落到 `.venv` 那一级。两个环境各走各的，不需要 `app.isPackaged` 分支。
  //   产物由 `scripts/prepare_runtime.py` 组装（`.gitignore` 里有它
  //     新克隆的仓库里没有这个目录，得先跑那个脚本）。
  const bundled = process.resourcesPath
    ? path.join(process.resourcesPath, "primary-runtime", "python",
                process.platform === "win32" ? "python.exe" : "python")
    : "";
  if (bundled && fs.existsSync(bundled)) return bundled;
  const venv = path.resolve(__dirname, "../../.venv/Scripts/python.exe");
  return fs.existsSync(venv) ? venv : "python";
}

function startBackend() {
  return new Promise((resolve, reject) => {
    const python = pythonExe();
    const root = backendRoot();
    backend = spawn(python, ["-m", "web"], {
      cwd: root,
      env: {
        ...process.env,
        CRANNIA_DESKTOP: "1",
        CRANNIA_API_PORT: String(API_PORT),
        //: 后端的 stdout/stderr 走管道，壳按 UTF-8 解码（`String(chunk)`）。
        //:   不设这一行时 Python 按控制台代码页写，两边的编码对不上，
        //:   小窗上那行系统异常就是一串方框。
        PYTHONIOENCODING: "utf-8",
        //: 壳生成的令牌（后端照它比对，不再自己铸一个）。
        CRANNIA_LAUNCH_TOKEN: _token,
        //: 系统语言：界面语言"没设过"时后端按它选。
        //:   壳是唯一知道 `app.getLocale()` 的那一侧 —— 后端自己猜（`locale` 模块）
        //:   在 Windows 上给的是 `Chinese (Simplified)_China` 这种不是标签的串。
        CRANNIA_SYSTEM_LOCALE: systemLocale(),
      },
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"]
    });
    // 诊断信息有界（64KB，留尾部）—— 理由：插件的 stderr 里可能混着凭据。
    //   它不替代 `[python]` 那两行实时回显，只是另存一份尾巴给"退不干净"时用。
    backend.stdout.on("data", (chunk) => { backendDiag.push(chunk); process.stdout.write(`[python] ${chunk}`); });
    backend.stderr.on("data", (chunk) => { backendDiag.push(chunk); process.stderr.write(`[python] ${chunk}`); });
    backend.once("error", reject);
    backend.once("exit", (code) => { if (!quitting && code) reject(new Error(`Crannia backend exited: ${code}`)); });
    waitForBackend(resolve, reject, 0);
  });
}

//: 探测时见过「有人应答、但不认我们的令牌」—— 那个端口上坐着别人。
//:   只影响最后那句错误怎么说，不参与判定。
let backendProbeForeign = false;

function waitForBackend(resolve, reject, attempt) {
  // 判据是「我们 spawn 的那个后端起来了」，不是「5000 上有人应答」。
  //
  // 原来探的是 `/ping`，而它不验令牌：机器上已经开着另一个 Crannia
  //   （安装版 `D:\cai\Crannia` 占着 5000）时，它的 `/ping`
  //   也回 200，于是壳以为自己的子进程好了，而子进程其实因 `WinError 10048`
  //   当场退出了；随后壳拿自己的令牌去问那个陌生后端，一路 403，
  //   一路卡到 60 秒超时 —— 用户看到小窗停在「加载」，真相是端口冲突。
  //   `/api/status` 要令牌，只有我们自己那份会认。
  if (backend && backend.exitCode !== null) {
    return reject(new Error(
      `后端进程退出了（code=${backend.exitCode}）—— 常见原因：端口 ${API_PORT} 已被占用`
      + "（例如已安装的 Crannia 正在运行）"));
  }
  const request = http.get(`${API_BASE}/api/status`, {
    timeout: 500,
    headers: { "X-Crannia-Token": _token },
  }, (response) => {
    let body = "";
    response.setEncoding("utf8");
    response.on("data", (c) => { body += c; });
    response.on("end", () => {
      if (response.statusCode !== 200) {
        // 403 = 应答的不是我们的人（端口被占）；连不上 = 还没起来。
        if (response.statusCode === 403) backendProbeForeign = true;
        return retryBackend(resolve, reject, attempt);
      }
      backendReady = true;                 // 令牌认了，转发有地方可去
      resolve();
    });
  });
  request.on("error", () => retryBackend(resolve, reject, attempt));
  request.on("timeout", () => { request.destroy(); retryBackend(resolve, reject, attempt); });
}

function retryBackend(resolve, reject, attempt) {
  if (attempt >= 120) {
    return reject(new Error(backendProbeForeign
      ? `后端 30 秒内没有就绪 —— 127.0.0.1:${API_PORT} 上应答的不是我们启动的后端`
        + "（令牌被拒）；端口可能被另一个进程占用"
      : "后端 30 秒内没有就绪（python -m web）"));
  }
  setTimeout(() => waitForBackend(resolve, reject, attempt + 1), 250);
}

function api(method, urlPath, timeoutMs = 8000) {
  return new Promise((resolve, reject) => {
    const req = http.request(`${API_BASE}${urlPath}`, {
      method, timeout: timeoutMs,
      headers: { "X-Crannia-Token": _token },
    }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (c) => { body += c; });
      res.on("end", () => {
        try { resolve({ status: res.statusCode, json: JSON.parse(body || "{}") }); }
        catch { resolve({ status: res.statusCode, json: {} }); }
      });
    });
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(new Error(`${urlPath} 超时`)); });
    req.end();
  });
}

/** 启动 Agent —— 启动 GUI 就要启动对话。
 *
 *  只有 `CRANNIA_DESKTOP=1` 时只是不开浏览器，框架（Agent）还得用户手点
 *  「启动 Agent」，壳起来了但 Agent 没起，而"加载好"到底指哪一步没人说得清。
 *，小窗要等的是这一步：`/api/start` 发出去、`/api/status` 说
 *  `framework_running: true` 才算好。
 *
 *  `/api/start` 不等它返回 —— 它内部是同步跑 `_init_framework`，
 *     起得慢时那个 POST 会挂很久；我们要的是"它开始了、然后我去问状态"。
 *     （`web/server.py::api_start` 自己有 `_FRAMEWORK_STARTING` 去重闸。）
 */
async function startFramework() {
  api("POST", "/api/start").catch(() => {});          // 发起即可，别等
  const deadline = Date.now() + 60000;
  for (;;) {
    if (backend && backend.exitCode !== null && backend.exitCode !== 0) {
      throw new Error(`后端进程退出了（code=${backend.exitCode}）`);
    }
    let r;
    try { r = await api("GET", "/api/status"); } catch { r = null; }
    if (r && r.status === 200) {
      if (r.json && r.json.framework_running) return;
      if (r.json && r.json.init_error) throw new Error(`Agent 初始化失败：${r.json.init_error}`);
    }
    if (Date.now() > deadline) throw new Error("Agent 60 秒内没有就绪");
    await new Promise((r2) => setTimeout(r2, 300));
  }
}

// 后端子进程的生命周期

/** 退出前问一句「现在退会打断什么」。
 *
 *  判据：2 秒 deadline；超时/失败，按"有任务在跑"处理。
 *  这条 fail-safe 的方向是承重的：反过来写（问不出来，当没事）时，
 *  "后端卡死"与"一切正常"会让壳做出同一个动作，而前者恰恰最该问一句。
 */
function inspectQuit() {
  return life.inspectQuit(() => api("GET", "/api/status").then((r) => r.json), 2000);
}

/** 有活在跑时那一个确认框。返回 true = 真的退。
 *
 *  唯一一处 fail-open 在这里写着：问不出人也不行把人关在里面 ——
 *  弹框本身炸了（无显示环境 / 对话框子系统坏了）时照退。
 *  另一条出路是「再按一次退出」—— `askThenQuit` 那边认它（见 `quitAsking`）。
 */
async function confirmQuit(reason) {
  try {
    const r = await dialog.showMessageBox({
      type: "warning",
      buttons: ["取消", "仍然退出"],
      defaultId: 0,
      cancelId: 0,
      title: "有任务正在跑",
      message: "现在退出会打断它。",
      detail: reason,
    });
    return r.response === 1;
  } catch (err) {
    console.error(`[quit] 问不了用户（${err && err.message ? err.message : err}）—— 照退`);
    return true;
  }
}

/** `before-quit` 那一关：问一句 → （有活时）问用户 → 才真的退。 */
async function askThenQuit() {
  if (quitAsking) {
    // 第二次按「退出」= 不等回答了 —— 有它，这个确认框就永远关不住人。
    console.log("[quit] 再按一次退出 —— 不再等确认");
  } else {
    quitAsking = true;
    try {
      const verdict = await inspectQuit();
      if (verdict.busy) {
        const go = await confirmQuit(verdict.reason);
        if (!go) {
          console.log(`[quit] 用户取消（${verdict.reason}）`);
          quitting = false;          // 取消 = 回到"关窗只是收进托盘"那一路
          return;
        }
        console.log(`[quit] 用户确认（${verdict.reason}）`);
      }
    } finally {
      quitAsking = false;
    }
  }
  quitChecked = true;
  quitting = true;
  app.quit();
}

/** 三级降级停后端 —— 优雅 shutdown → 10s → SIGTERM → 5s → SIGKILL → 5s → 抛。
 *
 *  第一级的载体：我们这边 Python 子进程没有 `process.send`
 *，第一级是 HTTP `POST /api/shutdown`，那边回一个 `ack: true`，
 *  而不是 IPC（`child.send`）。
 *
 *  `requireGraceful`（更新交接专用）：exitCode 0
 *  但没 ack shutdown 也算不干净 —— 没有这个 ack，"自己退了"与"被人杀了"
 *  看起来一模一样（都是 0）。
 */
async function stopBackend({ requireGraceful = false } = {}) {
  if (backendStopped || !backend) return { stop: { ok: true, level: "no-process" }, request: null };
  backendStopped = true;
  let request = null;
  const result = await life.stopChild(backend, {
    graceful: async () => {
      try {
        const r = await api("POST", "/api/shutdown", 3000);
        request = { ack: !!(r && r.json && r.json.ack) };
      } catch (err) {
        request = { ack: false, error: String((err && err.message) || err) };
        throw err;                       // 送不到就往下走一级（`stopChild` 会记一句）
      }
    },
  });
  if (!result.ok) {
    console.error(`[stop] 三级都走完了后端还没退（等了 ${result.waitedMs}ms）`
      + `\n--- 后端诊断（尾部 64KB，丢了 ${backendDiag.dropped()} 字符）---\n` + backendDiag.text());
  } else if (result.level !== "graceful") {
    console.warn(`[stop] 后端是被 ${result.level} 送走的（优雅那一级没成）`
      + ` —— 丢了 ${backendDiag.dropped()} 字符诊断`);
  }
  if (requireGraceful && !life.isCleanExit(result, request)) {
    throw new Error(`后端退得不干净：${result.level}`
      + (request && request.ack ? "" : "、而且没 ack"));
  }
  return { stop: result, request };
}

/** 系统深浅切换时，把已开窗口的底色一起换掉（页面内容由渲染进程自己跟）。 */
function _syncWindowTheme() {
  const bg = nativeTheme.shouldUseDarkColors ? "#121514" : "#f7f9f6";
  for (const w of BrowserWindow.getAllWindows()) {
    try { w.setBackgroundColor(bg); } catch (e) { /* 窗口可能已销毁 */ }
  }
}

nativeTheme.on("updated", _syncWindowTheme);

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440, height: 920, minWidth: 980, minHeight: 680, show: false,
    //: 窗口底色跟着系统深浅走 —— 页面自己会在首屏前打 `data-theme`，
    //:   这一格管的是"页面还没画出来的那一瞬"（写死浅色 = 深色用户先闪一下白）。
    backgroundColor: nativeTheme.shouldUseDarkColors ? "#121514" : "#f7f9f6",
    icon: path.join(__dirname, "../../crannia.ico"),
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      //: 令牌经它进渲染进程（`preload.cjs` 读 `process.argv`）。
      //: 不用查询串：那会进历史/日志，而这里只在本进程内传。
      //: 用字符串拼接而不是模板串：模板里的右花括号会把
      //:   `tests/test_desktop_shell.py` 那个非贪婪正则提前截断
      //:   （主窗那三条断言会假红）。
      additionalArguments: ["--crannia-token=" + _token],
      nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true,
      // 允许无手势自动播放。
      //   判据：那个"兼容性警告"弹窗是框架事件触发的，不是用户点出来的
      //   （"手动切自动"那条能播出来，是因为那次点击本身就是一个用户手势）。
      //   没有这一格，`.play()` 会被自动播放策略拦掉 —— 而且是静默的
      //   （rejection 被吞掉），用户看到一屏看着对、就是没声音的警告。
      autoplayPolicy: "no-user-gesture-required",
    }
  });
  // 不许有兜底值 —— 不要写成
  //   `process.env.VITE_DEV_SERVER_URL || "http://127.0.0.1:5173"`，
  //   否则下面那个 `if (devUrl)` 恒真，`else` 那条（`loadFile(dist)`）
  //   是死代码，构建产物从来没被跑过（`npm run desktop` 只会去连
  //   一个没人在听的 5173，窗口一片白）。
  //   所以判据是只有显式给了才走 dev：`npm run dev` 里那个 vite 分支会设它。
  const devUrl = process.env.VITE_DEV_SERVER_URL || "";
  // dev：渲染进程挂在 vite（5173），走它的同源代理转发 /api /ping /static 到后端。
  // 打包：页面来自 `crannia-app://app`，`/api` 一族由 `handleProtocolRequest`
  //      同源转给后端 —— 两条路都是"页面与它的 fetch 同一个 origin"，
  //      所以后端始终不需要 CORS。
  // 不传 `?api=http://127.0.0.1:<port>`（那是页面在 `file://` 下
  //    跨源直连后端用的；现在同源，`baseUrl` 该是空串）。
  //    `apps/desktop/src/main.jsx` 那边的 `get("api") || ""` 一个字不用改 ——
  //    不传就是空串，正是我们要的那一路。
  if (devUrl) mainWindow.loadURL(devUrl);
  else mainWindow.loadURL(`${SCHEME}://app/`);
  // ── 小窗 → 主窗的交接 ──────────────────────────────────────
  //
  // 交接点选在 `ready-to-show`，不是"Backend ready 那一刻" ——
  //   主窗从 `loadURL` 到画出来还要一两百毫秒；早关小窗的话，中间会露出
  //   一块白屏（那比多等半秒难看得多）。
  // 顺序：先喊「准备就绪」→ 等够 `MIN_HOLD_MS` → 再显示主窗、关小窗。
  //   不等的后果：主窗建得快时那句「准备就绪」一闪而过，等于没有。
  mainWindow.once("ready-to-show", () => {
    if (!splash || splash.isDestroyed()) { mainWindow.show(); return; }
    splashReadyAt = Date.now();
    tellSplash({ ok: true });
    const wait = Math.max(0, MIN_HOLD_MS - (Date.now() - splashReadyAt));
    setTimeout(() => {
      mainWindow.show();
      if (splash && !splash.isDestroyed()) splash.destroy();
      splash = null;
    }, wait);
  });

  bridgeRenderer(mainWindow, "renderer");
  // 右键复制 / 粘贴（只看主窗 —— 小窗没有可编辑的东西，
  //   而它那两句随机句也不值得给它配一个菜单）。
  attachContextMenu(mainWindow);

  // ── 布局探针（`CRANNIA_UI_PROBE=1` 才开）──
  //
  // 判据："该滚的滚、不该长的别长" 是一个读数，不是一句断言。
  // `.shell` 高 == 窗口高（没被内容撑大）· `.conversation` 的 `scrollHeight > clientHeight`
  // （内容装不下、正在滚）—— 两条同时成立才算对。
  // 只在开了环境变量时才跑，且只读（不碰任何状态）。
  // ── 截图钩子（`CRANNIA_UI_SHOT=<png 路径>` 才开）──
  //
  // 判据：纯样式改动，眼睛是唯一可靠的验收工具 —— 这个钩子把目视检查变成自动截图。
  // 延迟可用 `CRANNIA_UI_SHOT_DELAY`（毫秒，默认 15s）调 —— 要在那一屏出现之后截。
  // 只读：`capturePage()` 不碰页面状态。
  if (process.env.CRANNIA_UI_SHOT) {
    const delay = Number(process.env.CRANNIA_UI_SHOT_DELAY || 15000);
    setTimeout(async () => {
      try {
        // `CRANNIA_UI_CLICK=<css 选择器>`：截图之前先点一下 ——
        //   判据："点了按钮才崩" 那类 bug，光截静止画面看不见。
        //   只点一次、且只点第一个匹配（照 `querySelector` 的语义）。
        const sel = process.env.CRANNIA_UI_CLICK;
        if (sel) {
          const clicked = await mainWindow.webContents.executeJavaScript(
            `(() => { const el = document.querySelector(${JSON.stringify(sel)}); if (!el) return false; el.click(); return true; })()`
          );
          console.log(`[shot] click ${sel} -> ${clicked}`);
          await new Promise((r) => setTimeout(r, 900));   // 等 React 渲染完再截
        }
        const img = await mainWindow.webContents.capturePage();
        require("node:fs").writeFileSync(process.env.CRANNIA_UI_SHOT, img.toPNG());
        console.log(`[shot] 已保存 ${process.env.CRANNIA_UI_SHOT}`);
      } catch (err) {
        console.error("[shot] 失败:", err);
      }
    }, delay);
  }

  if (process.env.CRANNIA_UI_PROBE === "1") {
    // 还带一条负对照：量完一次之后，当场把修复撤掉再量一次 ——
    //   两次读数若一样，说明"修好了"是碰巧（验证要先能看见它坏，再谈它好）。
    const probe = `(() => {
      const q = (s) => document.querySelector(s);
      const box = (el) => el ? [el.clientWidth, el.clientHeight, el.scrollHeight] : null;
      // 量底部那一格离侧栏底边还有多远。
      //   设置按钮会随对话列表伸缩而移动 —— 这是布局读数，眼睛看不准，量一下才有数。
      //   不能只量 'fr.bottom <= sr.bottom'（「在不在框里」）：
      //   footer 在框里，但下面可能还有一大片空白，会对着明显坏掉的界面报正常。
      //   所以现在报的是差值（像素）：钉在底部时它 ≈ 侧栏的 padding-bottom（13）。
      const side = q('.sidebar'), foot = q('.sidebar footer');
      const sr = side ? side.getBoundingClientRect() : null;
      const fr = foot ? foot.getBoundingClientRect() : null;
      const read = () => JSON.stringify({
        shell: box(q('.shell')), main: box(q('.main')),
        conv: box(q('.conversation')), body: document.body.scrollHeight,
        side: box(side), foot: box(foot),
        //: null = 没量到（选择器不对 / 还没渲染）—— 不是 0（读不到 ≠ 0）。
        footGap: (sr && fr) ? Math.round(sr.bottom - fr.bottom) : null,
      });
      const withFix = read();
      const undo = document.createElement('style');
      undo.textContent = '.shell{grid-template-rows:auto !important}'
        + '.main{min-height:auto !important}';
      document.head.appendChild(undo);
      const withoutFix = read();
      undo.remove();
      return JSON.stringify({
        win: [window.innerWidth, window.innerHeight],
        msgs: document.querySelectorAll('.message').length,
        withFix: JSON.parse(withFix), withoutFix: JSON.parse(withoutFix),
      });
    })()`;
    setInterval(() => {
      mainWindow.webContents.executeJavaScript(probe)
        .then((r) => console.log(`[probe] ${r}`))
        .catch(() => {});
    }, 8000);
  }
  mainWindow.webContents.setWindowOpenHandler(({ url: externalUrl }) => {
    try { const parsed = new URL(externalUrl); if (["http:", "https:"].includes(parsed.protocol)) shell.openExternal(externalUrl); } catch {}
    return { action: "deny" };
  });
  mainWindow.on("close", (event) => { if (!quitting) { event.preventDefault(); mainWindow.hide(); } });
}

/** 提供最基础的复制 / 粘贴右键菜单。
 *
 *  需求：选择某段文字右键支持复制；发送框右键支持粘贴；
 *  发送框选中右键支持剪切和复制。
 *
 *  Electron 里没有默认右键菜单（Chromium 那套被壳关掉了），不加就是"点了没反应"。
 *
 *  为什么这一条落在主进程、而不是像会话列表那样在 React 里画一个
 *  （那套在 `App.jsx` 的 `ctxMenu`）：
 *
 *  ① 写剪贴板在渲染进程里需要权限、结果不确定 —— 确认按钮是否真的工作
 *     要在真实设备上手动点击，写代码的人做不了。，换一条不需要赌的路：Electron 的
 *     `role:"copy" / "cut" / "paste"` 由浏览器进程执行，
 *     它不经过渲染进程、不问 `navigator.clipboard`、没有权限这一步。
 *  ② 走渲染进程那条路还得给 preload 加一个 IPC 口 —— 而 preload 那份
 *     最小白名单（只放 `pathForFile` 一个）是承重的不变量
 *     （`tests/shell_probe.cjs` 逐条钉着），不该为一个右键菜单去动它。
 *
 *  只在"能编辑"或"有选中文字"时弹 —— 这一条也是承重的：
 *  会话列表那套右键菜单是页面自己画的（`App.jsx` 的 `ctxMenu`）；
 *  这里若无条件弹，就会在它上面再盖一层原生菜单（同一个右键两个菜单）。
 *，判据：`params.isEditable || params.selectionText`，两个都不是，直接返回。
 */
function attachContextMenu(win) {
  win.webContents.on("context-menu", (_e, params) => {
    const p = params || {};
    const sel = String(p.selectionText || "").trim();
    if (!p.isEditable && !sel) return;                 // 见上面那条：让页面自己的菜单活着

    //: `editFlags` 是 Electron 按这一刻的编辑状态算出来的（能不能剪/能不能粘）。
    //: 缺字段时按"能"处理（`!== false`）—— 宁可让用户点了没反应，
    //: 也不要因为壳的某个版本没给这一格而明明能粘却灰着。
    const f = p.editFlags || {};
    const tpl = p.isEditable
      ? [
        { role: "cut", label: "剪切", enabled: f.canCut !== false },
        { role: "copy", label: "复制", enabled: f.canCopy !== false },
        { role: "paste", label: "粘贴", enabled: f.canPaste !== false },
        { type: "separator" },
        { role: "selectAll", label: "全选" },
      ]
      // 非编辑区：只有"复制"这一条（对应"选择某段文字右键支持复制"）。
      : [{ role: "copy", label: "复制" }];
    Menu.buildFromTemplate(tpl).popup({ window: win });
  });
}

function createTray() {
  tray = new Tray(path.join(__dirname, "../../crannia.ico")); tray.setToolTip("Crannia");
  tray.setContextMenu(Menu.buildFromTemplate([{ label: "打开 Crannia", click: () => mainWindow?.show() }, { type: "separator" }, { label: "退出", click: () => { quitting = true; app.quit(); } }]));
  tray.on("click", () => mainWindow?.show());
}
// ── 退出的两关 ──────────────────────────────────────────────────────
//
//   `before-quit`：问一句「现在退会打断什么」—— 有活在跑就问用户。
//   `will-quit`  ：收尾 —— 三级降级把后端子进程停掉，然后才真的走。
//
// 两关都 `preventDefault()` 一次再自己重入（各有一个 flag）——
//    这是 Electron 里做异步收尾的标准形状：同步那一关不许 await。
// 收尾失败（三级都走完它还赖着）也要让人退得出去 —— 只是出声：
//    卡在一个退不掉的窗口里，比"退出时丢了一份诊断"坏得多。
app.on("before-quit", (event) => {
  if (quitChecked) return;
  event.preventDefault();
  askThenQuit();
});
app.on("will-quit", (event) => {
  if (backendStopped) return;
  event.preventDefault();
  stopBackend()
    .then(({ stop }) => {
      console.log(`[stop] 后端已停（${stop.level}，${stop.waitedMs}ms）`);
      app.exit(0);
    })
    .catch((err) => {
      console.error(`[stop] 收尾时出错：${err && err.message ? err.message : err}`);
      app.exit(1);
    });
});
