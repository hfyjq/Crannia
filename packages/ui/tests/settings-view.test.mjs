// `packages/ui/src/settings-view.js` 的读者 —— 零依赖、node 直接跑。
//
//   node packages/ui/tests/settings-view.test.mjs
//
// Python 那一侧由 `tests/test_desktop_settings.py` 接进全量 runner。
//
// 判据：这是软警告，不是限制 —— 下面专门有一条钉它不吓唬人。

import assert from "node:assert/strict";
import {
  allowlistBody, isExtra, leafNotice, optionsFor, ROWS, rowsFor, saveBody,
  SECTIONS, THREAD_LIST_LIMIT, THREAD_NOTICE_AT, valueOf,
} from "../src/settings-view.js";

let pass = 0;
const failed = [];
function check(label, ok, note = "") {
  if (ok) { pass++; return; }
  failed.push(`${label}${note ? " -- " + note : ""}`);
}

// ── ① 还没装载过（读不到 ≠ 0）────────────────────────────────
const na = leafNotice(null);
check("★ 没有读数 -> ready=false（不是「显示 0 字符」）",
      na.ready === false && na.warn === false, JSON.stringify(na));
check("★★ 文案里**不出现「0 字符」**（那个 0 看起来像一个结论，`N1`/`W25a`）",
      !/0\s*字符/.test(na.text), na.text);
check("★ 说得出口「还没装载」", na.text.includes("还没装载"), na.text);
check("★ `loaded:false` 与 `leaf:null` **走同一条路**",
      leafNotice({ loaded: false, chars: 0, warn_chars: 40000, over: false }).ready === false, "");

// ── ② 装载了、没超 ─────────────────────────────────────────────
const okLeaf = leafNotice({ loaded: true, chars: 12345, warn_chars: 40000, over: false });
check("★ ready=true、warn=false", okLeaf.ready === true && okLeaf.warn === false,
      JSON.stringify(okLeaf));
check("★ 数字带千分位（12,345）", okLeaf.text.includes("12,345"), okLeaf.text);
check("★ 软告警线也带千分位（40,000）", okLeaf.text.includes("40,000"), okLeaf.text);
check("★ 边界：**恰好等于**阈值**不算超**（后端 `over` 说了算）",
      leafNotice({ loaded: true, chars: 40000, warn_chars: 40000, over: false }).warn === false, "");

// ── ③ 超了 ────────────────────────────────────────────────────
const over = leafNotice({ loaded: true, chars: 51234, warn_chars: 40000, over: true });
check("★★ 超了 -> warn=true（界面据此上告警色）",
      over.warn === true && over.ready === true, JSON.stringify(over));
check("★ 超了那句说得出「超过」", over.text.includes("超过"), over.text);
check("★ 51,234 也带千分位", over.text.includes("51,234"), over.text);

// ── ④ 它是提示不是限制 ─────────────────────────────────────
const ALL = [na.text, okLeaf.text, over.text].join(" | ");
check("★★ 三段文案里都不许出现「吓唬人」的词（它不拦任何东西）",
      !/无法保存|已达上限|请精简|必须|禁止|超出限制/.test(ALL), ALL);
check("★ 而「没超」那段明说「超了只提示，不限制」（否则用户以为这是上限）",
      okLeaf.text.includes("不限制"), okLeaf.text);

// ────────────────────────────────────────────────────────────────────
// ── 第 9 节（批次 4）：那张四栏的表 ─────────────
//    "每个键真接得上"那半在 `tests/test_desktop_settings.py`（要去读后端）；
//       这里只管表自己的形状与那些纯函数。

check("★ 四栏就是用户给的那四个、顺序也没变",
  SECTIONS.map((s) => s.label).join("/") === "工作设置/对话设置/安全设置/其他设置",
  SECTIONS.map((s) => s.label).join("/"));
check("★ 每一行都归在四栏之一、都有 id/label/kind",
  ROWS.every((r) => SECTIONS.some((s) => s.id === r.section) && r.id && r.label && r.kind),
  JSON.stringify(ROWS.filter((r) => !(r.id && r.label && r.kind)).slice(0, 3)));
check("★ id 不重号（重号 = React 的 key 会撞）",
  new Set(ROWS.map((r) => r.id)).size === ROWS.length);
check("★★ 每个 `toggle`/`choice`/`number` 都有 `key`，而 `act` 都有 `act`",
  ROWS.every((r) => (["toggle", "choice", "number"].includes(r.kind) ? !!r.key : true)
    && (r.kind === "act" ? !!r.act : true)),
  JSON.stringify(ROWS.filter((r) => ["toggle", "choice", "number"].includes(r.kind) && !r.key)));

//:上级开关关着，那几行不显示（`设置.txt` 末尾那条规矩）。
const off = { cross_zone_enabled: false, cascade_enabled: false, ice: { enabled: false } };
check("★★ 上级关着 -> 子行不出现（跨区那两行）",
  !rowsFor("work", off).some((r) => r.id === "auto_approve"),
  JSON.stringify(rowsFor("work", off).map((r) => r.id)));
check("★★ 上级打开 -> 子行出现", 
  rowsFor("work", { cross_zone_enabled: true }).some((r) => r.id === "auto_approve"));
check("★★ ICE 那两格是**嵌套键**（`ice.enabled`）—— 读得到才显示",
  !rowsFor("safe", off).some((r) => r.id === "ice_info")
  && rowsFor("safe", { ice: { enabled: true, info_protection: false } })
       .some((r) => r.id === "ice_info"));

//:嵌套键的写必须拼成后端收的那个形状（`{ice: {enabled: …}}`）。
check("★★ `saveBody` 把 `ice.enabled` 拼成 `{ice:{enabled:…}}`（后端收的是对象）",
  JSON.stringify(saveBody({ key: "ice.enabled" }, false)) === '{"ice":{"enabled":false}}',
  JSON.stringify(saveBody({ key: "ice.enabled" }, false)));
check("★ 扁平的键原样包一层",
  JSON.stringify(saveBody({ key: "stream_output" }, true)) === '{"stream_output":true}');
check("★★ 两颗「全部放行」走 `/api/allowlist`（`subject` 就是那一行 `*`）",
  JSON.stringify(allowlistBody({ allowlist: "egress" }, true))
    === '{"kind":"egress","action":"add","subject":"*"}'
  && allowlistBody({ allowlist: "command" }, false).action === "remove");

//:选项表从载荷里取，不是表里抄一份。
const s1 = { subagent_model_options: [{ id: "m1", name: "便宜的", card_label: "Qwen" }],
             framework_presets: [{ name: "Fern", summary: "最省" }],
             framework_preset_custom: "custom", modes: [{ key: "auto", label: "自动" }],
             effort_levels: [{ level: 3, name: "high", implemented: true }] };
check("★★ 子代理那份用的是**载荷里的名字**（表里不抄一份）",
  optionsFor(s1, { from: "subagent_model_options" })[0].label === "便宜的（Qwen）");
check("★ 框架预设：三条 + **「自定义」那一项**（它是状态，也得能选中）",
  optionsFor(s1, { from: "framework_presets" }).length === 2
  && optionsFor(s1, { from: "framework_presets" })[1].value === "custom");
check("★ 投入档带上「（未实现）」标记", 
  optionsFor({ effort_levels: [{ level: 6, name: "ultra", implemented: false }] },
             { from: "effort_levels" })[0].label.includes("未实现"));
check("★ 记忆接续三档的口径与后端一致（fresh / balanced / memory）",
  optionsFor({}, { from: "memory_continuity_options" }).map((o) => o.value).join(",")
    === "fresh,balanced,memory");

//: 「本版多出来的」判据只有一处（`isExtra`）—— 组件只问它。
check("★★ `isExtra` 认两种写法（行自带的 `extra` 与那张名单）",
  isExtra({ id: "mode_default", extra: true }) && isExtra({ id: "approval_wait" })
  && !isExtra({ id: "cross_zone" }));

//:没做 / 在别处那几格必须带 why（node 这侧也钉一条；后端那侧还有一条）。
check("★★ 「没做 / 在别处」每一格都写了为什么",
  ROWS.filter((r) => r.kind === "moved" || r.kind === "notdone").every((r) => r.why),
  JSON.stringify(ROWS.filter((r) => (r.kind === "moved" || r.kind === "notdone") && !r.why)));

check("★ `valueOf` 读得到嵌套键（`ice.enabled`），读不到回 undefined",
  valueOf({ ice: { enabled: true } }, { key: "ice.enabled" }) === true
  && valueOf({}, { key: "ice.enabled" }) === undefined);


// ────────────────────────────────────────────────────────────────────
// ── 保留期与上限的界面说明─────────────────────
//
//   设计稿 `GUI设计-2026-09-29.md §1.1` 那张表：
//     「保留期｜三十天前以及更久依然保留」·「上限｜后续在设置里注明」
//   所以整条 一直欠的就是把它从文档搬进界面。
//
//   这一段最要紧的判据是第 3 条（**不许出现 markdown 的 `**`**）：
//   那一格是 `<span>{row.note}</span>` 渲染的 —— 纯文本，星号会原样显示。
//   据反馈出现过同一个形状（卸载确认框里 `挪进系统回收站`）。

/** 把 `**` 当记号用，会在界面上显示成星号。**剥掉反引号里的那段**不算
 *  （那是发给模型的话，模型读 markdown）。
 *  抽成纯函数才能拿合成样本打它（本仓：判据自己也要能红）。 */
function markdownLeak(text) {
  return String(text).replace(/`[^`]*`/g, "").includes("**");
}

const noticeRow = ROWS.find((r) => r.id === "thread_notice");
check("★ 先自证：找得到 `thread_notice` 那一行", !!noticeRow, JSON.stringify(Object.keys(ROWS[0] || {})));

// ① 两个数钉死（改了它 = 一次要有人签字的改动，而不是静默漂）
check("★ 页长常量 = 30", THREAD_LIST_LIMIT === 30, String(THREAD_LIST_LIMIT));
check("★ 提醒阈值常量 = 50", THREAD_NOTICE_AT === 50, String(THREAD_NOTICE_AT));

// ② 那句话念的是常量（不是又抄一份字面量）
const noteText = (noticeRow && noticeRow.note) || "";
//:用户把这段说明改短了：只念提醒阈值，不再念页长。
//:   页长常量（`THREAD_LIST_LIMIT`）仍然存在并被 `App.jsx` 取用 —— 那两条在上面。
//:值改走 `noteVars`（`note` 是模板）—— i18n 的键是中文模板，
//:在模块级用模板串插值会把键变成 "…超过 50 时…"，表里查不到、英文界面回落中文。
check("★★ 说明里念的是提醒阈值",
      String((noticeRow && noticeRow.noteVars || {}).n) === String(THREAD_NOTICE_AT),
      JSON.stringify(noticeRow && noticeRow.noteVars));
// 「那两个数是从常量插进来的、不是又抄一份字面量」这一条在这里判不了 ——
//    运行时它已经是一个插好的字符串了（抄一份字面量会长得一模一样）。
//    所以它由 `tests/test_desktop_settings.py` 按源文本钉（同 `B13` 那条判据）。
//    我第一版在这里写了一条 `"ok"` 当判据的检查 —— 那是恒真的假检查，
//      正是本仓记过二十来回的形状。当场删掉了，留这行说明为什么不能长在这儿。

// ③ 纯文本：不许有 markdown 的 `**`
check("★★ 说明里不许出现 `**`（那一格是纯文本渲染，星号会**原样显示**）",
      !markdownLeak(noteText), noteText);
check("★★ 负对照：合成样本里那个 `**` 抓得住",
      markdownLeak("这句要**加粗**") && !markdownLeak("而 `**这个**` 是发给模型的，不算"), "");

// ④ 用户逐字给的文案：说清什么时候会出现提醒
check("★★ 说明里写的是提醒触发条件",
      /对话条目超过/.test(noteText) && /出现提醒/.test(noteText), noteText);

if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(`${pass + failed.length} tests: ${pass} passed, ${failed.length} failed`);
assert.equal(failed.length, 0);
