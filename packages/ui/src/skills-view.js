// 技能页那一屏的纯判据 —— 谁显示 · 什么标 · 卸载按钮按下去走哪条路。
//
// 四条：
//   · 框架自带的不显示（技能页只保留 MCP 管理和用户新装的或读来的）
//   · 目录内（用户装在仓内 `skills/` 的），绿标「目录内」
//   · 外部来源（共享目录读来的），黄标「外部来源」
//   · 被屏蔽的，亮红 + 一个「解封」
//
// 为什么单独一个文件：本包没有 vitest/jest，而"写一条判据却不配测试"本仓库不认
//   所以写成纯函数，`node` 直接跑断言（形状照 `context.js` / `mode.js` 那两对）。
//
// 后端那三格（`source` / `builtin` / `blocked`）由 `web/server.py::_scan_skills`
//   一处算准 —— 这里不重算，只做映射（两套真相源会漂）。

/** 绿标：用户装在仓内 `skills/` 的。 */
import { t } from "./i18n.js";

export const BADGE_REPO = { text: "目录内", cls: "repo" };

/** 黄标：从共享目录读来的（不是我们目录里的东西）。 */
export const BADGE_EXTERNAL = { text: "外部来源", cls: "external" };

/** 别的 agent 留下的目录 —— 读到的技能按来源分开标。 */
export const BADGE_AGENTS = { text: "共享目录", cls: "external" };
export const BADGE_CLAUDE = { text: "Claude 目录", cls: "external" };
export const BADGE_CODEX = { text: "Codex 目录", cls: "external" };
export const BADGE_WORKBUDDY = { text: "WorkBuddy 目录", cls: "external" };

/** 外部来源的行为说明（技能页要写出来，不能只在文档里）。
 *
 *  判据：这些技能只进 `/` 命令面，不进模型上下文 ——
 *  用户有权知道"为什么模型没提过它"。 */
// 不要再把它抽成常量：`i18n_scan.py` 只认 `t("…")` 里的字面量，
//   走常量那一层它扫不到，英文界面里这句一直是中文。
//   这句话现在直接写在 `Skills.jsx` 的 `t("…")` 里。
export const EXTERNAL_SCOPE_NOTE = "这些技能只能用 `/` 调用，不会进入模型的上下文";

/** 外部来源点「卸载」时那句（后面拼上路径）。
 *
 *  键里不许带首尾空格：扫描器按行归一后落表，而运行时的 `t()` 用原串
 *  查，带着那个尾巴空格就永远查不到，界面上一直是中文
 *  （英文界面里这句原样是中文）。空格放在拼接处。 */
export const EXTERNAL_UNINSTALL_MSG = "这是外部应用的组件，暂不提供卸载。路径";

/** 这一条上不上技能页。
 *
 * `kind === "mcp"`，不上：MCP 有它自己那一栏（浮窗），
 *    两处都列就是两套真相源。
 * `builtin`，不上：内置技能直接不显示。
 */
export function isVisible(s) {
  if (!s) return false;
  if (s.kind === "mcp") return false;
  // 自带技能默认不显示（"你不需要管它"）——
  //   但需要用户填东西的那些必须显示：藏起来等于"这功能根本不存在"。
  //   判据是 `has_config`（manifest 声明了 `config` 段），不是名字白名单。
  if (s.builtin && !s.has_config) return false;
  return true;
}

/** 过滤 + 保序（顺序由后端给，这里不动它）。 */
export function visibleSkills(list) {
  return (list || []).filter(isVisible);
}

/** 那一枚来源标。先看 `origin`（细），没有就按 `source`（两值）回退 ——
 *  读不到一律算「目录内」（后端没报 = 旧版后端，它只有仓内那一个根，
 *  此时这个判断是对的）。 */
const _BY_ORIGIN = {
  agents: BADGE_AGENTS,
  claude: BADGE_CLAUDE,
  codex: BADGE_CODEX,
  workbuddy: BADGE_WORKBUDDY,
};

export function sourceBadge(s) {
  // 标签是模块级常量，取用时才翻（import 时翻会把语言定死）。
  const byOrigin = s && _BY_ORIGIN[s.origin];
  const b = byOrigin
    || ((s && s.source === "external") ? BADGE_EXTERNAL : BADGE_REPO);
  return { ...b, text: t(b.text) };
}

/** 点「卸载」走哪条路：`confirm`（真卸，要确认）· `external`（只念一句话）· `none`。 */
export function uninstallMode(s) {
  if (!s || s.builtin) return "none";
  return s.source === "external" ? "external" : "confirm";
}

/** `external` 那一路要念的那句话（带路径）。其余返回空串。 */
export function uninstallMessage(s) {
  if (uninstallMode(s) !== "external") return "";
  return t(EXTERNAL_UNINSTALL_MSG) + " " + ((s && s.path) || t("（未提供）"));
}

/** 被屏蔽了没有（读后端的屏蔽账本）。读不到，假（不冒充"有"）。 */
export function isBlocked(s) {
  return !!(s && s.blocked);
}

/** 收起时那枚红标上只写这三个字：不兼容的模块只显示已屏蔽，
 *  点击这个卡片可以展开看见详细的报错信息。
 *
 *  原因不是不见了 —— 它挪进展开那一行（`blockedText`），点开就是全文。
 *，「收起只说结论、点开才给证据」，两句话各自只在一个地方。
 *  与 `blockedText` 是两句不同的话，别合并：这句是标记，那句是详情。
 */
export const BLOCKED_MARK = "已屏蔽";

/** 红标上那句话：说清为什么被屏蔽（原因由账本给，缺了就说"原因未提供"）。 */
export function blockedText(s) {
  if (!isBlocked(s)) return "";
  const why = (s.blocked_reason || "").trim();
  return t("已被屏蔽") + (why ? "：" + why : t("（原因未提供）"));
}

// ── 「外部来源集合」 ─────────────────────────────────────────────────
//
// 外部来源读了很多技能（≥3）时，做成一个集合，类似 MCP 的分组：
//   集合外面显示黄标"外部来源"和路径，点进去是一个浮窗，展开里面的内容。
//   （典型场景：某个共享目录下有几十个同前缀的技能，例如飞书那一族。）
//
// 三条判据：
//   · 阈值是 `>= 3`（2 个仍然平铺）
//   · 只有外部来源成组 —— 仓内用户自己装的（绿标）仍一个一个平铺
//     （仓内那个"来源"就是仓库本身，路径信息没有用）
//   · 集合那一行的标题就是路径（`root_display`，零发明）——
//     不做任何名字映射：显示什么，就是从哪儿读的

/** 大于等于 3 才成集合。 */
export const GROUP_MIN = 3;

/** 这一条有没有资格进集合：外部来源且后端给了分组键。
 *
 * 没有 `root` 就不成组（旧版后端 / 没有那一格）—— 宁可平铺，
 *   也不在 `dirname(path)` 上重推一个键（那会造出第二套真相源）。
 */
export function isGroupable(s) {
  return !!(s && s.source === "external" && s.root);
}

/** 集合里被屏蔽的有几个（>0，那一行整卡亮红 + 标一句）。 */
export function groupBlockedCount(items) {
  return (items || []).filter(isBlocked).length;
}

/** 分组：保序地把列表摊成行（`{kind:"skill"}` / `{kind:"group"}`）。
 *
 * 判据：
 *   · 保序 —— 组出现在它第一个成员的位置，其余原样穿插（顺序由后端给）
 *   · 一个来源下 `>= GROUP_MIN` 个才成组；不够的按原样平铺
 *   · 只收 `isGroupable` 的（外部来源 + 有 `root`）
 *
 * 成对：`>=3` 与 `恰好 2` 两条都要钉 —— 只写一边的话，
 *   「全都成组」或者「全都不成组」总有一种能全绿。
 */
export function groupSkills(list) {
  const arr = list || [];
  const buckets = new Map();          // root -> 成员（按首次出现定序）
  for (const s of arr) {
    if (!isGroupable(s)) continue;
    if (!buckets.has(s.root)) buckets.set(s.root, []);
    buckets.get(s.root).push(s);
  }
  for (const [k, v] of buckets) if (v.length < GROUP_MIN) buckets.delete(k);

  const rows = [];
  const emitted = new Set();
  for (const s of arr) {
    const key = isGroupable(s) ? s.root : null;
    if (key && buckets.has(key)) {
      if (emitted.has(key)) continue;               // 已经在这一组里了
      const items = buckets.get(key);
      emitted.add(key);
      rows.push({
        kind: "group",
        root: key,
        title: items[0].root_display || key,
        items,
        blockedCount: groupBlockedCount(items),
      });
    } else {
      rows.push({ kind: "skill", skill: s });
    }
  }
  return rows;
}
