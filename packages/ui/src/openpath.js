// 「这个链接指向本机的一个路径，还是一个网址」—— 一行判据，纯函数。
//
// 本地路径链接要让用户直接打开文件夹。
//
// 载体是回复里的 Markdown 链接：`[工作区 → 项目一 → 作业.docx](真实路径)`。
//   显示文字保持模型爱写的那种人话（它写的「工作区 → …」纯靠正则认不出来），
//   而 href 里那个串是它自己给的 —— 那是声明，不是我们猜的。
//
// 分工：本文件只回答"该走哪扇门"；真的开门在服务端（`POST /api/reveal`，
//   那里做存在性检查 + 只定位不执行）。前端不许自己碰本机文件系统。
//
// 判据写成"不认识的一律当网址"还是"不认识的一律当路径"？取后者更危险
//   （把 `javascript:` 之类塞进本机路径解析），所以这里只把明确的"是网址"那几种排掉，
//   其余当路径 —— 而路径那扇门在服务端是只定位、不执行的，代价有限。
//   两个方向的代价不对称（检测可以错、打码不能错）。

/** 明确是"网址"的那几种 scheme。其余（含 `C:\…`、`workspace/…`、`./a.docx`）当本机路径。 */
const URL_SCHEME = /^(https?|mailto|tel|data|ftp|file):/i;

/** 这个 href 该走"网址"那扇门吗？（`false`，当本机路径，走 `/api/reveal`） */
export function isWebUrl(href) {
  const h = String(href == null ? "" : href).trim();
  if (!h) return false;                    // 空 href：`markdown.js` 已经不当链接了
  if (h.startsWith("//")) return true;     // 协议相对，是网址
  if (h.startsWith("#")) return false;     // 页内锚点：今天没有锚点，当路径（会被服务端如实拒）
  return URL_SCHEME.test(h);
}

// ── 打开本机路径的那扇门由壳注册（本包不认识 `client`）──────────────
//
// 为什么用"注册一个函数"而不是把 `client` 一路 prop 传下来：
//   `Markdown` 在气泡里，而气泡在 `messages.map` 里 —— 一路传要穿透三四层，
//   每层都得知道"路径"这件事，而它们都不关心。
// 没注册时什么也不做（不抛）：那是"这个壳不支持打开本机路径"的正常状态
//   （浏览器里的 WebUI 就是它），而不是错误。
let _opener = null;

/** 壳启动时调一次（`App.jsx`）—— 传进来的函数负责真的去开后端那扇门。 */
export function setPathOpener(fn) {
  _opener = typeof fn === "function" ? fn : null;
}

/** 点了一个本地路径链接。返回 Promise（失败由壳那边报给用户）。 */
export function openLocalPath(href) {
  if (!_opener) return Promise.resolve(false);
  try {
    return Promise.resolve(_opener(String(href == null ? "" : href).trim()));
  } catch (e) {
    return Promise.reject(e);
  }
}
