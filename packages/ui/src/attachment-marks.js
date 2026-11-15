// 附件在正文里的短标记 —— 生成、重编号、发送前按标记过滤。
//
// 纯函数、不 import React：node 直接跑断言
// （`packages/ui/tests/attachment-marks.test.mjs`）。
//
// 形状：`【文本1】` / `【图片1】` / `【文件1】`，粘贴那一刻插在光标处。
//   用户和模型都看得见 —— 模型由此知道附件对应正文里的哪一段。
//   编号与附件清单、与前言里那行 `[附件N]` 是同一套：图片一栏、非图片一栏
//   各自从 1 起（图片那栏必须跟着后端：它按数组位置发 `[图片 N]`）。

const MARK_KINDS = { image: "图片", text: "文本", path: "文件" };

/** 第 *n* 个某类附件的标记。 */
export function attachMark(kind, n) {
  return "【" + (MARK_KINDS[kind] || "文件") + n + "】";
}

/** 把正文里的标记重新编成与附件清单一致。
 *
 *  删掉中间一个之后，后面的必须跟着往前挪：否则正文里写着 `【文本2】`，
 *  而前言里只剩 `[附件1]`，两边对不上。
 *
 *  两趟替换：旧标记先换成占位符，占位符再换成新标记。一趟替换会撞车 ——
 *  `【文本1】→【文本2】` 写下去之后，下一条 `【文本2】→【文本3】`
 *  会把刚写的那一个也改掉。
 *
 *  标记不在正文里的（用户自己删掉了）照样更新到新编号：它仍然绑着一个
 *  不存在的标记，发送时会被拦下并出声（见 `splitByMark`）。
 *
 *  返回新的 `{text, files, images}`，不改入参（React 状态要能逐次比较）。
 */
export function renumberMarks(textDraft, fileList, imageList) {
  let s = String(textDraft || "");
  const nextFiles = fileList.slice();
  const nextImages = imageList.slice();
  const swaps = [];
  fileList.forEach((f, i) => {
    if (!f.mark) return;
    const want = attachMark(f.kind, i + 1);
    if (want === f.mark) return;
    const hole = "\u0000m" + i + "\u0000";
    if (s.indexOf(f.mark) >= 0) { s = s.split(f.mark).join(hole); swaps.push([hole, want]); }
    nextFiles[i] = Object.assign({}, f, { mark: want });
  });
  imageList.forEach((im, i) => {
    if (!im.mark) return;
    const want = attachMark("image", i + 1);
    if (want === im.mark) return;
    const hole = "\u0000m" + (1000 + i) + "\u0000";
    if (s.indexOf(im.mark) >= 0) { s = s.split(im.mark).join(hole); swaps.push([hole, want]); }
    nextImages[i] = Object.assign({}, im, { mark: want });
  });
  swaps.forEach(([hole, want]) => { s = s.split(hole).join(want); });
  return { text: s, files: nextFiles, images: nextImages };
}

/** 这一条真正要发出去的附件：标记已经从正文里删掉的，不发。
 *
 *  判据是「它绑的那枚标记还在不在最终那段正文里」。删掉的那几个要
 *  说出来（由调用方渲染成一行提示）：静默少发的话，模型读不到，
 *  用户也不知道，事后只能从回执猜。
 *
 *  没有标记的（`+` 按钮 / 拖拽进来的）一律照发。
 */
export function splitByMark(textDraft, imageList, fileList) {
  const alive = (a) => !a.mark || textDraft.indexOf(a.mark) >= 0;
  return {
    images: imageList.filter(alive),
    files: fileList.filter(alive),
    dropped: imageList.concat(fileList).filter((a) => a.mark && !alive(a)),
  };
}
