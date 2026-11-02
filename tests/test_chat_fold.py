"""前端「事件折叠器」的行为测试 —— 用假 DOM 把 `chat.html` 的 JS 真跑一遍。

为什么值得这么做（而不是 grep 源码）
------------------------------------
`web/templates/chat.html` 里那套折叠逻辑一行 Python 都碰不到：
`foldEvent()` 把 SSE 事件折成活动行 + 气泡，而它是唯一决定"用户看到什么顺序"
的地方。人工肉眼看不住这一层：`/api/stop` 的会话键写错时，点了按钮那一轮
照跑满 30 轮、接口还回 `{"ok":true}`，而界面看起来一切正常。

grep 源码只能证明"那行还在"，证明不了"顺序对" —— 而这一次要钉的恰恰是顺序：
`say` 气泡出现时，上面的步骤段要收口、后面的步骤要挂到气泡下面。

做法：把 `<script>` 抽出来 + 一个够用的假 DOM，`eval` 进同一个作用域，
然后直接调 `foldEvent()` 断言 NODES 的顺序与各节点的状态。
不引入浏览器，也不改生产代码的一个字符。

[!] 没有 node 时会大声跳过（不算通过）—— 与 `test_settings_template` 同规矩：
一条会自己静默跳过的护栏等于没有护栏。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_chat_fold.py
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
CHAT_HTML = _PROJECT_ROOT / "web" / "templates" / "chat.html"

# ASCII-only 输出（Windows 控制台默认 GBK）。

# ------------------------------------------------------------------
# 假 DOM —— 只实现 chat.html 真正用到的那几个 API
# ------------------------------------------------------------------

_STUBS = r"""
class El {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.parent = null;
    this.className = ''; this.style = {}; this.attrs = {}; this._text = '';
    this.scrollTop = 0; this.scrollHeight = 0; this.disabled = false;
  }
  get textContent() { return this._text; }
  set textContent(v) {                    // 真 DOM 赋 textContent 会清空子节点
    this._text = String(v == null ? '' : v);
    this.children.forEach(c => { c.parent = null; });
    this.children = [];
  }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  append() { Array.prototype.forEach.call(arguments, c => this.appendChild(c)); }
  remove() {
    this._removed = true;
    if (this.parent) {
      const i = this.parent.children.indexOf(this);
      if (i >= 0) this.parent.children.splice(i, 1);
    }
    this.parent = null;
  }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  focus() {}
  get classList() {
    const self = this;
    return {
      add(c) { self.className = (self.className + ' ' + c).trim(); },
      remove(c) {
        self.className = self.className.split(/\s+/)
          .filter(x => x && x !== c).join(' ');
      },
      contains(c) { return self.className.split(/\s+/).indexOf(c) >= 0; },
    };
  }
  get innerHTML() { return ''; }
  set innerHTML(v) {}                      // 生产里全是空串赋值
  get value() { return this._value || ''; }
  set value(v) { this._value = v; }
}

const _byId = {};
function _mkId(id) { return (_byId[id] = _byId[id] || new El('div')); }

globalThis.document = {
  createElement: t => new El(t),
  createTextNode: t => ({ nodeValue: t, textContent: t }),
  getElementById: id => _mkId(id),
  addEventListener: () => {},
};
globalThis.window = { addEventListener: () => {} };
// ★ **回调要留下来** —— 本地计时器（那个 `~` 跳秒）是在**脚本加载时**
//   注册的，`() => 0` 会让它永远不跑 -> 无法验证。
globalThis._intervals = [];
globalThis.setInterval = (fn, ms) => { globalThis._intervals.push({ fn, ms }); return 0; };
globalThis.setTimeout = () => 0;
globalThis.fetch = () => new Promise(() => {});   // 永不落地：顶层 refreshStatus() 静默挂着
globalThis.EventSource = class { constructor() {} close() {} addEventListener() {} };
globalThis.alert = () => {};
const _store = {};
globalThis.sessionStorage = {
  getItem: k => (k in _store ? _store[k] : null),
  setItem: (k, v) => { _store[k] = String(v); },
  removeItem: k => { delete _store[k]; },
};
globalThis.localStorage = globalThis.sessionStorage;
globalThis.location = { search: '' };

// [!] `api()` 的**真货在 `web/templates/base.html`**（本夹具只抽 `chat.html` 的
//    script 块）。这里放一个**最小同形**替身：拼 token -> `fetch` -> 非 ok 抛 ->
//    `res.json()`。★ 它是"够用"不是"逐字" —— 所以 Python 侧另有一条断言钉住
//    **真 `api` 还在 base.html 里**（否则这条替身会在生产坏掉时继续绿）。
async function api(url, opts) {
  const _tk = (typeof TOKEN === 'undefined' ? '' : TOKEN);
  const full = url + (url.indexOf('?') >= 0 ? '&' : '?') + 'token=' + _tk;
  const res = await fetch(full, opts || {});
  if (!res.ok) {
    let msg = res.status + ' ' + res.statusText;
    try { const body = await res.json(); msg = body.error || msg; } catch (e) { /* 非 JSON */ }
    throw new Error(msg);
  }
  return res.json();
}
"""

# ------------------------------------------------------------------
# 断言 —— 直接对 NODES 的顺序与节点状态提问
# ------------------------------------------------------------------

_CHECKS = r"""
const _fails = [];
let _scene = '(setup)';
let _scenes = 0;
let _asserts = 0;
function ok(cond, msg) { _asserts += 1; if (!cond) _fails.push('[' + _scene + '] ' + msg); }
function next(name) { _scene = name; _scenes += 1; }
function ev(type, data) { return { type: type, ts: Date.now() / 1000, data: data || {} }; }

/** 调一个**可能不存在**的处理器，不存在就什么都不做。
 *
 *  [!] 为什么不直接 `x.onclick()`：缺处理器时会**抛 TypeError 打断整个脚本**，
 *      报告变成 `(无 stdout)` + 一句 node 栈 —— 而按关键词匹配的负对照脚本
 *      连"红没红"都判不出来。本文件在这上面**栽过三次**（场景 17 两次、场景 18 一次）。
 *      -> 直调"可能不存在"的处理器之前，一律走这里。
 */
function callIfFn(fn, recv) {
  var extra = Array.prototype.slice.call(arguments, 2);
  return (typeof fn === 'function') ? fn.apply(recv, extra) : undefined;
}
// [!] `recv` 不是可选的礼貌参数：处理器里用了 `this`（`_approvalRowClick` 读
//     `this._key`）。写成 `fn()` 会让 `this` 变成 undefined，于是
//     "点第 3 行"静默失效，而断言报的是"实际 1"。**简化掉的是语义，不是噪音。**

// [!] 下面 5 个场景（21+5 条断言）描述的是 **stream 渲染器**的行为 —— 无限堆叠、
// `say` 切段并推气泡。默认值已经是 `box`，所以这里**必须显式钉住**：
// 不钉的话它们会因为换渲染器而集体变红，而那是**预期的**，不是回归。
// （反过来说：这几条逐字重跑，正是"stream 路径一行没删"的最强证据 —— 比 grep 强。）

// ── 场景 0：渲染器开关 —— 只有字面量 `stream` 是退出 ──
// 直接调 `_readRendererMode()` 而不是读加载时的常量：这样它不依赖进程启动时的
// `location`，拼错兜底那条才真的可测。
next('场景 0 · 渲染器开关');
var _savedSearch = location.search;
location.search = '';
ok(_readRendererMode() === 'box', '无参数时必须是 box');
location.search = '?renderer=stream';
ok(_readRendererMode() === 'stream', '?renderer=stream 该切到 stream');
location.search = '?renderer=boox';
ok(_readRendererMode() === 'box',
   '**拼错的参数必须落回 box** —— 不能静默退回正要退休的那个渲染器');
location.search = '?token=abc&renderer=stream';
ok(_readRendererMode() === 'stream', '参数排在后面也该认出来');
location.search = _savedSearch;

ACTIVITY_RENDERER = 'stream';

// ── 场景 1：say 把步骤流**切段** ──
next('场景 1 · say 把步骤流切段');
foldEvent(ev('task.start'));
foldEvent(ev('thinking', { phase: 'start' }));
foldEvent(ev('tool.start', { call_id: 'c1', name: 'x', activity: '正在查看工作区' }));
foldEvent(ev('tool.end',   { call_id: 'c1', ok: true, duration_ms: 800 }));

var seg1 = ACTIVITY;
ok(seg1 && !seg1.closed, 'setup: 第一段应当是打开的');
ok(seg1.steps.length === 1, 'setup: 第一段该有 1 步，实际 ' + seg1.steps.length);

foldEvent(ev('say', { text: '我先看看工作区。' }));

ok(seg1.closed === true, 'say 之后第一段必须收口（否则上面的转轮一直转）');
ok(seg1.stopEl.parent === null, 'say 之后第一段的「打断」按钮必须退场');
ok(seg1.labelEl.textContent === '共 1 步',
   '段摘要应当是「共 1 步」，实际 ' + JSON.stringify(seg1.labelEl.textContent));
ok(seg1.labelEl.className === 'activity-label seg',
   '段标签 class 不对：' + seg1.labelEl.className);
ok(ACTIVITY === null, 'say 之后当前段必须置空（下一段等下一个事件再开）');

var iSeg1 = NODES.indexOf(seg1);
var bubble = NODES[NODES.length - 1];
ok(bubble && bubble.kind === 'msg', '最后挂上的应当是那句话的气泡');
ok(bubble.textEl.textContent === '我先看看工作区。',
   '气泡内容不对：' + JSON.stringify(bubble.textEl.textContent));
ok(NODES.indexOf(bubble) > iSeg1, '气泡必须在第一段**之后**');

// ── 场景 2：下一个工具开**新的一段**，挂在气泡下面 ──
next('场景 2 · 下一个工具开新的一段');
foldEvent(ev('tool.start', { call_id: 'c2', name: 'y', activity: '正在新建文档' }));
var seg2 = ACTIVITY;
ok(seg2 && seg2 !== seg1, 'must open a NEW segment, not reuse the first one');
ok(NODES.indexOf(seg2) > NODES.indexOf(bubble), '新的一段必须在气泡**之后**');
ok(seg1.steps.length === 1,
   '第一段的步骤数不该再涨（实际 ' + seg1.steps.length + '）—— 步骤回流了');

// ── 场景 3：打断 -> **推气泡**，不是改标题 ──
next('场景 3 · 打断：只收口 + 表头状态词（本地不再推那句话）');
// [!] 用**生产函数**置位，不手写一行赋值 —— 同场景 5 的规矩。
//     而且这个标志**带轮次身份**，手写 `= true` 已经不是合法形状。
markStopRequested(seg2);
foldEvent(ev('task.end', { ok: true, duration_ms: 3600, tokens: 36332, response: '' }));
ok(seg2.closed === true, 'task.end 应当收尾当前段');
ok(seg2.labelEl.textContent === '已中断',
   '收尾后标题应当是「已中断」，实际 ' + JSON.stringify(seg2.labelEl.textContent));
// ★★ 本地**不许**再推那口气泡 ——
//   那句话的产出点只能有一个，而正确的是**服务端**（`CANCEL_REPLY` 走
//   `/api/chat` 的响应）。两处都说 = 用户看到两次「已打断」。
var _w160 = NODES.map(function (n) { return n.textEl ? n.textEl.textContent : ''; }).join('|');
ok(_w160.indexOf('已打断') < 0,
   '本地区不该出现那句话（该由服务端说）：' + JSON.stringify(_w160));
ok(NODES[NODES.length - 1] === seg2,
   '活动行就是最后一个节点（没有本地气泡）');
ok(_stopRequested === 0, '标志应当被清掉（否则下一轮会被误判成又中断了）');

// ── 场景 3b：`task.end` **之后**落地的打断，不许跨到下一轮 ──
//
// 裸布尔的病：`stopTurn` 要 `await api('/api/stop')` **之后**才置位，而
// `task.end` 完全可能在这一段往返里就落地了 -> 标志一直留着，把**下一轮**
// 平白标成「已中断」。长框下它只是"下一轮的框折叠成那行问句"，更安静，
// 因此更不容易被发现（那是长框改了**症状形状**，不是长框引入的）。
next('场景 3b · 打断标志不许跨轮（W7b）');
NODES.length = 0; chatEl.textContent = ''; ACTIVITY = null; _stopRequested = 0;

// 第一轮：跑完，**没人按打断**
foldEvent(ev('task.start'));
foldEvent(ev('tool.start', { call_id: 'w1', name: 'w', activity: '正在读取 a.txt' }));
foldEvent(ev('task.end', { ok: true, duration_ms: 100, tokens: 10, response: '' }));
ok(NODES[NODES.length - 1].kind !== 'msg', 'setup: 没按打断就不该有气泡');

// ★ 用户**在这一轮结束之后**才落地了打断请求 —— `markStopRequested` 就是
//   `stopTurn` 在 `await api('/api/stop')` 之后调的那一步。
markStopRequested(null);

// 第二轮：它**不是**被打断的那一轮
foldEvent(ev('task.start'));
var w7box = ACTIVITY;
foldEvent(ev('tool.start', { call_id: 'w2', name: 'w', activity: '正在读取 b.txt' }));
foldEvent(ev('task.end', { ok: true, duration_ms: 100, tokens: 10, response: '' }));
var w7all = NODES.map(function (n) { return n.textEl ? n.textEl.textContent : ''; }).join('|');
ok(w7all.indexOf('已打断') < 0,
   '下一轮不许出现「已打断」气泡（修 `W7b` 之前这里必红）：' + JSON.stringify(w7all));
ok(w7box.labelEl.textContent !== '已中断',
   '下一轮的活动行也不许被写成「已中断」，实际 ' + JSON.stringify(w7box.labelEl.textContent));
ok(_stopRequested === 0, '标志不许留到下一轮');

// ── 场景 4：**没有 say 就不分段**（防倒退）──
next('场景 4 · 没有 say 就不分段');
NODES.length = 0;
ACTIVITY = null;
foldEvent(ev('task.start'));
foldEvent(ev('thinking', { phase: 'start' }));   // 真实序列里有它：它把 task.start 的
                                                 // 「正在处理…」换掉，后者才不会被落成一步
foldEvent(ev('tool.start', { call_id: 'd1', name: 'z',  activity: '正在读取 a.txt' }));
foldEvent(ev('tool.end',   { call_id: 'd1', ok: true, duration_ms: 10 }));
foldEvent(ev('tool.start', { call_id: 'd2', name: 'z2', activity: '正在读取 b.txt' }));
foldEvent(ev('tool.end',   { call_id: 'd2', ok: true, duration_ms: 10 }));
ok(NODES.length === 1, '没有 say 时应当只有**一条**活动行，实际 ' + NODES.length);
ok(ACTIVITY.steps.length === 2,
   '两步都该在同一条里，实际 ' + ACTIVITY.steps.length);

// ── 场景 5：中断的状态回执**不许**落成一步 ──
// 症状（用户导出的对话记录）：
//     [ok] 正在读取 投资策略.pdf   1.2s
//     [ok] 已请求中断，等这一步跑完…  1.3s   ← 一条**状态回执**变成了"已完成步骤"
// 根因：closeActivity 用 force=true 调 sealHead，而 force 恰好绕过 think 的跳过规则。
//
// [!] 这里必须调 **markStopRequested()**（生产函数），不能手写一行 setHead 复现它 ——
//    手写的那份会**连出错的参数一起复现**（就是漏掉的第 4 个参数），
//    于是"参数又被删了"时这条护栏依然绿。这正是这条 bug 一直没被发现的原因：
//    stopTurn 是 async 而夹具里 fetch 永不落地，护栏根本执行不到出问题那一行。
next('场景 5 · 中断的状态回执不许落成一步（W6e）');
NODES.length = 0;
ACTIVITY = null;
_stopRequested = 0;
foldEvent(ev('task.start'));
foldEvent(ev('thinking', { phase: 'start' }));
foldEvent(ev('tool.start', { call_id: 'e1', name: 'w', activity: '正在读取 投资策略.pdf' }));
var wnode = ACTIVITY;
foldEvent(ev('tool.end', { call_id: 'e1', ok: true, duration_ms: 1200 }));

markStopRequested(wnode);          // ← 生产调用本身
ok(wnode._headKind === 'status',
   '中断回执的 kind 必须是 status（漏了第 4 个参数 sealHead 就挡不住它），实际 ' +
   JSON.stringify(wnode._headKind));

foldEvent(ev('task.end', { ok: true, duration_ms: 17200, tokens: 162200, response: '' }));
var wtexts = wnode.steps.map(function (s) { return s.text; });
ok(wtexts.indexOf('已请求中断，等这一步跑完…') < 0,
   '状态回执不许出现在步骤列表里（W6e），实际：' + JSON.stringify(wtexts));
ok(wnode.steps.length === 1,
   '这一轮只该有 1 步（那条工具），实际 ' + wnode.steps.length + '：' + JSON.stringify(wtexts));
ok(wnode.closed === true, 'task.end 之后这条活动行应当收尾');
ok(_stopRequested === 0, '标志应当被 task.end 清掉');

// ══════════════════════════════════════════════════════════════════
// 套件 B：**box 渲染器**（有界可折叠长框）
// ══════════════════════════════════════════════════════════════════
// 为什么用"赋值切换"而不是跑第二个 node 进程：`_CHECKS` 与 `foldEvent` 在同一个
// 作用域里，而 `_CHECKS` 本来就直接赋值 `_stopRequested`。换进程要多一层环境变量、
// 而且常量一旦被改成 `const` 就断。
ACTIVITY_RENDERER = 'box';
NODES.length = 0; chatEl.textContent = ''; ACTIVITY = null; _stopRequested = 0;
ok(ACTIVITY_RENDERER === 'box',
   'box 套件必须在 box 模式下跑 —— 否则下面全是空跑还报绿');

// ── 场景 6：长框的形状 + 框内滚动 ──
next('场景 6 · 长框的形状与框内滚动');
foldEvent(ev('task.start'));
foldEvent(ev('thinking', { phase: 'start' }));
var box = ACTIVITY;
ok(NODES.length === 1, 'task.start 之后只该有一条节点，实际 ' + NODES.length);
ok(box.el.classList.contains('activity-box'), '活动行该带 activity-box 标记类');
ok(!!box.summaryEl && !!box.bodyEl, 'box 模式该有 summaryEl 与 bodyEl');
ok(box.el.classList.contains('collapsed') === false, '运行中的框不该是折叠态');
ok(box.bodyEl.children.indexOf(box.stepsEl) >= 0,
   'stepsEl 必须在 bodyEl **里面** —— 240px 上限靠这条才成立');
ok(box.bodyEl.children.indexOf(box.detailEl) >= 0,
   'detailEl 也要在滚动区里（否则长思考会把框撑高、把表头顶出视口）');

// 框内自动贴底（假 DOM 里 scrollTop / scrollHeight 是可写属性 -> 这条不是恒真）
box.bodyEl.scrollHeight = 500;
box.bodyEl.scrollTop = 0;
foldEvent(ev('tool.start', { call_id: 'b1', name: 'p', activity: '正在读取 a.docx' }));
foldEvent(ev('tool.end',   { call_id: 'b1', ok: true, duration_ms: 146 }));
ok(box.bodyEl.scrollTop === 500,
   '新步骤该把框内滚动贴到底，实际 scrollTop=' + box.bodyEl.scrollTop);

// ── 场景 6b：步骤行的状态符号 = 红/绿两个点（换掉 [ok]/[x]）──
// 为什么值得钉：要的是**四态**，查下来只有两个真有落点
// （"执行中" 只能是表头那个转轮；"等待审批" 等待审批**没有信号** —— 见 chat.html 的 TICK_OK 注释）。
// 钉住它，下次有人想加第三个符号时会先看见这条。
// [!] 断言消息里**不许回显这个符号** —— Windows 控制台是 GBK，编不出来（见文件头）。
next('场景 6b · 步骤符号只有红绿');
// [!] 必须钉**码点**，不能只比 `tick === TICK_OK` —— 那样两边一起变、**恒真**
// （把常量改回 [ok]/[x]，只比常量的那条照样绿）。失败消息里回显码点，
// 是 ASCII，不会撞 GBK。
ok(TICK_OK.codePointAt(0) === 0x1F7E2, '成功的符号该是绿圆 U+1F7E2');
ok(TICK_ERR.codePointAt(0) === 0x1F534, '失败的符号该是红圆 U+1F534');
ok(TICK_OK !== TICK_ERR, 'setup: 两个符号必须不同');
ok(box.steps[0] && box.steps[0].tickEl.textContent === TICK_OK,
   '成功的步骤该用成功符号（且不许是旧的 [ok]）');
foldEvent(ev('tool.start', { call_id: 'b2', name: 'p2', activity: '正在解析 x.pdf' }));
foldEvent(ev('tool.end', { call_id: 'b2', ok: false, duration_ms: 5, error: 'boom' }));
var lastStep = box.steps[box.steps.length - 1];
ok(lastStep && lastStep.tickEl.textContent === TICK_ERR,
   '失败的步骤该是红点（且不许是旧的叉号）');

// ── 场景 7：say 落成**框内分隔行**（不开新段、不推气泡）──
next('场景 7 · say 落成框内分隔行');
var nodesBefore = NODES.length;
foldEvent(ev('say', { text: '我先看看工作区。' }));
ok(ACTIVITY === box, 'say 之后当前框应当**不变**（不像 stream 那样置空）');
ok(box.closed === false, 'say 不该把框收口 —— 一轮只有一个框');
ok(NODES.length === nodesBefore, 'say 不该新增节点（气泡没了），实际 ' + NODES.length);
ok(box.says.length === 1 && box.says[0].text === '我先看看工作区。',
   'say 该记进 box.says，实际 ' + JSON.stringify(box.says.map(function (s) { return s.text; })));
ok(box.stepsEl.children.indexOf(box.says[0].el) >= 0, 'say 行该落在框的步骤流里');
ok(box.labelEl.textContent === '正在处理…',
   'say 之后表头该被重置成中性文案（否则一直挂着上一步，看起来像卡住），实际 ' +
   JSON.stringify(box.labelEl.textContent));

// ── 场景 8：完成后整框折叠成一行 ──
next('场景 8 · 完成后折叠成一行');
foldEvent(ev('task.end', { ok: true, duration_ms: 17200, tokens: 162200, response: '' }));
ok(box.closed === true, 'task.end 应当收尾');
ok(box.el.classList.contains('collapsed') === true, '完成后整框该折叠');
ok(box.summaryEl.textContent === '已完成 · 17.2s · 162.2K tok',
   '折叠行文案不对：' + JSON.stringify(box.summaryEl.textContent));
ok(box.spinnerEl.parent === null, '折叠后转轮该退场');
ok(box.stopEl.parent === null, '折叠后打断按钮该退场');
ok(NODES.length === 1, '整轮仍该只有**一个**节点，实际 ' + NODES.length);

// ── 场景 9：展开/收起往返，且**步骤行还在** ──
next('场景 9 · 展开收起往返');
var stepsBefore = box.stepsEl.children.length;
ok(stepsBefore >= 2, 'setup: 这一步该有至少 2 行（一条工具 + 一条 say），实际 ' + stepsBefore);
toggleActivityBox(box);
ok(box.el.classList.contains('collapsed') === false, 'toggle 一次该展开');
ok(box.stepsEl.children.length === stepsBefore,
   '展开后步骤行数必须不变（折叠是 display:none，不是删节点）—— 实际 ' +
   box.stepsEl.children.length + '，原本 ' + stepsBefore);
toggleActivityBox(box);
ok(box.el.classList.contains('collapsed') === true, 'toggle 两次该收回去');

// 运行中的框点不塌（表头右上角那个「打断」按钮的点击会冒泡到 row 上）
NODES.length = 0; chatEl.textContent = ''; ACTIVITY = null;
foldEvent(ev('task.start'));
var running = ACTIVITY;
toggleActivityBox(running);
ok(running.el.classList.contains('collapsed') === false,
   '运行中的框不该被点塌（否则打断按钮一点就顺手把框收了）');

// ── 场景 10：打断 → 折叠成那行问句 ──
next('场景 10 · 打断折叠成那个状态词');
NODES.length = 0; chatEl.textContent = ''; ACTIVITY = null; _stopRequested = 0;
foldEvent(ev('task.start'));
foldEvent(ev('tool.start', { call_id: 'c9', name: 'q', activity: '正在读取 b.pdf' }));
var ibox = ACTIVITY;
markStopRequested(ibox);
foldEvent(ev('task.end', { ok: true, duration_ms: 3600, tokens: 36332, response: '' }));
ok(ibox.el.classList.contains('collapsed') === true, '打断后该折叠');
ok(ibox.summaryEl.textContent === '已中断',
   '打断折叠行该是状态词「已中断」，实际 ' + JSON.stringify(ibox.summaryEl.textContent));
ok(ibox.summaryEl.className.indexOf('interrupted') >= 0,
   '打断折叠行该带 interrupted 类，实际 ' + ibox.summaryEl.className);
ok(NODES.length === 1, '打断**不推气泡** -> 只该有活动行这一个节点，实际 ' + NODES.length);

// ── 场景 11：失败 → 「已失败」 ──
next('场景 11 · 失败折叠行');
NODES.length = 0; chatEl.textContent = ''; ACTIVITY = null; _stopRequested = 0;
foldEvent(ev('task.start'));
foldEvent(ev('tool.start', { call_id: 'd9', name: 'r', activity: '正在解析 c.pdf' }));
var fbox = ACTIVITY;
foldEvent(ev('task.end', { ok: false, duration_ms: 5000, tokens: 12000, response: '' }));
ok(fbox.summaryEl.textContent === '已失败 · 5.0s · 12.0K tok',
   '失败折叠行不对：' + JSON.stringify(fbox.summaryEl.textContent));
ok(fbox.summaryEl.className.indexOf('error') >= 0,
   '失败折叠行该带 error 类，实际 ' + fbox.summaryEl.className);

// ── 场景 12：clearChat 之后不许留悬空的活动行 ──
next('场景 12 · clearChat 之后不留悬空的活动行');
NODES.length = 0; chatEl.textContent = ''; ACTIVITY = null; _stopRequested = 0;
foldEvent(ev('task.start'));
foldEvent(ev('tool.start', { call_id: 'z9', name: 's', activity: '正在读取 d.txt' }));
var stale = ACTIVITY;
clearChat();
// [!] 这里**不能**自己把 ACTIVITY 置空 —— 那会掩盖被测的那个 bug
ok(ACTIVITY === null, 'clearChat 必须清掉 ACTIVITY（否则下一轮写进一个脱离 DOM 的框）');
ok(_stopRequested === 0, 'clearChat 也该清掉中断标志');
var fresh = openActivity();
ok(fresh !== stale, 'clearChat 之后 openActivity() 必须给一条**新的**活动行');
ok(fresh.el.parent === chatEl, '新的活动行必须挂在 chatEl 上');

// ══════════════════════════════════════════════════════════════════
// 场景 13+ · 审批浮窗
// ══════════════════════════════════════════════════════════════════
//
// [!] 这里调的是 chat.html 里的**具名函数**（renderApproval / approvalKey /
//    approvalReply / approvalDock），不是重写一份。夹具里 `addEventListener`
//    是空操作、`fetch` 永不落地 —— 所以逻辑**必须**能被直接调用，
//    否则这层根本测不到。

function _pk(over) {
  var p = {
    request_id: 'R1', kind: 'cross_zone', ask_index: 1,
    body: '找不到 报告.docx',
    options: [
      { key: '1', label: '同意', is_escalation: false },
      { key: '2', label: '缩小范围', is_escalation: false },
      { key: '3', label: '不同意', is_escalation: false },
      { key: '4', label: '我有话要说', is_escalation: false },
    ],
    default_key: '1', expect: '', question: '',
  };
  if (over) { for (var k in over) { p[k] = over[k]; } }
  return p;
}

// ── 场景 13：渲染 + 两步确认（**单独按数字不提交**）──
next('场景 13 · 浮窗：按数字只选中，不提交');
APPROVAL_SEEN = '';
APPROVAL = null;
renderApproval(_pk());
ok(APPROVAL !== null, 'renderApproval 该把 payload 收下');
ok(APPROVAL_SEL === '1', '默认选中该是 default_key（这里是 1）');
var box = document.getElementById('approval-modal');
ok(box.style.display === 'block', '浮窗该显示出来');

// ★ 核心：按一个数字**只选中**，绝不提交。
//   提交会把 APPROVAL_SEEN 写上 —— 用它当"提交过了"的判据。
var before = APPROVAL_SEEN;
var handled = approvalKey({ key: '3' });
ok(handled === true, '按 3 该被浮窗接住');
ok(APPROVAL_SEL === '3', '按 3 该选中第 3 项');
ok(APPROVAL_SEEN === before, '**单独按数字就提交了** -- 两步确认没了');
ok(APPROVAL !== null, '还没确认，payload 不该被清掉');

// 方向键也要能选
approvalKey({ key: 'ArrowDown' });
ok(APPROVAL_SEL === '4', 'ArrowDown 该往下走一格');
approvalKey({ key: 'ArrowDown' });
ok(APPROVAL_SEL === '1', '到底之后该回到第一项（环形）');

// 没给出的数字不接受
ok(approvalSelect('9') === false, '没给出的键不该被选中');
ok(APPROVAL_SEL === '1', '被拒的键不该改掉选中态');

// ── 场景 14：停靠 —— 标签精确是「等待审批」──
next('场景 14 · 浮窗：停靠/飘回');
ok(approvalDock() === true, 'approvalDock 该切到停靠态');
ok(APPROVAL_DOCKED === true, '停靠标志没置上');
var title = document.getElementById('approval-title');
ok(title.textContent === '等待审批',
   '停靠后的标签必须**精确**是「等待审批」，实际是 ' + JSON.stringify(title.textContent));
ok(box.classList.contains('docked'), '停靠态该有 docked 类');
ok(approvalDock() === false, '再点一次该飘回来');
ok(APPROVAL_DOCKED === false, '飘回后停靠标志该清掉');
ok(title.textContent !== '等待审批', '飘回后不该还叫「等待审批」');

// ── 场景 15：中间态给输入框，不给选项 ──
next('场景 15 · 浮窗：中间态');
APPROVAL = null; APPROVAL_SEEN = '';
renderApproval(_pk({ request_id: 'R2', expect: 'text', question: '文件可能在哪儿？', options: [] }));
ok(APPROVAL.expect === 'text', '中间态该标成 text');
ok(approvalSelect('1') === false, '中间态不该能选选项');
// [!] 用 JS 留下的**直接引用**，不用 getElementById —— 假 DOM 的 `_mkId`
//    会给任何 id 造一个空壳：拿到的不是生产里那个对象，也会让真实的隐患
//    在夹具里隐形。
ok(APPROVAL_INPUT !== null, '中间态该建出一个输入框');
APPROVAL_INPUT.value = '';
ok(approvalReply() === false, '输入框空的时候不该提交');
APPROVAL_INPUT.value = 'D 盘';
ok(approvalReply() === true, '有内容时该提交');
ok(APPROVAL === null, '提交之后浮窗该收起来');

// ── 场景 16：答过的 request_id 不再渲染（无幽灵）──
next('场景 16 · 浮窗：答过的不再出现');
var answered = APPROVAL_SEEN;
ok(answered !== '', 'setup：上一次提交该记下了 request_id');
renderApproval(_pk({ request_id: answered }));
ok(APPROVAL === null, '**已经答过的 request_id 又被渲染出来了** -- 幽灵浮窗');
ok(box.style.display === 'none', '答过的浮窗该是隐藏的');

// 换了新的 request_id -> 重新出现，且不继承停靠态
renderApproval(_pk({ request_id: 'R3' }));
ok(APPROVAL !== null, '新 request_id 该重新出现');
ok(APPROVAL_DOCKED === false, '新请求不该继承上一次的停靠态');

// 服务端说没有了 -> 收起来
renderApproval(null);
ok(APPROVAL === null, 'pending 为 null 时该收起来');
ok(box.style.display === 'none', '没有待审批时浮窗必须隐藏');

// ── 场景 17：鼠标要真的点得动 ──
next('场景 17 · 浮窗：鼠标点击');
APPROVAL = null; APPROVAL_SEEN = ''; APPROVAL_DOCKED = false;
renderApproval(_pk({ request_id: 'R4' }));

// 找出选项行（APPROVAL_NODES 里 className 以 approval-option 开头的）
function _rows() {
  return APPROVAL_NODES.filter(function (n) {
    return String(n.className || '').indexOf('approval-option') === 0;
  });
}
var rows = _rows();
ok(rows.length === 4, '该有 4 个选项行，实际 ' + rows.length);

// ★ 核心：每一行都必须**挂了 onclick**。
//   若用的是 addEventListener，夹具里那是空操作，于是"选项点不动"一条
//   测试都抓不到（浮窗出来了，鼠标点选项没反应）。
for (var i = 0; i < rows.length; i++) {
  ok(typeof rows[i].onclick === 'function',
     '第 ' + (i + 1) + ' 行没挂 onclick -- 鼠标点它不会有任何反应');
}
// 点第 3 行 -> **只选中**（两步确认），不提交
APPROVAL_SEL = '1';
callIfFn(rows[2].onclick, rows[2]);
ok(APPROVAL_SEL === '3', '点第 3 行该选中 3，实际 ' + APPROVAL_SEL);
ok(APPROVAL !== null, '点一下就提交了 -- 两步确认没了');
ok(APPROVAL_SEEN === '', '点选项不该算提交');

// ── 场景 18：停靠/回飘 —— 按钮那一击不许被冒泡抵消 ──
//
// [!] 这一处连着踩了三次，测试的形状也跟着变了三次。现在钉的是**机制**：
//     假 DOM 里 `getElementById` 返回的是**合成空壳**（`_mkId`），
//     所以按钮**没有真实的父链**，没法真的走一遍冒泡。
//     -> 直接断言"按钮处理器调了 stopPropagation"，那才是承重的那一行。
next('场景 18 · 浮窗：停靠/回飘');
APPROVAL = null; APPROVAL_SEEN = ''; APPROVAL_DOCKED = false;
renderApproval(_pk({ request_id: 'R4b' }));
var card = document.getElementById('approval-card');
var dockBtn = document.getElementById('approval-dock');

ok(typeof dockBtn.onclick === 'function',
   '按钮没挂处理器 -- 必须在 JS 里挂（HTML 内联属性夹具解析不到，那条路测不到）');

// ★ 承重的一条：按钮处理器**必须 stopPropagation()**。
//   没有它，这一击会冒泡到卡片，而卡片处理器同样是 toggle -> 互相抵消
//   -> **按钮看着像坏了**。实测踩过两次（第二次是我以为"只在停靠态挂"能躲开，
//   结果漏了过渡那一击：处理器是在冒泡那一刻才读的）。
// [!] 这个名字**不能叫 `ev`** —— 本文件顶部有个 `ev(type, data)` 事件工厂，
//     而这是一个**脚本作用域**的 `var`（不是块作用域）：`var` 声明提升 ->
//     函数声明先就位、这一行执行时把它**整个盖掉**，于是**它以下**的任何场景
//     调 `ev(...)` 都是 `TypeError: ev is not a function`。
//     实测证据：它以下那三条场景（21/22/23）**全都手写事件字面量**，
//     一次 `ev()` 都没用 —— 那不是风格，是绕开了这颗雷。
var _clickEv = { stopPropagation: function () { this._stopped = true; } };
APPROVAL_DOCKED = false;
callIfFn(dockBtn.onclick, dockBtn, _clickEv);
ok(_clickEv._stopped === true,
   '按钮处理器没有 stopPropagation -- 点按钮会冒泡到卡片，两个 toggle 互相抵消');
ok(APPROVAL_DOCKED === true, '点按钮之后该处于停靠态');

// 因果演示：**不掐冒泡**的话，冒泡那一击确实会把停靠抵消掉
APPROVAL_DOCKED = false;
approvalDock();                    // 按钮那一下
callIfFn(card.onclick, card);      // 冒泡到卡片那一下
ok(APPROVAL_DOCKED === false,
   '负对照失效：两次 toggle 本该互相抵消 -- 那"按钮失效"就另有原因');

// 停靠之后：**整张卡**（含标题）都能点回来，不是只有四个字
APPROVAL_DOCKED = true; paintApproval();
ok(typeof card.onclick === 'function', '停靠后卡片处理器不见了');
callIfFn(card.onclick, card);
ok(APPROVAL_DOCKED === false, '点停靠后的卡片该飘回来');

// 标题在卡片**里面** -> 它自己不许再挂一个，否则点标题触发两次（又是净零）
var titleEl = document.getElementById('approval-title');
ok(titleEl.onclick == null,
   '标题自己挂了处理器 -- 点它会触发两次（标题一次 + 冒泡到卡片一次），又是净零');

// ── 场景 19：升级项带 escalate 类（要显眼）──
next('场景 19 · 浮窗：第 5 项要显眼');
APPROVAL = null; APPROVAL_SEEN = '';
renderApproval({
  request_id: 'R5', kind: 'cross_zone', ask_index: 3, body: 'x',
  options: [
    { key: '1', label: '同意', is_escalation: false },
    { key: '5', label: '本次对话内自动放行', is_escalation: true },
  ],
  default_key: '1', expect: '', question: '',
});
var r5 = _rows();
ok(r5.length === 2, 'setup：该有 2 行');
ok(String(r5[0].className).indexOf('escalate') < 0, '普通项不该带 escalate');
ok(String(r5[1].className).indexOf('escalate') >= 0,
   '升级项必须带 escalate 类（否则那个闪烁提示不会出现）');
ok(APPROVAL_SEL === '1', '升级项仍然不该被默认选中');

// ── 场景 20：待审批时**禁用打字框** ──
//
// 浮窗才是 Web 的回答通道，但用户不知道 —— 会到聊天框里打字回答，
// 而那里**只认数字** -> 落进"打断 + 补充" -> 模型再调一次工具 -> 看着像死循环。
// 修法：禁用打字框 —— 最直接的办法。
next('场景 20 · 待审批时打字框让位');
var _ta = document.getElementById('chat-input');
var _btn = document.getElementById('chat-send');
var _ph0 = _ta.placeholder;

APPROVAL = null; APPROVAL_SEEN = ''; APPROVAL_DOCKED = false;
_setComposerEnabled(true);                 // 先回到可用，做基线
ok(_ta.disabled === false, 'setup：基线该是可用');

renderApproval(_pk());                     // 挂上一份待审批
ok(_ta.disabled === true, '[场景20] 有待审批时打字框该被禁用');
ok(_btn.disabled === true, '[场景20] 发送按钮也要一起禁用（只禁一个没用）');
ok(String(_ta.placeholder).indexOf('浮窗') >= 0,
   '占位文字该指路（否则用户只看到一个灰框，不知道去哪答）');

APPROVAL_SEEN = ''; renderApproval(null);  // 待审批没了
ok(_ta.disabled === false, '[场景20] 待审批消失后该恢复可用');
ok(_btn.disabled === false, '发送按钮也要恢复');
// [!] 别拿禁用之前抓的那个值比 —— 假 DOM 里 `getElementById` 给的是**合成空壳**，
//    抓到的 `placeholder` 是 `undefined`，而实现里 `|| ''` 过一道 -> 假红。
//    判据是"不再写着指路那句话"。
ok(String(_ta.placeholder).indexOf('浮窗') < 0, '占位文字该不再指路浮窗');

// * 负对照①：**答过的** request_id 不算待审批 -> 不该禁（否则答完界面就锁死了）
APPROVAL = null; APPROVAL_SEEN = 'R1';     // `_pk()` 的 request_id 就是 'R1'
_setComposerEnabled(true);
renderApproval(_pk());
ok(_ta.disabled === false,
   '* 已经答过的（stale）不该禁用 —— 那会让答完之后界面锁死');

// * 负对照②：**取失败也要恢复**（宁可让用户能说话，也不要留一个解不开的死锁）
//
// [!] 夹具里逐字是 `globalThis.fetch = () => new Promise(() => {})`：
//    **fetch 存在，且永不落地**（既不 resolve 也不 reject，更没有 catch）。
//    所以断言"什么都没发生"当然红 —— **用夹具之前先看夹具**。
//
// 这里改成让 fetch **同步抛** —— 那正好走 outer catch 那条路。
var _realFetch = globalThis.fetch;
APPROVAL = null; APPROVAL_SEEN = ''; APPROVAL_DOCKED = false;
_setComposerEnabled(false);
globalThis.fetch = function () { throw new Error('boom'); };
refreshApproval();
ok(_ta.disabled === false,
   '* refreshApproval 同步失败后必须恢复打字框（死锁界面比循环更糟）');
// [!] **没覆盖到的**：fetch 返回一个 **reject 的 promise** 那条路 ——
//    它是异步的，而这个场景脚本是同步的（微任务要等调用栈清空才跑）。
//    如实写在这里，别让下一个人以为两条都测了。
globalThis.fetch = _realFetch;

// ── 场景 21：流式草稿（`EVT_DELTA`）──
//
// 判据只有一条：**谁权威谁覆盖**。草稿随增量长大，权威文本（`say` / 最终回复）
// 一到就**整条丢掉** —— 所以"SSE 丢一条增量"顶多让预览短一截，不是正确性问题。
next('场景 21 · 流式草稿：增量拼成一条，且不进历史');

function _drafts() {
  return NODES.filter(function (n) {
    return String(n.el.className).indexOf('bubble-draft') >= 0;
  });
}
ACTIVITY = null;
clearChat();
var _hist0 = sessionStorage.getItem('crannia-chat-history');
foldEvent({ type: 'delta', data: { text: '你' } });
foldEvent({ type: 'delta', data: { text: '好' } });
ok(_drafts().length === 1, '增量该拼在**同一条**草稿气泡上（一条一片就错了）');
ok(_drafts()[0].textEl.textContent === '你好',
   '草稿内容该是增量拼接，实得：' + _drafts()[0].textEl.textContent);
ok(sessionStorage.getItem('crannia-chat-history') === _hist0,
   '★ 草稿**不许**进历史 —— 刷新后看到半截话比不流式更糟');

// ★★ **草稿的时间戳必须是「现在」**。
//
// 判据选的是**行为**（它显示成几点），不是读源码 —— 因为 bug 是**单位**传错，
// 而单位错在哪一行、用什么写法错，是不确定的。只钉"那一行怎么写"会漏。
//
// 单位错的形状：`pushDelta` 传了 `Date.now()`（**毫秒**），
// 而 `fmtTime(ts)` 的约定是**秒**（它自己 `ts * 1000`）->
// `new Date(1.79e12 * 1000)` = 公元 5 万多年 -> 用户看到一个**看起来很真实
// 但毫无意义**的时间 —— 而它让"多出来的那条草稿"看起来像是**另一次对话**，
// 把排查引偏。
var _hm = _drafts()[0].timeEl.textContent;
var _nowHM = fmtTime(Math.floor(Date.now() / 1000));
ok(/^\d\d:\d\d$/.test(_hm), '时间戳该是 HH:MM 形状，实得 ' + _hm);
ok(_hm === _nowHM || _hm === fmtTime(Math.floor(Date.now() / 1000) - 1),
   '★★ 草稿的时间戳该是**现在**（' + _nowHM + '），实得 ' + _hm
   + ' —— 把毫秒当秒传会显示一个公元 5 万多年的时刻');

// * 负对照：空增量不许凭空造出一条气泡
var _n0 = NODES.length;
foldEvent({ type: 'delta', data: { text: '' } });
ok(NODES.length === _n0, '* 空 text 不该新建草稿');

// ── 场景 22：权威文本一到，草稿整条丢掉 ──
next('场景 22 · 流式草稿：权威覆盖预览');
ok(_drafts().length === 1, 'setup：该有草稿');
foldEvent({ type: 'say', data: { text: '完整的话' } });
ok(_drafts().length === 0, '★ `say` 是权威渲染 —— 草稿必须整条消失（不许两份并存）');

// ★★ **`say` 之后要有下一轮的 `turn.start`，预览才重新开闸。**
//
// 它是真实事件序列的一部分（`agent_loop` 每轮 LLM 往返开头都发一条）——
// 缺了它，这个场景测的就是一个**生产里不存在的序列**。
// [!] 它是**竞态修复的边界**：`dropDraft()` 现在把「权威已到」记成**状态**
//    （不是一次性动作），所以「什么时候重新开闸」必须有个明确的事件来管。
next('场景 22b · 流式草稿：新一轮才重新开闸（竞态）');
foldEvent({ type: 'delta', data: { text: '不该出现的' } });
ok(_drafts().length === 0,
   '★★ 权威（say）已到、又**没有新的一轮** -> 后到的增量必须作废。'
   + '这就是用户报的那个 bug：响应先到、SSE 的 delta 后到，'
   + '于是多出一条永远没人收的草稿气泡（`测试记录13`，库里只有一条）');

foldEvent({ type: 'turn.start', data: { turn: 2 } });
foldEvent({ type: 'delta', data: { text: '又一段' } });
ok(_drafts().length === 1, 'setup②：新的一轮开始 -> 预览重新开闸');
foldEvent({ type: 'tool.start', data: { name: 'probe_tool', call_id: 'c1' } });
ok(_drafts().length === 0, '开始调工具，说明这一轮的文本已经说完，草稿让位');

// * 负对照：没有草稿时反复 dropDraft 不许抛（它每轮都会被调到）
ok((function () { try { dropDraft(); dropDraft(); return true; } catch (e) { return false; } })(),
   '* 没有草稿时反复 dropDraft 不许抛');

// ── 场景 22c：`task.start` 也是"重新开闸"（★ 它**是 durable 的**）──
//
// ★★ 为什么两处都要：`turn.start` **不落盘**（`turn.end` 已带时长与 token），
// 所以**浏览器中途重连**时它**不会**被 `Last-Event-ID` 补发 -> 那一轮往后的
// 增量会被自己的闸门吞掉。而 `task.start` **在 `DURABLE_TYPES` 里** -> 重连也补得到。
// -> 「至少每条用户消息开一次闸」由它兜住。
next('场景 22c · 流式草稿：task.start 兜底开闸（重连也补得到）');
foldEvent({ type: 'delta', data: { text: '又一段' } });
ok(_drafts().length === 0, 'setup：闸是关着的（上一段刚被 tool.start 关掉）');
foldEvent({ type: 'task.start', data: {} });
foldEvent({ type: 'delta', data: { text: '再来' } });
ok(_drafts().length === 1, '★ `task.start` 也要开闸 —— 它是重连时唯一补得到的那个');

clearChat();
ok(_drafts().length === 0, 'clearChat 之后草稿也不该留下（挂在已清空的 DOM 上）');

// ── 场景 22d：★★ `task.end` **也关闸** —— 审批触发的那条路 ──
//
// 事件序列（逐条）：
//   01:38:45.459  approval.request cross_zone   ← 任务 A 挂审批、结束
//   01:38:48.084  task.start                    ← 任务 B：审批答复＝新一轮
//   01:38:53.587  turn.end turn=1
//   01:38:55.397  turn.end turn=2               ← 既没有 `say`、也没有 `tool.start`
//   01:38:55.409  task.end
//
// ★★ 那条路里 `task.end` **自己**把权威回复落成气泡（`origin !== 'web'`），
//    而关闸口原来只有 `tool.start` / `say` / `/api/chat` 的响应 ——
//    **后两个在审批那条路上都不会发生**（它走 `/api/approval`，没有 `/api/chat` 响应）
//    -> 闸门开着 -> 迟到的增量造一条**永远没人收**的草稿。
//    用户看到的：同一段结论**出现两次**，其中一条是淡颜色的。
next('场景 22d · 流式草稿：task.end 关闸（审批触发时唯一的关闸口）');

function _agentMsgs() {
  return NODES.filter(function (n) { return n.kind === 'msg' && n.role === 'agent'; });
}

clearChat();
foldEvent({ type: 'task.start', data: {} });
foldEvent({ type: 'delta', data: { text: '搜完了，没找到。' } });
ok(_drafts().length === 1, 'setup：草稿该在');

foldEvent({ type: 'task.end', data: { ok: true, origin: 'approval',
                                      response: '搜完了，没找到。' } });
ok(_drafts().length === 0,
   '★★ `task.end` 必须关闸 —— 审批那条路上**没有别的关闸口**（测试记录14）');
// 注意：判据是**最终条数恰好 1**，不是"比之前多 1"：草稿本身就记成 `role='agent'`，
//    所以"草稿退场(-1) + 权威落下(+1)"的净值是 **0** —— 拿净值当判据会两头都错
//    （真多出一条时它照样是 0）。
ok(_agentMsgs().length === 1,
   '★ 权威落下 **且** 草稿退场 -> agent 气泡**恰好 1 条**'
   + '（2 条就是用户看到的"同一段话发了两遍"），实得 ' + _agentMsgs().length);
ok(_agentMsgs()[0].textEl.textContent === '搜完了，没找到。',
   '而且那一条就是权威文本，实得 '
   + JSON.stringify(_agentMsgs()[0].textEl.textContent));

// * 负对照：闸关着时，迟到的增量不许再开一条草稿
foldEvent({ type: 'delta', data: { text: '迟到的一片' } });
ok(_drafts().length === 0,
   '* 负对照：`task.end` 之后迟到的增量不许再造草稿（闸门是**状态**，不是一次性动作）');

// ── 场景 23：折叠器认识的每一种事件，前端都必须**订阅**到 ──
//
// [!] 这条钉的是：`to_sse()` 发的是**命名事件**
//    （帧里带 `event: <type>`），浏览器**只派发给注册了该类型名的监听器**。
//    于是"往 `foldEvent` 里加 `case`"**是不够的** —— 而上面场景 21/22
//    **直接调 `foldEvent`**，正好跳过了坏掉的那一层。
//    症状：折叠器认 `delta`、`EVT_TYPES` 没订，于是开关打开、流式在跑，
//    用户**一个字都看不到**。
next('场景 23 · 订阅名单必须覆盖折叠器认识的事件');
var _cases = [];
String(foldEvent).replace(/case '([^']+)':/g, function (m, t) { _cases.push(t); return m; });
ok(_cases.length >= 8, 'setup：从 foldEvent 里该抽出 >=8 个 case，实得 ' + _cases.length);
var _missing = _cases.filter(function (t) { return EVT_TYPES.indexOf(t) < 0; });
ok(_missing.length === 0,
   '★ 这些事件在 foldEvent 里有 case，却**没订阅**：' + _missing.join(', ') +
   ' —— 浏览器只派发已注册的类型名，用户看不到它们');
ok(EVT_TYPES.indexOf('delta') >= 0,
   '* 负对照：`delta` 必须在订阅名单里（否则上面那条对它就是空跑）');

// ── 场景 24：`context.state` 折叠成状态条读数 ──
//
// [!] 场景 23 只证明"折叠器认它、前端也订阅了"。**认了不等于渲染对** ——
//    这里问的是"读数的字面是什么、`armed` 有没有真的换一档、清空有没有退场"。
// [!] `_mkId` 会自动造元素 -> `getElementById('status-ctx')` 一定有值，所以
//    "盒子不存在就 return"那条早退**不会**让这些断言空跑。失败方向也是对的：
//    真把它挪走了，第一条断言就会红。
next('场景 24 · 上下文读数');
var _ctxBox = document.getElementById('status-ctx');
foldEvent(ev('context.state', {
  used: 42000, window: 131072, pct: 32.0,
  threshold_pct: 85.0, threshold_tokens: 111411,
  armed: false, window_source: '声明(config.yaml)',
  messages: 12, factor: 1.02,
}));
ok(_ctxBox.textContent === '上下文 32% / 阈值 85%',
   '常态读数该是「占用 / 阈值」，实得 ' + JSON.stringify(_ctxBox.textContent));
ok(_ctxBox.className === '', '常态不加档位类，实得 ' + JSON.stringify(_ctxBox.className));
ok(String(_ctxBox.title).indexOf('声明(config.yaml)') >= 0,
   '★ 窗口那个数**从哪儿来**要看得见（`N1`）—— 只给值不给来源，读的人无从判断'
   + '它是不是一个保守兜底。实得 ' + JSON.stringify(_ctxBox.title));

// `armed` —— 越过阈值的 75% -> **换一档**，不是只换个数字
foldEvent(ev('context.state', {
  used: 90000, window: 131072, pct: 68.7,
  threshold_pct: 85.0, threshold_tokens: 111411,
  armed: true, window_source: '声明(config.yaml)',
  messages: 40, factor: 1.02,
}));
ok(_ctxBox.textContent.indexOf('接近触发') >= 0,
   '★ `armed`（快触发了）必须**看得出来**，实得 ' + JSON.stringify(_ctxBox.textContent));
ok(_ctxBox.className === 'ctx-armed',
   '`armed` 该换到 ctx-armed 档，实得 ' + JSON.stringify(_ctxBox.className));

// 压缩那一轮：显示**这一次**的进出（不是"上次压过"）
foldEvent(ev('context.state', {
  used: 20000, window: 131072, pct: 15.3,
  threshold_pct: 85.0, threshold_tokens: 111411,
  armed: true, window_source: '声明(config.yaml)',
  messages: 5, factor: 1.02,
  compressed: { pre_tokens: 118000, post_tokens: 20000, saved: 98000,
                mode: 'auto', source: 'llm', turns: 4,
                messages_before: 60, messages_after: 5 },
}));
ok(_ctxBox.textContent.indexOf('118.0K tok') >= 0
   && _ctxBox.textContent.indexOf('20.0K tok') >= 0,
   '压缩那一轮要显示 pre -> post，实得 ' + JSON.stringify(_ctxBox.textContent));
ok(_ctxBox.className === 'ctx-just-compressed',
   '压缩那一轮走压缩档，实得 ' + JSON.stringify(_ctxBox.className));

// * 负对照：**粘住的旧值不许伪装成新事实** —— 没带 `compressed` 的那一轮必须
//   回到常态读数（哪怕上一轮刚压过、后端那边 `last_compression` 还挂着）。
foldEvent(ev('context.state', {
  used: 21000, window: 131072, pct: 16.0,
  threshold_pct: 85.0, threshold_tokens: 111411,
  armed: false, window_source: '声明(config.yaml)',
  messages: 6, factor: 1.02,
}));
ok(_ctxBox.textContent === '上下文 16% / 阈值 85%',
   '* 负对照：没压的那一轮不许留着"已压缩"的读数，实得 '
   + JSON.stringify(_ctxBox.textContent));

// `/clear`：读数属于**上一个会话**，必须一起退场
//
// [!] 退场要留**占位**：若清成空串，而 `#status-ctx` 是个 `onclick` 挂在上面的
// span -> **空串 = 没有可点区域 = 面板打不开**（"没读数"是个常见状态：
// 整页重载之后、`/clear` 之后）。
// -> 留一个**占位**（`上下文 —`）。判据不变：**上一个会话的数必须消失**
//   —— 占位说的正是"这个会话还没有读数"。
clearChat();
ok(_ctxBox.textContent === '上下文 —',
   '`/clear` 之后读数要退场成**占位**（空串会让面板点不开），实得 '
   + JSON.stringify(_ctxBox.textContent));
ok(_ctxBox.textContent.indexOf('%') < 0,
   '上一段会话的占用数不许留在状态条上');

// ── 场景 24：背景回执块必须**自己收口** ──
//
// 症状：`memory.saved` 那一块永远显示"进行中" —— 转轮一直转、计时器一路涨到
// `~53.4s`、还挂着「打断」按钮，整框也不折叠。
// 根因：那两个 `case` 若只调 `sealHead`，而 **`sealHead` 只是把表头压进步骤列表，
// 它不收口** —— 收口是 `closeActivity`。偏巧这两条事件**在 `task.end` 之后到达**
// -> **没有任何后续事件会去关它**。
// 这条场景同时钉住"收口"和"回执文字落在折叠行"两件事：只钉前者会放过
// 「收口了但折叠后什么都看不见」那个半边（`.activity-head` 折叠时是 `display:none`）。
next('场景 24 · 背景回执块必须自己收口');
foldEvent(ev('task.end', { ok: true, duration_ms: 1000, tokens: 100, response: '' }));
var _nBefore = NODES.length;
foldEvent(ev('memory.saved', { count: 2, names: ['甲', '乙'] }));
var _rcp = NODES[NODES.length - 1];
ok(NODES.length === _nBefore + 1,
   'memory.saved 该开一个**新块**（后台活自成一段），实际 +' + (NODES.length - _nBefore));
ok(_rcp.closed === true,
   '★ 回执块必须**当场收口** —— 否则转轮一直转、计时器一直涨（实测 ~53.4s）');
ok(_rcp.spinnerEl.parent === null, '收口后转轮必须退场');
ok(_rcp.stopEl.parent === null, '★ 收口后「打断」按钮必须退场');
ok(_rcp.el.classList.contains('collapsed') === true, '收口后该折叠成一行');
ok(_rcp.summaryEl.textContent === '已保存 2 条记忆：甲、乙',
   '★ 折叠行必须**就是回执文字**（写进表头的话折叠后什么都看不见），实得 '
   + JSON.stringify(_rcp.summaryEl.textContent));
ok(_rcp.steps.length === 0, '回执不该在步骤列表里再出现一次（否则同一句话显示两遍）');

foldEvent(ev('memory.organized', { count: 1, sessions: 5, names: ['丙'] }));
var _org = NODES[NODES.length - 1];
ok(_org.closed === true, '★ memory.organized 同病同治（它也在 task.end 之后到达）');
ok(_org.summaryEl.textContent === '记忆巩固：改动 1 条（看了 5 个会话）：丙',
   '整理回执的折叠行不对：' + JSON.stringify(_org.summaryEl.textContent));

// ── 场景 24b：`tool.neutralized` —— 轮内的一行，**不许**提前收口 ──
//
// [!] 它与场景 24 的 `receiptBlock` 是**两条相反的路**：那两条
//    （`memory.saved` / `memory.organized`）在 `task.end` **之后**到达 ->
//    **必须自己收口**（没有任何后续事件会去关它）。
//    而这一条到达在**轮内**（工具调用与 `task.end` 之间）-> 收口人是现成的
//    -> 只补一行、**不收口**。若照抄 `receiptBlock`，会把**整轮**的活动块
//    提前折叠掉（用户就看不到那几个工具步骤了）。
next('场景 24b · 中和的一行不许提前收口（F2）');
foldEvent(ev('task.start', {}));
foldEvent(ev('tool.start', { name: 'run_command', activity: '正在执行命令', call_id: 'c1' }));
foldEvent(ev('tool.end', { call_id: 'c1', ok: true, duration_ms: 12 }));
var _blk = ACTIVITY;
var _nBefore2 = NODES.length;
foldEvent(ev('tool.neutralized', { tool: 'send_email' }));
ok(NODES.length === _nBefore2, '中和那一行该**并进当前活动块**，不是新开一块');
ok(ACTIVITY === _blk, '不该换块（那是 `receiptBlock` 的形状，这里是轮内）');
ok(_blk.closed !== true, '★ 轮内到达 -> **不许当场收口**（收口人 `task.end` 还在）');
var _lastStep = _blk.steps[_blk.steps.length - 1];
ok(_lastStep && _lastStep.text.indexOf('另一条待审批') >= 0
   && _lastStep.text.indexOf('send_email') >= 0,
   '那一行必须说清是**哪一件**没执行，实得 '
   + JSON.stringify(_lastStep && _lastStep.text));

// 收口人（`task.end`）到场时，整块按正常路径折叠 —— 中性化那行留在步骤里
foldEvent(ev('task.end', { ok: true, duration_ms: 100, tokens: 5, response: '' }));
ok(_blk.closed === true, '`task.end` 该把这一块正常收掉（收口人没被抢走）');


// ── 场景 26：`files.changed` —— 独立一块 + 文件名可点 ──
//
// [!] 这条前端路**必须有自己的机械读者** —— 否则出了问题只能靠人看，
//    而"改完了没出现那个按钮"这类症状没有任何东西会红。
next('场景 26 · files.changed：独立一块 + 文件名可点（F1）');
foldEvent(ev('task.end', { ok: true, duration_ms: 100, tokens: 10, response: '' }));
var _n3 = NODES.length;
foldEvent(ev('files.changed', { count: 1, files: [
  { path: 'D:/ws/a.txt', name: 'a.txt', link: true, version: 3, added: 2, removed: 1 },
]}));
ok(NODES.length === _n3 + 1, 'files.changed 该开一个**独立**块（它在 task.end 之后到达）');
var _fc = NODES[NODES.length - 1];
ok(_fc.closed === true, '★ 必须**自己收口**（task.end 之后没有别的收口人）');
ok(String(_fc.summaryEl.textContent).indexOf('本轮改过 1 个文件') >= 0,
   '折叠行要说改了几个，实得 ' + JSON.stringify(_fc.summaryEl.textContent));
var _rows = _fc.stepsEl.children;
ok(_rows.length === 1, '一个文件 -> 一行，实得 ' + _rows.length);
var _chip = _rows[0] && _rows[0].children[0];
ok(_chip && _chip.textContent === 'a.txt',
   '★ 那一行的第一个子节点该是**文件名按钮**，实得 '
   + JSON.stringify(_chip && _chip.textContent));
ok(typeof _chip.onclick === 'function', '★ 它必须可点（点了走 openDiff）');
ok(String(_chip.title).indexOf('点一下看') >= 0,
   '要有 title 提示 —— 没有它没人知道那个文件名能点，实得 '
   + JSON.stringify(_chip.title));

// ★ 负对照：**本轮没改动** -> 什么都不画（空框看起来像「坏了」）
var _n4 = NODES.length;
foldEvent(ev('files.changed', { count: 0, files: [] }));
ok(NODES.length === _n4, '★ 没有改动时**不许**开空框（空框看起来像坏了）');

// ── 场景 25：整页重载之后读数要**补得回来** ──
//
// 症状：用户看见过上下文状态，去了一趟**设置页**再回来（独立路由 -> 整页重载），
// 那个读数就消失了。
// ★ 真正的代价**不是"少一个数"，是面板打不开**：读数为空 -> 空 span 没有可点区域
//   -> `toggleContextPanel()` 在那段时间里根本点不到。
// -> 两条一起钉：**永远有可点区域**（占位）+ **不用等下一轮就有数**（重载即补）。
async function _scene25() {
  next('场景 25 · 重载后补读数 + 永远有可点区域');

  // ① 没读数 -> **占位**，不是空串
  foldContextState({});
  ok(_ctxBox.textContent === '上下文 —',
     '没读数时该留占位（空 span 没有可点区域 -> 面板点不开），实得 '
     + JSON.stringify(_ctxBox.textContent));
  ok(String(_ctxBox.title).length > 0,
     '占位也要有 title —— 说清"为什么现在没有"（`N1` 那条：值要带来源/理由）');

  // ② 重载之后：`refreshContextOnLoad()` 拿 `/api/context` 把读数填回来
  var _realFetch25 = globalThis.fetch;
  var _urls25 = [];
  globalThis.fetch = function (url) {
    _urls25.push(String(url));
    if (String(url).indexOf('/api/context') === 0) {
      return Promise.resolve({ ok: true, status: 200, json: function () {
        return Promise.resolve({ pct: 37.5, threshold_pct: 85.0,
                                 threshold_tokens: 111411, used: 49152,
                                 window: 131072, messages: 12,
                                 window_source: '声明(config.yaml)' }); } });
    }
    return new Promise(function () {});          // 别的请求照旧永不落地
  };
  try {
    await refreshContextOnLoad();
  } finally {
    globalThis.fetch = _realFetch25;
  }
  ok(_urls25.some(function (u) { return u.indexOf('/api/context') === 0; }),
     '重载后**根本没去拉** `/api/context`：' + JSON.stringify(_urls25));
  ok(_ctxBox.textContent === '上下文 37.5% / 阈值 85%',
     '★ 补回来的读数不对（走的是与 `context.state` 同一个渲染）：'
     + JSON.stringify(_ctxBox.textContent));

  // ③ 负对照：接口**503**（框架没起来）时**保持占位、绝不清空**
  //    —— 清空 = 把入口又拿掉了（那正是这条要治的病）。
  foldContextState({});
  var _realFetch25b = globalThis.fetch;
  globalThis.fetch = function () {
    return Promise.resolve({ ok: false, status: 503, statusText: 'Service Unavailable',
      json: function () { return Promise.resolve({ error: 'framework not started' }); } });
  };
  try {
    await refreshContextOnLoad();
  } finally {
    globalThis.fetch = _realFetch25b;
  }
  ok(_ctxBox.textContent === '上下文 —',
     '取不到时该**保持占位**（清空 = 面板又点不开了），实得 '
     + JSON.stringify(_ctxBox.textContent));


  // ── 场景 26：`/` 候选层 ──────────────────────────────────────────
  //
  // [!] 它在 `_finish()` **之前**，且自己是 async —— 与场景 25 同一条规矩：
  //     摘要必须等所有 async 断言跑完再报，否则"没红"与"没跑到"同形。
  next('场景 26 · `/` 候选层（H7c）');

  // 目录走真 `api()` -> 真 `fetch`（夹具里那个默认 fetch 是"永不落地"）
  var _realFetch26 = globalThis.fetch;
  var _asked26 = [];
  globalThis.fetch = function (url) {
    _asked26.push(String(url));
    if (String(url).indexOf('/api/commands') === 0) {
      return Promise.resolve({ ok: true, status: 200, json: function () {
        return Promise.resolve({ ok: true, commands: [
          { cmd: '/compact', desc: '把前面的对话压成摘要', implemented: true },
          { cmd: '/context', desc: '看这一段会话装了什么', implemented: true },
          { cmd: '/cancel', desc: '打断正在跑的这一轮', implemented: false },
        ] }); } });
    }
    return new Promise(function () {});          // 别的请求照旧永不落地
  };

  var _pop = document.getElementById('slash-popover');
  var _kd = function (key) {                       // 走**真**的按键处理器
    var e = { key: key, shiftKey: false, prevented: false,
              preventDefault: function () { this.prevented = true; } };
    callIfFn(onInputKey, null, e);
    return e;
  };
  var _cmdsIn = function () {
    return (_pop.children || []).map(function (r) {
      return (r.children && r.children[0]) ? r.children[0].textContent : '';
    });
  };

  slashHide();
  TXAREA.value = '/';
  await slashUpdate();
  ok(_pop.hidden === false, '打了 `/` 却没弹候选');
  ok(_asked26.some(function (u) { return u.indexOf('/api/commands') === 0; }),
     '**根本没去拉** `/api/commands`：' + JSON.stringify(_asked26));
  ok(_cmdsIn().join(',') === '/compact,/context,/cancel',
     '候选内容不对：' + JSON.stringify(_cmdsIn()));
  // ★ 零 `innerHTML`：夹具那个替身的 `innerHTML` 是**空操作** ——
  //   用了它就一个子节点都不会有（只用 createElement 的顺带读者）。
  ok((_pop.children || []).length === 3,
     '候选不是用 createElement 建的（数到 ' + (_pop.children || []).length + ' 个）');
  // ★ 后端如实标了 `implemented:false` -> 前端**要摆出来**，不许藏
  ok(String(_pop.children[2].className).indexOf('slash-item') >= 0
     && (_pop.children[2].children || []).some(function (n) {
          return n.className === 'slash-todo'; }),
     '「未实现」的那条被藏起来了（`D7`：不许承诺兑现不了的东西，藏起来同样是骗）');

  TXAREA.value = '/co';
  await slashUpdate();
  ok(_cmdsIn().join(',') === '/compact,/context',
     '前缀过滤不对：' + JSON.stringify(_cmdsIn()));

  // 上下移动走**真**的按键路径
  var _before = _slashSel;
  var _e1 = _kd('ArrowDown');
  ok(_slashSel === (_before + 1) % 2, '`↓` 没动选中项');
  ok(_e1.prevented === true, '`↓` 没吃掉事件（页面会跟着滚）');
  _kd('ArrowUp');
  ok(_slashSel === _before, '`↑` 没回到原处');

  // 采纳：`Tab`
  _kd('ArrowDown');                       // 选到 /context
  var _e2 = _kd('Tab');
  ok(TXAREA.value === '/context ', '`Tab` 采纳后输入框是 ' + JSON.stringify(TXAREA.value));
  ok(_pop.hidden === true, '采纳之后候选没收掉');
  ok(_e2.prevented === true, '`Tab` 没吃掉事件 —— 焦点会跳走');

  // ★ 采纳**要把后面打的内容留着**（`/co 你好` -> `/context 你好`）
  TXAREA.value = '/co 你好';
  await slashUpdate();
  ok(_pop.hidden === false && _cmdsIn().join(',') === '/compact,/context',
     '还没选完（`/co` 不是完整命令）就把候选收了：' + JSON.stringify(_cmdsIn()));
  _slashSel = 1;
  _kd('Enter');
  ok(TXAREA.value === '/context 你好',
     '采纳时把后面的内容弄丢了：' + JSON.stringify(TXAREA.value));

  // ★ 已经是一条完整命令 + 空格 => 收掉（那时是在打参数，不该再弹）
  TXAREA.value = '/context 看装了什么';
  await slashUpdate();
  ok(_pop.hidden === true, '完整命令后面打参数时还在弹候选');

  // `Esc` 关掉
  TXAREA.value = '/';
  await slashUpdate();
  ok(_pop.hidden === false, 'setup：`/` 该弹');
  _kd('Escape');
  ok(_pop.hidden === true, '`Esc` 没关掉候选');

  // ★★ 负对照：后端**读不到**时**不许静悄悄什么都不弹** ——
  //    那会让打 `/` "没反应"，而"没反应"与"没有匹配"同形。
  slashHide();
  _slashAll = null; _slashErr = ''; _slashTriedAt = 0;
  var _realFetch26b = globalThis.fetch;
  globalThis.fetch = function () {
    return Promise.resolve({ ok: false, status: 503, statusText: 'Service Unavailable',
      json: function () { return Promise.resolve({ ok: false, error: 'framework not started' }); } });
  };
  TXAREA.value = '/';
  await slashUpdate();
  ok(_pop.hidden === false,
     '★ 目录读不到时**什么都不弹** —— 用户看到的是"打了 / 没反应"（`W25a`）');
  ok((_pop.children || []).length === 1
     && String(_pop.children[0].className) === 'slash-err',
     '读不到时该**单独占一行说清**，实得 ' + JSON.stringify(_cmdsIn()));
  ok(String(_pop.children[0].textContent).indexOf('framework not started') >= 0,
     '★ 那一行**没带原因** —— 只说"读不到"，用户与我都无从下手');
  // ★ 而且它**不可采纳**（不许把那一行报错当成一条命令写进输入框）。
  //   [!] 直接问 `slashAccept()` —— **别按 Enter**：那会落到 `send()`，
  //      而这条要问的是"报错行能不能被采纳"，不是"回车会不会发消息"。
  TXAREA.value = '/compact';
  ok(slashAccept() === false, '★ 报错那一行被当成命令采纳了');
  ok(TXAREA.value === '/compact',
     '输入框被那一行报错改写了：' + JSON.stringify(TXAREA.value));
  slashHide();
  globalThis.fetch = _realFetch26b;

  // 负对照：候选关着时，`Enter` **照旧是发送**（这条防的是"吃掉所有 Enter"）
  slashHide();
  globalThis.fetch = _realFetch26;

  _finish();
}

// ── 汇总 ──
// [!] 挪进函数：上面场景 25 是 **async** 的，摘要必须等它跑完再报
//    （否则会在那条场景断言之前就 `process.exit` —— "没红"与"没跑到"同形）。
function _finish() {
  if (_fails.length) {
    console.log('FAIL');
    _fails.forEach(function (m) { console.log('  - ' + m); });
    process.exit(1);
  }
  console.log('OK  ' + _scenes + ' scenarios, ' + _asserts + ' assertions passed');
}

// ── 场景 27：运行中的计时**不许虚构** ──
//
// [!] 这一半必须有读者：收尾那一半已有覆盖；**运行中那个 `~` 跳秒没有读者**。
// [!] 它测的是**两件事**，缺一条都不算：
//    ① 运行中显示的是 `~` + **本地钟推出来的**值（明说是估计，且来源是真钟）；
//    ② 收尾显示的是**服务端给出的 `duration_ms`**，**不带 `~`**。
//    只钉①，一个永远显示 `~0ms` 的坏钟也会绿；只钉②，就与 `:381` 重复。
next('场景 27 · 运行中的 `~` 跳秒 + 收尾用实测值');
(function () {
  var _tick = null;
  for (var i = 0; i < _intervals.length; i++) {
    if (_intervals[i].ms === 200) { _tick = _intervals[i].fn; }
  }
  ok(_tick !== null,
     'setup：夹具里该留下一个 200ms 的本地计时器，实得 ' +
     _intervals.map(function (x) { return x.ms; }).join(','));

  foldEvent(ev('task.start', { label: '计时探针' }));
  ok(ACTIVITY && ACTIVITY.startedAt,
     'setup：`task.start` 之后该有一个在跑的活动块');

  // 假钟：把"现在"钉在开始之后 1500ms
  var _realNow = Date.now;
  Date.now = function () { return ACTIVITY.startedAt + 1500; };
  _tick();
  ok(ACTIVITY.elapsedEl.textContent === '~1.5s',
     '★ 运行中该显示 `~` + 本地钟推出来的值，实得 ' +
     JSON.stringify(ACTIVITY.elapsedEl.textContent));
  ok(String(ACTIVITY.elapsedEl.textContent).indexOf('~') === 0,
     '★ 运行中**必须**带 `~` —— 那是"已经过去多久"，不是"还差多久"');

  // 收尾：服务端给的实测值，**不带 `~`**
  foldEvent(ev('task.end', { ok: true, duration_ms: 1234, tokens: 0 }));
  Date.now = _realNow;
  ok(ACTIVITY.elapsedEl.textContent === '1.2s',
     '★★ 收尾该显示**服务端实测**的那个数（不带 `~`），实得 ' +
     JSON.stringify(ACTIVITY.elapsedEl.textContent));
  ok(String(ACTIVITY.elapsedEl.textContent).indexOf('~') < 0,
     '★★ 收尾之后还挂着 `~` —— 那会把"估计"冒充成"实测"');

  // [!] 复位：后面的场景还要用这个全局
  ACTIVITY = null;
})();

_scene25();
"""


def _extract_script() -> str:
    html = CHAT_HTML.read_text(encoding="utf-8")
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    assert blocks, "chat.html 里一个 <script> 块都没有 —— 结构变了？"
    return "\n".join(blocks)


def test_the_api_helper_still_lives_in_base_html():
    """夹具里的 `api()` 是替身 —— 这条钉住"真货还在 `base.html` 里"。

    判据：替身与真货分居两个文件时，替身会在真货消失之后继续绿。
    场景 25 要调 `refreshContextOnLoad()`，而它走 `api()` —— 本夹具只抽 `chat.html`，
    所以必须自带一个同形替身。-> 配套这条：真 `api` 没了/改名了、或 `chat.html`
    自己又长出一个同名的（两套），都当场红。
    """
    base = (_PROJECT_ROOT / "web" / "templates" / "base.html").read_text(
        encoding="utf-8")
    assert re.search(r"async\s+function\s+api\s*\(", base), (
        "`base.html` 里的 `api()` 不见了 —— 夹具里那个替身会在生产坏掉之后继续绿")
    chat = CHAT_HTML.read_text(encoding="utf-8")
    assert not re.search(r"function\s+api\s*\(", chat), (
        "`chat.html` 自己定义了一个 `api()` —— 两个同名助手会各漂各的")


def test_hidden_attribute_is_not_overridden_by_display():
    """面板关不掉 —— `hidden` 被 CSS 顶掉了。

    症状：刚启动就看见一个右侧黑框，还关不掉。
    根因：`.diff-panel` 写了 `display: flex`，而 **作者样式里的 `display`
    永远优先于 UA 样式表里的 `[hidden] { display: none }`**（与特异性无关）
    -> `el.hidden = true`（`closeDiff()`）一个像素都不动。
    `.chat-attach`（`display: flex`）同一处伤；`.context-panel` 没写 `display`
    所以恰好没事 —— 而"恰好"正是要消灭的东西。

    -> 修法是堵这一类：`style.css` 里一条全局
    `[hidden] { display: none !important; }`。这条护栏钉住"那条规则还在"。

    [!] 它证明不了浏览器会照做（那要真浏览器）—— 它挡的是把规则删掉。
    [!] `!important` 是承重的：同特异性的后置规则只在书写顺序上分胜负，
    而那正是下一个 bug 的温床。
    """
    css = (_PROJECT_ROOT / "web" / "static" / "style.css").read_text(
        encoding="utf-8")
    # 先剥注释再匹配 —— 这条判据是"找一条 CSS 规则"，而上面那段注释里
    #    逐字写着同一串（`[hidden] { display: none }`）。
    #    不剥就等于让检查器打到自己的说明文字上：多出来的那一处会让
    #    "规则被删掉了"变绿。
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules = re.findall(r"\[hidden\]\s*\{([^}]*)\}", css)
    assert rules, (
        "`style.css` 里没有 `[hidden]` 规则 —— 只要有任何组件写了 `display:`，"
        "它的 `hidden` 就会静默失效（面板关不掉，现场反馈）")
    body = " ".join(rules)
    assert re.search(r"display\s*:\s*none", body), (
        "`[hidden]` 规则里没有 `display: none`：%r" % body)
    assert "!important" in body, (
        "`[hidden] { display: none }` 少了 `!important` —— 组件自己的 "
        "`display: flex` 与它**特异性相同**，那只在**书写顺序**上分胜负")


#: 扫描得到的存量（5 个名字 / 7 处）—— 棘轮基线，只许变短。
#: 它们今天都悄悄落在 `var(--x, 兜底)` 那个兜底上（外观就那样）-> 不擅自改，
#: 只为挡新增。修好一个就把它从这儿删掉。
_KNOWN_UNDEFINED_CSS_VARS = frozenset({
    # base.html / security.html：多半想要 `--bg`（同一个东西的旧名）
    "bg-main",
    # base.html / dashboard.html：多半想要 `--accent`
    "primary",
    # dashboard.html：多半想要 `--orange`
    "warn",
    # style.css：这两个在 CSS 里只有引用点、没有同名定义
    "bg-hover", "fg-muted",
})


def test_no_css_variable_is_referenced_without_being_defined():
    """引用了未定义的 CSS 变量 -> 悄悄落到 `var(--x, 兜底)` 那个兜底上。

    `.diff-panel` 写的是 `var(--bg-panel, #1b1b1f)`，而 `--bg-panel`
    全仓没有定义 -> 面板变黑底 —— 那个兜底是照深色主题写的，
    而这个 app 是浅色的 -> 用户看到的是"看着也太难受了，改成白底吧"。

    这类失败的形状是「看着不对」而不是「报错」-> 只有机械读抓得住。
    棘轮：基线里那几个是今天就已经存在的（动它们 = 改外观，
    所以不擅自修）；这一条只挡新增。
    """
    import re
    root = _PROJECT_ROOT / "web"
    defined: set = set()
    used: dict = {}
    re_def = re.compile(r"--([A-Za-z0-9_-]+)[ \t]*:")
    re_use = re.compile(r"var[(][ \t]*--([A-Za-z0-9_-]+)")
    for f in sorted(root.rglob("*")):
        if f.suffix not in (".css", ".html"):
            continue
        t = f.read_text(encoding="utf-8", errors="replace")
        for m in re_def.finditer(t):
            defined.add(m.group(1))
        for m in re_use.finditer(t):
            used.setdefault(m.group(1), set()).add(f.name)
    missing = {k: v for k, v in used.items() if k not in defined}
    new = sorted(set(missing) - _KNOWN_UNDEFINED_CSS_VARS)
    assert not new, (
        "这些 CSS 变量**没有定义**，只会在 `var(--x, 兜底)` 里悄悄用兜底值："
        + repr([(n, sorted(missing[n])) for n in new])
        + chr(10) + "  -> 兜底值若照的是另一套主题，症状是**看着不对**而不是报错"
 + "（`--bg-panel` 就是这么把 diff 面板变成黑底的）。"
        + chr(10) + "  -> 要么补上定义，要么改成已存在的变量。")
    stale = sorted(v for v in _KNOWN_UNDEFINED_CSS_VARS if v in defined)
    assert not stale, (
        "这些变量**已经有定义**了，请从 `_KNOWN_UNDEFINED_CSS_VARS` 删掉："
        + repr(stale))

def _console_safe(text: str) -> str:
    """把 GBK 编不出来的字符换成 `?`，再交出去打印。

    为什么需要：`chat.html` 里有 emoji（步骤符号 🟢/🔴）。断言失败时这段输出
    会打印到 Windows 控制台，而控制台默认 GBK -> 一个 emoji 就让**报告失败的那一行
    自己崩掉，看起来像测试挂了。"崩掉的输出看起来像别的问题"** 正是本仓
    反复记过的那种坑（emoji 曾让整个测试运行崩掉）。
    """
    return text.encode("gbk", "replace").decode("gbk", "replace")


def test_fold_event_segments_and_interrupt_bubble():
    node = shutil.which("node")
    if not node:
        # [!] 这里必须 raise，不能 return。
        # `_main` 对任何正常返回的函数都印 PASS -> `return` 会让这条护栏在没装 node
        # 的机器上报"通过"。那正是本文件 docstring 自己警告的形状：
        # 一条会自己跳过的护栏等于没有护栏。
        # 负对照：`PATH=` 跑一次，必须看到 FAIL。
        raise AssertionError(
            "没有 node —— 这条护栏无法运行，不算通过。"
            "（装 node，或从这里开始就别再靠肉眼看这层逻辑）"
        )

    src = _STUBS + "\n" + _extract_script() + "\n" + _CHECKS
    with tempfile.TemporaryDirectory() as tmp:      # 不落进工作区
        js = Path(tmp) / "fold_check.js"
        js.write_text(src, encoding="utf-8")
        r = subprocess.run([node, str(js)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=60)

    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    assert r.returncode == 0 and out.startswith("OK"), (
        "前端折叠逻辑断言失败：\n" + _console_safe(out or "(无 stdout)") +
        ("\n--- stderr ---\n" + _console_safe(err[-1500:]) if err else "")
    )
    print("  [ok] " + out.replace("OK  ", ""))


# ------------------------------------------------------------------

def _main():
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except AssertionError as exc:
            failed += 1
            print("FAIL  " + name + ": " + str(exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("ERROR  " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
