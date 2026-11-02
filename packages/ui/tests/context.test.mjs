// `packages/ui/src/context.js` 的读者 —— 零依赖、node 直接跑。
//
//   node packages/ui/tests/context.test.mjs
//
// Python 那一侧由 `tests/test_desktop_context.py` 接进全量 runner。
//
// 判据（/ `N10`）：
//    ① 读不到一律是「—」，不是 0 ② 分母要说清（来源跟着值走）
//    ③ 「与上次比」那一列才是信号，而第一格永远读不到（没有"上次"）
//    所以下面每一条"读不到"都与"量到了是 0"成对钉 —— 这两种情况
//    长得一样（都印一个数），而它们要讲的不是同一句话。

import assert from "node:assert/strict";
import { cellLabel, cellRows, ctxPanelCloses, deltaText, fmtInt, fmtTok, noteText, readoutText }
  from "../src/context.js";

let pass = 0;
const failed = [];
function check(label, got, want) {
  if (got === want) { pass++; return; }
  failed.push(`${label}: 得到 ${JSON.stringify(got)}，期望 ${JSON.stringify(want)}`);
}

// ── ① 读不到 ≠ 0 ────────────────────────────────────────────────────
check("★ `null` ⇒ 破折号，不是 0", fmtInt(null), "—");
check("★ `undefined` ⇒ 破折号", fmtInt(undefined), "—");
check("★ NaN ⇒ 破折号", fmtInt(NaN), "—");
check("★★ 而**真的 0** ⇒ 印 0（这两件事不许同形）", fmtInt(0), "0");
check("千分位", fmtInt(1423456), "1,423,456");
check("四位就分", fmtInt(1234), "1,234");
check("三位不分", fmtInt(999), "999");
check("负数的分法（`-` 后不插逗号）", fmtInt(-12345), "-12,345");

// ── ③ 「与上次比」那一格 ────────────────────────────────────────────
check("★ 读不到 ⇒ 破折号且不加类", JSON.stringify(deltaText(null)), '{"text":"—","cls":""}');
check("★ 没变（0）⇒ 印 0 且**不加类**（「没变」与「涨了」是两句话）",
  JSON.stringify(deltaText(0)), '{"text":"0","cls":""}');
check("涨 ⇒ `+N` 且 `up`", JSON.stringify(deltaText(1234)), '{"text":"+1,234","cls":"up"}');
check("跌 ⇒ 带符号的千分位且 `down`", JSON.stringify(deltaText(-1234)),
  '{"text":"-1,234","cls":"down"}');

// ── 顶栏那一行读数 ──────────────────────────────────────────────────
const base = { pct: 12, threshold_pct: 85, threshold_tokens: 212500, window: 250000,
  window_source: "声明(config.yaml)", messages: 7, factor: 0.8851 };
check("★ 常态 ⇒ 百分比 + 阈值百分比（token 绝对值只在悬停里）",
  readoutText(base).text, "上下文 12% / 阈值 85%");
check("★ 底下的 title 里**带来源**（判据 ②）",
  readoutText(base).title.includes("来源: 声明(config.yaml)"), true);
check("★ 而来源读不到时写 `?`，**不写空**（空 = 看着像没有这一栏）",
  readoutText({ ...base, window_source: "" }).title.includes("来源: ?"), true);
check("没读数 ⇒ 空态那一句", readoutText(null).text, "上下文 —");
check("★ 空态**也有 title**（要点得开、也要说清为什么是空的）",
  readoutText(null).title.length > 10, true);
check("★ `pct` 不是数字（后端没报）⇒ 也走空态",
  readoutText({ ...base, pct: undefined }).text, "上下文 —");
check("★ `pct: 0` 是**真读数**，不许当成空态（与上面那条成对）",
  readoutText({ ...base, pct: 0 }).text, "上下文 0% / 阈值 85%");
check("armed ⇒ 追加「接近触发」", readoutText({ ...base, armed: true }).text,
  "上下文 12% / 阈值 85% · 接近触发");
check("armed ⇒ 带类 `armed`", readoutText({ ...base, armed: true }).cls, "armed");
check("★ 本轮压过 ⇒ 整句换成进出（不是「上次压过」）",
  readoutText({ ...base, armed: true, compressed: { pre_tokens: 12345, post_tokens: 3100 } }).text,
  "本轮已压缩 12.3K tok→3.1K tok，现 12%");
check("★ 压过那条优先于 armed（同一轮两件事都真时，说更重要的那件）",
  readoutText({ ...base, armed: true, compressed: { pre_tokens: 12345, post_tokens: 3100 } }).cls,
  "compressed");
check("★ 半截 compressed（缺 post）⇒ **退回常态**，不印 `undefined`",
  readoutText({ ...base, compressed: { pre_tokens: 12345 } }).text,
  "上下文 12% / 阈值 85%");

// ── 面板那三列 ──────────────────────────────────────────────────────
const report = {
  estimated_tokens: 34567, window: 250000, window_source: "声明(config.yaml)",
  pct: 13.8, threshold_tokens: 212500, factor: 0.8851,
  cells: [
    { key: "system", label: "系统提示词", tokens: 12000, source: "实测", delta: 300 },
    { key: "messages", label: "对话历史", tokens: 20000, source: "实测", delta: null },
    { key: "skill_index", label: "技能索引", tokens: 241, source: "派生",
      delta: -41, subset_of: "messages" },
  ],
};
check("★ 子集行带 `↳ ` 前缀", cellLabel(report.cells[2]), "↳ 技能索引");
check("★ 非子集行**不带**", cellLabel(report.cells[0]), "系统提示词");
const rows = cellRows(report);
check("行数 = cells 数", rows.length, 3);
check("值那一格是精确值 + ` tok`", rows[0].val, "12,000 tok");
check("★ 涨的那一格", rows[0].delta + "/" + rows[0].deltaCls, "+300/up");
check("★ **读不到的那一格**（第一次打开时每一格都长这样）", rows[1].delta, "—");
check("跌的那一格", rows[2].delta + "/" + rows[2].deltaCls, "-41/down");
check("来源原样带出去（给悬停）", rows[0].source, "实测");
check("★ 没有 cells 时给空数组，不抛", JSON.stringify(cellRows(null)), "[]");
check("★ 没读数时 `tokens` 那一格也是破折号", cellRows({ cells: [{ key: "x", label: "x" }] })[0].val,
  "— tok");
check("合计那一行**说清窗口来源**", noteText(report).includes("250,000（声明(config.yaml)）"), true);
check("★★ 合计那一行**明说加总不含技能索引**（不说，读的人会自己去做一道减法）",
  noteText(report).includes("不含「技能索引」"), true);
check("没读数时合计是空串（面板不渲染它）", noteText(null), "");

// ── `fmtTok`：只用在那一句话里，所以只钉换档那两点 ──────────────────
check("999 tok", fmtTok(999), "999 tok");
check("★ 1000 ⇒ K", fmtTok(12345), "12.3K tok");
check("★ 1e6 ⇒ M", fmtTok(2500000), "2.5M tok");

// ── 点面板外面就关：`ctxPanelCloses`（据反馈）──────────
//:判据：面板里与读数那枚按钮上都不算外面（那两颗各自有自己的 onClick）。
//:这几条必须成对钉 —— 只钉"点别处，关"的话，一个永远返回 true
//:    的实现也全绿，而它会顺手把面板变成"点一下读数就关不上"。
const fake = (hits) => ({ closest: (sel) => (hits.includes(sel) ? {} : null) });
check("★★ 点在**面板里** ⇒ 不关（面板自己那个 ✕ 管这一半）",
  ctxPanelCloses(fake([".ctx-panel"])), false);
check("★★ 点在**读数那枚按钮**上 ⇒ 不关（它自己的 onClick 是开合；两边都响应就再也关不上）",
  ctxPanelCloses(fake([".ctx-readout"])), false);
check("★ 点在别处 ⇒ 关（这一条才是这次修的那半）", ctxPanelCloses(fake([])), true);
check("★ 拿不到 `closest`（合成事件 / 旧环境）⇒ 当成外面（关）：宁可多点一下，也别关不掉",
  ctxPanelCloses({}), true);
check("★ 连 target 都没有 ⇒ 也关，且不抛", ctxPanelCloses(null), true);

console.log(`context.js 自检：${pass} 通过 / ${failed.length} 失败`);
for (const f of failed) console.log("  FAIL " + f);
process.exit(failed.length ? 1 : 0);
