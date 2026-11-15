// 用户消息里附件的界面视图 —— 从消息正文里解析出附件，再把正文还原。
//
// ## 为什么是"解析"，不是"另存一个字段"
//
// 那两段文字本来就是库里的那一份（模型/检索/跨轮取回都靠它）：
//
//   · 图片：`core/image_input.content_text()` 把块列表落成一句指针
//     `[图片 1 —— 原图在 data/attachments/<16位hex>.png]`
//     —— 路径就是产物的 id（内容寻址，同一个目录）。
//   · 文件：`Composer.attachmentPreamble()` 拼的
//     `[附件N] 名字（大小字节）：<路径>` + 紧跟一行两空格的说明。
//   · 内联文本（粘贴的长文本 / 拿不到路径的文件）：同一行前言之后跟一对
//     `[附件正文#id 开始]` / `[附件正文#id 结束]`，正文夹在中间 ——
//     模型看的也是这一段。
//
// 所以界面只需要一个只读的解析器：不新增存储、不改协议、历史消息立刻能画。
//   多存一份结构化字段就是第二套真相源（改一处、另一处漂）。
//
// 只认我们自己生成的那几种形状（与 `user-text.js` 同一条取舍）：
//   用户正文里如果自己写了 `[附件]` 开头的行，会被误剥 —— 可接受。
//
// 纯函数、不 import React：node 直接跑断言（`packages/ui/tests/attachments.test.mjs`）。

import { t } from "./i18n.js";

/** 产物 id 的合法形状 —— 与 `core/artifacts.py::_ID_RE` 同一条（图片那几种）。 */
const IMAGE_ID = /^[0-9a-f]{16}\.(png|jpg|jpeg|webp|gif)$/;

/** `data/attachments/<id>.png` 或 Windows 反斜杠写法，只取文件名。 */
function basename(path) {
  const parts = String(path || "").split(/[\\/]/);
  return parts[parts.length - 1] || "";
}

/**
 * 把一条用户消息的正文拆成 `{text, attachments}`。
 *
 * 返回的 `attachments` 保持出现顺序（图片与文件混排时也按原文顺序），
 * 每一项是下面三种之一：
 *   · `{kind:"image", id, name?}` —— `id` 可直接喂 `client.artifactUrl(id)`
 *   · `{kind:"file", name, size?, path?}` —— 只用于显示（图标 + 名字）
 *   · `{kind:"text", name, size?, text}` —— 正文就在消息里（可点开看全文）
 *
 * 认不出路径的图片（`[图片 N]` 没有路径），不冒充图片，降级成一个文件条目，
 * 名字写"图片 N"：宁可少一张缩略图，也不显示一张取不回来的图
 * （裂图看起来像"图丢了"，而它其实从来没落过盘）。
 */
export function parseAttachments(content) {
  const lines = String(content || "").split("\n");
  const attachments = [];
  const rest = [];

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];

    // ① 图片指针（`core/image_input.content_text`）
    let m = line.match(/^\[图片\s*(\d+)\s*——\s*原图在\s*(.+?)\]$/);
    if (m) {
      const id = basename(m[2]);
      if (IMAGE_ID.test(id)) attachments.push({ kind: "image", id, name: "" });
      else attachments.push({ kind: "file", name: t("图片 {n}", { n: m[1] }), path: m[2] });
      continue;
    }

    // ①b 没有路径的图片指针（`[图片 N]`，`content_text` 反推不出路径时的形状）
    //     它取不回来 —— 所以按文件条目显示（名字写"图片 N"），
    //     绝不画一张会裂的缩略图。
    m = line.match(/^\[图片\s*(\d+)\]$/);
    if (m) {
      attachments.push({ kind: "file", name: t("图片 {n}", { n: m[1] }) });
      continue;
    }

    // ② 音频指针（同一条 `content_text`；界面上按文件处理 —— 不在这一批里播放）
    m = line.match(/^\[音频\s*(\d+)\s*——\s*原文件在\s*(.+?)\]$/);
    if (m) {
      attachments.push({ kind: "file", name: basename(m[2]) || t("音频 {n}", { n: m[1] }),
                         path: m[2] });
      continue;
    }

    // ③ 文件附件（`Composer.attachmentPreamble`）+ 紧跟的两空格说明行
    //
    // 方括号里的编号是可选的：更早产出的消息没有编号（`[附件]`），
    //   库里还有，两种都要认。
    m = line.match(/^\[附件(\d*)\]\s*(.+?)（(\d+)\s*(字节|字)）：(.+)$/);
    if (m) {
      const [, , name, size, , tail] = m;
      if (tail.startsWith("内容")) {
        // 内联文本：正文夹在一对带随机 id 的边界行里（正文里可能本来就有
        //   方括号行，固定边界会被它提前关掉）。
        //   更早产出的那一类没有正文 —— 如实记成空串，不冒充有内容。
        let body = "";
        const open = lines[i + 1] && lines[i + 1].trim().match(/^\[附件正文#(\S+) 开始\]$/);
        if (open) {
          const end = "[附件正文#" + open[1] + " 结束]";
          const buf = [];
          i += 2;
          for (; i < lines.length; i += 1) {
            if (lines[i].trim() === end) break;
            buf.push(lines[i]);
          }
          body = buf.join("\n");
        }
        attachments.push({ kind: "text", name, size: Number(size), text: body });
        continue;
      }
      attachments.push({ kind: "file", name, size: Number(size), path: tail });
      while (i + 1 < lines.length && /^ {2}\S/.test(lines[i + 1])) i += 1;
      continue;
    }

    rest.push(line);
  }

  const text = rest.join("\n").trim();
  return { text, attachments };
}

/** 正文里还有没有这两个标记（`user-text.js` 的快速判定用）。 */
export function hasAttachmentMarks(content) {
  const s = String(content || "");
  return /^\[附件\]/m.test(s) || /^\[图片\s*\d+/m.test(s) || /^\[音频\s*\d+/m.test(s);
}
