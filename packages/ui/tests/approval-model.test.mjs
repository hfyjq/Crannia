// `packages/ui/src/approval-model.js` 的读者 —— 零依赖、node 直接跑。
//
//   node packages/ui/tests/approval-model.test.mjs
//
// Python 那一侧由 `tests/test_desktop_approval.py` 接进全量 runner。
//
// 判据（，逐条来自 WebUI）：① 两步确认 ② 选中态 ③ 默认永不是升级项
//    ④ 键盘全套（含环绕）⑤ 升级项要标出来 ⑥ `expect==='text'` 换输入框。
//    外加两条拍的：✕ = 停靠（不是拒绝）·
//    "不同意"走选项表里的 key，不许猜。
//    所以每一条判据都配了它自己的反面（猜一个不存在的 key 必须 `ok:false`），
//    因为"猜"这件事在旧实现里恰好每次都对（所有表都从 1 连续）——
//    那是巧合，不是契约。

import assert from "node:assert/strict";
import {
  canSubmit, DOCK_LABEL, displayNumberOf, hintText, initialSel, keyAction,
  keyAtDisplayNumber, moveSel, optionClass, selectKey, submitBody,
} from "../src/approval-model.js";

let pass = 0;
const failed = [];
function check(label, got, want) {
  if (JSON.stringify(got) === JSON.stringify(want)) { pass++; return; }
  failed.push(`${label}: 得到 ${JSON.stringify(got)}，期望 ${JSON.stringify(want)}`);
}

// 三种真表（都取自 `core/approval.py::options_for` 的形状）
const plan = { request_id: "r1", kind: "plan", expect: "",
  options: [{ key: "1", label: "同意", is_escalation: false },
            { key: "2", label: "缩小范围", is_escalation: false },
            { key: "3", label: "不同意", is_escalation: false }],
  default_key: "1" };
const del = { request_id: "r2", kind: "delete", expect: "",
  options: [{ key: "1", label: "同意", is_escalation: false },
            { key: "2", label: "不同意", is_escalation: false },
            { key: "3", label: "改一改", is_escalation: false },
            { key: "4", label: "本次对话内自动放行", is_escalation: true }],
  default_key: "1" };
const ice = { request_id: "r3", kind: "ice_reveal", expect: "",
  options: [{ key: "1", label: "同意", is_escalation: false },
            { key: "2", label: "不同意", is_escalation: false },
            { key: "3", label: "只放行这一次", is_escalation: false },
            { key: "4", label: "我有别的话说", is_escalation: false }],
  default_key: "1" };
const texty = { request_id: "r4", kind: "delete", expect: "text",
  question: "你要改成什么？", options: [], default_key: "" };

// ── ③ 默认选中：永不是升级项 ────────────────────────────────────────
check("默认 = `default_key`", initialSel(plan), "1");
check("★ 服务端给的 `default_key` **指到升级项**时**也不选它**",
  initialSel({ ...del, default_key: "4" }), "1");
check("服务端没给 `default_key` ⇒ 第一个非升级项",
  initialSel({ ...plan, default_key: "" }), "1");
check("★★ `default_key` 指向**表里没有的 key** ⇒ 退回第一个非升级项（别选一个不存在的项）",
  initialSel({ ...plan, default_key: "9" }), "1");
check("全是升级项 ⇒ **一项都不选**（空串）",
  initialSel({ options: [{ key: "1", label: "x", is_escalation: true }], default_key: "1" }), "");
check("没有选项 ⇒ 空串，不炸", initialSel({}), "");

// ── ①选中：只选中，且必须在表里 ──────────────────────────────
check("选中表里有的", selectKey(plan, "3"), { ok: true, sel: "3" });
check("★★ 选中表里**没有**的 ⇒ 拒（这正是硬编码 \"4\" 在 `plan` 上撞的那一下）",
  selectKey(plan, "4"), { ok: false, sel: "" });
check("★ 而 `4` 在 `ice_reveal` 那张表里是**「我有别的话说」** —— 同一个数字两件事",
  selectKey(ice, "4"), { ok: true, sel: "4" });
check("中间态不给选项 ⇒ 选中一律拒", selectKey(texty, "1"), { ok: false, sel: "" });

// ── ④ 环绕移动 ─────────────────────────────────────────────────────
check("↓ 往后一格", moveSel(plan, "1", 1), { ok: true, sel: "2" });
check("★ ↓ 在最后一项 ⇒ **绕回第一项**", moveSel(plan, "3", 1), { ok: true, sel: "1" });
check("★ ↑ 在第一项 ⇒ **绕到最后一项**", moveSel(plan, "1", -1), { ok: true, sel: "3" });
check("★ 一项都没选时 ↓ ⇒ 从第一项起", moveSel(plan, "", 1), { ok: true, sel: "1" });
check("★ 一项都没选时 ↑ ⇒ 从最后一项起", moveSel(plan, "", -1), { ok: true, sel: "3" });
check("中间态 ⇒ 不动", moveSel(texty, "", 1), { ok: false, sel: "" });

// ── 键盘 → 动作（一处判准）────────────────────────────────────────
check("数字键 ⇒ 只选中（判据 ①）", keyAction(plan, "1", "2"),
  { action: "select", sel: "2" });
check("★ 数字键选表里没有的 ⇒ none（不是「选中了但发不出去」）", keyAction(plan, "1", "5"),
  { action: "none", sel: "" });
check("↓ ⇒ 移动", keyAction(plan, "1", "ArrowDown"), { action: "move", sel: "2" });
check("↑ ⇒ 移动", keyAction(plan, "2", "ArrowUp"), { action: "move", sel: "1" });
check("Enter ⇒ 提交", keyAction(plan, "1", "Enter"), { action: "submit", sel: "" });
check("★ Escape ⇒ **停靠**（不是拒绝）", keyAction(plan, "1", "Escape"), { action: "dock", sel: "" });
check("★ 别的键 ⇒ 什么都不做（不许顺手提交）", keyAction(plan, "1", "a"),
  { action: "none", sel: "" });
check("★★ **在别人的输入框里打字时** ⇒ 数字键不抢", keyAction(plan, "1", "2", { typing: true }),
  { action: "none", sel: "" });
check("★ 但在**我们自己的输入框**里回车 = 提交", keyAction(texty, "", "Enter", { typing: true }),
  { action: "submit", sel: "" });
check("★★ **停靠之后** ⇒ 一律 none（面板已经收起来了，别抢键）",
  keyAction(plan, "1", "1", { docked: true }), { action: "none", sel: "" });

// ── ⑥ 提交什么 ─────────────────────────────────────────────────────
check("常态提交选中的 key", submitBody(plan, "3", ""), { ok: true, body: { choice: "3" } });
check("★ 没选中 ⇒ 发不出去（别发一个必然 409 的）", submitBody(plan, "", ""),
  { ok: false, body: null });
check("★ 选中的 key 不在表里 ⇒ 也发不出去", submitBody(plan, "9", ""), { ok: false, body: null });
check("中间态提交文本", submitBody(texty, "", " 照旧 "), { ok: true, body: { text: "照旧" } });
check("★ 中间态**空文本** ⇒ 发不出去（空话不该提交）", submitBody(texty, "", "   "),
  { ok: false, body: null });
check("★★ 中间态**只发 text，不发 choice**（两者同发时服务端拿 choice，用户那句话会静默消失）",
  submitBody(texty, "1", "照旧"), { ok: true, body: { text: "照旧" } });
check("「确认」按钮的可用状态与它同源", canSubmit(plan, "2", ""), true);
check("★ 没选中时按钮是灰的", canSubmit(plan, "", ""), false);
check("★ 中间态空文本时也是灰的", canSubmit(texty, "", ""), false);

// ── ② ⑤ 类名 ───────────────────────────────────────────────────────
check("普通项", optionClass(plan.options[0], "1"), "approval-option sel");
check("未选中的普通项", optionClass(plan.options[1], "1"), "approval-option");
check("★ 升级项**要标出来**（不带提示就会被整个忽略）",
  optionClass(del.options[3], "1"), "approval-option escalate");
check("★ 升级项 + 选中（今天不会同时出现 —— 它永不被默认选中；但用户能点它）",
  optionClass(del.options[3], "4"), "approval-option escalate sel");
check("停靠后那颗药丸上的字**精确**是这四个字", DOCK_LABEL, "等待审批");

// ── 显示序号 ≠ 键位─────────────────────────
//
// 现场：`EGRESS_OPTIONS` 的键位是 `1,2,3,4,5,6`，而`5` 是升级项
// （`ask_index < 2` 时根本不在表里），第一次问的时候选项是 `1,2,3,4,6`
// 所以屏幕上跳号。
// 所以判据：屏幕上永远连续 `1..N`，数字键按位置选，而回传仍是真键位。
const gap = { request_id: "r5", kind: "egress", expect: "",
  options: [{ key: "1", label: "同意本次", is_escalation: false },
            { key: "2", label: "同意，本次对话内不再询问此目标", is_escalation: false },
            { key: "3", label: "不同意", is_escalation: false },
            { key: "4", label: "我有别的话说...", is_escalation: false },
            // 键位 `5` 缺席（升级项没出现）—— 这正是跳号
            { key: "6", label: "同意，且信任此站点，不再询问", is_escalation: false }],
  default_key: "1" };
const gapUp = { request_id: "r6", kind: "egress", expect: "",
  options: gap.options.slice(0, 4).concat([
    { key: "5", label: "同意，且本次对话内所有目标自动放行", is_escalation: true },
    { key: "6", label: "同意，且信任此站点，不再询问", is_escalation: false }]),
  default_key: "1" };

check("★ 自证：这个夹具**真的有跳号**（不然下面几条恒真）",
  gap.options.map((o) => o.key), ["1", "2", "3", "4", "6"]);
check("★★ 显示序号是**连续**的：第 5 项（屏幕上写 5）的真键位是 `6`",
  keyAtDisplayNumber(gap, 5), "6");
check("★ 反过来：键位 `6` 显示成第 `5` 个", displayNumberOf(gap, "6"), 5);
check("★★ 按 `5` ⇒ 选中**真键位 `6`**（信任此站点）",
  keyAction(gap, "", "5"), { action: "select", sel: "6" });
check("★★ 负对照：按 `6` ⇒ **什么都不做**（屏幕上只有 5 项；越界不许「选一个相邻的」）",
  keyAction(gap, "", "6"), { action: "none", sel: "" });
check("★★ 升级项出现后（键位 1..6）⇒ 按 `5` 才是「全部放行」`5`",
  keyAction(gapUp, "", "5"), { action: "select", sel: "5" });
check("★ 而那时按 `6` ⇒ 信任此站点 `6`",
  keyAction(gapUp, "", "6"), { action: "select", sel: "6" });

// ── 提示行 ─────────────────────────────────────────────────────────
check("常态提示", hintText(plan), "↑↓ 或数字选择，回车确认");
check("中间态提示", hintText(texty), "输入后回车确认");

console.log(`approval-model.js 自检：${pass} 通过 / ${failed.length} 失败`);
for (const f of failed) console.log("  FAIL " + f);
process.exit(failed.length ? 1 : 0);
