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

const here = dirname(fileURLToPath(import.meta.url));
//: 仓根（`apps/desktop/` 往上两级）。
const ROOT = join(here, "..", "..");

/** 后端那一套源码 —— 白名单（见文件头判据 2）。 */
const BACKEND_TREE = [
  "core/**/*",
  "bus/**/*",
  "providers/**/*",
  "channels/**/*",
  "web/**/*",
  "mcpx/**/*",
  "skills/**/*",
  "creators/**/*",
  //: 随包的 ripgrep（`core/rg.py` 用它做 `grep` 的执行层）。
  //:   判据：用户装一次就该齐活 —— 运行期绝不下载、也不要他另装。
  //:   漏了这一行 = 装出来的包里没有二进制，`grep` 当场报"找不到 ripgrep"。
  //:   许可文本（`LICENSE-MIT.txt` / `UNLICENSE.txt` / `COPYING.txt` /
  //:   `NOTICE-vscode-ripgrep.txt`）与二进制同一个目录，一起发。
  "vendor/ripgrep/**/*",
  "config.yaml",
  "apps/desktop/quotes.txt",
  //: 顶层那两个入口与依赖表：`main.py` / `crannia_tray.py` 是别的跑法用的，
  //:   发出去不占地方，而"包里少了它"是那种只有真跑才发现的问题。
  "main.py",
  "crannia_tray.py",
  "requirements.txt",
  //: 第三方署名必须跟着发行包走。
  //:   判据：`web/static/img/provider-*.svg` 是 LobeHub 的 MIT 产物，
  //:   而 MIT 唯一的要求就是"保留版权声明与许可文本" —— 漏了白名单
  //:   = 发出去的副本没有署名，等于没署。见 `THIRD-PARTY.md`。
  "THIRD-PARTY.md",
  //: 项目自身的许可：`LICENSE` + `NOTICE` 必须随安装包走。
  "LICENSE",
  "NOTICE",
  //: LGPL / MPL 的完整许可文本 ——
  //:   `python-telegram-bot` / `pystray` / `py7zr` 系是 LGPL，`certifi` / `tqdm` /
  //:   `orjson` 是 MPL-2.0。判据：保留声明 + 许可文本是履约的最低要求；
  //:   这些库本身也各自带着 `dist-info/licenses/`，这里是人一眼能找得到的那份。
  "licenses/**/*",
  //: 全量 SBOM：Python 包清单 + 前端闭包清单（含逐包许可原文）+
  //:   CycloneDX JSON。它们由 `scripts/audit_licenses.py --all` 生成，
  //:   是随包证据，不是开发文档。
  "THIRD-PARTY-PYTHON.md",
  "THIRD-PARTY-FRONTEND.md",
  "sbom.json",
  //: 产物目录里会有 `.pyc` / `__pycache__`（本机的），别发。
  "!**/__pycache__/**",
  "!**/*.pyc",
  //: 测试 / 演示用的探针 skill 不进发行包 —— 它们是仓库里的测试夹具
  //:   （`probe_good` 是 `create_skill` 的体检样本；`weather_probe` 已于删除）。
  //:   用户装到的包里不该出现"我们的测试残留"。
  "!skills/probe_good/**",
];

/** 壳自己的那几样 —— 进 asar（`files`）。 */
const SHELL_FILES = [
  //: `main.cjs` 顶层 `require` 的那几个，一个都不能少 —— 它们
  //:   在进程启动的第一秒就要读进来，缺一个就是"双击 exe 弹 JS 错误框"
  //:   （`v0.11.519` 起 `renderer-gone.cjs` 就是这么漏的：加了文件、
  //:   没人加白名单，而开发时 `electron .` 永远发现不了 —— 那时读的是源码目录）。
  //:   所以机械读者：`tests/test_desktop_package.py::test_every_file_the_shell_opens_is_packaged`。
  "apps/desktop/main.cjs",
  "apps/desktop/preload.cjs",
  "apps/desktop/renderer-gone.cjs",
  "apps/desktop/ipc-guard.cjs",
  "apps/desktop/backend-lifecycle.cjs",
  "apps/desktop/web-document.cjs",
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

  //: 应用图标。不写这一条时，构建日志会出现
  //:   `default Electron icon is used`（装出来的 exe 是 Electron 默认图标）。
  icon: join(ROOT, "crannia.ico"),

  //: 产物目录不入库（几十上百 MB）—— `.gitignore` 里有它。
  directories: { output: join(ROOT, "apps", "desktop", "release") },
  asar: true,

  //: 用本机已经装好的那份 electron，不去网上再下一遍。
  //:   `npm install` 的时候 `node_modules/electron/dist` 就已经下好了，而
  //:   electron-builder 默认会再从 GitHub 拉一份 —— 代理配错时会以
  //:   `ECONNREFUSED 127.0.0.1:443` 失败，包打到一半就停了。
  //:   不影响正确性：用的是同一个 electron（版本仍读 `package.json` 的
  //:   `devDependencies.electron`），只是换了个取件的地方。
  //:   这一条只在"本机已经有它"时成立 —— 换一台机器先 `npm install`。
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

  //: 三个平台都留着（当前只验 Windows）。`nsis` 不做任何定制 —— 见文件头判据 1。
  win: { target: ["nsis"] },
  mac: { target: ["dmg", "zip"], category: "public.app-category.productivity" },
  linux: { target: ["AppImage"], category: "Utility" },
  nsis: {
    oneClick: false,
    perMachine: false,
    allowToChangeInstallationDirectory: true,
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
