// 产物记录的展示判据（纯函数，node 可逐条断言）。
//
// 为什么单独一个模块：批 1 把"能不能存"和"能不能在聊天区
//   直接画/播"拆成了两个闸（`core/artifacts.py`）。界面上得有一处
//   只回答后者 —— 而它必须能被真跑（`tests/test_artifact_limits.py`
//   用 node 断言，同 `test_desktop_toasts.py` 对 `toasts.js` 的做法）。
//
// 大小门槛与颜色/排版无关，是产品判据（用户落盘不设限，
//   单个超过 1GB 时右上角给一次通知），它住在这里，不散在 JSX 里。

/** 单个产物超过它，右上角通知一次。 */
export const LARGE_ARTIFACT_BYTES = 1024 * 1024 * 1024;

/** 这条记录能不能内联画 / 播。
 *
 *  老记录没有 `inline` 这一格（字段是批 1 才加的）
 *    缺字段按可内联。否则升级之后，历史里所有的图都会变成下载行
 *    —— 那不是"更保守"，那是把已经能看的东西弄没了。
 */
export function isInline(rec) {
  return !rec || rec.inline !== false;
}

/** 人类可读的大小；读不到，空串（读不到 ≠ 0，与后端同一条）。 */
export function formatSize(bytes) {
  const n = typeof bytes === "number" && isFinite(bytes) && bytes >= 0
    ? bytes : null;
  if (n === null) return "";
  if (n < 1024) return n + " B";
  const kb = n / 1024;
  if (kb < 1024) return kb.toFixed(kb < 10 ? 1 : 0) + " KB";
  const mb = kb / 1024;
  if (mb < 1024) return mb.toFixed(mb < 10 ? 1 : 0) + " MB";
  return (mb / 1024).toFixed(1) + " GB";
}

/** 够不够格弹"很大的产物"。
 *
 *  判据只看 `size`：缺失/非数，不弹（不确定的事不打扰人）。
 */
export function needsLargeNotice(rec) {
  return !!(rec && typeof rec.size === "number"
    && isFinite(rec.size) && rec.size > LARGE_ARTIFACT_BYTES);
}
