// Markdown → 树（纯函数，不认识 React）。
//
// 为什么分成"解析"和"渲染"两个文件：本包（`packages/ui`）没有测试设施，
//    而"写一个解析器却不配测试"不可接受（原则：检测可以错，打码不能错）。
//    所以解析是纯的（进出都是普通对象），能用 `node` 直接跑断言，
//      不需要 JSX、不需要打包器、不需要装任何东西（见 `packages/ui/tests/`）。
//
// 一条硬判据：宁可多显示字面量，绝不吃掉正文。
//    解析不认识的形状，原样当文本（不是"少半句"）。
//    反例：用 `<[^>]*>` 这类"边界中和"会一路吃到下一个 `>`，
//    一句话直接少半句。这里用同一个尺度。
//
// 零依赖、零 `innerHTML` —— 渲染层只产 React 元素，正文一律走文本节点。

/** 行内：`粗` / `*斜*` / `==高亮==` / `` `码` `` / `[字](url)` / `\` 转义。 */
export function parseInline(src, depth = 0) {
  const text = String(src == null ? "" : src);
  const out = [];
  let buf = "";
  let i = 0;
  const flush = () => { if (buf) { out.push({ type: "text", text: buf }); buf = ""; } };
  // 递归深度封顶：病态输入（几百个 `*`）不许把栈吃掉。
  //   到顶之后原样输出（绝不丢字）。
  const tooDeep = depth >= 6;

  while (i < text.length) {
    const c = text[i];
    if (c === "\\" && i + 1 < text.length) { buf += text[i + 1]; i += 2; continue; }
    if (!tooDeep && c === "`") {
      const end = text.indexOf("`", i + 1);
      if (end > i) { flush(); out.push({ type: "code", text: text.slice(i + 1, end) }); i = end + 1; continue; }
    }
    // 只认 `**`，不认 `__`；`_` 一律不当强调。
    //    判据：`cron_jobs` 与 `cron_runs` 出现在同一行时，
    //    中间的 `_…_` 会被当成斜体，把标识符渲染成斜体（还是错的那种）。
    //    源码与文档里这种形状遍地都是（`run_tests.py` / `tool_perms` / `_CORE_TOOLS`…），
    //    而 CommonMark 本来也不认"词内 `_`"。
    //    用 `*` 就够 —— 模型输出里强调几乎全是 `**`，而单个 `*` 的歧义面小得多。
    if (!tooDeep && text.startsWith("**", i)) {
      const mark = "**";
      const end = text.indexOf(mark, i + 2);
      if (end > i + 2) {
        flush();
        out.push({ type: "b", children: parseInline(text.slice(i + 2, end), depth + 1) });
        i = end + 2; continue;
      }
    }
    if (!tooDeep && c === "*") {
      const end = text.indexOf(c, i + 1);
      if (end > i + 1) {
        flush();
        out.push({ type: "i", children: parseInline(text.slice(i + 1, end), depth + 1) });
        i = end + 1; continue;
      }
    }
    // 高亮：`==文字==`（加 —— 说明页要一句红字）。
    //    判据同上面几条：闭合不上就原样当文本（不吃字）。
    //    两边紧贴才算：开 `==` 后第一个字符、闭合前最后一个字符都不许是空白。
    //    否则 `a == b == c` 这种比较表达式会被高亮（正文里合法，但显然不是意图）。
    if (!tooDeep && text.startsWith("==", i)) {
      const end = text.indexOf("==", i + 2);
      if (end > i + 2) {
        const inner = text.slice(i + 2, end);
        if (!/^\s/.test(inner) && !/\s$/.test(inner)) {
          flush();
          out.push({ type: "mark", children: parseInline(inner, depth + 1) });
          i = end + 2; continue;
        }
      }
    }
    if (!tooDeep && c === "[") {
      // 只认封闭的 `[字](url)` —— 认不出就落回文本（那半句不会消失）。
      //
      // href 允许空格（用 `[^)\s]*` 会把带空格的路径挡在外面）。
      //   失败模式：`[桌面 → 项目一产品设计开发作业.docx](C:…产品设计开发作业.docx)`
      //   —— 文件名里就有空格，整串落回文本，看着像链接、点不动。
      //   宽松之后要防"吃错"：先 `trim`，再去掉 Markdown 的可选标题
      //   （`[字](url "标题")` 那种）—— 不去掉的话 href 会变成 `url "标题"`，指错地方。
      const m = /^\[([^\]]*)\]\(([^)]*)\)/.exec(text.slice(i));
      const href = m ? m[2].trim().replace(/\s+("[^"]*"|'[^']*')\s*$/, "") : "";
      // href 为空的 `[字]()` 不算链接 —— 算了就会把 `[]()` 这几个字符
      //   吃掉（渲染出来只剩"字"）。这正是本条判据要防的那一侧：
      //   "宁可多显示字面量"。因此判据里必须带 `m[2] &&`。
      if (m && href) {
        flush();
        out.push({ type: "link", href, children: parseInline(m[1], depth + 1) });
        i += m[0].length; continue;
      }
    }
    buf += c; i += 1;
  }
  flush();
  return out;
}

const _FENCE = /^\s*(```|~~~)\s*([^\s`]*)\s*$/;
const _HEAD = /^\s{0,3}(#{1,6})\s+(.*)$/;
const _HR = /^\s{0,3}(-{3,}|\*{3,}|_{3,})\s*$/;
const _UL = /^(\s*)([-*+])\s+(.*)$/;
const _OL = /^(\s*)(\d{1,9})[.)]\s+(.*)$/;
const _QUOTE = /^\s{0,3}>\s?(.*)$/;
const _TABLE = /^\s*\|(.+)\|\s*$/;
const _SEP = /^\s*\|[\s:|-]+\|\s*$/;

/** 把一个表格行切成格子（去首尾空、去未转义的 `|`）。 */
function _cells(line) {
  const inner = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  const out = [];
  let buf = "";
  for (let i = 0; i < inner.length; i += 1) {
    if (inner[i] === "\\" && i + 1 < inner.length) { buf += inner[i + 1]; i += 1; continue; }
    if (inner[i] === "|") { out.push(buf.trim()); buf = ""; continue; }
    buf += inner[i];
  }
  out.push(buf.trim());
  return out;
}

/** 全文 → 块数组（纯函数）。 */
export function parseBlocks(text) {
  const lines = String(text == null ? "" : text).split(/\r?\n/);
  const blocks = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // ── 围栏代码：里面逐字，不做任何行内解析 ──
    const fence = _FENCE.exec(line);
    if (fence) {
      const mark = fence[1];
      const body = [];
      i += 1;
      while (i < lines.length && !new RegExp("^\\s*" + mark + "\\s*$").test(lines[i])) {
        body.push(lines[i]); i += 1;
      }
      i += 1;                                     // 吃掉收尾那行（没有也算闭合）
      blocks.push({ type: "code", lang: fence[2] || "", text: body.join("\n") });
      continue;
    }

    if (!line.trim()) { i += 1; continue; }

    const head = _HEAD.exec(line);
    if (head) {
      blocks.push({ type: "h", level: head[1].length, children: parseInline(head[2]) });
      i += 1; continue;
    }

    if (_HR.test(line)) { blocks.push({ type: "hr" }); i += 1; continue; }

    // ── 引用：连续的 `>` 收成一块 ──
    if (_QUOTE.test(line)) {
      const body = [];
      while (i < lines.length && _QUOTE.test(lines[i])) {
        body.push(_QUOTE.exec(lines[i])[1]); i += 1;
      }
      blocks.push({ type: "quote", blocks: parseBlocks(body.join("\n")) });
      continue;
    }

    // ── 表格：`|…|` 后面紧跟一行分隔线才算 ──
    if (_TABLE.test(line) && i + 1 < lines.length && _SEP.test(lines[i + 1])) {
      // 格子在这里就解析成行内节点 —— 与别的块（`children`）同一种形状，
      //   渲染层因此不需要"再解析一次"（那会多一段没用的转发）。
      const head = _cells(line).map(parseInline);
      const rows = [];
      i += 2;
      while (i < lines.length && _TABLE.test(lines[i])) {
        rows.push(_cells(lines[i]).map(parseInline)); i += 1;
      }
      blocks.push({ type: "table", head, rows });
      continue;
    }

    // ── 列表：连续的同类收成一块（缩进只记层级，不拆嵌套）──
    const ul = _UL.exec(line);
    const ol = ul ? null : _OL.exec(line);
    if (ul || ol) {
      const ordered = !!ol;
      // 记住这一块列表的起始编号。
      //    症状：原文 `1. …\n\n段落\n\n2. …\n\n段落\n\n3. …` 显示成三个「1.」。
      //    根因：段落打断列表，解析成三块 `ol`，而这一格若把编号丢掉、
      //    渲染层又只写 `<ol>`（浏览器每块都从 1 起）。
      //    判据与 CommonMark 一致：只有第一个条目的编号决定 `start`
      //    （后面写几都不影响渲染，浏览器按顺序数）。
      const start = ordered ? Number(ol[2]) : 0;
      const items = [];
      while (i < lines.length) {
        const m = ordered ? _OL.exec(lines[i]) : _UL.exec(lines[i]);
        if (!m) break;
        const indent = Math.floor(m[1].replace(/\t/g, "  ").length / 2);
        items.push({ level: indent, children: parseInline(m[3]) });
        i += 1;
      }
      blocks.push({ type: "list", ordered, start, items });
      continue;
    }

    // ── 兜底：段落（连续的普通行合成一段，行内解析照做）──
    const para = [];
    while (i < lines.length && lines[i].trim()
           && !_FENCE.test(lines[i]) && !_HEAD.test(lines[i]) && !_HR.test(lines[i])
           && !_QUOTE.test(lines[i]) && !_UL.test(lines[i]) && !_OL.test(lines[i])) {
      para.push(lines[i]); i += 1;
    }
    // 理论上到不了这里（上面每一条都不匹配时 para 至少收一行）；
    //   真到了就原样推一行，绝不让某一行为空转 —— 空转 = 丢字。
    blocks.push({ type: "p", children: parseInline(para.length ? para.join("\n") : line) });
    if (!para.length) i += 1;
  }
  return blocks;
}
