// `packages/ui/src/group-form.js` 的读者 —— 零依赖、node 直接跑。
//
//   node packages/ui/tests/group-form.test.mjs
//
// Python 那一侧由 `tests/test_desktop_groups.py` 接进全量 runner。
//
// 判据（用户逐字）：「配置群聊人数/是否有主理人……选择
//   是否自定义成员名称，不勾选就默认编号……最长 20 字符」。
//   下面每条边界都成对钉（刚好在界内 / 刚好跨过去）—— 这类 bug 永远长在边界上。
//
// 加的那一族（职责 / 模型 / 单成员保存）：
//   「无主理人存在时此为必须操作」，整表校验拒；而单成员保存允许中间态
//   （否则无主理人的群永远填不完）。

import assert from "node:assert/strict";
import {
  assignAvatarSlots, blankCreate, buildEditPayload, buildMemberPayload,
  buildPayload, canRoundtable, defaultMemberNames, draftAvatars, draftModels,
  draftNames, draftRoles,
  groupToForm, modelLabel, modelOptions,
  orderedMembers, overlongNames, overlongRoles, roundtablePayload, stepOneSummary,
  validate, validateBasics, validateMemberDraft,
} from "../src/group-form.js";

let pass = 0;
const failed = [];
function check(label, ok, note = "") {
  if (ok) { pass++; return; }
  failed.push(`${label}${note ? " -- " + note : ""}`);
}
const OPTS = { maxMembers: 8, maxName: 20 };
const LIMITS_G = { maxMembers: 8, maxName: 20, maxRole: 500 };
const ROLES3 = ["整理资料", "查数据", "写总结"];

// ── 默认编号 ────────────────────────────────────────────────────────
check("不勾自定义 -> worker1..workerN（**按位次**）",
      defaultMemberNames(3).join(",") === "worker1,worker2,worker3",
      defaultMemberNames(3).join(","));
check("★ 0 个人 -> 空表（不是 `worker1`）", defaultMemberNames(0).length === 0, "");
check("★ 主理人排在最前 = 第一个成员拿到 worker1（设计 §3.2「主理人在最前」）",
      buildPayload({ ...blankCreate(), count: 3, hasHost: true }, OPTS).members[0].host === true
      && buildPayload({ ...blankCreate(), count: 3, hasHost: true }, OPTS).members[0].name === "worker1", "");

// ── 自定义名字：填了的用填的，没填的回落到编号 ──────────────────
const custom = { ...blankCreate(), count: 3, hasHost: true, custom: true, names: ["师傅", "", "小飞"] };
check("★ 填了的用填的", draftNames(custom).join(",") === "师傅,worker2,小飞",
      draftNames(custom).join(","));
check("★ 没填的那一格回落到**它那一位**的编号（不是空串、不是 worker1）",
      draftNames(custom)[1] === "worker2", draftNames(custom)[1]);
check("★ 只有空白字符也算没填", draftNames({ ...custom, names: ["  ", "", ""] })[0] === "worker1",
      draftNames({ ...custom, names: ["  ", "", ""] })[0]);

// ── 20 字符那道线：两边都钉 ─────────────────────────────────────
const mk = (n) => ({ ...blankCreate(), count: 1, custom: true, names: ["x".repeat(n)] });
check("20 个字 -> 过", validate(mk(20), OPTS).ok === true, validate(mk(20), OPTS).error);
check("★ 21 个字 -> 拒（刚好跨过去）", validate(mk(21), OPTS).ok === false, "");
check("★ 标红的那个（`overlongNames`）与 `validate` 同一口径",
      overlongNames(mk(21), 20).length === 1 && overlongNames(mk(20), 20).length === 0, "");
check("★ 而**不勾自定义**时，占了 21 个字的编号**不算超长**（`worker1` 才 7 个字）",
      overlongNames({ ...blankCreate(), count: 1, custom: false }, 20).length === 0, "");
check("★ 人数调小之后，多出来那几格**不再参与**超长判定",
      overlongNames({ ...blankCreate(), count: 1, custom: true,
                      names: ["ok", "x".repeat(30)] }, 20).length === 0, "");

// ── 人数：1 / 8 / 9 三格 ────────────────────────────────────────────
check("1 个人 -> 过（后端也说至少一个）", validate({ ...blankCreate(), count: 1 }, OPTS).ok, "");
check("8 个人 -> 过", validate({ ...blankCreate(), count: 8 }, OPTS).ok, "");
check("★★ 9 个人 -> 拒（上限 8）", validate({ ...blankCreate(), count: 9 }, OPTS).ok === false, "");
check("★ 0 个人 -> 拒", validate({ ...blankCreate(), count: 0 }, OPTS).ok === false, "");
check("★ 上限是**外面传进来的**（不是写死的 8）—— 传 2 时 3 个人就拒",
      validate({ ...blankCreate(), count: 3 }, { maxMembers: 2, maxName: 20 }).ok === false, "");

// ── `blankCreate` 的默认值不许越界 ──────────────────────────────────
check("★ 默认人数在 1..上限 里（上限被调到 2 时，默认也得跟着夹）",
      blankCreate(2).count === 2 && blankCreate(8).count === 4, String(blankCreate(2).count));
check("★ `blankCreate(0)` 也回一个合法值（不是 0 —— 那会让「下一步」一进来就是灰的）",
      blankCreate(0).count === 1, String(blankCreate(0).count));

// ── 请求体的形状：与后端那一份同形 ──────────────────────────────
const body = buildPayload({ ...blankCreate(), name: " 示例群 ", count: 3, hasHost: true }, OPTS);
check("★ 群名 trim 过", body.name === "示例群", JSON.stringify(body.name));
check("★ `members` 三项都有（后端 normalize_members 要这几格）",
      body.members.every((m) => "name" in m && "model" in m && "role" in m && "host" in m),
      JSON.stringify(body.members[0]));
check("★★ 恰好一个主理人（且是第一个）",
      body.members.filter((m) => m.host).length === 1 && body.members[0].host === true, "");

//:无主理人时职责必填 —— 旧的"空职责也能建"那条已经作废。
const noHostEmpty = buildPayload({ ...blankCreate(), count: 3, hasHost: false }, OPTS);
check("★★ 无主理人 + 职责全空 -> **建不出来**（`null`）", noHostEmpty === null, "");

const noHost = buildPayload({ ...blankCreate(), count: 3, hasHost: false,
                              roles: ROLES3 }, OPTS);
check("★★ 勾掉主理人 -> **一个都没有**（用户逐字「也可以不要讲话的」）",
      noHost.members.filter((m) => m.host).length === 0, "");
check("★ 无主理人 + 职责都填了 -> 建得出，且职责进了请求体",
      noHost !== null && noHost.members.map((m) => m.role).join(",") === ROLES3.join(","),
      JSON.stringify(noHost && noHost.members));
check("★ 表单不合法 -> `null`（不发出一个注定 400 的请求）",
      buildPayload({ ...blankCreate(), count: 9 }, OPTS) === null, "");

// ── `G28` 改版：每张卡片的模型 / 头像 / 默认群名 ─────────────────────
check("★ 模型进了请求体（空 = 沿用会话）",
      buildPayload({ ...blankCreate(), count: 1, models: ["m11111111"] }, OPTS)
        .members[0].model === "m11111111", "");
check("★ 头像槽位进了请求体（预览与落库同一个值）",
      buildPayload({ ...blankCreate(), count: 1, avatars: [3] }, OPTS)
        .members[0].avatar === 3, "");
check("★ 没指定头像 -> `undefined`（JSON 会丢掉 ⇒ 后端随机）",
      buildPayload({ ...blankCreate(), count: 1, avatars: [] }, OPTS)
        .members[0].avatar === undefined, "");
check("★「群名称留空则默认编号」—— 空的时候用调用方给的默认名",
      buildPayload({ ...blankCreate() }, { ...OPTS, defaultName: "群聊2" }).name === "群聊2", "");
check("★ 默认名也没有 -> 空串（交给后端兜底成「未命名群聊」）",
      buildPayload({ ...blankCreate() }, OPTS).name === "", "");
check("★ `draftModels` / `draftAvatars` 按人数补位（缺的算空 / 未指定）",
      draftModels({ ...blankCreate(), count: 3, models: ["m1"] }).join(",") === "m1,,"
      && draftAvatars({ ...blankCreate(), count: 2, avatars: [5] })[1] === undefined, "");

// ── 头像不重复（用户「一个群别出现相同头像的」）────────
const uniq = (arr) => new Set(arr).size === arr.length;
check("★★ 新建表单的头像两两不同",
      uniq(blankCreate().avatars) && blankCreate().avatars.length === blankCreate().count, "");
check("★★ 8 个人 => 8 个不同槽位（0..7 全用上）",
      (() => { const a = assignAvatarSlots(8, []); return uniq(a) && a.sort((x, y) => x - y).join(",") === "0,1,2,3,4,5,6,7"; })(), "");
check("★ 已有的合法槽位**原位保留**（不该因为一次保存就换头像）",
      (() => { const a = assignAvatarSlots(3, [5, 1, 7]); return a[0] === 5 && a[1] === 1 && a[2] === 7; })(), "");
check("★★ 重复的槽位被换掉，且换成一个**没人用过**的",
      (() => { const a = assignAvatarSlots(2, [3, 3]); return a[0] === 3 && a[1] !== 3 && uniq(a); })(), "");
check("★ 缺的槽位补上（不返回 undefined）",
      (() => { const a = assignAvatarSlots(3, [2]); return a.length === 3 && a.every((x) => Number.isInteger(x)) && uniq(a); })(), "");
check("★★ 打开编辑页时给老账本去重（两个成员同一个头像 => 变成两个）",
      (() => {
        const f = groupToForm({ name: "x", members: [
          { name: "a", avatar: 4, role: "r" }, { name: "b", avatar: 4, role: "r" },
        ] }, { maxMembers: 8 });
        return f.avatars[0] === 4 && f.avatars[1] !== 4 && uniq(f.avatars);
      })(), "");

// ── 职责（`G28`）：边界 500 / 501，与整表校验同口径 ──────────────────
const roleAt = (n) => ({ ...blankCreate(), count: 1, hasHost: false,
                          roles: ["x".repeat(n)] });
check("500 个字 -> 过（后端上限 500）", validate(roleAt(500), OPTS).ok === true,
      validate(roleAt(500), OPTS).error);
check("★ 501 个字 -> 拒", validate(roleAt(501), OPTS).ok === false, "");
check("★ `overlongRoles` 与 `validate` 同一口径",
      overlongRoles(roleAt(501), 500).length === 1 && overlongRoles(roleAt(500), 500).length === 0, "");
check("★ 有主理人时职责可以空",
      validate({ ...blankCreate(), count: 2, hasHost: true }, OPTS).ok === true, "");
const missNames = validate({ ...blankCreate(), count: 2, hasHost: false,
                             custom: true, names: ["甲", "乙"] }, OPTS);
check("★★ 无主理人缺职责的错误话术**列出每一个**缺的人",
      missNames.ok === false && missNames.error.includes("甲") && missNames.error.includes("乙"),
      missNames.error);
check("★ 只缺一个时只点它一个",
      (() => { const v = validate({ ...blankCreate(), count: 2, hasHost: false,
                                    custom: true, names: ["甲", "乙"],
                                    roles: ["有", ""] }, OPTS);
               return v.ok === false && v.error.includes("乙") && !v.error.includes("甲"); })(), "");

// ── 第一步必须只用轻量校验（否则无主理人的群走不到第二步）──────
check("★★ 第一步校验：无主理人 + 职责全空也放行（第二步才有职责框）",
      validateBasics({ ...blankCreate(), count: 2, hasHost: false }, OPTS).ok === true, "");
check("★ 第一步仍会拦人数超限", validateBasics({ ...blankCreate(), count: 9 }, OPTS).ok === false, "");

//:预填常量已删，
//:   「主理人事先询问」改由提示词兜底（职责没写全时先问用户）。

// ── 第二步标题上那句回显 ────────────────────────────────────────────
check("有主理人 -> 摘要里说得出来",
      stepOneSummary({ count: 4, hasHost: true }).includes("有主理人"),
      stepOneSummary({ count: 4, hasHost: true }));
check("没有 -> 也说得出（不是同一句）",
      stepOneSummary({ count: 4, hasHost: false }) === "4 人 · 无主理人",
      stepOneSummary({ count: 4, hasHost: false }));

// ── 展示顺序（主理人最前，但保存用账本位次）──────────────
const ordered = orderedMembers([
  { name: "小飞", host: false, role: "", avatar: 1 },
  { name: "师傅", host: true, role: "", avatar: 2 },
  { name: "阿明", host: false, role: "", avatar: 3 },
]);
check("★★ 主理人排最前（展示）", ordered[0].member.name === "师傅", JSON.stringify(ordered.map((x) => x.member.name)));
check("★★ 但每项都带着**账本位次**（保存不能用展示顺序）",
      ordered[0].index === 1 && ordered[1].index === 0 && ordered[2].index === 2,
      JSON.stringify(ordered.map((x) => x.index)));
check("★ 没有主理人时保持原序", (() => {
  const o = orderedMembers([{ name: "a" }, { name: "b" }]);
  return o[0].index === 0 && o[1].index === 1 && o[0].member.name === "a";
})(), "");

// ── `G24` 尾巴②：编辑已有的群（2026-10-14）────────────────────────
//: 一个"建好了的"群 —— 形状照 `GET /api/groups` 回的那份。
const GROUP = {
  id: "g1a2b3c4",
  name: "资料整理组",
  members: [
    { name: "师傅", model: "deepseek-v4", role: "统筹", host: true, avatar: 3 },
    { name: "小飞", model: "", role: "查数据", host: false, avatar: 5 },
  ],
};

check("★ 反解成表单：名字**原样**，且 `custom: true`（不回落到 workerN）",
      groupToForm(GROUP, OPTS).names.join(",") === "师傅,小飞"
      && groupToForm(GROUP, OPTS).custom === true, "");
check("★ 有主理人 -> 表单里也勾着", groupToForm(GROUP, OPTS).hasHost === true, "");
check("★ 没有主理人 -> 不勾（不是恒 true）",
      groupToForm({ ...GROUP, members: [{ name: "a", host: false, avatar: 0, role: "x" }] }, OPTS).hasHost === false, "");
check("★ 群名与人数都带过来了（编辑时看得见原来是什么）",
      groupToForm(GROUP, OPTS).name === "资料整理组" && groupToForm(GROUP, OPTS).count === 2, "");
check("★ `G28`：职责也反解进表单",
      groupToForm(GROUP, OPTS).roles.join(",") === "统筹,查数据", JSON.stringify(groupToForm(GROUP, OPTS).roles));
check("★ `G28` 改版：模型与头像也反解进表单（卡片上要显示）",
      groupToForm(GROUP, OPTS).models[0] === "deepseek-v4"
      && groupToForm(GROUP, OPTS).avatars[0] === 3, "");

//:最要紧的一条：原样带回 avatar —— 后端 `_avatar_of(None)` 是随机的，
//:不带回，用户每编辑一次、头像就换一次（而他从没提过要换头像）。
const edited = buildEditPayload(GROUP, groupToForm(GROUP, OPTS), OPTS);
check("★★ 编辑**保留**每个成员的头像槽位（不带回 = 每次编辑换头像）",
      edited.members[0].avatar === 3 && edited.members[1].avatar === 5, JSON.stringify(edited.members));
check("★ 也保留 model / role（今天没有改它们的输入，不能丢）",
      edited.members[0].model === "deepseek-v4" && edited.members[1].role === "查数据", "");
check("★ 带回 id（端点靠它认哪个群）与 trim 过的群名",
      edited.id === "g1a2b3c4" && edited.name === "资料整理组", "");
check("★ 主理人仍是**第一个**（设计 §3.2；编辑不改这条拓扑）",
      edited.members[0].host === true && edited.members[1].host === false, "");

//:按位次保留 —— 改了第一个成员的名字，那个位置的头像不该跳。
const renamed = buildEditPayload(GROUP,
      { ...groupToForm(GROUP, OPTS), names: ["大师傅", "小飞"] }, OPTS);
check("★ 改名不换头像（按位次保留，不是按名字匹配）",
      renamed.members[0].avatar === 3 && renamed.members[0].name === "大师傅", "");

//:新加的成员没有"原位"，`avatar` = `undefined`（`JSON.stringify` 会丢那个键，后端随机）。
const grown = buildEditPayload(GROUP,
      { ...groupToForm(GROUP, OPTS), count: 3, names: ["师傅", "小飞", "新人"] }, OPTS);
check("★ 新加的成员没有原位 -> avatar 缺席（后端照随机走）",
      grown.members[2].avatar === undefined
      && JSON.parse(JSON.stringify(grown)).members[2].avatar === undefined, "");
check("★ 表单不合法 -> `null`（同建群那条）",
      buildEditPayload(GROUP, { ...groupToForm(GROUP, OPTS), count: 9 }, OPTS) === null, "");

//:老调用方（表单里没有 `roles` 键），必须回落到原职责，不许抹掉。
const legacy = { ...groupToForm(GROUP, OPTS) };
delete legacy.roles;
check("★★ 表单缺 `roles` 时回落到原职责（绝不静默抹掉用户写过的职责）",
      buildEditPayload(GROUP, legacy, OPTS).members[0].role === "统筹", "");
//:改版：表单里的模型/头像优先（用户改过就用新的）。
const remodelled = buildEditPayload(GROUP, {
  ...groupToForm(GROUP, OPTS), models: ["m11111111", ""], avatars: [7, 6],
}, OPTS);
check("★★ 编辑时表单里的模型/头像优先（改了就用新的）",
      remodelled.members[0].model === "m11111111" && remodelled.members[0].avatar === 7
      && remodelled.members[1].avatar === 6, JSON.stringify(remodelled.members));

// ── 单成员填单（`G28`）──────────────────────────────────────────────
check("★ 有主理人 + 职责空 -> 可以存（职责可选）",
      validateMemberDraft({ role: "", model: "" }, { hasHost: true }).ok === true, "");
check("★★ 无主理人 + 职责空 -> 拒", validateMemberDraft({ role: "  " }, { hasHost: false }).ok === false, "");
check("★ 无主理人 + 职责填了 -> 过",
      validateMemberDraft({ role: "查数据" }, { hasHost: false }).ok === true, "");
check("★ 单成员这一格也有 500 的边界（501 拒）",
      validateMemberDraft({ role: "x".repeat(501) }, { hasHost: true, maxRole: 500 }).ok === false, "");
check("★ 模型名超 80 也拒",
      validateMemberDraft({ role: "x", model: "m".repeat(81) }, { hasHost: true }).ok === false, "");

const memberBody = buildMemberPayload(GROUP, 1,
      { name: "小飞", role: "  查数据 + 出图  ", model: "m1234abcd" }, { hasHost: true });
check("★ 单成员请求体：只带 id / index / name / role / model",
      memberBody && memberBody.id === "g1a2b3c4" && memberBody.index === 1
      && memberBody.role === "查数据 + 出图" && memberBody.model === "m1234abcd",
      JSON.stringify(memberBody));
check("★ 位次越界 -> null", buildMemberPayload(GROUP, 9, { role: "x" }, { hasHost: true }) === null, "");
check("★ 位次不是整数 -> null", buildMemberPayload(GROUP, "1", { role: "x" }, { hasHost: true }) === null, "");
check("★★ 无主理人 + 空职责 -> null（前端先拦住，不发出注定 400 的请求）",
      buildMemberPayload({ ...GROUP, members: [{ name: "a", host: false, role: "" }] },
                         0, { role: "" }, { hasHost: false }) === null, "");

// ── 模型下拉（`G28`）：选项来自 `GET /api/groups` 的 `model_options` ─
const MO = modelOptions({ model_options: [
  { id: "m11111111", name: "便宜的", card_label: "Qwen", model: "qwen-max" },
  { id: "m22222222", name: "稳的", card_label: "", model: "x" },
  { id: "", name: "没有 id 的坏行" },
] });
check("★ 拍平成 [{id, label, model}]，并丢掉没有 id 的坏行",
      MO.length === 2 && MO[0].label === "便宜的（Qwen）" && MO[1].label === "稳的",
      JSON.stringify(MO));
check("★ 空值念成「沿用当前会话的模型」", modelLabel("", MO) === "沿用当前会话的模型", modelLabel("", MO));
check("★ 认得的 id 念名字", modelLabel("m11111111", MO) === "便宜的（Qwen）", "");
check("★★ 认不出的旧值**也要念出来**（念成空 = 用户一保存就静默清掉）",
      modelLabel("deepseek-v4", MO).includes("deepseek-v4"), modelLabel("deepseek-v4", MO));

// ────────────────────────────────────────────────────────────────────
// ── G31：群级 brief + 圆桌开关 ──────────────────────
check("blankCreate 默认：brief 空 / 圆桌关",
      blankCreate(8).brief === "" && blankCreate(8).roundtable === false);
check("★ 无主理人才能勾圆桌",
      canRoundtable({ hasHost: false }) === true &&
      canRoundtable({ hasHost: true }) === false);
check("★★ 有主理人时提交的 roundtable **强制 false**（不发一个注定 400 的请求）",
      roundtablePayload({ hasHost: true, roundtable: true }).roundtable === false);
check("★ 无主理人 + 勾了 -> true",
      roundtablePayload({ hasHost: false, roundtable: true }).roundtable === true);
check("★ brief 会 trim",
      roundtablePayload({ hasHost: false, brief: "  背景  " }).brief === "背景");

const rtForm = { ...blankCreate(8), count: 2, hasHost: false,
                 names: ["a", "b"], roles: ["r1", "r2"],
                 brief: "一起做报告", roundtable: true };
const rtBody = buildPayload(rtForm, LIMITS_G);
check("★★ 建群请求体带上 brief / roundtable",
      rtBody.brief === "一起做报告" && rtBody.roundtable === true,
      JSON.stringify(rtBody));
const rtEdit = buildEditPayload({ id: "g1", members: [{ name: "a" }, { name: "b" }] },
                                rtForm, LIMITS_G);
check("★ 改群请求体也带",
      rtEdit.brief === "一起做报告" && rtEdit.roundtable === true,
      JSON.stringify(rtEdit));
check("★★ 编辑页反解得到这两格",
      groupToForm({ name: "g", members: [{ name: "a" }],
                    brief: "背景", roundtable: true }).brief === "背景" &&
      groupToForm({ name: "g", members: [{ name: "a" }],
                    brief: "背景", roundtable: true }).roundtable === true);
check("★ 老账本没有这两格 -> 缺省（空 / 关）",
      groupToForm({ name: "g", members: [{ name: "a" }] }).brief === "" &&
      groupToForm({ name: "g", members: [{ name: "a" }] }).roundtable === false);

if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(`${pass + failed.length} tests: ${pass} passed, ${failed.length} failed`);
assert.equal(failed.length, 0);
