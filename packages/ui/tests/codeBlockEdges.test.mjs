// 围栏解析的边界：大段注释 / 大段空行 / 缩进 / 字符保真。
//
// 用户反映过：某些平台上，大段注释或空行会把代码块识别"断掉"，
// 屏幕上只剩半截可复制，甚至缩进错、字符错。这里是复现与回归。
//
// 零依赖，node 直接跑；输出 ASCII（GBK 控制台）。

import assert from "node:assert";
import { parseBlocks } from "../src/markdown.js";

let pass = 0;
const failed = [];
function check(label, ok, note = "") {
  if (ok) { pass += 1; console.log("  [ok]   " + label); }
  else { failed.push(label); console.log("  [!!]   " + label + (note ? " -- " + note : "")); }
}
function code(md) { return parseBlocks(md).filter(b => b.type === "code"); }

console.log("=".repeat(62));
console.log("Fence edges: long comments / blank runs / indentation / fidelity");
console.log("=".repeat(62));

console.log("\n-- 1. long comments inside a fence --");
{
  const body = [
    "# ============ section ============",
    "# this is a long comment block",
    "# line 3 of comment",
    "# line 4 of comment",
    "# line 5 of comment",
    "KEY=value",
    "# tail comment",
  ].join("\n");
  const got = code("```bash\n" + body + "\n```");
  check("7-line comment block stays inside one code block",
        got.length === 1 && got[0].text === body,
        JSON.stringify(got.map(g => g.text)));
}
{
  const body = ["/* first */", "/* second */", "// third", "// fourth",
                "// fifth", "int x = 1;"].join("\n");
  const got = code("```c\n" + body + "\n```");
  check("C-style comment run stays intact",
        got.length === 1 && got[0].text === body,
        JSON.stringify(got));
}

console.log("\n-- 2. blank lines --");
{
  const body = "a\n\n\n\n\nb";
  const got = code("```text\n" + body + "\n```");
  check("blank run inside code preserved",
        got.length === 1 && got[0].text === body, JSON.stringify(got));
}
{
  const md = "```text\n\n\n\n\n```";
  const got = code(md);
  check("all-blank fence still yields one code block",
        got.length === 1, JSON.stringify(got));
}
{
  const md = "before\n\n\n\n\n```text\nbody\n```\n\nafter";
  const blocks = parseBlocks(md);
  check("blank run before fence does not swallow the following code",
        blocks.some(b => b.type === "code" && b.text === "body"),
        JSON.stringify(blocks));
}

console.log("\n-- 3. indentation fidelity --");
{
  const body = "def f():\n    x = 1\n        y = 2\n  z = 3";
  const got = code("```python\n" + body + "\n```");
  check("indentation preserved byte-for-byte",
        got.length === 1 && got[0].text === body,
        JSON.stringify(got.map(g => g.text)));
}
{
  const body = "\tif true; then\n\t\techo yes\n\tfi";
  const got = code("```bash\n" + body + "\n```");
  check("tabs preserved", got.length === 1 && got[0].text === body);
}

console.log("\n-- 4. characters fidelity --");
{
  const body = "<html> &amp; \"double\" 'single' \\ slash $VAR `tick` | pipe > redir";
  const got = code("```text\n" + body + "\n```");
  check("special characters preserved byte-for-byte",
        got.length === 1 && got[0].text === body,
        JSON.stringify(got.map(g => g.text)));
}
{
  const body = "中文注释：路径 C:\\Users\\测试 · emoji 不是转义";
  const got = code("```text\n" + body + "\n```");
  check("unicode and backslashes preserved",
        got.length === 1 && got[0].text === body);
}
{
  const md = "```text\na\r\nb\r\n```";
  const got = code(md);
  check("CRLF normalizes to LF without losing characters",
        got.length === 1 && got[0].text === "a\nb",
        JSON.stringify(got.map(g => g.text)));
}

console.log("\n-- 5. fence-in-comment (the real breakage) --");
{
  // 代码正文里出现"看起来像围栏"的行 —— 这是最常见的半截来源。
  const body = "# example:\n# ```\n# code here\n# ```\nKEY=value";
  const got = code("```bash\n" + body + "\n```");
  check("markdown fence markers inside comment lines do not close early",
        got.length === 1 && got[0].text === body,
        JSON.stringify(got.map(g => g.text)));
}
{
  const body = "print('```')\nprint('~~~')";
  const got = code("```python\n" + body + "\n```");
  check("quoted fence markers do not close early",
        got.length === 1 && got[0].text === body,
        JSON.stringify(got.map(g => g.text)));
}
{
  // 真正的闭合行必须"整行只有围栏"（前后空白允许）
  const md = "```text\na\n   ```   ";
  const got = code(md);
  check("indented closing fence is accepted (CommonMark)",
        got.length === 1 && got[0].text === "a",
        JSON.stringify(got.map(g => g.text)));
}

console.log("\n-- 6. streaming: unclosed fence must be marked, not offered as complete --");
{
  const partial = "说明\n```python\ndef f():\n    x = 1";
  const got = code(partial);
  check("unclosed fence yields a code block", got.length === 1, JSON.stringify(got));
  check("unclosed fence is marked closed=false (no copy button)",
        got[0]?.closed === false, JSON.stringify(got));
  check("closed fence is marked closed=true",
        code("```python\nx=1\n```")[0]?.closed === true);
  check("indented closing fence also marks closed=true",
        code("```python\nx=1\n   ```")[0]?.closed === true);
  check("streamed partial text is still complete-so-far (no invented chars)",
        got[0]?.text === "def f():\n    x = 1", JSON.stringify(got[0]?.text));
}

console.log("=".repeat(62));
if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(`${pass + failed.length} tests: ${pass} passed, ${failed.length} failed`);
if (failed.length) process.exitCode = 1;
