// `@` 候选层（`G29` 尾巴，2026-10-17）—— 无主理人群聊的成员点名补全。
// 运行：node packages/ui/tests/mention.test.mjs
//
// 这里钉的是判据，不是组件：什么时候弹 / 弹什么 / 采纳之后是什么文本。
//   组件那一半（键位、互斥、只有群聊才传名册）在 `tests/test_desktop_groups.py`。
import assert from "node:assert";
import {
  ALL_TOKEN, applyMention, mentionEntries, mentionItems, mentionQuery,
} from "../src/mention.js";

let pass = 0;
function eq(name, got, want) {
  assert.deepStrictEqual(got, want, name + ": got " + JSON.stringify(got));
  pass++;
}
function ok(name, cond, detail) {
  assert.ok(cond, name + (detail ? ": " + detail : ""));
  pass++;
}

const MEMBERS = [
  { name: "worker1", role: "整理资料", avatar: "3" },
  { name: "worker2", role: "" },
  { name: "Alice" },
];

// ── 名册，候选 ─────────────────────────────────────────────────────
{
  const es = mentionEntries(MEMBERS);
  eq("members keep order + tokens", es.map((e) => e.token),
    ["@worker1", "@worker2", "@Alice", ALL_TOKEN]);
  eq("role is the description", es[0].desc, "整理资料");
  eq("empty role is said, not hidden", es[1].desc, "（未写职责）");
  eq("avatar is carried", es[0].avatar, "3");
  ok("everyone is last and marked", es[es.length - 1].all === true);
  eq("everyone token matches the backend word", ALL_TOKEN, "@全员");
  eq("no members still offers everyone", mentionEntries([]).map((e) => e.token), [ALL_TOKEN]);
  eq("duplicate names collapse",
    mentionEntries([{ name: "a" }, { name: "a" }]).map((e) => e.token), ["@a", ALL_TOKEN]);
  eq("a member literally named 全员 does not double it",
    mentionEntries([{ name: "全员" }]).map((e) => e.token), ["@全员"]);
}

// ── 光标处是不是在打 @token ─────────────────────────────────────────
{
  eq("at the start", mentionQuery("@wo", 3), { start: 0, end: 3, query: "wo" });
  eq("mid sentence after a space", mentionQuery("请 @wo 看下", 5),
    { start: 2, end: 5, query: "wo" });
  // 中文不算单词字符 —— 中文里没人会在 @ 前面打空格。
  eq("right after Chinese", mentionQuery("请@小王", 4),
    { start: 1, end: 4, query: "小王" });
  eq("just typed @ shows the whole roster", mentionQuery("@", 1),
    { start: 0, end: 1, query: "" });
  // end 是整个 token 的末尾：在 token 中间补全时要换掉整段，否则会拼出
  //   `@worker1ker1`。
  eq("caret in the middle consumes the whole token", mentionQuery("@worker1 看", 4),
    { start: 0, end: 8, query: "wor" });
  eq("email is not a mention", mentionQuery("a@b.com", 7), null);
  eq("after the token is finished it closes", mentionQuery("@worker1 看", 9), null);
  eq("no at sign", mentionQuery("你好", 2), null);
  eq("caret out of range falls back to the end", mentionQuery("@wo", 99),
    { start: 0, end: 3, query: "wo" });
}

// ── 过滤 ────────────────────────────────────────────────────────────
{
  const es = mentionEntries(MEMBERS);
  eq("empty query = everyone", mentionItems(es, "").map((e) => e.name),
    ["worker1", "worker2", "Alice", "全员"]);
  eq("case-insensitive", mentionItems(es, "ali").map((e) => e.name), ["Alice"]);
  eq("contains, not only prefix", mentionItems(es, "ker2").map((e) => e.name), ["worker2"]);
  eq("no match = empty list (the layer stays closed)", mentionItems(es, "zzz"), []);
  eq("everyone can be found by name", mentionItems(es, "全员").map((e) => e.token), [ALL_TOKEN]);
}

// ── 采纳 ────────────────────────────────────────────────────────────
{
  const q = mentionQuery("请@wo", 4);
  eq("replaces the token and leaves the caret after a space",
    applyMention("请@wo", q, { token: "@worker1", name: "worker1" }),
    { text: "请@worker1 ", caret: 10 });
  // 光标在 token 中间：整段替换，尾巴一个字都不丢。
  eq("mid-token replacement keeps the suffix",
    applyMention("@worker1 看下", mentionQuery("@worker1 看下", 4),
      { token: "@worker1", name: "worker1" }),
    { text: "@worker1 看下", caret: 8 });
  // 后面已经有空格，不再补一个（一次点击不许改两个字符）。
  eq("an existing space is not doubled",
    applyMention("@wo 看下", mentionQuery("@wo 看下", 3),
      { token: "@worker1", name: "worker1" }),
    { text: "@worker1 看下", caret: 8 });
  eq("everyone token works the same",
    applyMention("@全", mentionQuery("@全", 2), { token: ALL_TOKEN, name: "全员" }),
    { text: "@全员 ", caret: 4 });
  eq("no query is a no-op", applyMention("abc", null, { token: "@x" }),
    { text: "abc", caret: 3 });
}

console.log("mention: " + pass + "/" + pass + " passed");
