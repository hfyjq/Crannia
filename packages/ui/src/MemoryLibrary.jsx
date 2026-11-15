// 记忆库的独立页（设置弹窗里的子页）。
//
// 数据全走现成端点（`/api/memory` 一族）—— 这一层只负责摆：
//   · 左：条目列表（名字 / 描述 / 字数）；点一条在右边打开；
//   · 右：改写（名字是身份，编辑时锁住）/ 新建 / 删除（二次确认）；
//   · 下：封条区（每条一个「解除」）；
//   · 顶栏：「删除时自动封条」全局开关 + 返回设置。
//
// 列表端点不带正文（单条最大 4000 字符），点开再取，这是端点自己的判据。
// 删除不可逆（内容不保留），第二次点才算；整库清空仍在设置页那行（5 秒倒计时）。
import React, { useEffect, useRef, useState } from "react";
import { jsonRequest } from "./api.js";
import { t } from "./i18n.js";

export function MemoryLibrary({ client, notify, onBack, settings, onReload }) {
  const [data, setData] = useState(null);        // {entries, tombstones}
  const [sel, setSel] = useState(null);          // 当前打开的条目名
  const [draft, setDraft] = useState({ name: "", description: "", content: "" });
  const [isNew, setIsNew] = useState(false);
  const [confirm, setConfirm] = useState("");    // 待确认删除的名字（二次确认）
  const [busy, setBusy] = useState(false);
  //:列表筛选词。纯前端过滤（条目最多几百条、端点一次全给），
  //: 判据是名字 ∪ 描述 —— 用户找一条记忆时，记得住的往往是其中任意一个。
  //: 不做拼音/模糊：这里要的是"我按名字找那一条"，不是搜索排序。
  const [q, setQ] = useState("");

  // ── 记忆包导出 / 导入（批 3 的界面那一半）──
  //    `xfer`：`""` / `"export"` / `"import"`；`plan` 是 dry-run 的回执。
  const [xfer, setXfer] = useState("");
  const [includeHistory, setIncludeHistory] = useState(false);
  const [zipBlob, setZipBlob] = useState(null);
  const [zipName, setZipName] = useState("");
  const [plan, setPlan] = useState(null);
  const [withHistory, setWithHistory] = useState(false);
  const zipRef = useRef(null);

  const autoSeal = !(settings && settings.memory_auto_seal === false);

  async function load() {
    try {
      const d = await client.call("/api/memory");
      setData(d || { entries: [], tombstones: [] });
    } catch (e) { notify(e.message); }
  }

  useEffect(() => { load(); /* eslint-disable-line */ }, []);

  async function open(name) {
    setConfirm("");
    try {
      const d = await client.call("/api/memory/entry?name=" + encodeURIComponent(name));
      setSel(name);
      setIsNew(false);
      setDraft({ name: d.name || name, description: d.description || "", content: d.content || "" });
    } catch (e) { notify(e.message); }
  }

  function startNew() {
    setConfirm("");
    setSel(null);
    setIsNew(true);
    setDraft({ name: "", description: "", content: "" });
  }

  async function save() {
    if (!draft.name.trim()) { notify(t("先给它起个名字。")); return; }
    setBusy(true);
    try {
      const r = await client.call("/api/memory/entry", jsonRequest("POST", {
        name: draft.name.trim(),
        description: draft.description,
        content: draft.content,
      }));
      if (r && r.ok === false) throw new Error(r.error || t("保存失败"));
      notify(r && r.created ? t("已新建「{name}」。", { name: draft.name.trim() })
                            : t("已保存「{name}」。", { name: draft.name.trim() }));
      setIsNew(false);
      setSel(draft.name.trim());
      await load();
    } catch (e) { notify(e.message); }
    finally { setBusy(false); }
  }

  async function remove(name) {
    //:二次确认：第一次点把按钮变成「确认删除」，第二次才真删。
    if (confirm !== name) { setConfirm(name); return; }
    setConfirm("");
    setBusy(true);
    try {
      const r = await client.call("/api/memory/delete", jsonRequest("POST", { name }));
      if (r && r.ok === false) throw new Error(r.error || t("删除失败"));
      notify(r && r.sealed
        ? t("已删除「{name}」，名字已封条（不会再自动生成）。", { name })
        : t("已删除「{name}」，自动封条已关闭 —— 后台抽取可能把它再生成回来。", { name }));
      setSel(null);
      setDraft({ name: "", description: "", content: "" });
      await load();
    } catch (e) { notify(e.message); }
    finally { setBusy(false); }
  }

  async function unseal(name) {
    setBusy(true);
    try {
      const r = await client.call("/api/memory/restore", jsonRequest("POST", { name }));
      if (r && r.ok === false) throw new Error(r.error || t("解除失败"));
      notify(t("已解除「{name}」的封条（内容不会回来，需要重新写）。", { name }));
      await load();
    } catch (e) { notify(e.message); }
    finally { setBusy(false); }
  }

  async function toggleSeal(on) {
    try {
      const r = await client.call("/api/settings", jsonRequest("POST", {
        memory_auto_seal: on,
      }));
      if (r && r.ok === false) throw new Error(r.error || t("保存失败"));
      if (onReload) await onReload();
      notify(on ? t("已开启：删除时自动封条。") : t("已关闭：删除后后台抽取可能把它再生成回来。"));
    } catch (e) { notify(e.message); }
  }

  /** 导出：默认不带聊天历史（6 MB 量级、含工具结果原文）—— 勾了才带。 */
  async function doExport() {
    setBusy(true);
    try {
      const got = await client.callBlob("/api/memory/export",
        jsonRequest("POST", { include_history: includeHistory }));
      const url = URL.createObjectURL(got.blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = got.filename || "crannia-memory.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      notify(t("已导出记忆包。"));
      setXfer("");
    } catch (e) { notify(e.message); }
    finally { setBusy(false); }
  }

  /** 导入第一段：只看计划（`dry_run=1`）。冲突逐条列名字，不替用户选。 */
  async function pickZip(e) {
    const f = e.target.files && e.target.files[0];
    if (e.target) e.target.value = "";     // 允许重复选同一个文件
    if (!f) return;
    setZipBlob(f);
    setZipName(f.name);
    setPlan(null);
    setXfer("import");
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", f);
      form.append("dry_run", "1");
      const d = await client.callForm("/api/memory/import", form);
      setPlan(d);
    } catch (err) { notify(err.message); setXfer(""); setZipBlob(null); }
    finally { setBusy(false); }
  }

  /** 导入第二段：`on_conflict` 必须由用户选（覆盖 / 跳过冲突）。
   *  后端没给默认值 —— 那正是"三选一不替你选"的落法。 */
  async function applyZip(onConflict) {
    if (!zipBlob) return;
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", zipBlob);
      form.append("dry_run", "0");
      form.append("on_conflict", onConflict);
      if (withHistory) form.append("with_history", "1");
      const d = await client.callForm("/api/memory/import", form);
      //:用户「已导入 {n} 条」—— 只写这个，不再报跳过/封条细分。
      notify(t("已导入 {n} 条。", { n: d.written || 0 }));
      setXfer("");
      setPlan(null);
      setZipBlob(null);
      await load();
    } catch (e) { notify(e.message); }
    finally { setBusy(false); }
  }

  const allEntries = (data && data.entries) || [];
  const _kw = q.trim().toLowerCase();
  //: 命中判据 = 名字 ∪ 描述（都转小写比）。空词就是全量，不做任何排序改动 ——
  //:  列表的次序由后端给（按 slug 排），这里只筛不改序。
  const entries = _kw
    ? allEntries.filter((e) => (String(e.name || "") + " " + String(e.description || ""))
        .toLowerCase().includes(_kw))
    : allEntries;
  const tombs = (data && data.tombstones) || [];
  const editing = isNew || sel;
  const conflicts = (plan && plan.conflicts) || [];
  const counts = (plan && plan.counts) || {};

  return (
    <div className="mem-page">
      <div className="mem-head">
        <button className="mem-back" onClick={onBack}>{t("← 返回设置")}</button>
        <h2>{t("记忆库")}{data ? (
          //: 筛选中同时给两个数：筛出来的和总数 —— 只给一个会把
          //:  "库里只剩这么多" 说成事实。
          <small>{_kw ? t("（{n}/{m} 条）", { n: entries.length, m: allEntries.length })
                      : t("（{n} 条）", { n: allEntries.length })}</small>
        ) : null}</h2>
        <label className="mem-seal" title={t("关掉之后，删掉的条目可能被后台抽取重新生成。")}>
          <input type="checkbox" checked={autoSeal} disabled={busy}
                 onChange={(e) => toggleSeal(e.target.checked)} />
          {t("删除时自动封条")}
        </label>
        <div className="log-acts">
          <button className="skill-btn" disabled={busy}
                  onClick={() => { setIncludeHistory(false); setXfer("export"); }}>
            {t("导出")}
          </button>
          <button className="skill-btn" disabled={busy}
                  onClick={() => zipRef.current && zipRef.current.click()}>
            {t("导入")}
          </button>
          <input ref={zipRef} type="file" accept=".zip,application/zip"
                 style={{ display: "none" }} onChange={pickZip} />
        </div>
      </div>

      <div className="mem-body">
        <div className="mem-list">
          {/* 筛选框放在「新建一条」上面：找一条旧记忆的频次远高于新建，
              而列表按名字排、五六十条靠滚很容易漏（2026-11-02 现场）。 */}
          <input className="mem-search" value={q} disabled={busy}
                 placeholder={t("筛选：名字或描述")}
                 onChange={(e) => setQ(e.target.value)} />
          <button className="skill-btn" onClick={startNew}>{t("新建一条")}</button>
          {entries.length === 0 ? (
            <p className="muted">{_kw ? t("没有匹配的条目。") : t("还没有任何条目。")}</p>
          ) : null}
          {entries.map((e) => (
            <button key={e.slug}
                    className={"mem-item" + (sel === e.name ? " on" : "")}
                    onClick={() => open(e.name)}>
              <b>{e.name}</b>
              <span>{e.description}</span>
              <em>{t("{n} 字符", { n: e.chars })}</em>
            </button>
          ))}
          {tombs.length ? (
            <div className="mem-tombs">
              <h3>{t("已删除（封条）")}</h3>
              {tombs.map((t0) => (
                <div className="mem-tomb" key={t0.slug}>
                  <span title={t0.at || ""}>{t0.name || t0.slug}</span>
                  <button className="link" disabled={busy}
                          onClick={() => unseal(t0.slug)}>{t("解除")}</button>
                </div>
              ))}
            </div>
          ) : null}
        </div>

        <div className="mem-detail">
          {!editing ? <p className="muted">{t("左边选一条，或者新建一条。")}</p> : (
            <>
              <div className="mem-field">
                <label>{t("名字")}</label>
                <input value={draft.name} disabled={!isNew || busy}
                       placeholder={t("短标题，会进索引")}
                       onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
                {!isNew ? <span className="muted">{t("名字是身份，编辑时不能改；要改名请删掉再新建。")}</span> : null}
              </div>
              <div className="mem-field">
                <label>{t("一句话描述")}</label>
                <input value={draft.description} disabled={busy}
                       placeholder={t("这句会显示在索引里")}
                       onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
              </div>
              <div className="mem-field grow">
                <label>{t("正文")}</label>
                <textarea value={draft.content} disabled={busy}
                          onChange={(e) => setDraft({ ...draft, content: e.target.value })} />
              </div>
              <div className="mem-acts">
                <button className="skill-btn" disabled={busy} onClick={save}>{t("保存")}</button>
                {!isNew ? (
                  <button className={"skill-btn" + (confirm === sel ? " skill-btn-danger" : "")}
                          disabled={busy} onClick={() => remove(sel)}>
                    {confirm === sel ? t("确认删除（内容不保留）") : t("删除")}
                  </button>
                ) : null}
                {confirm === sel ? (
                  <button className="link" onClick={() => setConfirm("")}>{t("取消")}</button>
                ) : null}
              </div>
            </>
          )}
        </div>
      </div>

      {/* 记忆包的两个浮窗。
          复用 `.backdrop` + `.safety` 那一套 —— 不新造第三种浮窗。 */}
      {xfer === "export" ? (
        <div className="backdrop" onClick={busy ? undefined : () => setXfer("")}>
          <section className="safety" role="dialog" aria-modal="true"
                   onClick={(e) => e.stopPropagation()}>
            <div className="safety-head"><h2 className="safety-title">{t("导出记忆包")}</h2></div>
            <p className="safety-body">
              {t("携带内容：长期事实 · 会话笔记 · 删除账本")}
            </p>
            <label className="xfer-line">
              <input type="checkbox" checked={includeHistory} disabled={busy}
                     onChange={(e) => setIncludeHistory(e.target.checked)} />
              {t("勾选：聊天历史（含工具结果原文，请注意隐私保护和存储空间）")}
            </label>
            <div className="safety-actions">
              <button className="safety-go" disabled={busy} onClick={doExport}>
                {t("导出")}
              </button>
              <button className="skill-btn" disabled={busy}
                      onClick={() => setXfer("")}>{t("取消")}</button>
            </div>
          </section>
        </div>
      ) : null}

      {xfer === "import" ? (
        <div className="backdrop" onClick={busy ? undefined : () => setXfer("")}>
          <section className="xfer" role="dialog" aria-modal="true"
                   onClick={(e) => e.stopPropagation()}>
            <div className="safety-head"><h2 className="safety-title">{t("导入记忆包")}</h2></div>
            <p className="safety-muted">{zipName}</p>
            {!plan ? <p className="safety-body">{t("正在检查这个包…")}</p> : (
              <>
                <p className="safety-body">
                  {t("新增 {n} 条 · 与本地有差异 {c} 条 · 无差异 {same} 条", {
                    n: counts.new || 0, c: counts.conflict || 0,
                    same: counts.identical || 0,
                  })}
                </p>
                {/* 冲突确认的标题 */}
                {conflicts.length ? (
                  <>
                    <p className="xfer-warn">
                      {t("存在冲突条目，继续导入会覆盖。冲突条目：")}
                    </p>
                    <ul className="xfer-conflicts">
                      {conflicts.map((c) => (
                        <li key={c.path}>
                          <b>{c.name}</b>
                          <span className="xfer-muted">{c.path}</span>
                        </li>
                      ))}
                    </ul>
                  </>
                ) : null}
                {plan.has_history ? (
                  <label className="xfer-line">
                    <input type="checkbox" checked={withHistory} disabled={busy}
                           onChange={(e) => setWithHistory(e.target.checked)} />
                    {t("历史勾选：并入包内聊天历史（按主键去重，本机已有行不变）")}
                  </label>
                ) : null}
                {(plan.warnings || []).length ? (
                  <ul className="xfer-warn">
                    {plan.warnings.map((w, i) => <li key={i}>{w}</li>)}
                  </ul>
                ) : null}
              </>
            )}
            <div className="safety-actions">
              {conflicts.length ? (
                <>
                  <button className="safety-go" disabled={busy}
                          onClick={() => applyZip("overwrite")}>
                    {t("覆盖")}
                  </button>
                  <button className="skill-btn" disabled={busy}
                          onClick={() => applyZip("skip")}>
                    {t("跳过冲突")}
                  </button>
                </>
              ) : (
                <button className="safety-go" disabled={busy || !plan}
                        onClick={() => applyZip("skip")}>
                  {t("导入")}
                </button>
              )}
              <button className="skill-btn" disabled={busy}
                      onClick={() => setXfer("")}>{t("取消")}</button>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}
