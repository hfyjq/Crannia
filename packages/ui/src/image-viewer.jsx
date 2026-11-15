// 大图查看器 —— 一份，两边用（助手产物 / 用户消息里的附件）。
//
//   判据：预览这件事只要有两份实现，迟早一份支持左右翻、另一份不支持，
//   而用户看不出"为什么这张能翻"。
//
// ## 形状
//
//   · portal 到 body、Esc / 点遮罩关、关掉之后把焦点还给打开它的那个元素。
//     后两条不分家：它盖住整页，任何一条漏了都会让键盘用户卡在一个
//     看不见焦点的状态里。
//   · `items[]` + `activeIndex`：翻页要能左右环绕，而"当前第几张"必须是
//     显式状态 —— 靠 DOM 顺序的话，图片加载失败重排一次就翻错。
//
// token 走查询串（`<img>` 发不了自定义 header，与 `api.js::sseUrl` 同一条）：
//   地址由 `client.artifactUrl(id)` 异步取回 —— 这里只拿 id，不碰磁盘路径。

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ChevronLeft, ChevronRight, X } from "lucide-react";

import { t } from "./i18n.js";

/** 取一个产物 id 的可显示地址（异步：token 要先拿到）。
 *  拿不到，空串（不抛）—— 一张取不回来的图不该毁掉整条消息。 */
export function useArtifactUrl(client, id) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    if (!client || !id) return undefined;
    let alive = true;
    Promise.resolve()
      .then(() => client.artifactUrl(id))
      .then((u) => { if (alive) setUrl(u || ""); })
      .catch(() => { if (alive) setUrl(""); });
    return () => { alive = false; };
  }, [client, id]);
  return url;
}

/** 一张图（有 `url` 就直用 —— 刚发出去、还没落成产物的那一张是 data URL）。 */
function ViewImage({ item, client, className, onClick }) {
  const remote = useArtifactUrl(client, item && item.url ? "" : (item && item.id));
  const src = (item && item.url) || remote;
  if (!src) return <div className="artifact-loading">{t("正在取回…")}</div>;
  return <img className={className} src={src} alt={(item && item.name) || t("图片")}
              onClick={onClick} />;
}

/**
 * 全屏看大图。`items` = `[{id|url, name}]`，`index` = 点开的那一张。
 *
 * 翻页环绕（最后一张的下一张是第一张）：到头的"停住"会让人以为没有了。
 * `items.length === 1` 时不画箭头与角标（单张不存在"翻"这件事）。
 */
export function ImageViewer({ items, index = 0, onClose, client }) {
  const list = Array.isArray(items) ? items.filter(Boolean) : [];
  const [at, setAt] = useState(() => {
    const n = list.length;
    return n ? Math.max(0, Math.min(Number(index) || 0, n - 1)) : 0;
  });
  const closeRef = useRef(null);
  const restoreRef = useRef(null);

  const step = (d) => setAt((i) => (list.length ? (i + d + list.length) % list.length : 0));

  useEffect(() => {
    // 焦点还原：开着的时候把焦点给关闭钮，关掉还给点开它的那个元素。
    restoreRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement : null;
    closeRef.current?.focus();
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      restoreRef.current?.focus();
    };
    // 依赖只有 `onClose`：`step` 用的是函数式 set，不需要重挂监听。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onClose]);

  if (!list.length) return null;
  const multi = list.length > 1;

  return createPortal(
    <div className="backdrop img-viewer" role="dialog" aria-modal="true"
         aria-label={t("图片预览")} onClick={onClose}>
      <button ref={closeRef} type="button" className="artifact-close"
              title={t("关闭")} aria-label={t("关闭")}
              onClick={(e) => { e.stopPropagation(); onClose(); }}>
        <X size={18} />
      </button>
      {multi ? (
        <button type="button" className="img-nav prev" title={t("上一张")}
                aria-label={t("上一张")}
                onClick={(e) => { e.stopPropagation(); step(-1); }}>
          <ChevronLeft size={22} />
        </button>
      ) : null}
      <ViewImage item={list[at]} client={client} className="artifact-large"
                 onClick={(e) => e.stopPropagation()} />
      {multi ? (
        <>
          <button type="button" className="img-nav next" title={t("下一张")}
                  aria-label={t("下一张")}
                  onClick={(e) => { e.stopPropagation(); step(1); }}>
            <ChevronRight size={22} />
          </button>
          <span className="img-counter">{at + 1} / {list.length}</span>
        </>
      ) : null}
    </div>,
    document.body,
  );
}
