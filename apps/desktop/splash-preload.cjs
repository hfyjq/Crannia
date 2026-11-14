// 启动小窗的 preload。
//
// 为什么这个小窗要 preload，而主窗那个（`preload.cjs`）不能兼用：
//   小窗需要两个方向的通道，而它们的做法不一样：
//
//   · 主 → 小窗（"就绪了" / "失败了"）：`webContents.send` 就够 ——
//     主进程单向喊话，页面不需要任何特权。
//   · 小窗 → 主（用户点了「重试」/「退出」）：必须走 IPC。
//     `executeJavaScript` 是单向的（主进程调页面），做不了这个；
//     而 `window.close()` 只能表达"退出"，表达不了"重试"。
//
// 暴露面就这两个动词 + 一个回调，没有别的。
//    判据：这个窗口上没有任何需要特权的操作 —— 它连后端都不直接连
//    （状态是主进程问出来的、再喂给它的）。
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("shell", {
  /** 主进程喊话：`{ok: true}` 就绪 · `{ok: false, reason:"..."}` 失败。 */
  onState: (cb) => ipcRenderer.on("splash:state", (_e, state) => cb(state)),
  /** 「重试」—— 主进程会 `app.relaunch()` 重启整个应用。 */
  retry: () => ipcRenderer.send("splash:retry"),
  /** 「退出」—— 关掉小窗并结束进程。 */
  quit: () => ipcRenderer.send("splash:quit"),
  /** 启动轨迹：页面每走到一步就报一声，
   *  主进程打到终端（`[splash] boot: <步>`）。
   *  判据：小窗坏掉时画布是全空的，而"空"有好几种死法
   *  （素材没读到 / 画布上下文拿不到 / 脚本死在别处 / 干脆没渲染），
   *  它们在屏幕上长得一模一样，只有轨迹分得开。
   *  它只报"走到哪了"，不含任何数据，也不改变任何行为。 */
  boot: (tag) => ipcRenderer.send("splash:boot", String(tag)),
  /** 取随机句：主进程去读 `apps/desktop/quotes.txt`。
   *  拉，不是推 —— 页面自己决定什么时候要，主进程不必知道它加载到哪一步了。
   *  读不到时返回空数组，不是抛 —— 少一句话不该拦着应用启动；
   *  页面那边有内置的兜底两句（见 `splash.html` 的 `FALLBACK_QUOTES`）。 */
  quotes: () => ipcRenderer.invoke("splash:quotes"),
});
