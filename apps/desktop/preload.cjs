// preload 沙箱桥 —— 最小白名单 + fail-closed。
//
// 需要的只有一件：
//
//   Electron ≥ 32 移除了渲染进程里的 `File.path`，用户在文件选择器里选中的文件，
//   渲染进程拿不到它的真实路径。而附件方案是「只给路径」
//   所以没有路径这条路就不成立。
//   `webUtils.getPathForFile(file)` 是官方给的替代，且只能在 preload 里经 contextBridge 暴露。
//
// 白名单只放这一个函数 —— 不给 `fs`、不给 `ipcRenderer` 裸句柄。
//   判据：「最小白名单；错的文档里什么都不暴露」。
//
// 桥被拒，抛错，不是"可用但为空"
//
//   反例的形状是 `return webUtils.getPathForFile(file) || undefined;` +
//   `catch { return undefined }` —— `contextBridge` / `webUtils` 没装上时，
//   调用方拿到的 `undefined` 与「这个文件本来就没有路径」一模一样。
//
//   为什么这个区分承重：调用方（`Composer.jsx::toAttachment`）看到空值会
//   降级去内联文本，而那条路如实标着 `kind:"text"`（"未以路径形式提供"）。
//   所以两件事塌成一个值之后，「这台机器的壳坏了」在界面上长得像
//     「用户自己挑了个没有路径的文件」—— 用户会去检查自己的操作，而该查的是壳。
//   所以抛错就是把这两件事分开：界面不说谎；没出声与没跑必须分得开。
//
//   三条判据是分开定的，别塌回去（`shell_probe.cjs` 逐条钉着）：
//
//     ① `webUtils.getPathForFile` 根本不存在，抛。
//        桥没装好（Electron < 32，或 preload 没跑在这个渲染进程上）——
//        此刻不存在任何"取到路径"的可能，而那不是调用方该降级的情形。
//     ② 取的时候它自己抛，照抛（包一句人话）。吞掉它，
//        于是"桥内部坏了"与"这文件没路径"又是一样的值。
//     ③ 它返回空串，返回 `undefined`。这是正确答案，不是坏掉：
//        Electron 的 docstring 逐字 *"an empty string if the file is not a local file"*
//        —— 这就是"这个文件没有路径"，调用方就该降级。
//        三条里只有这一条是"可用但为空"，而它不是 bootstrap 被拒。
//
//   抛出去必须有人接（`Composer.jsx` 那一侧）：判据是
//   安全机制不许妨碍把事做完 —— 桥坏了要看得见，但不许因此
//   让用户连附件都加不上。，那边接住、说一句、照旧降级。

const { contextBridge, ipcRenderer, webUtils } = require("electron");

/** 桥装好了没有 —— 判据只认"那个函数在不在"（整个 `webUtils` 不存在也算没装）。
 *
 *  它在模块加载期算一次、之后不再重算：`contextBridge.exposeInMainWorld`
 *  也只在加载期发生一次，两者说的是同一时刻的事。
 *  在 getter 里每次重算会把"bootstrap 时被拒"悄悄改成"这一次调用时不可用"——
 *  那正是要分开的两件事之一。
 */
const BRIDGE_OK = !!webUtils && typeof webUtils.getPathForFile === "function";

/** 启动令牌 —— 由 `main.cjs` 经 `webPreferences.additionalArguments`
 * 注入本进程（`--crannia-token=…`），这里只读不改。
 *
 *  判据：`/ping` 不再明文回令牌（审计第 2 条），
 *  渲染进程改从这里拿；浏览器里单独调试后端时才用
 *  `CRANNIA_ALLOW_PING_TOKEN=1` 那条退路（见 `web/server.py::ping`）。
 *  只暴露一个字符串，不是 `process` 句柄 —— 最小白名单照旧。
 */
const _ARGV = (typeof process !== "undefined" && Array.isArray(process.argv))
  ? process.argv : [];
const _TOKEN_ARG = _ARGV.find((a) => typeof a === "string"
  && a.startsWith("--crannia-token=")) || "";
const LAUNCH_TOKEN = _TOKEN_ARG ? _TOKEN_ARG.slice("--crannia-token=".length) : "";

contextBridge.exposeInMainWorld("crannia", {
  //: 渲染进程用它发 `X-Crannia-Token`（`api.js::ensureToken` 第一优先）。
  //:   空串 = 没注入（浏览器调试），那边会退到 `/ping`（需要服务端开关）。
  token: LAUNCH_TOKEN,
  pathForFile: (file) => {
    // ① bootstrap 被拒，fail-closed（不许"可用但为空"）。
    if (!BRIDGE_OK) {
      throw new Error(
        "crannia 桌面桥不可用：electron.webUtils.getPathForFile 不存在"
        + "（Electron < 32，或 preload 没跑在这个渲染进程上）");
    }
    let p;
    try {
      p = webUtils.getPathForFile(file);
    } catch (err) {
      // ② 桥内抛错，照抛（不能把原因吞掉，否则与"没有路径"同形）。
      throw new Error("crannia 桌面桥取路径时出错："
        + ((err && err.message) || String(err)));
    }
    // ③ 空串 = 这个文件本来就不是本地文件（官方语义），老实说"没有"。
    return p || undefined;
  },
  // 接管期间那条顶层横幅的开关。
  //   判据仍是最小白名单：只给这一个具名动作，不给 `ipcRenderer` 裸句柄。
  //   横幅的"不抢焦点 / 不被截图拍到"在 `main.cjs` 建窗时做 —— 这里只传一个布尔。
  takeoverOverlay: (on) => ipcRenderer.send("takeover:overlay", !!on),
  // 接管期间一轮结束，把主窗拉回前台。
  //   与上面同一个判据：只给具名动作，不给 `ipcRenderer` 裸句柄。
  focusWindow: () => ipcRenderer.send("window:focus"),
  // 主题模式（浅色 / 深色 / 跟随系统）—— 主进程据此设
  //   `nativeTheme.themeSource`：窗口底色、以及启动小窗（另一个 origin、
  //   读不到这里的 localStorage）都跟着用户的显式选择走。
  //   同一条最小白名单：只给这个具名动作，不收任意字符串以外的参数。
  setThemeMode: (mode) => ipcRenderer.send("theme:mode", String(mode || "")),
  // 装技能时选目录：只回一个绝对路径（空串 = 用户取消）。
  //   与上面同一条最小白名单：只给具名动作，不给 `ipcRenderer` 裸句柄。
  pickSkillDir: () => ipcRenderer.invoke("dialog:pick-skill-dir"),
});
