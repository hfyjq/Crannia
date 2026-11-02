// Mermaid 围栏 —— 懒加载 + 复用同一条消毒链。
//
// `import("mermaid")` 只在真的出现 ```mermaid 时才执行：它是 ~1 MB 的依赖。
//   Vite 会把它切成一个动态 chunk；打包后要走 `crannia-app://`，所以
//   桌面壳里的冒烟是验收项 —— 不通就退回静态 import。
//
// `htmlLabels: false`：Mermaid 默认把节点文字放进 `foreignObject`（HTML 岛），
//   而我们的消毒器明确剥掉 foreignObject（XSS 面 + 与 `<img>` 渲染冲突）。
//   关掉之后节点都是纯 SVG `<text>`，两份规则才对得上。
//
// `securityLevel:"strict"`：Mermaid 自己的第二道闸（它内部也会消毒/转义）。
//
// 渲染结果仍然要过 `SvgPreview` 的 DOMPurify —— 不因为"这是 Mermaid
//   生成的"就信任它：图里可能带着模型写的标签/链接。

import { useEffect, useState } from "react";
import SvgPreview from "./SvgPreview.jsx";

import { t } from "./i18n.js";
let _mermaid = null;

/** 取（并初始化）Mermaid —— 进程内只做一次。 */
export async function loadMermaid() {
  if (_mermaid) return _mermaid;
  const mod = await import("mermaid");
  const m = mod.default || mod;
  m.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    // 见文件头：foreignObject 与我们的消毒规则冲突。
    htmlLabels: false,
    theme: "neutral",
  });
  _mermaid = m;
  return m;
}

/** 测试用：丢掉已加载的实例（也让下一次 import 重新走一遍）。 */
export function resetMermaidForTests() {
  _mermaid = null;
}

export default function MermaidPreview({ code }) {
  const [state, setState] = useState({ svg: "", error: "" });

  useEffect(() => {
    let alive = true;
    setState({ svg: "", error: "" });
    (async () => {
      try {
        const m = await loadMermaid();
        // id 必须每次不同：Mermaid 用它给内部元素加 id，重复会串图。
        const id = "mmd-" + Math.random().toString(36).slice(2, 10);
        const out = await m.render(id, String(code || ""));
        const svg = (out && out.svg) || "";
        if (alive) setState({ svg, error: "" });
      } catch (e) {
        if (alive) {
          setState({ svg: "", error: String((e && e.message) || e || t("渲染失败")) });
        }
      }
    })();
    return () => { alive = false; };
  }, [code]);

  if (state.error) {
    return (
      <div className="md-svg">
        <div className="md-svg-note">{t("Mermaid 渲染失败：{state.error}", { "state.error": state.error })}</div>
        <pre className="md-svg-pre"><code>{String(code || "")}</code></pre>
      </div>
    );
  }
  if (!state.svg) return <div className="md-svg-note">{t("正在渲染图表…")}</div>;
  return <SvgPreview code={state.svg} label="Mermaid" />;
}
