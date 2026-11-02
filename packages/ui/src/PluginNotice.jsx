import React, { useEffect, useRef } from "react";
import { noticeFacts, noticeKey, noticeKind, subjectWord } from "./plugin-notice-view.js";

//:`noticeText` 住在纯判据那个文件里（`node` 读不了 `.jsx`）。
//: 这里转出去，免得外面出现两套 import。
export { noticeText } from "./plugin-notice-view.js";

// 兼容性警告弹窗 —— 与「手动→自动」那条安全提示同一个设计。
//
// 一句话：后端判定"这个插件被屏蔽了"，这一屏把这个事实告诉用户。
//    两格必须同落 —— 文案里那句「此插件目前已被 Crannia 屏蔽」
//    是承重的：它承诺了一个后端动作，而那个动作在
//    `bus/registry.get_active_tool_names` 里（把它的工具全摘掉）。少了那一半，
//    这句话就是「标签在说谎」。
//
// 判据：
//   · 一定要看得见 · 在什么地方弹出都是这个表意（，全局面，
//     不属于任何会话）
//   · 即时响应：新包进来（外面读到的 / 用户直接装的）就检测，不兼容当场弹
//
// 它和安全提示那一条最大的不同：这条不是"点出来的"。
//   安全提示能播出来是因为弹窗是点出来的（那次点击就是一个用户手势）；
//   而本弹窗是框架事件触发的，没有那个手势，浏览器/Electron 的
//   自动播放策略会把 `.play()` 直接拦掉（静默：rejection 被吞掉）。
//   所以壳那一侧开了一格：`main.cjs` 的主窗 `webPreferences.autoplayPolicy =
//   "no-user-gesture-required"`（我们自己的壳，合法的）。
//   少了那一格，这一屏看着对、就是没声音，而没有任何东西会报错。

//: 「这一条已经给他看过了」——存 `localStorage`（同安全提示那条：
//:它是这个壳的界面偏好，不是框架行为设置）。
//:键里存的是 `since`（被屏蔽的时刻），不是"看过"这个布尔 ——
//:判据：同一个包修好又坏时它是另一件事（`since` 更新了），
//:   拿一个布尔去压它，用户就再也看不到第二次警告了。
//:读写一律包 `try`（某些上下文里 `localStorage` 会抛）——
//:读不到就照常弹（fail-open 到"提醒"这一侧）。
import { t } from "./i18n.js";
const SEEN_KEY = "crannia.plugin-notice.seen";

export function noticeSeen(entry) {
  try {
    const raw = JSON.parse(localStorage.getItem(SEEN_KEY) || "{}") || {};
    return Number(raw[entry?.owner] || 0) >= Number(entry?.since || 0);
  } catch { return false; }
}

export function rememberNotice(entry) {
  try {
    const raw = JSON.parse(localStorage.getItem(SEEN_KEY) || "{}") || {};
    raw[entry.owner] = Number(entry.since || 0);
    localStorage.setItem(SEEN_KEY, JSON.stringify(raw));
  } catch { /* 存不了，下次再提醒一遍（宁可多提醒，不可漏） */ }
}

export function PluginNotice({ entries, audioUrl, onConfirm }) {
  const audioRef = useRef(null);
  const list = entries || [];

  //:挂载即播（与安全提示同一个音效文件）。播放失败刻意吞掉不报：
  //:   少一声响不该在界面上再弹一条红字。
  //:依赖键是整批（`noticeKey`），不是第 0 条 —— 见那个函数的注释。
  useEffect(() => {
    if (!audioUrl) return undefined;
    let a = null;
    try {
      a = new Audio(audioUrl);
      a.volume = 0.7;
      audioRef.current = a;
      const p = a.play();
      if (p && typeof p.catch === "function") p.catch(() => {});
    } catch { /* 造不出来就当没有音频，不拦弹窗 */ }
    return () => { try { if (a) { a.pause(); a.currentTime = 0; } } catch { /* 忽略 */ } };
  }, [audioUrl, noticeKey(list)]);

  //:判据在 `plugin-notice-view.js`（纯函数，node 逐条断言）——
  //:一条说「此插件」· 多条说「部分插件」。这里只管画。
  const facts = noticeFacts(list);

  //:两件事分开讲：一批里可能既有"缺运行环境"又有"真不兼容"。
  //:   拿其中一种的口气盖住另一种，就是在替另一半说谎。
  const kind = noticeKind(list);
  const runtimes = facts.filter((f) => f.runtime);
  const broken = facts.filter((f) => !f.runtime);
  //:主语只在不兼容那一撮里数 —— 混合批次里说「部分插件」是对的，
  //:   而"此插件被屏蔽"那句必须只指着真被屏蔽的那几条。
  const subject = subjectWord(broken);

  //:点遮罩不关它 —— 这一屏要用户真的读一眼（同安全提示那条）。
  return (
    <div className="backdrop">
      <section className="safety plugin-notice" role="dialog" aria-modal="true"
               aria-labelledby="plugin-notice-title">
        <div className="safety-head">
          <h2 className="safety-title" id="plugin-notice-title">
            {kind === "runtime" ? t("运行环境缺失") : t("兼容性警告")}
          </h2>
        </div>
        {/* ── 缺运行环境：不是不兼容，装上就好 ------------------------ */}
        {runtimes.length > 0 && (
          <>
            <p className="safety-body">
              {t("我们的测试显示以下插件的运行需要额外依赖 —— 这并不是插件本身的问题，请先安装依赖，随后重启尝试。")}
            </p>
            <dl className="plugin-notice-facts">
              <dt>{t("缺少的程序")}</dt>
              {runtimes.map((f, i) => (
                <dd key={"rc" + i}>
                  {f.command ? t("`{cmd}`（装上它，或改用 Python / HTTP 型的 MCP）",
                                 { cmd: f.command }) : f.reason}
                </dd>
              ))}
              <dt>{t("插件")}</dt>
              {runtimes.map((f, i) => <dd key={"rn" + i}>{f.name}</dd>)}
            </dl>
          </>
        )}
        {/* ── 真不兼容：原来那段，一个字不改 ------------------------------ */}
        {broken.length > 0 && (
          <>
            <p className="safety-body">
              {t("我们的测试显示{subject}在", { "subject": subject })} <b>Crannia</b> {t("上的兼容性情况为")}
              <b className="plugin-notice-bad">{t("不支持")}</b>。
              {t("{subject}的某些或全部功能当前无法在 Crannia 上运行，", { subject })}
              <b>{t("{subject}目前已被 Crannia 屏蔽", { "subject": subject })}</b>。
              {t("我们仍努力在 Crannia 上为{subject}添加支持。", { subject })}
            </p>
            {/* 「冲突包名 / 位置」正好是 `Diagnosis` 那两个字段（`skill` / `path`）
                —— 不用另造数据，也不许在这里自己拼路径。
                多条时每个名字 / 每个位置各占一格。 */}
            <dl className="plugin-notice-facts">
              <dt>{t("冲突包名")}</dt>
              {broken.map((f, i) => <dd key={"n" + i}>{f.name}</dd>)}
              <dt>{t("位置")}</dt>
              {broken.map((f, i) => (
                <dd className="plugin-notice-path" key={"p" + i} title={f.path}>{f.path}</dd>
              ))}
            </dl>
          </>
        )}
        <div className="safety-actions">
          <button className="safety-go" onClick={() => onConfirm(list)}>{t("我知道了")}</button>
        </div>
      </section>
    </div>
  );
}
