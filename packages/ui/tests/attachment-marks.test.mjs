// 正文里的短标记（`packages/ui/src/attachment-marks.js`）—— 零依赖，node 直接跑。
//
// 钉住三件事：
//   · 编号与附件清单同序（图片一栏、非图片一栏各自从 1 起）；
//   · 删掉中间一个之后，后面的标记跟着往前挪；
//   · 标记不在正文里的附件不随这条发出，并回报到 `dropped`。
//
// 输出一律 ASCII（GBK 控制台；一 print 中文就崩）。

import assert from "node:assert";
import { attachMark, renumberMarks, splitByMark } from "../src/attachment-marks.js";

let pass = 0;
const failed = [];
function check(label, cond) {
  if (cond) { pass += 1; console.log("  [ok]   " + label); }
  else { failed.push(label); console.log("  [!!]   " + label); }
}

const M_TEXT = "【文本";   // 【文本
const M_IMG = "【图片";    // 【图片
const M_FILE = "【文件";   // 【文件
const END = "】";                  // 】
const mark = (prefix, n) => prefix + n + END;

// ── (1) 三种标记的形状 ───────────────────────────────────────────
{
  check("text mark", attachMark("text", 1) === mark(M_TEXT, 1));
  check("image mark", attachMark("image", 2) === mark(M_IMG, 2));
  check("path mark", attachMark("path", 3) === mark(M_FILE, 3));
  check("unknown kind falls back to file",
    attachMark("weird", 4) === mark(M_FILE, 4));
}

// ── (2) 编号已经对得上时，一个字节不动 ──────────────────────────
{
  const files = [
    { name: "a", kind: "text", mark: mark(M_TEXT, 1) },
    { name: "b", kind: "path", mark: mark(M_FILE, 2) },
  ];
  const src = "x" + mark(M_TEXT, 1) + "y" + mark(M_FILE, 2) + "z";
  const r = renumberMarks(src, files, []);
  check("already numbered: text untouched", r.text === src);
  check("already numbered: marks untouched",
    r.files[0].mark === files[0].mark && r.files[1].mark === files[1].mark);
}

// ── (3) 删掉中间一个：后面的往前挪一格 ──────────────────────────
{
  // 剩下的是原来第 2 个（【文本2】），它现在该是第 1 个。
  const files = [{ name: "b", kind: "text", mark: mark(M_TEXT, 2) }];
  const r = renumberMarks("前面" + mark(M_TEXT, 2) + "后面", files, []);
  check("shift down: text renumbered",
    r.text === "前面" + mark(M_TEXT, 1) + "后面");
  check("shift down: mark renumbered", r.files[0].mark === mark(M_TEXT, 1));
}

// ── (4) 两个标记互换：一趟替换会撞车，两趟才对 ──────────────────
{
  const files = [
    { name: "a", kind: "text", mark: mark(M_TEXT, 2) },
    { name: "b", kind: "text", mark: mark(M_TEXT, 1) },
  ];
  const r = renumberMarks(mark(M_TEXT, 2) + "-" + mark(M_TEXT, 1), files, []);
  check("swap: both renumbered",
    r.text === mark(M_TEXT, 1) + "-" + mark(M_TEXT, 2));
  check("swap: marks follow",
    r.files[0].mark === mark(M_TEXT, 1) && r.files[1].mark === mark(M_TEXT, 2));
}

// ── (5) 标记已从正文里删掉：编号照更新，正文不凭空长出来 ────────
{
  const files = [{ name: "a", kind: "text", mark: mark(M_TEXT, 2) }];
  const r = renumberMarks("没有标记的正文", files, []);
  check("missing mark: text untouched", r.text === "没有标记的正文");
  check("missing mark: mark still renumbered to slot 1",
    r.files[0].mark === mark(M_TEXT, 1));
}

// ── (6) 图片那一栏单独编号 ──────────────────────────────────────
{
  const images = [
    { kind: "image", mark: mark(M_IMG, 2) },
    { kind: "image", mark: mark(M_IMG, 3) },
  ];
  const r = renumberMarks(mark(M_IMG, 2) + " " + mark(M_IMG, 3), [], images);
  check("images numbered 1..n",
    r.images[0].mark === mark(M_IMG, 1) && r.images[1].mark === mark(M_IMG, 2));
  check("images: text follows", r.text === mark(M_IMG, 1) + " " + mark(M_IMG, 2));
}

// ── (7) 发送前过滤：标记没了的附件不发，并回报 ──────────────────
{
  const images = [{ kind: "image", mark: mark(M_IMG, 1) }];
  const files = [
    { name: "kept", kind: "text", mark: mark(M_TEXT, 1) },
    { name: "dropped", kind: "text", mark: mark(M_TEXT, 2) },
    { name: "unmarked", kind: "path" },
  ];
  const r = splitByMark(mark(M_IMG, 1) + " " + mark(M_TEXT, 1), images, files);
  check("image kept", r.images.length === 1);
  check("marked file kept", r.files.length === 2
    && r.files[0].name === "kept" && r.files[1].name === "unmarked");
  check("dropped reported", r.dropped.length === 1 && r.dropped[0].name === "dropped");
}

// ── (8) 一个都没有标记被删：dropped 是空数组（不是 undefined）────
{
  const files = [{ name: "a", kind: "text", mark: mark(M_TEXT, 1) }];
  const r = splitByMark(mark(M_TEXT, 1), [], files);
  check("nothing dropped: empty array", Array.isArray(r.dropped) && r.dropped.length === 0);
}

if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(`${pass + failed.length} tests: ${pass} passed, ${failed.length} failed`);
assert.equal(failed.length, 0);
