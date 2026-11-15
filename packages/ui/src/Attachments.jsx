// 用户消息下面那一排附件（图 = 缩略图，文件 = 图标 + 名字）。
//
// ## 为什么要它
//
// 以前附件是看不见的：图片只剩正文里那句给模型看的指针
// `[图片 1 —— 原图在 …]`，文件只剩前言（还被界面剥掉了）——
// 发一条"只有附件"的消息，气泡里几乎是空的，用户不知道对方到底收到了什么。
//
// ## 形状
//
//   · 缩略图两档：单张按长边 240px、比例夹在 [0.25, 4]；多张时缩成 64px
//     方贴片。分界是"一屏放得下几张"—— 单张要看得清，多张要排得下。
//   · 文件卡：图标 + 文件名 + 类型/大小。三项都来自本地元数据，
//     不在渲染时读文件内容（一列几百个附件时，只读元数据才滚得动）。
//   · 点图开全屏预览，翻页见 `image-viewer.jsx`。
//
// ## 两条纪律
//
// 1. 图片地址只由 `id` 拼（`client.artifactUrl`）—— 库里那句指针里的
//    路径只是解析用，UI 拿到的永远是 id（`core/artifacts.py` 的口径）。
//    刚发出去、还没落成产物的那一张直接给 data URL（`item.url`）。
// 2. 排在气泡下面 —— 上面那条窄栏留给正文。

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { File, FileArchive, FileImage, FileSpreadsheet, FileText, X } from "lucide-react";

import { t } from "./i18n.js";
import { ImageViewer, useArtifactUrl } from "./image-viewer.jsx";

/** `123456`，`120.6 KB`。读不到大小，空串（不写 `0 B` —— 那是另一个事实）。 */
export function fmtSize(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return "";
  if (v < 1024) return v + " B";
  if (v < 1024 * 1024) return (v / 1024).toFixed(1) + " KB";
  return (v / 1024 / 1024).toFixed(1) + " MB";
}

/** 扩展名，图标。认不出就给通用 `File`（不猜成文本/表格）。 */
export function iconFor(name) {
  const ext = String(name || "").split(".").pop().toLowerCase();
  if (["pdf", "doc", "docx", "rtf", "md", "txt"].includes(ext)) return FileText;
  if (["xls", "xlsx", "csv"].includes(ext)) return FileSpreadsheet;
  if (["png", "jpg", "jpeg", "webp", "gif", "svg"].includes(ext)) return FileImage;
  if (["zip", "rar", "7z", "tar", "gz"].includes(ext)) return FileArchive;
  return File;
}

function Thumb({ item, client, onClick, single }) {
  const remote = useArtifactUrl(client, item.url ? "" : item.id);
  const src = item.url || remote;
  return (
    <button type="button" className={"att-img" + (single ? " single" : "")}
            title={item.name || t("图片")} onClick={onClick}>
      {/* 取不回来时留一个占位，不是零宽度 —— 否则点不到、也看不出少了一张 */}
      {src ? <img src={src} alt={item.name || t("图片")} loading="lazy" />
        : <span className="att-img-missing">{t("正在取回…")}</span>}
    </button>
  );
}

/**
 * 点开一个附件看全文。
 *
 * 只服务非图片那一类（图片有 `ImageViewer`，那是看图不是看字）。
 *   · `kind:"text"` —— 粘贴的长文本，正文就在消息里；
 *   · `kind:"file"` —— 只带路径的那种：文字内容不在消息里，
 *     面板如实给名字 / 大小 / 路径，不假装自己能读文件。
 *
 * 外壳复用 `.backdrop`（与图片查看器、自动模式说明同一套）：
 * portal 到 body、`Esc` / 点空白关、关掉把焦点还给打开它的那个元素。
 * 正文按纯文本渲染 —— 那是用户的原文，渲染 Markdown 会改写它。
 */
export function AttachmentPanel({ item, onClose }) {
  const closeRef = useRef(null);
  const restoreRef = useRef(null);
  useEffect(() => {
    restoreRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement : null;
    closeRef.current?.focus();
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      restoreRef.current?.focus();
    };
  }, [onClose]);

  if (!item) return null;
  const body = item.kind === "text" ? String(item.text || "") : "";

  return createPortal(
    <div className="backdrop" role="dialog" aria-modal="true"
         aria-label={t("附件全文")} onClick={onClose}>
      <div className="attach-panel" onClick={(e) => e.stopPropagation()}>
        <div className="skill-panel-head">
          <h2 className="skill-panel-title">{item.name || t("附件")}</h2>
          <button ref={closeRef} type="button" className="skill-panel-close"
                  title={t("关闭")} aria-label={t("关闭")} onClick={onClose}>
            <X size={16} />
          </button>
        </div>
        {item.kind === "text" ? (
          body
            ? <pre className="attach-panel-text">{body}</pre>
            : <p className="skill-panel-note">{t("这条附件没有正文。")}</p>
        ) : (
          <div className="skill-panel-body">
            {item.size ? <p className="attach-panel-meta">{fmtSize(item.size)}</p> : null}
            <pre className="attach-panel-text">{item.path || ""}</pre>
            <p className="skill-panel-note">
              {t("内容在下面这个路径的文件里，需要时由文件工具去读。")}
            </p>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}

/**
 * 一条用户消息的附件条。`items` 来自 `parseAttachments()`（或刚发出去时的本地形状）。
 *
 * 结构：先文件、后图片 —— 文件是一行行读的（名字要看得清），
 * 图片是缩略图（看一眼就够）；两者的信息密度不一样，不混排。
 */
export function MessageAttachments({ items, client }) {
  const [openAt, setOpenAt] = useState(-1);
  //:点开看全文的那个附件（非图片）。发送前后两处都能点 ——
  //:  只做一处的话，用户看到的是"有时能点、有时不能"。
  const [openFile, setOpenFile] = useState(null);
  const list = Array.isArray(items) ? items.filter(Boolean) : [];
  if (!list.length) return null;
  const images = list.filter((i) => i.kind === "image");
  const files = list.filter((i) => i.kind !== "image");

  return (
    <div className="msg-attachments">
      {files.length ? (
        <div className="att-files">
          {files.map((f, i) => {
            const Icon = iconFor(f.name);
            //: 内联文本的大小是字数不是字节（`fmtSize` 会把它算成 KB，
            //:   而那是个它不掌握的事实）；名字里本来就带着字数。
            const size = f.kind === "text" ? "" : fmtSize(f.size);
            return (
              <button type="button" className="att-file" key={(f.name || "") + i}
                      title={t("点开看全文")} onClick={() => setOpenFile(f)}>
                <Icon size={14} />
                <span className="att-file-name">{f.name || t("（未命名）")}</span>
                {size ? <span className="att-file-size">{size}</span> : null}
              </button>
            );
          })}
        </div>
      ) : null}
      {openFile ? (
        <AttachmentPanel item={openFile} onClose={() => setOpenFile(null)} />
      ) : null}
      {images.length ? (
        <div className={"att-images" + (images.length === 1 ? " single" : " stack")}>
          {images.map((im, i) => (
            <Thumb key={(im.id || im.url || "") + i} item={im} client={client}
                   single={images.length === 1} onClick={() => setOpenAt(i)} />
          ))}
        </div>
      ) : null}
      {openAt >= 0 ? (
        <ImageViewer items={images} index={openAt} client={client}
                     onClose={() => setOpenAt(-1)} />
      ) : null}
    </div>
  );
}
