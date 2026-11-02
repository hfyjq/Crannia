import React, { useCallback, useEffect, useState } from "react";
import { Clock3, Pause, Play, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { jsonRequest } from "./api.js";
import { TestRunModal } from "./TestRunModal.jsx";

//: 「多久跑一次」的三种大类型 —— 与后端 `rule.kind` 一一对应（`repeat`/`every`/`once`）。
//:这三档是时间表达的全部，所以这里没有第四个选项，
//:也没有 cron 输入框 —— 我们的控件与规则对象是 1:1，缺的那两样（"每 50 小时"、
//:   "4月21 或 6月1"）分别由 `every` 与 `selectors` 并集表达。
import { t } from "./i18n.js";
const KINDS = [
  ["calendar", "按日历重复"],
  ["every", "固定间隔"],
  ["once", "仅一次"],
];

//: 「按日历重复」下有三行（每周 / 每月 / 每年）—— 它们可以一起勾、
//:取并集（命中任一即跑）；一行都不勾 = 每天
//: （与后端"`selectors` 空数组 = 每天"逐字同一条规矩）。

const WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
//: ISO 口径：`1 = 周一 … 7 = 周日`（不是 cron 的 `0 = 周日`）。
//:下面 `toRule` 里那个 `i + 1` 就是它 —— 后端对 `0` 会专门报错，
//:所以这里写错会当场看见（预览那一格会红），不会静默跑错一天。
const pad = (n) => String(n).padStart(2, "0");

/** 表单状态 → 规则对象。这是本文件里唯一的"逻辑" ——
 *  规则怎么解释（合法吗、中文怎么说、下次是什么时候）全在后端
 *  （`POST /api/cron/preview`），这里只做"把控件拼成一个对象"。
 *，前端不重写规则语义（重写就是第二套真相，而两套给出的答案看着都对）。
 */
function toRule(f) {
  const tz = (f.tz || "").trim();
  const withTz = (o) => (tz ? { ...o, tz } : o);

  if (f.kind === "once") return withTz({ kind: "once", at: f.onceAt });
  if (f.kind === "every") {
    const n = Number(f.interval) || 0;
    const mult = { minute: 60, hour: 3600, day: 86400 }[f.unit] || 60;
    return withTz({ kind: "every", seconds: Math.round(n * mult) });
  }
  const selectors = [];
  if (f.weekdays.length) selectors.push({ type: "weekdays", values: f.weekdays });
  if (f.monthDays.length) selectors.push({ type: "month_days", values: f.monthDays });
  if (f.dates.length) selectors.push({ type: "dates", values: f.dates });
  return withTz({
    kind: "repeat",
    at: `${pad(f.hour)}:${pad(f.minute)}`,
    // 一个都不勾，空数组，后端读作"每天"。
    selectors,
  });
}

const EMPTY_FORM = {
  kind: "calendar", title: "", prompt: "", deliver: "here", tz: "",
  hour: 8, minute: 30,
  weekdays: [], monthDays: [], dates: [],
  interval: 30, unit: "minute",
  onceAt: "",
};

function Chip({ on, children, ...rest }) {
  return <button type="button" className={on ? "chip on" : "chip"} {...rest}>{children}</button>;
}

function Schedules({ client, connected, onToast }) {
  const [jobs, setJobs] = useState([]);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState(null);        // null = 表单关着
  const [draft, setDraft] = useState(EMPTY_FORM);
  const [preview, setPreview] = useState(null);
  const [saving, setSaving] = useState(false);
  //:正在试跑的那条（`null` = 浮窗关着）。试跑在一个临时浮窗里跑。
  const [testing, setTesting] = useState(null);
  //:二次确认 ——「删掉一条定时任务」不可恢复（`cron_delete` 是真删，
  //:   没有回收站）。
  //:`""` = 没有待确认的行；否则是那一行的 `job_id`（按行记，不是全局开关 ——
  //:列表里同时挂着两条任务时，一个布尔会让另一行看起来也在等确认）。
  //:与名册的删除同形（`App.jsx` 的 `ctxMenu.confirm`）：第一次点只是"举手"，
  //:   第二次点才真删；取消 / 重新拉列表都会把它清掉。
  const [confirmDel, setConfirmDel] = useState("");

  const load = useCallback(async () => {
    if (!connected) return;
    try {
      const data = await client.call("/api/cron");
      setJobs(data.jobs || []);
      setConfirmDel("");        // 列表一变，「举手」就作废（那一行可能已经不在了）
    } catch (e) {
      onToast(`取定时任务失败：${e.message}`);
    }
  }, [client, connected, onToast]);

  useEffect(() => { load(); }, [load]);

  // ── 预览：每次表单变了就问后端（合法吗 / 中文怎么说 / 下次何时）──
  // 判据：这三件事只有后端知道。前端自己拼一句"每天 08:30"看着像对的，
  //   而它与真跑的那一刻可能不同（那正是最难查的一种）。
  useEffect(() => {
    if (!form) return;
    let dead = false;
    const rule = toRule(draft);
    client.call("/api/cron/preview", jsonRequest("POST", { rule }))
      .then((d) => { if (!dead) setPreview({ ...d, ok: true }); })
      .catch((e) => { if (!dead) setPreview({ ok: false, error: e.message }); });
    return () => { dead = true; };
  }, [client, form, draft]);

  function openForm() {
    setDraft(EMPTY_FORM);
    setPreview(null);
    setForm(true);
  }

  function toggleIn(key, value) {
    setDraft((d) => {
      const arr = d[key];
      const next = arr.includes(value) ? arr.filter((x) => x !== value) : arr.concat([value]).sort((a, b) => a - b);
      return { ...d, [key]: next };
    });
  }

  async function submit() {
    setSaving(true);
    try {
      const out = await client.call("/api/cron/create", jsonRequest("POST", {
        title: draft.title, prompt: draft.prompt, deliver: draft.deliver,
        rule: toRule(draft),
      }));
      onToast(`建好了：${out.when}`);
      setForm(null);
      await load();
    } catch (e) {
      onToast(e.message);              // 这一句就是后端那句（带路线），不是前端另写的
    } finally {
      setSaving(false);
    }
  }

  async function act(path, body, okMsg) {
    setBusy(true);
    try {
      await client.call(path, jsonRequest("POST", body));
      if (okMsg) onToast(okMsg);
      await load();
    } catch (e) {
      onToast(e.message);
    } finally {
      setBusy(false);
    }
  }

  // 试跑 = 打开浮窗。
  //    这里不直接打那个端点 —— 打端点、看结果、关掉时清理，全在浮窗里
  //    （它要在跑之前先订上事件，否则收不到步骤流）。
  //    结果不往 toast 里塞 —— 结果该在浮窗里完整看（toast 会截断）。
  function test(job) {
    setTesting(job);
  }

  const rule = form ? toRule(draft) : null;

  return (
    <section className="page">
      <div className="page-head">
        <div><small>CRANNIA</small><h1>{t("定时任务")}</h1></div>
        <div className="head-actions">
          <button onClick={load} disabled={!connected}><RefreshCw size={15} />{t("刷新")}</button>
          <button onClick={openForm} disabled={!connected}><Plus size={15} />{t("新建任务")}</button>
        </div>
      </div>

      {!connected ? <p className="muted">{t("启动本地服务后查看定时任务。")}</p> : (
        <div className="cron-list">
          {jobs.map((j) => (
            <div className={j.enabled ? "cron-row" : "cron-row off"} key={j.job_id}>
              <Clock3 size={17} />
              <div className="cron-main">
                <b>{j.title}</b>
                <span>{j.when}{j.next_run
                  ? t(" · 下次 {at}", { at: j.next_run.slice(0, 16).replace("T", " ") })
                  : ""}</span>
                <span className="cron-stat">
                  {j.run_count ? t("跑过 {j.run_count} 次", { "j.run_count": j.run_count }) : t("还没跑过")}
                  {j.last_status
                    ? t(" · 上次 {r}", { r: j.last_status === "ok" ? t("成功") : t("失败") })
                    : ""}
                  {/* `empty_reply` = 跑完了但一个字都没产出 ——
                      它必须与"正常成功"看起来不一样，否则用户以为它干活了。 */}
                  {j.last_reason === "empty_reply" ? t(" · 没有产出") : ""}
                  {j.last_error ? ` · ${String(j.last_error).slice(0, 60)}` : ""}
                  {j.enabled ? "" : t(" · 已暂停")}
                  {/* 台账：上一次它做了哪些动作（到点时没人在场，
                      这是唯一能事后核查的东西）。`n` 是真数，`list` 可能被截断。 */}
                  {j.last_actions && j.last_actions.n > 0 && (
                    <span className="cron-acts-line"
                          title={(j.last_actions.list || []).join(" → ")}>
                      {t(" · 上次做了 {n} 步：", { n: j.last_actions.n })}
                      {(j.last_actions.list || []).slice(0, 3).join("、")
                        + (j.last_actions.n > 3 ? "…" : "")}
                    </span>
                  )}
                </span>
              </div>
              <div className="cron-acts">
                <button disabled={busy} onClick={() => act("/api/cron/toggle",
                  { job_id: j.job_id, enabled: !j.enabled }, j.enabled ? t("已暂停") : t("已恢复"))}
                  title={j.enabled ? t("暂停") : t("恢复")}>
                  {j.enabled ? <Pause size={15} /> : <Play size={15} />}
                </button>
                <button disabled={busy} onClick={() => test(j)} title={t("立刻跑一次看看效果")}>
                  <Play size={15} /><span className="btn-word">{t("试跑")}</span>
                </button>
                {confirmDel === j.job_id ? (
                  /* 二次确认 —— 文案照名册那条（`App.jsx`）
                     「确认删除（不可恢复）」，两处说同一件事就用同一句话。 */
                  <>
                    <span className="cron-confirm">{t("确认删除？")}</span>
                    <button className="danger" disabled={busy}
                            onClick={() => act("/api/cron/delete",
                              { job_id: j.job_id }, t("删掉了「{j.title}」", { "j.title": j.title }))}
                            title={t("确认删除（不可恢复）")}>
                      <Trash2 size={15} /><span className="btn-word">{t("删除")}</span>
                    </button>
                    <button disabled={busy} onClick={() => setConfirmDel("")} title={t("取消")}>
                      <X size={15} /><span className="btn-word">{t("取消")}</span>
                    </button>
                  </>
                ) : (
                  <button disabled={busy} onClick={() => setConfirmDel(j.job_id)}
                          title={t("删除（会再问一次）")}>
                    <Trash2 size={15} />
                  </button>
                )}
              </div>
            </div>
          ))}
          {!jobs.length && (
            <p className="muted">{t("还没有定时任务。点右上角「新建任务」，或直接在对话里说。")}</p>
          )}
        </div>
      )}

      {testing && (
        <TestRunModal job={testing} client={client}
                      onClose={() => { setTesting(null); load(); }} />
      )}

      {form && (
        <div className="backdrop" onMouseDown={(e) => e.target === e.currentTarget && setForm(null)}>
          <section className="cron-form">
            <div className="settings-head">
              <div><small>CRANNIA</small><h2>{t("新建定时任务")}</h2></div>
              <button className="round" onClick={() => setForm(null)}><X size={18} /></button>
            </div>

            <label className="field"><span>{t("名字")}</span>
              <input value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })}
                     placeholder={t("早报")} /></label>

            <label className="field"><span>{t("任务")}</span>
              <textarea value={draft.prompt} rows={3}
                        onChange={(e) => setDraft({ ...draft, prompt: e.target.value })}
                        placeholder={t("总结今天的新闻，挑三条最重要的")} /></label>

            <div className="field"><span>{t("频率")}</span>
              <div className="modes">
                {KINDS.map(([k, label]) => (
                  <button key={k} className={draft.kind === k ? "selected" : ""}
                          onClick={() => setDraft({ ...draft, kind: k })}>{t(label)}</button>
                ))}
              </div>
            </div>

            {draft.kind === "once" && (
              <label className="field"><span>{t("什么时候（本地时间）")}</span>
                <input type="datetime-local" value={draft.onceAt}
                       onChange={(e) => setDraft({ ...draft, onceAt: e.target.value })} /></label>
            )}

            {draft.kind === "every" && (
              <div className="field"><span>{t("间隔（最短 5 分钟）")}</span>
                <div className="row">
                  <input type="number" min="1" value={draft.interval}
                         onChange={(e) => setDraft({ ...draft, interval: e.target.value })} />
                  <div className="modes">
                    {[["minute", t("分钟")], ["hour", t("小时")], ["day", "天"]].map(([u, l]) => (
                      <button key={u} className={draft.unit === u ? "selected" : ""}
                              onClick={() => setDraft({ ...draft, unit: u })}>{l}</button>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {draft.kind === "calendar" && (
              <>
                <div className="field"><span>{t("时间")}</span>
                  <div className="row">
                    <input type="number" min="0" max="23" value={draft.hour}
                           onChange={(e) => setDraft({ ...draft, hour: Number(e.target.value) })} />
                    <b>:</b>
                    <input type="number" min="0" max="59" value={draft.minute}
                           onChange={(e) => setDraft({ ...draft, minute: Number(e.target.value) })} />
                  </div>
                </div>

                <div className="field">
                  <span>{t("选取时间；留空 = 每天")}</span>

                  <div className="chip-row"><em>{t("每周")}</em>
                    <div className="chips">
                      {WEEKDAY_LABELS.map((label, i) => (
                        <Chip key={label} on={draft.weekdays.includes(i + 1)}
                              onClick={() => toggleIn("weekdays", i + 1)}>{t(label)}</Chip>
                      ))}
                    </div>
                  </div>

                  <div className="chip-row"><em>{t("每月")}</em>
                    <div className="chips">
                      {Array.from({ length: 31 }, (_, i) => i + 1).map((d) => (
                        <Chip key={d} on={draft.monthDays.includes(d)}
                              onClick={() => toggleIn("monthDays", d)}>{d}</Chip>
                      ))}
                    </div>
                  </div>

                  <div className="chip-row"><em>{t("每年")}</em>
                    <div className="chips">
                      {draft.dates.map(([m, d], i) => (
                        <span className="datepick" key={`${m}-${d}-${i}`}>
                          <input type="number" min="1" max="12" value={m}
                                 onChange={(e) => setDraft({
                                   ...draft,
                                   dates: draft.dates.map((x, k) => (k === i ? [Number(e.target.value), x[1]] : x)),
                                 })} /> {t("月")}
                          <input type="number" min="1" max="31" value={d}
                                 onChange={(e) => setDraft({
                                   ...draft,
                                   dates: draft.dates.map((x, k) => (k === i ? [x[0], Number(e.target.value)] : x)),
                                 })} /> {t("日")}
                          <button type="button"
                                  onClick={() => setDraft({ ...draft, dates: draft.dates.filter((_, k) => k !== i) })}>
                            <X size={13} />
                          </button>
                        </span>
                      ))}
                      <Chip on={false}
                            onClick={() => setDraft({ ...draft, dates: draft.dates.concat([[1, 1]]) })}>
                        {t("+ 加一个日期")}
                      </Chip>
                    </div>
                  </div>
                </div>
              </>
            )}

            <label className="field"><span>{t("时区（留空 = 本机）")}</span>
              <input value={draft.tz} onChange={(e) => setDraft({ ...draft, tz: e.target.value })}
                     placeholder="Asia/Shanghai" /></label>

            <div className="field"><span>{t("发送位置")}</span>
              <div className="modes">
                {[["here", t("这条对话")], ["new", t("另起一段")]].map(([k, l]) => (
                  <button key={k} className={draft.deliver === k ? "selected" : ""}
                          onClick={() => setDraft({ ...draft, deliver: k })}>{l}</button>
                ))}
              </div>
            </div>

            {/* 这一格是后端算的（`/api/cron/preview`）—— 前端不拼这句话。 */}
            <div className={preview && !preview.ok ? "preview bad" : "preview"}>
              {preview && !preview.ok ? preview.error
                : preview
                  ? t("将会：{when}", { when: preview.when })
                    + (preview.next_run
                        ? t(" · 下一次 {at}", { at: preview.next_run.slice(0, 16).replace("T", " ") })
                        : "")
                    + (preview.note ? ` · ${preview.note}` : "")
                  : t("正在算…")}
            </div>

            <footer>
              <span>{t("任务由本地服务保存")}</span>
              <button disabled={saving || !(preview && preview.ok)} onClick={submit}>
                {saving ? t("正在建…") : t("建好它")}
              </button>
            </footer>
          </section>
        </div>
      )}
    </section>
  );
}

export { Schedules };
