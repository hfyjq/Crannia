// 更新公告的解析与挑选（`packages/ui/src/update-notice.js`）—— 零依赖，node 直接跑。
//
// 钉住三件事：
//   · 仓库根那份 `更新公告.txt` 的形状能原样拆开（标题 / 小节 / 条目）；
//   · id 按数字比大小（`v1.0.9` 小于 `v1.0.10`，按字符串比是反的）；
//   · 只看没看过的那一份：看过就不弹。
//
// 输出一律 ASCII（GBK 控制台；一 print 中文就崩）。

import assert from "node:assert";
import { compareNoticeId, isPrerelease, latestNotice, parseNotice, pickUnseen }
  from "../src/update-notice.js";

let pass = 0;
const failed = [];
function check(label, cond) {
  if (cond) { pass += 1; console.log("  [ok]   " + label); }
  else { failed.push(label); console.log("  [!!]   " + label); }
}

// ── (1) 仓库根那份公告的形状 ────────────────────────────────────
const SAMPLE = [
  "更新公告-常规更新（2026/11/X）",
  "",
  "UI：",
  "1、修正了一个导致兼容性弹窗显示异常的问题",
  "2、对部分文案和翻译进行了调整",
  "",
  "性能与功能性：",
  "1、修正了工作区文件统计的一处误解，当配置了额外工作目录时，",
  "   统计结果现在会说明它包含哪些目录",
].join("\n");

{
  const r = parseNotice(SAMPLE);
  check("title is the first line", r.title === "更新公告-常规更新（2026/11/X）", r.title);
  check("two sections", r.sections.length === 2, JSON.stringify(r.sections.map((s) => s.head)));
  check("section heads keep no colon",
    r.sections[0].head === "UI" && r.sections[1].head === "性能与功能性",
    JSON.stringify(r.sections.map((s) => s.head)));
  check("items lose their numbering",
    r.sections[0].items[0] === "修正了一个导致兼容性弹窗显示异常的问题",
    r.sections[0].items[0]);
  check("continuation line joins the item above",
    r.sections[1].items.length === 1
    && r.sections[1].items[0].indexOf("统计结果现在会说明它包含哪些目录") > 0,
    JSON.stringify(r.sections[1].items));
}

// ── (2) 空文本 / 只有标题 ───────────────────────────────────────
{
  check("empty text: no title, no sections",
    parseNotice("").title === "" && parseNotice("").sections.length === 0, "");
  const t = parseNotice("只有标题\n");
  check("title only", t.title === "只有标题" && t.sections.length === 0, JSON.stringify(t));
}

// ── (3) 认不出的行不丢 ─────────────────────────────────────────
{
  const r = parseNotice("标题\nUI：\n1、第一条\n这是一句没有编号的话");
  check("unknown line appended instead of dropped",
    r.sections[0].items.length === 1
    && r.sections[0].items[0].indexOf("这是一句没有编号的话") > 0,
    JSON.stringify(r.sections[0].items));
}

// ── (4) 编号按数字比，不按字符串 ───────────────────────────────
{
  check("0.11.96 < 0.11.953 (string compare says the opposite)",
    compareNoticeId("v1.0.9", "v1.0.10") < 0,
    String(compareNoticeId("v1.0.9", "v1.0.10")));
  check("same id compares equal", compareNoticeId("v1", "v1") === 0, "");
  const list = [{ id: "v1.0.9", text: "" }, { id: "v1.0.10", text: "" }];
  check("latestNotice picks the numerically largest",
    latestNotice(list).id === "v1.0.10", latestNotice(list).id);
}

// ── (5) 只看没看过的那一份 ─────────────────────────────────────
{
  const list = [{ id: "v1", text: "a" }, { id: "v2", text: "b" }];
  check("unseen -> the latest one", pickUnseen(list, "") === null
    ? false : pickUnseen(list, "").id === "v2", "");
  check("already seen the latest -> nothing", pickUnseen(list, "v2") === null, "");
  check("seen an older one -> the latest still shows",
    pickUnseen(list, "v1") && pickUnseen(list, "v1").id === "v2", "");
  check("empty list -> null", pickUnseen([], "v1") === null, "");
}

// ── (6) 测试版不发公告 ─────────────────────────────────────────
{
  check("beta is a prerelease", isPrerelease("1.0.0-beta.1") === true, "");
  check("rc / dev too", isPrerelease("1.2.0-rc1") && isPrerelease("1.2.0-dev"), "");
  check("a release is not", isPrerelease("1.0.0") === false, "");
  check("empty / unknown is not (fail-open: 宁可多弹一次)",
    isPrerelease("") === false && isPrerelease(undefined) === false, "");
}

if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(`${pass + failed.length} tests: ${pass} passed, ${failed.length} failed`);
assert.equal(failed.length, 0);
