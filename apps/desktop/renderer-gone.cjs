/** 渲染进程没了的时候，在窗口里说人话。
 *
 * 为什么单独一个文件：与 `apps/desktop/ipc-guard.cjs` 同样的理由 ——
 *   那个文件不 import electron，真 node 跑得起来，文案与判据可以被
 *   机械读者钉住（`tests/test_renderer_gone.py`），不必为它起一个 Electron。
 *
 * ## 这里为什么必须有话说
 *
 * `render-process-gone` 如果只 `console.error` 到终端（`main.cjs::bridgeRenderer`）
 * —— 用户看到的是一片白，得自己去翻终端才知道出了事。
 * 那违反三条既有判据：启动失败要在小窗里报，不许静默消失；
 * 「没出声」与「没跑」必须分得开；界面在说谎。
 *
 * `reason` 是这句话的骨头 —— `launch-failed` 与 `crashed` 在屏幕上长得
 * 一模一样（都是白屏），但事情完全不同：
 *
 *   · `launch-failed` = 渲染进程压根没启动起来（官方文档逐字
 *     *"Process never successfully launched"*；而且此时 `exitCode`
 *     不是进程退出码，是平台侧的「启动失败」码）
 *，页面 / JS / 任何前端代码都还没轮到被执行，它不是前端 bug，
 *       去改 `App.jsx` / `dist/` 是白费功夫；
 *   · `crashed` / `oom` / `killed` = 起来过又没了。
 *
 *，文案里必须把这条点破，否则用户（和下一个维护者）会朝错的方向查
 *   —— 第一反应可能是「是不是刚改的 `App.jsx`」，而这行读数能把疑心洗掉。
 *   判据：别让下一个人重走那一段。
 */

/** `render-process-gone` 的 `details` → 窗口里那个框的三句话。
 *
 *  喂什么都别炸 —— 这个函数的调用点在一个已经出事的路径上
 *  （渲染进程刚没），它自己抛异常等于把「报错」变成「又一层报错」。
 *
 *  @param {unknown} details  Electron 给的 `details`（可能是 `undefined`）
 *  @returns {{title: string, message: string, detail: string}}
 */
function noticeText(details) {
  const d = (details && typeof details === "object") ? details : {};
  const reason = d.reason ? String(d.reason) : "(没给)";
  const exitCode = (d.exitCode === undefined || d.exitCode === null)
    ? "(没给)" : String(d.exitCode);

  // 只有这一档要在界面上把方向掰正（见文件抬头那条判据）。
  const launchFailed = reason === "launch-failed";

  const message = launchFailed
    ? "界面没能启动起来 —— 这一片白不是页面坏了"
    : "界面刚刚没了";

  const detailLines = [
    "原因：" + reason + "（exitCode " + exitCode + "）",
    "",
  ];
  if (launchFailed) {
    detailLines.push(
      "这一类是「渲染进程压根没启动起来」—— 页面、JS、前端代码一个字都还没轮到执行，",
      "所以【不是】App.jsx / 打包产物的问题，改前端是白费功夫。",
      "");
  }
  detailLines.push(
    "先照这个顺序看：",
    "  1. 是不是刚玩过带内核级反作弊的游戏（例如《三角洲行动》的 ACE）？",
    "     它是常驻内核驱动，已知会和显卡驱动抢 WDDM 资源、让子进程起不来。",
    "  2. Windows 是否刚更新过，或「核心隔离 / VBS」是不是被打开了。",
    "  3. 换个时间点再起一次。",
    "",
    "⚠️ 第 1 条目前【只有一个样本、还没排除替代解释】—— 是疑凶，不是结论。",
    "关掉这个框之后，终端里那一行 [renderer gone] {...} 就是原始读数。");

  return {
    title: launchFailed ? "界面没能启动起来" : "界面没了",
    message: message,
    detail: detailLines.join("\n"),
  };
}

module.exports = { noticeText };
