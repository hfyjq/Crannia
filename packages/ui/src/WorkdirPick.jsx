import React, { useState } from "react";
import { Folder, FolderOpen, RotateCcw } from "lucide-react";

// 草稿态那一格「选择工作区」。
//
// 判据：
//
//   · 只在草稿态存在 —— 发出去之后那一格整个不显示
//     （用位置的移动表达状态转换，那个控件因此根本不需要有"不可改"的形态）。
//   · 框外 · 居中 —— 它是 `Composer` 的兄弟，不是框内那一行。
//   · 发送后那一格什么都不显示 ——
//     目录在哪由左栏按目录分组承载，这一格只留
//     「内容由AI生成，请核实重要信息」那一句。
//
// 选目录走已有的 `POST /api/browse-folder`（原生目录选择器，
//   `web/server.py`）—— 那是 WebUI 就在用的同一条路，不新开 IPC
//   （桌面壳的 preload 白名单是最小集，不为这件事放宽）。
//
// 校验不在这一层：路径能不能用由后端 `session_workspace.check_dir`
//   一处判。这里只负责"选到"与"拿在手里"。

/** 路径太长时只显示叶子那一节（完整路径进 `title`，悬停看得到）。 */
import { t } from "./i18n.js";
function tailName(path) {
  const s = String(path || "").replace(/[\\/]+$/, "");
  const parts = s.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : s;
}

export function WorkdirPick({ client, value, onPick, notify, disabled }) {
  const [busy, setBusy] = useState(false);

  async function pick() {
    if (busy || disabled) return;
    setBusy(true);
    try {
      const r = await client.call("/api/browse-folder", { method: "POST" });
      if (r && r.ok && r.path) onPick(String(r.path));
      else if (r && r.error && r.error !== "未选择目录") notify && notify(r.error);
    } catch (e) {
      notify && notify(t("选目录没成功：") + (e && e.message ? e.message : e));
    } finally {
      setBusy(false);
    }
  }

  const has = !!value;
  return (
    <div className="ws-pick">
      <button
        className={"ws-pick-btn" + (has ? " on" : "")}
        onClick={pick}
        disabled={busy || disabled}
        title={has ? value : t("选一个目录：这一段对话的产出落在那里")}
      >
        {has ? <FolderOpen size={14} /> : <Folder size={14} />}
        {has ? tailName(value) : t("选择工作区")}
      </button>
      {has ? (
        <button className="ws-pick-reset" onClick={() => onPick("")}
                title={t("不选了，这一段回到默认工作区")}>
          <RotateCcw size={12} />{t("用默认")}
        </button>
      ) : null}
    </div>
  );
}
