// `packages/ui/src/plugin-notice-view.js` 的读者 —— 零依赖、node 直接跑。
//
//   node packages/ui/tests/plugin-notice-view.test.mjs
//
// Python 那一侧由 `tests/test_plugin_notice_view.py` 接进全量 runner。
//
// 判据（用户逐字）：「有多个插件不支持时…此插件改成部分插件，
//    冲突栏显示多个名字和位置」。，成对钉：
//    恰恰一条，「此插件」· 两条以上，「部分插件」。
//    只写一边的话，「永远说部分」或者「永远说此」总有一种能全绿。

import assert from "node:assert/strict";
import {
  RUNTIME_CODE, SUBJECT_PLURAL, SUBJECT_SINGULAR, isRuntimeMissing, missingCommand,
  noticeFacts, noticeKey, noticeKind, noticeText, subjectWord,
} from "../src/plugin-notice-view.js";

let pass = 0;
const failed = [];
function check(label, got, want) {
  if (got === want) { pass++; return; }
  failed.push(`${label}: 得到 ${JSON.stringify(got)}，期望 ${JSON.stringify(want)}`);
}

const one = { owner: "skill:demo-broken", name: "demo-broken",
              path: "/p/skills/demo-broken", since: 1 };
const two = { owner: "skill:other-bad", name: "other-bad",
              path: "/p/skills/other-bad", since: 2 };
const three = { owner: "mcp:dead", name: "dead", path: "/p/mcp/dead", since: 3 };

// ── 主语：一条 vs 多条（成对）────────────────────────────────────
check("★ 恰好一条 ⇒「此插件」（**不许**为了省事一律说「部分」）",
  subjectWord([one]), SUBJECT_SINGULAR);
check("★★ 成对：两条 ⇒「部分插件」", subjectWord([one, two]), SUBJECT_PLURAL);
check("★ 三条也一样是「部分插件」", subjectWord([one, two, three]), SUBJECT_PLURAL);
check("★ 空数组也回单数（它压根不会渲染）", subjectWord([]), SUBJECT_SINGULAR);
check("★ `null` / `undefined` 不抛", subjectWord(null) + subjectWord(undefined),
  SUBJECT_SINGULAR + SUBJECT_SINGULAR);
check("★★ 两枚主语**不是同一个词**（只写一条的话，「永远此插件」也全绿）",
  SUBJECT_SINGULAR !== SUBJECT_PLURAL, true);

// ── 冲突栏那几格 ────────────────────────────────────────────────────
check("★ 一条 ⇒ 一格 name", noticeFacts([one])[0].name, "demo-broken");
check("★★ 两条 ⇒ **两格**",
  noticeFacts([one, two]).length, 2);
check("★ 而且**按原序**（顺序是账本给的，这里不动它）",
  noticeFacts([one, two]).map((f) => f.name).join(","), "demo-broken,other-bad");
check("★ 位置那一格也在（不是只有名字）",
  noticeFacts([one, two]).map((f) => f.path).join(","),
  "/p/skills/demo-broken,/p/skills/other-bad");
check("★ 空 ⇒ 空数组，不抛", noticeFacts(null).length, 0);

// ── 单条那一份的取值（与搬家前逐字同一套判据）──────────────────────
check("★ 缺 name ⇒ 退回 owner", noticeText({ owner: "skill:x" }).name, "skill:x");
check("★★ 什么都没有 ⇒「（未提供）」，**不打印 undefined**",
  noticeText(null).name + "/" + noticeText(null).path, "（未提供）/（未提供）");

// ── 音频那一格的依赖键 ──────────────────────────────────────────────
check("★ 整批当键（换掉任何一条都要重播）",
  noticeKey([one, two]), "skill:demo-broken|skill:other-bad");
check("★★ 换掉**头一条**键必须变 —— 拿 `entries[0]` 当键时它会不变、声音不响",
  noticeKey([two]) !== noticeKey([one]), true);
check("★ 空 ⇒ 空串，不抛", noticeKey(null), "");

// ── 「缺运行环境」不许被说成「不兼容」──────────────────
//
// 现场：没装 Node，npx 型 MCP 连不上，界面说「不支持 / 已被屏蔽」，
// 而真相是"这台机器少个程序"，装上就好。两件事的做法完全相反。
const noNode = { owner: "mcp:files", name: "files", path: "",
                 code: RUNTIME_CODE, reason: "需要 Node.js",
                 detail: "command=npx", since: 4 };
const badPkg = { owner: "skill:broken", name: "broken", code: "handler_missing",
                 path: "/p/skills/broken", since: 5 };

check("★ 缺运行环境认得出", isRuntimeMissing(noNode), true);
check("★ 真不兼容不会被认成缺环境（反过来也钉住）", isRuntimeMissing(badPkg), false);
check("★ 老账目没有 code ⇒ 不算缺环境（向后兼容）",
  isRuntimeMissing({ owner: "skill:x" }), false);

check("★★ 全是缺环境 ⇒ `runtime`（标题换成「运行环境缺失」）",
  noticeKind([noNode]), "runtime");
check("★★ 一条都不缺 ⇒ `incompatible`（原来那一屏，一个字不改）",
  noticeKind([badPkg, one]), "incompatible");
check("★★ 混着 ⇒ `mixed`（**不许**拿其中一种的口气盖住另一种）",
  noticeKind([noNode, badPkg]), "mixed");
check("★ 空数组 ⇒ `incompatible`（它压根不会渲染）", noticeKind([]), "incompatible");

check("★★ 命令名从 `detail` 里抠得出来（后端只写 ASCII）",
  missingCommand(noNode), "npx");
check("★ 抠不到 ⇒ 空串（调用方回落到 reason），**不抛**",
  missingCommand(badPkg) + "|" + missingCommand(null), "|");
check("★ 带引号的旧写法不会抠出半个命令", missingCommand({ detail: "command='npx'" }), "");

const fact = noticeText(noNode);
check("★★ 单条那一份同时带出 reason / runtime / command 三样",
  [fact.runtime, fact.command, fact.reason].join("|"), "true|npx|需要 Node.js");
check("★ 不兼容那条的 `runtime` 是 false（两栏据此分开画）",
  noticeText(badPkg).runtime, false);

console.log(`plugin-notice-view.js 自检：${pass} 通过 / ${failed.length} 失败`);
for (const f of failed) console.log("  FAIL " + f);
process.exit(failed.length ? 1 : 0);
