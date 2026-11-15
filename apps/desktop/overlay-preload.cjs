// 接管横幅的 preload。
//
// 判据同 `preload.cjs`：最小白名单 —— 只暴露一个具名动作，
//   不给 `ipcRenderer` 裸句柄、不给 `fs`/`require`。
// 频道名与 `main.cjs` 的 `ipcMain.on("takeover:stop", …)` 是同一个字符串；
//   改一处不改另一处 = 按钮点了没反应。
"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("cranniaOverlay", {
  stop: () => ipcRenderer.send("takeover:stop"),
});
