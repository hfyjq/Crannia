// preload 沙箱的**行为读者**（`G15`，2026-10-14；`G6` 补 fail-closed，2026-10-17）——
// 真跑 `preload.cjs`，看它到底暴露了什么、以及**拿不到路径时到底答什么**。
//
// 用法: node tests/shell_probe.cjs apps/desktop/preload.cjs
//
// 为什么不是「读源码 grep」：`contextBridge.exposeInMainWorld(X, {...})` 里那个
//    对象多一个键就是多开一扇门，而 grep 数不出「几个键」、也分不清"注释里提到"与
//    "真暴露了"。这里把 `require('electron')` 换成假的（`Module._load` 注入），
//    把 preload 真跑一遍，捕获 `exposeInMainWorld` 实际收到的东西。
//    判据是跑出来的，不是读出来的（跑过不等于在生产会走的那条路上跑过）。
//
// fail-closed 那一半：光测一种 `webUtils` 不够 ——
//    修前这一档只有"取不到就返回 undefined"一条，而那恰好就是要分开的那两件事
//    塌在一起的地方。所以现在五种假桥各跑一遍（见 `SCENARIOS`），
//    判据逐条对着 `preload.cjs` 抬头那三条（① 桥没装就抛 · ② 桥内抛就照抛 ·
//    ③ 空串给 undefined）。只有 ③ 是"可用但为空"，而它不是 bootstrap 被拒。

const Module = require("module");
const path = require("path");

const target = path.resolve(process.argv[2]);
const targetId = require.resolve(target);

//: 每次换一个假 `webUtils`，把 preload 重新 require 一遍（清缓存）。
//:   `want` 是期望值：`"throw"` / `"undefined"` / 具体路径字符串。
//:   `wantMsg` 只有 `throw` 那几档要 —— 见下面那段。
const SCENARIOS = [
  { id: "empty_string", why: "③ 空串 = 这个文件不是本地文件（官方语义）",
    webUtils: { getPathForFile: () => "" }, want: "undefined" },
  { id: "real_path", why: "正常那一支",
    webUtils: { getPathForFile: () => "C:\\tmp\\a.txt" }, want: "C:\\tmp\\a.txt" },
  { id: "impl_throws", why: "② 桥内抛错 -> 照抛（修前被 catch 吞了）",
    webUtils: { getPathForFile: () => { throw new Error("boom"); } },
    want: "throw", wantMsg: "取路径时出错" },
  { id: "no_function", why: "① webUtils 在、那个函数不在",
    webUtils: {}, want: "throw", wantMsg: "不可用" },
  { id: "no_webUtils", why: "① 整个 webUtils 都不在（Electron < 32 / preload 没跑上）",
    webUtils: undefined, want: "throw", wantMsg: "不可用" },
];

// 为什么 `throw` 那一档还要一个 `wantMsg`（变异当场抓到的）：
//    第一版只判「抛了没有」，于是 ① 与 ② 塌成同一条路——把 ① 那道
//    `if (!BRIDGE_OK) throw` 整段删掉，`no_webUtils` 照样"抛"：`webUtils` 是
//    `undefined`，读属性当场 TypeError，被 ② 那个 catch 接住、重新包成
//    ② 那句话抛出来。两条检查一起绿，而 ① 那道闸已经不在了。
//    所以判据必须是「抛的是哪一句」——①说的是「不可用 / 不存在」，
//    ②说的是「取路径时出错」。这正是本仓库那条：
//    「两个都对」的观测要能分开，否则它证明的是别的东西。

let current = null;
const origLoad = Module._load;
Module._load = function (request) {
  if (request === "electron") {
    return {
      contextBridge: {
        exposeInMainWorld: (name, api) => { current.captured.push({ name, api }); },
      },
      webUtils: current.webUtils,
      // preload 多了 `takeoverOverlay` 这一个具名动作 ——
      //   它用的是 `ipcRenderer.send`，所以假 electron 也得给这一格。
      ipcRenderer: {
        send: (...args) => { current.sent.push(args); },
        // `pickSkillDir` 用的是 `invoke`（要拿回一个路径字符串）。
        invoke: (...args) => {
          current.invoked.push(args);
          return Promise.resolve(current.invokeResult);
        },
      },
    };
  }
  return origLoad.apply(this, arguments);
};

let pass = 0;
const failed = [];
function check(label, ok, note = "") {
  if (ok) { pass++; console.log("  [ok] " + label); return; }
  failed.push(label + (note ? " -- " + note : ""));
  console.log("  [!!] " + label + (note ? "  -- " + note : ""));
}

/** 跑一个场景，返回那个被捕获的桥（顺带把"跑不起来"记成失败）。 */
function runScenario(sc) {
  current = { webUtils: sc.webUtils, captured: [], sent: [],
              invoked: [], invokeResult: sc.invokeResult };
  delete require.cache[targetId];              // ★ 必须清：不清就还在用上一场景的桥
  try {
    require(target);
  } catch (e) {
    check("preload.cjs 跑得起来（假 electron 注入后）[" + sc.id + "]", false, String(e.message));
    return null;
  }
  return current.captured[0] || null;
}

// ── 1. 形状：白名单（任何一个场景都一样，取第一个）───────────────────
const first = runScenario(SCENARIOS[0]);
check("★ 恰好暴露了**一个**桥（不多不少）",
      (current.captured || []).length === 1, "captured=" + current.captured.length);
const bridge = first || {};
check("★ 桥的名字是 `crannia`", bridge.name === "crannia", String(bridge.name));

const keys = Object.keys(bridge.api || {});
// 白名单从 1 个长到 6 个。每加一个键都不是"顺手多开一扇门"：
//    · takeoverOverlay：接管横幅必须由渲染进程告诉主进程"现在开没开"。
//      最小白名单的判据是每个键都说得出为什么，不是"永远只有一个"。
//      新增这一格时同时补了下面那条行为检查（它只能发这一个频道）。
//    · token：壳自己生成、经 argv 注入的启动令牌（`/ping` 不再吐它）。
//      它只暴露一个字符串，不是 `process` 句柄。
//    · setThemeMode：主进程读不到渲染进程的 localStorage，而启动小窗在另一个
//      origin，没有这一格，用户手动选的主题到不了它那里。
//      它只发一个具名频道 + 那三个字符串之一。
//    · pickSkillDir：装技能时选目录，用户点按钮才调，主进程弹系统选择器，
//      只回一个路径字符串。它只 invoke 一个具名频道、只收一个字符串。
check("★★ 白名单**恰好六个**：`pathForFile` + `takeoverOverlay` + `focusWindow` + `token` + `setThemeMode` + `pickSkillDir`（多一个键 = 多开一扇门）",
      keys.length === 6 && keys.includes("pathForFile") && keys.includes("takeoverOverlay")
      && keys.includes("focusWindow") && keys.includes("token")
      && keys.includes("setThemeMode") && keys.includes("pickSkillDir"),
      JSON.stringify(keys));
check("★★ 不给裸句柄：`ipcRenderer` / `fs` / `require` 三样一个都不在",
      !keys.includes("ipcRenderer") && !keys.includes("fs") && !keys.includes("require"),
      JSON.stringify(keys));
check("★ `pathForFile` 是个函数", typeof (bridge.api || {}).pathForFile === "function", "");
check("★ `takeoverOverlay` 是个函数", typeof (bridge.api || {}).takeoverOverlay === "function", "");
if (typeof (bridge.api || {}).takeoverOverlay === "function") {
  bridge.api.takeoverOverlay(true);
  const sent = current.sent || [];
  check("★★ `takeoverOverlay(true)` 只发 `takeover:overlay` 这一个频道、且把布尔原样带过去",
        sent.length === 1 && sent[0][0] === "takeover:overlay" && sent[0][1] === true,
        JSON.stringify(sent));
  // 接管期间一轮结束，主窗拉回前台（只发一个具名频道）。
  bridge.api.focusWindow();
  check("★★ `focusWindow()` 只发 `window:focus` 这一个频道",
        sent.length === 2 && sent[1][0] === "window:focus",
        JSON.stringify(sent));
  // 主题模式：只发一个频道、值原样带过去；认不出的值也不在这里过滤
  //    （拦截在主进程那侧，白名单只保证"只发这一个频道"）。
  if (typeof bridge.api.setThemeMode === "function") {
    bridge.api.setThemeMode("dark");
    check("★★ `setThemeMode('dark')` 只发 `theme:mode` 这一个频道、且把值原样带过去",
          sent.length === 3 && sent[2][0] === "theme:mode" && sent[2][1] === "dark",
          JSON.stringify(sent));
  // 装技能选目录：一个具名频道、回一个字符串（空串 = 用户取消）。
  if (typeof bridge.api.pickSkillDir === "function") {
    bridge.api.pickSkillDir();
    const inv = current.invoked || [];
    check("★★ `pickSkillDir()` 只 invoke `dialog:pick-skill-dir` 这一个频道",
          inv.length === 1 && inv[0][0] === "dialog:pick-skill-dir",
          JSON.stringify(inv));
    check("★ 它不带任何参数（选哪个目录由用户在系统对话框里决定）",
          inv.length === 1 && inv[0].length === 1, JSON.stringify(inv[0] || []));
  } else {
    check("★ `pickSkillDir` 是个函数", false, "缺失");
  }
  } else {
    check("★ `setThemeMode` 是个函数", false, "缺失");
  }
}

// 2. 行为：五个场景，逐条对
//   这一节是 fail-closed 那一半的全部依据 —— 抛与不抛是判据，不是实现细节。
for (const sc of SCENARIOS) {
  const b = runScenario(sc);
  const fn = b && b.api && b.api.pathForFile;
  if (typeof fn !== "function") {
    check("[" + sc.id + "] 拿得到 pathForFile", false, "not a function");
    continue;
  }
  let got;
  let threw = null;
  try {
    got = fn({ name: "a.txt" });
  } catch (e) {
    threw = e;
  }
  if (sc.want === "throw") {
    check("★ [" + sc.id + "] " + sc.why + " -> **抛错**（fail-closed，不是「可用但为空」）",
          threw !== null,
          "没抛，返回了 " + JSON.stringify(got));
    // 抛出来的话要说得出为什么，而且要能分出是哪一档（见 `SCENARIOS` 上面那段）。
    check("  ★ [" + sc.id + "] 抛的是**这一档**那句（含「" + sc.wantMsg + "」）",
          threw !== null && String(threw.message).includes(sc.wantMsg),
          threw ? String(threw.message) : "没抛");
  } else if (sc.want === "undefined") {
    check("★ [" + sc.id + "] " + sc.why + " -> 返回 `undefined`（**调用方该降级的那一支**）",
          threw === null && got === undefined,
          threw ? ("threw: " + threw.message) : JSON.stringify(got));
  } else {
    check("★ [" + sc.id + "] " + sc.why + " -> 原样给出那条路径",
          threw === null && got === sc.want,
          threw ? ("threw: " + threw.message) : JSON.stringify(got));
  }
}

// 3. 负对照：判据自己也得会红（拿"桥被拒"那一档单独再钉一遍）
//   判据同 `preload.cjs` 抬头：桥没装 与 文件没路径 不许同形。
{
  const sc = { id: "neg_control", webUtils: undefined };
  const b = runScenario(sc);
  const fn = b && b.api && b.api.pathForFile;
  let got;
  let threw = null;
  try { got = fn({}); } catch (e) { threw = e; }
  check("★★ 负对照：桥被拒时**不许**返回 `undefined`（那正是「可用但为空」那个形状）",
        threw !== null, "返回了 " + JSON.stringify(got));
  // 而且必须是 ① 那一句，不是被 ② 的 catch 顺手接住的那种"也算抛"。
  check("★★ 负对照：它抛的是 ① 那句（含「不可用」）—— 不许被 ② 顶替",
        threw !== null && String(threw.message).includes("不可用"),
        threw ? String(threw.message) : "没抛");
}

console.log("%d tests: %d passed, %d failed", pass + failed.length, pass, failed.length);
process.exit(failed.length ? 1 : 0);
