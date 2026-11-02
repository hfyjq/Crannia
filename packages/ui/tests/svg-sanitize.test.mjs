// 真的过一遍 DOMPurify（不是读源码猜）。
// 运行：node packages/ui/tests/svg-sanitize.test.mjs
//
// 为什么要有这一条：第一次给 `SvgPreview` 写的配置把 `<style>` 剥掉了，
//   而模型画的鹈鹕（实际探测）与 Mermaid 的产物都把样式写在 `<style>` 里
//   剥掉之后轮子变成实心黑圆、车架整条消失。那条不是靠读源码能发现的，
//   得拿真产物过一遍消毒器。
// 判据依赖"最终装进 `<img>`"（secure static mode）—— 这条也在这里写明。
import assert from "node:assert";
import { JSDOM } from "jsdom";
import createDOMPurify from "dompurify";
import { isSvgSource, sanitizeSvg } from "../src/svg.js";

let pass = 0;
function ok(name, cond, detail) {
  assert.ok(cond, name + (detail ? ": " + detail : ""));
  pass++;
}

const dom = new JSDOM("<!doctype html><html><body></body></html>");
const purify = createDOMPurify(dom.window);

const SAMPLE = `<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <style>.wheel{fill:none;stroke:#333;stroke-width:5}</style>
  <circle class="wheel" cx="50" cy="50" r="40" onclick="alert(1)"/>
  <script>alert("xss")</script>
  <foreignObject width="10" height="10"><div xmlns="http://www.w3.org/1999/xhtml">hi</div></foreignObject>
  <g><animateTransform attributeName="transform" type="rotate" from="0" to="360" dur="1s" repeatCount="indefinite"/>
     <line x1="10" y1="10" x2="90" y2="90"/></g>
</svg>`;

const clean = sanitizeSvg(SAMPLE, purify);
console.log("clean length:", clean.length);

ok("still an svg", isSvgSource(clean), clean.slice(0, 60));
ok("★ <style> survives (the drawing's own CSS)",
  /<style[^>]*>[\s\S]*\.wheel\{/.test(clean), clean.slice(0, 200));
ok("... and its declarations are intact",
  clean.includes("stroke-width:5") || clean.includes("stroke-width: 5"),
  clean.match(/\.wheel\{[^}]*\}/)?.[0] || "");
ok("SMIL animateTransform survives",
  /animatetransform/i.test(clean), "");
ok("script is gone", !/<\s*script/i.test(clean), "");
ok("event handler is gone", !/onclick/i.test(clean), "");
ok("foreignObject is gone", !/foreignObject/i.test(clean), "");
ok("the circle itself survives", /<circle/.test(clean), "");

// 不是 SVG 的东西不该被当成图（`sanitizeSvg` 的第二个出口）
ok("non-svg text -> empty", sanitizeSvg("hello <b>world</b>", purify) === "", "");
ok("empty svg -> empty", sanitizeSvg("", purify) === "", "");

console.log("svg-sanitize: " + pass + "/" + pass + " passed");
