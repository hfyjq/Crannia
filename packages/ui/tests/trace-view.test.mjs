// 「逐条轨迹」的纯函数读者—— 零依赖，node 直接跑。
//
//   node packages/ui/tests/trace-view.test.mjs

import assert from "node:assert/strict";
import {
  emptyState, formatDuration, mergeRecords, preview, recordOk, recordTitle,
} from "../src/trace-view.js";

let failed = [];
function check(label, ok, note = "") {
  if (ok) {
    console.log(`  [ok]   ${label}`);
  } else {
    failed.push(label);
    console.log(`  [FAIL] ${label}${note ? "  -- " + note : ""}`);
  }
}

// ── 1. 标题与成败 ─────────────────────────────────────────────────────
check("★ 工具用名字", recordTitle({ kind: "tool", name: "read_file" }) === "read_file");
check("★ 思考固定一个词", recordTitle({ kind: "thinking" }) === "思考");
check("★ 工具 ok=true -> true", recordOk({ kind: "tool", ok: true }) === true);
check("★ 工具 ok=false -> false", recordOk({ kind: "tool", ok: false }) === false);
check("★★ 思考**没有** ok 这一格 -> null（不是'成功'）",
      recordOk({ kind: "thinking" }) === null);

// ── 2. 时长 ───────────────────────────────────────────────────────────
check("★ 秒级", formatDuration(1234) === "1.2s");
check("★ 毫秒级", formatDuration(340) === "340ms");
check("★ 负数/坏值 -> 空串（不猜 0）",
      formatDuration(-1) === "" && formatDuration("abc") === "" && formatDuration(null) === "");

// ── 3. 预览 ───────────────────────────────────────────────────────────
check("★ 换行压成空格", preview("a\nb\tc") === "a b c");
check("★ 超长加省略号", preview("x".repeat(130), 120).endsWith("…"));
check("★ 不超就原样", preview("  hi  ") === "hi");
check("★ 空值不炸", preview(null) === "");

// ── 4. 增量合并 ───────────────────────────────────────────────────────
const a = [{ seq: 1, name: "a" }, { seq: 2, name: "b" }];
const merged = mergeRecords(a, [{ seq: 2, name: "b" }, { seq: 3, name: "c" }]);
check("★ 按 seq 去重（不重复画同一行）", merged.length === 3, JSON.stringify(merged));
check("★ 顺序保持（旧的在前，新的追加）",
      merged.map((r) => r.seq).join(",") === "1,2,3");
check("★ 坏输入不炸", mergeRecords(null, null).length === 0);

// ── 5. 空态：没打开 ≠ 没有记录 ───────────────────────────────────
check("★★ 开关关着 -> `off`（界面说'去设置页打开'）",
      emptyState({ enabled: false, records: [] }) === "off");
check("★ 开着但没记录 -> `empty`", emptyState({ enabled: true, records: [] }) === "empty");
check("★ 有记录 -> 空态为空串", emptyState({ enabled: true, records: [1] }) === "");
check("★ 载荷还没到（null）-> 空串（读作'再等等'，不是'关着'）",
      emptyState(null) === "");

console.log(`\n${failed.length === 0 ? "OK" : "FAILED: " + failed.join(", ")}`);
assert.equal(failed.length, 0);
