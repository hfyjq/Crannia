// 回复的落点与跨通道去重 —— 纯函数，node 逐条断言。
//
// 背景：一条回复会经两条通道到前端（`/api/chat` 的 POST 响应与 `task.end` 事件），
// 谁先到不定，必须去重；而用户连发两条时，第二条 user 消息会先上屏，
// 第一条的回复后到，直接 append 会把顺序弄成 user1, user2, assistant1。

/** 这条回复该插在哪个下标。
 *
 *  判据是"段"：一条 `user` 到下一条 user 之间有没有 `assistant`。
 *  服务器按会话串行，回复的到达顺序与段顺序一致，第一条没回过的段
 *  就是这条回复的家（插在下一段 user 之前；走到末尾就 append）。
 *
 *  `activity`（步骤流）不算"回过"；`pending` 的占位也不算。
 *
 *  `queued` 的 user 消息不算"一段"：
 *  跑着的时候补的那条还没发出去（连 POST 都没发），它不是一轮。
 *  把它当段起点会出事：上一条还没回，它就成了"第一条没回过的段"，
 *  于是上一轮的回复被插到它前面，界面上看到的是
 *  「[排队中那条] [回复] [刚发的那条]」—— 回复夹在两条用户消息中间，
 *  而它答的那条用户消息反而被挤到自己答案的下面。
 *，锚点只看真的发出去了的用户消息；排队的那些透明。
 */
export function replyAnchorIndex(msgs) {
  const list = Array.isArray(msgs) ? msgs : [];
  //:从最后一条"真的发出去了"的用户消息起算，不是从第一条。
  //
  //   为什么不是"第一条还没回过的段"（旧判据）：有一条排队中的消息横在
  //   中间时，旧判据会把更早那条当成"没回过"的段，于是这次回复被插到
  //   排队消息与它自己那条提问之间 —— 界面上看到的是
  //   「[排队中] [回复] [刚发的那条]」，而回复答的那条反而被挤到自己答案下面。
  //   所以回复只可能属于最近一次真的发出去的那一轮（服务端按会话串行，
  //     所以同一时刻最多一条"已发出、还没回"）。
  let last = -1;
  for (let i = list.length - 1; i >= 0; i--) {
    const m = list[i];
    if (m && m.role === "user" && !m.queued) { last = i; break; }
  }
  if (last < 0) return list.length;        // 没有用户消息（IM 起的轮 / 审批回答）
  //:走到下一条用户消息为止（排队的那条也算边界 —— 回复插在它前面）。
  let j = last + 1;
  for (; j < list.length; j++) {
    if (list[j] && list[j].role === "user") break;
  }
  return j;
}

/** 新的步骤流块该插在哪个下标。
 *
 *  判据是「这一轮从哪儿开始」，不是「最后一条 `user` 消息在哪儿」。
 *
 *  有些轮次根本没有新的用户消息 —— 审批 / 提问面板的回答（
 *  同意 = 框架重放、`user_message=None`）、后台任务回报自动起的那一轮……
 *  那时「最后一条 `user`」属于上一轮，按它插，块落在上一轮的
 *  步骤流与回复之间（审批板之后，步骤流出现在
 *  「前一个步骤流那里」）。
 *
 *，只看末尾那串 `assistant` 里哪些还没落库：
 *   · `!rowId`（`attachRows` 还没贴过行号），它是这一轮刚到的回复
 *     （SSE 与 POST 两条通道，回复可能先到），块插在它前面；
 *   · `rowId` 有值，它属于已经结束的上一轮，绝不越过它。
 *
 *  兜底：无论怎么退，都不许越过最后一条 `user` 消息（块永远排在
 *     "用户的问"之后）—— 保留这一条，是因为上面的判据依赖 `rowId` 有没有贴上。
 */
export function activityAnchorIndex(msgs) {
  const list = Array.isArray(msgs) ? msgs : [];
  let at = list.length;
  while (at > 0) {
    const m = list[at - 1];
    if (!m || m.role !== "assistant" || m.rowId) break;
    at -= 1;
  }
  for (let k = list.length - 1; k >= 0; k -= 1) {
    if (list[k] && list[k].role === "user") {
      if (at <= k) at = k + 1;
      break;
    }
  }
  return at;
}

/** 同一个窗口内算"同一句话"（秒）。只按文本会吞掉"连着两轮都回了同一句话"。 */
export const DUP_WINDOW_SEC = 10;

/** 这条回复是不是已经有了。
 *
 *  返回 `{index, kind}`：
 *   · `same`    —— 已经有完全相同的一条，丢掉这次
 *   · `upgrade` —— 已有一条截断版，这次是全文，就地换成全文
 *   · `drop`    —— 已有全文，这次是截断版，丢掉这次
 *   · `none`    —— 没有，按落点插入
 *
 *  *truncMark* 是截断标记（空串 = 不做截断对判定）。
 *  时间窗只对两边都有 ts 的情况生效；缺 ts 时退回纯文本比较。
 */
export function classifyReply(msgs, text, ts, truncMark = "") {
  const list = Array.isArray(msgs) ? msgs : [];
  const txt = String(text || "").trim();
  if (!txt) return { index: -1, kind: "none" };
  const mine = Number(ts) || 0;
  let sawUser = false;
  for (let i = list.length - 1; i >= 0; i--) {
    const m = list[i];
    if (!m) continue;
    //:排队的（还没发出去的）user 消息不是一段边界 —— 与
    //:   `replyAnchorIndex` 同一条判据（同上那段情形）。
    if (m.role === "user" && !m.queued) {
      sawUser = true;
      // 没有时间戳（测试夹具 / 老数据），退回同一段判据：
      //   中间隔着一条 user 就是两条不同的回复（的本意）。
      if (!mine) break;
      continue;
    }
    if (m.role !== "assistant" || m.pending) continue;
    const c = String(m.content || "").trim();
    const t = Number(m.ts) || 0;
    if (t && mine) {
      if (Math.abs(t - mine) >= DUP_WINDOW_SEC) continue;
    } else if (sawUser) {
      continue;
    }
    if (c === txt) return { index: i, kind: "same" };
    if (truncMark && c.endsWith(truncMark) && txt.length > c.length
        && txt.startsWith(c.slice(0, -truncMark.length))) {
      return { index: i, kind: "upgrade" };
    }
    if (truncMark && txt.endsWith(truncMark) && c.length > txt.length
        && c.startsWith(txt.slice(0, -truncMark.length))) {
      return { index: i, kind: "drop" };
    }
  }
  return { index: -1, kind: "none" };
}
