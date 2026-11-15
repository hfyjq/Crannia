// `packages/ui/src/mode.js` 的读者 —— 零依赖、node 直接跑。
//
//   node packages/ui/tests/mode.test.mjs
//
// Python 那一侧由 `tests/test_desktop_mode.py` 接进全量 runner。
//
// 判据（「标签在说谎」）：那枚 chip 显示的是这一段真正在跑的模式。
//    现场反馈：对话前「手动」、对话后「自动」，而模式从头到尾没变过
//    —— 变的只是标签。根因是"未冻结"时的兜底读了 `settings.default_mode`
//    （那个对象只在打开设置弹窗时才拉，恒为 `undefined`）。
//    所以下面每一对都要成对钉：「冻结值赢」与「没冻结时用配置值」不是同一句话。

import assert from "node:assert/strict";
import { chipMode, MODE_LABELS } from "../src/mode.js";

let pass = 0;
const failed = [];
function check(label, got, want) {
  if (got === want) { pass++; return; }
  failed.push(`${label}: 得到 ${JSON.stringify(got)}，期望 ${JSON.stringify(want)}`);
}

// ── 档表（与后端 `core/modes.py::_MODES` 同源）──────────────────────
check("四档、顺序 = 手动 / 自动 / 计划 / 完全权限",
  MODE_LABELS.map((m) => m.key).join(","), "manual,auto,plan,full_access");
check("中文名与后端同源",
  MODE_LABELS.map((m) => m.label).join(","), "手动,自动,计划,完全权限");
// 完全权限只能按会话开 —— 它出现在 chip 表里，但不在设置页
//    默认档的候选里（后端 `default_allowed=False`，`settings-view.js` 过滤）。

// ── chip 取哪一个 ───────────────────────────────────────────────────
check("★★ 已冻结 ⇒ 用**冻结值**（不是设置里那个）",
  chipMode({ mode_session: "plan", mode_configured: "auto" }), "plan");
check("★★ 没冻结 ⇒ 用**配置里那个**（这一段将要用的是它）",
  chipMode({ mode_session: null, mode_configured: "auto" }), "auto");
check("★ 现场那一条：配置是 auto、session 还没冻结 ⇒ **不许**是 manual",
  chipMode({ mode_configured: "auto" }), "auto");
check("★ 只给了 session ⇒ 用它",
  chipMode({ mode_session: "auto" }), "auto");
check("★ 冻结成 manual 就是 manual（**真值**，与「读不到」分开）",
  chipMode({ mode_session: "manual", mode_configured: "auto" }), "manual");
check("★ 两个都读不到 ⇒ manual（兜底那一格）", chipMode({}), "manual");
check("★ 整个 status 都没有 ⇒ 也 manual，且不抛", chipMode(null), "manual");
check("★ 空串与 `undefined` 一视同仁（`||` 那一串的判据）",
  chipMode({ mode_session: "", mode_configured: "" }), "manual");

console.log(`mode.js 自检：${pass} 通过 / ${failed.length} 失败`);
for (const f of failed) console.log("  FAIL " + f);
process.exit(failed.length ? 1 : 0);
