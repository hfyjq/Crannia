// 模型写的 SVG —— 消毒 → data URL → `<img>`。
//
// 为什么不直接 `innerHTML` / 为什么不放进 `<iframe>`：
//   · `innerHTML` 会执行 `<script>` / `onload` —— 那是持久化 XSS（模型是从
//     网页内容里学的 SVG，它自己也不知道里面有什么）；
//   · `<img src="data:image/svg+xml,…">` 里脚本物理上不执行，而
//     SMIL（`<animate>` / `<animateTransform>` / `<animateMotion>`）照常播。
// DOMPurify 只是第二道，第一道是 `<img>` 本身。
//
// `<style>` 保留：模型与 Mermaid 都把样式
//   写在 `<style>` 里，剥掉会让图坏掉（轮子变实心黑圆、车架消失）。
//   安全性由 `<img>` 兜着 —— 图片文档是 secure static mode，脚本不执行、
//   外部资源不加载（见 `SVG_SANITIZE_CONFIG` 的 docstring）。
// `foreignObject` 仍剥掉：它能把任意 HTML 塞进 SVG 文档。
// 消毒失败 / 空结果 / 超过 200 KB，只显示源码 + 一句说明（不白屏）。

import { useMemo, useState } from "react";
import {
  downloadName, isSvgSource, sanitizeSvg, svgDataUrl, tooLarge,
} from "./svg.js";

// 消毒配置与判据住在 `svg.js`（一处定义，node 测试直接过 DOMPurify 验）。

import { t } from "./i18n.js";
export default function SvgPreview({ code, label = "SVG" }) {
  const [showSource, setShowSource] = useState(false);

  const { url, error } = useMemo(() => {
    const text = String(code == null ? "" : code);
    if (tooLarge(text)) {
      return { url: "", error: t("此SVG过大，此处只显示源码") };
    }
    if (!isSvgSource(text)) {
      return { url: "", error: t("这段内容不是 SVG 文件。") };
    }
    let clean = "";
    try {
      clean = sanitizeSvg(text);
    } catch (e) {
      return { url: "", error: t("清理失败：") + (e && e.message ? e.message : String(e)) };
    }
    if (!clean) {
      return { url: "", error: t("清理后内容为空：这段 SVG 里没有可保留的标签。") };
    }
    return { url: svgDataUrl(clean), error: "" };
  }, [code]);

  const src = String(code == null ? "" : code);

  return (
    <div className="md-svg">
      {url ? (
        <img className="md-svg-img" src={url} alt={label + t(" 图")} />
      ) : null}
      {error ? <div className="md-svg-note">{error}</div> : null}
      <div className="md-svg-actions">
        <button type="button" className="md-svg-btn"
                onClick={() => setShowSource((v) => !v)}>
          {showSource ? t("收起源码") : t("查看源码")}
        </button>
        <button type="button" className="md-svg-btn"
                onClick={() => downloadSvg(src)}>
          {t("下载 SVG")}
        </button>
      </div>
      {showSource || !url ? (
        <pre className="md-svg-pre"><code>{src}</code></pre>
      ) : null}
    </div>
  );
}

/** 把 SVG 保存到本机（走 Blob，不导航 —— 壳那边 `will-navigate` 没接）。 */
export function downloadSvg(text) {
  try {
    const blob = new Blob([String(text || "")], { type: "image/svg+xml" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = downloadName(text);
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
  } catch {
    /* 存不下就算了 —— 源码还在，用户可以自己复制（不弹一个假成功） */
  }
}
