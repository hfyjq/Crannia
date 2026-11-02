// 「新建群聊」那个向导的判据 —— 纯函数，node 直接断言。
//
//   新建群聊可配置群聊人数 / 是否有主理人；配置完成后下一项展开，
//   可选择是否自定义成员名称（不勾选就默认编号，勾选就展开一个列表填名字，
//   最长 20 字符），配置完成后加入上方群聊列表。
//
// 成员还有两项属性：职责与模型。
//   点击头像可以配置此成员，展开为填单：可填入一段话说明模型职责
//   （无主理人存在时此为必须操作，有主理人时可不填，主理人可事先询问用户是否
//   需要协助分工），此处也可以配置模型（需在端配置处有配置）。
//   所以判据分两条不许混用：
//     · `validate()`         = 整表（新建 / 整表编辑）：无主理人，职责必填，点名；
//     · `validateMemberDraft()` = 单成员（点头像填单）：只判这一格，允许中间态。
//   为什么单成员不套整表规则：套了的话，无主理人的群第一次填任何一个都会被拒
//   （其他成员还空着），永远填不完。后端两条路也是这么分的（`groups.normalize_members`
//   与 `groups.update_member`）。
//
// 为什么单独一个文件：这套东西的坏法全在边界上（人数 0 / 1 / 8 / 9 ·
//    名字空一格 · 名字 20 还是 21 个字 · 职责 500 还是 501），而组件里那些
//    `useState` 是断不了的。这里一个 React 都不 import（同 `avatar.js` /
//    `skills-view.js`）。
//
// 上限不在这里写死 —— 它们由后端给（`GET /api/groups` 的
//    `max_members` / `max_name` / `max_role`）。写死一份就是第二份真相源，
//    而上限会变。

/** 一个空白的建群表单。*maxMembers* 由后端给（不写死）。 */
import { t } from "./i18n.js";

export function blankCreate(maxMembers = 8) {
  //: 默认 4 人：够看出"这是个群"，又不至于一上来就八个输入框。
  //:用 `Math.min` 夹一下 —— 万一后端把上限调到 2，默认值不许越界
  //:   （越界的默认值会让"下一步"按钮一进来就是灰的，看起来像坏了）。
  const n = Math.max(1, Math.min(4, Number(maxMembers) || 1));
  return {
    name: "", count: n, hasHost: true,
    //:名字直接在小卡片里改（界面上没有独立的"自定义名称"复选框），恒为 true
    custom: true,
    names: [], roles: [],
    //:每张卡片还带模型与头像槽位。
    //:   `models` 存端配置条目 id（空 = 沿用会话）；`avatars` 存 0..7
    //:   —— 新建时随机分配，而且预览与落库是同一个值
    //:   （否则用户看到企鹅、建完变青蛙，界面在说谎）。
    models: [],
    //:头像不重复（一个群里不出现相同头像）。
    avatars: assignAvatarSlots(n, []),
    //:群级 brief + 圆桌开关。
    //:圆桌的前提是不设主理人（`hasHost === true` 时界面不给勾，
    //:   提交前也强制回落 `false` —— 两处都守，后端还有第三道）。
    brief: "",
    roundtable: false,
  };
}

/** 默认编号：`worker1` … `workerN`（按位次，主理人排在最前）。
 *
 *  这一套是名字，不是占位提示。
 *    主理人拿到的也是编号（不是"主理人"三个字）—— 那是零发明的做法：
 *    谁是主理人由那一格 `host` 说，界面上另外给它一枚角标。
 */
export function defaultMemberNames(count) {
  const n = Math.max(0, Number(count) || 0);
  return Array.from({ length: n }, (_, i) => `worker${i + 1}`);
}

/** 这一份表单将要发出去的成员名（自定义没填的那些回落到编号）。
 *
 *  回落是有意的：用户勾了自定义、只填了前两个名字 —— 剩下的该拿到编号，
 *    不是空串（空串会被后端当成"没填"、也回落成编号 —— 结果一样，
 *    但界面上要先显示得出来，否则用户以为那几格是空的）。
 */
export function draftNames(form) {
  const base = defaultMemberNames(form && form.count);
  if (!form || !form.custom) return base;
  const typed = form.names || [];
  return base.map((fallback, i) => String(typed[i] || "").trim() || fallback);
}

/** 这一份表单将要发出去的职责（按位次，空着就是空串 —— 不做回落）。
 *
 *  职责没有默认值：空着就是"还没写"，由 `validate` 判要不要拦
 *    （无主理人时拦、有主理人时放行）。给它编一句默认职责就是替用户决定。
 */
export function draftRoles(form) {
  const n = Math.max(0, Number((form && form.count) || 0));
  const typed = (form && form.roles) || [];
  return Array.from({ length: n }, (_, i) => String(typed[i] || "").trim());
}

/** 这一份表单将要发出去的模型 id（按位次；空串 = 沿用会话）。 */
export function draftModels(form) {
  const n = Math.max(0, Number((form && form.count) || 0));
  const typed = (form && form.models) || [];
  return Array.from({ length: n }, (_, i) => String(typed[i] || "").trim());
}

/** 这一份表单的头像槽位（按位次；不是整数 = 不指定，后端随机）。
 *
 *  新建时界面会给每个新卡片发一个随机槽位 —— 预览与落库是同一个值
 *    （否则用户看到企鹅、建完变成青蛙，界面在说谎）。
 */
export function draftAvatars(form) {
  const n = Math.max(0, Number((form && form.count) || 0));
  const typed = (form && form.avatars) || [];
  return Array.from({ length: n }, (_, i) => (
    Number.isInteger(typed[i]) && typed[i] >= 0 && typed[i] < 8 ? typed[i] : undefined));
}

/** 一个群里不重复的头像槽位（一个群不出现相同头像）。
 *
 *  规则：已有的合法槽位原位保留；重复的 / 缺失的，从没用过的槽位里随机补。
 *  成员上限 8 == 槽位数 8，永远补得齐（所以 `free.pop()` 不会撞 undefined）。
 *  存量数据里可能已有重复 —— 打开编辑页时会被这里去重，
 *    保存后落库；只读列表（不打开编辑页）仍显示原来的。
 */
export function assignAvatarSlots(count, existing = [], slots = 8) {
  const n = Math.max(0, Number(count) || 0);
  const src = Array.isArray(existing) ? existing : [];
  const used = new Set();
  const out = [];
  for (let i = 0; i < n; i++) {
    const v = src[i];
    if (Number.isInteger(v) && v >= 0 && v < slots && !used.has(v)) {
      out.push(v);
      used.add(v);
    } else {
      out.push(undefined);
    }
  }
  const free = [];
  for (let s = 0; s < slots; s++) if (!used.has(s)) free.push(s);
  for (let i = free.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [free[i], free[j]] = [free[j], free[i]];
  }
  for (let i = 0; i < n; i++) {
    if (out[i] === undefined) out[i] = free.pop();
  }
  return out;
}

/** 哪几个名字超长了（返回下标）—— 给界面当场标红用。
 *
 *  与 `validate` 同一处口径（`maxName`）：一个说"能建"、一个说"标红"的话，
 *    用户会看到红框却建得成（或者反过来）。`validate` 就是拿它判的。
 */
export function overlongNames(form, maxName) {
  if (!form || !form.custom) return [];
  const limit = Number(maxName) || 0;
  const out = [];
  (form.names || []).forEach((raw, i) => {
    if (i >= (form.count || 0)) return;          // 人数调小之后，多出来的那几格不算数
    if (String(raw || "").trim().length > limit) out.push(i);
  });
  return out;
}

/** 哪几个职责超长了（返回下标）。与 `validate` 同口径（`maxRole`）。 */
export function overlongRoles(form, maxRole) {
  const limit = Number(maxRole) || 0;
  const out = [];
  draftRoles(form).forEach((r, i) => {
    if (r.length > limit) out.push(i);
  });
  return out;
}

/** 向导第一步能不能进下一步：只判人数与群名。
 *
 *  第一步必须只用这一条 —— 第二步才有职责输入框；
 *    第一步就拿整表规则判的话，无主理人的群永远走不到第二步去填职责。
 */
export function validateBasics(form, { maxMembers = 8, maxName = 20 } = {}) {
  const n = Number(form && form.count);
  if (!Number.isInteger(n) || n < 1) return { ok: false, error: t("群聊至少要有 1 个成员") };
  if (n > maxMembers) return { ok: false, error: t("一个群至多 {max} 个成员", { max: maxMembers }) };
  const bad = overlongNames(form, maxName);
  if (bad.length) {
    return { ok: false,
             error: t("名字最长 {max} 个字符（第 {i} 个超了）", { max: maxName, i: bad[0] + 1 }) };
  }
  if (String((form && form.name) || "").trim().length > maxName) {
    return { ok: false, error: t("群名最长 {max} 个字符", { max: maxName }) };
  }
  return { ok: true, error: "" };
}

/** 这一份表单能不能建 / 能不能整表保存。回 `{ok, error}` —— `error` 是给用户看的一句话。
 *
 *  判据与后端逐条对齐（`core/groups.normalize_members`）：
 *    人数在 1..max · 名字不超 maxName · 群名不超 maxName ·
 *    职责不超 maxRole · 无主理人，每个成员的职责必填。
 *  它是前置提示，不是那唯一的判定 —— 真正的拒绝在后端（400 + 出处）。
 *    两边都判是有意的：这里管"按钮灰不灰"，那里管"数据合不合法"。
 */
export function validate(form, { maxMembers = 8, maxName = 20, maxRole = 500 } = {}) {
  const basics = validateBasics(form, { maxMembers, maxName });
  if (!basics.ok) return basics;
  const over = overlongRoles(form, maxRole);
  if (over.length) {
    const names = draftNames(form);
    return { ok: false,
             error: t("职责最长 {max} 个字符（「{who}」超了）",
                      { max: maxRole, who: names[over[0]] }) };
  }
  if (!(form && form.hasHost)) {
    const names = draftNames(form);
    const missing = draftRoles(form)
      .map((r, i) => (r ? "" : names[i]))
      .filter(Boolean);
    if (missing.length) {
      return {
        ok: false,
        error: t("没有主理人时职责必填：{who} 还没填 —— 在第二步补上，或者展开群聊条目逐个填写。",
                 { who: missing.join("、") }),
      };
    }
  }
  return { ok: true, error: "" };
}

/** 表单 → `POST /api/groups/create` 的请求体。与后端那一份同形。 */
export function buildPayload(form,
  { maxMembers = 8, maxName = 20, maxRole = 500, defaultName = "" } = {}) {
  const v = validate(form, { maxMembers, maxName, maxRole });
  if (!v.ok) return null;
  const names = draftNames(form);
  const roles = draftRoles(form);
  const models = draftModels(form);
  const avatars = draftAvatars(form);
  return {
    //: 「群名称（留空则默认编号）」—— 空的时候用调用方给的默认名
    //:   （如 `群聊2`），再退到后端那句 `未命名群聊`。
    name: String((form && form.name) || "").trim() || String(defaultName || "").trim(),
    //:群级背景 + 圆桌开关（有主理人时强制 false，见 `roundtablePayload`）。
    ...roundtablePayload(form),
    members: names.map((nm, i) => ({
      name: nm,
      model: models[i] || "",
      role: roles[i] || "",
      //:主理人永远是第一个 —— 所以它是 `i === 0` 而不是"某个人挑的"。
      //:   这跟后端那条"至多一个"互为两半：界面保证恰好一个（或零个），
      //:   后端保证不超一个。
      host: !!(form && form.hasHost) && i === 0,
      //:`undefined` 会被 `JSON.stringify` 丢掉，后端随机（新成员该有的行为）
      avatar: avatars[i],
    })),
  };
}

/** 人数 / 主理人那一步的摘要（第二步展开时在标题上回显一句）。 */
export function stepOneSummary(form) {
  const n = Number((form && form.count) || 0);
  return form && form.hasHost
    ? t("{n} 人 · 有主理人", { n })
    : t("{n} 人 · 无主理人", { n });
}

/** 展示顺序：主理人排最前，其余按账本位次。
 *
 *  返回 `[{member, index}]` —— `index` 是账本位次。
 *  保存必须用 `index`，不能用展示顺序：主理人排最前只是展示，
 *    账本里它可能不在 0 号位（存量数据 / 导入的群）。
 */
export function orderedMembers(members) {
  return (members || [])
    .map((member, index) => ({ member, index }))
    .sort((a, b) => ((b.member && b.member.host ? 1 : 0)
                   - (a.member && a.member.host ? 1 : 0)) || (a.index - b.index));
}

// 编辑已有的群 —— 建好之后可以在 GUI 里改

/** 一个已存在的群 → 同一个编辑表单。
 *
 *  判据是"反解成同一套表单"：改群与建群共用一屏，不另造第二套界面
 *    （抄一套 = 两处判据会各自漂）。
 *  有意把 `custom` 置 `true`：编辑时名字是已知的实值，不能让它回落到
 *    `workerN` 编号 —— 否则用户一打开编辑页、名字就"变回去了"。
 *  人数上限照旧由后端给（`maxMembers`）—— 不在这里写死。
 *  职责也一起反解；`askDivision` 编辑时默认不勾 —— 那是"保存后
 *    额外做一件事"，不该因为打开一次编辑页就自己打开。
 */
export function groupToForm(group, { maxMembers = 8 } = {}) {
  const members = (group && group.members) || [];
  const n = Math.max(1, Math.min(Number(maxMembers) || 1, members.length || 1));
  return {
    name: String((group && group.name) || "").trim(),
    count: n,
    hasHost: members.some((m) => m && m.host),
    custom: true,
    names: members.slice(0, n).map((m) => String((m && m.name) || "")),
    roles: members.slice(0, n).map((m) => String((m && m.role) || "")),
    models: members.slice(0, n).map((m) => String((m && m.model) || "")),
    //:存量数据里可能已有重复头像，打开编辑页时去重（见 `assignAvatarSlots`）。
    avatars: assignAvatarSlots(n, members.slice(0, n).map((m) => (m && m.avatar))),
    //:存量数据里没有这两格，缺省（brief 空 / 圆桌关）。
    brief: String((group && group.brief) || ""),
    roundtable: !!(group && group.roundtable),
  };
}

/** 圆桌开关能不能勾 —— 有主理人就不能。 */
export function canRoundtable(form) {
  return !(form && form.hasHost);
}

/** 建群 / 改群请求体里的那两格。`hasHost` 时 `roundtable` 强制 false
 *  （后端也会拒 —— 这里只是不发一个注定 400 的请求）。 */
export function roundtablePayload(form) {
  return {
    brief: String((form && form.brief) || "").trim(),
    roundtable: canRoundtable(form) && !!(form && form.roundtable),
  };
}

/** 编辑表单 → `POST /api/groups/update` 的请求体。
 *
 *  必须把原成员的 `model` / `role` / `avatar` 原样带回 ——
 *    端点的 `normalize_members` 对缺省的这三格是重新生成的
 *    （`avatar` 缺省 = 随机，见 `core/groups._avatar_of`），不带回来的话，
 *    用户每编辑一次、头像就换一次（而他从没提过要换头像）。
 *  按位次保留（第 i 个还是第 i 个），不按名字 —— 改名不该顺手把头像也换掉。
 *  职责以表单里的那份为准（`roles[i]`）；调用方没带 `roles` 时
 *    回落到 `prev[i].role`，绝不把已有职责抹掉。
 */
export function buildEditPayload(group, form, { maxMembers = 8, maxName = 20, maxRole = 500 } = {}) {
  const v = validate(form, { maxMembers, maxName, maxRole });
  if (!v.ok) return null;
  const names = draftNames(form);
  const roles = Array.isArray(form && form.roles) ? draftRoles(form) : null;
  const models = Array.isArray(form && form.models) ? draftModels(form) : null;
  const avatars = Array.isArray(form && form.avatars) ? draftAvatars(form) : null;
  const prev = (group && group.members) || [];
  return {
    id: (group && group.id) || "",
    name: String((form && form.name) || "").trim(),
    //:同上（缺席 = 后端不动那两格；这里总是带上 —— 表单是真相）。
    ...roundtablePayload(form),
    members: names.map((nm, i) => {
      const p = prev[i] || {};
      return {
        name: nm,
        //:表单里有就用表单的（用户可以改），没有就原样带回。
        model: models ? models[i] : String(p.model || ""),
        role: roles ? roles[i] : String(p.role || ""),
        host: !!(form && form.hasHost) && i === 0,
        avatar: avatars && avatars[i] !== undefined
          ? avatars[i]
          : (typeof p.avatar === "number" ? p.avatar : undefined),
      };
    }),
  };
}

// 单成员配置 —— 点头像展开的那个填单

/** 单成员填单能不能存。只判这一格（见文件头那两条判据的分工）。
 *
 *  `draft` = `{name, role, model}`；`hasHost` 由调用方从群里取。
 */
export function validateMemberDraft(draft, { hasHost = true, maxRole = 500, maxModel = 80 } = {}) {
  const role = String((draft && draft.role) || "").trim();
  const model = String((draft && draft.model) || "").trim();
  if (role.length > maxRole) {
    return { ok: false,
             error: t("职责最长 {max} 个字符（现在 {cur}）",
                      { max: maxRole, cur: role.length }) };
  }
  if (model.length > maxModel) {
    return { ok: false, error: t("模型名最长 {max} 个字符", { max: maxModel }) };
  }
  if (!hasHost && !role) {
    return { ok: false, error: t("这个群没有主理人 —— 职责不能空，写一句它负责什么。") };
  }
  return { ok: true, error: "" };
}

/** 单成员保存的请求体（`POST /api/groups/member`）。不合法 / 位次越界，`null`。
 *
 *  `index` 是账本位次（`orderedMembers` 给的那个），不是展示顺序。
 *  带上 `name`：后端拿它挡"两个窗口 + 旧位次"改错人。
 */
export function buildMemberPayload(group, index, draft,
  { hasHost = true, maxRole = 500, maxModel = 80 } = {}) {
  const members = (group && group.members) || [];
  if (!Number.isInteger(index) || index < 0 || index >= members.length) return null;
  const v = validateMemberDraft(draft, { hasHost, maxRole, maxModel });
  if (!v.ok) return null;
  return {
    id: (group && group.id) || "",
    index,
    name: String((draft && draft.name) || members[index].name || ""),
    role: String((draft && draft.role) || "").trim(),
    model: String((draft && draft.model) || "").trim(),
  };
}

/** 模型下拉的选项 —— 来自 `GET /api/groups` 的 `model_options`
 *  （后端由 `model_configs.names_payload()` 产出，与设置页那个子 agent 下拉同源）。
 *
 *  回 `[{id, label, model}]`；首项「沿用会话默认」由调用方补（空值）。
 */
export function modelOptions(data) {
  return ((data && data.model_options) || [])
    .filter((o) => o && o.id)
    .map((o) => ({
      id: o.id,
      label: o.card_label
        ? `${o.name || o.id}（${o.card_label}）`
        : String(o.name || o.id),
      model: String(o.model || ""),
    }));
}

/** 某个已存值在界面上怎么念。
 *
 *  认不出也要念出来（`未在端配置中找到（xxx）`）—— 存量数据里可能是
 *  供应商目录名、也可能是被删掉的条目 id；显示成空的话，用户一保存就会
 *  把它静默清掉（界面在说谎）。
 */
export function modelLabel(value, options) {
  const v = String(value || "");
  if (!v) return t("沿用当前会话的模型");
  const hit = (options || []).find((o) => o.id === v);
  return hit ? hit.label : t("未在端配置中找到（{id}）", { id: v });
}
