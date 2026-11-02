// 页面截图器 —— 用真 Chromium 渲一张 PNG。
//   node node_modules/electron/cli.js scripts/shot_splash.cjs <page.html> <out.png> [W] [H]
//   例：node node_modules/electron/cli.js scripts/shot_splash.cjs \
//         apps/desktop/splash.html "UI重构/启动窗-预览5.png" 820 500
//
// 为什么要有它：预览图必须用真渲染器生成 —— 拿 Pillow 复刻 CSS 会
//   算不对文本折行（会误判"被截断"，见 `splash.html` 里 `.orig` 那段）。
//   所以这里直接用真渲染器截：折行、字体回退、圆角、阴影全是真的。
// 页面若是 `splash.html`：截图前喊一次 `window.__ready()`（跳到最后一帧 + 落到
//   「准备就绪」），并把预览用的调试条藏掉。别的页面没有 `__ready` 也不影响。
// 输出路径由调用方给，脚本自己不写死目录（运行数据不进源码目录）。
// 控制台只打 ASCII —— GBK 终端上打中文会乱码。
const { app, BrowserWindow, nativeTheme } = require("electron");
const fs = require("fs");
const path = require("path");

const page = path.resolve(process.argv[2] || "apps/desktop/splash.html");
const out = path.resolve(process.argv[3] || "splash.png");
const W = parseInt(process.argv[4] || "820", 10);
const H = parseInt(process.argv[5] || "500", 10);

// `SHOT_DARK=1` 时先把系统主题钉成深色再建窗（页面读 `prefers-color-scheme`）。
if (process.env.SHOT_DARK === "1") nativeTheme.themeSource = "dark";
app.disableHardwareAcceleration();
app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: W, height: H, show: false, frame: false, backgroundColor: "#e9ebe6",
    webPreferences: { offscreen: true },
  });
  await win.loadFile(page);
  await new Promise((r) => setTimeout(r, 800)); // 等首帧 + 随机句从主进程拉回来
  await win.webContents.executeJavaScript(
    "document.querySelector('.dbg')&&(document.querySelector('.dbg').style.display='none');" +
    "(typeof window.__ready==='function')?window.__ready():'no-__ready'"
  ).catch((e) => console.error("[shot] pre-step skipped:", e.message));
  await new Promise((r) => setTimeout(r, 400));
  const img = await win.webContents.capturePage();
  fs.writeFileSync(out, img.toPNG());
  console.log("[shot] wrote " + out);
  app.quit();
});
