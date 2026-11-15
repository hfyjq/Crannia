import React, { useEffect, useState } from "react";
import { Plug } from "lucide-react";
import { jsonRequest } from "./api.js";

// 「MCP 管理」浮窗。
//
// 内置的后面带黄标「内置」，所有条目都可停用。
//
// 与「外部来源集合」共用同一个外壳（`.backdrop` + `.skill-panel*`）——
//   规矩是「不新造第三种浮窗」。两个浮窗都在「技能与连接器」这一屏上，
//   所以外壳的类名沿用 `.skill-panel`（不是又一个 `mcp-panel`）。
//
// 按钮上写的是「停用 / 启用」，不是「卸载」 —— 这是故意的：
//     · 内置那几条是随代码走的（`mcpx/servers/`，不在 `skills/` 里、
//       也没有配置项），删不掉；
//     · 外部那几条的配置在 `data/mcp_servers.json` —— 那是用户自己写的文件，
//       我们不该替他删（删了他还得自己重新敲一遍，而且没有撤销）。
//   所以两边能做、且真做了的，都是"停用它"。标签跟着事实走（标签不许说谎）。
//
// 停用是可逆的：它只改筛子（`bus/mcp_disabled`），不摘注册表 ——
//   所以「启用」当场就回来，不需要重启。收尾的那句回执由后端给。

import { t } from "./i18n.js";
export function McpPanel({ client, notify, onClose }) {
  //:`null` = 还没读到（与"读到了、但一台都没有"是两件事）。
  const [servers, setServers] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  //:点开一个卡片就展开并展示全部内容，点一下 × 再平滑缩回去。
  //:与集合浮窗同一套（一次只开一个）。
  const [openCard, setOpenCard] = useState("");

  /** 点卡片 = 展开 / 再点 = 收起。点里面的按钮不算（`停用` / `×`）。 */
  function onCardClick(e, name) {
    if (e.target.closest("button")) return;
    setOpenCard((cur) => (cur === name ? "" : name));
  }

  async function load() {
    try {
      const d = await client.call("/api/mcp/servers");
      setServers(d.servers || []);
      //:后端读不到时会带一个 `error` —— 原样留着（"没有服务器"与
      //:   "我没读到"长得一样，而前者会被读成一个结论）。
      setErr(d.error || "");
    } catch (e) {
      setServers([]);
      setErr(e.message);
    }
  }
  useEffect(() => { load(); }, [client]);   // eslint-disable-line

  async function toggle(s) {
    setBusy(s.name);
    try {
      //:两条路都是字面路由（有一条两边对账的读者要求这一点）。
      const r = await client.call(s.disabled ? "/api/mcp/enable" : "/api/mcp/disable",
                                  jsonRequest("POST", { name: s.name }));
      notify((r && r.message) || (s.disabled ? t("已启用") : t("已停用")));
      await load();
    } catch (e) {
      notify(e.message);
    } finally {
      setBusy("");
    }
  }

  const head = (
    <div className="skill-panel-head">
      <h2 className="skill-panel-title"><Plug size={15} /> {t("MCP 管理")}</h2>
      <button className="skill-btn" onClick={onClose}>{t("关闭")}</button>
    </div>
  );

  return (
    <div className="backdrop"
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="skill-panel" role="dialog" aria-modal="true">
        {head}
        <p className="skill-panel-note">
          {t("在此处管理你的MCP插件")}
        </p>
        <div className="skill-panel-body">
          {servers === null && <p className="muted">{t("读取中…")}</p>}
          {err && <p className="muted">{err}</p>}
          {servers !== null && !err && !servers.length
            && <p className="muted">{t("一台 MCP 服务器都没有。")}</p>}
          {/* 用小号卡片，与集合浮窗同一套壳（`.skill-cards` / `.skill-card`）——
              两个浮窗一种长相。只有一条时 `auto-fit` 会让它自己撑满。 */}
          {!!(servers || []).length && (
            <div className="skill-cards">
              {(servers || []).map((s) => (
                <div className={"skill-card" + (s.disabled ? " blocked" : "")
                                + (openCard === s.name ? " open" : "")}
                     key={s.name} onClick={(e) => onCardClick(e, s.name)}>
                  <div className="skill-card-head">
                    <b>{s.name}</b>
                    {openCard === s.name && (
                      <button className="skill-card-x" title={t("收起")}
                              onClick={(e) => { e.stopPropagation(); setOpenCard(""); }}>×</button>
                    )}
                  </div>
                  <span className="skill-desc">
                    {(s.tools || []).join("、") || t("（还没有工具）")}
                    {s.connected === false ? t(" · 没连上") : ""}
                  </span>
                  <span className="skill-meta">
                    {s.builtin && <i className="skill-badge skill-badge-builtin">{t("内置")}</i>}
                    {s.disabled && <i className="skill-badge skill-badge-blocked">{t("已停用")}</i>}
                  </span>
                  {/* 展开才露出来的那一行：归属键（与停用名单里那个键逐字同形，
                      出问题时对得上）。 */}
                  <span className="skill-card-more">{s.owner || ""}</span>
                  <div className="skill-card-actions">
                    <button className="skill-btn" disabled={busy === s.name}
                            onClick={() => toggle(s)}>
                      {s.disabled ? t("启用") : t("停用")}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
