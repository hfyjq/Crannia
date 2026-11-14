// 用户消息的界面视图（只要正文那一段）。
//
// 库里那条消息带两段框架话术（都不是用户说的话）：
//
//     [附件] 名字（大小字节）：D:/.../文件.pdf      ← `Composer.attachmentPreamble`
//       这个文件以路径形式给你 —— 需要内容时用你的文件工具去读。
//     [图片 1 —— 原图在 data/attachments/xxxx.png]  ← `core/image_input.content_text`
//
// 它们是给模型看的（跨轮取回、检索都靠它们），界面上只显示用户真正打的那句话。
//
// 解析只有一处：`attachments.js::parseAttachments`（它同时把附件取出来
//   给气泡下面那一排用）。这里只留"正文 + 空消息的兜底文案"这一层薄壳。
//   历史上这里自己写过一条正则 —— 两处正则迟早对不上（漏一种形状 = 那句话
//   原样漏到气泡里，看起来像"模型在自言自语"）。
import { t } from "./i18n.js";
import { parseAttachments } from "./attachments.js";

export function userTextForDisplay(content) {
  const { text } = parseAttachments(content);
  return text || t("（附件）");
}
