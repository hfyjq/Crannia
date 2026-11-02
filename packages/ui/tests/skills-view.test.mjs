// `packages/ui/src/skills-view.js` 的读者 —— 零依赖、node 直接跑。
//
//   node packages/ui/tests/skills-view.test.mjs
//
// Python 那一侧由 `tests/test_desktop_skills.py` 接进全量 runner。
//
// 判据（用户逐字）：自带的不显示 · 目录内绿标 · 外部来源黄标 ·
//    被屏蔽的亮红。，下面每一对都要成对钉 —— 只写一边的话，
//    "全都显示"或者"全都不显示"总有一样能过。

import assert from "node:assert/strict";
import {
  BADGE_EXTERNAL, BADGE_REPO, BLOCKED_MARK, EXTERNAL_UNINSTALL_MSG, GROUP_MIN,
  blockedText, groupSkills, isBlocked, isGroupable, isVisible, sourceBadge,
  uninstallMessage, uninstallMode, visibleSkills,
} from "../src/skills-view.js";

let pass = 0;
const failed = [];
function check(label, got, want) {
  if (got === want) { pass++; return; }
  failed.push(`${label}: 得到 ${JSON.stringify(got)}，期望 ${JSON.stringify(want)}`);
}

const repo = { name: "mine", source: "repo", builtin: false, kind: "tool", path: "skills/mine" };
const ext = { name: "lark-base", source: "external", builtin: false, kind: "prompt",
              path: "/home/u/.agents/skills/lark-base" };
const built = { name: "office_parser", source: "repo", builtin: true, kind: "tool", path: "skills/office_parser" };
const mcp = { name: "_mcp_builtin_", source: "mcp", builtin: true, kind: "mcp", path: "" };

// ── 谁上页 ──────────────────────────────────────────────────────────
check("★ 用户装在仓内的 ⇒ 显示", isVisible(repo), true);
check("★ 外部来源的 ⇒ 显示", isVisible(ext), true);
check("★★ 框架自带的 ⇒ **不显示**", isVisible(built), false);
// 成对：需要用户配置的自带技能必须显示 —— 藏起来等于"这功能不存在"
//   （GitHub 那张卡曾被 `builtin` 规则吞掉，用户在页面上找不到填令牌的地方）。
check("★★ 自带 + 需要配置 ⇒ **显示**", isVisible({ ...built, has_config: true }), true);
check("★ 成对：自带 + 不需要配置 ⇒ 仍不显示",
      isVisible({ ...built, has_config: false }), false);
check("★ MCP 那条 ⇒ 不显示（它有自己那一栏）", isVisible(mcp), false);
check("★ 什么都没有 ⇒ 不显示，不抛", isVisible(null), false);
check("★★ 成对：过滤之后**还剩两条**（不是把整页清空）",
  visibleSkills([repo, ext, built, mcp]).map((s) => s.name).join(","), "mine,lark-base");
check("★ 过滤保序（顺序由后端给，这里不动）",
  visibleSkills([ext, repo]).map((s) => s.name).join(","), "lark-base,mine");
check("★ 空列表 ⇒ 空数组，不抛", visibleSkills(null).length, 0);

// ── 来源标 ──────────────────────────────────────────────────────────
check("★ 仓内 ⇒ 绿标「目录内」", sourceBadge(repo).text + "/" + sourceBadge(repo).cls, "目录内/repo");
check("★ 外部 ⇒ 黄标「外部来源」",
  sourceBadge(ext).text + "/" + sourceBadge(ext).cls, "外部来源/external");
check("★★ 两枚标**不是同一个**（只写一条的话，'永远绿'也全绿）",
  sourceBadge(repo).text !== sourceBadge(ext).text, true);
check("★ 后端没报 source（老服务端）⇒ 一律算「目录内」",
  sourceBadge({ name: "x" }).text, BADGE_REPO.text);

// ── 卸载那条路 ──────────────────────────────────────────────────────
check("★ 仓内 ⇒ 真卸（要确认）", uninstallMode(repo), "confirm");
check("★ 外部 ⇒ **只念一句话**", uninstallMode(ext), "external");
check("★ 自带 ⇒ 没有卸载按钮", uninstallMode(built), "none");
check("★ 什么都没有 ⇒ none，不抛", uninstallMode(null), "none");
check("★★ 那句**逐字**在（用户给的稿）",
  uninstallMessage(ext).startsWith(EXTERNAL_UNINSTALL_MSG), true);
check("★★ 而且**带上了路径**（不然用户不知道该去哪儿删）",
  uninstallMessage(ext).includes("/home/u/.agents/skills/lark-base"), true);
check("★ 路径读不到 ⇒ 说「未提供」，不印 undefined",
  uninstallMessage({ source: "external" }).includes("未提供"), true);
check("★ 成对：仓内那条**不产生**那句话（它走的是确认框）", uninstallMessage(repo), "");

// ── 屏蔽（`W170` 的账本）────────────────────────────────────────────
check("★ 没报 blocked ⇒ 不红（不冒充）", isBlocked(repo), false);
check("★ 报了 ⇒ 红", isBlocked({ ...repo, blocked: true }), true);
check("★ 红标上那句话带上原因",
  blockedText({ ...repo, blocked: true, blocked_reason: "handler.py 语法都不通" }).includes("语法都不通"),
  true);
check("★★ 成对：原因缺了 ⇒ 说「原因未提供」，**不印 undefined**",
  blockedText({ ...repo, blocked: true }).includes("未提供"), true);
check("★ 没屏蔽 ⇒ 那句话是空串（不渲染红标）", blockedText(repo), "");

// ── 收起时那枚红标（据反馈提的）────────────────
check("★★ 红标上**只有「已屏蔽」三个字**（用户逐字：「不兼容的模块只显示已屏蔽」）",
  BLOCKED_MARK, "已屏蔽");
check("★★ 成对：**详情那句还在**（原因没丢，它挪进展开那一行）",
  blockedText({ ...repo, blocked: true, blocked_reason: "handler.py 语法都不通" })
    .includes("语法都不通"), true);
check("★★ 而且两句**不是同一句**（一个当标记、一个当详情，别合并）",
  BLOCKED_MARK !== blockedText({ ...repo, blocked: true }), true);
check("★ 标记里**不带原因**（带上了它就又变成那枚长红标）",
  BLOCKED_MARK.includes("handler"), false);

// ── 外部来源集合 ──────────────────────────────────────────────
// 某个外部来源的技能多于等于 3 个时，做成一个集合。
const e1 = { name: "lark-a", source: "external", builtin: false, kind: "prompt",
             path: "/h/.agents/skills/lark-a", root: "/h/.agents/skills",
             root_display: "~/.agents/skills" };
const e2 = { ...e1, name: "lark-b", path: "/h/.agents/skills/lark-b" };
const e3 = { ...e1, name: "lark-c", path: "/h/.agents/skills/lark-c" };
const x1 = { name: "other-a", source: "external", builtin: false, kind: "tool",
             path: "/h/.other/skills/o-a", root: "/h/.other/skills",
             root_display: "~/.other/skills" };

check("★ 阈值常量就是 3（用户逐字「大于等于 3」）", GROUP_MIN, 3);
check("★ 外部来源 + 有 root ⇒ 可成组", isGroupable(e1), true);
check("★ 仓内那条 ⇒ 不可成组", isGroupable(repo), false);
check("★ 没给 root（老服务端）⇒ 不可成组，**不在 dirname 上重推**",
  isGroupable({ ...e1, root: "" }), false);
check("★ 什么都没有 ⇒ false，不抛", isGroupable(null), false);

check("★ 恰好 2 个 ⇒ **不成组**（两边都平铺）",
  groupSkills([e1, e2]).map((r) => r.kind).join(","), "skill,skill");
check("★★ 成对：第 3 个一到 ⇒ 收成**一个**集合",
  groupSkills([e1, e2, e3]).map((r) => r.kind).join(","), "group");
check("★ 集合里有几个成员", groupSkills([e1, e2, e3])[0].items.length, 3);
check("★★ 标题**就是路径**（零发明 —— 显示什么，就是从哪儿读的）",
  groupSkills([e1, e2, e3])[0].title, "~/.agents/skills");
check("★ 分组键是**机器可读**那一格（`root`，不是显示串）",
  groupSkills([e1, e2, e3])[0].root, "/h/.agents/skills");

check("★★ 只有**外部来源**成组 —— 仓内用户自己装的（绿标）哪怕有 root 也照样平铺",
  groupSkills([{ ...e1, name: "m1", source: "repo" },
               { ...e2, name: "m2", source: "repo" },
               { ...e3, name: "m3", source: "repo" }]).map((r) => r.kind).join(","),
  "skill,skill,skill");
check("★ 没 `root` ⇒ 不成组（三份都不成）",
  groupSkills([{ ...e1, root: "" }, { ...e2, root: "" }, { ...e3, root: "" }])
    .map((r) => r.kind).join(","), "skill,skill,skill");

check("★★ 混排：集合出现在它**第一个成员**的位置，其余原样穿插",
  groupSkills([e1, repo, e2, x1, e3]).map((r) => (r.kind === "group" ? "G" : r.skill.name))
    .join(","), "G,mine,other-a");
check("★ 两个来源各自成组（不是一个「大锅」）",
  groupSkills([e1, e2, e3, x1, { ...x1, name: "other-b" }, { ...x1, name: "other-c" }])
    .map((r) => r.kind).join(","), "group,group");
check("★ 空列表 ⇒ 空数组，不抛", groupSkills(null).length, 0);

const b1 = { ...e1, blocked: true, blocked_reason: "handler 语法都不通" };
check("★ 集合里被屏蔽的有几个（那一行据此亮红）",
  groupSkills([b1, e2, e3])[0].blockedCount, 1);
check("★★ 成对：一个都没被屏蔽 ⇒ 0（不是恒 1）",
  groupSkills([e1, e2, e3])[0].blockedCount, 0);

console.log(`skills-view.js 自检：${pass} 通过 / ${failed.length} 失败`);
for (const f of failed) console.log("  FAIL " + f);
process.exit(failed.length ? 1 : 0);
