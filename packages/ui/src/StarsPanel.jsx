import React, { useCallback, useEffect, useState } from "react";
import { Loader2, Star, Trash2 } from "lucide-react";

// 收藏夹。
//
// ## 它是什么
//
// 入口 = 右缘那个 → 点进去是条目表，
// 每条 = 上半用户的问题 / 下半回答，各截两行 + 省略号，
// 点击条目跳转到对应对话位置。聊天区里也能点 。
//
// ## 三条判据
//
// 1. 一条星标 = 一轮（不是一条消息）—— 锚点是那一轮的提问那条
//    （`stars.message_id` = 那条 `role='user'` 的行）。
// 2. 引用式、不存副本 —— 「来源被删，条目消失」是一条
//    结构要求。后端那两个 `JOIN` 让它天然成立，这里
//    不做任何过滤（列表给什么就画什么，"解不出来"不是这一层的事）。
// 3. 它一个字节都不写（除了用户自己点"取消收藏"）—— 不落库、不发事件、
//    不碰 `messages`。模型永远不知道用户翻过收藏夹。

/** 一行的省略：用 CSS 截行，不用 `slice` ——
 *  `slice(0, 80)` 会把一个中文字切成半个、而且宽度不定（同一段文字
 *  在窄屏能放三行、宽屏一行）。CSS 的 `-webkit-line-clamp` 才是"两行 + 省略号"。
 *  这里给的是原文，截断发生在渲染层（`.stars-line`）。 */
import { t } from "./i18n.js";
function line(text) {
  return String(text || "").trim();
}

export function StarsPanel({ client, onOpen }) {
  const [items, setItems] = useState(null);   // null = 还没读到
  const [total, setTotal] = useState(0);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setErr("");
    try {
      const d = await client.call("/api/stars?limit=200");
      setItems(d.stars || []);
      // `total` 是真数，列表可能被 `limit` 截断 —— 两件事分开报
      //   （报数，不报字符）。
      setTotal(typeof d.total === "number" ? d.total : (d.stars || []).length);
    } catch (e) {
      setErr(e.message || String(e));
      setItems([]);
    } finally {
      setBusy(false);
    }
  }, [client]);

  useEffect(() => { load(); }, [load]);

  async function remove(messageId, e) {
    e.stopPropagation();          // 别把t("取消收藏")点成"跳过去"
    try {
      await client.call("/api/stars/delete", {
        method: "POST", body: JSON.stringify({ message_id: messageId }),
      });
      setItems((old) => (old || []).filter((s) => s.message_id !== messageId));
      setTotal((n) => Math.max(0, n - 1));
    } catch (e2) {
      setErr(e2.message || String(e2));
    }
  }

  return (
    <div className="stars">
      {err ? <p className="stars-err">{err}</p> : null}

      {items === null ? (
        <p className="stars-note"><Loader2 size={13} className="spin" /> {t("读…")}</p>
      ) : !items.length ? (
        <div className="stars-empty">
          <Star size={22} />
          <p>{t("还没有收藏")}</p>
          {/* 说清怎么收藏 —— 空状态是唯一能说这句话的地方 */}
          <span>{t("在对话中点消息下面的 ☆来收藏消息")}</span>
        </div>
      ) : (
        <>
          {total > items.length ? (
            <p className="stars-note">{t("共 {total} 条，这里只列出最近 {items.length} 条。", { "total": total, "items.length": items.length })}</p>
          ) : null}
          {items.map((s) => (
            <button className="stars-item" key={s.message_id}
                    title={t("跳到这条对话")}
                    onClick={() => onOpen && onOpen(s.storage_id, s.message_id)}>
              <div className="stars-line q">{line(s.question) || t("（这条提问已不在库里）")}</div>
              {/* `answer === null` 与 `answer === ""` 不是一回事：
                  前者 = 那一轮没有回复（或被压缩掉了），后者 = 回复是空的。
                  后端刻意分开（读不到 ≠ 0），这里也要分开说 ——
                  画一片空白会让人以为"那条回复本来就没内容"。 */}
              {s.answer === null ? (
                <div className="stars-line a muted">{t("（这一轮没有找到回复）")}</div>
              ) : (
                <div className="stars-line a">{line(s.answer)}</div>
              )}
              <div className="stars-foot">
                <span>{s.ts ? String(s.ts).replace("T", " ").slice(5, 16) : ""}</span>
                <span className="stars-drop" onClick={(e) => remove(s.message_id, e)}
                      title={t("取消收藏")}>
                  <Trash2 size={12} />
                </span>
              </div>
            </button>
          ))}
          {busy ? <p className="stars-note"><Loader2 size={13} className="spin" /> {t("刷新…")}</p> : null}
        </>
      )}
    </div>
  );
}
