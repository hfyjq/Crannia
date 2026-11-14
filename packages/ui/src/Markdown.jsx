import React from "react";
import { parseBlocks } from "./markdown.js";
import { isWebUrl, openLocalPath } from "./openpath.js";
// 模型直接写的 SVG / Mermaid —— 走同一条消毒链（`SvgPreview`）。
//   Mermaid 那个模块本身轻，真正的 ~1 MB 依赖在它内部的
//   `import("mermaid")` 里（懒加载，Vite 切 chunk）。
import SvgPreview from "./SvgPreview.jsx";
import MermaidPreview from "./MermaidPreview.jsx";

// Markdown 的渲染层（解析在 `markdown.js`，纯函数、可单独测）。
//
// 零 `innerHTML`（硬规矩）—— 只产 React 元素，正文一律走文本节点。
//    正文里有模型写的字（也可能是它从网页读回来的字），
//    一次 `innerHTML` 就等于把别人的字变成我们的标签。
//
// 链接绝不渲染成 `<a href>` —— 壳（`apps/desktop/main.cjs`）接了
//    `setWindowOpenHandler`（只放 http/https 去系统浏览器），但没接 `will-navigate`
//    所以一张真的 `<a href>` 被点一下，整页会被导航走（白屏 + 回不来）。
//    所以一律用 `window.open(url)` 走那条闸；`href` 只放 `title` 里给人看（悬停可见）。
//    通则：能点的东西必须走框架那扇门，不能自己开一扇。

function Inline({ nodes }) {
  if (!nodes) return null;
  return nodes.map((n, i) => {
    switch (n.type) {
      case "text":
        return <React.Fragment key={i}>{n.text}</React.Fragment>;
      case "code":
        return <code key={i} className="md-icode">{n.text}</code>;
      case "b":
        return <b key={i}><Inline nodes={n.children} /></b>;
      case "i":
        return <em key={i}><Inline nodes={n.children} /></em>;
      case "mark":
        // `==文字==` —— 说明页里用它标一句要紧的话（颜色在样式表里，
        //   只有 `.help-body` 那一块是红的；聊天里的 `==` 保持原色，不抢眼）。
        return <mark key={i} className="md-mark"><Inline nodes={n.children} /></mark>;
      case "link": {
        // 两条路，看 href 是什么（判据在 `openpath.js`）。
        //   · 网址，走 `window.open`（壳的闸只放 http/https 去系统浏览器）
        //   · 本机路径，`openLocalPath`，`POST /api/reveal`，资源管理器里定位
        // 本机路径不能走 `window.open` —— 壳会拒掉非 http(s)，而且拒得没声音，
        //   点了什么也不发生。
        const web = isWebUrl(n.href);
        return (
          <span key={i} className={"md-link" + (web ? "" : " md-path")} title={n.href}
                role="link" tabIndex={0}
                onClick={() => { try { web ? window.open(n.href, "_blank") : openLocalPath(n.href); } catch { /* 见上 */ } }}>
            <Inline nodes={n.children} />
          </span>
        );
      }
      default:
        return null;
    }
  });
}

function Blocks({ blocks }) {
  return blocks.map((b, i) => {
    switch (b.type) {
      case "h": {
        const Tag = `h${Math.min(Math.max(b.level, 1), 6)}`;
        return <Tag key={i} className="md-h"><Inline nodes={b.children} /></Tag>;
      }
      case "hr":
        return <hr key={i} className="md-hr" />;
      case "code": {
        // 围栏语言只有两个字面量有特殊渲染 ——
        //   ` ```svg ` 与 ` ```mermaid `。其余一律 `<pre>`
        //   （不在名单里的语言不去猜，猜错等于把一段代码当图渲染）。
        const lang = String(b.lang || "").toLowerCase();
        if (lang === "svg") return <SvgPreview key={i} code={b.text} />;
        if (lang === "mermaid") return <MermaidPreview key={i} code={b.text} />;
        return (
          <pre key={i} className="md-pre">
            <code>{b.text}</code>
          </pre>
        );
      }
      case "quote":
        return (
          <blockquote key={i} className="md-quote"><Blocks blocks={b.blocks} /></blockquote>
        );
      case "list":
        // `start` —— 被段落打断的有序列表是多块，
        //    没有它每块都从 1 起（原文 1./2./3. 会显示成三个「1.」）。
        //    `start` 只在 >1 时给（`start=1` 是默认值，写出来只会多一个属性）。
        return b.ordered
          ? <ol key={i} className="md-list" start={b.start > 1 ? b.start : undefined}>{b.items.map((it, k) => (
              <li key={k} style={{ marginLeft: it.level * 14 }}><Inline nodes={it.children} /></li>))}</ol>
          : <ul key={i} className="md-list">{b.items.map((it, k) => (
              <li key={k} style={{ marginLeft: it.level * 14 }}><Inline nodes={it.children} /></li>))}</ul>;
      case "table":
        return (
          <div key={i} className="md-tablewrap">
            <table className="md-table">
              <thead><tr>{b.head.map((c, k) => <th key={k}><Inline nodes={c} /></th>)}</tr></thead>
              <tbody>{b.rows.map((r, k) => (
                <tr key={k}>{r.map((c, j) => <td key={j}><Inline nodes={c} /></td>)}</tr>))}</tbody>
            </table>
          </div>
        );
      default:
        return <p key={i} className="md-p"><Inline nodes={b.children} /></p>;
    }
  });
}

export function Markdown({ text }) {
  const blocks = parseBlocks(text == null ? "" : String(text));
  if (!blocks.length) return null;
  return <div className="md"><Blocks blocks={blocks} /></div>;
}
