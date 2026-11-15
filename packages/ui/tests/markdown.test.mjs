// `packages/ui/src/markdown.js` 的读者 —— 零依赖、node 直接跑。
//
//   node packages/ui/tests/markdown.test.mjs
//
// 为什么是这个形状：本包没有测试设施（没有 vitest/jest），而
//    "写一个解析器却不配测试"本仓库不会认。，解析写成纯函数（进出都是普通对象），
//    于是不用 JSX、不用打包器、不装任何东西就能断言。
//    Python 那一侧由 `tests/test_desktop_markdown.py` 把它接进全量 runner。
//
// 头号判据：宁可多显示字面量，绝不吃掉正文。
//    所以下面第一组全是"病理输入" —— 它们的期望不是"渲染得好看"，是
//    一个字都没少。（本仓库真事故：旧的边界中和器一路吃到下一个 `>`，
//     一句话少半句。同一个尺度。）

import assert from "node:assert/strict";
import { parseBlocks, parseInline } from "../src/markdown.js";

let pass = 0;
const failed = [];
function check(label, fn) {
  try { fn(); pass += 1; console.log("  [ok]   " + label); }
  catch (e) { failed.push(label); console.log("  [FAIL] " + label + "  -- " + e.message); }
}

/** 把一棵块树里的所有文本抠出来（用来判"有没有丢字"）。 */
function textOf(blocks) {
  const walkInline = (nodes) => (nodes || []).map((n) => {
    if (n.type === "text") return n.text;
    if (n.type === "code") return n.text;
    return walkInline(n.children);
  }).join("");
  return blocks.map((b) => {
    switch (b.type) {
      case "h": return walkInline(b.children);
      case "p": return walkInline(b.children);
      case "code": return b.text;
      case "quote": return textOf(b.blocks);
      case "list": return b.items.map((it) => walkInline(it.children)).join("");
      case "table": return b.head.map(walkInline).join("")
        + b.rows.map((r) => r.map(walkInline).join("")).join("");
      default: return "";
    }
  }).join("\n");            // 块之间用换行接（与下面 visible() 同一个口径）
}

/** 原样，但去掉空行（空行是结构，不是内容）。
 *
 *  第一版这里写的是 `.join("")`（把换行也去掉）—— 于是"段落里本来就有的换行"
 *  被误判成"多出来的"，三条断言假红。
 *，教训：比之前先对齐两边的口径（`S1` 那一族：两个数不同源时，
 *    先问它们量的是不是同一个量）。 */
function visible(src) {
  return src.split(/\r?\n/).filter((l) => l.trim()).join("\n");
}

// ── ① 绝不吃掉正文：病理输入也一个字不许少 ───────────────────────────
check("★ 未闭合的 `**` 原样显示（不吞）", () => {
  const s = "这是 **没闭合的加粗";
  assert.equal(textOf(parseBlocks(s)), visible(s));
});
check("★ 孤立的 `<` `>` 一字不差（旧事故的形状）", () => {
  const s = "如果 a < b 且 c > d 那就别动 <user_document> 这段";
  assert.equal(textOf(parseBlocks(s)), visible(s));
});
check("★ 半截链接 `[文字]` 原样（不当成链接、也不吞）", () => {
  const s = "看这个 [没有目标的链接] 和 [这个]( ) 也是";
  assert.equal(textOf(parseBlocks(s)), visible(s));
});
check("★ 未闭合的围栏（``` 没关）：**内容行一字不少**", () => {
  // 期望不含那三个反引号 —— 它是标记不是内容：按标准，未闭合的围栏
  //    一直吃到结尾，标记本身不渲染。第一版这里按"整段原样"写，
  //    那是把「标记」与「正文」混成一件了；判据要盯的是内容行在不在
  //    （被上游截断的情形正是这个形状：正文在、标记不全）。
  const s = "前面\n```\ncode line\n还有一行";
  const b = parseBlocks(s);
  const code = b.find((x) => x.type === "code");
  assert.ok(code, "没认出围栏");
  assert.equal(code.text, "code line\n还有一行");
  assert.equal(textOf(b), "前面\ncode line\n还有一行");
});
check("★ 表格分隔线坏掉时退回普通段落（不当表格、不吞）", () => {
  const s = "| a | b |\n不是分隔线\n| c | d |";
  assert.equal(textOf(parseBlocks(s)), visible(s));
});
check("★ 一长串 `*` 不许把栈吃掉（深度封顶，超出原样）", () => {
  const s = "*".repeat(200) + "结尾";
  assert.equal(textOf(parseBlocks(s)), visible(s));
});
check("★ 空输入 / null 不炸", () => {
  assert.deepEqual(parseBlocks(""), []);
  assert.deepEqual(parseBlocks(null), []);
  assert.deepEqual(parseInline(undefined), []);
});

// ── ② 真形状：标识符不许被斜体化 ─────────────────────────────────
check("★★ `cron_jobs` 与 `cron_runs` 同一行 ⇒ 中间那段**不许变斜体**", () => {
  const s = "表是 cron_jobs 和 cron_runs 两张";
  const nodes = parseInline(s);
  assert.equal(nodes.length, 1, "被切成了 " + nodes.length + " 段（说明 `_` 被当强调）");
  assert.equal(nodes[0].type, "text");
  assert.equal(nodes[0].text, s);
});
check("★ `run_tests.py` / `_CORE_TOOLS` / `a_b_c` 同理", () => {
  for (const s of ["跑 run_tests.py 就行", "常驻集是 _CORE_TOOLS", "蛇形 a_b_c_d_e"]) {
    assert.equal(parseInline(s).length, 1, s);
  }
});

// ── ③ 行内：加粗 / 斜体 / 行内码 / 链接 ───────────────────────────
check("`**粗**` 出 b 节点，且里面文字在", () => {
  const n = parseInline("前 **粗体** 后");
  assert.deepEqual(n.map((x) => x.type), ["text", "b", "text"]);
  assert.equal(n[1].children[0].text, "粗体");
});
check("`*斜*` 出 i 节点", () => {
  assert.equal(parseInline("a *斜* b")[1].type, "i");
});
check("`` `码` `` 出 code 节点（里面**不再解析**）", () => {
  const n = parseInline("看 `**这不该变粗**` 这段");
  assert.equal(n[1].type, "code");
  assert.equal(n[1].text, "**这不该变粗**");
});
// ── ③b 高亮：`==文字==`（2026-10-26 加 —— 说明页要一句红字）─────────
check("`==高亮==` 出 mark 节点", () => {
  const n = parseInline("前 ==红字== 后");
  assert.deepEqual(n.map((x) => x.type), ["text", "mark", "text"]);
  assert.equal(n[1].children[0].text, "红字");
});
check("★★ 没闭合的 `==` 原样当文本（不吃字）", () => {
  const s = "只写了 == 一半";
  assert.deepEqual(parseInline(s), [{ type: "text", text: s }]);
});
check("★ `a == b == c` 那种比较表达式**不被**高亮（两边有空白）", () => {
  const s = "if a == b == c";
  assert.deepEqual(parseInline(s), [{ type: "text", text: s }]);
});
check("★ 高亮里还能有行内强调（递归）", () => {
  const n = parseInline("==**要紧**==");
  assert.equal(n[0].type, "mark");
  assert.equal(n[0].children[0].type, "b");
});
check("★ 行内码里的 `==` 不变高亮", () => {
  const n = parseInline("看 `a == b` 这段");
  assert.equal(n[1].type, "code");
  assert.equal(n[1].text, "a == b");
});
check("`[字](url)` 出 link 节点（href 原样）", () => {
  const n = parseInline("见 [官网](https://example.com/a?b=1) 结束");
  assert.equal(n[1].type, "link");
  assert.equal(n[1].href, "https://example.com/a?b=1");
  assert.equal(n[1].children[0].text, "官网");
});
// ── href 里允许空格（现场逼出来的）──────────────
//
// 现场（用户截图）：模型把路径写成
// `[桌面 → 项目一 产品设计开发 作业.docx](C:…产品设计开发 作业.docx)` ——
// 文件名里有空格，而原来的 href 正则写的是 `[^)\s]*`（不许空白）
// 所以整串落回文本：用户看着像链接、点不动。
check("href 带空格（中文文件名里常有）", () => {
  const n = parseInline("[桌面 → 项目一 作业.docx](C:/x/项目一 作业.docx)");
  assert.equal(n[0].type, "link");
  assert.equal(n[0].href, "C:/x/项目一 作业.docx");
});
check("★ 宽松之后**不许把 Markdown 的标题吃进 href**", () => {
  const n = parseInline('[A](D:/a b.docx "标题")');
  assert.equal(n[0].type, "link");
  assert.equal(n[0].href, "D:/a b.docx");     // 不是 `D:/a b.docx "标题"`
});
check("★ 空 href 仍然**落回文本**（放宽的是空格，不是这条护栏）", () => {
  const n = parseInline("[]( )");
  assert.ok(!n.some((x) => x.type === "link"), JSON.stringify(n));
});

check("`\\*` 转义 ⇒ 字面星号", () => {
  const n = parseInline("2 \\* 3");
  assert.equal(n.length, 1);
  assert.equal(n[0].text, "2 * 3");
});

// ── ④ 块：标题 / 列表 / 引用 / 代码 / 表格 / 分隔线 ────────────────
check("标题：层级对，且 `#` 不进正文", () => {
  const b = parseBlocks("## 小标题");
  assert.equal(b[0].type, "h");
  assert.equal(b[0].level, 2);
  assert.equal(textOf(b), "小标题");
});
check("无序 / 有序列表分开成两种块", () => {
  const u = parseBlocks("- 甲\n- 乙");
  assert.equal(u[0].type, "list"); assert.equal(u[0].ordered, false);
  assert.equal(u[0].items.length, 2);
  const o = parseBlocks("1. 甲\n2. 乙");
  assert.equal(o[0].ordered, true);
});
// （用户截图）：原文 1./2./3. 被段落打断，显示成三个「1.」。
//    判据是 CommonMark 那条：每个列表块记住自己第一个条目的编号（`start`）。
check("★★ 被段落打断的有序列表：每块记住**自己的起始编号**", () => {
  const b = parseBlocks("1. 甲\n\n说明\n\n2. 乙\n\n说明二\n\n3. 丙");
  const lists = b.filter((x) => x.type === "list");
  assert.equal(lists.length, 3);
  assert.deepEqual(lists.map((x) => x.start), [1, 2, 3]);
  // 负对照：把编号丢掉的那版三个都是 1 —— 正是截图里那个形状。
  assert.notDeepEqual(lists.map((x) => x.start), [1, 1, 1]);
});
check("连续有序列表的 `start` 取第一条的编号（后面的编号不影响渲染）", () => {
  const b = parseBlocks("5. 甲\n9. 乙");
  assert.equal(b.length, 1);
  assert.equal(b[0].start, 5);
  assert.equal(b[0].items.length, 2);
});
check("引用收成一块", () => {
  const b = parseBlocks("> 引用一\n> 引用二");
  assert.equal(b[0].type, "quote");
  assert.equal(b[0].blocks[0].type, "p");
});
check("围栏代码逐字（含 `**` 与空行）", () => {
  const b = parseBlocks("```py\nx = 1\n\n**不是加粗**\n```");
  assert.equal(b[0].type, "code");
  assert.equal(b[0].lang, "py");
  assert.equal(b[0].text, "x = 1\n\n**不是加粗**");
});
check("分隔线", () => {
  assert.equal(parseBlocks("---")[0].type, "hr");
});
check("表格：表头 / 数据行数对，且**格子也解析行内**", () => {
  const b = parseBlocks("| 名 | 值 |\n|---|---|\n| **a** | 1 |\n| b | 2 |");
  assert.equal(b[0].type, "table");
  assert.equal(b[0].head.length, 2);
  assert.equal(b[0].rows.length, 2);
  assert.equal(b[0].rows[0][0][0].type, "b");
});

// ── ⑤ 报告 ───────────────────────────────────────────────────────
console.log("");
if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(pass + " tests: " + pass + " passed, " + failed.length + " failed");
process.exit(failed.length ? 1 : 0);
