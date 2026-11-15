import React, { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Pencil, Trash2 } from "lucide-react";
import { jsonRequest } from "./api.js";
import { Avatar } from "./Avatar.jsx";
import { UpPicker } from "./UpPicker.jsx";
import { Confirm } from "./Confirm.jsx";
import {
  assignAvatarSlots, blankCreate, buildEditPayload, buildMemberPayload,
  buildPayload, draftAvatars, draftModels, draftNames, draftRoles, groupToForm, modelLabel,
  modelOptions, orderedMembers, overlongNames, overlongRoles, validate, validateMemberDraft,
} from "./group-form.js";

// 「协作群聊」那一屏。
//
//   新建群聊用规整的表格填写：群名称（留空则默认编号）、团队人数、
//   是否选举主理人；勾选主理人后浮现——在此输入对主理人的安排
//   （如对员工的分工要求和工作要点，主理人会在群聊创建后再次向用户确认）、
//   确认主理人【按钮】；确认后下面浮现——员工配置（前面多少人这里就给
//   对应数量-1 的小卡片）：每个小卡片内是随机头像和默认 workerN 名称、
//   模型配置按钮、职能，点击名称可以自定义，点击模型配置选择模型，
//   点击职能框输入对员工的安排、确认员工配置【按钮】——给一个弹窗，
//   弹窗从上到下包含此群名称、主理人、员工（各自显示名称和对应模型配置），
//   弹窗下面是 【创建群聊】【返回修改】，右上角 × 取消。
//   不选举主理人时直接进入员工配置页。
//
// 成员展开 / 配置：群组条目点击展开或收起（离开此页面时自动全部收起），
//   上头像下名字，省略号；点击头像配置此成员，职责（无主理人必填）+ 模型（端配置条目）。
//
// 判据全在 `group-form.js`（纯函数，node 逐条断言）—— 这里只管画。
//    上限（`max_members` / `max_groups` / `max_name` / `max_role`）都由后端给。
//
// 「离开此页面时自动全部收起」是白拿的：`App.jsx` 只在 `page === "groups"`
//    时挂载本组件，切走就卸载，组件内的 `openIds` / `memberEdit` 自己没了。

import { t } from "./i18n.js";
export function Groups({ client, notify, connected, onEnter, onGroupsChanged }) {
  //:`null` = 还没读到（与"读到了、一个群都没有"是两件事）
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [form, setForm] = useState(blankCreate);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(null);
  //:哪些群展开了（可以多个 —— 离开页面时自动全部收起）。
  const [openIds, setOpenIds] = useState({});
  //: 正在填单的成员：`{gid, index, name, role, model, error}`（`null` = 没开）。
  const [memberEdit, setMemberEdit] = useState(null);
  const [memberBusy, setMemberBusy] = useState(false);
  //: 模型下拉的开合（同一时间只开一个；与设置页那条同一个组件）。
  const [openPicker, setOpenPicker] = useState("");
  //:主理人那一段确认过没有（没确认就不浮现员工配置）。
  //:编辑已有群时直接算确认过 —— 那些值早就定下来了。
  const [hostConfirmed, setHostConfirmed] = useState(false);
  //: 确认弹窗（「确认员工配置」之后那张）。
  const [summary, setSummary] = useState(false);
  //:待解散的群（二次确认）。`null` = 没有在问。
  const [confirming, setConfirming] = useState(null);
  const [summaryError, setSummaryError] = useState("");

  const limits = {
    maxMembers: (data && data.max_members) || 8,
    maxName: (data && data.max_name) || 20,
    maxRole: (data && data.max_role) || 500,
    maxBrief: (data && data.max_brief) || 1000,
  };
  const maxGroups = (data && data.max_groups) || 5;

  async function load() {
    try {
      const d = await client.call("/api/groups");
      setData(d);
      setErr(d.error || "");
      //:左栏那一栏「群聊」跟这一份同源（`onGroupsChanged`）——
      //:   建/改/删之后不用等下一次轮询，左栏立刻对得上。
      if (onGroupsChanged) onGroupsChanged(d.groups || []);
    } catch (e) {
      setData({ groups: [], count: 0, error: e.message });
      setErr(e.message);
    }
  }
  useEffect(() => { load(); }, [client]);   // eslint-disable-line

  //:模型下拉是 `fixed` + portal 到 `body` 的 ——
  //:点空白 / Esc / 滚页面都必须能关掉（与 `Composer` 那两枚同一条规矩）。
  //:判据要同时认 `.picker-menu`：菜单已不在 `.popover-host` 里，
  //:   只认后者的话，按下菜单里的选项会被这一下先关掉（症状："点了没反应"）。
  //:滚动时直接关（不跟着重算位置）—— `fixed` 菜单不随页面走，
  //:   留着就会相对按钮"跟着跑"。
  useEffect(() => {
    if (!openPicker) return undefined;
    const close = (e) => {
      const node = e.target;
      if (node && node.closest && (node.closest(".popover-host") || node.closest(".picker-menu"))) return;
      setOpenPicker("");
    };
    const esc = (e) => { if (e.key === "Escape") setOpenPicker(""); };
    const onScroll = (e) => {
      const node = e.target;
      if (node && node.closest && node.closest(".picker-menu")) return;   // 菜单自己滚不算
      setOpenPicker("");
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [openPicker]);

  const groups = (data && data.groups) || [];
  const full = groups.length >= maxGroups;
  const verdict = validate(form, limits);
  const names = draftNames(form);
  const roles = draftRoles(form);
  const models = draftModels(form);
  const avatars = draftAvatars(form);
  const over = overlongNames(form, limits.maxName);
  const overRoles = overlongRoles(form, limits.maxRole);
  const opts = modelOptions(data);
  //: 「群名称（留空则默认编号）」—— 新建时的默认编号（编辑不动它）。
  const defaultName = t("群聊{groups.length + 1}", { "groups.length + 1": groups.length + 1 });
  const displayName = String((form && form.name) || "").trim()
    || (editing ? t("未命名群聊") : defaultName);
  //:员工位次：有主理人，从 1 开始（0 号是主理人）；没有，从 0 开始。
  const staffStart = form.hasHost ? 1 : 0;
  const staffIndices = Array.from(
    { length: Math.max(0, Number(form.count || 0) - staffStart) },
    (_, k) => staffStart + k);
  const staffVisible = !!editing || !form.hasHost || hostConfirmed;

  function startEdit(g) {
    setEditing(g);
    setForm(groupToForm(g, limits));
    setHostConfirmed(true);       // 编辑时那段早就定下来了
    setSummary(false);
    setErr("");
    setMemberEdit(null);
  }

  function cancelEdit() {
    setEditing(null);
    setForm(blankCreate(limits.maxMembers));
    setHostConfirmed(false);
    setSummary(false);
    setSummaryError("");
  }

  /** 一个模型下拉的选项：首项「沿用会话」+ 端配置条目。
   *
   *  已存值不在选项里（存量数据里的供应商目录名 / 被删掉的条目）时补一条
   *  看得见的选项 —— 否则按钮显示兜底文案，用户一保存就把它静默清掉（界面在说谎）。
   */
  function pickerOptions(current) {
    const out = [{ key: "", label: modelLabel("", opts) }]
      .concat(opts.map((o) => ({ key: o.id, label: o.label })));
    if (current && !opts.some((o) => o.id === current)) {
      out.push({ key: current, label: modelLabel(current, opts) });
    }
    return out;
  }

  async function submit() {
    const payload = editing
      ? buildEditPayload(editing, form, limits)
      : buildPayload(form, { ...limits, defaultName });
    if (!payload) {
      setSummaryError(verdict.error || t("表单还没填好"));
      return;
    }
    setBusy(true);
    setSummaryError("");
    try {
      const r = await client.call(
        editing ? "/api/groups/update" : "/api/groups/create",
        jsonRequest("POST", payload));
      notify((r && r.message) || (editing ? t("已保存") : t("群聊已创建")));
      const saved = (r && r.group) || editing;
      setSummary(false);
      if (editing) cancelEdit();
      else {
        setForm(blankCreate(limits.maxMembers));
        setHostConfirmed(false);
      }
      await load();
      //:创建后进群（开屏会显示主理人头像与说明框）。
      //:输入框不预填那句话；「主理人事先询问」仍由提示词兜底
      //:   （职责没写全时它会先问用户）。
      if (onEnter && saved) onEnter(saved);
    } catch (e) {
      setSummaryError(e.message);
      notify(e.message);
    } finally {
      setBusy(false);
    }
  }

  /** 点删除 = 先问一次（解散不可逆，一次点击不许发生）。 */
  function remove(g) {
    setConfirming(g);
  }

  /** 真的解散。只在确认之后调用。 */
  async function doRemove(g) {
    setBusy(true);
    try {
      const r = await client.call("/api/groups/delete", jsonRequest("POST", { id: g.id }));
      notify((r && r.message) || t("已解散"));
      setConfirming(null);
      await load();
    } catch (e) {
      notify(e.message);
    } finally {
      setBusy(false);
    }
  }

  /** 展开 / 收起一个群。多个可以同时开着（离开页面时全部收起）。 */
  function toggleGroup(g) {
    setOpenIds((m) => {
      const next = { ...m };
      if (next[g.id]) delete next[g.id];
      else next[g.id] = true;
      return next;
    });
    setMemberEdit((cur) => (cur && cur.gid === g.id ? null : cur));
  }

  /** 点头像 / 瓦片：展开这个成员的填单（再点一次收起）。 */
  function openMember(g, index, m) {
    setOpenIds((x) => ({ ...x, [g.id]: true }));
    setMemberEdit((cur) => (
      cur && cur.gid === g.id && cur.index === index
        ? null
        : { gid: g.id, index, name: m.name, role: m.role || "", model: m.model || "", error: "" }
    ));
    setOpenPicker("");
  }

  /** 保存一个成员的职责/模型（`POST /api/groups/member`）。 */
  async function saveMember(g) {
    const cur = memberEdit;
    if (!cur) return;
    const hasHost = (g.members || []).some((m) => m.host);
    const payload = buildMemberPayload(g, cur.index, cur, { ...limits, hasHost });
    if (!payload) {
      const v = validateMemberDraft(cur, { ...limits, hasHost });
      setMemberEdit((x) => ({ ...x, error: v.error }));
      return;
    }
    setMemberBusy(true);
    try {
      const r = await client.call("/api/groups/member", jsonRequest("POST", payload));
      notify((r && r.message) || t("已保存"));
      setMemberEdit(null);
      await load();
    } catch (e) {
      setMemberEdit((x) => ({ ...x, error: e.message }));
      notify(e.message);
    } finally {
      setMemberBusy(false);
    }
  }

  function setCount(n) {
    const v = Math.max(1, Math.min(limits.maxMembers, n));
    setForm((f) => {
      const names = (f.names || []).slice(0, v);
      const roles = (f.roles || []).slice(0, v);
      const models = (f.models || []).slice(0, v);
      //:头像不重复（一个群不出现相同头像）—— 已有的原位保留，
      //:   新加的从没用过的槽位里随机补。预览与落库同一个值。
      const avatars = assignAvatarSlots(v, f.avatars || []);
      return { ...f, count: v, names, roles, models, avatars };
    });
    setHostConfirmed(false);
  }

  function setName(i, v) {
    setForm((f) => {
      const next = (f.names || []).slice();
      while (next.length < f.count) next.push("");
      next[i] = v;
      return { ...f, names: next };
    });
  }

  function setRole(i, v) {
    setForm((f) => {
      const next = (f.roles || []).slice();
      while (next.length < f.count) next.push("");
      next[i] = v;
      return { ...f, roles: next };
    });
  }

  function setModel(i, v) {
    setForm((f) => {
      const next = (f.models || []).slice();
      while (next.length < f.count) next.push("");
      next[i] = v;
      return { ...f, models: next };
    });
  }

  /** 展开区：成员瓦片 + （可能的）成员填单。 */
  function renderBody(g) {
    const hasHost = (g.members || []).some((m) => m.host);
    const missing = (g.members || []).filter((m) => !String(m.role || "").trim());
    return (
      <div className="group-body" onClick={(e) => e.stopPropagation()}>
        {!hasHost && missing.length > 0 && (
          <p className="group-form-hint">
            {t("这个群没有主理人：还有 {n} 位成员的职责没填 （{names}）—— 保存群设置前必须补齐，点下面的成员逐个填写。",
               { n: missing.length, names: missing.map((m) => m.name).join("、") })}
          </p>
        )}
        <div className="group-members">
          {orderedMembers(g.members).map(({ member: m, index }) => (
            <button className="group-member" key={m.name} title={m.name}
                    onClick={() => openMember(g, index, m)}>
              <Avatar slot={m.avatar} name={m.name} size={44} title={m.name} />
              <span className="group-member-name" title={m.name}>{m.name}</span>
              {m.host ? <span className="group-member-badge">{t("主理人")}</span> : null}
              <span className={"group-member-role" + (String(m.role || "").trim() ? "" : " group-empty")}
                    title={m.role || t("职责未填")}>
                {m.role || t("职责未填")}
              </span>
            </button>
          ))}
        </div>
        {memberEdit && memberEdit.gid === g.id && (
          <div className="group-member-form">
            <div className="group-member-title">
              配置「{memberEdit.name}」
              {!hasHost && <span className="group-member-hint">{t("（没有主理人 ⇒ 职责必填）")}</span>}
            </div>
            <label className="group-member-field">
              <span>{t("职责（这个成员负责什么）")}</span>
              <textarea rows={3} maxLength={limits.maxRole}
                        value={memberEdit.role}
                        placeholder={t("例如：负责把资料整理成结论")}
                        onChange={(e) => setMemberEdit((x) => ({ ...x, role: e.target.value, error: "" }))} />
            </label>
            <div className="group-member-field">
              <span>{t("模型（来自端配置）")}</span>
              <UpPicker fixed name={"member-" + g.id + "-" + memberEdit.index}
                        label={t("成员模型")}
                        options={pickerOptions(memberEdit.model)}
                        value={memberEdit.model || ""}
                        open={openPicker === g.id + ":" + memberEdit.index}
                        onToggle={(o) => setOpenPicker(o ? g.id + ":" + memberEdit.index : "")}
                        onPick={(k) => { setMemberEdit((x) => ({ ...x, model: k, error: "" })); setOpenPicker(""); }}
                        disabled={memberBusy} />
            </div>
            {!opts.length && (
              <p className="group-member-note">
                {t("端配置里还没有模型配置 —— 先去「设置 → 端配置」建一条，这里才选得到； 留空就沿用当前会话的模型。")}
              </p>
            )}
            {memberEdit.error && <p className="group-member-error">{memberEdit.error}</p>}
            <div className="group-member-actions">
              <button className="skill-btn" disabled={memberBusy} onClick={() => saveMember(g)}>{t("保存")}</button>
              <button className="skill-btn" disabled={memberBusy}
                      onClick={() => setMemberEdit(null)}>{t("取消")}</button>
            </div>
          </div>
        )}
      </div>
    );
  }

  /** 一张「人」的小卡片（主理人与员工共用）—— 头像 / 名字 / 模型 / 职能。 */
  function personCard(i, { host = false } = {}) {
    return (
      <div className={"group-staff-card" + (host ? " group-staff-host" : "")} key={(host ? "h" : "s") + i}>
        <Avatar slot={avatars[i]} name={names[i]} size={40} title={names[i]} />
        <input className={"group-staff-name" + (over.includes(i) ? " over" : "")}
               value={(form.names || [])[i] || ""}
               maxLength={limits.maxName}
               placeholder={names[i]}
               onChange={(e) => setName(i, e.target.value)} />
        <UpPicker fixed name={"pick-" + (host ? "h" : "s") + i}
                  label={t("模型配置")}
                  options={pickerOptions(models[i])}
                  value={models[i] || ""}
                  open={openPicker === (host ? "h" : "s") + i}
                  onToggle={(o) => setOpenPicker(o ? (host ? "h" : "s") + i : "")}
                  onPick={(k) => { setModel(i, k); setOpenPicker(""); }}
                  disabled={busy} />
        <textarea className={"group-staff-role" + (overRoles.includes(i) ? " over" : "")}
                  rows={2} maxLength={limits.maxRole}
                  value={(form.roles || [])[i] || ""}
                  placeholder={host
                    ? t("在此填写对主理人的安排，例如分工要求和工作要点。")
                    : t("职能：这个员工负责什么")}
                  onChange={(e) => setRole(i, e.target.value)} />
      </div>
    );
  }

  if (!connected) return <p className="muted">{t("启动本地服务后查看群聊。")}</p>;

  return (
    <section className="page">
      <div className="page-head">
        <div><small>CRANNIA</small><h1>{t("协作群聊")}</h1></div>
      </div>

      {/* ── 最上面：已创建的群聊 ──────────────────────────────────── */}
      <div className="group-sec">
        <h2 className="group-sec-title">
          {t("群聊列表")} <span className="muted">{groups.length} / {maxGroups}</span>
        </h2>
        {data === null && <p className="muted">{t("读取中…")}</p>}
        {err && <p className="muted">{err}</p>}
        {data !== null && !err && !groups.length && (
          <p className="muted">{t("还没有群聊 —— 在下面建一个。")}</p>
        )}
        <div className="group-cards">
          {groups.map((g) => {
            const host = (g.members || []).find((m) => m.host);
            const open = !!openIds[g.id];
            return (
              <div className={"group-card"
                  + (editing && editing.id === g.id ? " group-card-editing" : "")
                  + (open ? " group-card-open" : "")}
                   key={g.id}
                   role="button" tabIndex={0} aria-expanded={open ? "true" : "false"}
                   onClick={() => toggleGroup(g)}
                   onKeyDown={(e) => {
                     //:只管卡片自己的键 —— 展开区里有 textarea / 输入框，
                     //:它们按 Enter 会冒泡到这张卡，不判 target 的话，
                     //:在职责框里按回车会把整个群收起来（而且看起来像"焦点丢了"）。
                     if (e.target !== e.currentTarget) return;
                     if (e.key === "Enter" || e.key === " ") {
                       e.preventDefault();
                       toggleGroup(g);
                     }
                   }}>
                <button className="group-toggle" disabled={busy}
                        title={open ? t("收起成员") : t("展开成员")}
                        aria-label={open ? t("收起成员") : t("展开成员")}
                        onClick={(e) => { e.stopPropagation(); toggleGroup(g); }}>
                  {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </button>
                {/* 点名字进群 —— 与"点条目展开"两个手势并存：
                    名字是进群，卡片其余部分是展开。 */}
                <button className="group-open"
                        onClick={(e) => { e.stopPropagation(); onEnter && onEnter(g); }}>
                  <b>{g.name}</b>
                  <span className="group-sub">
                    {g.members.length} 人 · {host ? t("主理人 {host.name}", { "host.name": host.name }) : t("无主理人")}
                  </span>
                </button>
                <span className="group-avatars">
                  {orderedMembers(g.members).map(({ member: m, index }) => (
                    <button className={"group-avatar-btn" + (m.host ? " group-avatar-host" : "")}
                            key={m.name}
                            title={(m.host ? m.name + t("（主理人）") : m.name) + t(" —— 点击配置")}
                            onClick={(e) => { e.stopPropagation(); openMember(g, index, m); }}>
                      {/* 不要黄条，主理人头像用一圈黄边框
                          （这样所有头像还是同一个高度）。 */}
                      <Avatar slot={m.avatar} name={m.name} size={22}
                              title={m.host ? t("{m.name}（主理人）", { "m.name": m.name }) : m.name} />
                    </button>
                  ))}
                </span>
                <button className="skill-btn" disabled={busy}
                        title={t("编辑这个群聊（群名 / 人数 / 主理人 / 成员名字）")}
                        onClick={(e) => { e.stopPropagation(); startEdit(g); }}><Pencil size={13} /></button>
                <button className="skill-btn skill-btn-danger" disabled={busy}
                        title={t("解散此群聊（聊天记录仍会保留）")}
                        onClick={(e) => { e.stopPropagation(); remove(g); }}><Trash2 size={13} /></button>
                {open && renderBody(g)}
              </div>
            );
          })}
        </div>
      </div>

      {/* ── 下面：新建 / 编辑群聊（表格式流程）──────────────────────── */}
      <div className="group-sec">
        <h2 className="group-sec-title">{editing ? t("编辑群聊") : t("新建群聊")}</h2>
        <p className="muted">
          {t("最多 {maxGroups} 个群聊，每个群里至多 {limits.maxMembers} 个人（包括主理人）。", { "maxGroups": maxGroups, "limits.maxMembers": limits.maxMembers })}
        </p>

        {/* ① 基本信息 —— 规整的三行 */}
        <div className="group-form-table">
          <div className="group-form-row">
            <span className="group-form-label">{t("群名称（留空则默认编号）")}</span>
            <input className="group-form-input" value={form.name}
                   maxLength={limits.maxName}
                   placeholder={editing ? t("未命名群聊") : defaultName}
                   onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} />
          </div>
          <div className="group-form-row">
            <span className="group-form-label">{t("团队人数")}</span>
            <input className="group-form-input num" type="number"
                   min="1" max={limits.maxMembers} value={form.count}
                   onChange={(e) => setCount(Number(e.target.value))} />
            <span className="group-form-hint">{t("最多 {limits.maxMembers} 人，包括主理人", { "limits.maxMembers": limits.maxMembers })}</span>
          </div>
          <div className="group-form-row">
            <span className="group-form-label">{t("是否选举主理人")}</span>
            <label className="group-check">
              <input type="checkbox" checked={form.hasHost}
                     onChange={(e) => {
                       setForm((f) => ({ ...f, hasHost: e.target.checked }));
                       setHostConfirmed(false);
                       setOpenPicker("");
                     }} />
              <span>{t("是（主理人会成为你和团队沟通的桥梁）")}</span>
            </label>
          </div>
          {/* 群级 brief —— 整群一段共同背景。
              它与成员各自的「职责」正交：brief 说"我们在干什么"，职责说"你是谁"。 */}
          <div className="group-form-row">
            <span className="group-form-label">{t("群背景 / 目标（可留空）")}</span>
            <textarea className="group-form-input group-brief" rows={2}
                      maxLength={limits.maxBrief} value={form.brief || ""}
                      placeholder={t("例如：我们在做一个面向中小企业的年度报告，先定口径再分工")}
                      onChange={(e) => setForm((f) => ({ ...f, brief: e.target.value }))} />
          </div>
          {/* 圆桌只有无主理人时才给勾（启用圆桌的前提是不选举主理人）。 */}
          {!form.hasHost && (
            <div className="group-form-row">
              <span className="group-form-label">{t("启用圆桌交谈")}</span>
              <label className="group-check">
                <input type="checkbox" checked={!!form.roundtable}
                       onChange={(e) => setForm((f) => ({ ...f, roundtable: e.target.checked }))} />
                <span>{t("开启后成员共享同一段对话、按顺序自动接力（最多 3 位）")}</span>
              </label>
            </div>
          )}
        </div>

        {/* ② 主理人（勾了才出现）—— 确认后才浮现员工配置 */}
        {form.hasHost && (
          <div className="group-host-sec">
            <h3 className="group-sub-title">{t("主理人")}</h3>
            {personCard(0, { host: true })}
            {!editing && !hostConfirmed ? (
              <button className="skill-btn group-next" disabled={busy}
                      onClick={() => setHostConfirmed(true)}>{t("确认主理人")}</button>
            ) : (
              <span className="group-ok">{t("主理人已确认")}</span>
            )}
          </div>
        )}

        {/* ③ 员工配置 —— 有主理人时是（人数-1）张卡；没有主理人就直接出现 */}
        {staffVisible && (
          <div className="group-staff-sec">
            <h3 className="group-sub-title">
              {t("员工配置")} <span className="muted">{t("（在这里配置你的员工属性）")}</span>
            </h3>
            {staffIndices.length ? (
              <div className="group-staff-grid">
                {staffIndices.map((i) => personCard(i))}
              </div>
            ) : (
              <p className="muted">{t("这次没有员工 —— 只有主理人一个人。")}</p>
            )}
            {/* 禁用按钮的原因必须写在脸上 —— 禁用的按钮不显示 title。 */}
            {!verdict.ok && <p className="group-form-hint">{verdict.error}</p>}
            <button className="skill-btn group-next"
                    disabled={busy || (!editing && full) || !verdict.ok}
                    title={verdict.error || (!editing && full ? t("已经有 {maxGroups} 个群聊了", { "maxGroups": maxGroups }) : "")}
                    onClick={() => { setSummaryError(""); setSummary(true); }}>
              {t("确认员工配置")}
            </button>
            {!editing && full && (
              <span className="muted"> {t("已经有 {maxGroups} 个群聊了，先解散一个。", { "maxGroups": maxGroups })}</span>
            )}
            {editing && (
              <button className="skill-btn" disabled={busy} onClick={cancelEdit}>{t("取消编辑")}</button>
            )}
          </div>
        )}
      </div>

      {/* ── 确认弹窗（从上到下：群名称 / 主理人 / 员工）───────────────── */}
      {summary && (
        <div className="backdrop"
             onMouseDown={(e) => { if (e.target === e.currentTarget) setSummary(false); }}>
          <div className="group-summary">
            <button className="close" title={t("取消")} onClick={() => setSummary(false)}>✕</button>
            <h2>{displayName}</h2>
            {form.hasHost && (
              <section className="group-summary-sec">
                <b>{t("主理人")}</b>
                <div className="group-summary-row">
                  <Avatar slot={avatars[0]} name={names[0]} size={26} />
                  <span className="group-summary-name">{names[0]}</span>
                  <span className="group-summary-model">{modelLabel(models[0], opts)}</span>
                </div>
              </section>
            )}
            <section className="group-summary-sec">
              <b>{t("员工（{staffIndices.length}）", { "staffIndices.length": staffIndices.length })}</b>
              {staffIndices.length ? staffIndices.map((i) => (
                <div className="group-summary-row" key={i}>
                  <Avatar slot={avatars[i]} name={names[i]} size={26} />
                  <span className="group-summary-name">{names[i]}</span>
                  <span className="group-summary-model">{modelLabel(models[i], opts)}</span>
                </div>
              )) : <p className="muted">{t("只有主理人一个人。")}</p>}
            </section>
            {summaryError && <p className="group-member-error">{summaryError}</p>}
            <div className="group-summary-actions">
              <button className="skill-btn" disabled={busy} onClick={submit}>
                {editing ? t("保存修改") : t("创建群聊")}
              </button>
              <button className="skill-btn" disabled={busy}
                      onClick={() => setSummary(false)}>{t("返回修改")}</button>
            </div>
          </div>
        </div>
      )}

      {/* 解散群聊的二次确认（不可逆，一次点击不许发生）。 */}
      {confirming && (
        <Confirm
          title={t("解散这个群聊？")}
          body={<>
            <b>{confirming.name || t("这个群聊")}</b>
            <br />
            {t("群成员、主理人与群配置都会删掉；它说过的话仍留在历史库里。")}
          </>}
          confirmLabel={t("解散")}
          busy={busy}
          onConfirm={() => doRemove(confirming)}
          onCancel={() => setConfirming(null)}
        />
      )}
    </section>
  );
}
