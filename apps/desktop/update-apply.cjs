#!/usr/bin/env node
/**
 * 换装（apply pending update）的纯逻辑 —— 只产出文本与判定，不碰 Electron、不起进程。
 *
 * ## 它治什么（2026-11-22 真机现场）
 *
 * 换装脚本原先用
 *   `Start-Process powershell.exe -ArgumentList @(…, '-Dir', 'C:\Program Files\Crannia', …)`
 * 把安装目录交给子进程 —— 而 `-ArgumentList` 不给含空格的元素加引号：
 * 子进程收到 `-Dir C:\Program`，`Files\Crannia` 成了没人接收的多余参数。
 * 于是安装包被喂了 `/S /D=C:\Program`（装到盘根），收尾 `Start-Process -FilePath $Exe`
 * 拉的是那个目录。次生灾害：NSIS 判「应用在运行」用 `StartsWith($INSTDIR)`，
 * `C:\Program` 命中 `C:\Program Files\*` 下一切进程，点卸载器的「关闭」等于杀全机。
 *
 * ## 三条判据
 *
 *  ① 路径以 PowerShell 单引号字面量写进每次现生成的 ps1（`'` 转义成 `''`），
 *    不再经 `-ArgumentList` 传参；relay 只传 `-File "<ps 路径>"`（它自己也可能带空格）。
 *  ② 安装目录不安全就不装 —— 只写日志，不产出面向用户的文案（防呆文案容易误解）。
 *  ③ 日志一律 UTF-8：此前 `Add-Content` 默认 ANSI，异常原文在日志里成了乱码，
 *    这次现场就是靠猜的。
 */

"use strict";

const fs = require("node:fs");
const path = require("node:path");

/** PowerShell 单引号字面量（`'` 要写成 `''`）。 */
function psQuote(value) {
  return "'" + String(value == null ? "" : value).replace(/'/g, "''") + "'";
}

/** 装完之后要拉起的那个可执行文件。 */
function relaunchExePath(dir) {
  return path.join(dir, "Crannia.exe");
}

/**
 * 安装目录安全吗？
 *
 * 两条独立判据（都只写日志，不做用户文案）：
 *  1. 目录里必须有 `Crannia.exe` —— 否则说明路径本身推错了，装了也拉不起来；
 *  2. 目录名不能是同级其它目录名的前缀 —— 这正是误杀事故的形状
 *     （`C:\Program` 是 `C:\Program Files` 的前缀）。
 *
 * @param {string} dir
 * @param {{existsSync?: Function, readdirSync?: Function}} [io] 测试注入
 * @returns {{ok: boolean, reason: string}}
 */
function checkInstallDir(dir, io) {
  const read = (io && io.readdirSync) || fs.readdirSync;
  const exists = (io && io.existsSync) || fs.existsSync;
  const full = path.resolve(String(dir || ""));
  if (!path.isAbsolute(full)) return { ok: false, reason: "not an absolute path" };
  if (!exists(full)) return { ok: false, reason: "install dir does not exist" };
  if (!exists(relaunchExePath(full))) {
    return { ok: false, reason: "no Crannia.exe in the install dir" };
  }
  const base = path.basename(full);
  if (!base) return { ok: false, reason: "install dir is a drive root" };
  let siblings;
  try {
    siblings = read(path.dirname(full));
  } catch (e) {
    return { ok: false, reason: "cannot read parent dir: " + errText(e) };
  }
  const lower = base.toLowerCase();
  const clash = siblings.find(function (n) {
    return n !== base && String(n).toLowerCase().startsWith(lower);
  });
  if (clash) {
    return {
      ok: false,
      reason: "dir name is a prefix of sibling '" + clash + "'",
    };
  }
  return { ok: true, reason: "" };
}

function errText(e) {
  return e && e.message ? e.message : String(e);
}

/**
 * 生成这一次换装用的 PowerShell 脚本（路径全部以字面量嵌入）。
 *
 * `/D=` 必须是最后一个参数且不加引号（NSIS 的规矩：它把行尾原样吃下）。
 * 成功判据是"安装器 exit 0 且目标 exe 真的在了"——只看 exit code
 * 不够（这次现场就是 exit 0 而装到了别处）。
 */
function buildApplyScript(opts) {
  const o = opts || {};
  const installer = String(o.installer || "");
  const dir = String(o.dir || "");
  const exe = String(o.exe || "");
  const fallback = String(o.fallbackExe || exe);
  const logFile = String(o.logFile || "");
  const marker = String(o.markerFile || "");
  const logDir = path.dirname(logFile);
  const lines = [
    "# 由 Crannia 生成：等本进程退出 -> 静默装 -> 拉起新版本。路径是字面量，不走参数。",
    '$ErrorActionPreference = "Continue"',
    "$log = " + psQuote(logFile),
    "$installer = " + psQuote(installer),
    "$dir = " + psQuote(dir),
    "$exe = " + psQuote(exe),
    "$fallback = " + psQuote(fallback),
    "$marker = " + psQuote(marker),
    "if (-not (Test-Path -LiteralPath " + psQuote(logDir) + ")) {",
    "  New-Item -ItemType Directory -Force -Path " + psQuote(logDir) + " | Out-Null",
    "}",
    "function Note([string]$m) {",
    '  Add-Content -LiteralPath $log -Encoding UTF8 -Value ("$(Get-Date -Format o) " + $m)',
    "}",
    'Note ("start: " + $installer + " -> " + $dir)',
    "Start-Sleep -Seconds 2",
    "try {",
    "  $p = Start-Process -FilePath $installer -ArgumentList ('/S /D=' + $dir) -Wait -PassThru",
    '  Note ("installer exit " + $p.ExitCode)',
    "  if (($p.ExitCode -eq 0) -and (Test-Path -LiteralPath $exe)) {",
    "    Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue",
    "    Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue",
    '    Note ("updated ok, relaunching " + $exe)',
    "    Start-Process -FilePath $exe",
    "  } else {",
    '    Note ("install did not land at " + $exe + " (exit " + $p.ExitCode + ") -- relaunching previous version")',
    "    Start-Process -FilePath $fallback",
    "  }",
    "} catch {",
    '  Note ("failed: " + $_.Exception.Message + " -- relaunching previous version")',
    "  Start-Process -FilePath $fallback",
    "}",
    "",
  ];
  return lines.join("\r\n");
}

/**
 * 把脚本甩出去的 relay 命令 —— 经它，子进程才不挂在我们这棵进程树上
 * （真机实测 2026-11-13：直接 spawn 脚本再 app.exit(0)，脚本一次都没跑起来）。
 *
 * `-File` 后面的路径自己加双引号：临时目录可能带空格（用户名带空格时）。
 * 整串交给 `Start-Process -ArgumentList` 时是一个字符串，不再分元素。
 */
function buildRelayCommand(scriptPath) {
  const file = String(scriptPath || "").replace(/"/g, "");
  const args = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "'
    + file + '"';
  return "Start-Process powershell.exe -ArgumentList " + psQuote(args)
    + " -WindowStyle Hidden";
}

module.exports = {
  psQuote,
  relaunchExePath,
  checkInstallDir,
  buildApplyScript,
  buildRelayCommand,
};
