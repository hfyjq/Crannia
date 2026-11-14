// 提问面板的纯函数读者—— 零依赖，node 直接跑。
//
//   跑法： node packages/ui/tests/ask-user.test.mjs
//   （`tests/test_ask_user.py` 会跑它；也可以在 `npm test` 那一族里手跑。）
//
// 这里钉的是前端那一半：单选/多选、键盘序号、回传体。
//   判据的权威在后端（`core/pipeline/stages/pre_process.py::_answer_ask_user`）。

import assert from "node:assert/strict";
import {
  buildAnswer, canSubmit, hintText, keyAtDisplayNumber, toggleKey,
} from "../src/ask-user-model.js";

let failed = [];
function check(label, ok, note = "") {
  if (ok) {
    console.log(`  [ok]   ${label}`);
  } else {
    failed.push(label);
    console.log(`  [FAIL] ${label}${note ? "  -- " + note : ""}`);
  }
}

const SINGLE = {
  kind: "ask_user", multi: false,
  options: [
    { key: "1", label: "方案 A", description: "先做报表" },
    { key: "2", label: "方案 B", description: "先做接口" },
    { key: "3", label: "方案 C", description: "" },
  ],
};
const MULTI = { ...SINGLE, multi: true };

// ── 1. 选择 ───────────────────────────────────────────────────────────
check("★ 单选：再点另一个 = **替换**", JSON.stringify(toggleKey(SINGLE, ["1"], "2")) === '["2"]');
check("★ 单选：点已选的那个仍然选中它（不变成「什么都没选」）",
      JSON.stringify(toggleKey(SINGLE, ["1"], "1")) === '["1"]');
check("★★ 多选：点一下 = 加上", JSON.stringify(toggleKey(MULTI, ["1"], "3")) === '["1","3"]');
check("★★ 多选：再点一下 = 取消", JSON.stringify(toggleKey(MULTI, ["1", "3"], "3")) === '["1"]');
check("★ 不存在的键不会被塞进去", JSON.stringify(toggleKey(SINGLE, ["1"], "9")) === '["1"]');
check("★ 空载荷不炸（`payload == null`）", JSON.stringify(toggleKey(null, [], "1")) === "[]");

// ── 2. 键盘序号按显示位置 ─────────────────────────────────────────
check("★ 屏上第 2 个 = 真键位 2（这张表恰好一致）",
      keyAtDisplayNumber(SINGLE, 2) === "2");
const GAPPED = { ...SINGLE, options: [
  { key: "1", label: "A" }, { key: "6", label: "B" },
] };
check("★★ **显示序号 ≠ 键位**（第 2 个的真键位是 6）",
      keyAtDisplayNumber(GAPPED, 2) === "6", keyAtDisplayNumber(GAPPED, 2));
check("★ 越界回空串", keyAtDisplayNumber(SINGLE, 9) === "" && keyAtDisplayNumber(SINGLE, 0) === "");

// ── 3. 回传体 ─────────────────────────────────────────────────────────
check("★ 选了 → `{choice}`", JSON.stringify(buildAnswer(["1"], "").body) === '{"choice":"1"}');
check("★★ 多选 → `{choice: \"1,3\"}`（后端按逗号拆）",
      JSON.stringify(buildAnswer(["1", "3"], "").body) === '{"choice":"1,3"}');
check("★★ 只输入了文字 → `{text}`（**不选也算数** —— 那是「想先讨论」那条路）",
      JSON.stringify(buildAnswer([], "我想先看看数据").body) === '{"text":"我想先看看数据"}');
check("★ 文字优先于选中（用户写了字，就是那句话）",
      JSON.stringify(buildAnswer(["1"], "其实我想说").body) === '{"text":"其实我想说"}');
check("★ 两样都没有 → 不能提交", !buildAnswer([], "   ").ok);
check("★ `canSubmit` 与 `buildAnswer` 同一个判据",
      canSubmit([], "x") === true && canSubmit([], "") === false && canSubmit(["1"], "") === true);
check("★ 空白文字不算", !canSubmit([], "   "));

// ── 4. 提示语 ─────────────────────────────────────────────────────────
check("★ 单选提示说得出「回车确认」和「直接说」",
      /回车/.test(hintText(SINGLE)) && /想法/.test(hintText(SINGLE)));
check("★ 多选提示说得出「多个」", /多个/.test(hintText(MULTI)));
check("★ 空载荷不炸", typeof hintText(null) === "string");

console.log(`\n${failed.length === 0 ? "OK" : "FAILED: " + failed.join(", ")}`);
assert.equal(failed.length, 0);
