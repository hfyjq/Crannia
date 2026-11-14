// 审批盘的交互判据 —— 纯函数，不认识 React。
//
// 判据与 WebUI 对齐（`web/templates/chat.html`），并落成可断言的形状：
//
//   ① 两步确认 —— 点 / 数字键只选中，回车或「确认」才提交。
//   ② 选中态（`.sel`）
//   ③ 默认选中 = `default_key`（服务端给的），永不是升级项
//   ④ 键盘全套：`1–9` 选中 · `↑↓` 环绕移动 · `Enter` 提交 · `Escape` 停靠
//   ⑤ 升级项（范围最大的那一项）：排最后 + 永不默认选中，
//      且要显式标出来 —— 不加提示就会被整个忽略。
//   ⑥ `expect === 'text'`，中间态换成输入框（那一步用户该做的是说一句话）
//
// 两条关键约定：
//   · 右上角那个 ✕ 是「停靠」，不是「拒绝」（保留位置：用户本能会去点它）。
//     代价：没有"直接拒绝"的快捷方式 —— 那是对的（拒绝要走选项，
//     才看得见自己拒绝了什么）。
//   · "不同意"必须走选项表里那个 key（按 kind 取）—— 不许再猜。
//     服务端 `core/approval.py::reply_decision` 对不在这次真给出的选项里的
//     choice 回 `REPLY_BAD_CHOICE`，端点回 409 且什么都不结算；
//     而前端已经关掉浮窗，浮窗在用户这边消失了、审批还在服务端挂着，
//     那一条永远不会被回答。
//
// 本模块只回答"该不该、是哪一项、提交什么" —— 画成什么样是 `Approval.jsx` 的事。

/** 选项表（没有就给空表 —— 每一步都要能对空表不炸）。 */
function optsOf(payload) {
  const o = payload && payload.options;
  return Array.isArray(o) ? o : [];
}

/** 初始选中哪一项。判据 ③：`default_key` 优先，而它永不是升级项。
 *
 *  三层兜底，缺一层就有一种坏法：
 *    ① 服务端给了 `default_key` 且它真的在选项表里，用它
 *       （表里没有那个 key 时用它 = 选中一个不存在的项，提交必 409）
 *    ② 服务端没给（旧载荷 / 表被清空过），第一个非升级项
 *    ③ 全是升级项（今天没有这种表，但判据要立着），空串（一项都不选）
 */
export function initialSel(payload) {
  const opts = optsOf(payload);
  const dk = String((payload && payload.default_key) || "");
  if (dk && opts.some((o) => String(o.key) === dk && !o.is_escalation)) return dk;
  const first = opts.find((o) => !o.is_escalation);
  return first ? String(first.key) : "";
}

/** 显示序号 ≠ 键位 —— 两者分开算。
 *
 *  ## 为什么要分开
 *
 *  `core/approval.py` 的 `EGRESS_OPTIONS` 里，「信任此站点」的键位是 `"6"`
 *  （新选项一律追加在末尾，键位是回传的语义，不许为显示重排 ——
 *  重排会让记得「3 = 不同意」的老习惯点错，那一下会变成永久信任某个站点）。
 *  而「全部放行」那条是升级项：`ask_index < 2` 时根本不在表里
 *，第一次问的时候选项是 `1,2,3,4,6`，屏幕上会出现跳号。
 *
 *，判据：屏幕上的数字永远连续（`1..N`），而回传的仍是真键位。
 *    数字键 `1..9` 因此按位置选（`keyAction` 那一支）。
 *
 *  如实标一个副作用：升级项出现前后，同一个显示数字含义不同
 *    （没有升级项时，`5` = 信任此站点；有升级项时，`5` = 全部放行）。
 *    这是"连续编号 + 有条件出现的项"本身带来的，躲不掉 ——
 *，界面上每一行都写着标签，而默认选中项由服务端的 `default_key` 给，
 *      从不靠数字。按下之前看一眼标签就没事。
 */
export function displayNumberOf(payload, key) {
  const i = optsOf(payload).findIndex((o) => String(o.key) === String(key || ""));
  return i === -1 ? 0 : i + 1;
}

/** 第 `n` 个显示位对应哪个真键位（`n` 从 1 起）。越界，`""`。 */
export function keyAtDisplayNumber(payload, n) {
  const i = Number(n) - 1;
  const opts = optsOf(payload);
  return (Number.isInteger(i) && i >= 0 && i < opts.length) ? String(opts[i].key) : "";
}

/** 选中某一项。只选中，不提交（判据 ①）。
 *
 *  校验这一项在不在这次真给出的表里 —— 前端拦一道，别把必然 409 的东西发出去。
 *  中间态（`expect === 'text'`）不给选项，选中请求一律拒
 *     （那一屏上压根没有选项行）。
 */
export function selectKey(payload, key) {
  if (payload && payload.expect === "text") return { ok: false, sel: "" };
  const k = String(key == null ? "" : key);
  const hit = optsOf(payload).find((o) => String(o.key) === k);
  return hit ? { ok: true, sel: k } : { ok: false, sel: "" };
}

/** `↑` / `↓` 移动一格，环绕（判据 ④）。
 *
 *  `step` = `+1`（↓）/ `-1`（↑）。当前没选中时：↓ 从第一项起，↑ 从最后一项起
 *  —— 那正是"环绕"这个词在边界的含义。
 */
export function moveSel(payload, sel, step) {
  const opts = optsOf(payload);
  if ((payload && payload.expect === "text") || !opts.length) return { ok: false, sel: "" };
  const cur = opts.findIndex((o) => String(o.key) === String(sel || ""));
  const n = opts.length;
  const idx = cur === -1 ? (step > 0 ? 0 : n - 1) : ((cur + step) % n + n) % n;
  return { ok: true, sel: String(opts[idx].key) };
}

/** 键盘 → 该做什么。一处判准，`Approval.jsx` 只负责把事件喂进来。
 *
 *  返回 `{action}`，取值只有四种：`select` / `move` / `submit` / `dock` / `none`，
 *  外加 `sel`（`select` 之后的新选中）。
 *
 *  不在输入框里打字时才走这里（`typing`）—— 否则用户在发送框里打一个
 *  `2` 就会被当成"选中第 2 项"（判据：键盘是给这个面板用的，不是抢别人的）。
 *  停靠之后一律 `none`：面板已经收成一颗小药丸了，那时再让 `1–9` 生效
 *  同样是在抢发送框的键。
 */
export function keyAction(payload, sel, keyName, { docked = false, typing = false } = {}) {
  if (!payload || docked) return { action: "none", sel: "" };
  const k = String(keyName || "");
  //:只在我们自己的输入框里打字时，回车才算提交（判据 ⑥ 的收尾动作）。
  if (typing) return k === "Enter" ? { action: "submit", sel: "" } : { action: "none", sel: "" };
  if (/^[1-9]$/.test(k)) {
    //:按下的数字是屏幕上那个编号（连续），不是键位。
    //:两者今天确实不一样：`EGRESS_OPTIONS` 的键位是 `1,2,3,4,6`（跳号），
    //:而屏幕上是 `1,2,3,4,5`。这里必须先换一次再 `selectKey`。
    //:越界（比如屏幕上只有 5 项却按了 `6`），`keyAtDisplayNumber` 回 `""`
    //:，`selectKey` 拒绝，`action:"none"`（什么都不做，不是选错一项）。
    const r = selectKey(payload, keyAtDisplayNumber(payload, k));
    return { action: r.ok ? "select" : "none", sel: r.sel };
  }
  // 返回值只有两个键 `{action, sel}`（不把 `moveSel` 那个 `ok` 带出去）——
  //    多一个键就多一种"调用方要不要读它"的分叉，而 `sel` 空串本身就说明了没有可选项。
  if (k === "ArrowDown") return { action: "move", sel: moveSel(payload, sel, 1).sel };
  if (k === "ArrowUp") return { action: "move", sel: moveSel(payload, sel, -1).sel };
  if (k === "Enter") return { action: "submit", sel: "" };
  if (k === "Escape") return { action: "dock", sel: "" };
  return { action: "none", sel: "" };
}

/** 提交那一刻要发给服务端的东西。发不出去就返回 `ok:false`（别发必然 409 的）。
 *
 *  中间态发 `{text}`、常态发 `{choice}` —— 两者不同时发：
 *  服务端 `reply_decision` 先看 `choice`，两者都带时 `choice` 赢，
 *  而那会让用户敲的那句话静默消失。
 */
export function submitBody(payload, sel, text) {
  if (!payload) return { ok: false, body: null };
  if (payload.expect === "text") {
    const v = String(text == null ? "" : text).trim();
    return v ? { ok: true, body: { text: v } } : { ok: false, body: null };
  }
  if (!sel) return { ok: false, body: null };
  const hit = optsOf(payload).find((o) => String(o.key) === String(sel));
  return hit ? { ok: true, body: { choice: String(sel) } } : { ok: false, body: null };
}

/** 「确认」按钮能不能按（`disabled`）。判据与服务端那两关一致。 */
export function canSubmit(payload, sel, text) {
  return submitBody(payload, sel, text).ok;
}

/** 选项行挂哪些类。判据 ⑤：升级项带 `escalate`（CSS 给它呼吸框）· 判据 ②：选中带 `sel`。 */
export function optionClass(option, sel) {
  let cls = "approval-option";
  if (option && option.is_escalation) cls += " escalate";
  if (option && String(option.key) === String(sel || "")) cls += " sel";
  return cls;
}

/** 底部那行提示。逐字照 WebUI（两句话的差别就是"这一屏该怎么操作"）。 */
export function hintText(payload) {
  return (payload && payload.expect === "text")
    ? "输入后回车确认"
    : "↑↓ 或数字选择，回车确认";
}

/** 停靠后那颗药丸上的字。精确是这四个字（WebUI 有一条测试钉着它）。 */
export const DOCK_LABEL = "等待审批";
