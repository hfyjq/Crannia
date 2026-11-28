// 代码块"识别全语言 + 原样内容"的机械读者。
//
// 钉两件事（都是复制按钮正确性的前提）：
//   ① 围栏语言是任意 token，不是主流语言白名单 —— 命令行/冷门语言/自定义
//      标记都得识别成 code 块，否则用户拿不到复制按钮。
//   ② code 块的 text 必须逐字等于围栏内的原文：不多空格、不吞尾空行、
//      不改 CRLF 语义 —— 复制的必须是这段本身。
//
// 零依赖，node 直接跑；输出 ASCII（Windows 控制台 GBK）。

import assert from "node:assert";
import { parseBlocks } from "../src/markdown.js";

let pass = 0;
const failed = [];

function check(label, cond) {
  if (cond) { pass += 1; console.log("  [ok]   " + label); }
  else { failed.push(label); console.log("  [!!]   " + label); }
}

function codes(md) {
  return parseBlocks(md).filter(b => b.type === "code");
}

console.log("=".repeat(62));
console.log("Code fence: any language token / byte-exact body");
console.log("=".repeat(62));

// ── ① 语言识别：主流 + 命令行 + 冷门 + 自定义 ──────────────────
const langs = [
  "python", "js", "javascript", "ts", "typescript", "go", "rust", "java",
  "bash", "sh", "shell", "zsh", "fish", "console", "terminal",
  "powershell", "pwsh", "cmd", "bat",
  "sql", "json", "yaml", "yml", "toml", "ini", "dockerfile", "makefile",
  "text", "plaintext", "diff", "log",
  "verilog", "lisp", "haskell", "kotlin", "zig", "nix",
  "my-custom-lang", "c++", "c#", "objective-c",
];
for (const lang of langs) {
  const got = codes("```" + lang + "\nBODY\n```");
  check("lang " + lang + " recognized",
        got.length === 1 && got[0].lang === lang, JSON.stringify(got));
}
// 参照物 opencode 的 shell 白名单恰好这 7 种（markdown.tsx 的
// `shellLanguages`）；我们这里不设白名单，但它们一个都不能漏。
check("all 7 shell languages from the reference are recognized",
      ["bash", "sh", "shell", "zsh", "fish", "console", "terminal"]
        .every(l => codes("```" + l + "\ncmd\n```")[0]?.lang === l));
check("no language is still a code block",
      codes("```\nBODY\n```").length === 1);
check("tilde fence works too",
      codes("~~~bash\nBODY\n~~~").length === 1);
check("svg stays code in the parser (special render is upstream)",
      codes("```svg\n<svg/>\n```")[0]?.lang === "svg");

// ── ② 原样：不多空格、不吞尾空行、逐字 ────────────────────────
check("body is byte-exact",
      codes("```python\nx = 1\nprint(x)\n```")[0].text === "x = 1\nprint(x)");
check("leading indentation preserved",
      codes("```python\n    indented\n\tTabbed\n```")[0].text ===
      "    indented\n\tTabbed");
check("blank lines inside preserved",
      codes("```text\na\n\nb\n```")[0].text === "a\n\nb");
check("trailing blank line before fence preserved",
      codes("```text\na\n\n```")[0].text === "a\n");
check("no trailing newline invented",
      codes("```text\na```")[0].text !== "a\n");
check("trailing spaces on a line preserved",
      codes("```text\na   \n```")[0].text === "a   ");
check("markdown syntax inside code is not parsed",
      codes("```md\n# not a heading\n**not bold**\n```")[0].text ===
      "# not a heading\n**not bold**");
check("code content keeps symbols verbatim",
      codes("```text\n<b>&amp; \"quoted\" 'single' \\ $\n```")[0].text ===
      "<b>&amp; \"quoted\" 'single' \\ $");

console.log("=".repeat(62));
if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(`${pass + failed.length} tests: ${pass} passed, ${failed.length} failed`);
if (failed.length) process.exitCode = 1;
