// 命令块语言标注：只认 DNA 级特征，宁可漏判不可误判。
//
// 钉三件事：
//   ① 真正的 DNA 特征两边各认一批；
//   ② 看着像但其实共有的写法（&& / grep / 盘符 / $var / /dev/null）
//      一律不判 —— 这些是上一版误判的来源；
//   ③ 代码赋值、注释、混合证据都不判。
//
// 零依赖，node 直接跑；输出 ASCII（GBK 控制台）。

import assert from "node:assert";
import { detectCommandLanguage, languageLabel } from "../src/commandLang.js";

let pass = 0;
const failed = [];
function check(label, cond) {
  if (cond) { pass += 1; console.log("  [ok]   " + label); }
  else { failed.push(label); console.log("  [!!]   " + label); }
}

console.log("=".repeat(62));
console.log("Command language: DNA-only evidence, never guess shared syntax");
console.log("=".repeat(62));

console.log("\n-- PowerShell DNA --");
for (const cmd of [
  "[Console]::WriteLine('hi')",
  "[System.IO.File]::ReadAllText('x')",
  "$env:PATH",
  "$global:Config = 1",
  "Get-ChildItem -Path .",
  "Remove-Item -LiteralPath x",
  "Write-Host ok",
]) {
  check("PS: " + cmd.slice(0, 36),
        detectCommandLanguage(cmd) === "powershell",
        detectCommandLanguage(cmd));
}

console.log("\n-- Bash DNA --");
for (const cmd of [
  "if [ -f x ]; then echo yes; fi",
  "for f in *.txt; do echo $f; done",
  "export PATH=/usr/local/bin:$PATH",
  "set -e",
  "set -o pipefail",
  "cat <<EOF > out.txt",
  "[[ -f x ]] && echo yes",
]) {
  check("SH: " + cmd.slice(0, 36),
        detectCommandLanguage(cmd) === "bash",
        detectCommandLanguage(cmd));
}

// 上一版靠这些特征判过，但它们其实两边都能写出来 —— 现在必须不判。
console.log("\n-- NOT DNA (shared / tool names / paths) --");
for (const cmd of [
  "ls -la && pwd",
  "cat file 2>/dev/null || echo missing",
  "grep -r foo .",
  "curl -s http://x | jq .",
  "cd C:\\Users",
  "echo $HOME",
  "ls", "echo hi", "pwd", "mkdir foo", "date",
]) {
  check("no verdict: " + cmd.slice(0, 36),
        detectCommandLanguage(cmd) === "",
        detectCommandLanguage(cmd));
}

console.log("\n-- non-command code / mixed evidence --");
for (const code of [
  "x = a && b",
  "const x = foo && bar;",
  "# comment with && inside",
  "if (a && b) { return 1 }",
  "", "   ",
]) {
  check("no verdict: " + JSON.stringify(code).slice(0, 32),
        detectCommandLanguage(code) === "",
        detectCommandLanguage(code));
}

console.log("\n-- non-DNA peers do not cancel DNA --");
check("cmdlet wins when && is present (&& is not evidence)",
      detectCommandLanguage("Get-ChildItem && ls") === "powershell",
      detectCommandLanguage("Get-ChildItem && ls"));
check("cmdlet wins when grep is present (grep is not evidence)",
      detectCommandLanguage("grep foo . ; Get-Content x") === "powershell",
      detectCommandLanguage("grep foo . ; Get-Content x"));

console.log("\n-- label display --");
check("powershell label", languageLabel("powershell") === "PowerShell");
check("pwsh alias", languageLabel("pwsh") === "PowerShell");
check("bash label", languageLabel("bash") === "Bash");
check("sh alias", languageLabel("sh") === "Bash");
check("cmd label is distinct", languageLabel("cmd") === "CMD");
check("unknown stays lowercase", languageLabel("rust") === "rust");

console.log("=".repeat(62));
if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(`${pass + failed.length} tests: ${pass} passed, ${failed.length} failed`);
if (failed.length) process.exitCode = 1;
