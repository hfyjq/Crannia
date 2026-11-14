// `packages/ui/src/avatar.js` 的读者 —— 零依赖、node 直接跑。
//
//   node packages/ui/tests/avatar.test.mjs
//
// Python 那一侧由 `tests/test_desktop_avatar.py` 接进全量 runner，
//   并且两边对账 `AVATAR_SLOTS` 与 `core/groups.py`（少一边就红）。
//
// 判据（用户）：「头像你就随机几个 svg 纯色图形就好」——
//   所以三条：八个槽位各是一个合法图形 · 8 个人不撞脸（形状+颜色都不重） ·
//   坏槽位不抛（后端将来多给一个，界面不该白屏）。

import assert from "node:assert/strict";
import { AVATAR_SLOTS, avatarFor, COLORS, SHAPES } from "../src/avatar.js";

let pass = 0;
const failed = [];
function check(label, ok, note = "") {
  if (ok) { pass++; return; }
  failed.push(`${label}${note ? " -- " + note : ""}`);
}

// ── 表的形状 ────────────────────────────────────────────────────────
check("八个槽位", AVATAR_SLOTS === 8, String(AVATAR_SLOTS));
check("形状表与槽位数一样长", SHAPES.length === AVATAR_SLOTS, String(SHAPES.length));
check("颜色表与槽位数一样长", COLORS.length === AVATAR_SLOTS, String(COLORS.length));
check("八个形状**名字互不相同**（不然「随机」会画出两张一样的）",
      new Set(SHAPES.map((s) => s.name)).size === AVATAR_SLOTS,
      SHAPES.map((s) => s.name).join(","));
check("★ 八个颜色**互不相同**（错开取模那版会让 8 个人里有同色的）",
      new Set(COLORS).size === AVATAR_SLOTS, COLORS.join(","));

// ── 每个槽位都画得出来 ──────────────────────────────────────────────
const ALLOWED = new Set(["circle", "rect", "polygon", "path"]);
//:几何数据不止一种写法 —— `polygon` 用 `points`、`path` 用 `d`、
//:   `circle` 用 `r`、`rect` 用 `width`。第一版只列了前两种，于是
//:`square` 这一格当场红（它就是 `rect`，一个都没有）——
//:判据是「至少有一个几何属性」，不是"有某一个特定的名字"。
const GEOM = ["points", "d", "r", "width"];
let allOk = true;
const seen = [];
for (let i = 0; i < AVATAR_SLOTS; i++) {
  const a = avatarFor(i);
  seen.push(`${a.shape.name}/${a.color}`);
  const hasPaint = GEOM.some((k) => a.shape.attrs[k] !== undefined);
  if (!ALLOWED.has(a.shape.el) || !hasPaint || !/^#[0-9a-f]{6}$/i.test(a.color)) allOk = false;
}
check("★ 每个槽位都是「合法 SVG 元素 + 有几何数据 + 十六进制纯色」，**不分叉**", allOk,
      seen.join(" "));
check("★★ 8 个人不撞脸（形状+颜色这一对**两两不同**）",
      new Set(seen).size === AVATAR_SLOTS, seen.join(" "));

// ── 确定性 + 归一化 ─────────────────────────────────────────────────
check("同一个槽位两次给同一个结果（**确定的**，不是每次渲染换一个）",
      JSON.stringify(avatarFor(3)) === JSON.stringify(avatarFor(3)));
check("★ 槽位 3 与槽位 4 不是一个（相邻槽位不许塌成一个）",
      avatarFor(3).shape.name !== avatarFor(4).shape.name, "");
check("★ 出界的槽位**绕回来**（8 -> 0），不抛",
      avatarFor(AVATAR_SLOTS).slot === 0, JSON.stringify(avatarFor(AVATAR_SLOTS)));
check("★ 负数绕回来（-1 -> 最后一个），不抛",
      avatarFor(-1).slot === AVATAR_SLOTS - 1, JSON.stringify(avatarFor(-1)));
check("★ 不是整数 -> 退到 0（**不抛**：后端多给一格不该让界面白屏）",
      avatarFor(undefined).slot === 0 && avatarFor("3").slot === 0
      && avatarFor(null).slot === 0, "");
check("★ 归一化后的 `slot` 一定落在表内",
      Array.from({ length: 40 }, (_, i) => avatarFor(i - 20))
        .every((a) => a.slot >= 0 && a.slot < AVATAR_SLOTS), "");

// ────────────────────────────────────────────────────────────────────
if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(`${pass + failed.length} tests: ${pass} passed, ${failed.length} failed`);
assert.equal(failed.length, 0);
