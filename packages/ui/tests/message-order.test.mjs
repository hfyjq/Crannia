// `packages/ui/src/message-order.js` 的读者 —— 零依赖、node 直接跑。
//
//   node packages/ui/tests/message-order.test.mjs
//
// 钉住两件事：
//   ① 回复的落点：连发两条时，第一条的回复必须落在 user1 之后、user2 之前
//      （否则屏上是 user1, user2, assistant1）；
//   ② 跨通道去重：POST 与 SSE 到的是同一句话；但"连着两轮都回了同一句话"
//      不许被吞（按文本 + 10 秒窗口判）。

import assert from "node:assert/strict";
import { activityAnchorIndex, classifyReply, replyAnchorIndex }
  from "../src/message-order.js";

let pass = 0;
const failed = [];
function check(label, ok, note = "") {
  if (ok) { pass++; return; }
  failed.push(`${label}${note ? " -- " + note : ""}`);
}

const U = (id, text = "") => ({ id, role: "user", content: text });
const A = (id, text = "", ts = 0) => ({ id, role: "assistant", content: text, ts });
const ACT = (id) => ({ id, role: "activity" });

// ── ① 落点 ───────────────────────────────────────────────────
{
  //:这两个夹具的 `u2` 必须标 `queued`：现在"跑着时再发一条"走的是
  //:本地排队（那条还没发出去），它不是一轮。标了之后语义就是真的那种形状。
  check("两条都没回 -> 第一条的家 = 排队那条之前",
        replyAnchorIndex([U("u1"), { ...U("u2"), queued: true }]) === 1);
  check("第一条回过了 -> 家挪到末尾（排队那条之前）",
        replyAnchorIndex([U("u1"), A("a1"), { ...U("u2"), queued: true }]) === 2);
  check("★ 步骤流不算回过",
        replyAnchorIndex([U("u1"), ACT("x"), { ...U("u2"), queued: true }]) === 2);
  check("只有一条 user、还没回 -> 插在该段末尾",
        replyAnchorIndex([U("u1"), ACT("x")]) === 2);
  check("没有 user（IM 起的轮）-> 末尾",
        replyAnchorIndex([A("a1")]) === 1);
  check("空列表 -> 0", replyAnchorIndex([]) === 0);
  check("null 不炸", replyAnchorIndex(null) === 0);
  check("占位（pending）不算回过",
        replyAnchorIndex([U("u1"), { id: "p", role: "assistant", pending: true }]) === 2);
  //:现场：跑着的时候补的那条还没发出去（`queued`）
  //:，它不是一段。把它当段起点，上一轮的回复会被插到它前面
  //:   （看到的是「[排队中] [回复] [刚发的那条]」）。
  //:队列在末尾（跑着时补的那条）：回复是上一条提问的，插在它前面。
  check("★★ 排队的 user 不是段起点（回复插在它前面，不是插到它后面）",
        replyAnchorIndex([U("u1", "s1"), { ...U("q", "s2"), queued: true }]) === 1);
  check("★ 已有回复的那段照旧跳过（排队的不干扰）",
        replyAnchorIndex([U("u1", "s1"), A("a1", "x"), { ...U("q", "s2"), queued: true },
                          U("u2", "s3")]) === 4);
  check("★★ 两条都发、第一条已回 -> 第二条的家在末尾",
        replyAnchorIndex([U("u1"), A("a1"), U("u2"), ACT("y")]) === 4);
}

// ── ② 去重 ───────────────────────────────────────────────────
{
  const now = 1000;
  const msgs = [A("a1", "好的", now)];
  check("同文本 + 同时间窗 -> same",
        classifyReply(msgs, "好的", now + 1).kind === "same");
  check("★ 同文本但隔了 60 秒 -> 不算重复（真会连回同一句）",
        classifyReply(msgs, "好的", now + 60).kind === "none");
  check("两条都没有 ts -> 退回纯文本比较",
        classifyReply([A("a1", "好的")], "好的", 0).kind === "same");
  check("空文本 -> none（调用方会自己早退）",
        classifyReply(msgs, "  ", now).kind === "none");
  check("不碰 user 消息", classifyReply([U("u1", "好的")], "好的", now).kind === "none");

  const M = "…(truncated)";
  // 现实中"截断版"是被砍短的那一条：前缀相同、长度更短。
  const shortTxt = "x".repeat(20) + M;
  const fullTxt = "x".repeat(40);   // 必须比"截断版"更长（那才是现实里的形状）
  check("★ 已有截断版、来的是全文 -> upgrade",
        classifyReply([A("a1", shortTxt, now)], fullTxt, now, M).kind === "upgrade");
  check("已有全文、来的是截断版 -> drop",
        classifyReply([A("a1", fullTxt, now)], shortTxt, now, M).kind === "drop");
  check("不带 truncMark 时不做截断对判定",
        classifyReply([A("a1", shortTxt, now)], fullTxt, now, "").kind === "none");
  check("★ 时间窗外的截断对也不升格",
        classifyReply([A("a1", shortTxt, now)], fullTxt, now + 60, M).kind === "none");
}

// ── ③ 步骤流（活动块）的落点 ─────────────────────────────────
//
// 现场反馈：审批 / 提问面板回答之后，新的一轮没有用户消息
// （同意 = 框架重放），旧判据按"最后一条 user"插，块就落在上一轮
// 的步骤流与回复之间。判据换成"末尾那串 assistant 里谁还没落库"。
{
  const ACTV = (id, live = true) => ({ id, role: "activity", live });
  const persisted = (m, rowId) => ({ ...m, rowId });

  check("普通一轮（用户消息在末尾）-> 块落在它之后",
        activityAnchorIndex([U("u1"), ACTV("x")]) === 2);
  check("回复先到、还没落库 -> 块插在它前面",
        activityAnchorIndex([U("u1"), A("a1")]) === 1);
  check("★★ 审批/提问回答那一轮：上一轮**已落库**的回复不许越过",
        activityAnchorIndex([U("u1"), ACTV("x", false), persisted(A("a1"), 7)]) === 3);
  check("★★ 两轮都落过库 -> 块仍在末尾（别把上一轮整段卷进来）",
        activityAnchorIndex([persisted(U("u1"), 1), ACTV("x", false),
                             persisted(A("a1"), 2)]) === 3);
  check("先到的那条在落库那条**之后** -> 插在它前面",
        activityAnchorIndex([U("u1"), ACTV("x", false), persisted(A("a1"), 7),
                             A("a2")]) === 3);
  check("空列表 -> 0", activityAnchorIndex([]) === 0);
  check("null 不炸", activityAnchorIndex(null) === 0);
}

console.log(`${pass} passed` + (failed.length ? `, ${failed.length} failed` : ""));
if (failed.length) {
  for (const f of failed) console.log("  FAIL " + f);
  process.exit(1);
}
