import React, { useEffect, useMemo, useRef, useState } from "react";
import { FileText, Plus, Send, X } from "lucide-react";
import { StatusBar } from "./StatusBar.jsx";
// 上拉框是共用组件 —— 设置页的选项行也用它。
//   所以一处实现、两边 import；这里不再留一份。
import { UpPicker } from "./UpPicker.jsx";
import { takeoverAvailability, takeoverLabel } from "./takeover-model.js";
import { randomLoadingPhrase } from "./loadingPhrases.js";
// 模式档表与模式 chip 的判据同源（`mode.js`）—— 这里只引用，不另抄一份。
import { MODE_LABELS as MODES } from "./mode.js";
// 无主理人群聊的 `@` 候选层。判据全在 `mention.js`（纯函数），
//   这里只负责渲染与按键 —— 与 `/` 候选层同一套交互（`↑` `↓` / `Tab` / `Enter` / `Esc` / 鼠标）。
import { applyMention, mentionEntries, mentionItems, mentionQuery } from "./mention.js";

// 输入区：文本输入 + 附件 + 模式/投入档选择 + 发送。
//
// · 模式是一个上拉框（一个按钮，点一下向上展开切换）。
// · 投入档同理，且显示档位名称、不显示档位数字（`xhigh` 而不是 `4`）。
// · 左下角 `+`，文件上传；发送框下面不再有额外的一行。

/** 投入档的档名。与 `core/tiers.py::LEVEL_NAMES` 同源（1–6，这六个名字）。
 *  这里显示的是名字，不显示档位数字。 */
import { t } from "./i18n.js";
const EFFORT_NAMES = { 1: "low", 2: "medium", 3: "high", 4: "xhigh", 5: "max", 6: "ultra" };

//:粘贴的长文本超过它就自动变成附件（避免把大段文本直接塞进上下文）。
//:这个阈值是起点值，后续按实际使用观感调整。
const LONG_PASTE_CHARS = 1500;
//: 与后端 `core/image_input.py::MAX_IMAGES_PER_TURN` 对齐（= 20）。
const MAX_PENDING_IMAGES = 20;
//:横幅延迟多久才露头（750ms）。
//:它挡的是零点几秒就结束的短命令（`/clear` 那类）：短命令从头到尾不显示横幅；
//: 正常的任务本来就在 750ms 以上，只是一开始那 0.75 秒不显示而已。
const BAR_DELAY_MS = 750;
//:宽松的类型过滤。只是方便，不是边界 —— 走「只给路径」这一路时任何文件都能被
//: `read_file` 读，挡在选择器外面没有意义。
const ACCEPT = "image/*,application/pdf,text/*,.md,.json,.yaml,.yml,.csv,.docx,.xlsx,.pptx";

// `UpPicker` 住在 `./UpPicker.jsx` —— 设置页的选项行也用它。
//   开合状态由调用方持有；「点外面就关」的 listener 不放在它里面。
//   这里不留第二份实现 —— 两份会各自漂移。

export function Composer({
  client, onSend, busy, mode, onMode, effort, onEffort, connected, running, onStart, waiting,
  takeover, onTakeover,
  //:群聊不画模式选择器（三种群聊一律固定「自动」）。
  hideMode,
  //:模型连接重试 —— 横幅变红 + 「打断」；收尾后的错误横幅用 `onDismissError`。
  retry, onCancel, onDismissError,
  //:压缩中 / 压缩失败 / 没得压 —— 与 `retry` 同一条横幅（`StatusBar`）。
  //:压缩期间输入框与发送一起暂停：中途再发一条 = 并发改同一段历史。
  //:`compactNothing` 是第三态：`/compact` 说"轮次不够"时
  //:   它不是失败 —— 走过去会翻成红横幅「API错误…」，那是另一件事。
  compacting, compactFailed, compactNothing, compactSince, onDismissCompact,
  compactDone, compactNotice, compactMessage,
  //:模型 API 还没配好（`/api/status.model_api_ready === false`）。
  //:`undefined` = 还没读到，不显示、不拦（宁可放行一次，也不误挡）。
  apiReady, onGoEndpoint,
  //:只有无主理人群聊才传这张名册（`{name, role, avatar}[]`），
  //:别的场合传 `null`，`@` 候选层根本不存在（不占位、不拦键）。
  mentions,
  //:撤回一条排队中的消息，它被放回输入框：文案接在前面，图片/附件接回去。
  //:   判据是 `seq`（每次撤回都是一个新对象）—— 同一个对象不会重复应用。
  prefill,
}) {
  const [draft, setDraft] = useState("");
  //: 图片 —— 走后端 `core/image_input.py`（base64 多模态）。
  const [images, setImages] = useState([]);
  //:非图片附件 —— 只给路径（拿不到路径时退回内联文本，见 `addFiles`）。
  const [files, setFiles] = useState([]);
  //:桌面桥出错时的那一句 —— 必须显示出来：它不能与「这个文件没有路径」
  //:   塌成同一个值，否则壳坏了而界面一个字都不说。
  //:每次重新挑文件时清空（见 `addFiles`）；不做「知道了」按钮 —— 它不是一条
  //:   要用户处理的提示，是一次读数（同 `slash-err`）。
  const [attachErr, setAttachErr] = useState("");
  const [phrase, setPhrase] = useState(() => randomLoadingPhrase());
  const [turnStartedAt, setTurnStartedAt] = useState(0);
  const [outputTokens, setOutputTokens] = useState(0);
  //:横幅可见与否 —— 与 `busy` 差一个 `BAR_DELAY_MS` 的延迟。
  const [barOpen, setBarOpen] = useState(false);
  //:两个上拉框共用这一个开合状态，天然互斥。
  const [openPicker, setOpenPicker] = useState("");
  const fileInput = useRef(null);
  const box = useRef(null);
  //: 上一次应用过的 `prefill.seq`（撤销是按次的，不是按对象内容）。
  const lastPrefillRef = useRef(0);
  useEffect(() => {
    if (!prefill || !prefill.seq || prefill.seq === lastPrefillRef.current) return;
    lastPrefillRef.current = prefill.seq;
    if (prefill.text) setDraft((d) => (d ? prefill.text + "\n" + d : prefill.text));
    if (Array.isArray(prefill.images) && prefill.images.length) {
      setImages((x) => x.concat(prefill.images).slice(0, MAX_PENDING_IMAGES));
    }
    if (Array.isArray(prefill.files) && prefill.files.length) {
      setFiles((x) => x.concat(prefill.files));
    }
    //: 放回去之后把光标放回输入框（用户接下来多半是要改两个字）。
    try { box.current && box.current.focus(); } catch { /* 聚焦失败不影响还原 */ }
  }, [prefill]);
  //: `@` 候选层：光标位置 + 高亮项 + 这次按 Esc 关掉没有。
  //:光看 `draft` 不够 —— `@` 可以打在句中，判据是光标处正在打的那个 token。
  const ta = useRef(null);
  const pendingCaret = useRef(null);
  const [caret, setCaret] = useState(0);
  const [mentionSel, setMentionSel] = useState(0);
  const [mentionDismissed, setMentionDismissed] = useState(false);

  // 「点面板外面就关上」—— 一个 listener 管两个（共用状态，不必各写一份）。
  // 判据是 `closest(".popover-host")`：点另一个 picker 的按钮时
  //   它也算"里面"（不关）—— 而那一支由那个按钮自己的 onClick 接手
  //   （把 openPicker 换成它的名字，前一个就关了）。两条路各管一半，
  //   合起来才是"点开一个、前一个关闭"。
  useEffect(() => {
    if (!openPicker) return undefined;
    const close = (e) => {
      if (!e.target.closest || !e.target.closest(".popover-host")) setOpenPicker("");
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [openPicker]);

  // 状态横幅：每轮换一句 + 记下起点 + 决定露不露头。
  //
  // 判据是 750ms 之后这一轮还在不在跑：还在就出来，已经结束了就从头到尾没出现过
  //    （短命令不应闪一下）。落地就是 `setTimeout` + 在 `busy` 翻回去时 `clearTimeout`。
  //    它不改变任何一轮的行为 —— 短命令该多快还是多快，只是不再闪一下。
  //
  // `setTurnStartedAt` 在这里（而不是 750ms 之后）—— 计时从真起点算，
  //    所以横幅露头时显示的就是 ~0.75s，不是 0。
  useEffect(() => {
    if (!busy) {
      setBarOpen(false);
      return undefined;
    }
    setPhrase(randomLoadingPhrase());
    setTurnStartedAt(Date.now());
    setOutputTokens(0);
    const timer = setTimeout(() => setBarOpen(true), BAR_DELAY_MS);
    return () => clearTimeout(timer);
  }, [busy]);

  //:`turn.end` 到来，这一轮的 `output_tokens`。本轮输出，不是会话累计。
  useEffect(() => {
    if (!client || !client.subscribe) return undefined;
    return client.subscribe("turn.end", (d) => setOutputTokens(d.output_tokens || 0));
  }, [client]);

  async function toAttachment(file) {
    if (file.type && file.type.startsWith("image/")) {
      const url = await new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res(r.result);
        r.onerror = rej;
        r.readAsDataURL(file);
      });
      return { kind: "image", name: file.name, url };
    }
    // Electron ≥ 32 移除了 `File.path`，真实路径只能从 preload 的桥拿。
    //
    // 「桥坏了」与「这个文件没有路径」是两件事，不许塌成一个值。
    //   若桥出错时一律返回 `undefined`，「这台机器的壳坏了」在界面上就会长得像
    //   「用户自己挑了个没有路径的文件」—— 用户会去检查自己的操作，而该查的是壳。
    //   所以桥出错时会抛；这里必须接住，而且接住之后要分两步走：
    //     ① 把那句话显示出来（`attachErr`）② 照样降级成内联文本 ——
    //       不许因为壳坏了就让用户连附件都加不上（安全机制不许妨碍把事做完）。
    let path;
    let bridgeErr = "";
    try {
      path = window.crannia && window.crannia.pathForFile
        ? window.crannia.pathForFile(file)
        : undefined;
    } catch (err) {
      bridgeErr = (err && err.message) || String(err);
      path = undefined;
    }
    if (path) return { kind: "path", name: file.name, size: file.size, path };
    if (bridgeErr) setAttachErr(bridgeErr);
    // 兜底：浏览器里（`dev:web`）拿不到路径，只能内联文本。
    //   与「只给路径」不是一回事，所以这里如实标出来（`kind:"text"`）。
    const text = await file.text().catch(() => "");
    return { kind: "text", name: file.name, size: file.size, text };
  }

  async function addFiles(list) {
    const picked = Array.from(list || []);
    if (!picked.length) return;
    setAttachErr("");                        // 新的一批，上一批的读数作废
    const out = [];
    for (const f of picked) out.push(await toAttachment(f));
    const nextImages = out.filter((a) => a.kind === "image");
    const nextFiles = out.filter((a) => a.kind !== "image");
    setImages((old) => old.concat(nextImages).slice(0, MAX_PENDING_IMAGES));
    setFiles((old) => old.concat(nextFiles));
  }

  /** 发出前把「非图片附件」拼成一段给模型看的说明。
   *  措辞要告诉它这是什么、名字是什么、路径在哪、去哪里读。
   *  路径只对模型说一次；内联兜底那支要如实说"内容已随消息给出"，别让它再去读一遍。 */
  function attachmentPreamble() {
    if (!files.length) return "";
    const lines = files.map((f) =>
      f.kind === "path"
        ? `[附件] ${f.name}（${f.size} 字节）：${f.path}\n  这个文件以**路径**形式给你 —— 需要内容时用你的文件工具（read_file / glob / grep）去读。`
        : `[附件] ${f.name}（${f.size} 字节）：内容已随本消息给出（**未**以路径形式提供）。`
    );
    return lines.join("\n") + "\n\n";
  }

  function payloadText(body) {
    const pre = attachmentPreamble();
    return pre ? pre + body : body;
  }

  function send() {
    //:正在压缩，不发：压缩会原地改写这一段历史，
    //:   此时再塞一条进去，等于两个人同时改同一个数组。
    if (compacting) return;
    //:没有可用的模型 API，这一句不发（红横幅已经在上面说明）。
    //:别让它发出去再吃一个上游 401 —— 那看起来像"软件坏了"，
    //:   而真正缺的是配置。
    if (apiReady === false) return;
    const text = draft.trim();
    if (!text && !images.length && !files.length) return;
    // 发的是 `{data_url}`，不是裸字符串 —— 前后端契约必须是同一个字段名。
    //   若这里发 `i.url` 而后端只认 `data_url`，错误不出声：后端对认不出的条目
    //   `continue`，每一张粘贴的图都被丢掉，模型只收到文字。
    // `name` 一起带上：气泡下面的缩略图/查看器要显示它（后端只读 data_url，
    //   多余字段忽略 —— 加名字不改变协议）。
    // 传原始的 chips（不是给接口的映射）：撤回时要一模一样放回输入框。
    onSend(payloadText(text), images, files);
    setDraft("");
    setImages([]);
    setFiles([]);
  }

  function onKeyDown(e) {
    // `@` 候选层与 `/` 候选层同一套键位。
    //   两层互斥（`mentionQ` 在 `slashOpen` 时不产生），所以顺序不影响判据。
    if (mentionOpen) {
      if (e.key === "ArrowDown") { e.preventDefault(); moveMention(1); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); moveMention(-1); return; }
      if (e.key === "Escape") { e.preventDefault(); setMentionDismissed(true); return; }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        // 采纳成功才吃掉这个键；没得采纳就照旧发给 `send()`
        if (acceptMention()) { e.preventDefault(); return; }
      }
    }
    // 候选层开着时，这几个键先给它（没开着则一个字都不动）。
    if (slashOpen) {
      if (e.key === "ArrowDown") { e.preventDefault(); moveSlash(1); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); moveSlash(-1); return; }
      if (e.key === "Escape") { e.preventDefault(); setSlashDismissed(true); return; }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        // 采纳成功才吃掉这个键；没得采纳就照旧发给 `send()`
        if (acceptSlash()) { e.preventDefault(); return; }
      }
    }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  }

  async function onPaste(e) {
    const items = Array.from((e.clipboardData && e.clipboardData.items) || []);
    const blobs = items.filter((i) => i.kind === "file").map((i) => i.getAsFile()).filter(Boolean);
    if (blobs.length) { e.preventDefault(); await addFiles(blobs); return; }
    // 长文本 → 自动变附件。
    const text = e.clipboardData && e.clipboardData.getData("text/plain");
    if (text && text.length >= LONG_PASTE_CHARS) {
      e.preventDefault();
      setFiles((old) => old.concat([{
        kind: "text", name: t("粘贴的文本（{text.length} 字）", { "text.length": text.length }), size: text.length, text,
      }]));
    }
  }

  // `/` 候选层
  //
  // 数据只从后端来（`GET /api/commands`）—— 前端不抄第二份名单，
  //    抄一份就会与后端那张表各自漂移。
  // 与 WebUI 那一半同一套判据：`↑` `↓` 移动 · `Tab` / `Enter` 采纳 ·
  //    `Esc` 关 · 鼠标也能点；采纳时把后面留着。
  // 取不到 => 说清，不许静悄悄地什么都不弹（读不到 ≠ 没有）。
  const slashAll = useRef(null);          // 缓存的整份目录
  const slashTriedAt = useRef(0);
  const [slashErr, setSlashErr] = useState("");
  const [slashSel, setSlashSel] = useState(0);
  const [slashDismissed, setSlashDismissed] = useState(false);
  const [slashTick, setSlashTick] = useState(0);

  /** 当前输入的头一个词；不构成候选条件时 `null`。
   *  不是「见到空格就收」：头一个词正好是一条完整命令才收
   *    （那时是在打参数）；`/co 你好` 里那个 `/co` 还没选完，候选继续开。 */
  const slashTok = useMemo(() => {
    const v = draft || "";
    if (!v.startsWith("/")) return null;
    const m = v.match(/^(\S*)/);
    const tok = m ? m[1] : "";
    if (!tok) return null;
    if (tok === v) return tok;
    const all = slashAll.current;
    const exact = !!all && all.some(
      (c) => String(c.cmd || "").toLowerCase() === tok.toLowerCase());
    return exact ? null : tok;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, slashTick]);

  const slashItems = useMemo(() => {
    if (!slashAll.current || slashTok === null) return [];
    const pre = slashTok.toLowerCase();
    return slashAll.current
      .filter((c) => String(c.cmd || "").toLowerCase().startsWith(pre))
      .slice(0, 12);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slashTok, slashTick]);

  const slashNeedsLoad = slashTok !== null && !slashAll.current
    && Date.now() - slashTriedAt.current > 3000;
  const slashOpen = slashTok !== null && !slashDismissed
    && (slashItems.length > 0 || (!!slashErr && slashItems.length === 0));

  useEffect(() => {
    if (!slashNeedsLoad) return undefined;
    slashTriedAt.current = Date.now();
    let alive = true;
    client.call("/api/commands").then((d) => {
      if (!alive) return;
      if (d && d.ok && Array.isArray(d.commands)) {
        slashAll.current = d.commands;
        setSlashErr("");
      } else {
        // 后端如实回了 `ok:false`（例如框架没起来）—— 那不是
        //    「命令是空的」。不缓存失败 => 下次再试。
        slashAll.current = null;
        setSlashErr(String((d && d.error) || t("后端没有给出命令目录")));
      }
      setSlashTick((n) => n + 1);
    }).catch((e) => {
      if (!alive) return;
      slashAll.current = null;
      setSlashErr((e && e.message) ? e.message : String(e));
      setSlashTick((n) => n + 1);
    });
    return () => { alive = false; };
  }, [slashNeedsLoad, client]);

  function acceptSlash(idx) {
    const i = (typeof idx === "number") ? idx : slashSel;
    const item = slashItems[i];
    if (!item || !item.cmd) return false;
    const tok = slashTok || "";
    setDraft(String(item.cmd) + " " + (draft || "").slice(tok.length).replace(/^\s+/, ""));
    setSlashDismissed(true);
    setSlashSel(0);
    return true;
  }

  function moveSlash(delta) {
    if (!slashItems.length) return;
    const n = slashItems.length;
    setSlashSel((i) => (i + delta + n) % n);
  }

  // `@` 候选层
  //
  // 名单只从群里来（`chatGroup.members`）—— 前端不维护第二份成员表。
  // 只在无主理人群聊里存在：`App` 在 `groupMode` 为假时传 `null`，
  //   于是下面每一格都退化成"不存在"，普通对话与有主理人的群一个字节都不变。
  // 与 `/` 候选层互斥：以 `/` 开头的那个词还没选完时不弹 `@`（两层叠在一起会抢键）。
  const mentionAll = useMemo(() => mentionEntries(mentions), [mentions]);
  const mentionQ = useMemo(() => {
    if (!mentions || !mentions.length) return null;
    if (slashOpen) return null;
    return mentionQuery(draft, caret);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mentions, draft, caret, slashOpen]);
  const mentionList = useMemo(
    () => (mentionQ ? mentionItems(mentionAll, mentionQ.query) : []),
    [mentionQ, mentionAll]);
  const mentionOpen = !!mentionQ && !mentionDismissed && mentionList.length > 0;

  /** 采纳后把光标放回输入框（`draft` 是受控值，只能在这一帧之后设）。
   *  不 `focus()` 的话，鼠标点候选、或 `Tab` 之后焦点会掉到按钮上。 */
  useEffect(() => {
    if (pendingCaret.current == null) return;
    const pos = pendingCaret.current;
    pendingCaret.current = null;
    const el = ta.current;
    if (el) {
      el.focus();
      el.setSelectionRange(pos, pos);
      setCaret(pos);
    }
  }, [draft]);

  function acceptMention(idx) {
    const i = (typeof idx === "number") ? idx : mentionSel;
    const item = mentionList[i];
    if (!item || !mentionQ) return false;
    const out = applyMention(draft, mentionQ, item);
    pendingCaret.current = out.caret;
    setDraft(out.text);
    setMentionDismissed(true);
    setMentionSel(0);
    return true;
  }

  function moveMention(delta) {
    if (!mentionList.length) return;
    const n = mentionList.length;
    setMentionSel((i) => (i + delta + n) % n);
  }

  const chips = images.concat(files);

  return (
    <div className="composer-wrap">
      {/* 没配模型 API 时一直亮着，点它直接去端配置页。
          文案是用户逐字给的。 */}
      {apiReady === false ? (
        <button type="button" className="api-missing" onClick={onGoEndpoint}
                title={t("打开端配置")}>
          {t("API未配置，前往端配置页配置后开始")}
        </button>
      ) : null}
      <StatusBar phrase={phrase} startedAt={turnStartedAt} running={barOpen}
                 outputTokens={outputTokens} waiting={waiting}
                 retry={retry} onCancel={onCancel} onDismissError={onDismissError}
                 compacting={compacting} compactFailed={compactFailed}
                 compactSince={compactSince}
                 compactNothing={compactNothing}
                 compactDone={compactDone} compactNotice={compactNotice}
                 compactMessage={compactMessage}
                 onDismissCompact={onDismissCompact} />

      <div
        className="composer"
        ref={box}
        onDragOver={(e) => { e.preventDefault(); box.current && box.current.classList.add("dropping"); }}
        onDragLeave={() => box.current && box.current.classList.remove("dropping")}
        onDrop={async (e) => {
          e.preventDefault();
          box.current && box.current.classList.remove("dropping");
          await addFiles(e.dataTransfer.files);
        }}
      >
        {chips.length ? (
          <div className="attach-strip">
            {chips.map((a, i) => (
              <span className={`attach-chip ${a.kind}`} key={a.kind + i} title={a.path || a.name}>
                <FileText size={12} /><span className="attach-name">{a.name}</span>
                <button onClick={() => {
                  if (a.kind === "image") setImages((old) => old.filter((_, j) => j !== i));
                  else setFiles((old) => old.filter((_, j) => j !== i - images.length));
                }}><X size={11} /></button>
              </span>
            ))}
          </div>
        ) : null}

        {attachErr ? (
          // 壳坏了要看得见，但不许因此拦住用户 ——
          //   所以这句话同时说清两件事：出了什么问题 · 这次是怎么发的。
          <div className="attach-err">
            {t("桌面桥出错，这个文件改按内联文本发：{attachErr}", { "attachErr": attachErr })}
          </div>
        ) : null}

        {mentionOpen ? (
          // 复用 `/` 候选层那套外壳 —— `mention-pop` 只做一处覆盖：
          //   名字不是命令，等宽字体收回来（见 styles.css）。
          //   `data-kind` 给测试一个稳定的锚点（不靠文案）。
          <div className="slash-pop mention-pop" data-kind="mention">
            {mentionList.map((m, i) => (
              <div key={m.token}
                   className={"slash-item" + (i === mentionSel ? " sel" : "")}
                   title={m.all ? m.desc : `${m.name} — ${m.desc}`}
                   onMouseDown={(e) => {
                     // 同 `/` 那一层：`mousedown` 不用 `click`（`click` 之前 textarea 会先失焦）
                     e.preventDefault();
                     acceptMention(i);
                   }}>
                <span className="slash-cmd">{m.token}</span>
                <span className="slash-desc">{m.desc}</span>
              </div>
            ))}
          </div>
        ) : null}

        {slashOpen ? (
          <div className="slash-pop">
            {slashItems.length ? slashItems.map((c, i) => (
              <div key={c.cmd}
                   className={"slash-item" + (i === slashSel ? " sel" : "")}
                   onMouseDown={(e) => {
                     // `mousedown` 不用 `click`：`click` 之前 textarea 会先失焦
                     e.preventDefault();
                     acceptSlash(i);
                   }}>
                <span className="slash-cmd">{c.cmd}</span>
                <span className="slash-desc">{c.desc}</span>
                {c.implemented === false ? <span className="slash-todo">{t("未实现")}</span> : null}
              </div>
            )) : (
              <div className="slash-err">
                {t("命令列表读取失败，回车将按原文发送。")}
              </div>
            )}
          </div>
        ) : null}

        <textarea
          ref={ta}
          value={draft}
          //:压缩期间暂停输入，与上面 `send()` 那道闸成对。
          //:不禁用的话，用户能在压缩中途打好一句话、回车——`send()` 已拦，
          //:     但"打得进去、发不出去"本身也是一种说不出话的状态。
          disabled={!!compacting}
          onChange={(e) => {
            setDraft(e.target.value);
            // 光标跟着输入走 —— `@` 候选层的判据是光标处那个 token，不是整段文本。
            setCaret(e.target.selectionStart == null ? e.target.value.length : e.target.selectionStart);
            setSlashDismissed(false);
            setSlashSel(0);
            setMentionDismissed(false);
            setMentionSel(0);
          }}
          // 方向键 / 鼠标点进文本 / 粘贴之后，光标都会动，只有 `onChange` 不够。
          onSelect={(e) => setCaret(e.target.selectionStart == null ? 0 : e.target.selectionStart)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          placeholder={t("我可以怎么帮助你？")}
          rows="3"
        />

        <div className="composer-row">
          <button className="round attach" onClick={() => fileInput.current && fileInput.current.click()}
                  title={t("添加文件")}>
            <Plus size={20} />
          </button>
          <input
            ref={fileInput} type="file" multiple hidden accept={ACCEPT}
            onChange={async (e) => { await addFiles(e.target.files); e.target.value = ""; }}
          />

          {/* 模式 / 投入档 —— 两个上拉框，共用一个开合状态，点开一个，前一个自动关闭。 */}
          {/* 群聊不画模式选择器 —— 三种群聊一律固定「自动」。
              不是"藏起来但还能用"：后端 `core/modes.set_mode` 对群键
              直接拒绝（`auto` 除外），所以界面这里没有它在说谎的空间。 */}
          {hideMode ? null : (
            <UpPicker
              name="mode" label={t("模式")} value={mode}
              options={MODES.map((m) => ({ ...m, label: t(m.label) }))} onPick={onMode}
              //: 「保护已降级」那句话没有丢 —— 它在模式 chip 的悬停说明里，
              //:   同时 chip 本身写着「完全权限」。
              title={mode === "full_access"
                ? t("完全权限：审批不再询问；出网命令和受保护路径仍会拦截，批量删除仍会确认一次。ICE 信息层已关闭（退出即恢复）。")
                : t("切换本对话的工作模式（立即生效）")}
              open={openPicker === "mode"}
              onToggle={(v) => setOpenPicker(v ? "mode" : "")}
              disabled={!connected}
            />
          )}
          <UpPicker
            name="effort" label={t("投入档")} value={effort}
            options={Object.keys(EFFORT_NAMES).map((n) => ({ key: Number(n), label: EFFORT_NAMES[n] }))}
            onPick={onEffort}
            open={openPicker === "effort"}
            onToggle={(v) => setOpenPicker(v ? "effort" : "")}
            disabled={!connected}
          />

          {/* 接管按钮 —— 位于投入档右侧，颜色深一级，仅「完全权限」可用；
              其他模式下悬停提示"需要更高权限"且点不动。
              群聊里不画它：群固定「自动」，这一档永远进不去（见上面 `hideMode`）。 */}
          {hideMode ? null : (() => {
            const enabled = !!(takeover && takeover.enabled);
            const desktop = !!(typeof window !== "undefined"
              && window.crannia && window.crannia.takeoverOverlay);
            const avail = takeoverAvailability({ mode, connected, enabled, desktop });
            return (
              <span className="takeover-wrap" title={avail.reason || undefined}>
                <button className={"takeover-btn" + (enabled ? " on" : "")}
                        disabled={!avail.ok} onClick={onTakeover}>
                  {enabled ? <i className="dot" /> : null}
                  {takeoverLabel(takeover)}
                </button>
              </span>
            );
          })()}

          <span className="grow" />
          {!connected || !running ? <button className="start" onClick={onStart}>{t("启动 Agent")}</button> : null}
          <button className="send" disabled={!draft.trim() && !chips.length} onClick={send}>
            <Send size={16} />
          </button>
        </div>
      </div>
    </div>
  );
}
