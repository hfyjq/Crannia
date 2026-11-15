// 更新公告的解析与挑选 —— 纯函数，node 直接断言。
//
// 正文是发版时手写的一份 txt（`docs/updates/<id>.txt`），形状照仓库根那份
// `更新公告.txt`：
//
//     更新公告-常规更新（2026/11/X）
//
//     UI：
//     1、修正了一个导致兼容性弹窗显示异常的问题
//
// 解析只认三样东西：第一行是标题 · 以冒号结尾的行是小节头 ·
// `1、` 开头的行是条目。其余一律并进上一条（认不出的字不丢）。
//
// 为什么用文件名当 id、不用 `core/version.py`：那个常量两套版本号还没收敛，
// 每次发版它不一定变；而公告文件是发版时必然新加一份的。
// 判据是"换了一份公告"就弹一次，不是"版本号涨了"弹一次。

/** 把一份公告拆成 `{title, sections:[{head, items:[]}]}`。空文本给空标题。 */
export function parseNotice(text) {
  const lines = String(text || "").split(/\r?\n/);
  let title = "";
  const sections = [];
  let cur = null;

  const ensure = () => {
    if (!cur) {
      cur = { head: "", items: [] };
      sections.push(cur);
    }
    return cur;
  };

  for (const raw of lines) {
    const line = String(raw || "").trim();
    if (!line) continue;
    if (!title) { title = line; continue; }

    const item = line.match(/^\d+\s*[、.．)）]\s*(.+)$/);
    if (item) { ensure().items.push(item[1]); continue; }

    if (/[：:]$/.test(line)) {
      cur = { head: line.replace(/[：:]\s*$/, ""), items: [] };
      sections.push(cur);
      continue;
    }

    //: 认不出的行并进上一条 —— 宁可多一句，不要少一句。
    const sec = ensure();
    if (sec.items.length) {
      sec.items[sec.items.length - 1] += " " + line;
    } else {
      sec.items.push(line);
    }
  }
  return { title, sections };
}

/** 公告 id 的大小：`v1.0.10` 这类按数字比，别的按字符串比。
 *
 *  按字符串排会错：`"v1.0.9" > "v1.0.10"`（按字符串逐位比，'9' > '1'），
 *  而 9 比 10 小。发版号跨过一位数之后这个坑才会咬人。
 */
export function compareNoticeId(a, b) {
  const pa = String(a || "").match(/\d+/g) || [];
  const pb = String(b || "").match(/\d+/g) || [];
  const n = Math.max(pa.length, pb.length);
  for (let i = 0; i < n; i += 1) {
    const x = Number(pa[i] === undefined ? -1 : pa[i]);
    const y = Number(pb[i] === undefined ? -1 : pb[i]);
    if (x !== y) return x - y;
  }
  return String(a || "").localeCompare(String(b || ""));
}

/** 这个版本是不是"测试版"（`1.0.0-beta.1` / `-rc` / `-dev`）。
 *
 *  判据：测试版不发更新公告（用户 2026-11-13 定）—— 公告是给正式版用户
 *  看"这次更新了什么"的，beta 本来就不做发布通知。
 *  只认这四个后缀（与 `core/updater.py` 认的预发布标记同一族），
 *  认不出的一律当正式版 —— 宁可多弹一次，不要正式版漏弹。
 */
export function isPrerelease(version) {
  return /-(alpha|beta|rc|dev)/i.test(String(version || ""));
}

/** 最新的一份公告（`list` 是 `[{id, text}]`）。没有就 `null`。 */
export function latestNotice(list) {
  const items = Array.isArray(list) ? list.filter((x) => x && x.id) : [];
  if (!items.length) return null;
  return items.slice().sort((a, b) => compareNoticeId(a.id, b.id)).pop();
}

/** 这次该弹的那一份：最新的那份没看过才给，否则 `null`。
 *
 *  判据是 id，不是"版本号变了" —— 同名公告改内容不会重复弹（那正是想要的：
 *  用户看到的已经是最新那份）。
 */
export function pickUnseen(list, seenId) {
  const latest = latestNotice(list);
  if (!latest) return null;
  return String(latest.id) === String(seenId || "") ? null : latest;
}
