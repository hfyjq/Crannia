import React, { useEffect, useState } from "react";
import { ChevronRight, Plug, Plus, Wrench, X } from "lucide-react";
import { jsonRequest } from "./api.js";
import { McpPanel } from "./McpPanel.jsx";
import {
  BLOCKED_MARK, blockedText, groupSkills, isBlocked, sourceBadge, uninstallMessage,
  uninstallMode, visibleSkills,
} from "./skills-view.js";

// 「技能与连接器」那一屏。
//
// 判据全在 `skills-view.js`（纯函数，node 逐条断言）—— 这里只管画。
//    四条：自带的不显示 · 目录内绿标 · 外部来源黄标 · 被屏蔽的亮红 + 解封。
//    ＋ 同一个外部来源下 ≥3 个，收成一个「集合」（点开是个浮窗）。
//
// 它是一个导出件：单独导出才能被单独挂起来量 DOM。
//
// 三个动作各有各的出路，不许混成一句「操作失败」：
//   · 卸载（仓内）→ `POST /api/skills/uninstall` —— 真挪进系统回收站
//   · 卸载（外部）→ 不发请求，只念那句话（后端也会拒，但界面先别让用户白等）
//   · 解封 → `POST /api/quarantine/release` —— 立刻重体检

import { t, lang } from "./i18n.js";
//:声明式配置卡（步 3）：manifest 里写了 `settings` 的技能，
//:浮窗里画的是这张表驱动的卡（与设置页那一格同一个组件）。
import SkillConfig from "./SkillConfig.jsx";
/** 服务状态的语气：出错红、告警黄、运行绿、其它灰。 */
function serviceTone(sv) {
  if (!sv) return "off";
  if (sv.error) return "bad";
  if (sv.level === "warn") return "warn";
  return sv.running ? "on" : "off";
}

/** 服务状态那一行的文字。正文由技能自己写，这里只拼状态词。 */
function serviceText(sv) {
  if (!sv) return "";
  if (sv.error) return t("启动失败：{msg}", { msg: sv.error });
  const head = sv.running ? t("运行中") : t("已停止");
  return sv.text ? head + " · " + sv.text : head;
}

export function Skills({ skills, connected, client, notify, onRefresh }) {
  // 先过滤（谁上页），再分组（同一个外部来源 ≥3，一个集合）。
  const rows = groupSkills(visibleSkills(skills));
  const [confirming, setConfirming] = useState(null);   // 待确认卸载的那一条
  const [busy, setBusy] = useState("");
  //:展开着的那个集合（存 `root`，不存那一行本身）—— 每次渲染现查。
  //:这样刷新之后它跟着新的 `rows` 走：集合没了（比如成员掉到 2 个）
  //:浮窗自己就没了，不会停在一份已经不存在的快照上。
  const [openRoot, setOpenRoot] = useState("");
  const open = openRoot
    ? rows.find((r) => r.kind === "group" && r.root === openRoot) : null;
  //:展示信息用浮窗：被屏蔽的那张卡点开不再就地展开，而是弹一个浮窗
  //:放这一条的整条详情（为什么被屏蔽 + 位置 + 操作）。
  //:只针对错误插件 —— 正常的卡点不开（光标也不许说慌，
  //:   见 `.skill.blocked{cursor:pointer}`）。
  //:存名字、每次渲染现查（同上面 `openRoot` 那条）——
  //:存对象的话刷新之后就停在一份已经不存在的快照上了。
  const [detailName, setDetailName] = useState("");
  const detail = detailName
    ? (rows.find((r) => r.kind === "skill" && r.skill.name === detailName) || {}).skill
    : null;
  //: MCP 管理那个浮窗开着没有（与集合浮窗互斥 —— 两个 `.backdrop`
  //:   同时挂上去就是两层遮罩，用户只看得见上面那个）。
  const [mcpOpen, setMcpOpen] = useState(false);
  //:点开一张卡片就展开多一点、展示全部内容，点一下 × 又平滑缩回去。
  //:一次只开一个（存名字，不存布尔）—— 两张卡同时摊开会让这一屏
  //:长得不像卡片墙了；而且「点开一个卡片」本来就是单数的说法。
  const [openCard, setOpenCard] = useState("");
  //:技能配置（`manifest.yaml` 的 `config.fields` 声明、设置页渲染）。
  //:存 `skill_configs.<名字>`；凭据进这里，不进工具参数
  //: （模型看得到的参数里绝不放令牌）。
  const [cfgName, setCfgName] = useState("");
  const [cfgForm, setCfgForm] = useState({});
  const [cfgBusy, setCfgBusy] = useState(false);
  //:已保存的条目（`{名字: {...}}`）+ 默认那个。值在服务端打码，
  //: 界面只知道"有这条"，改的时候必须重新输入。
  const [cfgItems, setCfgItems] = useState({});
  const [cfgActive, setCfgActive] = useState("");
  //:正在改哪一条（空串 = 新建）。不静默覆盖：同名时会被拦。
  const [cfgEditing, setCfgEditing] = useState("");
  //: 条目名（GitHub 那种 `named: true` 的配置才有）。
  const [cfgEntryName, setCfgEntryName] = useState("");
  //:这个技能是声明式配置吗（`manifest.settings`）—— 是的话浮窗里画
  //:   `SkillConfig`（表驱动），否则走下面那套老的"条目 + 字段"表单。
  //:判据来自后端（`GET /api/skills/config` 会带 `schema`），不是前端猜。
  const [cfgSchema, setCfgSchema] = useState(null);

  //: 有后台服务时每 5 秒刷新一次状态；页面离开就停。
  const anyService = (skills || []).some((s) => s.service);
  useEffect(() => {
    if (!anyService || !onRefresh) return undefined;
    const timer = setInterval(() => onRefresh(), 5000);
    return () => clearInterval(timer);
  }, [anyService, onRefresh]);

  /** 点卡片 = 展开 / 再点 = 收起。
   *  点里面的按钮不算（`解封` / `×` 各有各的 onClick）——
   *    不拦的话点「解封」会顺手把卡也摊开，两件事挤在一个手势上。 */
  function onCardClick(e, name) {
    if (e.target.closest("button")) return;
    setOpenCard((cur) => (cur === name ? "" : name));
  }

  async function doUninstall(s) {
    setConfirming(null);
    setBusy(s.name);
    try {
      // [!] 名字走 body，不是拼进 URL —— 字面路径本身必须是路由
      //   （前后端两边对账）。
      const r = await client.call("/api/skills/uninstall",
                                  jsonRequest("POST", { name: s.name }));
      // 回执原样转给用户 —— 里面那句是 `core/recycle.py::explain` 说的，
      //   降级（没真挪走 / 退回永久删）时它会说清，不许在这儿压成"已卸载"。
      notify((r && r.message) || t("已卸载"));
      if (onRefresh) onRefresh();
    } catch (e) {
      notify(e.message);
    } finally {
      setBusy("");
    }
  }

  /** 启用 / 停用：状态写进 settings.json 的 skill_configs.<名>.state。
   *  停用后工具立即从 tools= 里消失、执行侧同时拒绝；启用立即恢复。 */
  async function toggleState(s) {
    const next = s.state === "off" ? "on" : "off";
    setBusy(s.name + ":state");
    try {
      await client.call("/api/skills/state",
                        jsonRequest("POST", { name: s.name, state: next }));
      notify(next === "off" ? t("已停用") : t("已启用"));
      if (onRefresh) onRefresh();
    } catch (e) {
      notify(e.message);
    } finally {
      setBusy("");
    }
  }

  async function doRelease(s) {
    setBusy(s.name);
    try {
      const r = await client.call("/api/quarantine/release",
                                  jsonRequest("POST", { owner: "skill:" + s.name }));
      // 回执由后端给 —— 它知道走的是哪一支
      //   （体检过了 / 仍坏 / 路径没了 / 重载炸了）。若这里写死一句话，
      //   则「没弹」与「没跑」在用户看来会长得一样。
      notify((r && r.message) || t("已解封"));
      if (onRefresh) onRefresh();
    } catch (e) {
      notify(e.message);
    } finally {
      setBusy("");
    }
  }

  /** 装一个技能：选目录（桌面端）-> 后端复制到用户技能目录。
   *
   *  判据在后端：形状对不对、目标有没有同名、复制成不成 —— 这里只管
   *    选目录与把回执原样念出来（前端不猜结果）。
   *  浏览器里没有选择器，明说"只有桌面端支持"，不做假的输入框。 */
  async function pickAndInstall() {
    const pick = typeof window !== "undefined" && window.crannia
      && window.crannia.pickSkillDir;
    if (!pick) return notify(t("只有桌面端支持选择目录"));
    let dir = "";
    try {
      dir = await pick();
    } catch (e) {
      return notify((e && e.message) || t("选择目录失败"));
    }
    if (!dir) return;                       // 用户取消 —— 什么都不说
    setBusy("__install__");
    try {
      const r = await client.call("/api/skills/install",
                                  jsonRequest("POST", { path: dir }));
      notify(t("已装好：{name}", { name: (r && r.name) || "" }));
      if (onRefresh) onRefresh();
    } catch (e) {
      notify(e.message);
    } finally {
      setBusy("");
    }
  }

  /** 打开某技能的配置浮窗：拉一次条目列表（值已打码）。 */
  async function openConfig(s) {
    setCfgName(s.name);
    setCfgForm({});
    setCfgEditing("");
    setCfgEntryName("");
    setCfgItems({});
    setCfgActive("");
    setCfgSchema(null);
    try {
      const r = await client.call("/api/skills/config?name=" + encodeURIComponent(s.name),
                                    { method: "GET" });
      // 声明式：后端带 `schema`，浮窗改画表驱动的 `SkillConfig`
      //   （它自己负责读/写，那套"条目 + 字段"的老表单一条都不走）。
      if (r && r.schema) {
        setCfgSchema(r.schema);
        return;
      }
      const got = (r && r.config) || {};
      // 存储键可配：GitHub 用 `entries`，mail_parser 用 `accounts`。
      const key = ((s.config || {}).entries_key) || "entries";
      setCfgItems(got[key] || {});
      setCfgActive(got.active || "");
    } catch (e) {
      // 拉不到就当"还没有条目"—— 新建仍然可用，别把浮窗卡死
    }
  }

  /** 把整个 `{active, entries}` 写回去（三个动作共用一条路）。 */
  async function saveItems(nextItems, nextActive) {
    const skillRow = rows.find((r) => r.kind === "skill" && r.skill.name === cfgName);
    const skill = (skillRow || {}).skill || {};
    const key = ((skill.config || {}).entries_key) || "entries";
    setCfgBusy(true);
    try {
      await client.call("/api/skills/config",
                        jsonRequest("POST", {
                          name: cfgName,
                          config: { active: nextActive, [key]: nextItems },
                        }));
      setCfgItems(nextItems);
      setCfgActive(nextActive);
      notify(t("已保存"));
      if (onRefresh) onRefresh();
      return true;
    } catch (e) {
      notify(e.message);
      return false;
    } finally {
      setCfgBusy(false);
    }
  }

  async function saveConfig() {
    const s = rows.find((r) => r.kind === "skill" && r.skill.name === cfgName);
    const skill = (s || {}).skill || {};
    const cfg = skill.config || {};
    const fields = cfg.fields || [];
    const named = !!cfg.named;

    const entryName = named ? String(cfgEntryName || "").trim() : "";
    if (named && !entryName) return notify(t("先给这个条目起个名字"));
    const missing = fields.filter((f) => f.required && !String(cfgForm[f.key] || "").trim());
    if (missing.length) {
      return notify(t("还差：{names}", { names: missing.map((f) => t(f.label)).join("、") }));
    }

    if (!named) {
      // 老形状（mail_parser 那种）：直接存字段本身。
      setCfgBusy(true);
      try {
        await client.call("/api/skills/config",
                          jsonRequest("POST", { name: cfgName, config: cfgForm }));
        notify(t("已保存"));
        setCfgForm({});
        if (onRefresh) onRefresh();
      } catch (e) {
        notify(e.message);
      } finally {
        setCfgBusy(false);
      }
      return;
    }

    // 不静默覆盖：新建模式下撞了已有名字，停下来说清楚。
    if (!cfgEditing && Object.prototype.hasOwnProperty.call(cfgItems, entryName)) {
      return notify(t("已经有叫「{name}」的条目了 —— 要改它请点「修改」，换个名字也行。",
                      { name: entryName }));
    }
    const next = { ...cfgItems, [entryName]: { ...cfgForm } };
    const nextActive = cfgActive || entryName;
    const ok = await saveItems(next, nextActive);
    if (ok) {
      setCfgForm({});
      setCfgEntryName("");
      setCfgEditing("");
    }
  }

  /** 改一条：凭据不回显（留空重填），其余字段照原样预填。 */
  function editItem(name) {
    const skillRow = rows.find((r) => r.kind === "skill" && r.skill.name === cfgName);
    const skill = (skillRow || {}).skill || {};
    const cfg = skill.config || {};
    const fields = cfg.fields || [];
    const item = cfgItems[name] || {};
    const filled = {};
    for (const f of fields) {
      if (f.type === "password") continue;        // 凭据必须重填
      if (Object.prototype.hasOwnProperty.call(item, f.key)) filled[f.key] = item[f.key];
    }
    setCfgEditing(name);
    // 有 label 的那一种（mail 多账号）：名字框预填成 label
    setCfgEntryName(cfg.label_key ? String(item[cfg.label_key] || name) : name);
    setCfgForm(filled);
  }

  async function removeItem(name) {
    const next = { ...cfgItems };
    delete next[name];
    const nextActive = cfgActive === name ? (Object.keys(next)[0] || "") : cfgActive;
    await saveItems(next, nextActive);
  }

  async function makeActive(name) {
    await saveItems(cfgItems, name);
  }

  function onUninstallClick(s) {
    // 外部来源那一路不发请求：只把那句话念给用户。
    if (uninstallMode(s) === "external") return notify(uninstallMessage(s));
    if (uninstallMode(s) === "confirm") return setConfirming(s);
  }

  return (
    <section className="page">
      <div className="page-head">
        <div><small>CRANNIA</small><h1>{t("技能与连接器")}</h1></div>
        {/* MCP 管理点击后是个浮窗 ——
            连接器那一半的入口就在这儿（技能那一半是下面那一片卡片）。 */}
        <div className="skill-head-actions">
          <button onClick={() => setMcpOpen(true)}><Plug size={15} />{t("MCP 管理")}</button>
          <button disabled={busy === "__install__"} onClick={pickAndInstall}><Plus size={15} />{t("添加技能")}</button>
        </div>
      </div>

      {!connected ? <p className="muted">{t("启动本地服务后查看技能。")}</p> : (
        <div className="skill-grid">
          {rows.map((r) => {
            // ── 集合那一行：标题就是路径（零发明）────────────────────
            if (r.kind === "group") {
              const n = r.items.length;
              const bad = r.blockedCount;
              //:来源标取任一成员就够 —— 同一个 `root`，同一枚标（判据在纯函数里）。
              const s = r.items[0];
              return (
                <div className={"skill skill-group" + (bad ? " blocked" : "")} key={r.root}
                     onClick={(e) => {
                       // 整张卡都能点开（`cursor:pointer` 才不说谎）—— 点里面那个
                       //   「展开」按钮时不算双击（它自己也会开，重复调无害，但别依赖）。
                       if (e.target.closest("button")) return;
                       setOpenRoot(r.root);
                     }}>
                  <Wrench size={17} />
                  <div className="skill-main">
                    <b className="skill-group-title">{r.title}</b>
                    <span className="skill-desc">{t("{n} 个技能", { "n": n })}</span>
                    {sourceBadge(s).cls === "external" && (
                      /* 外部来源要写明它只在这里可用 —— 用户有权知道
                         为什么模型从没提起过这些技能。 */
                      <span className="skill-desc skill-scope-note">
                        {t("这些技能只能用 `/` 调用，不会进入模型的上下文")}</span>
                    )}
                    <span className="skill-meta">
                      <i className={"skill-badge skill-badge-" + sourceBadge(s).cls}>
                        {sourceBadge(s).text}</i>
                      {!!bad && (
                        <i className="skill-badge skill-badge-blocked">{t("{bad} 个被屏蔽", { "bad": bad })}</i>
                      )}
                      {(() => {
                        const running = r.items.filter(
                          (x) => x.service && x.service.running && !x.service.error).length;
                        return running > 0 ? (
                          <span className="skill-service on">
                            <i className="skill-dot" />
                            {t("{n} 个服务运行中", { "n": running })}
                          </span>
                        ) : null;
                      })()}
                    </span>
                  </div>
                  <div className="skill-actions">
                    <button className="skill-btn" onClick={() => setOpenRoot(r.root)}>
                      <ChevronRight size={14} />{t("展开")}
                    </button>
                  </div>
                </div>
              );
            }
            const s = r.skill;
            const badge = sourceBadge(s);
            const blocked = isBlocked(s);
            const mode = uninstallMode(s);
            return (
              <div className={"skill" + (blocked ? " blocked" : "")
                              + (s.state === "off" ? " off" : "")} key={s.name}
                   onClick={blocked ? () => setDetailName(s.name) : undefined}>
                <Wrench size={17} />
                <div className="skill-main">
                  <b>{s.name}</b>
                  <span className="skill-desc">{s.summary || s.description || t("CRANNIA 技能")}</span>
                  <span className="skill-meta">
                    <i className={"skill-badge skill-badge-" + badge.cls}>{badge.text}</i>
                    {s.kind === "prompt" && <i className="skill-badge skill-badge-prompt">{t("指令型")}</i>}
                    {/* 不兼容的模块只显示已屏蔽 ——
                        原因没有丢，它挪进详情浮窗（点这张卡打开）。 */}
                    {blocked && <i className="skill-badge skill-badge-blocked">{t(BLOCKED_MARK)}</i>}
                  </span>
                  {!!s.service && (
                    <span className={"skill-service " + serviceTone(s.service)}
                          title={s.service.error || s.service.text || ""}>
                      <i className="skill-dot" />
                      {serviceText(s.service)}
                    </span>
                  )}
                  {/* 声明的外部程序缺了 —— 缺什么就写什么，补的办法进 title。
                      缺的时候不挡装载：这张卡照样可用，只是那几件做不了。 */}
                  {!!s.external && (s.external.missing || []).length > 0 && (
                    <span className="skill-service warn"
                          title={(s.external.missing || [])
                            .map((m) => (m.hint ? m.name + "：" + m.hint : m.name))
                            .join("；")}>
                      <i className="skill-dot" />
                      {t("缺少外部程序：{names}", {
                        names: s.external.missing.map((m) => m.name).join("、"),
                      })}
                    </span>
                  )}
                </div>
                <div className="skill-actions">
                  {!!s.has_config && (
                    <button className="skill-btn" onClick={(e) => {
                      e.stopPropagation(); openConfig(s);
                    }} title={t("配置这个技能")}>
                      {s.configured ? t("已配置") : t("配置")}
                    </button>
                  )}
                  {blocked && (
                    <button className="skill-btn" disabled={busy === s.name}
                            onClick={() => doRelease(s)}>{t("解封")}</button>
                  )}
                  {mode !== "none" && (
                    /* 不用垃圾桶图标 —— `title` 里已经说清了是"挪进回收站"，
                       图标是多余的噪声。 */
                    <button className="skill-btn skill-btn-danger" disabled={busy === s.name}
                            title={mode === "external" ? t("外部来源：暂不提供卸载") : t("卸载（挪进回收站）")}
                            onClick={() => onUninstallClick(s)}>{t("卸载")}</button>
                  )}
                  {/* 启用 / 停用：最后一项，落在卡片右下角。 */}
                  <button className="skill-btn"
                          disabled={busy === s.name + ":state"}
                          onClick={(e) => { e.stopPropagation(); toggleState(s); }}>
                    {s.state === "off" ? t("启用") : t("停用")}
                  </button>
                </div>
              </div>
            );
          })}
          {!rows.length && (
            <p className="muted">
              {t("还没有你自己装的技能 —— 把一个带")} <code>SKILL.md</code>{t("（或带")}
              <code> manifest.yaml</code>{t("）的目录放进")} <code>skills/</code> {t("就能用。 （框架自带的那些不在这里列。）")}
            </p>
          )}
        </div>
      )}

      {/* 集合点开就是这个浮窗 —— 与卸载确认框同一个外壳
          （`.backdrop` + 一块面板），不是第三种浮窗。 */}
      {/* 被屏蔽那一条的整条详情：点首页那张红卡打开；
          收起时不再就地展开（只针对错误插件）。
          仍是同一个浮窗外壳（`.backdrop` + `.skill-panel`）。 */}
      {/* 声明式配置浮窗（步 3）：manifest 里写了 `settings` 的技能。
          与设置页那一格同一个组件 —— 两份表单迟早漂，而"哪个字段是密钥"
          只能有一处判据（`x-secret`）。 */}
      {!!cfgName && !!cfgSchema && (
        <div className="backdrop"
             onClick={(e) => { if (e.target === e.currentTarget) setCfgName(""); }}>
          <section className="skill-panel" role="dialog" aria-modal="true">
            <div className="skill-panel-head">
              <b>{t("配置：{name}", { name: cfgName })}</b>
              <button className="skill-panel-close" title={t("关闭")}
                      aria-label={t("关闭")}
                      onClick={() => setCfgName("")}><X size={16} /></button>
            </div>
            <div className="skill-panel-body">
              <SkillConfig skill={cfgName} client={client} notify={notify}
                           connected={connected} />
            </div>
          </section>
        </div>
      )}
      {/* 技能配置浮窗（`has_config` 的技能才有那颗按钮）。
          判据：凭据只从这里进，不进工具参数 —— 模型永远看不到令牌。
          打开时不回填已有的值（凭据不回显到页面上）。 */}
      {!!cfgName && !cfgSchema && (() => {
        const row = rows.find((r) => r.kind === "skill" && r.skill.name === cfgName);
        const skill = (row || {}).skill || {};
        const cfg = skill.config || {};
        const fields = cfg.fields || [];
        return (
          <div className="backdrop"
               onClick={(e) => { if (e.target === e.currentTarget) setCfgName(""); }}>
            <section className="skill-panel" role="dialog" aria-modal="true">
              <div className="skill-panel-head">
                <b>{t("配置：{name}", { name: cfgName })}</b>
                <button className="skill-panel-close" title={t("关闭")}
                      aria-label={t("关闭")}
                      onClick={() => setCfgName("")}><X size={16} /></button>
              </div>
              <div className="skill-panel-body">
                {/* 已保存的条目：`named` 的配置（GitHub 令牌）才有。
                    值在服务端打码 —— 这里只列名字与"哪个是默认"。 */}
                {!!cfg.named && (
                  <div className="cfg-list">
                    {Object.keys(cfgItems).length === 0 && (
                      <p className="muted">{t("还没有条目。在下面新建一个。")}</p>
                    )}
                    {Object.keys(cfgItems).map((name) => (
                      <div className={"cfg-item" + (cfgActive === name ? " active" : "")}
                           key={name}>
                        <b>{cfg.label_key ? (cfgItems[name] || {})[cfg.label_key] || name : name}</b>
                        {cfgActive === name && (
                          <i className="skill-badge skill-badge-repo">{t("默认")}</i>
                        )}
                        <span className="cfg-item-actions">
                          <button className="skill-btn" disabled={cfgBusy || cfgActive === name}
                                  onClick={() => makeActive(name)}>{t("设为默认")}</button>
                          <button className="skill-btn" disabled={cfgBusy}
                                  onClick={() => editItem(name)}>{t("修改")}</button>
                          <button className="skill-btn skill-btn-danger" disabled={cfgBusy}
                                  onClick={() => removeItem(name)}>{t("删除")}</button>
                        </span>
                      </div>
                    ))}
                  </div>
                )}
                {!!cfg.named && (
                  <label className="cfg-field">
                    <span>{t("条目名")} *</span>
                    <input type="text"
                           placeholder={t("比如：工作号 / 个人号")}
                           value={cfgEntryName}
                           disabled={!!cfgEditing}
                           onChange={(e) => setCfgEntryName(e.target.value)}
                           autoComplete="off" />
                  </label>
                )}
                {fields.map((f) => (
                  <label key={f.key} className="cfg-field">
                    <span>{t(f.label || f.key)}{f.required ? " *" : ""}</span>
                    <input
                      type={f.type === "password" ? "password" : "text"}
                      placeholder={t(f.placeholder || "")}
                      value={cfgForm[f.key] || ""}
                      onChange={(e) => setCfgForm((cur) => ({ ...cur, [f.key]: e.target.value }))}
                      autoComplete="off" />
                    {!!f.help && <small className="muted">{t(f.help)}</small>}
                  </label>
                ))}
                {!!cfg.setup_guide && (
                  <details className="cfg-guide">
                    <summary>{t("怎么拿这个凭据？")}</summary>
                    {/* `setup_guide` 可以是一段文本（单语），也可以是
                        `{zh, en}` —— 后者按当前界面语言取。
                        不进 i18n 表：它是一整篇说明，不是界面短句。 */}
                    <pre>{typeof cfg.setup_guide === "string"
                      ? cfg.setup_guide
                      : (cfg.setup_guide[lang()] || cfg.setup_guide.zh || "")}</pre>
                  </details>
                )}
                <div className="skill-actions">
                  {!!cfgEditing && (
                    <button className="skill-btn" onClick={() => {
                      setCfgEditing(""); setCfgEntryName(""); setCfgForm({});
                    }}>{t("取消修改")}</button>
                  )}
                  <button className="skill-btn" onClick={() => setCfgName("")}>{t("关闭")}</button>
                  <button className="skill-btn skill-btn-primary" disabled={cfgBusy}
                          onClick={saveConfig}>
                    {cfgEditing ? t("保存修改") : (cfg.named ? t("新建条目") : t("保存"))}
                  </button>
                </div>
              </div>
            </section>
          </div>
        );
      })()}

      {detail && (
        <div className="backdrop"
             onClick={(e) => { if (e.target === e.currentTarget) setDetailName(""); }}>
          <section className="skill-panel" role="dialog" aria-modal="true">
            <div className="skill-panel-head">
              <h2 className="skill-panel-title">{detail.name}</h2>
              <button className="skill-panel-close" title={t("关闭")}
                      aria-label={t("关闭")}
                      onClick={() => setDetailName("")}><X size={16} /></button>
            </div>
            <div className="skill-panel-body">
              <span className="skill-meta">
                <i className={"skill-badge skill-badge-" + sourceBadge(detail).cls}>
                  {sourceBadge(detail).text}</i>
                <i className="skill-badge skill-badge-blocked">{t(BLOCKED_MARK)}</i>
                {detail.kind === "prompt" && (
                  <i className="skill-badge skill-badge-prompt">{t("指令型")}</i>
                )}
              </span>
              <p className="skill-detail-desc">{detail.description || t("CRANNIA 技能")}</p>
              {/* 收起时只说「已屏蔽」，证据在这一格（点开卡片即可看见详细的报错信息）。 */}
              <div className="skill-detail-sec">
                <h3>{t("为什么被屏蔽")}</h3>
                <p className="skill-why">{blockedText(detail)}</p>
              </div>
              <div className="skill-detail-sec">
                <h3>{t("位置")}</h3>
                <p className="skill-where">{detail.path || t("（未提供）")}</p>
              </div>
              <div className="skill-detail-actions">
                <button className="skill-btn" disabled={busy === detail.name}
                        onClick={async () => { await doRelease(detail); setDetailName(""); }}>
                  {t("解封")}</button>
                {uninstallMode(detail) !== "none" && (
                  <button className="skill-btn skill-btn-danger"
                          disabled={busy === detail.name}
                          onClick={() => { setDetailName(""); onUninstallClick(detail); }}>
                    {t("卸载")}</button>
                )}
              </div>
            </div>
          </section>
        </div>
      )}

      {/* MCP 管理 —— 与集合浮窗同一个外壳（不新造第三种）。 */}
      {mcpOpen && (
        <McpPanel client={client} notify={notify} onClose={() => setMcpOpen(false)} />
      )}

      {open && (
        <div className="backdrop" onClick={(e) => { if (e.target === e.currentTarget) setOpenRoot(""); }}>
          <section className="skill-panel" role="dialog" aria-modal="true">
            <div className="skill-panel-head">
              <h2 className="skill-panel-title">{open.title}</h2>
              <button className="skill-panel-close" title={t("关闭")}
                      aria-label={t("关闭")}
                      onClick={() => setOpenRoot("")}><X size={16} /></button>
            </div>
            <p className="skill-panel-note">
              {/* 带表达式的句子走动态通道：模板里写占位符、值按名字传。
                  两句合成一句：原来两个 `{…}` 相邻，
                  JSX 不会补任何空白，英文里变成 `source.This is …`。
                  句间的空格是译文的一部分（中文原文里没有），让译者去摆。 */}
              {t("{n} 个技能来自这个来源。{note}", {
                n: open.items.length,
                /* 那句「外部来源不给卸」在这一层说一次就够（27 遍是噪声） */
                note: uninstallMessage(open.items[0]),
              })}
            </p>
            <div className="skill-panel-body">
              {/* 浮窗里用小号的卡片：整行 -> 两列小卡片
                  （描述最多两行，超出省略）。 */}
              <div className="skill-cards">
                {open.items.map((s) => (
                  <div className={"skill-card"
                                  + (isBlocked(s) ? " blocked" : "")
                                  + (openCard === s.name ? " open" : "")}
                       key={s.name} onClick={(e) => onCardClick(e, s.name)}>
                    <div className="skill-card-head">
                      <b>{s.name}</b>
                      {/* 收起那个 ×：只在展开时出现（收起的卡上它没有用武之地，
                          而卡片本来就小，多一个角标会挤）。 */}
                      {openCard === s.name && (
                        <button className="skill-card-x" title={t("收起")}
                                onClick={(e) => { e.stopPropagation(); setOpenCard(""); }}>×</button>
                      )}
                    </div>
                    <span className="skill-desc">{s.summary || s.description || t("CRANNIA 技能")}</span>
                    <span className="skill-meta">
                      {s.kind === "prompt" && <i className="skill-badge skill-badge-prompt">{t("指令型")}</i>}
                      {isBlocked(s) && (
                        <i className="skill-badge skill-badge-blocked">{t(BLOCKED_MARK)}</i>
                      )}
                    </span>
                    {/* 展开才露出来的那两行：为什么被屏蔽 + 它在哪儿。
                        收起时这里什么都不显示，所以"点开总是能看到多一点"
                        —— 描述本来就短的那些（`lark-*` 大多是）也一样，不会像没反应。 */}
                    <span className="skill-card-more">
                      {isBlocked(s) && <span className="skill-why">{blockedText(s)}</span>}
                      <span className="skill-where">{s.path || ""}</span>
                    </span>
                    {isBlocked(s) && (
                      <div className="skill-card-actions">
                        <button className="skill-btn" disabled={busy === s.name}
                                onClick={() => doRelease(s)}>{t("解封")}</button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </section>
        </div>
      )}

      {/* 卸一个真删东西的动作，二次确认。形态照 `SafetyNotice`
          （`.backdrop` + `.safety`）—— 不新造第三种浮窗。 */}
      {confirming && (
        <div className="backdrop">
          <section className="safety" role="dialog" aria-modal="true">
            <div className="safety-head"><h2 className="safety-title">{t("卸载这个技能？")}</h2></div>
            <p className="safety-body">
              <b>{confirming.name}</b>
              <br />
              {t("它的目录会被")}<b>{t("挪进系统回收站")}</b>{t("（可还原），名字里的工具")}<b>{t("当场不能再用")}</b>。
            </p>
            <div className="safety-actions">
              <button className="safety-go" onClick={() => doUninstall(confirming)}>{t("卸载")}</button>
              <button className="skill-btn" onClick={() => setConfirming(null)}>{t("取消")}</button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}
