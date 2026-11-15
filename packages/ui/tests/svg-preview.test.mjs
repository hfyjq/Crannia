// SVG / Mermaid 呈现的纯函数那一半。
// 运行：node packages/ui/tests/svg-preview.test.mjs
//
// 渲染层（`SvgPreview.jsx`）要 DOMPurify，node 里没有 DOM，这里钉两件事：
//   ① 纯函数（根元素判定 / data URL / 上限 / 下载名）；
//   ② 消毒配置本身（源码级）—— 它是安全判据，删一条要有东西红。
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  downloadName, isSvgSource, MAX_SVG_CHARS, svgDataUrl, tooLarge,
} from "../src/svg.js";

let pass = 0;
function eq(name, got, want) {
  assert.deepStrictEqual(got, want, name + ": got " + JSON.stringify(got));
  pass++;
}
function ok(name, cond, detail) {
  assert.ok(cond, name + (detail ? ": " + detail : ""));
  pass++;
}

const _here = dirname(fileURLToPath(import.meta.url));
const _src = (f) => readFileSync(join(_here, "..", "src", f), "utf8");

// ── 1. 根元素判定 ──────────────────────────────────────────────────
ok("plain <svg>", isSvgSource('<svg xmlns="http://www.w3.org/2000/svg"></svg>'));
ok("xml declaration then <svg>", isSvgSource('<?xml version="1.0"?>\n<svg></svg>'));
ok("leading whitespace and BOM are tolerated",
  isSvgSource('﻿\n  <svg></svg>'));
ok("<svg> inside prose is NOT an svg (根元素不是它)",
  !isSvgSource("这张图长这样：<svg></svg>"));
ok("html isn't svg", !isSvgSource("<html><body></body></html>"));
ok("empty isn't svg", !isSvgSource(""));
ok("null isn't svg", !isSvgSource(null));

// ── 2. data URL ───────────────────────────────────────────────────
{
  const u = svgDataUrl('<svg><title>图</title></svg>');
  ok("data url prefix", u.startsWith("data:image/svg+xml;charset=utf-8,"), u.slice(0, 40));
  ok("percent-encoded (safe to put in src)",
    !u.includes("<") && !u.includes(">") && !u.includes('"'), u.slice(0, 80));
  ok("non-ascii survives the round trip",
    decodeURIComponent(u.split(",", 2)[1]).includes("图"));
  eq("empty -> still a valid (empty) data url",
    svgDataUrl(""), "data:image/svg+xml;charset=utf-8,");
}

// ── 3. 上限 ──────────────────────────────────────────────────────
{
  const at = "x".repeat(MAX_SVG_CHARS);
  ok("exactly at the cap passes", !tooLarge(at));
  ok("one char over fails", tooLarge(at + "x"));
  ok("cap is 200 KB", MAX_SVG_CHARS === 200 * 1024, String(MAX_SVG_CHARS));
}

// ── 4. 下载名（只是装饰，但别把路径带出去）────────────────────────
{
  eq("title wins",
    downloadName('<svg><title>销售 趋势</title></svg>'),
    "销售-趋势.svg");
  eq("prefix when no title", downloadName("<svg></svg>", "chart"), "chart.svg");
  eq("path separators are stripped",
    downloadName("<svg><title>a/b\\c:d</title></svg>"), "a-b-c-d.svg");
  ok("a huge title is truncated", downloadName(
    "<svg><title>" + "长".repeat(80) + "</title></svg>").length <= 46);
}

// ── 5. 消毒配置是承重的（源码级）──────────────────────────────────
{
  // 消毒判据住在 `svg.js`（`SvgPreview` 只是渲染层）
  const svgS = _src("svg.js");
  ok("DOMPurify is actually used", svgS.includes("p.sanitize("));
  ok("SMIL is explicitly allowed",
    svgS.includes('"animate"') && svgS.includes('"animateTransform"')
    && svgS.includes('"animateMotion"'));
  ok("script/foreignObject are explicitly forbidden",
    svgS.includes('"script", "foreignObject"'), "");
  // 现场订正：模型与 Mermaid 的样式都在 `<style>` 里，剥掉图就坏了。
  ok("style is explicitly ALLOWED (it carries the drawing's own CSS)",
    /ADD_TAGS:[^\]]*"style"/s.test(svgS)
    && !/FORBID_TAGS:[^\]]*"style"/s.test(svgS), "");

  const svgP = _src("SvgPreview.jsx");
  ok("render layer uses the shared sanitizer",
    /import\s*\{[^}]*sanitizeSvg[^}]*\}\s*from\s*"\.\/svg\.js"/s.test(svgP), "");
  // 判据是真的赋值/属性，不是那个词 —— 本文件的注释里就写着
  //    「innerHTML」三次（读文档的检查器会自己打自己，这条踩过）。
  ok("render goes through <img>, never injects DOM",
    svgP.includes("<img") && !svgP.includes(".innerHTML")
    && !svgP.includes("dangerouslySetInnerHTML"));

  const mm = _src("MermaidPreview.jsx");
  ok("mermaid is lazy-loaded (dynamic import, not top-level)",
    mm.includes('await import("mermaid")')
    && !/^import\s+mermaid/m.test(mm), "");
  ok("htmlLabels is off (foreignObject would fight the sanitizer)",
    mm.includes("htmlLabels: false"));
  ok("securityLevel strict", mm.includes('securityLevel: "strict"'));
  ok("mermaid output still goes through SvgPreview",
    mm.includes("<SvgPreview"));

  const md = _src("Markdown.jsx");
  ok("only the two literal fence languages are special-cased",
    md.includes('lang === "svg"') && md.includes('lang === "mermaid"'), "");
  ok("everything else stays a <pre>", md.includes('<pre key={i} className="md-pre">'));
}

console.log("svg-preview: " + pass + "/" + pass + " passed");
