import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Archive, BriefcaseBusiness, Check, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Clock3,
  Code2, Copy, Cpu, FileText, Folder, FolderOpen, MessageSquare, Pin, Plus, Trash2,
  Settings2, Square, Star, Users, Wrench, X,
} from "lucide-react";
import { jsonRequest } from "./api.js";
//:往剪贴板写文本：三条路按序试、结果如实返回（失败不静默）。
import { copyText } from "./clipboard.js";
//:通知队列的规则（纯函数；行为读者在 `tests/test_desktop_toasts.py`）。
import { pruneToasts, pushToast } from "./toasts.js";
import { setPathOpener } from "./openpath.js";
//:附件前言只给模型看；用户气泡 / 历史面板共用这一条界面视图。
import { userTextForDisplay } from "./user-text.js";
//:用户消息里的附件：从库里那两个指针形状解析出来（不新增存储字段），
//:   再在气泡下面画成缩略图 / 文件卡。解析是纯函数（`packages/ui/tests/attachments.test.mjs`）。
import { parseAttachments } from "./attachments.js";
import { MessageAttachments } from "./Attachments.jsx";
import { Confirm } from "./Confirm.jsx";
import { activityAnchorIndex, classifyReply, replyAnchorIndex } from "./message-order.js";
//:名册画哪些条目的唯一判据（0 轮且不是当前，不画）。
import { liveEventBelongs, visibleThreads, viewKeyOf } from "./thread-list.js";
//: 「还贴着底吗」—— 自动跟随的唯一判据（用户）。
import { nextStick } from "./scroll.js";
import { ActivityBlock } from "./ActivityBlock.jsx";
import { MemoryLibrary } from "./MemoryLibrary.jsx";
import { SettingsTransfer } from "./SettingsTransfer.jsx";
import { AutoRulesPanel } from "./AutoRulesPanel.jsx";
import { LogsView } from "./LogsView.jsx";
// 一条回复产出的图 / 音 / 视频（工具产物 + 上游媒体）。
import Artifacts from "./Artifacts.jsx";
//:大产物的判据（纯函数，node 真跑）：批 1 —— 落盘不设限，
//:   但单个超过 1GB 要在右上角说一声。
import { formatSize, needsLargeNotice } from "./artifacts-view.js";
//:审批浮窗：判据全在 `approval-model.js`（纯函数，node 可逐条断言）；
//:   组件本身导出成独立件，才能单独挂载测 DOM。
import { Approval } from "./Approval.jsx";
//:提问面板 —— 与审批是两种形状（问意见 ≠ 求授权）。
import { AskUser } from "./AskUser.jsx";
import { BtwPanel } from "./BtwPanel.jsx";
//:群聊开屏要画主理人的头像（与成员卡同一个组件）。
import { Avatar } from "./Avatar.jsx";
//:插件被屏蔽时的实时弹窗，与手动→自动那条安全提示共用同一个设计与音效
//:   （原则：一定要看得见；无论从哪条路触发，表意一致）。
import { noticeSeen, PluginNotice, rememberNotice } from "./PluginNotice.jsx";
import { Composer } from "./Composer.jsx";
//:上下文读数的格式化与映射全在 `context.js`（纯函数，node 可断言）。
//:顶栏那一行与面板那三列必须走同一个函数 —— 两套渲染会让同一个数讲两句话。
import { cellRows, ctxPanelCloses, noteText, readoutText } from "./context.js";
//:模式轴：档表与那枚 chip 的判据收在 `mode.js`（纯函数，node 可断言）。
//:   chip 不能读 `settings.default_mode`：那个对象只在打开设置弹窗时才拉，
//:   第一轮之前会一律显示「手动」（标签在说谎）。判据见那个文件。
//:`MODE_LABELS` 不从这里用：设置页那三颗按钮由表驱动（`settings-view.js`
//:的 `from:"modes"`，选项来自 `/api/settings` 的 `modes`），Composer 那一份
//:   仍直接用它。留一个没人用的 import 会制造"这里还有一份档表"的假象。
import { chipMode } from "./mode.js";
//:配色的六个轴与存取（纯模块，node 可断言）。
//:它只写 <style id="lv-theme">，不碰后端 —— 存的是 GUI 自己的 localStorage
//:   （为什么不跟 WebUI 共用，见 `theme.js` 抬头）。
import {
  applyMode as applyThemeMode, applyTheme, AXES, clearTheme, defaultsFor,
  loadMode, loadTheme, resolveMode, saveMode, saveTheme, THEME_MODES,
} from "./theme.js";
//:设置页那张四栏的表。
//:判据全在纯函数里，node 可逐条断言；测试会拿这份表反查后端
//:   （每个键 GET 得给得出、POST 得收）。
import { allowlistBody, isExtra, optionsFor, rowsFor, ROWS, saveBody, SECTIONS,
         THREAD_LIST_LIMIT, THREAD_NOTICE_AT, valueOf } from "./settings-view.js";
import { ListEditor } from "./ListEditor.jsx";
import { FileDiff } from "./FileDiff.jsx";
import { Markdown } from "./Markdown.jsx";
import { Drawer, Rail } from "./Rail.jsx";
import { autoNoticeOff, clearAutoNotice, rememberAutoNotice, SafetyNotice } from "./SafetyNotice.jsx";
import { TakeoverNotice } from "./TakeoverNotice.jsx";
import { HELP_DOCS, HelpPanel } from "./HelpPanel.jsx";
import { clearTakeoverNotice, rememberTakeoverNotice, stopReasonText, takeoverNoticeOff } from "./takeover-model.js";
//:技能页独立成一个导出件（同 `Approval` 那条理由）—— 这样才能单独挂载测 DOM。
//:   判据全在 `skills-view.js`（纯函数）。
import { Skills } from "./Skills.jsx";
//: 「协作群聊」那一屏。判据全在 `group-form.js`（纯函数，node 可逐条断言），
//:   这里只管画。
import { Groups } from "./Groups.jsx";
import { Endpoint } from "./Endpoint.jsx";
//:首启引导（从旧 WebUI 搬来的五步）—— 判据全在 `onboarding-model.js`。
import { Onboarding } from "./Onboarding.jsx";
import { dataSchemaNotice, leafNotice } from "./settings-view.js";
//:没有反馈面板了：反馈口改成一条 GitHub issue 外链
//:   （判据见 `settings-view.js` 那一行：拒绝多一个外发点）。
import { ISSUES_URL, PROJECT_URL, openExternal } from "./project-links.js";
//:上拉框从 `UpPicker.jsx` 来（一处实现、两边用）。
//:设置弹窗里要用 `fixed` 那一档 —— 这一列是 `overflow:auto`，
//:   绝对定位的菜单会被裁掉（见 `UpPicker.jsx` 抬头）。
import { UpPicker } from "./UpPicker.jsx";
import { Schedules } from "./SchedulesPanel.jsx";
import { Welcome } from "./Welcome.jsx";
import { WorkdirPick } from "./WorkdirPick.jsx";

//:图标地址不在这里写死 —— 它由壳注入（`client.asset`），见 `AppInner` 里那一行。
//: 写死的 `/static/img/logo.svg` 在 `file://` 下会解析成
//:`file:///static/img/logo.svg`（盘根目录），裂图。
/** 「活跃对话」的时间窗 —— 固定 30 分钟。
 *
 *  它决定横幅上「回到当前对话」按钮出不出来：最近的活跃对话超过这个窗口，
 *    就没有"当前"可回了（此时用户看的是一段过去，手上也没有别的活）。
 *  窗口在前端算（后端 `/api/threads` 只给 `last_at` 那个时刻）——
 *    后端若给一个算好的布尔，它会随那次拉取一起过期：界面 40 分钟不动，
 *    按钮就还亮着，而它本该消失（别把"那一刻的答案"当成"这个问题的答案"）。
 *  时刻的解析口径与 `histRow` 一致（`Date.parse`，无时区标记，按本地时间解，
 *    而后端 `_now()` 写的正是本地时间）。
 */
import { lang, loadI18n, t } from "./i18n.js";
const ACTIVE_WINDOW_MS = 30 * 60 * 1000;

/** 活动头那句「当前这一步」的两个取值。
 *
 *  它必须能在「正在执行命令」与「思考中」之间来回翻：`tool.start` 把它设成
 *    「正在执行命令」之后，`tool.end` 必须能翻回「思考中」；否则整段对话都挂着
 *    「正在执行命令」，哪怕那一刻模型其实在思考。
 *  一处定义：两个词都在这里，`task.start` / `tool.start` / `tool.end` 三处
 *    只许引用常量（写三份字面量会各自漂）。
 */
const THINKING = "思考中";
const RUNNING_CMD = "正在执行命令";

/** 「已打断」那句回执的身份（服务端 `cancelled` 标出来）。
 *
 *  它不是模型说的话、也不落库（`core/agent_loop.py::CANCEL_REPLY` 上面那句判据：
 *  「那是一句渲染，不是一条消息」），两件事都要管：
 *   · 用户发下一句就撤掉它 —— 否则它会一直垂在对话里；
 *   · 它不参与「最后一条消息」的判据 —— 否则活着的步骤流永远不算"最后一条"，
 *     底部长出一条假的「正在处理」（这是前一个现象的副产物）。
 *  判据用服务端的标记，不拿文案比 —— 同一句话写在两处就是两套真相源。
 */
const CANCEL_RECEIPT = "cancel_receipt";
const isReceipt = (m) => !!m && m.kind === CANCEL_RECEIPT;
/** 最后一条不是回执的消息下标（没有就 `-1`）。回执不算"最后一条"。 */
const lastNonReceipt = (msgs) => {
  const list = Array.isArray(msgs) ? msgs : [];
  for (let i = list.length - 1; i >= 0; i--) if (!isReceipt(list[i])) return i;
  return -1;
};
//: 侧栏第一区：新建任务 / 技能与连接器 / 端配置 / 定时任务 / 协作群聊。
//: 「端配置」是本前端的独立页面，不再跳系统浏览器去 WebUI。
//:角标不只是装饰：`nav()` 拿它决定"点进去要不要弹一句提示"，
//:留着它会让一屏能用的东西自称不能用（现役导航项都不带角标）。
const NAV = [
  ["chat", "新建任务", Plus], ["skills", "技能与连接器", Wrench],
  ["endpoint", "端配置", Settings2],
  // 「定时任务」有自己的面板（`SchedulesPanel.jsx`），不带「未实现」角标。
  //   角标不只是装饰：`nav()` 拿它决定"点进去要不要弹一句提示"，
  //   留着它会让一屏能用的东西自称不能用。
  ["schedules", "定时任务", Clock3],
  // 「协作群聊」占据侧栏这一格（原「资料库」的位置已作废，改由群聊承接）。
  ["groups", "协作群聊", Users]
];
//: 「技能」那一屏的主标题与副标题（`App` 的 header 与 `Skills` 页各用一处）。
//:   集中一个常量，免得两处再各写一遍"专家 · 技能 · 连接器"。
const SKILLS_TITLE = "技能与连接器";
//:审批/提问浮窗问的是"哪条会话" —— 待审批按会话存，群聊用的是
//:   `workspace:group:<id>`。不带 group 就会去私聊那条找（面板永远不出现，
//:而聊天里只有「(无回复)」）。两条路（GET 读 / POST 答）共用这一个拼法，
//:一处一个地方 —— 分开拼迟早只改一条。
const approvalUrl = (gid) =>
  "/api/approval" + (gid ? "?group=" + encodeURIComponent(gid) : "");

//:顶栏两格各自的状态（不是入口），计数为 0 时整格不画。
//:   判据与 `.task-chip` 那一对按钮同源（服务端 `GET /api/tasks` 的 counts）。

function _elapsedLabel(ms) {
  const n = Number(ms) || 0;
  if (n < 60000) return Math.round(n / 1000) + "s";
  return Math.floor(n / 60000) + "m" + Math.round((n % 60000) / 1000) + "s";
}

/** 一条后台任务 / 子代理。
 *
 *  后台任务：最多 3 行摘要（CSS `line-clamp`），点开看完整命令 ——
 *    命令可能很长，而摘要会骗人（`cc` 那份也是这么做的）。
 *  子代理：只显示名称 —— 它的过程与结论属于父的上下文，不该在这儿铺开。
 */
//:这一格的任务对象不许叫 `t`：`t` 是本模块的翻译函数，
//:   参数遮住它之后 `t("点一下收起")` 就变成"拿一个对象当函数调"
//:，渲染即抛 `TypeError: t is not a function`（浮窗一打开整页崩）。
function TaskRow({ task, expandable }) {
  const [open, setOpen] = useState(false);
  const label = task.description || task.command || task.task_id || "";
  const clickable = expandable && !!(task.command || task.description);
  return (
    <div className={"task-row" + (open ? " open" : "") + (clickable ? " clickable" : "")}
         onClick={clickable ? () => setOpen((v) => !v) : undefined}
         title={clickable ? (open ? t("点一下收起") : t("点一下看完整命令")) : ""}>
      <div className="task-row-head">
        <span className={"task-row-name" + (open ? " open" : "")}>{label}</span>
        <span className="task-row-ms">{_elapsedLabel(task.elapsed_ms)}</span>
      </div>
      {open ? <pre className="task-row-full">{task.command || label}</pre> : null}
    </div>
  );
}

/** 后台任务 / 子代理的浮窗 —— 形状复用会话构成面板（`.ctx-panel`）。
 *
 *  读不到时说出原因（`tasks.error`），不回一张空表：
 *    "没有任务"与"读不到"看起来一样，而前者是一个结论。
 */
function TaskPanel({ which, tasks, onClose }) {
  const list = which === "bg" ? ((tasks && tasks.background) || [])
                              : ((tasks && tasks.subagents) || []);
  const title = which === "bg" ? t("后台任务") : t("子代理");
  return (
    <section className="ctx-panel task-panel">
      <div className="ctx-panel-head">
        <b>{title}</b>
        <button className="round" onClick={onClose}><X size={14} /></button>
      </div>
      {tasks && tasks.error ? (
        <p className="ctx-error">{t("取不到任务列表：{tasks.error}", { "tasks.error": tasks.error })}</p>
      ) : list.length === 0 ? (
        <p className="ctx-menu-note">{t("现在没有在跑的{title}", { "title": title })}</p>
      ) : (
        <div className="ctx-panel-body">
          {list.map((task) => <TaskRow key={task.task_id} task={task} expandable={which === "bg"} />)}
        </div>
      )}
    </section>
  );
}

function AppInner({ client }) {
  //:图标走壳注入的 base（与 `/api` 那条缝是同一条）——
  //:打包后页面来自 `file://`，写死的 `/static/...` 会变成盘根目录，裂图。
  const LOGO = client.asset("/static/img/logo.svg");
  const [page, setPage] = useState("chat"), [status, setStatus] = useState(null), [connected, setConnected] = useState(false);
  const [spaces, setSpaces] = useState([]), [space, setSpace] = useState(""), [skills, setSkills] = useState([]);
  //: `#02` 组件异常 —— 本进程记下的运行时事故（见 `core/incidents.py`）。
  //:  只在新对话页（`empty`）以横幅提示；用户离开那一屏就不再提。
  const [incidents, setIncidents] = useState([]);
  const [messages, setMessages] = useState([]), [busy, setBusy] = useState(false);
  const [provisional, setProvisional] = useState("");
  const [approval, setApproval] = useState(null), [settingsOpen, setSettingsOpen] = useState(false), [settings, setSettings] = useState(null), [toasts, setToasts] = useState([]);
  //: `/btw` 的浮窗状态 —— `{question, loading, answer, error}`；`null` = 没开。
  //:  内容不进 messages、不落库：关掉就没了（`close` 直接置空）。
  const [btw, setBtw] = useState(null);
  //:首启引导。`replay` = 从设置页「重新运行新手引导」打开（纯客户端，
  //:不碰 `data/.onboarding-done`）。`onboardChecked` 保证只问后端一次。
  const [onboard, setOnboard] = useState({ open: false, replay: false });
  const onboardChecked = useRef(false);
  //: 「手动切到自动」那一次的安全提示弹窗开着没有。
  //:它是一次确认，不是状态 —— 关掉就是 `false`，不落任何存储。
  const [autoNotice, setAutoNotice] = useState(false);
  //:切「完全权限」前的那一次确认。
  //:与自动模式那个分开一格：它的「不再提醒」是有意不存在的。
  const [fullNotice, setFullNotice] = useState(false);
  //:接管状态 —— 后端是唯一真相源（每 5 秒跟着 `/api/status` 一起拉；
  //:   开着时另加一条 1.5 秒的短轮询，好让"物理输入退出"尽快反映到横幅与按钮上）。
  //:界面不许自己记一个 `enabled` 本地真值。
  const [takeover, setTakeover] = useState({ enabled: false });
  //:`task.end` 的 SSE 闭包读不到最新的 `takeover`（订阅只建一次）——
  //:   用 ref 镜像一份（同 `groupModeRef` / `ctxOpenRef` 那套）。
  const takeoverOnRef = useRef(false);
  useEffect(() => { takeoverOnRef.current = !!takeover.enabled; }, [takeover.enabled]);
  //:那张警告弹窗开着没有（不是状态：关掉就是 false，偏好另存 localStorage）。
  const [takeoverNotice, setTakeoverNotice] = useState(false);
  //:说明页：安全弹窗里点《…说明》，同一张卡片变大，
  //:左上角「返回」回到弹窗；点空白直接退出（连同弹窗）。
  //:   设置页那边的入口用自己的 `helpView`（子页，形状同记忆库/日志）。
  const [helpDoc, setHelpDoc] = useState("");
  //:左栏右键「删除群聊」的二次确认：`null` = 没有在问。
  const [confirmGroup, setConfirmGroup] = useState(null);
  //:撤回排队消息时放回输入框的东西；`seq` 递增用来触发 Composer 那一条 effect。
  const [prefill, setPrefill] = useState(null);
  //: 右侧抽屉开着哪一格（`""` = 关着）。
  const [drawer, setDrawer] = useState("");
  //:模型连接重试的状态 —— 显示在发送框上方那条横幅上（不改用对话流里另起一块）。
  //:   `null` = 没在重试。事件即时更新；`/api/status.llm_retry` 是重连后的兜底。
  const [llmRetry, setLlmRetry] = useState(null);
  //:事件流断没断（`stream.state`）。断了的时候步骤流不会来 ——
  //:   而它是"静默"的那种失败：回复照样由 POST 带回，只有活动块凭空少掉。
  //:，必须在界面上说一句（没出声与没跑要分得开）。
  const [streamDown, setStreamDown] = useState(false);
  //:压缩状态：`""` / `"running"` / `"failed"`。
  //:`running`，黄横幅 + 暂停输入框；`failed`，红横幅（压过还超）。
  //:   来源只有一条事件流（`compact.progress`），手动 `/compact` 与超长自救共用。
  const [compactState, setCompactState] = useState("");
  //:压缩从哪一刻开始的（黄横幅上那个 `xxS` 的起点）。
  //:   `0` = 没在压 —— 横幅那一格自己判空。
  const [compactSince, setCompactSince] = useState(0);
  //:压缩收尾那句话（完成 / 降级 / 熔断各一句），以及 done 态的自动收起。
  const [compactMessage, setCompactMessage] = useState("");
  const compactDoneTimer = useRef(0);
  //:轨迹按钮只在开关打开时存在。
  //:，开着那一格时把开关关掉，抽屉要自己退场 —— 否则它会挂在一格
  //:   已经不在栏上的面板上（点了没反应那种）。
  useEffect(() => {
    if (drawer === "trace" && status && status.trace_enabled === false) {
      setDrawer("");
    }
  }, [drawer, status && status.trace_enabled]);   // eslint-disable-line
  //:正在哪个群里聊天（`null` = 普通对话）。
  //:它只决定一件事：这一轮发出去的 `group=` 那一格（见 `send()`）。
  //:会话键由服务端从它推（`_web_session(group)`）—— 前端不拼键。
  const [chatGroup, setChatGroup] = useState(null);
  //:浮窗那两个请求要问"哪条会话"（`/api/approval?group=`）。
  //:用 ref 而不是把 `chatGroup` 塞进 `loadApproval` 的依赖：那个
  //:`useCallback` 被只跑一次的 SSE 订阅捕获（`[client]` 那个 effect）
  //:，依赖一变它还是旧闭包，`approval.request` 那一路永远问私聊会话，
  //:而开屏那一拉是对的（症状：切进群的那一刻能看见，之后再也不刷新）。
  const chatGroupRef = useRef(chatGroup);
  useEffect(() => { chatGroupRef.current = chatGroup; }, [chatGroup]);
  //:群 id 的标量形态 —— 只给 effect 依赖用（对象每次都是新的，依赖它
  //:   会让下面那条"进群就重拉一次待审批"每次都重跑）。
  const chatGroupId = chatGroup ? chatGroup.id : "";
  //:这个群的主理人（开屏头像用）。没有主理人，`null`（不开屏）。
  const chatHost = chatGroup
    ? ((chatGroup.members || []).find((m) => m.host) || null)
    : null;
  //:无主理人群聊（成员直接发言）—— 这一档不展示单 agent 的"步骤流"，
  //:改成成员工作状态（谁在被派活 / 谁完成了）。
  //:有主理人的群仍用活动块（主持人替团队干活，步骤流是它的工作记录）。
  const groupMode = !!chatGroup && !chatHost;
  const [groupWork, setGroupWork] = useState(null);
  const groupModeRef = useRef(groupMode);
  useEffect(() => { groupModeRef.current = groupMode; }, [groupMode]);
  //:群会话的历史 —— `{storage_id, has_more}`。
  //:   进群时拉一次得到（`?group=<群 id>`，键与存储 id 都由服务端推）；`null` = 不在群里。
  //:它与 `viewing` 不是一回事：`viewing` 管的是「同一段会话里的哪一条」，
  //:群是另一个会话（另一条 `storage_id`），单独一格，别塞进 `viewing`
  //:   （同 `send()` 里 `body.group` 排在 `draft`/`viewing` 之前那条判据）。
  const [groupHist, setGroupHist] = useState(null);
  //: ── 上下文读数 ────────────────────────────────────────────────
  //:`ctx` = 顶栏那一行的载荷（来自 `context.state` 事件，每轮一条）；
  //:`ctxReport` = 面板那份明细（`GET /api/context`，点开才拉）。
  //:两个不是一个量：事件那份是"这一轮收尾时的读数"，面板那份是"此刻的构成"
  //:    —— 面板可以随时重拉，而顶栏那行只在轮末更新。
  //: ── 被屏蔽插件的弹窗队列 ──────────────────────────────────────
  //:用队列而不是单个：一次可能屏蔽了两个包（比如两台 MCP 服务器同时挂），
  //:   而弹窗要一个一个来（同时叠两个 = 用户只看得见上面那个）。
  const [blockedQueue, setBlockedQueue] = useState([]);
  const [ctx, setCtx] = useState(null);
  const [ctxOpen, setCtxOpen] = useState(false);
  const [ctxReport, setCtxReport] = useState(null);
  //: ── 后台任务 / 子代理─────────────────────────────
  //:`tasks` = 现拉的 running 列表（`GET /api/tasks`）；`tasksOpen` = 哪个浮窗开着
  //: （`"bg"` 后台任务 / `"sub"` 子代理 / `""` 都没开）。
  //:两个计数只在 >0 时渲染 —— 它们是状态不是入口
  //:   （与「上下文 —」相反：那一条没读数也必须留占位，否则面板进不去）。
  //:读不到时存 `{error}`、两个计数都不显示 —— 别把"读不到"画成一个 0。
  const [tasks, setTasks] = useState(null);
  const [tasksOpen, setTasksOpen] = useState("");
  //: 「现在有没有可转后台的前台任务」——由 `tool.start.backgroundable` 维护。
  //:用 ref 而不是 state：Ctrl+B 的 keydown 订阅只建一次，读 state 会拿到旧值。
  const bgableRef = useRef(new Set());
  const end = useRef(null);
  //:自动跟随只跟"还贴着底"的时候（用户）：
  //:   往上翻看历史时，模型吐一个字就把他拽回底部 —— 读一页被打断十次。
  //:`conv` = 滚动容器；`stick` 用 ref（滚动事件与 effect 要读"此刻"的值）；
  //:`atBottom` 是同一件事的渲染副本（只用来决定那颗「回到底部」按钮显不显示）。
  //:判据在 `scroll.js::isNearBottom`（纯函数，node 有断言）。
  const conv = useRef(null);
  const stick = useRef(true);
  //:上一个 `scrollTop` —— "内容变高"与"用户往上滑"靠它分开
  //:   （`scroll.js::nextStick`）。步骤流一次冒出几行长命令时，内容变高会让
  //:"离底距离"瞬间变大；只按距离判，就会把它当成"用户滑走了"
  //:   自动跟随被关掉、按钮冒出来 —— 而用户一根手指都没动。
  const scrollState = useRef({ stick: true, lastTop: 0 });
  const [atBottom, setAtBottom] = useState(true);

  //:外面那坨「还没定性的文字」用 ref 镜像一份 —— `tool.start` 要读当前值再决定搬不搬，
  //:而 `setState` 的更新函数里读不到"刚 set 进去的那个值"。别把它简化掉。
  const provRef = useRef("");
  //: 「此刻有没有一个活着的活动块」—— 由 `messages` 派生（见下面那个 effect）。
  //:用 ref 而不是 state：`delta` 要在两次渲染之间读它，读 state 会拿到上一帧的旧值。
  const actRef = useRef(false);
  //: 「这一轮改了哪些文件」—— `files.changed` 在 `task.end` 之后到，所以先存一份兜底。
  const pendingFiles = useRef(null);
  //: 「面板开着没有」也用 ref 镜像一份 —— `task.end` 的订阅闭包拿不到新值
  //:   （那个 effect 的依赖只有 `client`），读 state 会读到订阅那一刻的旧值。
  //:同 `provRef`/`actRef` 那两条，别把它"简化"成 state。
  const ctxOpenRef = useRef(false);
  function setProv(v) { provRef.current = v; setProvisional(v); }

  //:开屏先把存过的配色挂上。
  //:   GUI 自己的 localStorage（`lv.colors`）—— 不碰后端。
  //:没存过，`applyTheme({})` 一个字节都不注入，用的就是样式表原值
  //:   （这是"默认观感一个像素都不变"的保证，见 `theme.js::themeCss`）。
  useEffect(() => {
    if (typeof localStorage === "undefined") return undefined;
    const mql = typeof matchMedia === "function"
      ? matchMedia("(prefers-color-scheme: dark)") : null;
    const paint = () => {
      const resolved = resolveMode(loadMode(localStorage), mql);
      applyThemeMode(resolved);
      applyTheme(loadTheme(localStorage, resolved), document, resolved);
    };
    paint();
    //:把模式同步给壳：主进程据此设 `nativeTheme.themeSource` ——
    //:窗口底色与启动小窗（另一个 origin，读不到这里的 localStorage）都跟着走。
    try { window.crannia?.setThemeMode?.(loadMode(localStorage)); } catch { /* 浏览器里没有壳 */ }
    //:跟随系统时系统换了主题要跟着换（其余两档与系统无关，回调里自己会判）。
    if (!mql) return undefined;
    const onChange = () => { if (loadMode(localStorage) === "system") paint(); };
    try {
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    } catch {
      mql.addListener(onChange);                     // 老内核
      return () => mql.removeListener(onChange);
    }
  }, []);

  /** 活动块现在是 `messages` 里的一个条目（`role:"activity"`），不是浮在底部的元素。
   *
   *  为什么必须这样：`task.end` 之后它要缩成小条留在原地、回复另开一个框 ——
   *  而浮在底部的元素排不到「问」与「答」之间，它会跑到回复下面去。
   *
   *  只动最后一条、且还活着的活动块；历史里已完结的那些不许被碰到。
   *  `create`：没有活块时要不要兜底起一个（`tool.start`/`say` 要，`delta`/`tool.end` 不要 ——
   *  后者在 `task.end` 之后到达是常态，起一个就等于在回复后面复活一个空框）。
   */
  /** 把一条新的活动块插进 `messages` —— 落点判据在
   *  `message-order.js::activityAnchorIndex`（纯函数，node 逐条断言）。
   *
   *  为什么不是 `concat`（回答中间会凭空冒出一个
   *  「✓ 已完成 0.0s」）：SSE 与 HTTP 是两条通道，回复可能先到
   *  （`pushAssistant` 把它追加在末尾），而 `task.start` 晚一步到
   *，直接 concat 就把块放到了回答后面。
   *  而"最后一条用户消息之后"也不够：有些轮次没有新的用户消息
   *  （审批 / 提问面板的回答、后台任务回报……），旧判据把块插进上一轮
   *  （步骤流会出现在"前一个步骤流那里"）。判据现在是
   *  「末尾那串 assistant 里谁还没落库」—— 详见那个函数的 docstring。
   */
  function insertActivity(x, block) {
    const at = activityAnchorIndex(x);
    if (at >= x.length) return x.concat([block]);
    return x.slice(0, at).concat([block], x.slice(at));
  }

  /** 把所有已有的活动块折叠（`foldToken` +1，`ActivityBlock` 收起）。
   *  用户追问之后上一个框要折起来，不许占用屏幕。 */
  //:同一时刻最多只有一个步骤框是展开的，而开合状态由这一层持有。
  //:不用"块自己的 state + 发一个 foldToken 信号"那一版 —— 那要块收到
  //:   信号才折，而信号走 props 更新：某次渲染没带上（或块被重新挂载）它就
  //:静静地不折。状态提上来之后，`setOpenAct("")` 是一次真实的 state 变更。
  const [openAct, setOpenAct] = useState("");

  function editActivity(fn, { create = false } = {}) {
    setMessages((x) => {
      // 收口要找的是「最后一块还活着的活动块」，不是"最后一条消息"。
      //    两条通道（SSE 的 task.end 与 POST /api/chat）到达顺序不定：
      //    POST 那条是先追加回复、再收口，收口那一刻"最后一条"已经是那条回复，
      //    按位置找会永远什么都不做，块一直 `live`，转轮一直转、计时器一路涨。
      //    判据用身份（`live:true` 就是那一轮的身份），不用位置：
      //    历史里已完结的块 `live` 都是 false，所以倒着找仍然只动这一轮的块。
      let i = -1;
      for (let k = x.length - 1; k >= 0; k -= 1) {
        if (x[k].role === "activity" && x[k].live) { i = k; break; }
      }
      if (i >= 0) { const next = x.slice(); next[i] = fn(x[i]); return next; }
      if (!create) return x;
      return insertActivity(x, fn({
        id: "act" + Math.random(), role: "activity", live: true,
        //:`stepLabel` / `stepStartedAt` / `running` 三格是活动头的数据源
        //:   （「当前这一步 + 这一步的用时」，`ActivityBlock.jsx` 里画）。
        //:`running` 是计数不是布尔 —— 一批只读工具是并发跑的（见 `tool.end`）。
        startedAt: Date.now(), stepLabel: t(THINKING), stepStartedAt: Date.now(),
        running: 0, lines: [], draft: "",
      }));
    });
  }

  //:回复有两个来源，必须去重：
  //:   · SSE 的 `task.end.response`（事件流那条通道）
  //:   · HTTP `POST /api/chat` 的 `reply`（请求 / 响应那条通道）
  //:它们是两条通道，谁先到不定，`sawTaskEnd` 标志只挡住"事件流先到"
  //:那一半，POST 先回时两个都加，用户看到同一句话两遍。
  //:，判据是按内容去重：相邻的两条助手消息同文就丢一条。
  //:这不会误吞"用户真的收到两次同样的回复" —— 那种情况中间隔着一条 user 消息，
  //:   不相邻（这条判据是"相邻"，不是"出现过"）。
  //:
  //:上面那条判据对超长回复必然失效，而失效的方式不是随机的：
  //:   SSE 那条过了 `core/events.py` 的 `_TEXT_CAPS["response"] = 4000`，
  //:被截成"前 4000 字 + 截断记号"，回复一超过 4000 字，两条通道的字符串必然不同
  //:，「内容相同」永远不成立，两条都落成气泡 ——
  //:用户看到的是一条被腰斩的 + 一条完整的。
  //:
  //:修法：不再找第三条"内容比对"的判据，而是认出这两条是同一条回复 ——
  //:短的以截断记号结尾、且长的是它的严格前缀，那就是同一条的截断版
  //:，只留长的（短的在前就就地升格它，短的在后就丢掉它）。
  //:两个方向都认：两条通道谁先到不定，不能把赌注写进代码。
  //:
  //:不能照搬 WebUI 里按 `origin` 抑制 SSE 的规则
  //:     （`d.origin !== 'web' || _postInFlight === 0`）：那条会让 SSE 整条不推，
  //:可 `attachRows`（`turn_rows`）与 `attachFiles`（`files.changed`）都是
  //:"从后往前找最后一条助手消息" —— 那一刻它还没推出来，会贴到上一轮那条消息上
  //:      （`/api/chat` 的返回只有 `{reply, tokens, files}`，没有 `turn_rows`
  //:，想补也补不回来）。
  //:，要么把这两处一起延后（多一个 ref + 三处改动 + 一个新的失败窗口），要么根本不动那套机器。
  //:这里选后者：SSE 照推（行号 / 附件那套不动），POST 回来时"升格"那条气泡。
  //:反方向也稳：SSE 没到（断线 / 没连上）时 POST 那条走追加分支，回复照旧不丢。
  //:如实标：模型自己写出一段以截断记号结尾、且下一段正好是它的严格前缀的正文时，
  //:      会被误判成截断对 —— 代价是"少一条气泡"，而正常路径下这两条本来就该是一条。
  const TRUNC_MARK = "…";   // ← 后端改这个记号（`core/events.py::_cap_data`），这里要跟着改；只写一处
  //:回复的落点与去重在 `message-order.js`（纯函数，node 逐条断言）。
  //:   为什么不写在组件里：React 层没有行为读者，而"顺序"与"去重"正是会悄悄坏的那两格。

  function pushAssistant(text, extra = {}) {
    const txt = (text || "").trim();
    if (!txt) return;
    setMessages((x) => {
      // 两条通道（POST 与 SSE）到的是同一句话，先后不定，必须去重。
      //    比的是 trim 过的：同一个字符串的两次序列化，前后空白不保证逐字节相同。
      //    判据 = 同样的 trim 文本且时间相近（10 秒内）：只按文本会让
      //    "连着两轮都回了同一句话"被误吞（用户真的会连发"继续"）。
      //    扫描跳过活动块（`activity`——活动块不是"消息"，它只是步骤流）
      //      与占位消息，并且整条列表都扫（不再只看末尾相邻的那几条）
      //      —— 回复现在可能落在中间（见下面的落点规则）。
      const hit = classifyReply(x, text, extra && extra.ts, TRUNC_MARK);
      if (hit.kind === "same" || hit.kind === "drop") return x;
      if (hit.kind === "upgrade") {
        // 就地升格：只换 `content`，`id` / `ts` / `files` / `rowId` 全部留着。
        const next = x.slice();
        next[hit.index] = { ...next[hit.index], content: text };
        return next;
      }
      // 落点：第一条还没回过的 user 段之后（见 `replyAnchorIndex`）。
      const at = replyAnchorIndex(x);
      const msg = { id: "a" + Math.random(), role: "assistant",
                    content: text, ...extra };
      if (at >= x.length) return x.concat([msg]);
      return x.slice(0, at).concat([msg], x.slice(at));
    });
  }

  /** 无主理人群聊的多条成员回复。
   *
   *  它们不是"主持人总结"，而是每条独立可见、带名字与头像的成员消息。
   *  走 `group_replies` 这一支时不许再 `pushAssistant(result.reply)` ——
   *    那会凭空多出一条"主持人说的话"（标签在说谎）。
   */
  function pushGroupReplies(replies) {
    if (!Array.isArray(replies) || !replies.length) return;
    //:群聊每位成员各带各的产物，大的也各说各的（同一条去重键）。
    for (const r of replies) notifyLargeArtifacts(r && r.artifacts);
    // 圆桌的直播气泡（`group.member.done` 带正文）
    //   只是预览 —— 权威那份到了就整批丢掉，避免同一段话出现两次。
    setMessages((x) => x.filter((m) => !m.streamGroup).concat(replies.map((r) => ({
      id: "g" + Math.random(),
      role: "assistant",
      content: r.content || "",
      speaker: r.name || null,
      avatar: (r.avatar != null ? r.avatar : null),
      kind: r.kind || "group_speaker",
      error: !!r.error,
      // 每位成员的产物挂在自己那条回复上（后端 `group_replies[]`
      //   每项各带各的 —— 一个成员画了图，另一个没画，不能混成一堆）。
      artifacts: (Array.isArray(r.artifacts) && r.artifacts.length) ? r.artifacts : null,
      ts: Date.now() / 1000,
    }))));
  }

  /** 「本轮改动文件」要回头贴到那条回复上。
   *
   *  判据是事件的先后：`files.changed` 在 `task.end` 之后才发，
   *  那时回复已经推进 `messages` 了，只能从后往前找第一条助手消息补上去。
   *  `pendingFiles` 是兜底（万一后端把顺序调了，`task.end` 也会把它带上）。
   *  已经贴过就不动 —— 一轮只会有一条 `files.changed`，但重入的要防。 */
  function attachFiles(payload) {
    setMessages((x) => {
      for (let i = x.length - 1; i >= 0; i--) {
        if (x[i].role !== "assistant") continue;
        if (x[i].files) return x;
        const next = x.slice();
        next[i] = { ...x[i], files: payload };
        return next;
      }
      return x;
    });
  }

  /** 把 `task.end` 带来的行号贴回那两条消息上。
   *
   *  `rows` = `{user: <行号>, assistant: <行号>}`（后端 `post_process` 从
   *  `save_messages` 的返回值收出来的）。拿不到就是 `undefined` —— 后端
   *  刻意发 `None` 而不是 `{}`（读不到 ≠ 空），所以这里用 `if (!rows)`
   *  一次挡住 `null` / `undefined` / `{}` 三种。
   *
   *  从后往前找「还没有行号的」那一条，不是"最后一条"：
   *    · 审批那一轮（`answer()`）不推用户气泡，"最后一条用户消息"会是
   *      上一轮的，贴上去就把旧消息的行号改坏了；
   *    · 加上 `!rowId` 之后，已经贴过的那条会被跳过 —— 每条消息一生只贴一次。
   */
  function attachRows(rows) {
    if (!rows) return;
    setMessages((x) => {
      const next = x.slice();
      let ai = -1;
      for (let i = next.length - 1; i >= 0; i--) {
        //:回执不是消息（不落库），它没有行号，也不许被贴上一个
        //:   （否则它会被当成"那一轮的回复"，收藏 / 行号引用都会指错）。
        if (next[i].role === "assistant" && !next[i].rowId
            && !isReceipt(next[i])) { ai = i; break; }
      }
      //:`users` = 这一轮全部 user 行的行号（起一轮可能有两句：
      //:原提问 + 跑着的时候补的那句）。按顺序倒着贴给"还没有行号"的那几条。
      if (Array.isArray(rows.users) && rows.users.length) {
        let k = rows.users.length - 1;
        for (let i = next.length - 1; i >= 0 && k >= 0; i--) {
          if (next[i].role === "user" && !next[i].rowId) {
            next[i] = { ...next[i], rowId: rows.users[k] };
            k -= 1;
          }
        }
      } else {
        //: 老形状（只有 `user`）：最后一条还没有行号的 user。
        let ui = -1;
        for (let i = next.length - 1; i >= 0; i--) {
          if (next[i].role === "user" && !next[i].rowId) { ui = i; break; }
        }
        if (ui >= 0 && rows.user != null) next[ui] = { ...next[ui], rowId: rows.user };
      }
      if (ai >= 0 && rows.assistant != null) next[ai] = { ...next[ai], rowId: rows.assistant };
      return next;
    });
  }
  //:活动块的「活着」状态从 `messages` 派生 —— 每次渲染后同步一次，`delta` 读它。
  //:判据取最后一条不是回执的消息：`cancel_receipt` 排在活块后面时，
  //:   按"最后一条"判会让活块永远不算"最后"（变成假「正在处理」的同一个根）。
  useEffect(() => {
    const i = lastNonReceipt(messages);
    const last = i >= 0 ? messages[i] : null;
    actRef.current = !!(last && last.role === "activity" && last.live);
  }, [messages]);

  //:通知是一个最多 3 条的栈（用户「多条通知出现的时候，
  //:   是会顶掉还是叠在一起？不要有bug」）。
  //:
  //:旧实现是单槽：后一条把前一条顶掉，而同一条文本再来一次时
  //:`setToast(同值)` 是 no-op，那个 2.8 秒的定时器不重置，第二条明明
  //: 发生了、提示却按第一条的时钟提前消失。那是真 bug，不是观感问题。
  //:
  //:现在：每条带 `id` 与 `at`；同样的文本再来一次 = 把它挪到最下面并重置时钟
  //: （不重复堆同一句）；超过 3 条丢最旧的（栈太高会盖住内容）。
  const notify = useCallback((text) => setToasts((xs) => pushToast(xs, text)), []);
  //:大产物只说一次：`task.end` 与 `/api/chat` 两条路
  //:   都会把同一批产物送到这里（SSE 先到、POST 后到），而它们带的记录
  //:可能各是一份，按 `id` 记账，同一件东西不弹第二遍。
  //:判据住在 `artifacts-view.js`（`needsLargeNotice`），这里只负责去重与措辞。
  const bigSeen = useRef(new Set());
  const notifyLargeArtifacts = useCallback((arts) => {
    for (const rec of Array.isArray(arts) ? arts : []) {
      if (!rec || !needsLargeNotice(rec)) continue;
      const key = String(rec.id || rec.name || "");
      if (!key || bigSeen.current.has(key)) continue;
      bigSeen.current.add(key);
      notify(t("「{name}」有 {size}，已保存在本地", {
        name: rec.name || rec.id, size: formatSize(rec.size),
      }));
    }
  }, [notify]);
  //:一条每 250ms 扫一次的钟（不是每条一个定时器）：少一层"谁忘了清定时器"。
  //:只在栈非空时挂着 —— 空栈挂钟是纯税。
  useEffect(() => {
    if (!toasts.length) return undefined;
    const timer = setInterval(() => setToasts((xs) => pruneToasts(xs)), 250);
    return () => clearInterval(timer);
  }, [toasts.length]);

  // ── 收藏夹 ───────────────────────────────────────────────────
  //
  // 聊天区能点，靠的是两条各做一半：
  //   · 后端在 `task.end` 里告诉我们"这一轮落库后是哪两行"
  //   · 这里把那两个行号贴回消息上（`attachRows`），才有东西可引用
  // 没有行号时消息没有任何身份 —— 不是"没做按钮"，是做不出来
  //   （收藏存的是引用）。
  const [starred, setStarred] = useState(() => new Set());
  //:收藏夹点条目跳过来的落点（`{storage_id, message_id}`）。
  const [jump, setJump] = useState(null);

  const refreshStars = useCallback(async () => {
    try {
      const d = await client.call("/api/stars?limit=500");
      setStarred(new Set((d.stars || []).map((s) => s.message_id)));
    } catch {
      // 拿不到就不点亮、也不弹错 —— 收藏夹坏了不该让聊天区报红
    }
  }, [client]);
  useEffect(() => { refreshStars(); }, [refreshStars]);

  const toggleStar = useCallback(async (messageId) => {
    const on = starred.has(messageId);
    try {
      await client.call(on ? "/api/stars/delete" : "/api/stars", {
        method: "POST", body: JSON.stringify({ message_id: messageId }),
      });
      await refreshStars();
    } catch (e) {
      notify(e.message || String(e));
    }
  }, [client, starred, refreshStars]);

  // ── 侧栏「任务」：列出旧对话 + 点一条装载进聊天区 ──────────────
  //
  // 三条会话语义：
  //   · 开屏（冷启动）＝ 草稿，第一次发送开一段新的（`draft`）。
  //   · 点旧对话 ＝ 看那一段；再发消息 ＝ 接着聊那一段（不是另起一段）。
  //   · 横幅那个按钮叫「回到当前对话」，只在真有活跃对话时出现。
  //
  // `is_current` 不参与"要不要先清"那个判断 —— 那会让旧对话一发新消息、
  //   旧历史就没了（见 `send()` 的注释）。
  const [threads, setThreads] = useState([]);
  //:左栏单独一栏「群聊」，条目显示群聊名字，点一下进群。
  //:数据源仍是 `/api/groups`（不新造）；`Groups` 页每次加载完会把
  //:   最新列表回传（`onGroupsChanged`），避免两处各自轮询。
  const [sideGroups, setSideGroups] = useState([]);
  useEffect(() => {
    let alive = true;
    client.call("/api/groups")
      .then((d) => { if (alive) setSideGroups((d && d.groups) || []); })
      .catch(() => { /* 后端没起来：左栏不显示群聊，不弹错（同侧栏那几条） */ });
    return () => { alive = false; };
  }, [client]);
  //:拿名册失败过（后端还没起来 / 请求挂了）。
  //:它与"名册是空的"必须分得开 —— 两者都渲染成「还没有会话历史」时，
  //:失败那句是假的（真相是"我没查到"）。
  //:   "没读数"不等于"可以清空"。
  const [threadsError, setThreadsError] = useState("");
  //:那一格红点（未读）与「排队中」。
  //:
  //:纯前端状态，判据：per-session · 只在"跑完了 + 当前不是它"时置位 ·
  //:在它成为当前视图时清掉。
  //:采用「按完成事件记未读」的模型 —— 这里记的是"那一次完成还没看过"，
  //:而不是一个粘性的"看过没"位；"看过"= 它成了当前视图，清掉那一格。
  //:不落库（跨重启不保留）—— 落库要多一张表 + 一条
  //:     `state_registry` 登记，而"重启后还记着上次没看的那条"对单用户没用。
  const [unread, setUnread] = useState({});          // storage_id -> true
  const [queued, setQueued] = useState({});          // storage_id -> true
  //:名册总数（`/api/threads` 的 `total`）—— 50 条提醒那一格要它。
  //:`null` = 读不到，不说话（不是"0 条"：读不到 ≠ 0）。
  const [threadTotal, setThreadTotal] = useState(null);
  //:置顶（「置顶此对话 / 此集」）。
  //:`threads` = 置顶的 `storage_id`（先置顶的在前）· `groups` = 置顶的目录路径。
  //:它跟名册同一趟取回来（`/api/threads` 的 `pinned_threads`/`pinned_groups`）
  //:   —— 分两趟的话，"名册到了、置顶还没到"那一瞬间列表会跳一下。
  const [pinned, setPinned] = useState({ threads: [], groups: [] });
  //:上面那两格要用的镜像 —— `task.end` 那个订阅闭包拿不到最新 state。
  const pinnedRef = useRef({ threads: [], groups: [] });
  //:开屏那三条淡入必须等窗口真的可见了再放。根因不在动画快慢：
  //:桌面壳是 `ready-to-show` → 等够 `MIN_HOLD_MS`(500ms) → 才 `show()`，
  //:而页面早在 `ready-to-show` 之前就渲染了，那条 ~1.2 秒的错开淡入
  //:在窗口还没显示的时候就已经播完。
  //:判据用 `visibilitychange`（壳 `show()` 那一刻会触发），
  //:不是猜一个延迟毫秒数 —— 猜的那个数得跟着 `MIN_HOLD_MS` 走，
  //:而它住在另一个文件里，改了没人会记得回来改这里。
  //:浏览器里 `visibilityState` 一开始就是 `visible`，行为一个字不变。
  const [shown, setShown] = useState(
    () => typeof document === "undefined" || document.visibilityState !== "hidden");
  useEffect(() => {
    const onVis = () => { if (document.visibilityState !== "hidden") setShown(true); };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);
  //:左栏批量删除。
  //:   `null` = 不在批量态；否则 `{sel: {storage_id: true}, confirm: false}`。
  //:它是纯界面态，不进 `state_registry` —— 它不跨请求、也不落库
  //:   （`state_registry` 普查的是 `core/`+`bus/` 里"活过一次请求"的模块级状态）。
  const [batch, setBatch] = useState(null);
  //:上面那两格要用的三个镜像（`task.end` 那个订阅闭包拿不到最新 state）：
  //:   · `thread`   —— 这一刻正在看哪一段（`viewing` 的镜像）
  //:   · `awaitingNew` —— 刚有一条草稿态的发出去还在飞（那一段的 id 还没铸）
  //:   · `inflight` —— 还在飞的那几条发送分别是哪一段（给「排队中」用）
  const visibleRef = useRef({ thread: "", awaitingNew: false });
  const inflightRef = useRef(new Set());
  //:还在跑的轮次数（send / 审批回答）。`busy` 不许被"先回来的那一条"关掉 ——
  //:两条并发时，登记在飞的是计数，不是一个布尔。
  const inflightTurnsRef = useRef(0);
  //:本地排队：跑着的时候再发一条，先不 POST —— 它还没到服务端，
  //:   所以撤得回来（"发出去再取消"做不到这件事）。按提交顺序排队，先来先发。
  const heldSendsRef = useRef([]);
  //: 撤回时要放回输入框的东西（`msgId -> {text, images, sentTo}`）。
  const heldSendDataRef = useRef({});
  //:等回执的 steer：`client_msg_id → {msgId, text, images, files}`。
  //:   `steer.delivered` 摘牌；`task.end.steer_leftover` 用它兜底（同一条气泡、
  //:接着跑一轮）—— 两种出口都必须有，否则那句话要么永远挂着、要么凭空消失。
  const steerWaitRef = useRef(new Map());
  //:最新那一份 `send`（`useEffect(..., [client])` 只建一次订阅
  //:里面的回调是首帧的闭包，那时 `connected === false`、`status === null`
  //:，直接在里面调 `send` 会一律 `notify("请先运行 python -m web")` 然后 return
  //:   ——「消息结算完成时弹了个通知『请先运行 python web…』」，
  //:而那句 steer 就永远挂着。订阅回调一律走 `sendRef.current`）。
  const sendRef = useRef(null);
  //:每次渲染都刷新（`send` 是函数声明，提升，这里拿到的是本次渲染那一份）。
  sendRef.current = send;
  //:这一轮跑在哪一段（`send()` 记，`task.end` 清）。
  //:   不判归属的话：SSE 事件按通道键广播（私聊全塌成 `workspace:private`），
  //:一轮在 A 里跑着、用户点开左栏另一段，那一屏会凭空长出「正在处理」
  //:   + 没有框的步骤流。
  //:`""` = 不知道（草稿态刚发出去 / 别的通道起的轮），照旧渲染。
  const liveSidRef = useRef("");
  //:刚发出去的那一条属于哪一段（服务端把它标成"当前"之前，名册还是旧值）。
  //:没有它时：resume 一段旧对话、立刻发一句，`viewKey`（取 `threads.is_current`）
  //:还是上一段的 id，而 `liveSidRef` 是这一段，`_liveMine()` 判假
  //:这一轮不建活动块（用户「追问后没有新框，只有正在处理」）。
  const sentSidRef = useRef("");
  //:这一屏的身份。根因：`visibleRef.thread` 只镜像 `viewing`（群聊 / 活跃态
  //:恒为 `""`），而 `liveSidRef` 在群聊发送时记的是当前私聊的 id
  //:，归属判据两头都对不上。
  //:，新增这一屏的身份（与 `visibleRef.thread` 分开 —— 后者必须保持
  //:   "真实 storage_id"，`task.end` 要拿它比 `d.storage_id` 置红点）：
  //:   群 = `group:<id>` · 看历史 = storage_id · 草稿 = `draft` · 活跃 = 当前段 id。
  const viewKeyRef = useRef("");
  //: 「这一屏是草稿（开屏 / 新建任务），发送时先开一段新的」。
  //:它与 `viewing === null` 不是同一件事：「回到当前对话」之后也是
  //:`viewing === null`，但那一屏是已有的一段对话，发送要追加进去。
  //:，少这一个标志，那两种屏就长得一样而语义相反。
  const [draft, setDraft] = useState(true);
  //:草稿态选的工作目录。
  //:草稿态还没有 `storage_id`（那要等新对话铸出来），选的值只能
  //:拿在手里，跟着第一条 `/api/chat` 的 `body.workspace` 一起交出去
  //:   （后端收它的地方：`web/server.py:3023`）。
  //:语义是「发送即锁」：发送后清掉，它已经落到那一段上了。
  const [wsPath, setWsPath] = useState("");
  //: 正在看哪一段历史（`null` = 不在查看态）。
  const [viewing, setViewing] = useState(null);
  const [viewHasMore, setViewHasMore] = useState(false);
  const [viewLoading, setViewLoading] = useState(false);
  //:删除走右键菜单，不在行里放垃圾桶。
  //:   `null` = 没开；否则 `{x, y, t, confirm}`（`confirm` = 已点过一次、等确认）。
  //:仍然要确认一次 —— 它是不可逆的（与 `/clear` 正相反）。
  //:   [!] 菜单是屏幕坐标（`position:fixed`），要夹在视口里，否则靠边的行
  //:   一右键，菜单就跑到屏幕外面去（看不见 = 点不到）。
  const [ctxMenu, setCtxMenu] = useState(null);
  const [resizing, setResizing] = useState(false);
  //:左栏与主界面之间可拖动。
  //:   拖动只改这一个数（写进 CSS 变量 `--side-w`），另两列一个字不动。
  //:   [!] 夹在 [180, 520] —— 太窄标题全被 ellipsis 吃掉、太宽主区没地方。
  const [sideW, setSideW] = useState(() => {
    let v = 0;
    try { v = Number(localStorage.getItem("lv.sideW") || 0); } catch { v = 0; }
    return v >= 180 && v <= 520 ? v : 240;
  });

  /** 左栏拖动（`pointer` 事件：鼠标 / 触控板 / 触屏一套走完）。
   *
   *  [!] 要在 `window` 上听 move/up，不在那根 6px 的条上 —— 鼠标一快就出条，
   *  挂在条上的话拖到一半就断了。
   *  [!] 只在 `up` 时落盘：拖动过程每帧写一次 localStorage 是没必要的抖动。
   *  [!] `setPointerCapture` 包在 try 里 —— 没有它也能拖，别为一个增强把主路弄挂。
   */
  function startResize(e) {
    e.preventDefault();
    // 拖动中这个状态是必要的，不是好看：指针一会儿就离开那根窄条了，
    //   只靠 `:hover` 的话拖到一半高亮就没了（看着像断线）。
    setResizing(true);
    const move = (ev) => setSideW(Math.min(520, Math.max(180, ev.clientX - 8)));
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      setResizing(false);
      setSideW((w) => {
        try { localStorage.setItem("lv.sideW", String(w)); } catch { /* 隐私模式 */ }
        return w;
      });
    };
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* 增强，不是主路 */ }
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  const refreshThreads = useCallback(async () => {
    try {
      // 页长取自 `settings-view.js` 那个常量 ——
      //   设置页那一行会把同一个数念给用户听，两处各写死一份就会各自漂。
      const d = await client.call(`/api/threads?limit=${THREAD_LIST_LIMIT}`);
      setThreads(d.threads || []);
      // 总数单给一格（那一页推不出总数）—— 读不到就是 `null`。
      setThreadTotal(d.total == null ? null : Number(d.total));
      // 置顶跟名册同一趟到（读不到，两张空表 = 谁都不置顶 = 今天的行为）。
      const _pn = { threads: d.pinned_threads || [], groups: d.pinned_groups || [] };
      setPinned(_pn); pinnedRef.current = _pn;
      setThreadsError("");
    } catch (e) {
      // 不弹错（侧栏是常驻的，偶发失败不该在界面上弹红字，同 `/api/approval`
      //   那条判据）—— 但也不能装作"没有"：记下原因，让侧栏说真话。
      setThreadsError(e.message || String(e));
    }
  }, [client]);
  useEffect(() => { refreshThreads(); }, [refreshThreads]);
  //:设置也开机拉一次 —— 「50 条提醒」那条横幅要读
  //:`thread_notice_off`，而它只在打开设置弹窗时才拉
  //:，不拉的话横幅永远不知道该不该显示。
  useEffect(() => { reloadSettings(); }, [client]);
  //:"它成了当前视图"就清红点。
  //:   由 `viewing` 驱动 —— 点开那一段（或它本来就是当前那段）就算看过了。
  //:顺带把 `visibleRef` 镜像一份：`task.end` 那个订阅闭包看的是挂载那一刻
  //:      的 state（同 `ctxOpenRef` 那条判据），不镜像就会拿旧值去判"当前不是它"。
  useEffect(() => {
    const sid = viewing ? viewing.storage_id : "";
    visibleRef.current.thread = sid;
    if (sid) setUnread((u) => (u[sid] ? { ...u, [sid]: false } : u));
    //:这一屏的身份（直播事件归属判据）。群聊单独成键 ——
    //:   「在 A 群跑、切到 B 群」时两个 storage id 都读不到（群不靠 `viewing`）。
    //:判据收在 `viewKeyOf` 一处，渲染那一侧用的是同一个函数。
    viewKeyRef.current = viewKeyOf({
      groupId: chatGroup ? chatGroup.id : "",
      viewingId: viewing ? viewing.storage_id : "",
      draft,
      //:刚发出去的那一段优先（服务端还没把它标成当前时，名册是旧值）。
      currentId: sentSidRef.current || ((threads.find((t) => t.is_current) || {}).storage_id || ""),
    });
  }, [viewing, chatGroup, draft, threads]);
  // 名册要等跑完一轮才出现：`/api/threads` 在框架没起来时返回 503，
  //   而挂载那一刻框架通常还没起；`refreshThreads` 原先只有"挂载"与
  //   "`task.end`"两个触发点，中间那段时间侧栏一直空着，而且再没有任何地方重试。
  //   所以框架起来的那一刻补拉一次。
  //   判据是 `framework_running` 这个跳变，不是"每 5 秒刷一次"
  //     （`refresh` 那个定时器只问 `/api/status` 与 `/api/workspaces`）。
  useEffect(() => {
    if (status?.framework_running) refreshThreads();
  }, [status?.framework_running, refreshThreads]);

  /** 点侧栏一条，把它的尾部一页装载进聊天区。
   *
   *  这是"看"，不是"切过去聊" —— 装载出来的每条都带 `rowId`（库里那一行），
   *    所以它们照样能点（收藏存的就是这个行号）。
   *  而再发一条消息就接着这一段 ——
   *    判据在 `send()` 里：`viewing` 有值就把它的 `storage_id` 带上去，
   *    由服务端把这一段接上。不清、不另起。
   */
  async function openThread(th) {
    // 批量态下，点一行 = 勾 / 取消它，不是打开它。
    //   它住在这里而不是把 `onClick` 换成分支表达式 ——
    //   源码级测试用正则钉着
    //   `onClick={() => openThread(t)}` 这个形状（"每行都真的接上了名册那一条"）。
    //   所以判据搬进函数体，那一行的形状一个字不动。
    if (batch) {
      setBatch((b) => (b ? { ...b, confirm: false,
                             sel: { ...b.sel, [th.storage_id]: !b.sel[th.storage_id] } } : b));
      return;
    }
    if (viewLoading) return;
    //:点开旧对话，离开群聊（否则顶部还写着群名、
    //:   左栏那个群还亮着，而下一句话会发进群）。
    clearGroupMode();
    setViewLoading(true);
    try {
      const d = await client.call(
        `/api/messages?storage_id=${encodeURIComponent(th.storage_id)}&limit=50`);
      // 必须切到聊天页 —— 否则人站在定时任务 / 技能那一屏时点侧栏旧对话，
      //   数据加载了、却什么都没发生（看起来像"点了没反应"）。
      //   位置是判据的一部分：放在取数成功之后，与 `setMessages` 同一批渲染
      //   —— `viewLoading` 只用来禁用按钮、没有 loading 提示，
      //   所以提前切屏会先闪一眼上一条会话的内容（比"没反应"更像 bug）。
      setPage("chat");
      setMessages((d.messages || []).map(histRow));
      setViewing({
        storage_id: th.storage_id,
        title: th.title || t("（这条会话没有用户消息）"),
        is_current: !!th.is_current,
      });
      setViewHasMore(!!d.has_more);
      setDraft(false);   // 一旦看过某一段，这一屏就不再是草稿
      setProv("");
      setDrawer("");     // 点了侧栏就顺手把右边抽屉收掉（它在看的那条已经不是这个了）
    } catch (e) {
      notify(e.message || String(e));
    } finally {
      setViewLoading(false);
    }
  }

  /** 屏幕上这一段的 id —— 看历史时是它；活跃态是当前绑定那一段。
   *
   *  上翻不能只认 `viewing` —— 那会让活跃对话那一屏既没有入口、点了也没用。
   *  而"往回翻自己刚聊过的"是一件在任何一屏都成立的事，判据是"屏幕上这一段是谁"。
   */
  function onScreenThreadId() {
    // 群里：屏幕上是群会话（另一条 `storage_id`）—— 判在最前，
    //   否则会回落到私聊的当前线程（那会把群历史接到私聊上去）。
    if (chatGroup) return (groupHist && groupHist.storage_id) || "";
    if (viewing) return viewing.storage_id;
    const cur = threads.find((t) => t.is_current);
    return cur ? cur.storage_id : "";
  }

  /** 这一屏该按哪一段取数：群里走 `?group=`（键由服务端推），否则 `?storage_id=`。
   *  只有一处拼这个 query —— 上翻 / 探针两处共用。拼错一处 = 有一处读的是
   *  别的会话，而屏幕上看起来完全正常。 */
  function histUrl({ before, limit }) {
    const q = [];
    if (chatGroup) {
      q.push(`group=${encodeURIComponent(chatGroup.id)}`);
    } else {
      const sid = onScreenThreadId();
      if (!sid) return "";
      q.push(`storage_id=${encodeURIComponent(sid)}`);
    }
    if (before) q.push(`before=${before}`);
    q.push(`limit=${limit}`);
    return `/api/messages?${q.join("&")}`;
  }

  /** 往上翻加载更早的消息（游标 = 当前最上面那条的行号）。
   *
   *  不要求 `viewing` —— 活跃那一屏同样能翻。
   */
  async function olderThread() {
    if (viewLoading) return;
    const oldest = messages.find((m) => m.rowId);
    if (!oldest) return;
    const url = histUrl({ before: oldest.rowId, limit: 50 });
    if (!url) return;
    setViewLoading(true);
    try {
      const d = await client.call(url);
      setMessages((old) => (d.messages || []).map(histRow).concat(old));
      setViewHasMore(!!d.has_more);
    } catch (e) {
      notify(e.message || String(e));
    } finally {
      setViewLoading(false);
    }
  }

  /** 问一次「这一屏还有没有更早的」—— 活跃那一屏从不装载这一段（它只追加），
   *  所以 `viewHasMore` 得现问。
   *
   *  `limit=1` 只取最新一条，`has_more` 就是"它前面还有"。问不到
   *  保持不给按钮（"点了没反应"比"没有入口"更坏，见 `view-bar` 那段注释）。
   */
  async function probeViewHasMore() {
    const url = histUrl({ limit: 1 });
    if (!url) return;
    try {
      const d = await client.call(url);
      setViewHasMore(!!d.has_more);
    } catch { /* 静默：这只是"要不要画一个按钮" */ }
  }

  /** 「回到当前对话」—— 从"正在看某一段历史"回到活跃的那一段。
   *
   *  它指的是最近的一次活跃对话；
   *    没有活跃对话（近 30 分钟没有更新）就不显示这个按钮。
   *
   *  它不是"开一段新的" —— 开新的入口是左栏那个「新建任务」（见 `draft`）。
   *    它也不能只做本地清屏：点完再发消息，后端其实是追加到当前那条线程上的
   *，屏幕上像新对话、模型那边在接着聊（标签在说谎）。
   *  它必须装载那一段的内容，不能只清屏：否则屏幕上是一张白纸、
   *    而发出去的话会接在一段看不见的历史后面。
   */
  async function goCurrent() {
    const cur = threads.find((t) => t.is_current);
    if (!cur || viewLoading) return;
    setViewLoading(true);
    try {
      const d = await client.call(
        `/api/messages?storage_id=${encodeURIComponent(cur.storage_id)}&limit=50`);
      setMessages((d.messages || []).map(histRow));
      setViewing(null);
      // 不能硬写 false：那会把服务端回的 `has_more` 扔掉，
      //   于是"回到当前对话"之后上翻口永久消失。
      setViewHasMore(!!d.has_more);
      setDraft(false);   // 已有的一段对话，发送是追加进去，不是开新的
      setProv("");
    } catch (e) {
      notify(e.message || String(e));
    } finally {
      setViewLoading(false);
    }
  }

  const refresh = useCallback(async () => {
    try {
      const [s, w, tk, inc] = await Promise.all([
        client.call("/api/status"), client.call("/api/workspaces"),
        // 后端没有这个端点时不许把整条 refresh 拖死（`.catch` 单收）。
        client.call("/api/computer-use/status").catch(() => null),
        // `#02` 同理：这个端点缺席（旧后端）不许把整条 refresh 拖死。
        client.call("/api/incidents").catch(() => null),
      ]);
      setStatus(s); setSpaces(w.workspaces || []); setConnected(true);
      if (tk) setTakeover(tk);
      if (inc) setIncidents(inc.items || []);
      //:重连 / 刷新之后的兜底 —— 状态是真相（事件只是即时通道）。
      //:只在还活着的那一轮里认它：一轮已经结束（没有 live 活动块）时，
      //:一份过期的 `llm_retry` 会把红条复活在下一轮 / 空屏上。
      if (!groupModeRef.current && actRef.current) {
        setLlmRetry(s.llm_retry || null);
      }
      setSpace((old) => old || w.workspaces?.find((x) => x.primary)?.id || w.workspaces?.[0]?.id || "");
    } catch { setConnected(false); setStatus(null); }
  }, [client]);

  useEffect(() => { refresh(); const timer = setInterval(refresh, 5000); return () => clearInterval(timer); }, [refresh]);

  //:首启检查 —— 不阻塞整个外壳（桌面壳已经在等后端了，
  //:   这里再卡一次只会让开屏更慢）。失败每 2 秒重试，最多约 16 秒就放弃；
  //:   放弃之后用户仍可以在设置里「重新运行新手引导」。
  useEffect(() => {
    if (onboardChecked.current) return undefined;
    let alive = true;
    let tries = 0;
    let timer = null;
    const check = async () => {
      try {
        const r = await client.call("/api/onboarding/status");
        if (!alive) return;
        onboardChecked.current = true;
        if (r && r.done === false) setOnboard({ open: true, replay: false });
      } catch (e) {
        if (!alive) return;
        tries += 1;
        if (tries < 8) timer = setTimeout(check, 2000);
      }
    };
    check();
    return () => { alive = false; clearTimeout(timer); };
  }, [client]);

  const replayOnboarding = useCallback(() => {
    setOnboard({ open: true, replay: true });
  }, []);

  //:接管开着时短轮询 —— "物理输入退出 / 模式切走"
  //:都由后端判定，界面只负责读回来。只有开着时才加这一条（关着时 5 秒那条够了）。
  useEffect(() => {
    if (!takeover.enabled) return undefined;
    let alive = true;
    const timer = setInterval(() => {
      client.call("/api/computer-use/status")
        .then((s) => { if (alive && s) setTakeover(s); })
        .catch(() => {});
    }, 1500);
    return () => { alive = false; clearInterval(timer); };
  }, [client, takeover.enabled]);

  //:壳的顶层横幅：只有 Electron 壳有这扇门；浏览器里
  //:`window.crannia` 不存在，no-op。横幅"不抢焦点 + 不被自己截图拍到"
  //:   两条都在 `main.cjs` 里做（`focusable:false` / `setContentProtection(true)`）。
  useEffect(() => {
    try { window.crannia?.takeoverOverlay?.(!!takeover.enabled); } catch { /* 壳不在就拉倒 */ }
    return () => {
      try { window.crannia?.takeoverOverlay?.(false); } catch { /* 忽略 */ }
    };
  }, [takeover.enabled]);

  //:"为什么退了"要说出来（任何干预都会退出）。
  //:只在 `last_stop.at` 变了时提示一次；`user`（自己点的）不提示 ——
  //:   该次点击本身已经有一句 toast 了。
  const lastStopAtRef = useRef(0);
  const stopInitRef = useRef(false);
  useEffect(() => {
    const stop = takeover.last_stop || {};
    const at = Number(stop.at) || 0;
    // 第一次读到（开屏 / 重连）只记下来，不提示 —— 否则用户会看见
    //   一条几分钟前的"已退出接管"，以为刚刚发生了什么。
    if (!stopInitRef.current) {
      stopInitRef.current = true;
      lastStopAtRef.current = at;
      return;
    }
    if (!at || at <= lastStopAtRef.current) return;
    lastStopAtRef.current = at;
    if (String(stop.reason || "") === "user") return;
    const text = stopReasonText(stop.reason);
    if (text) notify(text);
  }, [takeover.last_stop]);
  //:技能页的 mutation（解封 / 卸载）之后要能重拉一次。
  //:只 `notify(...)` 的话，红标与那一行停在旧快照上，
  //:   用户看到的是"点了、说了、界面上什么都没变"（界面在说谎）。
  //:依赖里只放 `[client]`：`notify` 现在是 `useCallback([])` 稳定的
  //:   （改成通知栈时定住），进不进依赖都不会每渲染重拉；
  //:但别把 `notify` 再改回裸箭头 —— 那样这条会被每次渲染重拉一遍。
  const reloadSkills = useCallback(() => {
    client.call("/api/skills").then((x) => setSkills(x.skills || [])).catch((e) => notify(e.message));
  }, [client]);
  useEffect(() => { if (page === "skills") reloadSkills(); }, [page, reloadSkills]);
  //:浮窗一闪而过就没了：`approval.request` 与 `task.end` 同一秒发出，
  //:而轮询是 1.2 秒一次，那一次请求在 pending 产生之前就发出去了，
  //:它回来时 `pending: null`，`setApproval(null)` 把刚弹出来的浮窗打回去。
  //:，判据：异步拉取要认序号，只让最新那一次落地（迟到的旧响应不许覆盖新状态）。
  //:四处拉取（轮询 / 信号 / 开屏 / 收尾）全都走这一个口 —— 分开写就是
  //:   四个各自为政的 `setApproval`，互相打起来很难排查。
  const approvalSeq = useRef(0);
  //:同一条回答被点了两遍，模型回了两遍。
  //:   一格"这一个回答正在提交"—— 双击 / 连点直接吞掉；同时让轮询别把
  //:   刚收掉的浮窗又拉回来（服务端要到 pre_process 才结算，中间有一个窗口）。
  const answeringRef = useRef(false);
  const loadApproval = useCallback(async () => {
    //:正在提交那一下：别把它拉回来（否则浮窗在步骤流上面闪一下）。
    if (answeringRef.current) return;
    const seq = ++approvalSeq.current;
    //:问的是当前这一屏属于哪条会话（群聊那条在 `chatGroupRef` 里）。
    //:这里读 ref、不读闭包里的 `chatGroup`：这个回调被 `[client]` 那个
    //:   （只跑一次的）SSE 订阅捕获，依赖变了它也还是旧闭包。
    const gid = chatGroupRef.current ? chatGroupRef.current.id : "";
    try {
      const r = await client.call(approvalUrl(gid));
      if (seq === approvalSeq.current) setApproval(r.pending || null);   // 只有最新的算数
    } catch { /* 拉不到不弹错（同侧栏那两条）；也不动现有状态 */ }
  }, [client]);

  useEffect(() => { if (!busy) return; loadApproval(); const timer = setInterval(loadApproval, 1200); return () => clearInterval(timer); }, [busy, loadApproval]);
  //:开屏也拉一次 —— 轮询效应只在 `busy` 时跑，所以
  //:   「关掉 GUI 再打开，而服务端还挂着一条待审批」这种情况永远不会被读到
  //:，浮窗照样不出现。
  //:服务端那份 `ctx.pending_*` 是进程内的，只重启壳不重启服务时它还在。
  //:进群 / 退群也要拉一次：待审批是按会话存的（`?group=`），
  //:   切到另一个会话时上一条浮窗的状态必须换成"那一条会话"的。
  useEffect(() => { loadApproval(); }, [loadApproval, chatGroupId]);
  // 必须是块体：
  //   简写体 `() => el?.scrollIntoView(…)` 会把那个调用的返回值当成 effect 的
  //   clean-up，React 报 `useEffect must not return anything besides a function`
  //   所以卸载时抛 `destroy is not a function`，整棵树卸载 = 白屏，且终端一声不响。
  //:依赖里不许再写 `activity` —— 那个 state 已经撤了（活动块进了 `messages`），
  //:   留着它会 `ReferenceError: activity is not defined`。
  //:   `messages` 本身就覆盖了活动块的变化（它现在是其中一条）。
  //:只在"还贴着底"时才跟随（用户）。
  //:   判据用 `stick.current`（ref）而不是 state —— 这个 effect 与滚动事件之间
  //:   必须读"此刻"的值，state 会慢一帧。
  useEffect(() => {
    if (!stick.current) return;
    end.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy, provisional]);

  //:用户自己滑回底部，恢复跟随（这就是"除非他自己滑回底部"）。
  //:程序化滚动也会触发 scroll 事件 —— 那时算出来本来就贴着底，结果一致。
  const onConvScroll = () => {
    const el = conv.current;
    if (!el) return;
    const next = nextStick(scrollState.current, el);
    scrollState.current = next;
    stick.current = next.stick;
    setAtBottom((prev) => (prev === next.stick ? prev : next.stick));
  };
  /** force follow (send / back-to-bottom / thread switch) -- also reset lastTop. */
  const forceFollow = () => {
    const el = conv.current;
    stick.current = true;
    scrollState.current = { stick: true, lastTop: el ? el.scrollTop : 0 };
    setAtBottom(true);
  };
  /** 那颗按钮：跳回底部并恢复跟随。 */
  const jumpToBottom = () => {
    forceFollow();
    end.current?.scrollIntoView({ behavior: "smooth" });
  };

  //:换一段对话（新建 / 点历史 / 进群），重新贴底。
  //:   不这样的话：在 A 段翻到中间、点开 B 段，B 段会停在一个莫名其妙的位置。
  useEffect(() => {
    forceFollow();
    end.current?.scrollIntoView({ block: "end" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, viewing, chatGroup]);

  // 事件流 —— 详细活动块的数据源
  useEffect(() => {
    if (!client || !client.subscribe || !client.connect) return undefined;
    const offs = [];

    //:直播事件只画在它那一轮的那一段上。
    //:正在看某一段历史（`visibleRef.thread` 非空）而这一轮属于另一段
    //:   （或反过来），不画。`""` = 不知道归属，照旧画（宁可多画，
    //:   也不让正常的直播消失 —— 草稿态刚发出去那一刻 id 还没铸）。
    const _liveMine = () => liveEventBelongs(viewKeyRef.current,
                                             liveSidRef.current);

    offs.push(client.subscribe("task.start", () => {
      setLlmRetry(null);          // 新的一轮从干净状态开始
      bgableRef.current.clear();  // 新的一轮：上一轮的可转登记一律作废
      //: 上一轮那份"改动文件"的兜底必须在这里清掉。
      //:   它由 `files.changed` 写、在 `task.end` 里配给新消息 —— 而那条事件
      //:   是 `task.end` 之后才到的，所以清空那一步（也在 task.end 里）
      //:   永远轮不到它；不清就等于把它原样挂到下一轮的回复上（
      //:   这一轮改的是 big.txt，列表里却是上一轮的 code.py / config.json）。
      pendingFiles.current = null;
      loadTasks();
      if (!_liveMine()) return;   // 不是正在看的那一段，不许在它头上长活动块
      setProv("");
      // 无主理人群聊不开活动块 —— 多人协作看的是"谁在工作"，
      //    不是单 agent 的工具步骤流。状态由 `group.member.*` 事件驱动。
      if (groupModeRef.current) {
        setGroupWork((x) => x || []);
        return;
      }
      //:活动块从这一刻起就是 `messages` 的一个条目 —— 它必须排在"用户的问"之后、"回复"之前。
      //:两个动作（用户）：
      //:   ① 先把旧块全部折叠 —— 追问之后上一个框还摊开着，占掉半屏；
      //:   ② 插在最后一条用户消息之后（不是 `concat`）—— 回复可能先到
      //:      （两条通道），直接追加会把块放到回答后面，看起来像"回答中间冒出步骤流"。
      setOpenAct("");
      setMessages((x) => insertActivity(x, {
        id: "act" + Math.random(), role: "activity", live: true,
        //:起手是思考中（这一轮还没调任何工具）。
        startedAt: Date.now(), stepLabel: t(THINKING), stepStartedAt: Date.now(),
        running: 0, lines: [], draft: "",
      }));
    }));

    offs.push(client.subscribe("tool.start", (d) => {
      if (!_liveMine()) return;
      // 无主理人群聊不展示工具步骤（成员状态条已经说明谁在干活）。
      if (groupModeRef.current) return;
      //:这一步能不能 Ctrl+B 转后台由服务端判（`d.backgroundable`）——
      //:   前端不按工具名猜：`run_command(job_id=…)` 是瞬时查询、
      //:   `run_subagent{background:true}` 本来就是后台，两者都没有前台执行体。
      if (d.backgroundable && d.call_id) bgableRef.current.add(d.call_id);
      // 有活干了，外面那坨没定性的文字搬进来（它是这一轮的自言自语），再记一步。
      //   `create:true` —— 没有活块时兜底起一个（`task.start` 是正路，这是保险）。
      const p = provRef.current;
      if (p) setProv("");
      editActivity((a) => {
        const lines = p ? a.lines.concat([{ id: "say-" + d.call_id, kind: "speech", text: p }]) : a.lines;
        return {
          ...a,
          //:头部翻到「正在执行命令」，这一刻的计时从零开始
          //:   （上一次是「思考中」的用时，两者不许串）。
          //:`running` 要加一不是"设成真" —— 一批只读工具是并发跑的，
          //:   好几条 `tool.start` 会连着来。
          stepLabel: t(RUNNING_CMD),
          stepStartedAt: Date.now(),
          running: (a.running || 0) + 1,
          //:可转后台的那几步现在有几条（步骤流顶上那行提示的判据）。
          backgroundable: (a.backgroundable || 0) + (d.backgroundable ? 1 : 0),
          draft: "",
          lines: lines.concat([{
            id: d.call_id || String(Math.random()),
            kind: "step",
            text: d.activity || d.name || t("调用工具"),
            //:`sub_hint` = 工具参数的原文形态（今天是 `run_command` 的命令行）——
            //:   它比 `arg_hint`（basename）长，界面上单行省略号 + 悬停看全文。
            sub: d.sub_hint || d.arg_hint || "",
            subFull: d.sub_hint || "",
            ok: null,
            ms: null,
          }]),
        };
      }, { create: true });
    }));

    offs.push(client.subscribe("tool.end", (d) => {
      if (!_liveMine()) return;
      if (groupModeRef.current) return;
      //:这一步结束了（或已经转后台），它不再是"可转"的那一条。
      //   判据是 `call_id`、不是"减一"：并发批里回头的是哪一条都知道。
      const wasBgable = d.call_id ? bgableRef.current.delete(d.call_id) : false;
      editActivity((a) => {
        // 一条命令跑完，这一步结束了，头部翻回「思考中」，
        //    而计时从这一刻重新开始（要的是"当前步骤用时"，不是"最后一步用了多久"）。
        //    `running` 是计数：减到 0 才翻回去。只认布尔的话，并发那一批
        //      里第一条 `tool.end` 一到就把标签打回「思考中」，
        //      而剩下几条还在跑 —— 那就是标签在说谎。
        const running = Math.max(0, (a.running || 0) - 1);
        return {
          ...a,
          running,
          backgroundable: Math.max(0, (a.backgroundable || 0) - (wasBgable ? 1 : 0)),
          ...(running === 0 ? { stepLabel: t(THINKING), stepStartedAt: Date.now() } : {}),
          lines: a.lines.map((l) => l.id === d.call_id
            ? { ...l, ok: d.ok !== false, ms: d.duration_ms }
            : l),
        };
      });
    }));

    // 模型连接重试 —— 红色横幅：绿色横幅变红并常驻，
    //   一旦连接成功就变绿、取消常驻。
    //   加事件 = 两处（`api.js::EVENT_TYPES` + 这里），少一处 = 开关打开、
    //   流在跑、用户一个字都看不到。
    offs.push(client.subscribe("llm.retry", (d) => {
      if (groupModeRef.current) return;
      setLlmRetry(d || null);
    }));
    offs.push(client.subscribe("llm.retry_ok", () => setLlmRetry(null)));

    // 压缩进行中 / 结果—— 黄横幅（挡输入）+ 失败转红。
    //   加事件 = 两处（`api.js::EVENT_TYPES` + 这里），少一处 = 用户一个字都看不到。
    //   群聊里同样只认当前这一段（与上面 `llm.retry` 一条判据）。
    offs.push(client.subscribe("compact.progress", (d) => {
      if (groupModeRef.current) return;
      const st = (d && d.state) || "";
      const msg = (d && d.message) || "";
      if (st === "start") {
        setCompactState("running"); setCompactSince(Date.now()); setCompactMessage("");
      } else if (st === "ok") {
        // 黄条落下去 -> 绿条说一句「压缩完成，当前上下文：xx%。」，
        //   停一会儿自己收（用户不需要点「知道了」去确认一件已经成功的事）。
        setCompactState("done"); setCompactSince(0); setCompactMessage(msg);
        clearTimeout(compactDoneTimer.current);
        compactDoneTimer.current = setTimeout(() => setCompactState(""), 4000);
      } else if (st === "degraded" || st === "broken") {
        // 降级/熔断：黄条停久一点（8s）再自己收，也可以提前点「知道了」。
        //   判据：它说的是「这次压得不完整」，比成功回执更需要被看见 ——
        //   但"必须点一下才能消失"会把一条提示变成一道门（用户会被它挡住）。
        //   同一句话已经进了对话正文，收起横幅不等于丢信息。
        setCompactState("notice"); setCompactSince(0); setCompactMessage(msg);
        clearTimeout(compactDoneTimer.current);
        compactDoneTimer.current = setTimeout(() => setCompactState(""), 8000);
      } else if (st === "none") {
        setCompactState("nothing"); setCompactSince(0); setCompactMessage("");
      } else if (st === "fail") {
        setCompactState("failed"); setCompactSince(0); setCompactMessage("");
      }
    }));

    // `say` = 模型在带工具调用的那一轮里说的话。
    //    它就是「自言自语」，要留在活动块里；不能一到就把草稿丢掉
    //    （草稿是预览，最终回复才是权威 —— 但这一轮的自言自语属于步骤流）。
    offs.push(client.subscribe("say", (d) => {
      if (!_liveMine()) return;
      if (groupModeRef.current) return;
      const p = provRef.current;
      if (p) setProv("");
      editActivity((a) => {
        const text = d.text || p;
        if (!text) return { ...a, draft: "" };
        const last = a.lines[a.lines.length - 1];
        // 去重：`tool.start` 刚把同一段搬进来过。
        if (last && last.kind === "speech" && last.text === text) return { ...a, draft: "" };
        return { ...a, lines: a.lines.concat([{ id: "say-" + Math.random(), kind: "speech", text }]), draft: "" };
      }, { create: true });
    }));

    offs.push(client.subscribe("delta", (d) => {
      if (!_liveMine()) return;
      if (groupModeRef.current) return;
      const chunk = d.text || "";
      if (!chunk) return;
      if (actRef.current) {
        //:`create:false` —— `task.end` 之后到达的 delta 不许复活一个空框（那会排在回复后面）。
        editActivity((a) => ({ ...a, draft: (a.draft || "") + chunk }), { create: false });
      } else {
        // 还没有活着的活动块（这可能是最后那一轮的文字），先摆在外面，
        // 等真有工具调用再搬进去。这样纯问答不会先闪进框里再跳出来。
        //:这里必须拼 `chunk`。写 `t` 是把 i18n 那个函数拼进去
        //:   （`"" + 函数` = 它的源码），每来一个分片就把整段压缩过的 JS
        //:再拼一遍，草稿区高速滚动的乱码，回复一到又消失
        //:   （IM 那边在等回复、GUI 这边看步骤流时稳定复现 ——
        //:那一轮不是本屏起的，没有活动块，走的正是这一支）。
        setProv(provRef.current + chunk);
      }
    }));

    // 无主理人群聊的成员状态（替代单 agent 的步骤流）。
    offs.push(client.subscribe("group.member.start", (d) => {
      //:切走了就别再往新那一屏里填成员状态（标签在说谎）。
      if (!_liveMine()) return;
      const name = (d && d.name) || "";
      if (!name) return;
      setGroupWork((x) => {
        const list = Array.isArray(x) ? x.slice() : [];
        const i = list.findIndex((w) => w.name === name);
        const item = { name, avatar: d.avatar, state: "working" };
        if (i >= 0) list[i] = item; else list.push(item);
        return list;
      });
    }));
    offs.push(client.subscribe("group.member.done", (d) => {
      //:同 start —— 不属于这一屏的事件一个字节都不画。
      if (!_liveMine()) return;
      const name = (d && d.name) || "";
      if (!name) return;
      setGroupWork((x) => (Array.isArray(x)
        ? x.map((w) => (w.name === name
          ? { ...w, state: (d.ok === false ? "error" : "done") }
          : w))
        : x));
      // 圆桌「说完就发」 —— 事件带着正文，立刻落一条气泡
      //   （谁说完了就先发出来）。
      //   只有圆桌这条路上带 `content`（并行那条的正文在整轮结束时
      //   一次性给，顺序是名册序）；`_liveMine()` 保证不往
      //   正在看的历史线程里乱贴。
      if (typeof d.content === "string" && d.content && _liveMine()) {
        setMessages((x) => x.concat([{
          id: "gs" + Math.random(),
          role: "assistant",
          content: d.content,
          speaker: name,
          avatar: (d.avatar != null ? d.avatar : null),
          kind: d.kind || "group_speaker",
          error: d.ok === false,
          ts: Date.now() / 1000,
          //: 整轮结束时的 `group_replies` 才是权威那份 —— 它到了就整批替换
          //: （见 `pushGroupReplies`），所以这里标记出来，别留下两条。
          streamGroup: true,
        }]));
      }
    }));

    offs.push(client.subscribe("task.end", async (d) => {
      // 接管期间一轮结束，把 GUI 拉回前台。
      //   用户在别的应用里等结果，回复落在我们窗口里而窗口在后台 —— 看不到。
      //   这是壳的行为，不是模型的行为：模型不该（也不能）去激活我们的窗口
      //     （`list_windows` 已经把自家窗口剔掉了）。只在接管开启时做。
      if (takeoverOnRef.current) {
        try { window.crannia?.focusWindow?.(); } catch { /* 浏览器里没有壳，拉倒 */ }
      }
      //:一轮结束，红横幅退场（重试耗尽时的"常驻"到这里为止；
      //: 那一轮为什么没成，下面有一条红色的活动条 + 说明文字）。
      setLlmRetry(null);
      //:这一轮是不是正在看的那一段。
      //:不是，记账照做（busy / 名册 / 未读），画面一个字都不动
      //:   —— 否则回复会被贴进你正在看的那段历史里。
      const _show = _liveMine();
      // 这一轮是一条 `/clear` 命令轮，它不是一个对话回合。
      //   两件事一起做：
      //   ① 那条回执不许落成气泡 —— 它是框架的界面话术，不是模型说的话，
      //      摆进对话里会像"模型突然说了一句清空成功"（同 `CANCEL_REPLY` 那条判据）；
      //   ② 聊天区清空 —— `/clear` 换了一段，屏幕上那一段就不该再留着。
      //   这条闸对用户自己手打 `/clear` 同样生效。
      //   `return` 必须排在 `setBusy(false)` 之前：这条轮可能与
      //   真正在跑的那一轮交错到达，顺手把 busy 抹掉会让「正在处理」提前消失、
      //   输入框又能再发一条（谁收口、谁不许收口，要看清楚）。
      if (d && d.cleared) {
        //:只有看着那一段时才清屏 —— 别的线程 `/clear` 不许把这一屏抹掉。
        if (_show) {
          setMessages([]);
          setProv("");
        }
        liveSidRef.current = "";
        //:`sentSidRef` 也必须清 —— 它喂 `viewKeyOf` 的 `currentId`，而那一格
        //:优先于名册里的 `is_current`。`/clear` 之后它还指着旧那一段
        //:这一屏的身份钉死在旧 id 上，而下一条消息跑在新 id 上
        //:   `task.end` 判成"不是这一屏的"、POST 那条 `stillMine()` 也判否
        //:，回复被吞掉（`/clear` 之后第一句没有回复，
        //:   而库里那两条消息都在）。
        sentSidRef.current = "";
        refreshThreads();
        return;
      }
      const p = provRef.current;
      const reply = (d && d.response) || p || "";
      // 活动块不删：标成 `done`，它会缩成一条「已完成」小条留在原地；
      //    回复另开一个框追加在后面。
      //    所以顺序天然是「问 → 活动块 → 答」（因为活动块在 `task.start` 时就进了 `messages`）。
      //    `create:false`：这一轮压根没起过活动块（纯问答）时，不许凭空长一个出来。
      //:失败的一轮不许显示成绿色「已完成」。
      //:   判据是 `task.end.ok`（后端在 `metadata["error"]` 上写）。
      //:`retry` 那一格不清：重试耗尽之后红条要常驻。
      if (_show) {
        editActivity((a) => ({
          ...a, live: false, done: true, endedAt: Date.now(), draft: "",
          failed: !!(d && d.ok === false),
        }), { create: false });
        //:纯问答的一轮没有步骤，那条「✓ 已完成 0.0s」是纯噪声
        //:   （用户回答中间莫名其妙有个步骤流）。
        //:判据是"没有任何步骤行且没失败"——失败的一轮要留着（它是给用户看的结论）。
        setMessages((x) => x.filter((m) => !(m.role === "activity" && m.done
          && !m.live && !m.failed && !(m.lines || []).length)));
        setProv("");
        //:大产物：不放进 `pushAssistant` —— 那个函数被
        //:   `tests/test_main_ui.py` 抠出来在 node 里真跑（它必须保持纯 JS）。
        //:两条实时路各喊一次，`bigSeen` 按 id 去重，同一件东西只弹一遍。
        notifyLargeArtifacts(d && d.artifacts);
        pushAssistant(reply, {
          ts: (d && d.ts) || Date.now() / 1000,
          //:这一轮是用户打断收尾的，那句话是回执，不是回答（见 `CANCEL_RECEIPT`）。
          ...((d && d.cancelled) ? { kind: CANCEL_RECEIPT } : {}),
          ...(pendingFiles.current ? { files: pendingFiles.current } : {}),
          // 工具 / 上游这一轮产出的图 / 音 / 视频（没有就不设键 ——
          //   读不到 ≠ 空列表）。
          ...((Array.isArray(d && d.artifacts) && d.artifacts.length)
            ? { artifacts: d.artifacts } : {}),
        });
        // 这一轮落库后是哪两行。必须排在 `pushAssistant` 之后
        //   —— 它要从后往前找"还没有行号的助手消息"，那时那条得已经在了。
        attachRows(d && d.turn_rows);
      }
      // 这一轮没赶上的 steer：用同一条气泡接着跑一轮。
      //   服务端在轮末把它们取空了（不许留给下一轮），所以这里是唯一的出口 ——
      //   不接就是"用户打的那句话没了"（比多一条更坏）。
      //   调 `send(..., { reuseId })`：不 push 新气泡，只复用那条。
      const _left = (d && d.steer_leftover) || [];
      for (const _it of _left) {
        const _rec = steerWaitRef.current.get(String((_it && _it.id) || ""));
        if (!_rec) continue;                 // 不是这一屏发出去的
        steerWaitRef.current.delete(String(_it.id));
        sendRef.current?.(
          _rec.text, _rec.images, _rec.files, { reuseId: _rec.msgId });
      }
      // 兜底不依赖服务端那份回报（报了没收到的情形出现过）：
      //   这一轮是本屏的、而且已经结束了，还挂在 `steerWaitRef` 里的那些
      //   再也不会有"下一个边界"了（纯问答一轮根本没有边界），就地接管：
      //   先让服务端把它从收件箱摘掉（否则下一轮会再注入一次），
      //   再用同一条气泡接着跑一轮。
      if (_show && steerWaitRef.current.size) {
        for (const [id, rec] of Array.from(steerWaitRef.current)) {
          steerWaitRef.current.delete(id);
          client.call("/api/steer", jsonRequest("POST", {
            cancel: true, client_msg_id: id,
          })).catch(() => {});
          sendRef.current?.(
            rec.text, rec.images, rec.files, { reuseId: rec.msgId });
        }
      }
      pendingFiles.current = null;
      liveSidRef.current = "";
      //:这一轮结束了，名册已经把它标成"当前"，那份兜底不再需要。
      sentSidRef.current = "";
      // 收尾自己再问一次（上面那个信号是主路，这里是兜底）——
      //   SSE 断线 / 事件漏收时还能把浮窗捞回来。
      //   必须排在 `setBusy(false)` 之前：那一刻上面那个轮询 effect
      //   就被清掉了；排在后面 = 白问一次。
      await loadApproval();
      // 红点 / 排队中 —— 判据：
      //   只在"跑完了 + 当前不是它"时置位。
      const _sid = (d && d.storage_id) || "";
      if (_sid) {
        setQueued((q) => (q[_sid] ? { ...q, [_sid]: false } : q));
        const vis = visibleRef.current;
        if (vis.awaitingNew) {
          // 这一轮就是刚从草稿态发出去的那一段，用户正看着它。
          vis.awaitingNew = false;
        } else if (!inflightRef.current.has(_sid) && vis.thread !== _sid) {
          // 如实记一条已知的漏：你在 X 还在飞的时候切去看 Y，
          //    X 跑完时 `inflight` 里还有它，不会被标未读。
          //    （"我发的、我知道它在跑" —— 这个漏是有意留的，代价比多一张
          //      "谁在看"的表小得多。）
          setUnread((u) => ({ ...u, [_sid]: true }));
        }
        inflightRef.current.delete(_sid);
      }
      // 事件流说"这一轮完了"，但别的轮可能还在跑（用户连发两条）——
      //    只有计数归零才关灯。
      if (inflightTurnsRef.current === 0) setBusy(false);
      // 侧栏那份名册跟着走一遍 —— 它带着轮数与标题，而这两个数
      //   正是这一轮刚改的（不刷的话，新会话要等下一次全量刷新才出现）。
      refreshThreads();
    }));

    // 「本轮改动文件」—— 按钮只报数「本轮改动文件：X」，点开才是列表。
    //   它在 `task.end` 之后才到，走 `attachFiles` 回头贴。
    //   `count == 0` 时什么都不做 —— 一轮没动文件是常态，不该长出个空按钮。
    offs.push(client.subscribe("files.changed", (d) => {
      //:它贴到"最后一条助手消息"上 —— 不是正在看的那一段
      //:   就别贴（否则会把 A 那一轮改的文件挂到 B 的回复下面）。
      if (!_liveMine()) return;
      const payload = { files: d.files || [], count: d.count != null ? d.count : (d.files || []).length };
      if (!payload.count) return;
      pendingFiles.current = payload;
      attachFiles(payload);
    }));

    // 审批浮窗有时永远不出现：服务端早就在发这个信号 ——
    //   `EVT_APPROVAL_REQUEST = "approval.request"  # 有新的待审批：{request_id,kind,ask_index}`。
    //   它的规矩是：
    //   「信号必须在"它让你去读的那份 state"可读之后才发」
    //   它只是"叫你来读"，正文要去 `GET /api/approval` 拿。
    //   前端必须订阅它；只靠挂在 `busy` 上的那个 1.2 秒轮询时，
    //   `task.end` 一到 `busy` 就 false，定时器被清掉。而两条事件的间隔
    //   通常小于 1200 ms，大概率漏掉 → 用户卡在「(无回复)」。
    //   所以判据：信号发了就得有人听（加一种事件 = 名单 + 订阅两处，
    //     漏任何一处都会静默失效）。测试两头对账：服务端发、前端听。
    offs.push(client.subscribe("approval.request", () => { loadApproval(); }));

    //:顶栏那一行读数的数据源。
    //:`context.state` 已经在 `api.js` 的 `EVT_TYPES` 名单里，只加订阅、不动名单
    //:   （「加一种事件 = 名单 + 订阅两处」的判据，名单那一半早就有了）。
    //:它是每轮一条（发送点在调度器轮末，与 `task.end` 同一条路）——
    //:所以 `/compact` 那种不跑 post_process 的轮照样报得到。
    offs.push(client.subscribe("context.state", (d) => setCtx(d)));

    //:插件被屏蔽那一下。
    //:这里是收到信号 → 去拉状态，不是"拿载荷直接画" ——
    //:   与审批浮窗那两条路同族（`approval.request` 叫你读、`GET /api/approval` 给正文）。
    //:理由：状态只许有一个来源，否则"信号到了、状态还没到"会出现半截浮窗。
    offs.push(client.subscribe("plugin.blocked", () => { loadBlockedNotices(); }));

    //:面板开着时，每轮收尾刷一次（判据同 WebUI：`if (_ctxPanelOpen) loadContextReport();`）。
    //:关着就不刷 —— 那是纯税：面板不在屏上，读出来的数没人看。
    offs.push(client.subscribe("task.end", () => { if (ctxOpenRef.current) loadCtxReport(); }));
    //:一轮收尾也刷一次任务两格 —— 与 N10 的会话构成面板同一条判据
    //:   （事件只是信号，状态现拉）。`/clear` 之后这里会显示 0。
    //:单独一条订阅（不塞进上面那一行）：那一行是
    //:   `test_desktop_context` 结构读者钉着的原文，混进别的东西会让它变红。
    offs.push(client.subscribe("task.end", () => {
      bgableRef.current.clear();   // 这一轮结束，没有"可转"的前台任务了
      loadTasks();
    }));

    //:任务四种事件—— 它们只改计数与浮窗，不新开活动块。
    //:可能落在 `task.end` 之后（通知轮 / 上一条任务晚一步收尾），
    //:开块就没有人会遇到它去收口（的现场）。
    ["task.started", "task.finished", "task.failed", "task.cancelled"]
      .forEach((t) => offs.push(client.subscribe(t, () => { loadTasks(); })));

    // 排队中与它的提示条。
    //
    //   提示条用不挡操作的 `toast` —— 文案由后端给
    //     （`events.py` 的 `TURN_QUEUED_MESSAGE` 一处定义，前端不抄第二份）。
    //   事件只带 `session_key`（通道键）—— 私聊全塌成 `workspace:private`
    //     所以分不出是哪一段。归给"这一刻还在飞的那几条发送"。
    //     草稿态那一条没有行可标（它的 id 还没铸），只靠提示条。
    offs.push(client.subscribe("turn.queued", (d) => {
      setQueued((q) => {
        const next = { ...q };
        for (const sid of inflightRef.current) if (sid) next[sid] = true;
        return next;
      });
      if (d && d.message) notify(d.message);
    }));

    //:模型在这一步里收下了你补的那句话—— 三件事，缺一件
    //:用户就会觉得"没生效"：① 摘「排队中」；② 上一个步骤流收成已完成并收起；
    //:   ③ 在这条消息之后新开一块（落点判据是修好的那个）。
    //:不碰 `busy` —— 绿横幅的计时只认 `busy` 的假→真，而补一句话不该让它重来。
    offs.push(client.subscribe("steer.delivered", (d) => {
      if (!_liveMine()) return;
      const ids = ((d && d.items) || []).map(String).filter(Boolean);
      if (!ids.length) return;
      const taken = new Set(ids);
      setMessages((x) => x.map((m) => (taken.has(m.id) ? { ...m, queued: false } : m)));
      ids.forEach((id) => steerWaitRef.current.delete(id));
      setOpenAct("");
      editActivity((a) => ({ ...a, live: false, done: true, endedAt: Date.now() }),
                   { create: false });
      setMessages((x) => insertActivity(x, {
        id: "act" + Math.random(), role: "activity", live: true,
        startedAt: Date.now(), stepLabel: t(THINKING), stepStartedAt: Date.now(),
        running: 0, lines: [], draft: "",
      }));
    }));

    //:事件流的连接状态（`api.js` 在 `open` / 原生 error / `too many streams`
    //:三处发）。`setState` 同值时 React 自己会跳过，3 秒一次的自动重连不会抖。
    offs.push(client.subscribe("stream.state", (d) => {
      setStreamDown(!(d && d.connected));
    }));

    offs.push(client.subscribe("error", (d) => {
      if (_liveMine()) {
        editActivity((a) => ({ ...a, live: false, done: true, endedAt: Date.now() }), { create: false });
      }
      if (inflightTurnsRef.current === 0) setBusy(false);
      notify((d && d.message) || t("这一轮出错了"));
    }));

    //:开屏那一拉 —— 与审批那两条同一形状：
    //:事件只在连着的时候到得了；"关掉 GUI 期间后台屏蔽了一个插件"这件事
    //:只能靠这一次补拉（否则用户永远不知道）。
    loadBlockedNotices();

    client.connect().catch(() => {});
    return () => offs.forEach((off) => off && off());
  }, [client]);

  // 两枚 chip 要显示"这一段真正在跑的"，不是"设置里写着的"（标签在说谎）。
  //   已冻结，用冻结值；还没冻结（这一段还没跑过），用配置里那个 —— 那正是它将要用的。
  //   反例：chip 只读 `settings.default_mode`，而点击又乐观更新那个值
  //   所以用户切了「自动」、chip 显示「自动」、会话其实一直冻在手动，每个动作都问。
  //   未冻结那一半也不能兜底到 `settings.default_mode` ——
  //   那个对象只在打开设置弹窗时才拉（见 `openSettings`）
  //   所以恒为 `undefined`，第一轮之前一律显示「手动」。判据收进
  //   `mode.js::chipMode`（`/api/status` 为此多报了一个 `mode_configured` 键）。
  const mode = chipMode(status);
  //:顶栏那一行的文案 / 类 / 悬停 —— 一处算准（`context.js` 那个纯函数）。
  //:面板与它共用同一份判据：两套渲染 = 同一个数讲两句话。
  const readout = readoutText(ctx);
  //:顶栏两格的计数来源：服务端现拉的那份（不是在事件里自己数）。
  //:读不到（`error`），两格都不显示 —— 不把"读不到"画成 0。
  const bgCount = (tasks && tasks.counts && !tasks.error) ? tasks.counts.background : 0;
  const subCount = (tasks && tasks.counts && !tasks.error) ? tasks.counts.subagents : 0;
  const effort = Number(status?.effort_session_level ?? settings?.effort_level) || 3;
  const spaceName = spaces.find((x) => x.id === space)?.name || t("选择工作空间");

  async function start() { try { await client.call("/api/start", jsonRequest("POST", {})); notify(t("Agent 正在启动")); setTimeout(refresh, 700); } catch (e) { notify(e.message); } }
  /** 手动切到自动时要先弹一次安全提示。
   *
   *  判据 —— 只有"手动切到自动"这一条路弹，其余一律直接落盘、不弹：
   *   · `key === "auto"`   ：切过去的是自动
   *   · `mode !== "auto"`  ：真的在变（重复点"自动"不算切换）
   *   · `!autoNoticeOff()` ：没勾过「不再提醒」
   *  手动 / 计划 / 自动→自动三条都不满足，走下面那条直接应用的路径。
   *  点「取消」= 不切（不是"切了再说"）—— 落盘推迟到确认之后，
   *    与「新建任务」那条"推迟副作用"是同一个形状。
   *  两个入口（`Composer` 的模式条 + `Settings` 弹窗）共用这一个函数
   *，不可能只在一处弹。 */
  function changeMode(key) {
    // 切「完全权限」每次都要确认（没有「不再提醒」）——
    //   与自动模式的提醒不同：这一档是"不问而放"，它不该被一次勾选永久免掉。
    if (key === "full_access" && mode !== "full_access") { setFullNotice(true); return; }
    if (key === "auto" && mode !== "auto" && !autoNoticeOff()) { setAutoNotice(true); return; }
    applyMode(key);
  }
  /** 切"现在这一段"（chip 走这里）。后端是 `/mode` 命令同一个
   *  setter，当场生效、并把那句"这是冷启动"的说明原样转给用户。
   *  不落盘（与 `/effort` 同一条判据）—— 要改默认值走设置页。
   *  切完必须 `refresh()` 重读，不许乐观更新：chip 要显示读回来的真值，
   *  否则又回到"标签在说谎"。 */
  async function applyMode(key) {
    try {
      const r = await client.call("/api/mode", jsonRequest("POST", { mode: key }));
      refresh();
      notify((r && r.message) || t("已切换模式（仅当前对话）。"));
    } catch (e) { notify(e.message); }
  }
  /** 子 agent 用哪个模型。
   *  空串 = 沿用当前会话的模型（默认，也是多数人的选择）。
   *  收的是端配置页里那条配置的 id（`m` + 8 位），不是供应商目录名
   *    —— 所以下拉里显示的必须是用户自己起的配置名（`opt.name`），
   *    而值是 id。拿 id 当标签显示，用户看到的是一串 `m3f8a1c2`。
   *  值不认识，后端当场 400、带可选名单（拒绝要带路线）——
   *    前端不自己判（判两份就会漂，同 `settings-view.js` 那条）。 */
  /** 弹窗点了「启用」—— 勾了「不再提醒」就先记下，然后才真的切。 */
  function confirmAutoMode(remember) { setAutoNotice(false); if (remember) rememberAutoNotice(); applyMode("auto"); }
  /** 点「取消」= 什么都不做（不切、不记）。 */
  function cancelAutoMode() { setAutoNotice(false); }
  /** 确认之后才真的切；`applyMode` 仍走 `/api/mode`（按会话、不落盘）。 */
  function confirmFullAccess() { setFullNotice(false); applyMode("full_access"); }

  //:接管。启用 / 停用都走后端（它是唯一真相源）。
  //:停用不需要二次确认 —— 停用永远是安全的那一侧，多问一次只会让
  //:     急停变慢（点「再次点击按钮停用」时不该再被拦一下）。
  async function enableTakeover() {
    try {
      const r = await client.call("/api/computer-use/enable", jsonRequest("POST", {}));
      setTakeover(r || { enabled: true });
      notify(t("接管已开启。不要在电脑上做其他操作；按 Esc 退出。"));
    } catch (e) { notify(e.message || String(e)); }
  }
  async function disableTakeover() {
    try {
      const r = await client.call("/api/computer-use/disable", jsonRequest("POST", {}));
      setTakeover(r || { enabled: false });
      notify(t("接管已停止"));
    } catch (e) { notify(e.message || String(e)); }
  }
  function toggleTakeover() {
    if (takeover.enabled) { disableTakeover(); return; }
    if (takeoverNoticeOff()) { enableTakeover(); return; }
    setTakeoverNotice(true);
  }
  /** 弹窗点「启用」= 真的启用（偏好不在这里改）。 */
  function confirmTakeover() { setTakeoverNotice(false); enableTakeover(); }
  /** 「我知道了」= 只改"以后还弹不弹"这个偏好 —— 不启用、也不取消。 */
  function acknowledgeTakeover() { rememberTakeoverNotice(); setTakeoverNotice(false); }
  //:注册"打开本机路径"那扇门（`Markdown` 里点路径链接时用它）。
  //:真的开门在服务端（`POST /api/reveal`：存在性检查 + 只定位不执行）。
  //:失败必须出声 —— 点了一个不存在的路径，静默就等于"点了没反应"
  //:   （后端那句已经把"找过哪儿"带上了，原样转给他）。
  useEffect(() => {
    setPathOpener((href) => client.call("/api/reveal", jsonRequest("POST", { path: href }))
      .catch((e) => { notify(e.message); throw e; }));
    return () => setPathOpener(null);
  }, [client]);
  /** 同 `applyMode`：切的是本会话的档 —— 后端走 `/effort` 命令同一个 setter
   *  （`tiers.set_level`），当场生效。也不落盘。 */
  async function changeEffort(level) {
    try {
      await client.call("/api/effort", jsonRequest("POST", { effort: level }));
      refresh();
      notify(t("已切换强度，下一条消息起生效。"));
    } catch (e) { notify(e.message); }
  }

  /** 离开群聊 = 失去焦点 —— 点别的对话 / 新建任务时，
   *  群那几格必须一起清掉。
   *
   *  不清的后果不只是高亮：`send()` 看的是 `chatGroup`，你以为在私聊里
   *    说话，消息发进了群；顶部也一直写着群名（界面在说谎）。
   *  与 `leaveGroup()` 的差别：那个还要清屏 + 退回草稿；
   *    这里只清"群的身份"，屏幕由调用方自己决定（看旧对话 / 新建任务）。
   */
  function clearGroupMode() {
    setChatGroup(null);
    setGroupWork(null);
    setGroupHist(null);
  }

  /** 进入某个群聊（点击群名进入）。
   *
   *  有主理人 / 没有主理人 —— 这两条拓扑在界面上是同一件事：
   *    用户发的消息进群会话，而"主理人替团队对外说话"那半是模型那一侧
   *    的行为（`ask_member` 的 directive 里写着"你是主理人" / "结论回给主理人"，
   *    见 `core/group_dispatch.build_identity`）。界面不按拓扑分叉 ——
   *    分叉的是提示词。
   *  进来看得见这个群说过的话 —— 拉 `?group=<群 id>`
   *    （键与存储 id 由服务端推，前端不拼键）。
   */
  async function openGroup(g) {
    setChatGroup(g);
    setGroupWork(null);
    setPage("chat");
    setDraft(false);
    setViewing(null);
    setProv("");
    setMessages([]);      // 换了一条会话，屏幕上那一段不是这个群的
    setViewHasMore(false);
    setGroupHist(null);
    // 进群就把这个群说过的话拉回来。
    //   只传群 id（`?group=`）—— 群还没说过话，服务端回 `empty_reason`，
    //   照实转述，别给一张看不出所以然的白纸。
    try {
      const d = await client.call(
        `/api/messages?group=${encodeURIComponent(g.id)}&limit=50`);
      setMessages((d.messages || []).map(histRow));
      setGroupHist({ storage_id: d.storage_id || "", has_more: !!d.has_more });
      setViewHasMore(!!d.has_more);
      notify(d.empty_reason || t("已进入群聊「{g.name}」", { "g.name": g.name }));
    } catch (e) {
      notify(e.message || String(e));
    }
  }

  /** 退出群聊，回到普通对话的干净状态。
   *  必须清屏：群里那几条留在屏幕上的话不属于接下来这条会话，
   *   留着 = 屏幕上显示的是群内容、发出去却进私聊（标签在说谎）。
   *，退成草稿屏（同「新建任务」）：屏幕是空的，下一句开一段新的。 */
  function leaveGroup() {
    setChatGroup(null);
    setGroupWork(null);
    setGroupHist(null);
    setMessages([]);
    setProv("");
    setDraft(true);
    setViewHasMore(false);
  }

  /** 打断这一轮。群聊那一轮要走同一个会话键，否则按了没反应。 */
  function stopTurn() {
    const url = chatGroup ? `/api/stop?group=${encodeURIComponent(chatGroup.id)}` : "/api/stop";
    return client.call(url, jsonRequest("POST", {})).catch(() => {});
  }

  /** `/btw` —— 旁路问一句，走 `POST /api/btw`，不进调度器、不排队。 */
  async function askBtw(question) {
    const q = String(question || "").trim();
    if (!q) { notify(t("请在 /btw 后面带上要问的话")); return; }
    setBtw({ question: q, loading: true, answer: "", error: "" });
    try {
      const body = { question: q };
      if (chatGroup) body.group = chatGroup.id;
      const r = await client.call("/api/btw", jsonRequest("POST", body));
      setBtw((b) => {
        if (!b || b.question !== q) return b;      // 已经换了/关了：丢掉这次结果
        if (r && r.ok) return { ...b, loading: false, answer: r.answer || "" };
        return { ...b, loading: false, error: (r && r.error) || t("没有拿到回复") };
      });
    } catch (e) {
      setBtw((b) => (b && b.question === q
        ? { ...b, loading: false, error: (e && e.message) || String(e) } : b));
    }
  }

  async function send(text, images, files, opts = {}) {
    // `/btw ...` 不上主对话这条线：它是旁路（服务端 `/api/btw` 不进调度器），
    //   主线程在跑时也要能问 —— 走这里只会排在那一轮后面。
    const btwText = String(text || "").trim();
    if (/^\/btw$/i.test(btwText)) {
      notify(t("请在 /btw 后面带上要问的话"));
      return;
    }
    const btwArg = /^\/btw\s+([\s\S]+)$/i.exec(btwText);
    if (btwArg) { askBtw(btwArg[1]); return; }
    //:`reuseId`：复用一条已经在屏上的气泡（的轮末兜底 ——
    //:   那颗"没赶上的 steer"接着跑一轮时，不许再 push 一条新的）。
    const reuseId = String((opts && opts.reuseId) || "");
    if (!text && !(images && images.length)) return;
    //: send => back to bottom (the user's own action).
    forceFollow();
    //: 「没得压」那条黄横幅跟着下一轮走——
    //:   它说的是一件已经过去的事，留着只会让下一轮也挂着一条旧提示。
    //:只清这一态：`running` 由 `compact.progress` 自己收尾，别在这里抢。
    setCompactState((s) => (s === "nothing" ? "" : s));
    //: 上一轮留下的错误红条：发下一条时收掉，这一轮从绿条开始
    //:   （后端也会在轮首 `clear()`，这里先做是为了不等那一帧）。
    setLlmRetry(null);
    if (!connected) return notify(t("请先运行 python -m web"));
    if (!status?.framework_running) return notify(t("请先启动 Agent"));
    // ── 这一轮该在哪一段对话里跑 ──────────────────────────────
    //
    // 三个动作，当场说清、一次请求（服务端在 `pre_process` 里照做）：
    //   · 草稿屏（开屏 / 新建任务），`new`    = 开一段新的
    //   · 正在看某一段历史，`thread` = 接着那一段
    //   · 其余，追加到当前绑定那段（默认）
    //
    // 不能靠"先发一条真的 `/clear`"：清完那一段就不再是"当前"，
    //   而屏幕上只剩刚打的那句话。库里一条没丢（名册是只追加的），
    //   丢的是屏幕上的那一段。
    // 那条 `/clear` 轮还会照常进事件流：开一个活动块、把它那句回执
    //   （「已清空窗口上下文…」）落成一个气泡。
    // 不能 `setMessages([])` —— 看的那一段就是接下来要聊的那一段，
    //   屏幕上的历史要留着。
    const body = { text };
    // 输入框给的是原始 chips（`{kind,name,url}`），给接口的那份在这里映射。
    if (images && images.length) {
      body.images = images.map((i) => ({ data_url: i.url, name: i.name }));
    }
    //:在群里聊，这一轮跑在那个群的会话里。
    //:它在 `draft` / `viewing` 之前判：群聊不是"某一段历史"，
    //:它是另一个会话（那两格管的是"同一段会话里的哪一条"，两回事）。
    if (chatGroup) body.group = chatGroup.id;
    //:无主理人群聊这一轮不展示步骤流，改成成员状态条。
    //:   先清空（成员 start 事件到了再逐个填）；`task.start` 也不会再开活动块。
    if (groupMode) setGroupWork([]);
    //:草稿态选的工作目录跟着这一条交出去（发送即锁）。
    //:它写在 `if (draft) body.new = true` 之前，但不改那一行的形状。
    if (draft && wsPath) body.workspace = wsPath;
    if (draft) body.new = true;
    else if (viewing) body.thread = viewing.storage_id;
    //:`startNew` 要记下来：它决定"上翻口该不该清"（见下）。
    //:上面那两行逐字别动 —— 源码级形状测试用正则
    //:  （`if \(draft\)\s*body\.new\s*=\s*true`）钉着它。
    const startNew = !!draft;
    //:记下"这一条发给了哪一段" —— 它跑完时不该被标成未读
    //:   （用户正看着它）。草稿态那一条的 id 还没铸，走 `awaitingNew` 那个标志。
    let sentTo = "";
    if (startNew) visibleRef.current.awaitingNew = true;
    else {
      sentTo = (viewing && viewing.storage_id)
        || ((threads.find((t) => t.is_current) || {}).storage_id) || "";
      if (sentTo) inflightRef.current.add(sentTo);
    }
    //:这一轮属于哪一段 —— 直播事件只画在那一段上。
    //:群聊的归属是群（`group:<id>`），不是"当前私聊那一段" ——
    //:   记成 `sentTo`（私聊 storage id）的话，"在 A 群跑、切到 B 群"判不出来
    //:   （两个 id 都读不到），加载中与回复都会落进 B。
    //:草稿态那一条 id 还没铸，`""`（不知道，照旧画）。
    const liveKey = chatGroup ? ("group:" + chatGroup.id) : sentTo;
    liveSidRef.current = startNew ? "" : liveKey;
    sentSidRef.current = startNew ? "" : liveKey;
    //:同步把这一屏的身份钉到这一轮（不等那个 effect）——
    //:   effect 的依赖是 `[viewing, chatGroup, draft, threads]`，不含这两个 ref；
    //:发送时若那三格都没变，它就不会重跑，`viewKeyRef` 停在上一段的 id 上
    //:，同 `/clear` 那个吞回复的坑。
    viewKeyRef.current = startNew ? "" : liveKey;
    //:这一屏在发送那一刻的身份：POST 回来时用它判"用户还在看这一段吗"。
    //:`""` = 草稿态（id 还没铸），不设栅栏。
    const sentViewKey = startNew ? "" : liveKey;
    setDraft(false);
    //:发送即锁：那个选择已经跟着这一条落到那段对话上了，手里不再留。
    //:留着的话，下一次「新建任务」会误带上上一段的选择。
    if (startNew) setWsPath("");
    setViewing(null);
    // 不能无条件清 false："开一段新的"与"接着当前那段"是两件事 ——
    //   前者确实没有更早的（屏幕上只有刚打的那句）；后者有，清掉就是
    //   把刚看到的入口又关一次（症状：发一条消息就翻不到历史了）。
    if (startNew) setViewHasMore(false);
    //:用户消息没有 SSE 帧，时间戳用本地时钟（`ts` 与后端同单位：epoch 秒）。
    //:附件要在这一条上就显示（不等落库）：正文里已经有文件的 `[附件]` 前言，
    //:图片则还没落成产物（刚发的就是本地 data URL），两块拼起来。
    //:解析器与回放那条同一个（`parseAttachments`），不另写一遍。
    const _parsed = parseAttachments(text);
    const _localAtt = [
      ..._parsed.attachments,
      ...(images || []).map((im, i) => ({
        kind: "image", url: im.url, name: im.name || t("图片 {n}", { n: i + 1 }),
      })),
    ];
    //:跑着的时候再发一条，本地排队（先不 POST）：
    //:请求还没出去，所以「撤回」是真的撤得回来；模型那边一个字都看不到。
    //:   顺序由 `heldSendsRef` 保证（先来先发）。
    const queuedNow = inflightTurnsRef.current > 0
      || heldSendsRef.current.length > 0;
    //:这一条会不会被塞进正在跑的那一轮—— 决定两件事：
    //:   ① 提示语：「当前步骤结束后发送」而不是「当前任务结束后执行」；
    //:   ② 排队期间能不能撤回（排队等锁的能撤回，已经交给服务端的不能）。
    //:只在队列为空时试：前面已经有人在等，必须保序（FIFO）。
    const canSteer = queuedNow && !reuseId && !heldSendsRef.current.length
      && !(images && images.length) && !(files && files.length)
      && !/^\s*\//.test(String(_parsed.text || ""));
    const user = { id: reuseId || ("u" + Math.random()), role: "user",
      content: _parsed.text, attachments: _localAtt.length ? _localAtt : null,
      queued: queuedNow,
      //:这一条打算走 steer（被拒时会置回 false）—— 提示语跟着它变。
      steer: canSteer,
      ts: Date.now() / 1000 };
    //:追问，旧步骤框一律折叠（用户）。
    //:   不等 `task.start`：那条事件在路上，而用户点发送的那一刻就该收起来。
    setOpenAct("");
    //:上一轮那句「已打断」到这一句为止（用户）——
    //:它是渲染不是消息（不落库），一直垂在对话里看着像模型说的话。
    //:只在发送时撤：打断当下它还得留着（那是"按生效了"的回执）。
    //:兜底复用同一条气泡时不再 push（只把它的「排队中」摘掉）——
    //:   否则那一条会在屏上变成两条。
    setMessages((x) => {
      const base = x.filter((m) => !isReceipt(m));
      if (!reuseId) return base.concat([user]);
      return base.map((m) => (m.id === reuseId ? { ...m, queued: false } : m));
    });
    if (queuedNow) {
      // 先问一句"能不能塞进正在跑的这一轮"——
      //   能，它不是新的一轮，不进等锁队列；由服务端在步的边界取走。
      //   不能（没在跑 / 是命令 / 带附件 / 有待审批），回落成今天的本地排队。
      if (canSteer) {
        // 先登记、再发请求 —— 这一轮可能在 POST 回来之前就结束了
        //   （纯问答一轮没有下一个边界），`task.end.steer_leftover` 会先到。
        //   先登记，兜底才找得到这条；被拒时再撤销。
        steerWaitRef.current.set(user.id, {
          msgId: user.id, text: _parsed.text,
          images: images || [], files: files || [],
        });
        let accepted = false;
        try {
          const r = await client.call("/api/steer", jsonRequest("POST", {
            text: _parsed.text, client_msg_id: user.id,
          }));
          accepted = !!(r && r.accepted);
        } catch { accepted = false; }
        if (accepted) {
          return;            // 这一条到此为止：它不是新的一轮
        }
        steerWaitRef.current.delete(user.id);
        setMessages((x) => x.map((m) => (m.id === user.id ? { ...m, steer: false } : m)));
      }
      // 排上：登记成"等待者"，把撤回要还原的东西一起存好。
      const ticket = { id: user.id, cancelled: false };
      heldSendsRef.current.push(ticket);
      heldSendDataRef.current[user.id] = {
        //:放回输入框的是用户可见的那句（附件前言剥掉）+ 原始 chips。
        text: _parsed.text, images: images || [], files: files || [], sentTo,
      };
      // 轮询而不是 promise 链：前面的轮次可能来自审批回答 / 别的通道，
      //    没有一个统一的事件能等；150ms 的粒度对"人点发送"完全够。
      while (!ticket.cancelled
             && (inflightTurnsRef.current > 0
                 || heldSendsRef.current[0] !== ticket)) {
        await new Promise((r) => setTimeout(r, 150));
      }
      const at = heldSendsRef.current.indexOf(ticket);
      if (at >= 0) heldSendsRef.current.splice(at, 1);
      delete heldSendDataRef.current[user.id];
      if (ticket.cancelled) return;      // 撤回：气泡已摘、请求根本没发
      // 轮到自己了，撤掉排队标记，接着走下面那条真实发送
      setMessages((x) => x.map((m) => (m.id === user.id ? { ...m, queued: false } : m)));
    }
    inflightTurnsRef.current += 1;
    setBusy(true);
    // 别在这里清浮窗：那条挂起的审批服务端还在（要到本轮轮末
    //   才被中和），于是 `busy` 一开、轮询立刻又把它捞回来，闪一下又消失。
    //   所以交给轮询 / 收尾那一次拉取去决定（它读的是权威 state）。
    //: POST 回来时用户可能已经切走了 ——
    //:回复只许落进发送那一刻的那一屏。切走了就只记账、不画
    //:   （消息已经落库；切回来 / 刷新都在 —— 不把 A 的话画到 B 头上）。
    const stillMine = () => !sentViewKey || viewKeyRef.current === sentViewKey;
    try {
      const result = await client.call("/api/chat", jsonRequest("POST", body));
      // 兜底：事件流断了（或没连上）时，这里仍然把回复补上 —— 但已经收到 `task.end`
      //   就别再加一遍（同一句话出现两次是最难查的那种"看起来像模型抽风"的 bug）。
      // 两条通道都可能到，走同一个去重口（`sawTaskEnd` 那个标志挡不住"POST 先回"）。
      // 这里拿到的是权威那一份（落库的就是它、没经过 `_TEXT_CAPS` 截断）：
      //   · SSE 已经推过一条截断版，`pushAssistant` 走升格分支，就地把它换全
      //     （详情见 `pushAssistant` 顶上那一段）
      //   · SSE 没到，走追加分支，回复照旧不丢
      // `ts` 必须带：SSE 那条可能被拦下 / 没到，那时这条是唯一一条，
      //   不带 `ts` 就是「第二条没有时间戳」。
      // 无主理人群聊回的是 `group_replies`（多条、带 speaker）——
      //   此时 `result.reply` 是空串，不许再补一条"主持人回复"。
      if (Array.isArray(result.group_replies) && result.group_replies.length) {
        if (stillMine()) pushGroupReplies(result.group_replies);
      } else if (stillMine() && result.reply) {
        // 空回复不画气泡：停在等审批时服务端回的是空串（模型挂起了，
        //   不是哑了）；不判这一下，用户会看到「(无回复)」+ 紧随其后的真回复
        //   = 一条空壳加一条真话。
        // POST 那条是权威那一份（没经过 SSE 的 4000 字截断）——
        //   产物也跟它一起带（若 SSE 已经先到，`pushAssistant` 的升格分支
        //   会保留已有那条上的 `artifacts`，不会丢）。
        //:大产物同 `task.end`：两条路各喊一次，按 id 去重。
        notifyLargeArtifacts(result.artifacts);
        pushAssistant(result.reply || "", {
          ts: Date.now() / 1000,
          //:同 `task.end`：被打断收尾的那一句是回执（两条到达顺序不定，
          //:   两边都标 —— 否则先到的那条没标，去重后留下的正是没标的那条）。
          ...(result.cancelled ? { kind: CANCEL_RECEIPT } : {}),
          ...((Array.isArray(result.artifacts) && result.artifacts.length)
            ? { artifacts: result.artifacts } : {}),
        });
      }
    } catch (e) {
      //:同 `stillMine`：切走了就别把这一屏的错误画到别人头上。
      if (stillMine()) {
        setMessages((x) => x.concat([{ id: "a" + Math.random(), role: "assistant", error: true, content: e.message }]));
      }
    } finally {
      // 这一条飞完了，从"还在飞"里摘掉（「排队中」那一格不再算它）。
      if (sentTo) inflightRef.current.delete(sentTo);
      // 这条的「排队中」标记到此为止（它已经跑完了）。
      setMessages((x) => x.map((m) => (m.id === user.id ? { ...m, queued: false } : m)));
      // 只有最后一条回来才关灯 —— 否则第二条还在跑，横幅 / 审批轮询
      //    已经跟着第一条的 finally 一起停了。
      inflightTurnsRef.current = Math.max(0, inflightTurnsRef.current - 1);
      if (inflightTurnsRef.current === 0) { setBusy(false); setProv(""); refresh(); }
      //:成员回复已经由 `group_replies` 落到消息里，状态条退场。
      setGroupWork(null);
      // 审批看不见：这一行不能是 `setApproval(null)`（无条件清浮窗）。
      //    而"挂起即停车"那一轮的待审批正是在这一刻产生的：
      //    事件流里 `approval.request` 与 `task.end` 同一秒，
      //    于是那一行把刚弹出来的面板打回去，而且之后再没有任何东西会去拉它
      //    （`busy` 那一支的 1.2s 轮询跟着 `setBusy(false)` 一起停了）
      //    所以症状 = 面板永远不出现（用户看到的是 `(无回复)` + 一片安静）。
      //    判据与 `answer()` 的 catch 那条同一条："这一轮结束了"不是
      //    "没有待审批" —— 那句结论只有权威 state（`GET /api/approval`）说得出来。
      //    所以这里改成拉一次（不再自己下结论）；`seq` 那道闸保证迟到的旧响应
      //    不会覆盖新状态。
      //    与上一行 `setBusy(false)` 的次序无所谓（两件事互不依赖）。
      loadApproval();
      // 接着当前那段跑完之后，补问一次"还有没有更早的" ——
      //   活跃那一屏从不装载这一段（它只追加），所以那个按钮没人告诉它该不该画。
      //   开新的那段不问（它确实没有更早的）。
      if (!startNew) probeViewHasMore();
      // 收口：POST 回来时若活动块还活着（SSE 断了 / 没跑成），把它标成 done ——
      //   否则它会一直转圈。正常情况下 `task.end` 已经标过，这里会因为 `live` 已为 false 而什么都不做。
      editActivity((a) => ({ ...a, live: false, done: true, endedAt: Date.now() }), { create: false });
    }
  }

  /** 撤回一条还排在本地的消息。
   *
   *  它之所以撤得回来，是因为这条请求还没发出去（见 `send()` 里的本地排队）。
   *     已经 POST 出去的那一条撤不回来 —— 这里如实返回 `false`。
   *  三件事一起做：摘气泡 · 从等待队列里删掉 · 内容放回输入框（文案 + 图片）。
   */
  function withdrawSend(msgId) {
    const t = heldSendsRef.current.find((q) => q.id === msgId);
    if (!t) return false;
    t.cancelled = true;
    heldSendsRef.current = heldSendsRef.current.filter((q) => q !== t);
    const back = heldSendDataRef.current[msgId] || {};
    delete heldSendDataRef.current[msgId];
    if (back.sentTo) inflightRef.current.delete(back.sentTo);
    setMessages((x) => x.filter((m) => m.id !== msgId));
    setPrefill({ seq: Date.now(), text: back.text || "",
                 images: back.images || [], files: back.files || [] });
    return true;
  }

  /** 回答一次审批 —— 服务端接着把那一轮跑完（"同意 = 重放"）。
   *
   *  两条约束：
   *
   *  ① 回答之后绿框（状态横幅）不能消失 —— 这里必须 `setBusy(true)`。
   *    审批"挂起即停车"，那一轮 `task.end` 已经把 `busy` 打成 `false`；
   *    而横幅的可见性（`Composer.jsx`）只认 `busy`
   *，回答之后服务端接着跑的那一轮，横幅再也没被打开过。
   *  ② 同一句回复不能出现两遍 —— 不能自己 `concat`，
   *    必须走 `pushAssistant` 那个去重口，而且带 `ts`。
   *
   *，先开 `busy`（横幅回来）· 回复走同一个去重口 · 完事再关。
   *  `setBusy(true)` 必须在 `await` 之前 —— 服务端那一轮在 POST 返回之前就开始了。
   *  `ts` 用本地时钟：这条回复的 SSE 帧可能还没到（也可能已经到了，被去重口吞掉，正是要的）。
   */
  /** 把用户的选择 / 那句话回传给服务端。
   *
   *  *body* 是 `{choice}` 或 `{text}` —— 由 `approval-model.submitBody` 一处决定，
   *  这里不自己拼。
   *
   *  `setApproval(null)` 只在成功那一支。服务端对
   *  不在本次实际给出的选项里的 choice 回 409 `bad_choice` 且什么都不结算
   *  （`core/approval.py::reply_decision`），如果提前清掉，结果是
   *  浮窗在用户这边消失了、那条审批还在服务端挂着，它永远不会被回答，
   *  而用户以为按掉了。
   *，成功才收；失败要出声 + 立刻重拉一次（把真实现状摆回来）。
   */
  async function answer(body, request_id) {
    //:连点两次，服务端跑了两轮、模型回了两遍。
    //:一格在途标记，第二下直接吞掉（服务端那边同样有一道占用闸）。
    if (answeringRef.current) return;
    answeringRef.current = true;
    //:提交即收浮窗。`/api/approval` 是同步 POST（那一轮跑完才回包）
    //:   —— 等到回包再收的话，浮窗会一直盖在正在走的步骤流上面，
    //:   直到回复出现。失败时下面那条 `loadApproval`
    //:   会把真实现状（还挂着 / 已结算）摆回来。
    setApproval(null);
    inflightTurnsRef.current += 1;
    setBusy(true);
    //:与读浮窗同一个会话键（`approvalUrl` 一处拼）——
    //:群聊里答的那一句要跑在那个群的会话上，否则结算找不到它。
    const gid = chatGroupRef.current ? chatGroupRef.current.id : "";
    try {
      const r = await client.call(approvalUrl(gid), jsonRequest("POST", { ...body, request_id }));
      if (r && r.reply) pushAssistant(r.reply, { ts: Date.now() / 1000 });
    } catch (e) {
      //: 先松开在途标记 —— `loadApproval` 第一行会看它（否则这次重拉被跳过）。
      answeringRef.current = false;
      notify(e.message);
      // 失败不是"关掉"的信号 —— 立刻问一次服务端"现在到底还挂着什么"。
      await loadApproval();
    } finally {
      answeringRef.current = false;
      inflightTurnsRef.current = Math.max(0, inflightTurnsRef.current - 1);
      if (inflightTurnsRef.current === 0) { setBusy(false); refresh(); }
      //:这一轮里模型可能又问了下一个问题 —— 在途期间那次 `task.end`
      //:   里的 `loadApproval` 被上面的标记跳过了，这里补一次。
      loadApproval();
    }
  }
  /** 拉一份设置（打开弹窗 / 每次保存之后重拉 —— 同一处）。
   *  重拉是必要的：保存之后生效值可能被后端矫正（比如框架预设转成「自定义」），
   *    不重拉界面就还显示着我们以为写进去的那个（标签在说谎）。
   */
  async function reloadSettings() {
    try { setSettings(await client.call("/api/settings")); } catch { setSettings(null); }
  }

  async function openSettings() { setSettings(null); setSettingsOpen(true); await reloadSettings(); }

  /** 「50 条存储提醒」上那个不再提醒。
   *
   *  它写的是持久化的"不许"（`settings.json` 的 `thread_notice_off`）——
   *    与「安全提示」那条存 `localStorage` 的正好相反。必须有一个
   *    看得见的撤回口，那就是设置 → 对话 →「关掉存储提醒」那一格。
   *  写失败要出声：静默失败会让横幅下一次又冒出来，而用户以为已经关掉了
   *    （标签在说谎）。
   */
  async function dismissThreadNotice() {
    try {
      await client.call("/api/settings",
                        jsonRequest("POST", { thread_notice_off: true }));
      await reloadSettings();
    } catch (e) {
      notify(t("未保存：") + (e && e.message ? e.message : String(e)));
    }
  }

  /** 去拉一次"现在有哪些插件被屏蔽了"，把没给用户看过的排进队列。
   *
   *  它是"状态"那一侧 —— `plugin.blocked` 那个事件只是"叫你来读"
   *  （信号必须在它所指向的那份 state 可读之后才发）。
   *，两条路都走同一个拉取口：开机一次（补发掉线 / 关机期间发生的屏蔽）·
   *  收到事件一次（对账）。
   *
   *  取不到不弹错（同侧栏那两条）：拉不到就下次再说 —— 一次网络抖动
   *  不该在界面上长出一条红字。
   */
  async function loadBlockedNotices() {
    let d;
    try {
      d = await client.call("/api/quarantine");
    } catch { return; }
    const fresh = (d?.blocked || []).filter((e) => e && e.owner && !noticeSeen(e));
    if (!fresh.length) return;
    setBlockedQueue((q) => {
      //:去重：开机那一拉与事件那一拉可能同时把同一条送进来
      //:   （同一条被弹两次 = 用户以为有两件事）。
      const have = new Set(q.map((x) => x.owner));
      return q.concat(fresh.filter((e) => !have.has(e.owner)));
    });
  }

  /** 弹窗一次摊开一整批（有多个插件不支持时，冲突栏显示多个名字和位置）
   *，收口也跟着从"弹掉一条"变成"弹掉这一批"。
   *
   *  一条都不许漏 —— `rememberNotice` 是"这条给他看过了"的唯一记录，
   *  漏一条，下一次拉取又把它排进来，用户被同一条反复弹。
   *  也不能顺手 `setBlockedQueue([])` 了事：那一拉与这一按可能交错
   *  （见 `loadBlockedNotices` 的去重注释），清空会把刚进来、还没显示过的那条也抹掉。
   */
  function dismissBlocked(shown) {
    (shown || []).forEach(rememberNotice);
    const done = new Set((shown || []).map((e) => e && e.owner));
    setBlockedQueue((q) => q.filter((x) => !done.has(x.owner)));
  }

  /** 拉那份会话构成明细。
   *
   *  取不到要出声 —— 面板里写 `取不到读数：<原因>`，不回一张空表：
   *  "没有数据"与"有数据且全是 0"看起来一样，而后者会被读成一个结论
   *  （服务端那一侧也照这条办 —— 框架没起来时它回 503 而不是空面板）。
   *  失败也存进同一个 `ctxReport`（带 `error`）—— 分两个 state 的话，
   *  面板会先渲染空的、再渲染错误，中间那一帧看起来像"全 0"。
   */
  async function loadCtxReport() {
    try {
      setCtxReport(await client.call("/api/context"));
    } catch (e) {
      setCtxReport({ error: (e && e.message) || t("取不到读数") });
    }
  }

  /** 点顶栏那一行 → 开/关面板。打开时现拉一次（关着时的那些读数可能已经旧了）。 */
  function toggleCtx() {
    const next = !ctxOpen;
    setCtxOpen(next);
    ctxOpenRef.current = next;
    if (next) loadCtxReport();
  }
  //:点面板外面就关。
  //:判据收在 `context.js::ctxPanelCloses` —— 面板与读数按钮两个都要排除，
  //:   否则点读数那一颗会"先关、再开"、看着没反应（同 `Rail.jsx::Drawer` 里
  //:   `.rail` 那一条；`Composer` 那两枚 picker 也是同一个形状）。
  //:只在开着时挂（关着挂 = 白占一个 listener）；收尾摘掉。
  useEffect(() => {
    if (!ctxOpen) return undefined;
    const close = (e) => {
      if (!ctxPanelCloses(e.target)) return;
      setCtxOpen(false);
      ctxOpenRef.current = false;
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [ctxOpen]);

  /** 拉正在跑的任务（顶栏两格 + 两个浮窗的数据源）。
   *
   *  与 `/api/context` 同一条判据：取不到要出声（存 `{error}`），
   *    不回一张空表 —— "没有任务"与"读不到"长得一样，而前者会被读成一个结论。
   *  会话键由服务端推（`?group=` 只带群 id）—— 前端不许自己拼会话键
   *    （同 `/api/stop` 那条；写错一位的症状是"面板永远空"）。
   */
  async function loadTasks() {
    try {
      const gid = chatGroupRef.current ? chatGroupRef.current.id : "";
      const url = gid ? `/api/tasks?group=${encodeURIComponent(gid)}` : "/api/tasks";
      setTasks(await client.call(url));
    } catch (e) {
      setTasks({ error: (e && e.message) || t("取不到任务列表") });
    }
  }

  /** 点顶栏那一格 → 开/关那个浮窗。打开时现拉一次（关着时的读数已经旧了）。 */
  function toggleTasks(which) {
    const next = tasksOpen === which ? "" : which;
    setTasksOpen(next);
    if (next) loadTasks();
  }

  /** Ctrl+B：把这条会话正在前台跑的任务转到后台。
   *
   *  没有前台任务时什么都不发生（不弹提示、不抢按键）——
   *  快捷键不该在没有可作用对象时吵闹。
   *  判据是 `bgableRef` —— 它由 `tool.start.backgroundable` 维护（服务端的口径）。
   */
  async function toBackground() {
    try {
      const gid = chatGroupRef.current ? chatGroupRef.current.id : "";
      const url = gid ? `/api/background?group=${encodeURIComponent(gid)}` : "/api/background";
      const d = await client.call(url, jsonRequest("POST", {}));
      if (!d.ok) notify(d.note || t("现在没有可转的前台任务"));
    } catch (e) {
      notify((e && e.message) || t("转后台失败"));
    }
  }

  //:任务浮窗的「点外面就关」—— 与上面那条逐字同一个判据
  //:   （`ctxPanelCloses`；它已经排除了 `.task-chip`，否则 mousedown 先关、
  //:   click 再开，看着像点了没反应）。
  useEffect(() => {
    if (!tasksOpen) return undefined;
    const closeTasks = (e) => {
      // 判据是同一个 `ctxPanelCloses`，但别把字面量写第二遍：
      //   `test_desktop_context` 的负对照拿 `ctxPanelCloses(e.target)` 当靶子，
      //   写第二份就会让"判据被换成常量"那个变异抓不住（恒绿）。
      const node = e.target;
      if (ctxPanelCloses(node)) setTasksOpen("");
    };
    document.addEventListener("mousedown", closeTasks);
    return () => document.removeEventListener("mousedown", closeTasks);
  }, [tasksOpen]);

  //: Ctrl+B 的键位只在真有可转的任务时才抢 —— 否则浏览器/系统那个
  //:   Ctrl+B（加粗之类的）被我们吃掉，而用户什么也没得到。
  useEffect(() => {
    const onKey = (e) => {
      if (!e.ctrlKey || e.altKey || e.shiftKey || e.metaKey) return;
      if (String(e.key || "").toLowerCase() !== "b") return;
      if (bgableRef.current.size === 0) return;
      e.preventDefault();
      toBackground();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  /** 左栏导航。「新建任务」＝ 开一段新的，它把这一屏打回草稿（`draft`），
   *  下一次发送由服务端开一段新的。
   *
   *  它不在这里发 `/clear`：用户可能只是点一下又走了。清空的副作用
   *    推迟到"真的发出一条消息"那一刻（`send()`）—— 否则每点一次就铸一条
   *    0 轮的空线程。
   */
  /** 右键那一段，开菜单。位置夹在视口里（否则靠边的行点不到）。 */
  function openCtx(e, t) {
    e.preventDefault();
    //:`h = 300` —— 这一格会长到「跟随默认 + N 条配置」。
    //:夹的是开菜单那一刻的位置，而高度是
    //:拉长之后才知道的，按最坏情形夹（否则菜单从屏幕下沿出去）。
    const w = 210, h = 300;
    setCtxMenu({
      x: Math.min(e.clientX, Math.max(0, window.innerWidth - w - 8)),
      y: Math.min(e.clientY, Math.max(0, window.innerHeight - h - 8)),
      //:`view:""` —— 每次都从第一层开。少了它，上一次开过"用什么模型"
      //:之后右键另一段，菜单会直接落在模型那一层（那一层说的是别的段）。
      t, confirm: false, view: "", model: null, modelErr: "",
    });
  }

  // 菜单开着时：点外面 / 按 Esc / 滚一下，关掉。
  //   [!] 三个都要 —— 少一个就会留一个关不掉的浮层（而它盖在内容上面）。
  useEffect(() => {
    if (!ctxMenu) return undefined;
    const close = () => setCtxMenu(null);
    const esc = (e) => { if (e.key === "Escape") close(); };
    window.addEventListener("pointerdown", close);
    window.addEventListener("keydown", esc);
    window.addEventListener("wheel", close, { passive: true });
    return () => {
      window.removeEventListener("pointerdown", close);
      window.removeEventListener("keydown", esc);
      window.removeEventListener("wheel", close);
    };
  }, [ctxMenu]);

  /** 删掉一段对话。
   *
   *  它是不可逆的（与 `/clear` 正相反：那个是留档），界面先确认一次，
   *  而服务端不替用户确认（它只管"这个 id 在不在本通道名册里"）。
   *  删的若是正在看的那一段，退出查看态、回草稿屏（否则屏幕上留着一屏
   *  "已经不存在了"的消息）。
   */
  async function removeThread(th) {
    const label = th.title || t("（没有用户消息）");
    try {
      const out = await client.call("/api/threads/delete",
                                    jsonRequest("POST", { storage_id: th.storage_id }));
      setCtxMenu(null);
      if (viewing && viewing.storage_id === th.storage_id) {
        setViewing(null); setMessages([]); setProv(""); setDraft(true);
      }
      await refreshThreads();
      notify(out && out.was_current ? t("删掉了「{label}」，并开了一段新的", { "label": label }) : t("删掉了「{label}」", { "label": label }));
    } catch (e) {
      setCtxMenu(null);
      notify(e.message || String(e));
    }
  }

  /** 重新拉左栏那一栏「群聊」（与 `Groups` 页同源）。 */
  async function refreshSideGroups() {
    try {
      const d = await client.call("/api/groups");
      setSideGroups((d && d.groups) || []);
    } catch (e) {
      // 后端没起来 / 请求挂了：左栏保持原样，不弹错（同挂载那条）。
    }
  }

  /** 右键删除一个群聊（与删除会话同一套逻辑）。
   *
   *  与「删除这段对话」同一套交互：右键 → 删除 → 二次确认（不可恢复）。
   *  后端 `groups.delete` 只删账本条目，那条会话的消息行一条都不删
   *     （`core/groups.py::delete` 的判据：留档；`history_search` 还搜得到）。
   *  删的是正在看的那个群，退出到草稿屏（同 `removeThread` 对
   *     `viewing` 的处理；否则屏幕上留着一个已经不存在的群）。
   */
  /** 点删除 = 先问一次（解散不可逆，一次点击不许发生）。 */
  function removeGroupChat(g) {
    setCtxMenu(null);
    setConfirmGroup(g);
  }

  /** 真的解散。只在确认之后调用。 */
  async function doRemoveGroupChat(g) {
    const label = (g && g.name) || t("这个群聊");
    try {
      const out = await client.call(
        "/api/groups/delete", jsonRequest("POST", { id: g.id }));
      setCtxMenu(null);
      setConfirmGroup(null);
      if (chatGroup && chatGroup.id === g.id) leaveGroup();
      setSideGroups((x) => x.filter((v) => v.id !== g.id));
      await refreshSideGroups();
      notify((out && out.message) || t("已解散「{label}」", { "label": label }));
    } catch (e) {
      setCtxMenu(null);
      notify(e.message || String(e));
    }
  }

  /** 批量删除。
   *
   *  走已有那一条端点的 N 次，不新开一条"批量"口 ——
   *    因为那条端点把归属核对做在了服务端（`thread_channel(sid) == 本通道键`：
   *    能不能删由服务端说了算，不由请求体说了算）。
   *    另开一条批量口 = 把那条判据抄第二份，而且抄的那份更容易写松。
   *  顺序有讲究：先删不是当前的那些，最后才删当前那段 ——
   *    当前那段一删，服务端立刻铸一段新的，名册会变。
   *  部分失败要如实报（"删了 3 段，2 段没删掉"），不许说成"删完了"。
   */
  async function removeThreads(ids) {
    const list = Array.from(ids || []);
    if (!list.length) return;
    setBatch(null); setCtxMenu(null);
    const cur = (threads.find((t) => t.is_current) || {}).storage_id || "";
    const ordered = list.filter((x) => x !== cur).concat(list.filter((x) => x === cur));
    let ok = 0; const bad = [];
    for (const sid of ordered) {
      try {
        const out = await client.call("/api/threads/delete",
                                      jsonRequest("POST", { storage_id: sid }));
        if (out && out.ok === false) bad.push(sid); else ok += 1;
      } catch (e) { bad.push(sid); }
    }
    //:删掉的里面有正在看的那一段，屏幕上那段不该再留着。
    if (viewing && list.indexOf(viewing.storage_id) >= 0) {
      setViewing(null); setMessages([]); setProv(""); setDraft(true);
    }
    await refreshThreads();
    notify(bad.length
      ? t("删掉了 {ok} 段，另有 {bad.length} 段没删掉", { "ok": ok, "bad.length": bad.length })
      : t("删掉了 {ok} 段对话", { "ok": ok }));
  }

  /** 置顶 / 取消置顶（「置顶此对话 / 此集」）。
   *  `kind` = `"thread"`（那一段）或 `"group"`（左栏那一集 = 一个目录）。
   *  置顶状态住服务端（`short_term.pins` 表，跨重启），这里只发意图、
   *    然后重拉名册，界面不自己猜结果（别让界面说一句没落地的话）。
   */
  async function togglePin(kind, key) {
    const now = kind === "thread" ? pinnedRef.current.threads : pinnedRef.current.groups;
    const on = now.indexOf(key) < 0;
    setCtxMenu(null);
    try {
      await client.call("/api/pins", jsonRequest("POST", { kind, key, on }));
      await refreshThreads();
      notify(on ? (kind === "thread" ? t("已置顶这段对话") : t("已置顶这一集"))
                : t("已取消置顶"));
    } catch (e) {
      notify(t("无改动：") + (e && e.message ? e.message : String(e)));
    }
  }

  /** 右键一个目录组（那一"集"）—— 菜单只有两项：置顶它 / 批量操作。 */
  function openGroupCtx(e, dir) {
    e.preventDefault();
    setCtxMenu({ x: Math.min(e.clientX, Math.max(0, window.innerWidth - 218)),
                 y: Math.min(e.clientY, Math.max(0, window.innerHeight - 140)),
                 t: null, confirm: false, view: "group", g: dir,
                 model: null, modelErr: "" });
  }

  /** 左栏群聊条目的右键菜单（与"删除这段对话"同一套）。
   *  `stopPropagation`：左栏空白处 / 别的右键处理器不该接这一下。 */
  function openGroupChatCtx(e, g) {
    e.preventDefault();
    e.stopPropagation();
    setCtxMenu({
      x: Math.min(e.clientX, Math.max(0, window.innerWidth - 218)),
      y: Math.min(e.clientY, Math.max(0, window.innerHeight - 140)),
      t: null, confirm: false, view: "groupchat", g,
      model: null, modelErr: "",
    });
  }

  /** 这一段用什么模型（会话右键那一格）。
   *
   *  界面这一侧只做一件事：让用户能把这一段换成某条配置、
   *  或者切回「跟随默认」。
   *  "改默认之后已有记录的不受影响"是服务端的事（惰性冻结）——
   *    界面不自己判"这一段有没有内容"，那个判据只有一处
   *    （`core/session_model` + 走到 `PostProcessStage`）。
   *  所以这里不画"已冻结 / 未冻结"的那个开关，只在上面说清楚
   *    （`source` 那个字段）—— 画一个让人以为是开关的东西 = 标签在说谎。
   */
  async function openModelMenu(t) {
    setCtxMenu((m) => ({ ...m, view: "model", model: null, modelErr: "" }));
    try {
      const d = await client.call(
        `/api/session-model?storage_id=${encodeURIComponent(t.storage_id)}`);
      setCtxMenu((m) => (m && m.view === "model"
        ? { ...m, model: d, modelErr: d.error || "" } : m));
    } catch (e) {
      setCtxMenu((m) => (m && m.view === "model" ? { ...m, modelErr: e.message } : m));
    }
  }

  async function pickModel(th, configId) {
    try {
      const out = await client.call("/api/session-model",
                                    jsonRequest("POST", { storage_id: th.storage_id,
                                                          config_id: configId }));
      setCtxMenu(null);
      notify((out && out.message) || t("已切换。"));
    } catch (e) {
      setCtxMenu(null);
      notify(e.message || String(e));
    }
  }

  function nav(key, stub) {
    if (stub) return notify(t("这个功能还没做。"));
    setPage(key);
    if (key === "chat") {
      setMessages([]); setProv(""); setViewing(null); setDraft(true);
      //:新建任务同样离开群聊（同一类"失去焦点"）。
      clearGroupMode();
    }
  }

  // 判据是 `draft` ——「开屏 / 新建任务那一屏」。
  //   只用 `!viewing` 不够：`viewing` 为空有两种情况，
  //   一种是草稿、另一种是"看过某一段之后回到当前对话"，而后者不该显示
  //   欢迎页（他手上有活）。`draft` 把两半一起管住了：
  //   看过任何一段之后 `draft` 就是 false。
  const empty = draft && !messages.length && !provisional;
  //: 「此刻那一轮还在跑」—— 由最后一条是不是活着的活动块决定（渲染用；`delta` 用的是 ref）。
  //:与 `actRef` 同一个口径：回执不算"最后一条" —— 否则「已打断」那句
  //:   一落在活块后面，底部长出一条假的「正在处理」。
  const _liveIdx = lastNonReceipt(messages);
  const liveActivity = _liveIdx >= 0
    && messages[_liveIdx].role === "activity"
    && messages[_liveIdx].live;
  //:这一轮跑在别的一段上（你正看着的不是它）——
  //:   「正在处理」那行字与活动块都不属于这一屏。
  //:判据与事件过滤同一个函数（`liveEventBelongs`），不是再写一遍。
  //:判据必须与 `_liveMine()` 同一个身份函数：
  //:只用 `viewing || ""` 时，它在群聊 / 草稿 / 活跃态下恒为 `""`
  //:，「这个加载中不是这一屏的」永远判不出来。
  const viewKeyNow = viewKeyOf({
    groupId: chatGroup ? chatGroup.id : "",
    viewingId: viewing ? viewing.storage_id : "",
    draft,
    currentId: ((threads.find((t) => t.is_current) || {}).storage_id || ""),
  });
  const liveElsewhere = !liveEventBelongs(viewKeyNow, liveSidRef.current);

  //:横幅那个「回到当前对话」出不出现 —— 三个条件缺一不可
  //:  （没有活跃对话、近 30 分钟没有更新时就不显示这个按钮）。
  //:  ① 正在查看某一段；② 名册里有一条"当前"、且不是正在看的这条（看的就是当前那条
  //:时，这个按钮点了等于没点）；③ 它活跃（最后一条消息在 `ACTIVE_WINDOW_MS` 之内）。
  //:`.find(t => t.is_current)` 取的是服务端给的那个标记（`ended_at IS NULL`），
  //:    不在前端另算一遍"哪条最近"—— 两套算法迟早给出两个答案。
  const liveThread = (() => {
    if (!viewing) return null;
    const cur = threads.find((t) => t.is_current);
    if (!cur || cur.storage_id === viewing.storage_id) return null;
    const at = cur.last_at ? Date.parse(cur.last_at) : NaN;
    if (Number.isNaN(at) || Date.now() - at > ACTIVE_WINDOW_MS) return null;
    return cur;
  })();

  // 抽屉摆在 `.shell` 外面：它是 `position:fixed` 的浮层，本来就不该占 grid 单元
  //   （规范如此），但放在壳外面就完全不必依赖那条。缩进就没跟着改（改动越小越好）。
  return <>
  <div className={"shell" + (resizing ? " resizing" : "")} style={{ "--side-w": sideW + "px" }}>
    {ctxMenu && (
      <div className="ctx-menu" style={{ left: ctxMenu.x, top: ctxMenu.y }}
           onPointerDown={(e) => e.stopPropagation()}>
        {/* 左栏空白处右键 —— 只有一项「批量操作」。
            它复用 `ctxMenu` 那套"点外面 / Esc / 滚一下就关"（三个都在），
              不另起一个浮层 —— 另起一个就要把那三条判据再写一遍。 */}
        {/* 右键一个目录组（那一"集"）——「置顶此集」。 */}
        {/* 左栏群聊条目右键 —— 与"删除这段对话"同一套（先确认）。 */}
        {ctxMenu.view === "groupchat" && !ctxMenu.confirm ? (
          <>
            <div className="ctx-menu-note" title={ctxMenu.g?.name}>
              {ctxMenu.g?.name || t("群聊")}
            </div>
            <button className="ctx-item"
                    onClick={() => setCtxMenu({ ...ctxMenu, confirm: true })}>
              <Trash2 size={13} />{t("删除群聊")}
            </button>
          </>
        ) : ctxMenu.view === "group" ? (
          <>
            <div className="ctx-menu-note" title={ctxMenu.g}>{tailName(ctxMenu.g)}</div>
            <button className="ctx-item" onClick={() => togglePin("group", ctxMenu.g)}>
              <Pin size={13} />
              {pinnedRef.current.groups.indexOf(ctxMenu.g) >= 0 ? t("取消置顶此集") : t("置顶此集")}
            </button>
            <button className="ctx-item"
                    onClick={() => { setBatch({ sel: {}, confirm: false }); setCtxMenu(null); }}>
              <Check size={13} />{t("批量操作")}
            </button>
          </>
        ) : ctxMenu.batchMenu ? (
          <button className="ctx-item"
                  onClick={() => { setBatch({ sel: {}, confirm: false }); setCtxMenu(null); }}>
            <Check size={13} />{t("批量操作")}
          </button>
        ) : ctxMenu.confirm ? (
          <>
            <button className="ctx-item danger"
                    onClick={() => (ctxMenu.view === "groupchat"
                      ? removeGroupChat(ctxMenu.g)
                      : removeThread(ctxMenu.t))}>
              {t("确认删除（不可恢复）")}
            </button>
            <button className="ctx-item" onClick={() => setCtxMenu(null)}>{t("取消")}</button>
          </>
        ) : ctxMenu.view === "model" ? (
          /* 这一段用什么模型。
             上面那行必须说清"现在是冻住的还是跟默认" ——
                不然用户点了「跟随默认」之后，看到菜单里还列着一堆配置，
                不知道那个动作到底生效没有（标签在说谎）。 */
          <>
            <button className="ctx-item" onClick={() => setCtxMenu({ ...ctxMenu, view: "" })}>
              <ChevronLeft size={13} />{t("用什么模型")}
            </button>
            <div className="ctx-menu-note">
              {!ctxMenu.model && !ctxMenu.modelErr ? t("读取中…") : null}
              {ctxMenu.modelErr ? ctxMenu.modelErr : null}
              {ctxMenu.model ? (
                ctxMenu.model.source === "frozen"
                  ? <>{t("这一段的模型已定：{ctxMenu.model.name}。之后改默认不影响它。", { "ctxMenu.model.name": ctxMenu.model.name })}</>
                  : ctxMenu.model.config_id
                    ? <>{t("跟随默认（{ctxMenu.model.default_name}）—— 收到第一条回复后就定在这条上。", { "ctxMenu.model.default_name": ctxMenu.model.default_name })}</>
                    : <>{t("目前还没有默认配置，将使用设置文件里的模型。")}</>
              ) : null}
            </div>
            <button className="ctx-item" disabled={!ctxMenu.model}
                    onClick={() => pickModel(ctxMenu.t, "")}>
              {t("跟随默认")}
            </button>
            {(ctxMenu.model?.options || []).map((o) => (
              <button key={o.id}
                      className={"ctx-item" + (o.id === ctxMenu.model.config_id ? " on" : "")}
                      onClick={() => pickModel(ctxMenu.t, o.id)}>
                {o.name}<span className="muted">{o.card_label}</span>
              </button>
            ))}
            {(ctxMenu.model && !ctxMenu.model.options.length) && (
              <div className="ctx-menu-note">{t("还没有任何配置 —— 去「端配置」建一条。")}</div>
            )}
          </>
        ) : (
          <>
            <div className="ctx-title">{ctxMenu.t.title || t("（没有用户消息）")}</div>
            <button className="ctx-item" onClick={() => openModelMenu(ctxMenu.t)}>
              <Cpu size={13} />{t("用什么模型")}
            </button>
            <button className="ctx-item" onClick={() => setCtxMenu({ ...ctxMenu, confirm: true })}>
              <Trash2 size={13} />{t("删除这段对话")}
            </button>
            {/* 行上右键也给一条「批量操作」—— 最自然的落点就是在某一行上右键；
                空白处那条也留着。 */}
            <button className="ctx-item"
                    onClick={() => { setBatch({ sel: {}, confirm: false }); setCtxMenu(null); }}>
              <Check size={13} />{t("批量操作")}
            </button>
            {/* 第四项：「置顶此对话」，已置顶时翻成"取消"。 */}
            <button className="ctx-item" onClick={() => togglePin("thread", ctxMenu.t.storage_id)}>
              <Pin size={13} />
              {pinnedRef.current.threads.indexOf(ctxMenu.t.storage_id) >= 0
                ? t("取消置顶此对话") : t("置顶此对话")}
            </button>
          </>
        )}
      </div>
    )}
    <aside className="sidebar">
      {/* 左栏右缘那根拖动条：9px 摸得到、1px 看得见（样式见 `.side-resizer`）。 */}
      <div className={"side-resizer" + (resizing ? " on" : "")} title={t("拖动调整左栏宽度")} onPointerDown={startResize} />
      <div className="brand"><img src={LOGO} /><b>Crannia</b></div>
      <nav>{NAV.map(([key, label, Icon, badge]) => <button key={key} className={`nav ${page === key ? "active" : ""}`} onClick={() => nav(key, !!badge && badge === "未实现")}><Icon size={18} /><span>{t(label)}</span>{badge && <small>{t(badge)}</small>}</button>)}</nav>
      {/* 侧栏「任务」列的是本通道的会话名册，
          点一条把它的尾部一页装载进聊天区（`openThread`）。
          这里只列不选 —— "选的是哪一条"由聊天区那条横幅说（单一真相）。 */}
      {/* 群聊对话也显示在左边栏里，条目显示群聊名字 ——
          单独一栏，点条目直接进群。 */}
      {sideGroups.length ? (
        <section className="side-chats">
          <h3 className="side-chats-title">{t("群聊")}</h3>
          {sideGroups.map((g) => (
            <button className={"side-chat" + (chatGroup && chatGroup.id === g.id ? " on" : "")}
                    key={g.id} title={g.name + "　" + t("右键可删除")}
                    onClick={() => openGroup(g)}
                    onContextMenu={(e) => openGroupChatCtx(e, g)}>
              <Users size={14} />
              <span className="side-chat-name">{g.name}</span>
            </button>
          ))}
        </section>
      ) : null}
      <section className="side-section tasks"
               onContextMenu={(e) => {
                 // 左栏空白处右键，只有一项「批量操作」。
                 //   行上那个右键必须先排掉 —— 行有自己的 `openCtx`，
                 //   而它只 `preventDefault()`、没有 stopPropagation
                 //   所以事件照样冒到这一层，把行菜单覆盖成批量菜单。
                 //   症状：行上右键只剩「批量操作」，
                 //   "用什么模型"和"删除这段对话"全不见了。
                 //   所以判据用 `defaultPrevented`（行那边已经 preventDefault 过了），
                 //   而不是 `closest(".thread-row")` —— 后者是第二份结构知识，
                 //   行 markup 一改就悄悄失效。
                 if (e.defaultPrevented) return;
                 e.preventDefault();
                 setCtxMenu({ x: Math.min(e.clientX, Math.max(0, window.innerWidth - 218)),
                              y: Math.min(e.clientY, Math.max(0, window.innerHeight - 100)),
                              t: null, confirm: false, view: "", model: null, modelErr: "",
                              batchMenu: true });
               }}>
        <h3>
          {/* 批量态：「任务」这两个字右边一个红字按钮。
              它必须在 `h3` 里面 —— `h3` 是
              `justify-content:space-between`，把按钮放在同一层会被推到另一头；
              所以「任务 + 按钮」包成一个左组。
              两段式：`批量删除` → `确认删除 N 段` + `取消`（二次确认；
                删对话不可逆，服务端那边也写着"界面必须先确认一次"）。
              一段没选时按钮是暗的、点了说一句"先点几段"而不是装死
                （画一个按了没反应的按钮 = 标签在说谎）。 */}
          <span className="h3-left">
            {t("任务")}
            {batch ? (
              batch.confirm ? (
                <>
                  <button className="batch-del"
                          onClick={() => removeThreads(Object.keys(batch.sel))}>
                    {t("确认删除 {Object.keys(batch.sel).length} 段", { "Object.keys(batch.sel).length": Object.keys(batch.sel).length })}
                  </button>
                  <button className="batch-cancel"
                          onClick={() => setBatch({ ...batch, confirm: false })}>{t("取消")}</button>
                </>
              ) : (
                <>
                  <button className={"batch-del" + (Object.keys(batch.sel).length ? "" : " dim")}
                          onClick={() => (Object.keys(batch.sel).length
                            ? setBatch({ ...batch, confirm: true })
                            : notify(t("请先选择删除条目")))}>
                    {t("批量删除")}
                  </button>
                  <button className="batch-cancel" onClick={() => setBatch(null)}>{t("退出")}</button>
                </>
              )
            ) : null}
          </span>
        </h3>
        {/* 50 条存储提醒。
            判据是 `settings &&` —— 不知道就不说话：`settings` 还没拉到那一刻
              既不显示、也不假装"已经关掉了"（读不到 ≠ 空）。
            形状是一支横幅（不是 modal）—— 它告知，不要求用户做决定。 */}
        {threadTotal != null && threadTotal > THREAD_NOTICE_AT && settings
         && !settings.thread_notice_off ? (
          <div className="thread-notice">
            <span>{t("已经存了")} <b>{threadTotal}</b> {t("段对话，清理一下会清爽些。")}</span>
            <button onClick={dismissThreadNotice}>{t("不再提醒")}</button>
          </div>
        ) : null}
        {!threads.length ? (
          // 「没读到」与「没有」必须分得开：
          //   两者都写「还没有会话历史」时，失败那句是假的 ——
          //   真相是"我没查到"（后端还没起来，`/api/threads` 返 503）。
          //   依据：入口和事实都不许凭空消失。
          <div className="empty" title={threadsError || ""}>
            {threadsError ? t("会话列表读取失败（服务尚未就绪）") : t("还没有会话历史")}
          </div>
        ) : (
          <ThreadTree threads={threads} viewing={viewing}
                      openThread={openThread} openCtx={openCtx}
                      unread={unread} queued={queued} batch={batch}
                      pinned={pinned} openGroupCtx={openGroupCtx} />
        )}
      </section>
      {/* 「工作区」不再单独成栏 —— 它的位置被上面那棵树接手：
          同一个信息（这些对话分别落在哪个目录）现在长在分组上，
          而不是另开一栏让人自己对。
          `/api/workspaces` 那条拉取留着（`refresh` 那个定时器本来就在问它），
            只是这一屏不再渲染它 —— 工作区列表本身仍然恒生效。 */}
      {/* 侧栏底部：一个版本号 + 一个设置按钮，
          设置按钮占一半宽（不是整栏宽的通栏按钮）。
          版本号取 `/api/status.version`（代码线 `core/version.py`）。
          读不到时留 `—` 占位，不是塌成空（入口和事实都不许凭空消失：
            整页刚重载 / `/clear` 那一刻这里还没有读数，但那一格必须还在）。 */}
      <footer>
        <span className="app-ver" title={status?.version ? `Crannia v${status.version}` : t("版本号还没读到")}>
          {status?.version ? `v${status.version}` : "—"}
        </span>
        <button className="settings-link" onClick={openSettings}><Settings2 size={14} />{t("设置")}</button>
      </footer>
    </aside>
    <main className="main">
      <header><strong>{page === "chat" ? (empty ? t("新建任务") : t("当前任务")) : ({ skills: t(SKILLS_TITLE), schedules: t("定时任务"), groups: t("协作群聊"), endpoint: t("端配置") }[page])}</strong>
        {/* 后台任务 / 子代理两格。
            只在计数 > 0 时渲染 —— 它们是状态，没有就不画；
               与「上下文 —」那条相反（那一条是入口，没读数也必须留占位）。 */}
        {page === "chat" && bgCount > 0 ? (
          <button className={"task-chip" + (tasksOpen === "bg" ? " on" : "")}
                  title={t("后台任务（点开看清单）")} onClick={() => toggleTasks("bg")}>
            {t("{bgCount} 个后台任务", { "bgCount": bgCount })}
          </button>
        ) : null}
        {page === "chat" && subCount > 0 ? (
          <button className={"task-chip" + (tasksOpen === "sub" ? " on" : "")}
                  title={t("子代理（点开看清单）")} onClick={() => toggleTasks("sub")}>
            {t("{subCount} 个子代理运行中", { "subCount": subCount })}
          </button>
        ) : null}
        <div className="header-right">{/* 顶栏那条常驻读数。
           它是唯一的入口，没读数时也必须渲染（文案是「上下文 —」而不是空）
           —— 空串 = 没有可点区域 = 面板在那段时间里根本进不去。 */}
        <button className={"ctx-readout" + (readout.cls ? " " + readout.cls : "")}
                title={t(readout.title)} onClick={toggleCtx}>{t(readout.text)}</button><span className={connected ? "online" : "offline"}><i />{connected ? (status?.framework_running ? t("就绪") : t("服务已连接")) : t("服务未连接")}</span>{status?.model && <em>{status.model_api_ready === false ? t("未配置") : status.model}</em>}<button className="round" onClick={openSettings}><Settings2 size={17} /></button></div></header>

      {/* ── 点顶栏那一行展开的会话构成面板。
          它是 `.main` 的子元素（不是 `header` 的）—— `header` 高 57px 且不定位，
          塞进去会被它自己的排版挤变形；而 `.main` 是 `position:relative`，
          面板挂在这里就锚在它右上角。 */}
      {ctxOpen ? (
        <section className="ctx-panel">
          <div className="ctx-panel-head">
            <b>{t("会话构成")}</b>
            <button className="round" onClick={() => { setCtxOpen(false); ctxOpenRef.current = false; }}><X size={14} /></button>
          </div>
          {ctxReport && ctxReport.error ? (
            <p className="ctx-error">{t("取不到读数：{ctxReport.error}", { "ctxReport.error": ctxReport.error })}</p>
          ) : (
            <>
              <div className="ctx-panel-body">
                {cellRows(ctxReport).map((c) => (
                  // `title` = 那一格的来源（只给值不给来源，
                  //   读的人无从判断它是不是一个保守兜底）。
                  <div className={"ctx-row" + (c.label.startsWith("↳") ? " ctx-sub" : "")} key={c.key} title={c.source}>
                    <span className="ctx-label">{c.label}</span>
                    <span className="ctx-val">{c.val}</span>
                    <span className={"ctx-delta" + (c.deltaCls ? " " + c.deltaCls : "")}>{c.delta}</span>
                  </div>
                ))}
              </div>
              <p className="ctx-menu-note">{noteText(ctxReport)}</p>
            </>
          )}
        </section>
      ) : null}

      {/* ── 顶栏那两格点开的浮窗。
          与上下文面板同一个位置（`.main` 的子元素）—— 挂进 `header`
             会被它 57px 高、不定位的排版挤变形（同上面那条）。 */}
      {tasksOpen ? (
        <TaskPanel which={tasksOpen} tasks={tasks} onClose={() => setTasksOpen("")} />
      ) : null}

      {page === "chat" ? (
        // 草稿态与发送后走同一个节点（不是两份 JSX 分支）——
        //   否则"掉回下面"那条 `transition` 根本不会播
        //   （换父节点 = 组件重挂载，CSS 过渡无从谈起）。
        <div className={"chat-main" + (empty ? " draft" : "")}>
        {empty ? (
          <Welcome running={status?.framework_running} connected={connected} logo={LOGO} shown={shown} />
        ) : (
          <>
            {/* 正在某个群里聊时的那一条横幅。
                它必须存在：没有它的话，"我在群里说话"与"我在跟自己说话"
                  长得一模一样 —— 而两者的受众完全不同（群里那条会进群会话、
                  有主理人时只有主理人看得见）。同 `view-bar` 那条判据。
                两个出口：回群聊列表 / 退出群聊回到普通对话（后者不清消息 ——
                  群里说过的话仍在它的会话里，清了屏幕只是把它从眼前拿开）。 */}
            {chatGroup ? (
              <div className="view-bar group-bar">
                <span className="view-tag">{t("协作群聊")}</span>
                <span className="view-title">{chatGroup.name}</span>
                <span className="group-bar-sub">
                  {chatGroup.members.length} 人 ·{" "}
                  {(() => { const h = chatGroup.members.find((m) => m.host);
                            return h ? t("只有主理人 {h.name} 看得见你说的话", { "h.name": h.name }) : t("多人聊天"); })()}
                </span>
                <button onClick={() => { setChatGroup(null); setGroupHist(null); setPage("groups"); }}>{t("← 群聊列表")}</button>
                <button className="view-exit" onClick={leaveGroup}>{t("退出群聊")}</button>
              </div>
            ) : null}
            {/* 正在看历史时的那一条横幅 —— 它说三件事：
                看的是哪一段 · 还能不能往上翻 · 能不能回到当前那段。
                它必须存在：没有它的话，"这一屏是过去"与"这一屏是我的活"
                  长得一模一样。而两者的语义只差一件事：发消息是接着这一段、
                  还是接着当前那段 —— 今天两边的答案都是"接着"，
                  区别在接的是哪一段，那正是这条横幅要说清的。
                第三个按钮不是常驻的（条件见 `liveThread`）：
                  它在没有活跃对话时不出现。
                  宁可不给，也不给一个点了没反应的东西。
                （同族：入口和事实都不许凭空消失 —— 但那条说的是
                  "有读数就必须看得见"，与"没有就别画"不冲突。） */}
            {viewing ? (
              <div className="view-bar">
                <span className="view-tag">{t("正在查看历史")}</span>
                <span className="view-title" title={viewing.title}>{viewing.title}</span>
                {viewHasMore ? (
                  <button disabled={viewLoading} onClick={olderThread}>{t("∧ 更早")}</button>
                ) : null}
                {liveThread ? (
                  <button className="view-exit" disabled={viewLoading} onClick={goCurrent}>{t("回到当前对话")}</button>
                ) : null}
              </div>
            ) : (viewHasMore ? (
              // 这一格不能只在 `viewing` 那一支里 —— 活跃对话那一屏
              //   否则根本没有上翻入口，而"往回翻自己刚聊过的"在任何一屏都该成立。
              //   所以活跃态也画一个只有它自己的条（没有标题、没有"回到当前对话"）。
              <div className="view-bar">
                <button disabled={viewLoading} onClick={olderThread}>{t("∧ 更早")}</button>
              </div>
            ) : null)}
            <section className="conversation" ref={conv} onScroll={onConvScroll}>
              {/* 事件流断了（浏览器在 3 秒一次地自动重连）。
                  这句话必须说 —— 否则用户看到的是"回复有了、步骤流没有"，
                  而那个形状看起来像模型没干活（没出声 ≠ 没跑）。 */}
              {streamDown ? (
                <div className="stream-down">
                  {t("事件流已断开，正在重连…（这段时间的工具步骤不会显示）")}
                </div>
              ) : null}
              {/* 无主理人群聊的成员状态条。
                  多人协作不要单 agent 的步骤流 —— 要看的是
                  「现在是谁在工作」，不是一串工具调用。 */}
              {groupMode && !liveElsewhere
                && (busy || (groupWork && groupWork.length)) ? (
                <div className="group-work">
                  <div className="group-work-head">{t("正在讨论")}</div>
                  <div className="group-work-members">
                    {(groupWork || []).map((w) => (
                      <div className={"group-work-member " + (w.state || "working")}
                           key={w.name}>
                        <Avatar slot={w.avatar} name={w.name} size={34}
                                title={w.name} />
                        <span className="group-work-name">{w.name}</span>
                        <span className="group-work-state">
                          {w.state === "done" ? t("已完成")
                            : w.state === "error" ? t("被拦住") : t("工作中…")}
                        </span>
                      </div>
                    ))}
                    {(!groupWork || !groupWork.length)
                      ? <span className="group-work-empty">{t("正在分派…")}</span>
                      : null}
                  </div>
                </div>
              ) : null}
              {/* 主理人群聊的开屏：
                  主理人头像像开屏动画的 logo 一样向上居中，下面一个淡绿色框
                  写着沟通说明；发送消息后头像与框消失。
                  条件 = 在群里 + 有主理人 + 还没说过话 + 不在跑 —— 一发消息就没了。 */}
              {chatGroup && chatHost && messages.length === 0 && !busy && !provisional && (
                <div className="group-welcome">
                  <div className="group-welcome-avatar">
                    <Avatar slot={chatHost.avatar} name={chatHost.name} size={96}
                            title={chatHost.name} />
                  </div>
                  <div className="group-welcome-box">
                    {t("在这里与此群聊主理人沟通，说明你的需求，主理人将指挥群聊分工。仅主理人可见消息")}
                  </div>
                </div>
              )}
              {/* 无主理人群聊的开屏：
                  所有人头像一起浮现，塞不下就两排，下面的方框写着讨论入口。
                  与主理人开屏同一套条件（还没说过话 + 不在跑），
                     只是把"一个头像"换成"全员头像"，盒子文案换成讨论入口。 */}
              {chatGroup && !chatHost && messages.length === 0 && !busy && !provisional && (
                <div className="group-welcome">
                  {/* 顶部一个圆形的群像图标，居中浮现；下面才是成员头像。 */}
                  <div className="group-welcome-emblem" aria-hidden="true">
                    <Users size={40} strokeWidth={1.7} />
                  </div>
                  <div className="group-welcome-members">
                    {(chatGroup.members || []).map((m) => (
                      <span className="group-welcome-member" key={m.name}
                            title={m.name}>
                        <Avatar slot={m.avatar} name={m.name} size={44}
                                title={m.name} />
                      </span>
                    ))}
                  </div>
                  <div className="group-welcome-box">
                    {t("群聊已建立，在这里开始讨论")}
                  </div>
                </div>
              )}
              {/* 收藏的锚点是那一轮的提问（`stars.message_id` = 那条 user 行）。
                  助手消息自己带的行号是"回答那一行"，收藏用不上 —— 所以往下传的是
                  往前找到的本轮提问，问与答用同一个锚点。
                  `anchor = m.rowId || null`（不是 `if (m.rowId) anchor = m.rowId`）：
                    刚发出去、还没落库的那条 user 消息必须把锚点清掉，
                    否则它会继承上一轮的锚点，新气泡上挂着一个能点、但指向
                    上一轮的 （点了会收藏错的那一轮）。 */}
              {(() => {
                let anchor = null;
                // 压缩摘要行只画最近一条：连着压两次会长出两条一模一样的旁注，
                //   而它们说的是同一件事（"上面那段被摘要了"）。旧那条不是被删掉
                //   —— 行还在库里，分页与搜索照旧，只是视图上让位给更新的一条。
                let lastSummary = "";
                messages.forEach((m) => {
                  if (m.kind === "history_summary") lastSummary = m.id;
                });
                return messages.map((m) => {
                  // 摘要行按 `P1` 就是 `user` 角色（摘要一律 user，靠 kind 辨认）
                  //   所以按 role 判会把它画成用户气泡，等于伪造一句用户说过的话。
                  //   它是边界标记，走中间那条细线。
                  if (m.kind === "history_summary") {
                    return m.id === lastSummary
                      ? <div key={m.id} className="compact-divider">{m.content}</div>
                      : null;
                  }
                  if (m.role === "user") anchor = m.rowId || null;
                  const star = ((m.role === "user" || m.role === "assistant") && anchor)
                    ? { id: anchor, on: starred.has(anchor), toggle: toggleStar }
                    : null;
                  // 成员回复用它自己的头像；主持人 / 普通会话
                  //   仍走 `chatHost`（没有主理人时退回框架 logo）。
                  const speakerAvatar = (m.speaker && m.avatar != null)
                    ? <Avatar slot={m.avatar} name={m.speaker}
                              size={30} title={m.speaker} />
                    : (chatHost
                      ? <Avatar slot={chatHost.avatar} name={chatHost.name}
                                size={30} title={chatHost.name} />
                      : null);
                  //:重试期间步骤流整块藏起来（出现红色横幅时步骤流直接消失，
                  //:恢复后再出来，而不是一直在思考中）。`live` 那一块的状态原样留着，
                  //:   红条一清它就自己回来。
                  return m.role === "activity"
                    ? (m.live && llmRetry ? null
                      : <ActivityBlock key={m.id} data={m} onCancel={stopTurn}
                                       open={openAct === m.id}
                                       onToggle={() => setOpenAct(openAct === m.id ? "" : m.id)} />)
                    : <Message key={m.id} data={m} star={star} logo={LOGO}
                               avatar={speakerAvatar} client={client}
                               group={chatGroupId}
                               onWithdraw={withdrawSend} />;
                });
              })()}
              {provisional ? (
                <article className="message assistant">
                  {/* 同上：群会话里草稿也挂主理人的头像 */}
                  {chatHost
                    ? <Avatar slot={chatHost.avatar} name={chatHost.name}
                              size={30} title={chatHost.name} />
                    : <img src={LOGO} />}
                  {/* 草稿也按 Markdown 渲染（与下面那条最终回复同一个渲染器）。
                      以前这里是 `{provisional}` 直出 —— 模型回一张表 / 一串
                      `[名字](本机路径)` 时，草稿显示的是原始 Markdown：
                      竖线、方括号、percent 编码的长路径，看起来像一屏代码，
                      而它比渲染后的正文高得多，滚屏；`task.end` 一到它被
                      换成渲染版，观感是「突然消失并漏出正常回复」。
                      判据：同一段文字，草稿与正文必须长得一样（草稿只是更早出现）。 */}
                  <div className="bubble draft"><Markdown text={provisional} /></div>
                </article>
              ) : null}
              {busy && !liveElsewhere && !liveActivity && !provisional ? <div className="thinking"><img src={LOGO} /><span className="dots">● ● ●</span>{t("正在处理")}</div> : null}
              <div ref={end} />
            </section>
          </>
        )}
          {/* 输入框那一格 —— 草稿态与发送后是同一份 JSX（同一个节点）。
              草稿态：框外居中一个「选择工作区」（`WorkdirPick`）。
              发送后：什么都不显示，只有「内容由AI生成，请核实重要信息」
                 —— 落点由左栏按目录分组承载，这一格不重复说一遍。 */}
          <div className="composer-dock">
            {/* `@` 候选表只在无主理人群聊里存在。
                这不是"顺手也给有主理人的群加上"—— 有主理人时用户只跟主理人说话，
                   `@成员` 后端根本不会路由（`parse_mentions` 只活在无主理人那一档），
                   给了候选就等于界面承诺一件后端做不到的事。 */}
            {/* `busy` 是全局的，但「正在整理…」那根条属于
                正在跑的那一屏 —— 切到别的对话后它不该跟过来。 */}
            {/* 「回到底部」—— 只在用户自己往上翻时出现（不再贴底）。
                用户往上翻时不要被强行拽回底部；这一颗是他自己回去的路。 */}
            {!atBottom ? (
              <button type="button" className="scroll-bottom" onClick={jumpToBottom}>
                {t("回到底部")}
              </button>
            ) : null}
            <Composer {...{ client, onSend: send, prefill, busy: busy && !liveElsewhere, mode, onMode: changeMode, effort, onEffort: changeEffort, connected, running: status?.framework_running, onStart: start, waiting: !!approval, retry: llmRetry, onCancel: stopTurn, onDismissError: () => setLlmRetry(null), compacting: compactState === "running", compactSince, compactFailed: compactState === "failed", compactNothing: compactState === "nothing", compactDone: compactState === "done", compactNotice: compactState === "notice", compactMessage, onDismissCompact: () => setCompactState(""), mentions: groupMode ? (chatGroup.members || []) : null, hideMode: !!chatGroup, takeover, onTakeover: toggleTakeover, apiReady: status ? status.model_api_ready : undefined, onGoEndpoint: () => nav("endpoint") }} />
            {empty ? (
              <>
                <WorkdirPick client={client} value={wsPath} onPick={setWsPath}
                             notify={notify} disabled={!connected} />
                {/* `#02` 组件异常 —— 只在新建对话页说一次。
                    判据是 `empty`（用户离开这一屏就不再提），不是"关掉过没有"。
                    账是空的时候不出现：没有异常就不该有横幅。 */}
                {incidents.length > 0 ? (
                  <div className="incidents-banner" role="status">
                    <div>{t("#02 组件异常——程序存在情况异常的组件，和agent沟通以尝试修复此问题")}</div>
                    {/* 具体是哪一件、为什么 —— 只有那一句固定文案时，用户知道
                        "有东西坏了"却无从下手（例如监听指向一个已经不存在的目录）。
                        内容由 `/api/incidents` 给，React 按文本渲染，不拼 HTML。 */}
                    <ul className="incidents-list">
                      {incidents.map((it, i) => (
                        <li key={i}>
                          <strong>{String(it.component || "")}</strong>
                          {it.error ? "：" + String(it.error) : ""}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </>
            ) : (
              <p className="ai-note">{t("内容由AI生成，请核实重要信息")}</p>
            )}
          </div>
        </div>
      ) : page === "skills" ? <Skills skills={skills} connected={connected} client={client} notify={notify} onRefresh={reloadSkills} />
        : page === "schedules" ? <Schedules client={client} connected={connected} onToast={notify} />
        : page === "endpoint" ? <Endpoint client={client} notify={notify} connected={connected} />
        : page === "groups" ? <Groups client={client} notify={notify} connected={connected}
                                     onEnter={openGroup}
                                     onGroupsChanged={(list) => setSideGroups(list || [])} />
        : <Stub title={(NAV.find((n) => n[0] === page) || [])[1] || "Crannia"} />}
    </main>
    {/* 右缘那条竖排栏 = `.shell` 的第三列。 */}
    <Rail open={drawer} onToggle={setDrawer}
          traceEnabled={!!(status && status.trace_enabled)} />
    {approval && (approval.kind === "ask_user"
      ? <AskUser data={approval} answer={answer} />
      : <Approval data={approval} answer={answer} />)}{btw && <BtwPanel data={btw} close={() => setBtw(null)} />}{settingsOpen && <Settings settings={settings} onReload={reloadSettings} close={() => setSettingsOpen(false)} connected={connected} client={client} notify={notify} onReplayOnboarding={replayOnboarding} />}{toasts.length ? (
        <div className="toast-stack" role="status" aria-live="polite">
          {toasts.map((t) => <div className="toast" key={t.id}>{t.text}</div>)}
        </div>
      ) : null}
    {/* 首启引导：与设置弹窗同一层（都挂在 `.shell` 里）。
        它不点背景关闭 —— 首次使用时必须走到完成；只有重跑模式才有关闭按钮。 */}
    {onboard.open && <Onboarding client={client} connected={connected} notify={notify}
                                 logo={LOGO} version={status && status.version}
                                 replay={onboard.replay}
                                 onDone={() => { setOnboard({ open: false, replay: false }); refresh(); reloadSettings(); }}
                                 onClose={onboard.replay ? () => setOnboard({ open: false, replay: false }) : undefined} />}
    {/* 「手动切到自动」那一次的安全提示。
        音频地址走 `client.asset(...)` —— 资源路径只有这一条合法出路
          （打包后裸的斜杠开头路径一律变成盘根目录，404）。 */}
    {/* 被屏蔽插件的兼容性警告（与安全提示同一个设计 + 同一个音效）。
        音频地址同样走 `client.asset(...)` —— 资源路径只有这一条合法出路。
        有多个插件不支持时，冲突栏显示多个名字和位置
          所以整批一起交进去（一条一条弹 = 用户得按 N 次，
          而它们本来就是同一件事）。只弹一批仍然成立：两个 `PluginNotice`
          同时挂上去 = 用户只看得见上面那个。 */}
    {blockedQueue.length > 0 && <PluginNotice entries={blockedQueue}
                                              audioUrl={client.asset("/static/audio/safety-mode.mp3")}
                                              onConfirm={dismissBlocked} />}
    {autoNotice && <SafetyNotice audioUrl={client.asset("/static/audio/safety-mode.mp3")}
                                 onConfirm={confirmAutoMode} onCancel={cancelAutoMode}
                                 onOpenDoc={setHelpDoc} />}
    {/* 切「完全权限」前每次都要过一次确认（没有「不再提醒」）。 */}
    {fullNotice && <SafetyNotice kind="full_access"
                                 audioUrl={client.asset("/static/audio/safety-mode.mp3")}
                                 onConfirm={confirmFullAccess}
                                 onCancel={() => setFullNotice(false)} />}
    {/* 接管警告：安全警告 / 3 秒倒计时 /
        「取消」= 不启用 / 「我知道了」只改"以后还弹不弹"。 */}
    {/* 左栏解散群聊的二次确认。 */}
    {confirmGroup && (
      <Confirm
        title={t("解散这个群聊？")}
        body={<>
          <b>{confirmGroup.name || t("这个群聊")}</b>
          <br />
          {t("群成员、主理人与群配置都会删掉；它说过的话仍留在历史库里。")}
        </>}
        confirmLabel={t("解散")}
        onConfirm={() => doRemoveGroupChat(confirmGroup)}
        onCancel={() => setConfirmGroup(null)}
      />
    )}
    {takeoverNotice && <TakeoverNotice audioUrl={client.asset("/static/audio/safety-mode.mp3")}
                                       onConfirm={confirmTakeover}
                                       onCancel={() => setTakeoverNotice(false)}
                                       onAcknowledge={acknowledgeTakeover}
                                       onOpenDoc={setHelpDoc} />}
    {/* 说明页盖在安全弹窗上（同一张卡片的"变大"形态）。
        · 「返回」（面板里那颗），只清 `helpDoc`，下面的弹窗还在
        · 点空白，直接退出：说明页 + 弹窗一起关 —— 空白的语义是"这一层不要了" */}
    {helpDoc && (
      <div className="backdrop"
           onClick={() => {
             setHelpDoc("");
             setAutoNotice(false);
             setFullNotice(false);
             setTakeoverNotice(false);
           }}>
        <section className="settings mem-shell" onClick={(e) => e.stopPropagation()}>
          <HelpPanel docId={helpDoc} onBack={() => setHelpDoc("")} />
        </section>
      </div>
    )}
  </div>
  <Drawer panel={drawer} onClose={() => setDrawer("")} client={client}
          jump={jump} onJumped={() => setJump(null)}
          onOpenStar={(storageId, messageId) => {
            // 点收藏条目 = 切到历史面板 + 落到那一行。
            setJump({ storage_id: storageId, message_id: messageId });
            setDrawer("history");
          }} />
  </>;
}

/** 库里那一行（`/api/messages` 的 `messages[]`）→ 聊天区那条消息。
 *
 *  `rowId` 就是那一行的行号 —— 聊天区的与收藏的跳转都靠它
 *  （实时消息的行号从 `task.end` 来，历史的行号本来就有）。
 *，装载进来的历史消息一样能收藏。
 *  `hist: true` 只是给渲染留个记号（它们是只读的过去，不是这一轮的活），
 *  今天没有别的地方读它 —— 别把它当成"历史专用渲染器"。
 */
function histRow(m) {
  const ts = Date.parse(m.ts || "");
  const speaker = m.speaker || null;
  let content = m.content || "";
  // 成员回复在库里带 `【名字】` 前缀（让模型读得出是谁说的）；
  //   界面上名字由 `speaker` 单独渲染，所以这里把前缀剥掉。
  //   只剥自己那一层：没有 speaker 的消息（用户 / 主持人）一个字节都不动。
  //: 模型会模仿前缀，旧数据里有 `【a】【a】`。
  //:   剥到没有为止（只剥自己那一层），不然老对话里会露出一个多余前缀。
  if (speaker) {
    const pfx = `【${speaker}】`;
    while (content.startsWith(pfx)) content = content.slice(pfx.length);
  }
  //: 用户消息：附件前言是给模型的，界面上只显示用户真正打的那句话。
  //  附件不丢 —— 解析出来挂在消息上（气泡下面那一排）。
  //  解析器只在 `attachments.js` 一处；这里不再写一遍正则。
  let attachments = null;
  if (m.role === "user") {
    const parsed = parseAttachments(content);
    content = parsed.text;
    attachments = parsed.attachments.length ? parsed.attachments : null;
  }
  return {
    id: "h" + m.id,
    role: m.role,
    attachments,
    content,
    kind: m.kind || null,
    speaker,
    avatar: (m.avatar != null ? m.avatar : null),
    // 产物随消息行一起落库（`extra_json`），刷新后照样画得出来。
    artifacts: (Array.isArray(m.artifacts) && m.artifacts.length) ? m.artifacts : null,
    rowId: m.id,
    ts: Number.isNaN(ts) ? undefined : ts / 1000,
    hist: true,
  };
}

/** `ts` 是 epoch 秒（后端 `core/events.py` 的 `time.time()`），×1000 才是 `Date`。 */
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function Message({ data, star, logo, avatar, client, group, onWithdraw }) {
  //: 那颗「复制」按钮失败要与成功分得开。
  //:   不能用 `navigator.clipboard?.writeText(...)` —— 带 `?.` 时坏了也静默
  //:   （界面在说谎）。这一格自己说结果，
  //:   而不是把"成没成"留给用户去别处粘一下试试。
  const [copyNote, setCopyNote] = useState("");
  // 动作条（复制 + 时间戳 + 改动文件）只挂在"明确的对用户回复"上，不在步骤流框里。
  //   重试不做。
  //   位置：气泡与动作条要包进一个竖排容器 —— 如果它们是 `article` 的直接子项，
  //      而 `article` 是 `display:flex`，`复制` 成了第 3 个 flex 子项，飘到了气泡右边。
  const [listOpen, setListOpen] = useState(false);
  //: 点开的那个文件（`{path,name,version}`）；`null` = 没开浮窗。
  const [diffFile, setDiffFile] = useState(null);
  const isReply = data.role === "assistant" && !data.error;
  const nFiles = (data.files && data.files.count) || 0;

  /** 点一个改动过的文件：有留档就看 diff，没有就定位到目录。
   *
   *  没有留档（`version === 0`）那些——老会话、超过留档封顶的、留档已过期——
   *  给不出 diff，硬开一个空浮窗等于让它看起来"什么都没改"。
   */
  function openFileDiff(row) {
    // 二进制文件没有"按行看改动"这回事，没有留档的也一样 ——
    //   两种情况都直接定位，不弹一个只会说"拿不到 diff"的空浮窗。
    if (!row.version || row.binary) {
      client.call("/api/reveal", jsonRequest("POST", { path: row.path }))
        .catch(() => { /* 定位失败就什么都不弹：没有更好的一句可说 */ });
      return;
    }
    setDiffFile(row);
  }
  return <article className={`message ${data.role}`}>
    {/* 群聊时的 agent 头像用主理人的头像 ——
        群会话里传进来 `avatar`；普通会话照旧是框架 logo。 */}
    {data.role === "assistant" && (avatar || <img src={logo} />)}
    <div className="msg-col">
      {/* 群成员回复的说话人。
          无主理人群聊里用户直接面对成员，必须有名字；否则几条回复长得一样。 */}
      {data.speaker ? <div className="message-speaker">{data.speaker}</div> : null}
      {/* 正文要按 Markdown 渲染：直接 `{data.content}` 直出会把
          `加粗` / `## 标题` 显示成字面量。
          出错那条（`error`）不走渲染 —— 那是框架 / 上游的报错原文，
          把报错按 Markdown 渲染等于给一串技术文本加语义
          （给谁看的决定了它走哪个出口）。 */}
      {/* 只有附件、没有文字的用户消息，不画气泡：
          空气泡看起来像"发丢了"。附件条自己就是这条消息的内容。 */}
      {/* 排队中：这一条还没发出去（本地排队），当前任务结束后才发。
          提示在气泡上方，气泡本身加呼吸黄框；旁边给一个撤回。*/}
      {data.queued ? (
        <div className="queued-note">
          {onWithdraw ? (
            <button className="queued-undo" onClick={() => onWithdraw(data.id)}>
              {t("撤回")}
            </button>
          ) : null}
          {/* 两句提示分得开（用户）：走 steer 的那条是
              「当前步骤」（做完手上这一步就发），等锁的那条（命令 / 带附件 /
              有待审批 / 没在跑）仍是「当前任务」—— 两件事的等待时间差一个量级。 */}
          <span>{t(data.steer ? "排队中：当前步骤结束后发送"
                              : "排队中：当前任务结束后执行")}</span>
        </div>
      ) : null}
      {data.error || data.content || !(data.attachments && data.attachments.length) ? (
        <div className={data.error ? "error"
          : ("bubble" + (data.queued ? " queued" : ""))}>
          {data.error ? data.content : <Markdown text={data.content} />}
        </div>
      ) : null}

      {/* 用户消息带的附件（图 = 缩略图，文件 = 图标 + 名字）。
          排在气泡下面 —— 上面那条窄栏留给正文。 */}
      {data.role === "user" && data.attachments && data.attachments.length ? (
        <MessageAttachments items={data.attachments} client={client} />
      ) : null}

      {/* 这一条回复产出的东西（工具画的图 / 上游返回的图、音、视频）。
          放在气泡下面、改动文件上面 —— 它是回复的一部分，不是附件栏。 */}
      <Artifacts records={data.artifacts} client={client} />

      {nFiles ? (
        <div className="files-wrap">
          <button className="files-btn" onClick={() => setListOpen((v) => !v)}>
            {t("查看 {n} 个改动文件", { n: nFiles })}
            {listOpen ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          </button>
          {listOpen ? (
            <ul className="files-list">
              {(data.files.files || []).map((f, i) => {
                // `files` 是对象数组（`{path,name,link,version[,added,removed]}`），
                //   不是字符串 —— 按字符串渲染会直接
                //   `Objects are not valid as a React child`，整棵树卸载（点了按钮就崩）。
                // 这里先判形：万一后端改了形状，宁可少显示，也不许再崩一次。
                const row = typeof f === "string" ? { name: f, path: f } : (f || {});
                // 增减是知道才给的（后端不给 0 兜底，读不到 ≠ 0）
                //   拿不到就什么都不显示；写 `+0/−0` 会看起来像"这文件没变"。
                const hasDelta = row.added != null || row.removed != null;
                return (
                  <li key={i} title={row.path || ""}>
                    <button className="files-open" onClick={() => openFileDiff(row)}>
                      {row.name || row.path || t("（未知）")}
                    </button>
                    {hasDelta ? (
                      <span className="files-delta">
                        {row.added != null ? <b className="add">+{row.added}</b> : null}
                        {row.removed != null ? <b className="del">−{row.removed}</b> : null}
                      </span>
                    ) : null}
                  </li>
                );
              })}
              {/* `files` 最多带 30 条而 `count` 是真数
                  （`scheduler.py:_MAX_FILES_IN_EVENT`：`count` 报的永远是真数）
                  所以列不全时要说出来，别让它看起来就是全部。 */}
              {data.files.count > (data.files.files || []).length ? (
                <li className="files-more">
                  {t("…还有 {n} 个没列出",
                     { n: data.files.count - (data.files.files || []).length })}
                </li>
              ) : null}
            </ul>
          ) : null}
        </div>
      ) : null}

      <div className="msg-foot">
        <span className="msg-time">{fmtTime(data.ts)}</span>
        {/* 收藏这一轮 —— 只在这条消息有行号时才出现。
            没有行号 = 还没落库（或后端没给），不画按钮。
            画一个点了没反应的按钮，比不画更坏（"把用户晾在那儿"）。 */}
        {star ? (
          <button className={"msg-act msg-star" + (star.on ? " on" : "")}
                  title={star.on ? t("取消收藏") : t("收藏这一轮")}
                  onClick={() => star.toggle(star.id)}>
            <Star size={13} fill={star.on ? "currentColor" : "none"} />
          </button>
        ) : null}
        {isReply && data.content ? (
          // 走 `copyText`（三条路按序试）+ 如实回话。
          //   失败那条要给出另一条路 —— 右键菜单是壳侧原生的
          //   （`main.cjs` 的 `role:"copy"`），它不经过渲染进程，一定可用。
          <button className="msg-act"
                  title={copyNote === "复制失败" ? t("换个方式：在消息上点右键 -> 复制") : t("复制")}
                  onClick={async () => {
                    const ok = await copyText(data.content);
                    setCopyNote(ok ? t("已复制") : t("复制失败"));
                    setTimeout(() => setCopyNote(""), 1600);
                  }}>
            <Copy size={13} />{copyNote || t("复制")}
          </button>
        ) : null}
      </div>
    </div>
    {diffFile ? (
      <FileDiff path={diffFile.path} name={diffFile.name} version={diffFile.version}
                group={group} client={client} onClose={() => setDiffFile(null)} />
    ) : null}
  </article>;
}

function Stub({ title }) { return <section className="stub"><Clock3 size={28} /><h1>{title}</h1><p>{t("此页面已预留，后端能力尚未实现。")}</p></section>; }

// 左栏「任务」那一栏是按目录分组的树。
//
// 结构：一条时间线 ——
//   · 没选目录的对话各自是一个条目（散装、平铺）
//   · 选了目录的合成一个条目（文件夹标，点开展开组内对话）
//   两者混在同一条线上，按「各自最新一条」倒序 —— 目录组因此落在它该在的位置。
//   此前是"先把散装全铺完、文件夹排在后面"，一个目录组
//      只可能出现最前或最后；它应当落在时间线上该在的位置。
// 分组依赖 `/api/threads` 的 `workspace` 字段（名册里带着"这段对话用哪个目录"）。
//
// 两个形状：展开 = 这一组的全部行；收起 = 只留组头。
//   节点带个数与"含当前那条"，含当前的默认展开，其余默认收起。
//   此前是"收起后仍露出 ≤5 行" —— 组里本来只有一两行时，
//      收起前后长得一模一样（点了收起而行还在，会被判定为"点了没反应"）。
//
// 它就住在这个文件里（不拆成 `ThreadTree.jsx`）——
//   源码级测试按源文本钉着「渲染出的每一行是 `.thread-row`」
//   必须出现在 `App.jsx` 里。那条测试防的是"名册渲染被换成别的东西而没人发现"，
//   不该为了拆文件去改它。
/** 路径太长时只显示叶子那一节（完整路径进 `title`，悬停看得到）。 */
function tailName(path) {
  const s = String(path || "").replace(/[\\/]+$/, "");
  const parts = s.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : s;
}

function ThreadTree({ threads, viewing, openThread, openCtx, unread, queued, batch,
                      pinned, openGroupCtx }) {
  //: 展开状态：`dir -> bool`。没记过的组用默认（含当前那条，展开）。
  const [open, setOpen] = useState({});
  //: 置顶的对话（先置顶的在前）—— 它们浮到最上面，不在各自的分组里重复出现。
  const pinnedThreads = (pinned && pinned.threads) || [];
  const pinnedG = (pinned && pinned.groups) || [];
  const entries = useMemo(() => {
    const byDir = new Map();
    for (const t of visibleThreads(threads)) {
      const dir = (t.workspace && t.workspace.dir) || "";
      if (!byDir.has(dir)) byDir.set(dir, []);
      byDir.get(dir).push(t);
    }
    const out = [];
    for (const [dir, rawItems] of byDir) {
      //: 组内排序：当前那一条浮到最上面，其余按最后一条消息的时间倒序。
      //:   用户「旧对话激活为当前活跃时应该挪到最上面去」。
      //:   判据用 `is_current`（服务端给的 `ended_at IS NULL`），不是"最近发过话"——
      //:   刚 `/resume` 过去、还一个字没说的那一段也算"当前"，也该在上面。
      //:   这不影响 `/resume <n>` 的编号：那一份走的是后端的 `list_threads`
      //:   （按插入序），而且它自己会打出编号表，两处各自自洽。
      const items = rawItems.slice().sort((a, b) =>
        (Number(!!b.is_current) - Number(!!a.is_current))
        || ((Number(b.last_at) || 0) - (Number(a.last_at) || 0)));
      out.push({
        dir, items,
        //: 整组都没选过目录，这组就是「默认工作区」。
        //:   判据是 `is_set`，不是"路径等于默认目录" —— 明确选了
        //:   默认那个目录时两者路径相同而语义不同（界面要说得出差别）。
        isDefault: !items.some((x) => x.workspace && x.workspace.is_set),
        containsCurrent: items.some((x) => x.is_current),
        lastAt: items.reduce((m, x) => Math.max(m, Number(x.last_at) || 0), 0),
      });
    }
    // 置顶的对话要浮到最上面，从各自那一组里摘掉（不许同时出现在两处）。
    for (const g of out) {
      if (pinnedThreads.length) g.items = g.items.filter((t) => pinnedThreads.indexOf(t.storage_id) < 0);
    }
    // 混成一条时间线：默认工作区那几段各自是一个条目，
    //   选了目录的组是一个条目；两者按「各自最新一条」排序，目录组落在它该在
    //   的时间位置上，而不是被整体推到最前或最后。
    //   ① 置顶的"集"仍排最前（按置顶先后）
    //   ② 其余按 `at` 降序
    const entries = [];
    for (const g of out) {
      if (g.isDefault) {
        for (const th of g.items) {
          entries.push({ kind: "thread", th, at: Number(th.last_at) || 0 });
        }
      } else if (g.items.length) {
        entries.push({ kind: "group", g, at: g.lastAt });
      }
    }
    //: 含当前那一段的条目浮到最上面 —— 用户「旧对话激活为当前
    //: 活跃时应该挪到最上面去」。两种形状都算（散装那行 / 那个目录组）。
    const isCur = (e) => (e.kind === "thread" ? !!e.th.is_current : !!e.g.containsCurrent);
    entries.sort((a, b) => {
      const ia = a.kind === "group" ? pinnedG.indexOf(a.g.dir) : -1;
      const ib = b.kind === "group" ? pinnedG.indexOf(b.g.dir) : -1;
      if (ia >= 0 || ib >= 0) {
        if (ia < 0) return 1;
        if (ib < 0) return -1;
        return ia - ib;
      }
      return (Number(isCur(b)) - Number(isCur(a))) || (b.at - a.at);
    });
    return entries;
  }, [threads, pinned]);

  const rowOf = (th) => (
    <button key={th.storage_id}
            className={"thread-row"
                       + (viewing && viewing.storage_id === th.storage_id ? " on" : "")
                       + (batch && batch.sel[th.storage_id] ? " picked" : "")}
            // 批量态下"勾没勾上"用整行高亮 + 左侧色条表示（不塞勾选框）——
            //   `.thread-row` 是纵向 flex（标题 / 计数各占一行），
            //   插一个图标进去会被挤成第三行。
            title={(th.title || t("（没有用户消息）")) + "　" + t("右键可删除")}
            onClick={() => openThread(th)}
            onContextMenu={(e) => openCtx(e, th)}>
      {/* 置顶标放在标题那一行里面（`<b>` 里）—— 同上的理由：
          放到 `<b>` 外面会变成又一行。 */}
      <b>{pinnedThreads.indexOf(th.storage_id) >= 0 ? <Pin size={11} className="pin-mark" /> : null}
        {th.title || t("（没有用户消息）")}</b>
      {/* 两格会话级状态，都在这一行上。
          「排队中」= 黄。
          红点 = "这一段跑完了、而你那一刻不是在它上面"。 */}
      {queued && queued[th.storage_id]
        ? <i className="row-queued" title={t("排在前序任务后面，还没开始")}>{t("排队中")}</i>
        : null}
      {unread && unread[th.storage_id]
        ? <i className="row-unread" title={t("有新回复")} />
        : null}
      <small>{t("{n} 轮", { n: th.turn_count })}{th.is_current ? " " + t("· 当前") : ""}</small>
    </button>
  );

  //: 置顶的那几段真的存在于名册里才知道怎么画（置顶表里可能留着
  //:   一段已经被删掉的 id —— 那一条就是空壳，直接不画）。
  //:   （同族：`stars` 那条"源没了，条目消失"是结构保证的，这里靠这一次过滤。）
  const pinnedItems = visibleThreads(
    pinnedThreads.map((sid) => threads.find((t) => t.storage_id === sid)));

  return (
    <div className="thread-tree">
      {/* 置顶区：最上面、平铺 ——
          它们已经从各自那一组里摘走了（见上面的 `filter`），不会出现两次。 */}
      {pinnedItems.length ? (
        <div className="thread-group pinned-block">
          <div className="pin-head"><Pin size={12} />{t("置顶")}</div>
          {pinnedItems.map(rowOf)}
        </div>
      ) : null}
      {entries.map((e) => {
        // 散装对话：没选过目录的那几段各自占一行，直接铺在时间线上
        //   （不画"默认工作区"组头 —— 那会凭空多一层）。
        if (e.kind === "thread") return rowOf(e.th);
        const g = e.g;
        // 一组里那几段全被置顶摘走了，别再画一个空组头（空文件夹看起来像 bug）。
        if (!g.items.length) return null;

        const viewingHere = !!(viewing && g.items.some((x) => x.storage_id === viewing.storage_id));
        const isOpen = open[g.dir] !== undefined ? open[g.dir] : (g.containsCurrent || viewingHere);
        //: 收起 = 一行都不显示（只留组头）。见文件头那条订正。
        const shown = isOpen ? g.items : [];
        return (
          <div className="thread-group" key={g.dir}>
            <button className={"group-row" + (pinnedG.indexOf(g.dir) >= 0 ? " pinned" : "")}
                    onClick={() => setOpen((o) => ({ ...o, [g.dir]: !isOpen }))}
                    onContextMenu={(e) => openGroupCtx(e, g.dir)}
                    title={g.dir + "　" + t("右键可置顶这一集")}>
              {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
              {isOpen ? <FolderOpen size={14} /> : <Folder size={14} />}
              <b>{tailName(g.dir)}</b>
              <small>{g.items.length}</small>
            </button>
            {shown.map(rowOf)}
          </div>
        );
      })}
    </div>
  );
}
/** 「设置」那一屏 —— 四栏 + 全部格。
 *
 *  形状：工作设置 / 对话设置 / 安全设置 / 其他设置，
 *  `-` 开头的是上级开关打开后才显示。
 *
 *  两条纪律（比"画全"更要紧）：
 *   ① 每一格都接在一条真键上 —— 表在 `settings-view.js`（纯函数，node 能断言），
 *      测试拿它去反查后端（GET 得给得出、POST 得收）。
 *      画一个按了没反应的开关就是标签在说谎。
 *   ② 没做的那几格照样画、但禁用并写清缺什么 —— 藏起来只会让人以为没这功能
 *      （入口和事实都不许凭空消失）。
 *
 *  兜底值住在后端：这一屏一个默认值都不抄（`GET /api/settings` 会给生效值）。
 */
function Settings({ settings, onReload, close, connected, client, notify,
                    onReplayOnboarding }) {
  //: `client` 必须由父组件传进来。
  //:   这一格会调 `client.call("/api/allowlist")`；如果 props 里没有 `client`
  //:   所以`ReferenceError: client is not defined`
  //:   所以一开设置弹窗就崩（那个 `useEffect` 空依赖、挂载即跑）。
  //:   注意：`npm run build` 对未声明的自由变量不报错 —— 它假定那是全局，
  //:   而项目里没有装 eslint，所以 `no-undef` 那一层根本不存在。
  //:   所以静态检查专门钉这一族。
  const [col, setCol] = useState("work");
  //: 尚未落盘的选中值。只给"选了也存不下去"的格子用（`draftValue`）：
  //:   代理选了「指定」而没填地址时，后端会 400；先落在草稿里，
  //:   下面那行输入框才露得出来，填完点保存时两个键一起提交。
  const [draft, setDraft] = useState({});
  //: 记忆库是设置弹窗里的子页（用户点按钮后浮窗画面切成独立页）。
  //:   `true` 时整块内容换成 `MemoryLibrary`，左上角返回。
  const [memView, setMemView] = useState(false);
  //: 设置包导出 / 导入—— `""` / `"export"` / `"import"`。
  //:   它是独立一层（不是子页）：要盖在设置弹窗上面，而不是替换它。
  const [xfer, setXfer] = useState("");
  //: 运行日志子页（形状同记忆库：整块内容换页、左上角返回、顶栏导出）。
  const [logView, setLogView] = useState(false);
  //: 帮助文档子页（形状同记忆库/日志：整块内容换页、左上角返回）。
  //:   `""` = 不在说明页；否则是 `HELP_DOCS` 里的 id。
  const [helpView, setHelpView] = useState("");
  //: 自动审查配置子页（形状同记忆库：整块内容换页、左上角返回、顶栏三段切换）。
  const [rulesView, setRulesView] = useState(false);
  //: 名单类子页 —— 值是那一行的 `id`（`access` / `forbidden` / `watchers` …）。
  //:   空串 = 不在子页。条目本身由这一层拉好传进去。
  const [listView, setListView] = useState("");
  const [busy, setBusy] = useState(false);
  //: 关 ICE 总闸那一下要先警告（警告框 + 5 秒倒计时）。
  //:   `null` = 没在警告；`{row, n}` = 正在倒数。
  const [warn, setWarn] = useState(null);
  //: 访问白名单的内容（`GET /api/allowlist`）。
  //:   `/api/settings` 只给两个布尔（"名单里有没有 `*`"），不给名单本身
  //:   所以这一格必须另拉。真相只有一份（`data/allowlist.json`），界面上
  //:   怎么摆都只是它的一个视图。
  const [wl, setWl] = useState(null);
  //: `kind:"text"` 那一类行的草稿（目前只有代理地址一格）。
  //:   不直接用 `settings[row.key]` —— 那一格后端给的是打码值，
  //:   把它当 value 渲染出来，用户一保存就把真凭据换成 `***@host`。
  const [textInput, setTextInput] = useState({});
  //: 监听器列表。真值在 `data/settings.json` 的 `watchers[]`，但那一格
  //:   模型读不到（规避清单），用户也没有别的入口 —— 所以这一屏就是他的全部。
  //:   `running` 来自另一个端点（`/api/watchers/status`），按 id 并进来。
  const [watchList, setWatchList] = useState([]);
  //: 不可逆那两颗按钮的 5 秒倒计时。
  //:   `{row, n}` 正在倒数；到 0 就执行 `row.act`。
  //:   与 `warn`（ICE 那档）分开：ICE 那张文案是一套专门的反诈骗文，
  //:   不许被这一档冲淡（两者的受众与后果完全不同）。
  const [ask, setAsk] = useState(null);
  //: 设置页那批上拉框共用一个开合状态（同 `Composer` 那条：
  //:   一个状态天然互斥，不必在两处各写一次互相关闭的逻辑）。
  const [openPicker, setOpenPicker] = useState("");
  //: 配色的六个轴。
  //:   它不走 `settings`（那份是后端载荷）：配色是纯前端的观感，
  //:   改一下当场生效、存 GUI 自己的 localStorage 就算数，不必等 `/api/settings`。
  //: 主题模式：浅色 / 深色 / 跟随系统（`lv.theme`，默认跟随系统）。
  //:   六色编辑器改的是解析后那一档的那一份（浅色与深色各存各的）。
  const [themeMode, setThemeMode] = useState(() =>
    (typeof localStorage === "undefined" ? "system" : loadMode(localStorage)));
  const [resolvedTheme, setResolvedTheme] = useState(() => {
    if (typeof localStorage === "undefined") return "light";
    const mql = typeof matchMedia === "function"
      ? matchMedia("(prefers-color-scheme: dark)") : null;
    return resolveMode(loadMode(localStorage), mql);
  });
  const [theme, setTheme] = useState(() => {
    const mode = (typeof localStorage === "undefined") ? "light" : (() => {
      const mql = typeof matchMedia === "function"
        ? matchMedia("(prefers-color-scheme: dark)") : null;
      return resolveMode(loadMode(localStorage), mql);
    })();
    return {
      ...defaultsFor(mode),
      ...(typeof localStorage === "undefined" ? {} : loadTheme(localStorage, mode)),
    };
  });

  useEffect(() => {
    let alive = true;
    client.call("/api/allowlist")
      .then((d) => { if (alive) setWl(d); })
      .catch(() => { /* 拉不到就空着 —— 不弹错（同侧栏那几条） */ });
    // 监听器同一趟拉（两个端点：列表给路径、status 给 running）。
    //   失败同样不弹错：这一格空着比一条假数据好。
    Promise.all([
      client.call("/api/watchers").catch(() => null),
      client.call("/api/watchers/status").catch(() => null),
    ]).then(([l, s]) => {
      if (!alive) return;
      const run = {};
      ((s && s.status) || []).forEach((x) => { run[x.id] = !!x.running; });
      setWatchList(((l && l.watchers) || []).map((w) => ({ ...w, running: !!run[w.id] })));
    });
    return () => { alive = false; };
  }, []);

  const notice = leafNotice(settings && settings.leaf_md);
  //: 数据格式比本程序新—— 常驻一行，与 ICE「保护已降级」同判据。
  const dsNotice = dataSchemaNotice(settings && settings.data_schema);
  //: 草稿也要参与"哪些行该露出来"的判断（`needsEq`）——
  //:   否则选了「指定」之后那一行输入框永远不出现（选了也存不下去）。
  const rows = rowsFor(col, draft && Object.keys(draft).length
    ? { ...settings, ...draft } : settings);
  //: 行说明：`note` 是模板，值放 `noteVars`（例：`对话条目超过 {n} 时…`）。
  //:   为什么要模板而不在表里插值：i18n 的键是中文原文，插值后
  //:   `t()` 拿到的是 "…超过 50 时…"，表里没这个键，永远回落中文。
  const noteOf = (row) => t(row.note, row.noteVars || undefined);

  /** 「当前生效」那一行怎么念（代理那两行共用）。
   *
   *  来源要说人话：`wininet` = Windows 系统代理；环境变量直接报变量名
   *  （用户能照着去系统里找）。`env_dead` = 机器上有那么一条环境变量代理，
   *  但它连不上，这一发就按直连走 —— 不说的话界面只是"生效：直连"，
   *  而用户明明在别处配了代理。
   */
  const proxyStateLine = (st) => {
    if (!st) return "";
    const src = st.source === "setting" ? t("设置")
      : st.source === "wininet" ? t("Windows 系统代理")
      : (st.source || t("无"));
    let line = t("当前生效：{v} · 来源：{src}", { v: st.effective, src });
    if (st.hint === "env_dead") {
      line += " " + t("环境代理异常，已降级直连");
    }
    return line;
  };

  async function save(body, okMsg) {
    setBusy(true);
    try {
      const r = await client.call("/api/settings", jsonRequest("POST", body));
      notify((r && r.message) || okMsg || t("已保存。"));
      await onReload();
      // 界面语言改了就整页重载：模块级的词表（`NAV` / `POOLS` /
      //    设置表）在 import 时就定下来了，不重载的话「设置页写着英文、
      //    侧栏还是中文」—— 那是「标签在说谎」。
      if (body && "interface_language" in body
          && body.interface_language !== lang()) {
        window.location.reload();
      }
      return true;
    } catch (e) { notify(e.message); return false; }
    finally { setBusy(false); }
  }

  async function toggle(row, on) {
    // 关 ICE 总闸，不直接写，先把那张警告摆出来（5 秒倒计时）。
    if (row.confirm === "ice" && !on) { setWarn({ row, n: 5 }); return; }
    //: 权限解禁：打开那一下先警告 + 3 秒倒计时；
    //:   关掉不警告（往安全那边走，不需要拦）。
    //:   文案取自表里那一行（`warnTitle` / `warnBody`），与 ICE 那张分开。
    if (row.confirm === "full" && on) {
      setWarn({ row, n: Number(row.confirmSeconds) || 3, turnOn: true });
      return;
    }
    if (row.allowlist) {
      setBusy(true);
      try {
        await client.call("/api/allowlist", jsonRequest("POST", allowlistBody(row, on)));
        notify(on ? t("已开启全部放行。") : t("已关闭全部放行。"));
        await onReload();
      } catch (e) { notify(e.message); }
      finally { setBusy(false); }
      return;
    }
    await save(saveBody(row, on), on ? t("已开启。") : t("已关闭。"));
  }

  async function run(row) {
    // 纯本机动作 —— 安全提示的「不再提醒」存的是
    //   GUI 自己的 `localStorage`（与主题同一套理由：`crannia-app://` 与 WebUI 的
    //   `127.0.0.1:5000` 是两个 origin，localStorage 天然不通），服务端碰不到它。
    //   这一格因此不打任何端点，也不需要连接（见渲染那一处的 `row.local`）。
    //   它只清提示偏好，不动权限 / 白名单（标签不许说谎）。
    if (row.act === "reset_notices") {
      clearAutoNotice();
      clearTakeoverNotice();
      notify(t("已恢复：下次会重新弹出自动模式与接管的提示。"));
      return;
    }
    //: 记忆库子页 —— 纯客户端动作（数据自己拉，见 `MemoryLibrary`）。
    if (row.act === "open_memory") { setMemView(true); return; }
    //: 设置包导出 / 导入—— 独立浮窗；导入是两段式（先看差异再确认）。
    if (row.act === "export_settings") { setXfer("export"); return; }
    if (row.act === "import_settings") { setXfer("import"); return; }
    //: 自动审查配置子页 —— 纯客户端动作（数据来自 `settings`，改完 onReload）。
    if (row.act === "open_auto_rules") { setRulesView(true); return; }
    //: 重跑新手引导 —— 纯客户端动作（不打端点、不需要连接）。
    //:   先关设置页，否则两层遮罩叠在一起，用户只看得见上面那个。
    if (row.act === "onboarding_replay") {
      close();
      if (typeof onReplayOnboarding === "function") onReplayOnboarding();
      return;
    }
    // 不可逆那两颗先问一次（5 秒倒计时）。
    //   判据是端点自己写的那句（`/api/memory/clear` 的 docstring：
    //   「界面那边必须先问一次（5 秒倒计时），这里不代它做决定」）。
    //   所以界面这一侧不许把"确认"这件事省掉。倒计时到 0 才真的执行（见下面那个 effect）。
    if (row.danger) { setAsk({ row, n: 5 }); return; }
    setBusy(true);
    try {
      if (row.act === "prune_history") {
        const r = await client.call("/api/file-history/prune", jsonRequest("POST", {}));
        notify(t("清掉了 {n} 份留档。", { n: (r && r.removed) || 0 }));
      } else if (row.act === "check_update") {
        // 修：端点回的是 `update_available` / `version` / `channel`，
        //    而这里原来读 `r.message` + `r.available` —— 两个字段都不存在
        //    所以手动检查永远说「已是最新」（有新版本也一样）。
        //    判据：只认端点真的给的那几个字段名（照抄它的 jsonify）。
        const r = await client.call("/api/update/check", jsonRequest("POST", {}));
        if (r && r.update_available) {
          notify(t("有新版本 {v}（{ch} 渠道）。", {
            v: r.version || "?",
            ch: r.channel || "stable",
          }));
        } else if (r && r.error) {
          notify(t("检查失败：{why}", { why: r.error }));
        } else {
          notify(t("已是最新。"));
        }
      } else if (row.act === "open_logs") {
        //: 原来拉 `/api/logs` 却读 `r.path` —— 那个端点回的是 `{logs: [...]}`
        //:   （环形缓冲），没有 `path`，永远弹一句"日志路径没读到"。
        //:   现在正式接成子页：看 + 刷新 + 导出。
        setLogView(true);
      } else if (row.act === "open_data_dir") {
        //: 打开数据目录。路径由服务端给
        //:   （`GET /api/settings` 的 `data_dir`）—— 安装版里它是
        //:   `<安装目录>\resources\app\data`，客户端猜不出来。
        //:   走 `/api/reveal`：那条端点只打开文件夹，不执行任何东西。
        const dir = settings && settings.data_dir;
        if (!dir) { notify(t("还没读到数据目录，稍后再试。")); return; }
        const r = await client.call("/api/reveal", jsonRequest("POST", { path: dir }));
        notify((r && r.ok)
          ? t("已打开：{path}", { path: r.path })
          : t("打不开：{why}", { why: (r && r.error) || t("原因未知") }));
      } else if (row.act === "restart") {
        await client.call("/api/restart", jsonRequest("POST", {}));
        notify(t("框架正在重启…"));
      } else if (row.act === "open_issues" || row.act === "open_project") {
        // 反馈与项目主页都只开一条外链 —— 框架这一侧不碰用户的邮箱凭据。
        //    `openExternal` → `window.open` → 壳的 `setWindowOpenHandler`
        //    → `shell.openExternal`（`apps/desktop/main.cjs:1028`），系统浏览器。
        //    框架这一侧零外发：不再有 `POST /api/feedback`、不再碰用户的邮箱凭据。
        const url = row.act === "open_issues" ? ISSUES_URL : PROJECT_URL;
        if (!openExternal(url)) notify(t("打不开链接：{url}", { url }));
      }
    } catch (e) { notify(e.message); }
    finally { setBusy(false); }
  }

  /** 倒计时到 0 之后真的做那件事（与 `run` 分两处，
   *  因为 `run` 是"按一下就发生"、这里是"按了还要等 5 秒"）。
   *
   *  只有这两条路，而且各带自己的收尾：
   *  · `clear_memory` —— 后端那句话要照实报（删了几条 / 失败为什么），
   *    不许说成"已清空"（返回里 `success` 与计数都在）。
   *  · `restore_defaults` —— 落盘之后要重新拉一次设置（`onReload`），
   *    否则界面上还显示着刚被覆盖掉的值（界面在说谎）。
   */
  async function doDanger(row) {
    setBusy(true);
    try {
      if (row.act === "clear_memory") {
        const r = await client.call("/api/memory/clear", jsonRequest("POST", {}));
        // 回执字段照 `core/memory_store.delete_all` 的返回值（不是猜的）：
        //   `{success, deleted:[], failed:[], count}`。
        //   `deleted` 是一个数组不是数字 —— 按数字写会打印成「删了 [] 条」。
        //   `failed` 非空要如实说：那一支停在「文件还在、名字已封」
        //   （安全的那一侧，但不是清空了）。
        if (r && r.ok) {
          const n = (r.count != null) ? r.count : ((r.deleted || []).length);
          const bad = (r.failed || []).length;
          notify(bad
            ? t("清掉了 {n} 条，另有 {bad} 条没删成（名字已封，不会再自己回来）。", { "n": n, "bad": bad })
            : t("记忆库已清空（{n} 条）。", { "n": n }));
        } else notify(t("失败：{why}", { why: (r && r.error) || t("原因未知") }));
      } else if (row.act === "restore_defaults") {
        const r = await client.call("/api/settings/restore-default", jsonRequest("POST", {}));
        notify((r && r.message) || t("已恢复默认。"));
        await onReload();
      }
    } catch (e) { notify(e.message); }
    finally { setBusy(false); }
  }

  //: 「点面板外面就关上」—— 同 `Composer` 那条判据，但多认一个
  //:  `.picker-menu`：`fixed` 那一档的菜单被 portal 到 `body` 上了，
  //:  不再是 `.popover-host` 的后代，只认后者的话，按下去的那一刻就把它关了，
  //:  `click` 永远落不到菜单里的按钮上（症状：点了没反应 —— 而没有任何东西会红）。
  useEffect(() => {
    if (!openPicker) return undefined;
    const closeIt = (e) => {
      if (!e.target.closest || !e.target.closest(".popover-host, .picker-menu")) setOpenPicker("");
    };
    document.addEventListener("mousedown", closeIt);
    return () => document.removeEventListener("mousedown", closeIt);
  }, [openPicker]);

  //: 5 秒倒计时：每秒 -1，到 0 只把按钮点亮 —— 关不关由人按。
  //
  //  不许自动关（用户当场否掉）。这张弹窗问的是
  //  「有人在要求你吗？」—— 它的全部价值就是"有一个人在读、在决定"。
  //  倒计时到 0 自己把 ICE 关掉，等于把这道防线绕过去了：
  //  冒充客服的人只要让你把窗口晾 5 秒，保护就没了。
  //  所以判据：倒计时只负责让按钮可用（`disabled={warn.n > 0}`），
  //  真正的关闭必须在按钮的 `onClick` 里发生。
  //  中途按「保持开启」，`setWarn(null)`，cleanup 把定时器清掉。
  useEffect(() => {
    if (!warn || warn.n <= 0) return undefined;
    const timer = setTimeout(() => setWarn((w) => (w ? { ...w, n: w.n - 1 } : w)), 1000);
    return () => clearTimeout(timer);
  }, [warn]);   // eslint-disable-line

  /** 加 / 删一条白名单。端点与那两颗「全部放行」是同一个
   *  （`/api/allowlist`，载荷 `{kind, action, subject}`）。 */
  async function wlChange(row, action, subject) {
    setBusy(true);
    try {
      const r = await client.call("/api/allowlist",
        jsonRequest("POST", { kind: row.allowlist, action, subject }));
      setWl(r);                                  // 后端回的就是新状态，不自己拼
    } catch (e) { notify(e.message); }
    finally { setBusy(false); }
  }

  /** 那颗按钮的 5 秒倒计时 —— 到 0 只解禁，不替人按。
   *
   *  倒计时到 0 只解禁按钮，不替人按。
   *  这一条对两种弹窗都成立 —— ICE 那张（反诈骗）与这张（不可逆动作）：
   *  倒计时的作用是让人有时间读完，不是"数完就当你同意了"。
   *  自动执行等于把"确认"这一步删掉，而那两个端点都是不可逆的。
   *  判据：倒计时只负责让按钮可用（`disabled={ask.n > 0}`），
   *  `doDanger(row)` 只能在 `onClick` 里调用。
   *
   *  与 ICE 那一档（`warn`）各一个 effect 是有意的：那一边是 `toggle` 的
   *  收尾（把一个开关关掉），这一边是 `run` 的收尾（打一个端点），
   *  两者除了"都要等 5 秒"以外没有共同点，合成一个只会让两边互相牵制。
   *  用户按「取消」就是 `setAsk(null)`，cleanup 把定时器清掉。
   */
  useEffect(() => {
    if (!ask || ask.n <= 0) return undefined;
    const timer = setTimeout(() => setAsk((a) => (a ? { ...a, n: a.n - 1 } : a)), 1000);
    return () => clearTimeout(timer);
  }, [ask]);   // eslint-disable-line

  /** 名单类那一格（规避目录）—— 加一条 / 删一条 / 清空。
   *
   *  一个端点：`POST /api/settings`（`{forbidden_directories: [...]}`）。
   *  它不像白名单那样有专门端点 —— 那一格就是 `settings.json` 里的一个
   *  字符串数组，WebUI（`security.html`）本来就是这么写的。
   *  空表不是"没有规避目录"：`GET /api/settings` 会把默认那一批并回来
   *，「还原默认」＝ 存一个空表。
   */
  async function liChange(row, next, okMsg) {
    setBusy(true);
    try {
      const r = await client.call("/api/settings",
        jsonRequest("POST", { [row.key]: next }));
      notify((r && r.message) || okMsg || t("已保存。"));
      await onReload();
      return true;
    } catch (e) { notify(e.message); return false; }
    finally { setBusy(false); }
  }

  function listItems(row) {
    const v = valueOf(settings, row);
    return Array.isArray(v) ? v : [];
  }

  /** 名单类行现在有哪些条目 —— 摘要那一行与子页共用这一处。 */
  function itemsOf(row) {
    if (row.kind === "watchers") return watchList;
    if (row.kind === "allowlist") return ((wl || {})[row.allowlist]) || [];
    return listItems(row);
  }

  /** 摘要那一行写什么。空表什么都不写（那句「名单为空…」只在子页里说）。 */
  function summaryOf(row) {
    const items = itemsOf(row);
    if (!items.length) return "";
    if (row.kind === "watchers") {
      const bad = items.filter((w) => w.enabled && !w.running).length;
      return bad ? t("共 {n} 个（{bad} 个未运行）", { n: items.length, bad })
                 : t("共 {n} 个", { n: items.length });
    }
    return t("共 {n} 项", { n: items.length });
  }

  /** 重新拉一遍监听器（列表 + 运行状态）。改完之后一律以服务端为准。 */
  async function wtLoad() {
    try {
      const [l, s] = await Promise.all([
        client.call("/api/watchers"),
        client.call("/api/watchers/status").catch(() => null),
      ]);
      const run = {};
      ((s && s.status) || []).forEach((x) => { run[x.id] = !!x.running; });
      setWatchList(((l && l.watchers) || []).map((w) => ({ ...w, running: !!run[w.id] })));
    } catch (e) {
      // 拉不到就保持原样：清空会让人以为"删光了"。
      notify(e.message);
    }
  }

  /** 追加一个监听目录，并立刻尝试启动它。
   *
   *  `POST /api/watchers` 只写配置、不启动 —— 启动要另一趟 `/toggle`。
   *  两条都不省：只 POST 会留下一个"启用了但没在跑"的监听，而那种状态
   *  就是 `#02` 要报的东西。
   */
  async function wtAdd(path) {
    const p = String(path || "").trim();
    if (!p || busy || !connected) return;
    setBusy(true);
    try {
      const r = await client.call("/api/watchers", jsonRequest("POST", {
        watch_path: p, enabled: true, auto_start: true,
        sync_documents: true, sync_images: true, sync_media: true,
        notify_on_sync: true,
      }));
      const id = r && r.watcher && r.watcher.id;
      if (id) {
        const rr = await client.call(
          `/api/watchers/${encodeURIComponent(id)}/toggle`,
          jsonRequest("PUT", { enabled: true }));
        if (rr && rr.ok === false && rr.error) notify(rr.error);
      }
      await wtLoad();
    } catch (e) { notify(e.message); } finally { setBusy(false); }
  }

  async function wtToggle(id, on) {
    setBusy(true);
    try {
      const r = await client.call(`/api/watchers/${encodeURIComponent(id)}/toggle`,
        jsonRequest("PUT", { enabled: on }));
      if (r && r.ok === false && r.error) notify(r.error);
      await wtLoad();
    } catch (e) { notify(e.message); } finally { setBusy(false); }
  }

  async function wtRemove(id) {
    setBusy(true);
    try {
      await client.call(`/api/watchers/${encodeURIComponent(id)}`,
        jsonRequest("DELETE", {}));
      await wtLoad();
    } catch (e) { notify(e.message); } finally { setBusy(false); }
  }

  /** 改一个轴 —— 当场生效 + 当场落盘（GUI 自己的存储）。
   *
   *  不设 `busy`：它不是网络动作，设了会让整页按钮白闪一下。
   *  存的是完整六格（不是只存改过的那几格）—— 这样"恢复默认"只需清存储，
   *  而读回来永远是一份完整配色（某一格坏掉也只丢那一格，见 `theme.js::normalize`）。
   */
  function themePick(key, value) {
    const next = { ...theme, [key]: value };
    setTheme(next);
    if (typeof localStorage !== "undefined") saveTheme(next, localStorage, resolvedTheme);
    applyTheme(next, document, resolvedTheme);
  }

  /** 恢复出厂配色：清存储 + 摘掉注入的 `<style>`（不是再写一份默认值）。
   *  只清当前主题那一份 —— 深色调好的一套不该被浅色的"恢复默认"抹掉。 */
  function themeReset() {
    setTheme(defaultsFor(resolvedTheme));
    if (typeof localStorage !== "undefined") clearTheme(localStorage, resolvedTheme);
    applyTheme({}, document, resolvedTheme);
    notify(t("已恢复默认配色。"));
  }

  /** 换主题模式（浅色 / 深色 / 跟随系统）—— 当场生效，存 GUI 自己的 localStorage。 */
  function themeModePick(mode) {
    setThemeMode(mode);
    if (typeof localStorage === "undefined") return;
    saveMode(mode, localStorage);
    const mql = typeof matchMedia === "function"
      ? matchMedia("(prefers-color-scheme: dark)") : null;
    const resolved = resolveMode(mode, mql);
    setResolvedTheme(resolved);
    applyThemeMode(resolved);
    const colors = loadTheme(localStorage, resolved);
    setTheme({ ...defaultsFor(resolved), ...colors });
    applyTheme(colors, document, resolved);
    try { window.crannia?.setThemeMode?.(mode); } catch { /* 同上 */ }
  }

  function cell(row) {
    //: 草稿优先：`draft` 只装"选了还存不下去"的那几格（今天只有「指定」代理）
    //:   —— 它要先露出发地址的那一行，填完再和模式一起提交。
    const v = valueOf(draft && Object.keys(draft).length
      ? { ...settings, ...draft } : settings, row);
    if (row.kind === "allowlist" || row.kind === "list" || row.kind === "watchers") {
      return (
        <div className="set-row" key={row.id}>
          <div>
            <b>{t(row.label)}</b>
            {row.note ? <span>{noteOf(row)}</span> : null}
            {summaryOf(row) ? <span className="set-sum">{summaryOf(row)}</span> : null}
          </div>
          <button className="skill-btn" disabled={busy || !connected}
                  onClick={() => setListView(row.id)}>{t("设置")}</button>
        </div>
      );
    }
    if (row.kind === "theme") {
      //: 六个主题轴的编辑器。
      //:   色板是 `<input type="color">`（系统取色器）—— 不用自绘，省掉一整套
      //:   "取色器该怎么画"的问题，而它本来就是个熟人控件。
      //:   不依赖连接（配色纯前端），这里只按 `busy` 禁用，不按 `connected`。
      return (
        <div className="set-row col" key={row.id}>
          <div>
            <b>{t(row.label)}</b>
            {row.note ? <span>{noteOf(row)}</span> : null}
          </div>
          {/* 先选模式，再调这一档的六个轴 —— 浅色与深色各存一套。
              上拉框与「工作模式 / 模型强度」那一排同一个组件（`UpPicker`）。 */}
          <div className="theme-mode">
            <span>{t("主题")}</span>
            <UpPicker fixed name="theme-mode" label={t("主题")}
                      options={THEME_MODES.map((m) => ({ key: m.key, label: t(m.label) }))}
                      value={themeMode}
                      open={openPicker === "theme-mode"}
                      onToggle={(o) => setOpenPicker(o ? "theme-mode" : "")}
                      onPick={(mode) => themeModePick(mode)}
                      disabled={busy} />
          </div>
          <div className="theme-grid">
            {AXES.map((a) => (
              <label className="theme-cell" key={a.key}
                     title={t("CSS 变量") + " " + a.cssVar}>
                {t(a.label)}
                <input type="color" value={theme[a.key] || defaultsFor(resolvedTheme)[a.key]}
                       disabled={busy}
                       onChange={(e) => themePick(a.key, e.target.value)} />
              </label>
            ))}
            <button className="skill-btn" disabled={busy} onClick={themeReset}>
              {t("↺ 恢复默认")}
            </button>
          </div>
        </div>
      );
    }
    //: 恒定生效、没有开关的那一类 —— 只说明"已经启用"，
    //:   不画一个按了没反应的控件（「标签在说谎」）。
    if (row.kind === "info") {
      return (
        <div className="set-row" key={row.id}>
          <div><b>{t(row.label)}</b>{row.note ? <span>{noteOf(row)}</span> : null}</div>
          <span className="set-flag on">{t("已启用")}</span>
        </div>
      );
    }
    if (row.kind === "moved" || row.kind === "notdone") {
      return (
        <div className="set-row off" key={row.id}>
          <div><b>{t(row.label)}</b><span>{t(row.why)}</span></div>
          <span className={"set-flag " + (row.kind === "moved" ? "elsewhere" : "todo")}>
            {row.kind === "moved" ? t("在别处") : t("尚未实现")}
          </span>
        </div>
      );
    }
    if (row.kind === "act") {
      return (
        <div className="set-row" key={row.id}>
          <div><b>{t(row.label)}</b>{row.note ? <span>{noteOf(row)}</span> : null}</div>
          {/* `local: true` 的动作只碰 GUI 自己的 localStorage ——
              服务端连不上时它照样该能按（否则是一个说谎的禁用态）。 */}
          <button className="skill-btn" disabled={busy || (!connected && !row.local)}
                  onClick={() => run(row)}>
            {row.button ? t(row.button) : t("执行")}
          </button>
        </div>
      );
    }
    if (row.kind === "toggle") {
      //: `settings == null` = 还没读到，不是"关着"。
      //:   以前这种状态渲染成关 —— 用户看到「跨区搜索」是灰的，
      //:   而它其实默认是开的（后端三处兜底都是 true），那是标签在说谎。
      //:   读不到时给禁用 + 「读取中」，别给一个假的关闭态。
      const unknown = settings == null;
      return (
        <div className="set-row" key={row.id}>
          <div>
            <b>{t(row.label)}</b>
            {row.note ? <span>{noteOf(row)}</span> : null}
            {isExtra(row) ? <span className="set-extra">{t("本版多出来的")}</span> : null}
            {unknown ? <span className="set-extra">{t("读取中…")}</span> : null}
          </div>
          <button className={"switch" + (unknown ? "" : v ? " on" : "")}
                  disabled={busy || !connected || unknown}
                  role="switch" aria-checked={unknown ? "mixed" : (v ? "true" : "false")}
                  onClick={() => toggle(row, !v)}>
            <i />
          </button>
        </div>
      );
    }
    if (row.kind === "text") {
      //: “留空 = 不改”（与端配置的 api 那一格同一条）。
      //:   占位符里显示的是「当前实际生效的那个”（`row.effectiveFrom` 给的载荷）。
      const cur = textInput[row.id] == null ? "" : textInput[row.id];
      const st = row.effectiveFrom ? (settings && settings[row.effectiveFrom]) : null;
      const commit = async () => {
        const val = String(cur || "").trim();
        if (!val || busy || !connected) return;
        //: 带着草稿里那格模式一起交：`{proxy_mode: "manual", proxy_url: ...}`。
        //:   分开交的话后端会先拒「指定没有地址」，用户看到的是"选了没反应"。
        const body = { ...saveBody(row, val), ...draft };
        // 成功了才清空：`save()` 失败时（400 之类）输入框里那串还在 ——
        //   先清再存会让用户丢掉刚打的地址，而且要重打一遍才知道错在哪。
        if (await save(body)) {
          setTextInput((x) => ({ ...x, [row.id]: "" }));
          setDraft({});
        }
      };
      return (
        <div className="set-row col" key={row.id}>
          <div>
            <b>{t(row.label)}</b>
            {row.note ? <span>{noteOf(row)}</span> : null}
            {st ? <span className="set-effective">{proxyStateLine(st)}</span> : null}
          </div>
          <div className="wl-add">
            <input className="set-input" type="text" value={cur}
                   disabled={busy || !connected}
                   placeholder={(st && st.url) || (row.ph ? t(row.ph) : "")}
                   onChange={(e) => setTextInput((x) => ({ ...x, [row.id]: e.target.value }))}
                   onKeyDown={(e) => {
                     if (e.key === "Enter") { e.preventDefault(); commit(); }
                   }} />
            <button className="skill-btn" disabled={busy || !connected || !cur.trim()}
                    onClick={commit}>{t("保存")}</button>
          </div>
        </div>
      );
    }
    if (row.kind === "choice") {
      // 与投入档用同一个上拉框 —— 同样是那套窗口颜色和视觉。
      //   判据是同一个组件（`UpPicker.jsx`），不是"画一个像的"。
      //   `fixed` 是必须的：这一列是 `overflow:auto`，绝对定位的菜单会被裁掉。
      //   `emptyLabel` 那一档（"用默认"）＝ 空串这个值，它是一条真选项，
      //   不是"没选"（`valueOf` 返回 `null` 时才落到它上面）。
      const opts = optionsFor(settings, row).map((o) => ({ key: o.value, label: o.label }));
      const all = row.emptyLabel
        ? [{ key: "", label: t(row.emptyLabel) }].concat(opts) : opts;
      const st = row.effectiveFrom
        ? ((draft && Object.keys(draft).length ? { ...settings, ...draft } : settings) || {})[row.effectiveFrom]
        : null;
      return (
        <div className="set-row" key={row.id}>
          <div>
            <b>{t(row.label)}</b>
            {row.note ? <span>{noteOf(row)}</span> : null}
            {st ? <span className="set-effective">{proxyStateLine(st)}</span> : null}
          </div>
          <UpPicker fixed name={row.id} label={t(row.label)} options={all}
                    value={v == null ? "" : v}
                    open={openPicker === row.id}
                    onToggle={(o) => setOpenPicker(o ? row.id : "")}
                    onPick={(k) => {
                      //: `draftValue` = "选了它还得再填点什么才存得下去"的那一档
                      //:   （代理的「指定」：后端拒绝"只要模式、没有地址"）。
                      //:   先记草稿 -> 关联的输入行露出来 -> 填完一起提交。
                      if (row.draftValue && k === row.draftValue) {
                        setDraft((d) => ({ ...d, [row.key]: k }));
                        return;
                      }
                      setDraft((d) => {
                        const rest = { ...d };
                        delete rest[row.key];
                        return rest;
                      });
                      save(saveBody(row, k));
                    }}
                    disabled={busy || !connected} />
        </div>
      );
    }
    if (row.kind === "help") {
      //: 对外文档的条目清单 —— 与两处弹窗里的链接同一张表（`HELP_DOCS`）。
      //:   它没有 `key`：不是设置项、不落盘、不走 `/api/settings`。
      return (
        <div className="set-row col" key={row.id}>
          <div><b>{t(row.label)}</b>{row.note ? <span>{noteOf(row)}</span> : null}</div>
          <div className="help-list">
            {HELP_DOCS.map((d) => (
              <button key={d.id} className="skill-btn" onClick={() => setHelpView(d.id)}>
                {t(d.title)}
              </button>
            ))}
          </div>
        </div>
      );
    }
    if (row.kind === "number") {
      return (
        <div className="set-row" key={row.id}>
          <div>
            <b>{t(row.label)}</b>
            {row.note ? <span>{noteOf(row)}</span> : null}
            {isExtra(row) ? <span className="set-extra">{t("本版多出来的")}</span> : null}
          </div>
          <span className="set-num">
            <input className="set-input num" type="number" min={row.min} max={row.max}
                   disabled={busy || !connected} defaultValue={v == null ? "" : v} key={String(v)}
                   onBlur={(e) => {
                     const n = Number(e.target.value);
                     if (!Number.isFinite(n) || n === v) return;
                     save(saveBody(row, n));
                   }} />
            <em>{row.unit ? t(row.unit) : ""}</em>
          </span>
        </div>
      );
    }
    return null;
  }

  //: 名单类子页当前编辑哪一行；`null` = 不在子页。
  const editRow = listView ? ROWS.find((r) => r.id === listView) : null;
  const editItems = editRow ? itemsOf(editRow) : [];

  return (
    <div className="backdrop" onMouseDown={(e) => e.target === e.currentTarget && close()}>
      {editRow ? (
        <section className="settings mem-shell">
          <ListEditor row={editRow} items={editItems} busy={busy} connected={connected}
                      onBack={() => setListView("")}
                      onAdd={(v) => (editRow.kind === "watchers" ? wtAdd(v)
                        : editRow.kind === "allowlist" ? wlChange(editRow, "add", v)
                        : liChange(editRow, editItems.concat([v])))}
                      onRemove={(item, i) => (editRow.kind === "watchers" ? wtRemove(item.id)
                        : editRow.kind === "allowlist" ? wlChange(editRow, "remove", item)
                        : liChange(editRow, editItems.filter((_x, j) => j !== i)))}
                      onRemoveMany={(picked) => (editRow.kind === "watchers"
                        ? picked.reduce((p, w) => p.then(() => wtRemove(w.id)), Promise.resolve())
                        : editRow.kind === "allowlist"
                          ? picked.reduce((p, s) => p.then(() => wlChange(editRow, "remove", s)),
                                          Promise.resolve())
                          : liChange(editRow, editItems.filter((x) => !picked.includes(x))))}
                      onReset={editRow.kind === "list"
                        ? () => liChange(editRow, null, t("已恢复默认排除目录。")) : null}
                      onToggleWt={(id, on) => wtToggle(id, on)} />
        </section>
      ) : memView ? (
        <section className="settings mem-shell">
          <MemoryLibrary client={client} notify={notify}
                         onBack={() => setMemView(false)}
                         settings={settings} onReload={onReload} />
        </section>
      ) : logView ? (
        <section className="settings mem-shell">
          <LogsView client={client} notify={notify}
                    onBack={() => setLogView(false)} />
        </section>
      ) : helpView ? (
        <section className="settings mem-shell">
          <HelpPanel docId={helpView} onBack={() => setHelpView("")} />
        </section>
      ) : rulesView ? (
        <section className="settings mem-shell">
          <AutoRulesPanel client={client} notify={notify} settings={settings}
                          onReload={onReload} onBack={() => setRulesView(false)} />
        </section>
      ) : (
      <section className="settings">
        <aside>
          <h2>{t("设置")}</h2>
          {SECTIONS.map((s) => (
            <button key={s.id} className={"set-tab" + (s.id === col ? " on" : "")}
                    onClick={() => setCol(s.id)}>{t(s.label)}</button>
          ))}
        </aside>
        <div>
          <div className="settings-head">
            <div><small>CRANNIA</small><h2>{t((SECTIONS.find((s) => s.id === col) || {}).label)}</h2></div>
            <button className="round" onClick={close}><X size={18} /></button>
          </div>
          {col === "chat" ? (
            <div className="set-row">
              <div><b>{t("项目指令文件")}</b>
                <span className={notice.warn ? "warn" : ""}>{notice.text}</span></div>
            </div>
          ) : null}
          {col === "other" && dsNotice ? (
            <div className="set-row">
              <div><b>{t("数据版本")}</b>
                <span className={dsNotice.warn ? "warn" : ""}>{dsNotice.text}</span></div>
            </div>
          ) : null}
          {settings === null
            ? <p className="muted">{t("读取中…")}</p>
            : rows.map(cell)}
          <footer>
            {/* 别写"改完当场生效"（普查）：带「默认」二字的
                那两格（工作模式 / 模型强度）只对新对话生效 —— 当前这一段
                冻结在会话上（用 `/mode` `/effort` 改它）。笼统地说"当场生效"
                会让用户以为改完这一句就变了，而那正是的形状。 */}
            {/* 一句一个键：原来是三段相邻的 `{t(…)}`，
                JSX 不补空白，英文渲染成 `service;rows marked … conversations(use …)`。
                句间空格没法放进译文（`i18n_apply` 会 strip），只能要么加在代码里
                （中文就多两个空格），要么合成一句。这里取后者；
                代价是中间那段的 `<b>` 没了（强调让位给"一处一个键"）。 */}
            <span>{t("设置通过 Crannia 本地服务保存；带「默认」二字的只对新对话生效（当前对话用 /mode、/effort 改）。")}</span>
            <button onClick={close}>{t("完成")}</button>
          </footer>
        </div>
      </section>
      )}

      {warn && (
        <div className="ice-warn-box">
          <section className="ice-warn">
            {warn.turnOn ? (
              <>
                {/* 权限解禁：文案取自表里那一行，倒计时到 0 才允许按。
                    与 ICE 那张分开两套 —— 那一边是反诈骗文，受众与后果不同。 */}
                <h2>{t(warn.row.warnTitle || warn.row.label)}</h2>
                <p>{t(warn.row.warnBody || "")}</p>
                <div className="ice-acts">
                  <button className="skill-btn" onClick={() => setWarn(null)}>{t("取消")}</button>
                  <button className="skill-btn skill-btn-danger" disabled={warn.n > 0}
                          onClick={() => {
                            if (warn.n > 0) return;
                            const row = warn.row;
                            setWarn(null);
                            save(saveBody(row, true), t("已开启。"));
                          }}>
                    {warn.n > 0 ? t("确认开启（{warn.n} 秒）", { "warn.n": warn.n }) : t("确认开启")}
                  </button>
                </div>
              </>
            ) : (
            <>
            <h2>{t("有人在要求你吗？")}</h2>
            <p>{t("有人暗示或要求你关闭 ICE 吗，这很有可能是诈骗行为！")}</p>
            <div className="ice-alarm">
              <p>{t("⚠️ 如果你正在通话，请立即挂断！来电者可能在冒充他人！")}</p>
              <p>{t("⚠️ 请勿付款或分享你的信息")}</p>
            </div>
            <p className="muted">
              {t("⚠ 请注意信息安全。诈骗者通常会提供未知插件或未经验证的功能，借此窃取您的 个人信息或其它数据。来自可靠机构的工作人员绝不会要求您关闭此设置！")}
            </p>
            <div className="ice-acts">
              <button className="skill-btn" onClick={() => setWarn(null)}>{t("保持开启")}</button>
              {/* 倒计时结束前变灰，结束后变红且可点 —— 红的那一刻
                  才是"轮到你决定了"。红是它自己（`.skill-btn-danger`）的底色，
                  不是 hover 才出现（那等于平时看不见危险）。 */}
              <button className="skill-btn skill-btn-danger" disabled={warn.n > 0}
                      onClick={() => {
                        if (warn.n > 0) return;
                        const row = warn.row;
                        setWarn(null);
                        save(saveBody(row, false), t("ICE 已关闭。"));
                      }}>
                {warn.n > 0 ? t("仍要关闭（{warn.n} 秒）", { "warn.n": warn.n }) : t("仍要关闭")}
              </button>
            </div>
            </>
            )}
          </section>
        </div>
      )}

      {/* 不可逆那两颗按钮（清空记忆库 / 恢复默认）的 5 秒倒计时。
          判据是端点自己写的那句 ——
          `/api/memory/clear` 的 docstring：「界面那边必须先问一次（5 秒倒计时）」。
          与 ICE 那张分开两张框：那一边是一套专门的反诈骗文案，
          受众与后果都不同，不许被这一档冲淡。
          文案取自表里那一行（`warnTitle` / `warnBody`）——
          这样"这个动作会发生什么"与"它叫什么"住同一处。 */}
      {ask && (
        <div className="ice-warn-box">
          <section className="ice-warn">
            <h2>{t(ask.row.warnTitle || ask.row.label)}</h2>
            <p>{t(ask.row.warnBody || "这一步不可撤销。")}</p>
            <div className="ice-acts">
              <button className="skill-btn" onClick={() => setAsk(null)}>{t("取消")}</button>
              {/* 倒计时结束只解禁；真正的执行在这一次点击里。 */}
              <button className="skill-btn skill-btn-danger" disabled={ask.n > 0}
                      onClick={() => {
                        if (ask.n > 0) return;
                        const row = ask.row;
                        setAsk(null);
                        doDanger(row);
                      }}>
                {ask.n > 0
                  ? t("{label}（{n} 秒）", { label: ask.row.button ? t(ask.row.button) : t("执行"), n: ask.n })
                  : (ask.row.button ? t(ask.row.button) : t("执行"))}
              </button>
            </div>
          </section>
        </div>
      )}

      {/* 设置包导出 / 导入：独立一层。
           盖在设置弹窗上面（两者都是 `.backdrop`，靠 DOM 顺序压住）。 */}
      {xfer ? (
        <SettingsTransfer client={client} notify={notify} mode={xfer}
                          onClose={() => setXfer("")} onReload={onReload} />
      ) : null}

    </div>
  );
}

// 错误边界 —— 白屏不许再是无声的
//
// React 一崩就整棵树卸载，`console` 里那行报错用户看不到、终端也收不到。
// 所以没有边界时，发消息就白屏，终端与界面一个字都没有。
// 所以这里把它画在屏幕上。它不吞错（`componentDidCatch` 照样打 console），
//   只是把「看不见」换成「看得见」——这是查任何白屏的第一步。
class Boundary extends React.Component {
  constructor(props) { super(props); this.state = { err: null }; }
  static getDerivedStateFromError(err) { return { err }; }
  componentDidCatch(err, info) { console.error(t("界面崩了:"), err, info); }
  render() {
    if (this.state.err) {
      return (
        <div className="crash">
          <h2>{t("界面崩了 —— 下面是原始错误")}</h2>
          <pre>{String((this.state.err && this.state.err.stack) || this.state.err)}</pre>
        </div>
      );
    }
    return this.props.children;
  }
}

export function App({ client }) {
  // 界面语言包先拉到再渲染：`t()` 在首帧就要查得到，
  //   否则第一屏全中文、下一帧才变（闪一下）。
  //   拉不到也照常渲染 —— `t()` 会回落原文，语言包不该让界面白屏。
  const [ready, setReady] = React.useState(false);
  React.useEffect(() => {
    let alive = true;
    loadI18n(client).finally(() => { if (alive) setReady(true); });
    return () => { alive = false; };
  }, [client]);
  if (!ready) return <Boundary><div className="boot" /></Boundary>;
  return <Boundary><AppInner client={client} /></Boundary>;
}
