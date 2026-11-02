#!/usr/bin/env node
/**
 * 去掉 electron-builder 的「安装器自拷贝」—— 那是 370 MB 的纯浪费。
 *
 * ## 它治什么
 *
 * `app-builder-lib@26` 的 `templates/nsis/include/installer.nsh` 里有这一行：
 *
 *     !insertmacro copyFile "$EXEPATH" "$LOCALAPPDATA\${APP_INSTALLER_STORE_FILE}"
 *
 * 它的用途是给 electron-updater 做差分下载的基底。我们没有自动更新
 * （`electron-builder.config.mjs` 里写着「签名 / 公证 / 自动更新都不做」），
 * 所以这一份拷贝没有任何读者：
 *
 *   · 安装时多读 + 多写约 370 MB（等于把整个安装器再复制一遍）
 *   · `%LOCALAPPDATA%` 里永久留一份 370 MB
 *   · 杀软要对它再扫一遍
 *
 * 上游只在 27.x（还是 alpha）改成了「存在 `resources/app-update.yml` 才拷贝」；
 *   26.15.3 与 26.17.0 都还是无条件 —— 所以只能在这里打补丁。
 *
 * ## 三条判据（每一条都对应一次翻车）
 *
 * 1. 靶子必须唯一：目标行在文件里出现恰好一次才动它。
 *    出现 0 次（上游改了形状）或多次（有新分支）就拒，并且说清楚。
 *    静默跳过 = 补丁以为自己打了、其实没打。
 * 2. 幂等：已经打过（有 `CRANNIA_NSIS_PATCH` 标记）就什么都不做，
 *    退出码 0 —— 这条命令会被 `npm run package` 每次都调。
 * 3. 行尾跟着目标文件走（本仓树是混排的）：
 *    读到的文件用 CRLF 就写回 CRLF，别整篇翻掉。
 *
 * 用法：
 *     node scripts/patch-nsis.cjs              # 给本仓 node_modules 里那份打补丁
 *     node scripts/patch-nsis.cjs --check      # 只检查是否已经是打过补丁的状态
 *     node scripts/patch-nsis.cjs --file X.nsh # 对指定文件操作（测试用）
 */

"use strict";

const fs = require("node:fs");
const path = require("node:path");

/** 上游那一行（逐字）。改模板时先来这里对。 */
const TARGET =
  '!insertmacro copyFile "$EXEPATH" "$LOCALAPPDATA\\${APP_INSTALLER_STORE_FILE}"';

/** 打过补丁的标记 —— 幂等的判据。 */
const MARKER = "# CRANNIA_NSIS_PATCH";

const REPLACEMENT = [
  MARKER + " —— 没有自动更新，安装器自拷贝（约 370 MB）没有读者，跳过。",
  "# 恢复办法：删掉这两行并重跑 `npm ci`；补丁本体在 scripts/patch-nsis.cjs。",
].join("\n");

/**
 * 纯函数：把一段模板文本变成「打过补丁」的版本。
 * @returns {{text: string, changed: boolean, error: string|null}}
 */
function patchText(text) {
  if (text.includes(MARKER)) {
    return { text, changed: false, error: null };
  }
  const hits = text.split(TARGET).length - 1;
  if (hits === 0) {
    return {
      text,
      changed: false,
      error:
        "模板里找不到目标行（" + TARGET + "）—— 上游可能改了形状。" +
        "请重新核对 app-builder-lib 的 installer.nsh，别让这个补丁静默失效。",
    };
  }
  if (hits > 1) {
    return {
      text,
      changed: false,
      error: "目标行出现 " + hits + " 次（应当恰好 1 次）—— 上游加了新分支，先读一遍再改。",
    };
  }
  //: 行尾跟着目标文件走（见文件头判据 3）。
  const eol = text.includes("\r\n") ? "\r\n" : "\n";
  const body = REPLACEMENT.split("\n").join(eol);
  return { text: text.replace(TARGET, body), changed: true, error: null };
}

/** 找到本仓 node_modules 里那份模板（跟着 electron-builder 自己的解析走）。 */
function defaultTemplate() {
  const pkg = require.resolve("app-builder-lib/package.json");
  return path.join(
    path.dirname(pkg), "templates", "nsis", "include", "installer.nsh");
}

function main(argv) {
  const check = argv.includes("--check");
  const i = argv.indexOf("--file");
  const file = i >= 0 ? argv[i + 1] : defaultTemplate();
  if (!file || !fs.existsSync(file)) {
    console.error("[nsis-patch] 找不到模板：" + file);
    return 2;
  }
  const text = fs.readFileSync(file, "utf8");
  const res = patchText(text);
  if (res.error) {
    console.error("[nsis-patch] **拒**：" + res.error);
    return 1;
  }
  if (check) {
    const ok = text.includes(MARKER);
    console.log("[nsis-patch] " + (ok ? "已打过补丁" : "**还没打**") + "：" + file);
    return ok ? 0 : 1;
  }
  if (!res.changed) {
    console.log("[nsis-patch] 已打过补丁，跳过：" + file);
    return 0;
  }
  fs.writeFileSync(file, res.text, "utf8");
  console.log("[nsis-patch] 已去掉安装器自拷贝（约 370 MB）：" + file);
  return 0;
}

if (require.main === module) {
  process.exit(main(process.argv.slice(2)));
}

module.exports = { patchText, TARGET, MARKER };
