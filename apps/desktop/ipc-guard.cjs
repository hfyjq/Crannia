// IPC 属主校验 —— 三要素同时成立才许办事。
//
// 本文件不 `require("electron")` —— 它吃的是鸭子类型的 `event` / `win`，
//    于是 `tests/test_desktop_ipc.py` 能用真的 node 拿假对象把它跑一遍
//    （证据要追到"哪一层被观察到"，源码 grep 不算）。
//
// 判据逐条：
//
//     ① 同一个 `webContents` 对象   —— `event.sender === win.webContents`
//     ② 它那个 `mainFrame` 对象     —— `event.senderFrame === event.sender.mainFrame`
//     ③ 自定义协议 URL              —— `crannia-app://<host>`，且 host 在允许名单里
//
// 三要素是「同时」，不是「任一」 —— 而且比的是对象，不是字符串：
//    只比 `frame.url` 的实现会被一张同名同值但不同对象的页面放行
//    （同形域名字典攻击就是它的变体：`...platform.deepseek.com.evil` 前缀一模一样）。
//    所以所以这里的判据是三个 `===`，一次 `startsWith` / `includes` 都不许有。
//
// 拒绝要拒在"发出之前" —— 本模块只回答"该不该拒"，
//    调用方必须在任何副作用之前问它（副作用断言见 `main.cjs` 各处理器）。
"use strict";

/**
 * 这个 IPC 来客该不该被拒？`null` = 放行，否则返回一句短原因（给日志用）。
 *
 * @param {object}  event   Electron 的 `IpcMainEvent` / `IpcMainInvokeEvent`。
 * @param {object}  win     它应该来自的那个 `BrowserWindow`。
 * @param {string[]} hosts  该窗口允许的 hostname（小窗 = `["shell"]`，主窗 = `["app"]`）。
 * @param {string}  scheme  自定义协议名（`crannia-app`）。
 * @returns {string|null}
 */
function senderRejection(event, win, hosts, scheme) {
  if (!win || win.isDestroyed()) return "窗口不在了";
  if (!event || event.sender !== win.webContents) return "不是我们那个 webContents";
  const frame = event.senderFrame;
  // 服务端 / 已销毁的帧上 `senderFrame` 会是 `null` —— 拒，别拿 `sender` 顶上：
  //    换成 `event.sender.mainFrame` 就等于把"哪个帧"这个判据整个扔掉。
  if (!frame) return "没有 senderFrame";
  // 这一条挡的是子 frame：一个 iframe 里的脚本也能 `ipcRenderer.send`，
  //    而它的 `sender` 与主 frame 是同一个 webContents，头两条会一起放行。
  if (frame !== event.sender.mainFrame) return "不是那个 mainFrame";
  let url;
  try {
    url = new URL(frame.url);
  } catch {
    return "帧的 URL 解析不了";                 // `about:blank` 一族走这里
  }
  if (url.protocol !== `${scheme}:`) return `协议不对（${url.protocol}）`;
  if (!hosts.includes(url.hostname)) return `host 不对（${url.hostname}）`;
  return null;
}

module.exports = { senderRejection };
