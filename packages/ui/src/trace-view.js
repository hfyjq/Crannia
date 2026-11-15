// 「逐条轨迹」的纯函数读者 —— 零依赖，node 直接跑。
//
// 判据的权威在 `core/trace.py`（记录形状）与 `web/server.py::api_trace`
//   （按会话拉取）。这里只管"怎么把它摆成人能看的样子"。

/** 一条记录的主标题。工具用名字，思考固定一个词。 */
import { t } from "./i18n.js";

export function recordTitle(r) {
  const row = r || {};
  if (row.kind === "thinking") return t("思考");
  return String(row.name || t("（没有名字）"));
}

/** 工具调用成没成。思考没有 ok 这一格，不画勾叉（不是"成功"）。 */
export function recordOk(r) {
  const row = r || {};
  if (row.kind === "thinking") return null;
  return row.ok !== false;
}

/** 毫秒 → `1.2s` / `340ms`。负数 / 坏值 → 空串（不猜 0）。 */
export function formatDuration(ms) {
  //:`Number(null)` 是 0、`Number("")` 也是 0，不显式挡掉的话，
  //:   "没有这个数"会被画成 "0ms"（读不到 ≠ 0）。
  if (ms == null || ms === "") return "";
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return "";
  return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${Math.round(n)}ms`;
}

/** 折一行预览：换行压成空格、超长加省略号。 */
export function preview(text, max = 120) {
  const s = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
  if (s.length <= max) return s;
  return s.slice(0, max) + "…";
}

/** 增量合并（按 `seq` 去重，保留旧的那份顺序）。 */
export function mergeRecords(old, incoming) {
  const out = Array.isArray(old) ? old.slice() : [];
  const seen = new Set(out.map((r) => Number(r && r.seq) || 0));
  for (const r of (Array.isArray(incoming) ? incoming : [])) {
    const seq = Number(r && r.seq) || 0;
    if (seq && seen.has(seq)) continue;
    if (seq) seen.add(seq);
    out.push(r);
  }
  return out;
}

/** 开关关着 vs 开了但还没有记录 —— 两件事，不许显示成同一屏。 */
export function emptyState(data) {
  //:载荷还没到（`null`），空串 = "再等等"。把它读成"关着"会在
  //:   打开面板的第一帧就闪一句假话。
  if (!data) return "";
  const d = data;
  if (d.enabled === false) {
    return "off";                      // 去设置页打开
  }
  if ((d.records || []).length === 0) {
    return "empty";                    // 打开了，这一轮还没发生什么
  }
  return "";
}
