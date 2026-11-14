// 附件前言 —— 拼给模型看的那一段，随用户消息一起发出去。
//
// 纯函数、不 import React：node 直接跑断言
// （`packages/ui/tests/attachment-wire.test.mjs`），
// 与 `attachments.js::parseAttachments` 做往返：拼出来的东西要能逐字拆回来。
//
// 三种附件三种写法：
//   · 路径：告诉模型去哪个文件读；
//   · 内联文本：正文夹在一对带随机 id 的边界行里 —— 请求体只有 `text` 与
//     `images` 两个字段，正文不拼进 `text` 就丢了（前言里那句"内容已随本消息
//     给出"会变成一句空话）；
//   · 空正文：如实说读不到，不留一句会被当成"有内容"的承诺。
//
// 编号（`[附件N]`）与正文里的短标记（`【文本N】`）同序，见 `attachment-marks.js`。

/** 把一批非图片附件拼成前言。没有附件时返回空串。 */
export function attachmentPreamble(list) {
  const items = Array.isArray(list) ? list : [];
  if (!items.length) return "";
  const lines = items.map((f, i) => {
    const n = i + 1;
    if (f.kind === "path") {
      return `[附件${n}] ${f.name}（${f.size} 字节）：${f.path}\n`
        + "  这个文件以路径形式给你 —— 需要内容时用你的文件工具（read_file / glob / grep）去读。";
    }
    const body = String(f.text || "");
    //: 字数取正文本身的长度，不取 `f.size` —— 那个数在"拿不到路径、退回内联"
    //:   那一支里是字节数（`File.size`），写成"N 字"就是在报一个假的量。
    if (!body) {
      return `[附件${n}] ${f.name}（0 字）：内容为空（没有读到正文，需要时向用户索要）。`;
    }
    //: 边界带随机 id：贴进来的原文里可能本来就有方括号行，固定边界会被它提前关掉。
    const tag = "a" + Math.random().toString(36).slice(2, 10);
    return `[附件${n}] ${f.name}（${body.length} 字）：内容如下（未以路径形式提供）。\n`
      + `[附件正文#${tag} 开始]\n${body}\n[附件正文#${tag} 结束]`;
  });
  return lines.join("\n") + "\n\n";
}

/** 前言 + 用户自己的话。 */
export function payloadText(body, list) {
  const pre = attachmentPreamble(list);
  return pre ? pre + body : body;
}
