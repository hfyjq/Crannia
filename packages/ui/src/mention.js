// `@` 候选层 —— 无主理人群聊里的成员点名补全（与命令补全同一套交互）。
//
// 判据与后端 `core/group_reply.py::parse_mentions` 对齐，不另发明一套：
//   · `@名字`（大小写不敏感）命中成员；
//   · `@all` / `@everyone` / `@所有人` / `@全员` / `@全体` = 全员。
//   所以候选表只提供能兑现的写法：成员名 + 一条 `@全员`。
//   后端不会因为候选表里有它就多认一种语法 —— 这里少一条，用户少一次空打；
//      这里多一条，就等于界面在承诺后端做不到的事（标签在说谎）。
//
// 本文件是纯函数（同 `group-form.js` / `user-text.js`）：
//    「什么时候弹 / 弹什么 / 采纳之后是什么文本」都住在这里，node 能直接断言；
//    组件只管渲染与按键。

//:全员那条候选的 token。与后端 `ALL_MENTION_TOKENS` 里的中文词逐字一致。
import { t } from "./i18n.js";

export const ALL_TOKEN = "@全员";

//: 候选表最多几条（与 `/` 候选层的 12 条同一个量级；4–6 人的群通常全都装得下）。
const MAX_ITEMS = 8;

/** 名册，候选条目。
 *  @param {Array<{name, role, avatar}>} members 群成员（`GET /api/groups` 的形状）
 *  @returns {Array<{token, name, avatar, desc, all}>}
 *
 *  顺序 = 名册顺序（主理人排在首位这条由数据保证；无主理人群里就是成员的排序）。
 *  去重按名字：重名成员在群里本来就分不清（后端点名也按名字），
 *     候选表更不该给两条看起来一样的。
 */
export function mentionEntries(members) {
  const out = [];
  const seen = new Set();
  for (const m of members || []) {
    const name = String((m && m.name) || "").trim();
    if (!name || seen.has(name)) continue;
    seen.add(name);
    out.push({
      token: "@" + name,
      name,
      avatar: String((m && m.avatar) || ""),
      desc: String((m && m.role) || "").trim() || t("（未写职责）"),
      all: false,
    });
  }
  // 全员那条永远在最后（它是兜底，不是某个人的职责）。成员真叫「全员」时不再追加
  //   —— 两条 `@全员` 长得一样，采纳哪条都一样，但看着像重复。
  if (!seen.has("全员")) {
    out.push({ token: ALL_TOKEN, name: t("全员"), avatar: "",
              desc: t("所有成员都回复"), all: true });
  }
  return out;
}

/** 光标处是不是正在打一个 `@token`。
 *  @returns {{start:number, end:number, query:string}|null}
 *    `start` = `@` 的下标；`end` = 整个 token 的末尾（不是光标处）；
 *    `query` = `@` 与光标之间已经打出来的字（采纳时按它过滤）。
 *
 *  `end` 取的是整个 token 的末尾，不是光标位置：
 *     用户在 `@wor|ker1` 中间回头补全时，采纳要换掉整段 `@worker1`，
 *     否则会拼出 `@worker1ker1`。
 *  判据「`@` 前面不能是单词字符」挡的是邮箱/路径里的 `@`（`a@b.com`）——
 *     中文不算单词字符，所以「请@小王」照样弹（中文里没人打空格）。
 *  光标往前遇到空白，这一段已经打完了，不再弹（候选只在正在打的 token 上出现）。
 */
export function mentionQuery(text, caret) {
  const src = String(text || "");
  const pos = (typeof caret === "number" && caret >= 0 && caret <= src.length)
    ? caret
    : src.length;
  let at = -1;
  for (let i = pos - 1; i >= 0; i--) {
    const ch = src[i];
    if (ch === "@") { at = i; break; }
    if (/\s/.test(ch)) return null;
  }
  if (at < 0) return null;
  if (at > 0 && /\w/.test(src[at - 1])) return null;
  let end = pos;
  while (end < src.length && !/[\s@]/.test(src[end])) end++;
  return { start: at, end, query: src.slice(at + 1, pos) };
}

/** 按已打的字过滤候选。
 *  前缀优先，但包含也算 —— 成员名往往是 `worker1`/`writer` 这种，
 *    用户记得住尾缀；空 query（刚打出 `@`）给全表。
 */
export function mentionItems(entries, query) {
  const list = entries || [];
  const q = String(query || "").trim().toLowerCase();
  const hit = q
    ? list.filter((e) => String(e.name || "").toLowerCase().includes(q))
    : list;
  return hit.slice(0, MAX_ITEMS);
}

/** 采纳一条候选：把 `@token` 换进去，光标落在后面那个空格之后。
 *  @returns {{text:string, caret:number}}
 *  保留 token 前后的原文（句首/句中都能用），失败时原样返回（不吞用户输入）。
 */
export function applyMention(text, q, entry) {
  const src = String(text || "");
  if (!q || !entry) return { text: src, caret: src.length };
  const token = String(entry.token || ("@" + entry.name));
  const head = src.slice(0, q.start);
  const tail = src.slice(q.end);
  // 只在后面没有空白时补一个空格：用户已经在 token 后打了空格，
  //   再补一个就变成两个（别让一次点击多改一个字符）。
  const sep = tail.startsWith(" ") ? "" : " ";
  return { text: head + token + sep + tail, caret: (head + token + sep).length };
}
