// 名册那一列画哪些。
//
// ## 空会话
//
// 一条没落库的发送会留下一个壳（`/api/chat` 的 `new` 先铸会话、
// 那一轮随后被中断/失败，一条消息都没有）。它点开只有
// 「（没有用户消息） / 0 轮」，还会显示"正在处理"。
//
// 数据本身不删（库里的话是档，宁可留着），
// 但不画：0 轮且不是当前那一段，就是纯噪声。
//
// 当前那一条要画 —— `/clear` 之后、或删掉当前段之后，
//   "新的空对话"正是用户该看见的那一格。

/** 一条直播事件该不该画在当前这一屏。
 *
 * SSE 是按通道键广播的（私聊全塌成 `workspace:private`）——
 * 所以"正在看哪一段"与"这一轮跑在哪一段"必须自己比。
 * 不比的话，一轮在 A 里跑着、用户点开左栏另一段，那一屏凭空长出
 * 「正在处理」+ 没有框的步骤流，跑完还把回复贴到了错的对话里。
 *
 * `""` = 不知道（草稿态刚发出去 id 还没铸 / 别的通道起的轮），照旧画：
 * 宁可多画一次，也不能让正常的直播消失。
 */
export function liveEventBelongs(viewed, live) {
  const v = String(viewed || ""), l = String(live || "");
  return !v || !l || v === l;
}

/** 这一屏的身份 —— 直播事件归属判据的另一半。
 *
 * 只镜像 `viewing` 不够：群聊、草稿、活跃态那一格恒为 `""`，
 * 而群聊发送时记下的又是当前私聊的 id，两头对不上，
 * 「正在处理」和回复会落在别的地方。
 *
 * 身份的形状：
 *   · 群聊   → `group:<群 id>`（群不靠 `viewing`，它是另一个会话）
 *   · 看历史 → 那一段的 `storage_id`
 *   · 草稿   → `"draft"`（不是空串 —— 空串在 `liveEventBelongs` 里是"不知道"，
 *              而草稿是一个确定的、与任何在跑的轮都对不上的地方）
 *   · 活跃态 → 当前那一段的 `storage_id`（读不到，`""` = 不知道，照旧画）
 *
 * 它不能拿去替换 `visibleRef.thread`：那一格必须是真实 `storage_id`
 *   —— `task.end` 拿它跟 `d.storage_id` 比，用来置「未读」红点。
 */
export function viewKeyOf({ groupId = "", viewingId = "",
                           draft = false, currentId = "" } = {}) {
  if (groupId) return "group:" + String(groupId);
  if (viewingId) return String(viewingId);
  if (draft) return "draft";
  return String(currentId || "");
}

/** 过滤名册：0 轮且不是当前，不显示。唯一的判据（左栏与历史抽屉共用）。 */
export function visibleThreads(list) {
  const out = [];
  for (const t of (list || [])) {
    if (!t) continue;
    if (!t.is_current && !(Number(t.turn_count) || 0)) continue;
    out.push(t);
  }
  return out;
}
