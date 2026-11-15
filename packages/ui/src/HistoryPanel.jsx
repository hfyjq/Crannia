import React, { useCallback, useEffect, useState } from "react";
import { ChevronDown, Loader2, Search, Star } from "lucide-react";
//:用户消息里的附件前言只给模型看 —— 历史面板与聊天气泡共用。
import { userTextForDisplay } from "./user-text.js";
import { visibleThreads } from "./thread-list.js";

// ⏱ 历史面板。
//
// ## 它回答什么
//
// 重启后对话记录"看起来消失了" —— 数据一直在库里（`short_term.db`），缺的是读口。
// 所以这一格做的不是"存起来"，是"读出来给人看"。
//
// ## 三条判据
//
// 1. 端点不是工具 —— 它读的是 `/api/threads` `/api/messages` `/api/search`，
//    三条只读端点。不经过 `history_search` 那个工具：
//    那个是给模型用的，而这个面板是给人看的。
// 2. 全量读 `messages`，不是 `load_session` —— `load_session` 按预算裁剪、
//    跳过 tool 消息，那是"装进模型上下文"的口径；界面要看的是存着的那些行。
// 3. 模型永远不知道用户翻过历史 —— 这个面板一个字节都不写：
//    不落库、不发事件、不碰 `messages`。它只做 GET。
//    所以这条不是"注意别写"，是结构性的：这里根本没有 POST 代码。
//
// 它不受 `memory_continuity` 管辖 —— 那个设置管的是装载预算，
//    不是存储、更不是这个面板能看多少。
//
// ## 层级
//
//    会话名册（`/api/threads`）  →  选一条  →  消息（`/api/messages`，尾部一页 + 往上翻）
//                                              ↳ 关键词搜（`/api/search`）

/** 库里 `created_at` 的形状是 `YYYY-MM-DDTHH:MM:SS`（本地时间、无时区）。
 *，`new Date(s)` 会按本地解析 ✓（ISO 不带 Z 就是本地）。
 *  解析不出来就原样显示，不要给一个假的 `Invalid Date`（读不到 ≠ 0）。 */
import { t } from "./i18n.js";
function when(s) {
  if (!s) return "";
  const d = new Date(s);
  if (isNaN(d.getTime())) return String(s);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 一条消息怎么称呼它。按 `kind` 认摘要，不按 role（摘要的 role 是 `user`）。 */
function who(m) {
  if (m.kind === "history_summary") return t("摘要");
  return { user: t("你"), assistant: t("助手"), tool: t("工具") }[m.role] || m.role;
}

/** `jump` = `{storage_id, message_id}`（收藏夹点条目跳过来的落点）。
 *  它是一次性的：滚到了（或翻到底了）就 `onJumped()` 把父组件的状态清掉
 *  —— 不清的话，用户自己再翻页时那个 effect 会又把他拽回去。 */
export function HistoryPanel({ client, jump, onJumped }) {
  const [threads, setThreads] = useState([]);
  const [sid, setSid] = useState("");
  const [rows, setRows] = useState([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [q, setQ] = useState("");
  const [hits, setHits] = useState(null);      // null = 没在搜；[] = 搜了没命中
  const [switching, setSwitching] = useState(false);
  //:哪些行已经被收藏（行号集合）—— 决定那个图标是实心还是空心。
  const [starred, setStarred] = useState(() => new Set());

  const refreshStars = useCallback(async () => {
    try {
      const d = await client.call("/api/stars?limit=500");
      setStarred(new Set((d.stars || []).map((s) => s.message_id)));
    } catch {
      // 拿不到就不点亮，不弹错 —— 收藏夹挂了不该让历史面板也打不开
      //   （同 `/api/approval` 那条判据：轮询式的读口偶发失败不该在界面上弹红字）。
    }
  }, [client]);

  useEffect(() => { refreshStars(); }, [refreshStars]);

  /** 收藏 / 取消。锚点就是这条 user 行自己的行号（`stars.message_id`）。
   *  这里只给 `user` 行按钮 —— 收藏的锚点定义就是"那一轮的提问"，
   *    给助手行也画一个会让人以为"收藏的是这条回答"（后端会 400 拒掉）。 */
  async function toggleStar(m) {
    const on = starred.has(m.id);
    try {
      await client.call(on ? "/api/stars/delete" : "/api/stars", {
        method: "POST", body: JSON.stringify({ message_id: m.id }),
      });
      await refreshStars();
    } catch (e) {
      setErr(e.message || String(e));
    }
  }

  // ── 名册：开面板就拉一次 ──
  useEffect(() => {
    let dead = false;
    (async () => {
      try {
        const d = await client.call("/api/threads");
        if (dead) return;
        const list = d.threads || [];
        setThreads(list);
        // 默认选当前那条（`is_current`）—— 面板默认展示当前会话的历史。
        //   没有当前的就退到最新一条；一条都没有就保持空。
        const cur = list.find((t) => t.is_current) || list[0];
        if (cur) setSid(cur.storage_id);
      } catch (e) {
        if (!dead) setErr(e.message || String(e));
      }
    })();
    return () => { dead = true; };
  }, [client]);

  // ── 选中哪条，拉它的尾页 ──
  useEffect(() => {
    if (!sid) return undefined;
    let dead = false;
    setLoading(true);
    setErr("");
    setHits(null);
    (async () => {
      try {
        const d = await client.call(
          `/api/messages?storage_id=${encodeURIComponent(sid)}&limit=50`);
        if (dead) return;
        setRows(d.messages || []);
        setHasMore(!!d.has_more);
      } catch (e) {
        if (!dead) { setRows([]); setErr(e.message || String(e)); }
      } finally {
        if (!dead) setLoading(false);
      }
    })();
    return () => { dead = true; };
  }, [client, sid]);

  /** 往上翻一页。游标用当前最上面那条的 id（后端就是 `before=<id>`）。 */
  const older = useCallback(async () => {
    if (!rows.length || loading) return;
    setLoading(true);
    try {
      const d = await client.call(
        `/api/messages?storage_id=${encodeURIComponent(sid)}&before=${rows[0].id}&limit=50`);
      setRows((old) => (d.messages || []).concat(old));   // 新的一页在上面
      setHasMore(!!d.has_more);
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }, [client, sid, rows, loading]);

  // ── 跳转落点（收藏夹点条目跳过来的）──────────────────────────────
  //
  // 三段，缺一段就跳不到位：
  //   ① 切到那一段会话 · ② 那一行不在当前页就往上翻 · ③ 翻到了滚过去。
  // 必须有一次性的收口（`onJumped`）：不清掉的话，用户自己再翻页时
  //    这个 effect 会又把他拽回去 —— 那正是"界面不听使唤"的典型形状。
  useEffect(() => {
    if (!jump || !jump.storage_id) return;
    setSid(jump.storage_id);
    // 清掉搜索态：搜索命中那批渲染的是 `hits`，目标行根本不在里面，
    //    effect 会一直找不到，永远翻页。切会话时本来就该退出搜索。
    setHits(null);
  }, [jump]);

  useEffect(() => {
    if (!jump || !jump.message_id) return;
    if (loading) return;
    const el = document.getElementById("hist-row-" + jump.message_id);
    if (el) {
      el.scrollIntoView({ block: "center" });
      if (onJumped) onJumped();
      return;
    }
    // 还没翻到：还有更早就继续翻，翻到底了就认了（也收口，别卡住）
    if (hasMore) older();
    else if (onJumped) onJumped();
  }, [jump, rows, hasMore, loading, older, onJumped]);

  async function runSearch() {
    const term = q.trim();
    if (!term || !sid) return;
    setLoading(true);
    setErr("");
    try {
      const d = await client.call(
        `/api/search?storage_id=${encodeURIComponent(sid)}&q=${encodeURIComponent(term)}`);
      // 回执里 `ok:false` 带的是人话（端点按 400 回），client 会抛，
      //   走 catch。走到这儿就是真搜过了：`hits` 为空 = 搜完了没命中。
      setHits(d.hits || []);
    } catch (e) {
      setHits(null);
      setErr(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  const curThread = threads.find((t) => t.storage_id === sid);

  return (
    <div className="hist">
      {/* 会话切换（收起时只显示当前那条的一句话标题） */}
      <button className="hist-thread" onClick={() => setSwitching((v) => !v)}>
        <span className="hist-thread-x">
          <span className="hist-thread-t">
            {curThread ? (curThread.title || t("（这条会话没有用户消息）")) : t("没有会话")}
          </span>
          <span className="hist-thread-m">
            {curThread ? t("{when(curThread.started_at)} · {curThread.turn_count} 轮 · {curThread.message_count} 条", { "when(curThread.started_at)": when(curThread.started_at), "curThread.turn_count": curThread.turn_count, "curThread.message_count": curThread.message_count }) : ""}
          </span>
        </span>
        <ChevronDown size={14} className={switching ? "flip" : ""} />
      </button>

      {switching ? (
        <div className="hist-threads">
          {visibleThreads(threads).map((th) => (
            <button key={th.storage_id}
                    className={"hist-trow" + (th.storage_id === sid ? " on" : "")}
                    onClick={() => { setSid(th.storage_id); setSwitching(false); }}>
              <b>{th.title || t("（没有用户消息）")}</b>
              <small>{when(th.started_at)} · {t("{n} 轮", { n: th.turn_count })}{th.is_current ? " " + t("· 当前") : ""}</small>
            </button>
          ))}
          {!threads.length ? <p className="drawer-stub">{t("这个通道还没有会话。")}</p> : null}
        </div>
      ) : null}

      {/* 搜索 —— 回车才搜（不边打边搜：那是每敲一个字一次全表扫） */}
      <div className="hist-search">
        <Search size={14} />
        <input value={q} placeholder={t("在这里搜索关键词")}
               onChange={(e) => setQ(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") runSearch(); }} />
        {hits !== null ? (
          <button className="hist-clear" onClick={() => { setHits(null); setQ(""); }}>×</button>
        ) : null}
      </div>

      {err ? <p className="hist-err">{err}</p> : null}

      <div className="hist-body">
        {hits !== null ? (
          hits.length ? (
            <>
              <p className="hist-note">{t("在这条会话里命中 {hits.length} 条", { "hits.length": hits.length })}</p>
              {hits.map((h) => (
                <div className="hist-hit" key={h.index}>
                  <b>{h.label}</b>
                  <span>{h.snippet}</span>
                </div>
              ))}
            </>
          ) : <p className="hist-note">{t("搜完了，这条会话里没有匹配。")}</p>
        ) : (
          <>
            {loading ? <p className="hist-note"><Loader2 size={13} className="spin" /> {t("读…")}</p> : null}
            {hasMore ? (
              <button className="hist-more" onClick={older} disabled={loading}>{t("∧ 加载更早")}</button>
            ) : null}
            {rows.map((m) => (
              // `id` 是跳转落点（收藏夹点条目跳过来时 `scrollIntoView` 靠它）。
              //    用行号而不是下标 —— 下标会随"加载更早"整段平移。
              <div className={"hist-row " + (m.role || "")} key={m.id}
                   id={"hist-row-" + m.id}>
                <span className="hist-who">{who(m)}</span>
                <span className="hist-ts">{when(m.ts)}</span>
                {m.role === "user" ? (
                  <button
                    className={"hist-star" + (starred.has(m.id) ? " on" : "")}
                    title={starred.has(m.id) ? t("取消收藏") : t("收藏这一轮")}
                    onClick={() => toggleStar(m)}>
                    <Star size={12}
                          fill={starred.has(m.id) ? "currentColor" : "none"} />
                  </button>
                ) : null}
                <p className="hist-text"
                   title={String(m.role === "user" ? userTextForDisplay(m.content) : (m.content || ""))}>
                  {String(m.role === "user" ? userTextForDisplay(m.content) : (m.content || ""))}
                </p>
              </div>
            ))}
            {!loading && !rows.length ? (
              <p className="hist-note">{t("这条会话在库里没有历史。")}</p>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}
