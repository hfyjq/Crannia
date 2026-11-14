// 提问面板的交互判据 —— 纯函数，不认识 React。
//
// 它与审批盘共用"选中 → 确认"那两步，但语义不同：审批是"要授权"，
//    提问是"问意见"。所以：没有"不同意"这一项，也没有升级项；
//    用户可以不选，直接在下面输入框里说别的（那段话会原样转给模型 ——
//    `core/ask_user.py::FREE_TEXT_NOTE` 是它对应的回传语义）。

/** 选项表（没有就给空表 —— 每一步都要能对空表不炸）。 */
function optsOf(payload) {
  const o = payload && payload.options;
  return Array.isArray(o) ? o : [];
}

/** 选/取消选一项。多选才是开关，单选是替换。回新的 keys 数组。 */
export function toggleKey(payload, keys, key) {
  const k = String(key == null ? "" : key);
  const hit = optsOf(payload).some((o) => String(o.key) === k);
  if (!hit) return Array.isArray(keys) ? keys : [];
  const cur = Array.isArray(keys) ? keys.map(String) : [];
  if (!payload || !payload.multi) return [k];
  return cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k];
}

/** 键盘数字键 → 该选中哪个真键位（按显示位置）。越界回空串。 */
export function keyAtDisplayNumber(payload, n) {
  const i = Number(n) - 1;
  const opts = optsOf(payload);
  return (Number.isInteger(i) && i >= 0 && i < opts.length) ? String(opts[i].key) : "";
}

/** 能不能提交：选了至少一项，或者输入框里有字。 */
export function canSubmit(keys, text) {
  return (Array.isArray(keys) && keys.length > 0)
    || String(text == null ? "" : text).trim().length > 0;
}

/** 回传体。只给了文字，`{text}`（那条"没做完选择、想讨论"的路）。 */
export function buildAnswer(keys, text) {
  const t = String(text == null ? "" : text).trim();
  if (t) return { ok: true, body: { text: t } };
  const ks = (Array.isArray(keys) ? keys : []).map(String).filter(Boolean);
  if (!ks.length) return { ok: false, body: null };
  return { ok: true, body: { choice: ks.join(",") } };
}

/** 底部提示。 */
export function hintText(payload) {
  return payload && payload.multi
    ? "可以选多个，回车确认；或者直接说你的想法"
    : "数字或点击选择，回车确认；或者直接说你的想法";
}
