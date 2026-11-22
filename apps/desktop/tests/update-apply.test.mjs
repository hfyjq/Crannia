// 换装脚本的纯逻辑（`apps/desktop/update-apply.cjs`）—— 零依赖，node 直接跑。
//
// 钉住四件事（2026-11-22 真机现场）：
//   · 含空格的安装目录以字面量进脚本（不再经 `Start-Process -ArgumentList` 传参）；
//   · 安装目录不安全（里面没有 Crannia.exe / 名字是同级目录名的前缀）就判不装；
//   · 收尾拉起的路径 = `<安装目录>\Crannia.exe`，失败回退到原来的 exe；
//   · 日志一律 UTF-8（此前的 ANSI 让异常原文在日志里成了乱码）。
//
// 输出一律 ASCII（GBK 控制台；一 print 中文就崩）。

import assert from "node:assert";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const U = require("../update-apply.cjs");

let pass = 0;
const failed = [];
function check(label, cond) {
  if (cond) { pass += 1; console.log("  [ok]   " + label); }
  else { failed.push(label); console.log("  [!!]   " + label); }
}

// ── (1) PowerShell 字面量引号 ──────────────────────────────────
check("psQuote keeps spaces verbatim",
  U.psQuote("C:\\Program Files\\Crannia") === "'C:\\Program Files\\Crannia'", "");
check("psQuote doubles single quotes",
  U.psQuote("O'Brien") === "'O''Brien'", "");

// ── (2) 收尾拉起的路径 ─────────────────────────────────────────
check("relaunch path is <dir>\\Crannia.exe",
  U.relaunchExePath("C:\\Program Files\\Crannia")
    === "C:\\Program Files\\Crannia\\Crannia.exe", "");

// ── (3) 安装目录判据（真实文件系统，临时树）────────────────────
const root = fs.mkdtempSync(path.join(os.tmpdir(), "lv-update-"));
try {
  const good = path.join(root, "Crannia");
  fs.mkdirSync(good);
  fs.writeFileSync(path.join(good, "Crannia.exe"), "");
  check("a normal install dir is accepted", U.checkInstallDir(good).ok === true, "");

  const noExe = path.join(root, "NoExe");
  fs.mkdirSync(noExe);
  check("dir without Crannia.exe is rejected", U.checkInstallDir(noExe).ok === false, "");

  //: 事故的形状：`C:\Program` 是 `C:\Program Files` 的前缀。
  const prog = path.join(root, "Program");
  fs.mkdirSync(prog);
  fs.writeFileSync(path.join(prog, "Crannia.exe"), "");
  fs.mkdirSync(path.join(root, "Program Files"));
  const r = U.checkInstallDir(prog);
  check("dir that is a prefix of a sibling is rejected",
    r.ok === false && r.reason.indexOf("Program Files") >= 0, r.reason);

  check("missing dir is rejected",
    U.checkInstallDir(path.join(root, "nope")).ok === false, "");
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}

// ── (4) 生成的脚本：路径是字面量，日志 UTF-8 ───────────────────
const dir = "C:\\Program Files\\Crannia";
const script = U.buildApplyScript({
  installer: "C:\\Users\\A B\\AppData\\Roaming\\Crannia\\data\\updates\\crannia-1.1.1.exe",
  dir,
  exe: U.relaunchExePath(dir),
  fallbackExe: "C:\\Program Files\\Crannia\\Crannia.exe",
  logFile: "C:\\Users\\A B\\AppData\\Roaming\\Crannia\\data\\updates\\update.log",
  markerFile: "C:\\Users\\A B\\AppData\\Roaming\\Crannia\\data\\updates\\pending.json",
});
check("install dir embedded as a full literal",
  script.indexOf("'C:\\Program Files\\Crannia'") >= 0, "");
check("no param() block -- paths do not travel as arguments",
  script.indexOf("param($") < 0, "");
check("/D= gets the full dir (no quoting: NSIS rule)",
  script.indexOf("-ArgumentList ('/S /D=' + $dir)") >= 0, "");
//: 判据是"每一条 Add-Content 都带 -Encoding UTF8"，不是"出现若干次" ——
//: 日志统一走 `Note` 时只会有一处，钉次数会把好形状判成坏形状。
const addCount = (script.match(/Add-Content/g) || []).length;
const utf8Count = (script.match(/-Encoding UTF8/g) || []).length;
check("every Add-Content is UTF-8",
  addCount > 0 && addCount === utf8Count, `add=${addCount} utf8=${utf8Count}`);
check("success branch relaunches the installed exe",
  script.indexOf("Start-Process -FilePath $exe") >= 0, "");
check("failure branches relaunch the previous exe",
  script.indexOf("Start-Process -FilePath $fallback") >= 0, "");
check("pending marker removed only after a landed install",
  script.indexOf("Remove-Item -LiteralPath $marker") > script.indexOf("Test-Path -LiteralPath $exe"),
  "");

// ── (5) relay：只传脚本路径，且自己加好引号 ────────────────────
const relay = U.buildRelayCommand(
  "C:\\Users\\A B\\AppData\\Local\\Temp\\crannia_apply_update.ps1");
check("relay double-quotes the -File path",
  relay.indexOf('-File "C:\\Users\\A B\\AppData\\Local\\Temp\\crannia_apply_update.ps1"') >= 0, "");
check("relay passes no install dir / exe as separate args",
  relay.indexOf("-Dir") < 0 && relay.indexOf("-Installer") < 0, "");

// ── (6) 壳真的改用了它（而不是把老写法留着）────────────────────
const mainSrc = fs.readFileSync(new URL("../main.cjs", import.meta.url), "utf8");
check("main.cjs requires update-apply.cjs",
  mainSrc.indexOf('require("./update-apply.cjs")') >= 0, "");
check("main.cjs no longer passes dir via -ArgumentList",
  mainSrc.indexOf("'-Dir', dir") < 0, "");
check("main.cjs checks the install dir before installing",
  mainSrc.indexOf("checkInstallDir(dir)") >= 0, "");

if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(`${pass + failed.length} tests: ${pass} passed, ${failed.length} failed`);
assert.equal(failed.length, 0);
