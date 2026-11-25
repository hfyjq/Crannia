/**
 * Crannia 的打包链。
 *
 *   跑法：`npm run package`（= 先 `npm run build` 再 electron-builder）
 *   只出目录不装安装器：`npm run package -- --dir`（快，验的还是同一套 files/extraResources）
 *
 * ## 三件有意不做的（写在这里，免得下一个人以为是漏了）
 *
 * 1. NSIS 深度定制整个跳过 —— 自绘圆角勾选框 + DWM + 自写解压
 *    是一整个 C++ 项目。默认 target 够用。
 * 2. 回滚不做 —— 坏版本靠发一个更高版本的后继包。
 *    代价如实记：装了坏版本只能等修复包，或手动卸载重装。
 * 3. 签名 / 公证不做 —— 没有证书，打出来的是未签名的包，Windows 上会弹
 *    SmartScreen。更新也不走 electron-updater 的差分机制：版本检查与下载由
 *    `core/updater.py` 读两条静态 `version.json` 完成（见 `THIRD-PARTY.md`
 *    与 `scripts/make_release.py`）。
 *    这一条还有一个必须跟着做的推论：electron-builder 26 的 NSIS
 *    模板会把安装器本体（约 370 MB）复制一份到 `%LOCALAPPDATA%` —— 那是给
 *    electron-updater 做差分基底用的。我们不用 electron-updater，那份拷贝没有读者，
 *    只是安装时多读多写 370 MB、并在用户盘上永久占着。
 *，`npm run package` 会先跑 `scripts/patch-nsis.cjs` 把它删掉；
 *      读者 `tests/test_desktop_package.py::test_the_installer_does_not_copy_itself_into_localappdata`
 *      会在 `npm ci` 冲掉补丁时当场红。
 *
 * ## 两条判据（这一份里最容易写错的地方）
 *
 * Python 那套源码要放 `extraResources`（asar 之外） ——
 *    解释器读不了 asar（它不是一个真目录）。`main.cjs::backendRoot()` 就是按
 *    `process.resourcesPath/app` 去找它的，两边必须对上。
 * 白名单，不是黑名单 —— `extraResources` 的 `filter` 只列要发的那几样。
 *    用排除法迟早漏一个（黑名单式的环境清洗会漏 `FEISHU_APP_ID`）；
 *    而这里漏的代价更大 —— 仓库根目录里有 `.env`（明文凭据）、`data/`（用户的记忆库）、
 *    `dist/`（539MB 的另一个包）。一个都不许进包。
 */

import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { existsSync, readFileSync } from "node:fs";

const here = dirname(fileURLToPath(import.meta.url));
//: 仓根（`apps/desktop/` 往上两级）。
const ROOT = join(here, "..", "..");

/** 后端那一套源码 —— 白名单（见文件头判据 2）。
 *
 *  名单住在 `package-tree.json`：GUI 的 `resources/app` 与 TUI 独立包读同一份
 *  （`scripts/make_tui_package.py`），不许各抄一遍。下面这些逐条理由仍成立：
 *
 *  - `vendor/ripgrep`：`core/rg.py` 的执行层。漏了 = 装出来的包里没有二进制，
 *    `grep` 当场报"找不到 ripgrep"；许可文本与二进制同目录，一起发。
 *  - `locales/en.json`：`core/i18n.available()` 扫安装根下的 `locales/`，
 *    漏了 = 安装版没有英语可选（开发树相对 cwd，一切正常）。
 *  - `THIRD-PARTY*` / `LICENSE` / `NOTICE` / `licenses/` / `sbom.json`：
 *    MIT / LGPL / MPL 的履约材料，必须随发行包走。
 *  - 本机的 `__pycache__` 目录与 `.pyc` 文件：不发。
 *  - `skills/probe_good/`：`create_skill` 的测试夹具，不该出现在用户装到的包里。
 *
 *  白名单，不是黑名单 —— 仓根躺着 `.env`（明文凭据）、`data/`（用户的记忆库）、
 *  `dist/`（另一个包），一个都不许进。
 */
const _TREE = JSON.parse(
  readFileSync(join(here, "package-tree.json"), "utf8"));
const BACKEND_TREE = [
  ..._TREE.dirs.map((d) => `${d}/**/*`),
  ..._TREE.files,
  ..._TREE.exclude.map((p) => `!${p}`),
];

/** 壳自己的那几样 —— 进 asar（`files`）。 */
const SHELL_FILES = [
  //: `main.cjs` 顶层 `require` 的那几个，一个都不能少 —— 它们
  //:   在进程启动的第一秒就要读进来，缺一个就是"双击 exe 弹 JS 错误框"
  //:   （较早一版起 `renderer-gone.cjs` 就是这么漏的：加了文件、
  //:   没人加白名单，而开发时 `electron .` 永远发现不了 —— 那时读的是源码目录）。
  //:   所以机械读者：`tests/test_desktop_package.py::test_every_file_the_shell_opens_is_packaged`。
  "apps/desktop/main.cjs",
  "apps/desktop/preload.cjs",
  "apps/desktop/renderer-gone.cjs",
  "apps/desktop/ipc-guard.cjs",
  "apps/desktop/backend-lifecycle.cjs",
  "apps/desktop/web-document.cjs",
  //: 换装脚本的生成与安装目录判据。同一个坑的第五条：
  //:   加了文件不加白名单，打出来的 exe 双击就弹 `Cannot find module`。
  "apps/desktop/update-apply.cjs",
  "apps/desktop/splash.html",
  "apps/desktop/splash-preload.cjs",
  "apps/desktop/splash-data.js",
  //: 接管横幅那一对 —— 走 `crannia-app://overlay/overlay.html`，同样从
  //:   `__dirname`（= asar 里）读。漏了不崩，只是接管时横幅一片空白（更坏查）。
  "apps/desktop/overlay.html",
  "apps/desktop/overlay-preload.cjs",
  "apps/desktop/index.html",
  //: 图标必须真的在包里：`main.cjs` 里窗口与托盘的图标都取
  //:   `path.join(__dirname, "../../crannia.ico")` —— 打包后那正好是
  //:   asar 根的 `crannia.ico`，所以这一条要的是 asar 根，不是 `apps/desktop/`。
  "crannia.ico",
  //: Linux/macOS 的窗口与托盘图标（`main.cjs::APP_ICON` 按平台选）。
  "crannia.png",
  //: 前端产物（`npm run build` 出的那个）。不在 git 里，打包前必须先 build。
  "apps/desktop/dist/**/*",
  "package.json",
  //: 不许进 `node_modules` —— 前端已经被 `vite build` 打成一个文件了
  //:   （见上面 dist 那两行），运行时的 npm 依赖一个都不需要：
  //:   主进程只 `require` electron + node 内置 + 本目录那几个 `.cjs`。
  //:   electron-builder 默认会把 `package.json` 的 `dependencies` 自动收进来
  //:   （不显式排除就会收进 `lucide-react` 一整棵树）—— 所以这一条必须写。
  "!node_modules/**/*",
];

export default {
  appId: "com.crannia.desktop",
  productName: "Crannia",
  artifactName: "crannia-${version}-${os}-${arch}.${ext}",

  //: 应用图标。用 PNG（512x512）：Linux 的 AppImage 图标工具只认 PNG/SVG，
  //:   而它会同时拿全局 icon 去生成图标集 —— 指 .ico 时 Linux 打包直接失败
  //:   （`Unsupported input format ".ico"`）。
  //:   Windows 的 .ico 由 electron-builder 从这张 PNG 转出来；运行时窗口/托盘
  //:   图标仍走 `main.cjs::APP_ICON`（Windows 读 asar 里那份 `crannia.ico`）。
  icon: join(ROOT, "crannia.png"),

  //: 产物目录不入库（几十上百 MB）—— `.gitignore` 里有它。
  directories: { output: join(ROOT, "apps", "desktop", "release") },
  asar: true,

  //: 用本机已经装好的那份 electron，不去网上再下一遍。
  //:   `npm ci` 之后 `node_modules/electron/dist` 就该在了 —— 根 `package.json`
  //:   的 `postinstall` 负责取它（electron 44 的 npm 包没有自己的
  //:   postinstall；缺了那一步，干净克隆里的 dist 是空的，打包会停在这一行）。
  //:   electron-builder 默认会再从 GitHub 拉一份 —— 代理配错时会以
  //:   `ECONNREFUSED 127.0.0.1:443` 失败，包打到一半就停了。
  //:   不影响正确性：用的是同一个 electron（版本仍读 `package.json` 的
  //:   `devDependencies.electron`），只是换了个取件的地方。
  //:   这一条只在"本机已经有它"时成立 —— 换一台机器先 `npm ci`。
  electronDist: join(ROOT, "node_modules", "electron", "dist"),

  //: 入口在仓根 `package.json` 里是新加的（electron-builder 认它）。
  //:   它同时是"开发时 `electron .` 从哪儿起"的答案 —— 与 `npm run desktop`
  //:   显式传路径那条路不冲突。
  extraMetadata: { main: "apps/desktop/main.cjs" },

  files: SHELL_FILES,

  /** 两条，都必须对得上代码里的路径。 */
  extraResources: [
    //: ① Python 那套源码 -> `resources/app`（`main.cjs::backendRoot()` 按它找）。
    { from: ROOT, to: "app", filter: BACKEND_TREE },
    //: ② 自带运行时 -> `resources/primary-runtime`
    //:    （`main.cjs::pythonExe()` 按它找）。
    //:    它不入库（`.gitignore`），打包前要先跑
    //:    `python scripts/prepare_runtime.py`，否则这一条会静默什么都不加。
    { from: join(ROOT, "apps", "desktop", "runtime", "primary-runtime"),
      to: "primary-runtime" },
  ],

  //: TUI 的入口（2026-11-21）：`bin\crannia.cmd` 是安装器要拷到
  //:   PATH 目录（`%LOCALAPPDATA%\Microsoft\WindowsApps`）的那一份。
  //:   代码在 `resources/app/tui`、解释器在 `resources/primary-runtime` ——
  //:   启动器自己认这个布局（见 `tui/crannia.cmd` 的 A0 段）。
  //:   Linux 那份是 `bin/crannia.sh`（同一个布局，同一个启动器判据）。
  extraFiles: [
    { from: join(ROOT, "tui", "crannia.cmd"), to: "bin/crannia.cmd" },
    { from: join(ROOT, "tui", "crannia.sh"), to: "bin/crannia.sh" },
  ],

  //: 打包前验一眼随包运行时是谁家的（2026-11-23，随 Linux target 进来）。
  //:
  //:   为什么必须有这一关：这台开发机是 Windows + WSL 共享一块盘，
  //:   `prepare_runtime.py` 两个 target 都能组装，产物目录却是同一个 ——
  //:   后组装的会顶掉先组装的。没有这道闸，Windows 安装包会静默带上
  //:   一份 Linux 解释器（或反过来），装机后后端起不来，而离根因隔着一整次打包。
  //:   runtime.json 里那份 `platform` 是组装时按锁写的（不是构建机猜的）。
  //:   文件不在 = 还没跑过 `prepare_runtime.py` —— 维持原状（那条
  //:   extraResources 会静默什么都不加，配置头注释里记着），不在这里加码。
  beforePack: async (context) => {
    const manifest = join(ROOT, "apps", "desktop", "runtime",
                          "primary-runtime", "runtime.json");
    if (!existsSync(manifest)) return;
    let plat = "";
    try {
      plat = String(JSON.parse(readFileSync(manifest, "utf8")).platform || "");
    } catch {
      throw new Error(`runtime.json 读不出来：${manifest} —— 先重跑 prepare_runtime.py`);
    }
    // `context.electronPlatformName` 是这次打包的目标；`process.platform`
    //   是构建机自己的平台。交叉打包时两者不同，要判目标。
    const want = (context && context.electronPlatformName)
      || (process.platform === "win32" ? "win32"
        : process.platform === "darwin" ? "darwin" : "linux");
    if (plat && plat !== want) {
      throw new Error(
        `随包运行时是给 ${plat} 组装的，而这次打包目标平台是 ${want}。\n` +
        `  先跑 .venv/Scripts/python.exe scripts/prepare_runtime.py（win）或 ` +
        `在 Linux 上跑 … --target linux-x64，再打包。`);
    }
  },

  //: 只保留中英两个 Chromium 语言包（2026-11-15）。
  //:   判据：`locales/` 解包后 49 MB，而应用只用到 zh-CN / en-US
  //:   （界面语言是自己的 `locales/*.json`，不是这些 `.pak`）。
  //:   缺的系统语言由 Chromium 回落到 en-US，不改变我们的界面语言选择。
  electronLanguages: ["zh-CN", "en-US"],

  //: 三个平台都留着（当前只验 Windows）。`nsis` 不做任何定制 —— 见文件头判据 1。
  win: { target: ["nsis"] },
  mac: { target: ["dmg", "zip"], category: "public.app-category.productivity" },
  //: Linux 的图标要 PNG（`.ico` 那一份 AppImage 认不了；512×512 是下限），
  //:   与 `crannia.ico` 同一次绘制（`scripts/make_brand_assets.py`，一处定义）。
  linux: {
    target: ["AppImage"],
    category: "Utility",
    icon: join(ROOT, "crannia.png"),
    //: `${arch}` 在 Linux 上是 `x86_64`，而我们的产物命名（与发布链、
    //:   `make_release.py` 的查找规则）统一用 `x64` —— 在这里钉死，
    //:   免得同一版在三个平台上长得不一样。
    artifactName: "crannia-${version}-linux-x64.${ext}",
    //: `.desktop` 文件名与 `desktopName`（package.json）对齐 —— 否则桌面环境
    //:   可能不把运行中的窗口关联到这个条目（任务栏图标/分组对不上）。
    syncDesktopName: true,
  },
  nsis: {
    oneClick: false,
    perMachine: false,
    allowToChangeInstallationDirectory: true,
    //: 应用归档保持 7z；`useZip: true` 在真机上装不上（2026-11-15 实测）。
    //:   现场：1.0.2 安装包跑到一半弹「Failed to decompress files。
    //:   Error opening ZIP file」，而且它已经先把安装目录清空了。
    //:   判据不是"配置写着这个键"，是真装一次；`--check-only` 只看体积，
    //:   看不出解压器打不开归档。要再试这一档，先证明能装、能启动、能卸载，
    //:   再回来把这条注释换掉。
    //:   （原来想买的收益：去掉安装期一遍 63 秒静默复制。代价是包变大
    //:   且装不上 —— 交换不成立。）
    //: 唯一的 NSIS 定制：欢迎页一句话（"安装过程会涉及大量文件操作，进度缓慢时请耐心等候"）
    //:   + 卸载前的数据说明（数据在 `%APPDATA%\Crannia`，卸载不动它）。
    //:   它只换文案，不碰解压 / 复制 / 安装流程 ——
    //:   读者 `test_desktop_package` 钉着"这个 include 里只许有 `customWelcomePage` + `customUnInstall`"。
    //:   两段文案也在 `locales/en.json` 里各留一份（那是应用界面的翻译表）；
    //:     改文案时两处一起改，否则安装器与界面会各说各的。
    //: 多语言（用户）：两段自定义文案在 `installer.nsh` 里是
    //:   `LangString` + `$(…)`。跟随系统语言，不弹语言选择框
    //:   （`displayLanguageSelector: false`）；语言加/减要同时改这里与 `installer.nsh` 的 LangString。
    //:   不要设 `language`：它身兼两职 —— 还被原样拼进 `VIAddVersionKey /LANG=`
    //:   （`NsisTarget.js:388`，那里要的是数字 locale id 1033/2052），
    //:   写 `zh_CN` 会让打包当场失败（`is not a valid language code`）。
    multiLanguageInstaller: true,
    installerLanguages: ["zh_CN", "en_US"],
    displayLanguageSelector: false,
    include: join(ROOT, "apps", "desktop", "installer.nsh"),
  },
};
