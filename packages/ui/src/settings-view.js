// 「设置」那一屏的表。
//
//   为什么单独一个文件（同 `endpoint-form.js` / `group-form.js`）：这是一张表，
//   而表最会烂的方式是"加了设置忘了加行 / 加了一行而那个键根本没接上"。
//   所以纯函数 + node 逐条断言 + 后端侧按同一张键表对账。
//
// 四栏：工作设置 / 对话设置 / 安全设置 / 其他设置；
//    带 `needs` 的是上级开关打开后才显示。
//
// 有一条纪律比"画全"更要紧：每一个格都必须接在一条真键上
//     —— 要么它有 `key`（那就要证明 `POST /api/settings` 收它），要么它是
//     `act`（一个有真端点的按钮），要么它明明白白是 `notdone` / `moved`
//     （带原因）。画一个按了没反应的开关就是在骗用户。
//     所以机械读者会拿这份表去反查后端：每个 `key`
//       必须在 `/api/settings` 的 GET 里出现、也必须被 POST 收。
//
// 兜底值住在后端（这一份里没有 `default` 字段）：`GET /api/settings` 会把
//    "没写过这个键时真实生效的值"一并交出来。
//    所以界面不抄字面量 —— 抄一份就是第二套真相源，改了默认值界面会撒谎。

// 界面语言：下面的 `label` / `note` / `why` 全是模块级常量，
//    一律在取用处翻（`optionsFor` / `leafNotice` 都是渲染时调的函数，
//    在这里 `t()` 是安全的；表本身不许包）。
import { t } from "./i18n.js";

// 那段说明里提到的两个数，家在这里
//
// 判据（一个口径一个地方）：下面那张表里 `thread_notice` 那一行会把这
// 两个数念给用户听，而真正拿它们干活的代码在 `App.jsx`
// （一个管"取多少条"、一个管"什么时候提醒清理"）。
// 所以数住在说出它们的那一份里，`App.jsx` 取用。
// 若两处各自写死（`App.jsx` 里 `?limit=30` 与 `THREAD_NOTICE_AT = 50`），
//    改一处就会让设置页那句话当场变成假话，而没有任何东西会红。

/** 左栏「任务」一次向 `/api/threads` 要多少条（页长，不是上限 —— 见下）。 */
export const THREAD_LIST_LIMIT = 30;

/** 归档超过这么多段就提醒一次清理。
 *
 *  它是提醒阈值，不是数量上限 —— 对话本身不设上限：
 *  用户自己的话与模型的回答是劳动成果，一条都不许自动动
 *  （自动淘汰的只有工具结果）。 */
export const THREAD_NOTICE_AT = 50;

/** 四栏（也是界面上的显示顺序）。 */
export const SECTIONS = [
  { id: "work", label: "工作设置" },
  { id: "chat", label: "对话设置" },
  { id: "safe", label: "安全设置" },
  { id: "other", label: "其他设置" },
];

/** 每一格。
 *
 *  `kind`：
 *   · `toggle`  布尔开关（`key` 是那个键）
 *   · `choice`  单选（`key` + `from` 从载荷里取选项表）
 *   · `number`  数字（`key` + `min`/`max`/`unit`）
 *   · `act`     一颗按钮（`act` 是动作名，组件里有对应实现）
 *   · `moved`   这块的控制在别处（`why` 说清在哪儿）—— 不是"没做"，是"不在这儿"
 *   · `notdone` 还没做（`why` 说清缺什么）—— 画出来但禁用，
 *               比"悄悄不显示"诚实：用户找得到它、也知道它现在是空的
 *
 *  `needs`：上级开关的键。上级关着，这一行不显示。
 */
export const ROWS = [
  // ── 工作设置 ──────────────────────────────────────────────────────
  { id: "cross_zone", section: "work", kind: "toggle", key: "cross_zone_enabled",
    label: "跨区搜索", note: "允许 Agent 离开工作区做只读操作" },
  { id: "auto_approve", section: "work", kind: "toggle", key: "auto_approve",
    needs: "cross_zone_enabled", label: "自动放行跨区搜索",
    note: "开启后，Agent 将不再就此询问你" },
  // 规避目录是一个真编辑器：它不是一个独立端点 —— 就是把 `settings.json`
  //   里的那一格（一个字符串数组）读出来、写回去（`POST /api/settings`）。
  //   空列表不等于没有规避目录 —— `GET /api/settings` 会把默认那一批
  //   （系统目录 / AppData / 开发缓存）并回来，「还原默认」就是存一个空表。
  { id: "forbidden", section: "work", kind: "list", key: "forbidden_directories",
    needs: "cross_zone_enabled",
    label: "规避目录",
    note: "这些目录不会被搜索",
    ph: "完整路径，例如 D:\\Games\\saves",
    empty: "（列表为空：系统默认目录仍然生效）" },
  // 没有「工作组」开关：工作区列表恒生效
  // （`core/workspace.py::load_from_settings`）。
  // 不要再加回来：它 ON 那半的语义（"允许跨工作区执行操作"）归跨区那一条，
  // 与"加载不加载列表"是两件事（把它们绑在一个开关上正是它说不清的原因）。
  { id: "cascade", section: "work", kind: "toggle", key: "cascade_enabled",
    label: "级联动态", note: "自动捕获目标文件夹的更新，回传文件至工作区" },
  { id: "cascade_notify", section: "work", kind: "toggle", key: "cascade_notify",
    needs: "cascade_enabled", label: "启用通知", note: "有文件回传至工作区时发出通知" },
  // 「通知音量」暂时不做—— 原来这里是个 `notdone` 占位，
  //    摆着一个永远点不动的开关只是在提醒"这里有洞"。声音通道真做了再加回来。
  //    条目本身仍在待办里，这里只是不显示。
  { id: "cascade_auto", section: "work", kind: "toggle", key: "cascade_auto_start",
    needs: "cascade_enabled", label: "自启动监听", note: "启动框架时自动启动监听器" },
  // 监听器列表。
  //   没有 `key`：真值是 `data/settings.json` 的 `watchers[]`，写侧走
  //   自己的端点（`/api/watchers`），不走 `POST /api/settings` ——
  //   与「访问白名单」同一类（真相不在那份键表里）。
  //   有它才说得通：`#02` 会告诉用户"某个监听没跑起来"，而没有这一格时
  //   用户只能去手改 JSON。
  { id: "watchers", section: "work", kind: "watchers",
    needs: "cascade_enabled",
    label: "监听目录",
    note: "从这些目录捕获新文件，回传到工作区",
    ph: "完整路径，例如 C:\\Users\\你\\Documents\\微信\\msg",
    empty: "（还没有监听任何目录）" },
  { id: "diff_days", section: "work", kind: "number", key: "file_history_days",
    label: "diff 留存周期", unit: "天", min: 1, max: 365,
    note: "改动前备份的保留时长（到期自动清理）" },
  { id: "diff_prune", section: "work", kind: "act", act: "prune_history",
    label: "立即清理", note: "按上面的周期清一次留档", button: "清理" },
  { id: "subagent", section: "work", kind: "choice", key: "subagent_model",
    label: "子代理模型设置", from: "subagent_model_options", emptyLabel: "沿用当前会话的模型",
    note: "更改子代理的模型选择" },

  // ── 对话设置 ──────────────────────────────────────────────────────
  { id: "stream", section: "chat", kind: "toggle", key: "stream_output",
    label: "逐字输出", note: "仅影响观感，不影响任何功能" },
  // 模型回答语言。
  //   只管模型写什么语言，不改界面文字（界面 i18n 是另一条线）。
  //   空选项 = 没设过（跟随系统）—— 它不是"中文"，这一点由后端原样下发
  //      （`response_language: null`）；界面不许拿生效值顶上去冒充"设过"。
  { id: "response_language", section: "chat", kind: "choice",
    key: "response_language", from: "response_languages",
    emptyLabel: "跟随系统", label: "模型回答语言",
    note: "只改模型的回答，不改界面文字；对话里可用 /language 临时改这一段" },
  // 界面语言—— 与上面那一条是两条轴：
  //   这个只管按钮 / 标签 / 通知 / 命令回话，回答语言只管模型写什么。互不影响。
  // 有「跟随系统」（非中文用户也得能用）：
  //   空值 = 没设过 = 跟随系统，而且从安装引导第一屏就生效 ——
  //   壳把 `app.getLocale()` 传给后端（`CRANNIA_SYSTEM_LOCALE`）。
  { id: "interface_language", section: "chat", kind: "choice",
    key: "interface_language", from: "interface_languages",
    emptyLabel: "跟随系统", label: "界面语言",
    note: "按钮、提示与命令回话用哪种语言；不影响模型的回答语言" },
  // 逐条轨迹（参数全文 / 思考 / 结果实质）。
  //   默认关 —— 与上面「逐字输出」同一条判据（观感因人而异，不替用户决定）。
  //   打开之后右侧竖栏多一格「轨迹」；它不落盘、不进模型上下文。
  { id: "trace", section: "chat", kind: "toggle", key: "trace_full",
    label: "记录完整轨迹",
    note: "右侧「轨迹」面板能看到每次工具调用的参数全文、模型的思考和结果原文" },
  // 生图与视频的配置卡不在这里（用户）——
  //   它的家在技能页（`media_gen` 那张卡的「配置」），与 `github` /
  //   `mail_parser` 同一个位置。两处入口 = 两份要维护的东西，而用户只会在
  //   技能页找它（就是这么找不到的）。
  // 接管的永久应用白名单。
  //   接管时弹窗里点「同意，且永久批准此应用」写进来的就是这一份；
  //   在这里可以移除，也可以手动加入。
  //   走的是 `/api/allowlist`（与 `net_list` / `cmd_list` 同一套），
  //   不是 `/api/settings` —— 真相是"名单里有没有那一行"，不是另存一个布尔。
  //   值 = exe 小写 basename（`notepad.exe`），不是窗口标题、不是完整路径。
  { id: "takeover_apps", section: "chat", kind: "allowlist", allowlist: "computer_app",
    label: "接管应用",
    note: "接管时，在此处的应用将不询问",
    ph: "例如 notepad.exe",
    empty: "（名单为空：首次控制某个应用时会询问）" },
  // 「50 条存储提醒」的撤回口。
  //   用户在横幅上点「不再提醒」，这一格跟着变成"打开"。
  //   它必须存在：那是一条持久化的"不许"，没有归位口就成了
  //   "存了就再也回不来"（持久化的拒绝必须配一条重判的路）。
  //   与「安全提示」那条存 `localStorage` 的相反 —— 那一条换设备就自己回来了。
  //   阈值取自常量（不许在这里再写死一个 50）。
  { id: "thread_notice", section: "chat", kind: "toggle", key: "thread_notice_off",
    label: "关掉存储提醒",
    //   这句是纯文本（`App.jsx` 那边是 `<span>{row.note}</span>`）
    //   不许写 markdown 的粗体记号，那会原样显示成星号。
    //   模板串 + `noteVars`，不要用 `${…}` 插值：插值在 import 时
    //   就定死成一个数，而 i18n 的键是模板（`对话条目超过 {n} 时…`）
    //   运行时 `t()` 拿 "…超过 50 时…" 去查，永远查不到（回落中文）。
    note: "对话条目超过 {n} 时将出现提醒",
    noteVars: { n: THREAD_NOTICE_AT } },
  { id: "preset", section: "chat", kind: "choice", key: "framework_preset",
    from: "framework_presets", label: "框架模式预设",
    note: "三档内置预设，Fern-轻量 / Willow-均衡 / Ginkgo-全面" },
  { id: "sliding", section: "chat", kind: "toggle", key: "context_sliding_window",
    label: "滑动上下文窗口", note: "超出保留期的消息会被丢弃，以节省上下文空间、加快回复。" },
  { id: "sliding_gap", section: "chat", kind: "number", key: "sliding_window_gap_minutes",
    needs: "context_sliding_window", label: "删除时间", unit: "分钟", min: 1, max: 1440,
    note: "在此时间之外的冷对话会被裁剪" },
  { id: "rewind_files", section: "chat", kind: "toggle", key: "rewind_restore_files",
    label: "对话回溯时回退文件改动",
    note: "使用 /rewind 时是否回退文件改动" },
  { id: "memory_continuity", section: "chat", kind: "choice", key: "memory_continuity",
    label: "记忆接续", from: "memory_continuity_options",
    note: "每次注入多少记忆" },
  { id: "memory_write", section: "chat", kind: "toggle", key: "memory_write",
    label: "记忆写入", note: "Agent 会自行写入记忆" },
  { id: "memory_organize", section: "chat", kind: "toggle", key: "memory_organize",
    label: "记忆巩固", note: "Agent 会自行维护记忆" },
  { id: "memory_extract", section: "chat", kind: "number", key: "memory_extract_every_n_turns",
    label: "记忆抽取频率", unit: "轮/次", min: 0, max: 50,
    note: "0 = 不自动抽取（只靠模型自己判断要不要记）" },
  // 记忆库的独立页（用户）：点开之后设置弹窗的画面整个换成
  //    列表 / 改写 / 删除 / 封条管理，左上角返回。放在「清空」上面 ——
  //    先能看见"有什么"，再决定要不要一把清掉。
  { id: "memory_view", section: "chat", kind: "act", act: "open_memory",
    label: "查看记忆库", button: "打开",
    note: "逐条查看 / 改写 / 删除，并管理封条" },
  // 清空记忆库是一个真按钮，端点是 `/api/memory/clear`。
  //   界面必须先问一次（5 秒倒计时），不能点一下就清。
  //   `danger: true`，走那个 5 秒倒计时（见 `App.jsx` 的 `ask` 那一档）。
  { id: "memory_clear", section: "chat", kind: "act", act: "clear_memory",
    label: "清空记忆库", button: "清空…", danger: true,
    note: "清空记忆库并标记",
    warnTitle: "清空记忆库？",
    warnBody: "长期记忆会被全部删除，且无法恢复。" },
  { id: "recap", section: "chat", kind: "toggle", key: "recap_enabled",
    label: "对话回顾", note: "在活跃对话里离开一段时间后，触发一段简短的工作回顾" },
  { id: "recap_after", section: "chat", kind: "number", key: "recap_after_minutes",
    needs: "recap_enabled", label: "回顾触发时间", unit: "分钟", min: 1, max: 1440 },
  { id: "approval_wait", section: "chat", kind: "number", key: "approval_timeout_seconds",
    label: "审批等待", unit: "秒", min: 0, max: 3600,
    note: "自动模式下的请求挂起时间，超出时间的请求按拒绝处理。留 0 则为持续挂起" },
  //:这两格是新会话的默认值，与主界面那枚 chip 分工不同：
  //:   chip 改的是「当前这一段」（`/api/mode` / `/effort`，`/effort` 不落盘），
  //:而「以后新会话用什么」只有这一个家。删掉它们等于删掉这个功能本身。
  { id: "mode_default", section: "chat", kind: "choice", key: "default_mode",
    from: "modes", label: "工作模式",
    note: "更改默认模式", extra: true },
  { id: "effort_default", section: "chat", kind: "choice", key: "effort_level",
    from: "effort_levels", label: "模型强度",
    note: "更改模型默认强度", extra: true },

  // ── 安全设置 ──────────────────────────────────────────────────────
  { id: "ice", section: "safe", kind: "toggle", key: "ice.enabled", confirm: "ice",
    label: "ICE", note: "本地安全模块：增强的信息处理与网络防护" },
  { id: "ice_info", section: "safe", kind: "toggle", key: "ice.info_protection",
    needs: "ice.enabled", label: "信息层", note: "增强信息处理防护" },
  { id: "ice_net", section: "safe", kind: "toggle", key: "ice.net_protection",
    needs: "ice.enabled", label: "网络层", note: "增强网络交互防护" },
  // 权限解禁 ——
  //   只在完全权限模式下生效：审批、命令黑名单、受保护路径、批量删除确认
  //   一并让开。打开那一下先警告 + 3 秒倒计时（`confirm:"full"`）。
  //   关掉不需要警告（往安全那边走）。
  { id: "full_unlock", section: "safe", kind: "toggle", key: "full_access_unlock",
    confirm: "full", label: "权限解禁",
    note: "仅对完全权限模式生效：代理执行时不再有任何拦截",
    warnTitle: "您正在更改完全权限下的安全边界",
    warnBody: "请注意，此项设置打开后 agent 的行动将不再有任何阻力，再次确认您的生产需要。",
    confirmSeconds: 3 },
  // 安全提示的偏好可逆。
  //   两颗安全提示（自动模式 / 接管）各有一个 localStorage 的「不再提醒」，
  //   而那一格只有本机能看见，撤销口必须长在同一个 origin 的界面上。
  //   `local: true` = 不碰服务端（它连不上时也该能按）。
  //   它只清提示偏好，不动任何权限/白名单；完全权限没有可关的偏好
  //   （每次必弹），所以文案不把它算进来。
  //:按钮写「恢复提示」而不是「恢复」：`恢复` 这个词同时被定时任务的
  //:   「暂停 → 恢复（Resume）」用着，而 i18n 的键是中文原文，同词不同义
  //:   会塌成一个键（英文界面上这颗按钮会变成 "Resume"实见）。
  { id: "notice_reset", section: "safe", kind: "act", act: "reset_notices", local: true,
    label: "恢复安全提示", button: "恢复提示",
    note: "恢复弹窗提醒（下次会重新提醒；只影响提示，完全权限仍每次询问）" },
  { id: "net_all", section: "safe", kind: "toggle", key: "allowlist_egress_all",
    allowlist: "egress", label: "网络策略 · 全部放行",
    note: "开启后，出网审批会移交自动审查，绝大部分情况下，Agent 将不再就此询问你" },
  // 访问白名单是一个真编辑器。
  //    审批弹窗里点「信任此站点，以后不再询问」会写进 `data/allowlist.json`
  //    —— 而看得见、撤得掉是那条破例的代价（界面必须一直看得见授权被改过）。
  //    它走的是 `/api/allowlist`，不是 `/api/settings`（判据同上面两颗
  //    「全部放行」开关：真相是"名单里有没有那一行"，不是另存一个布尔）。
  { id: "net_list", section: "safe", kind: "allowlist", allowlist: "egress",
    label: "访问白名单",
    note: "此处的站点不再询问",
    ph: "例如 example.com",
    empty: "（名单为空：每个新域名都会询问）" },
  // ── 出网代理──
  //   用户报「一直异常」，根因是客户端的默认值：
  //   `httpx` / `requests` 的 `trust_env=True` 会静默采纳环境变量里的
  //   `ALL_PROXY` —— 一条没在听的本地代理就能卡死整轮对话，而界面只会说
  //   「请检查宽带互联网连接」。这三档就是那条路的开关。
  //   默认 `system` = 今天的行为（有人只能靠代理上网，改默认会弄坏他们）；
  //     `direct` 是"系统代理配错了"的自救路，不必去改 Windows 环境变量。
  { id: "proxy_mode", section: "safe", kind: "choice", key: "proxy_mode",
    from: "proxy_modes", label: "网络代理",
    //: 选了这一档要先填地址才存得下去（后端会拒"只要模式、没有地址"）。
    //:   这里不落盘、先记草稿，让下面那行输入框露出来。
    draftValue: "manual",
    //: 生效值挂在模式这一行上（不是地址那一行）—— 地址那一行只在「指定」
    //:   时才露出来，而在「跟随系统」下用户同样需要看得见它实际走了哪儿。
    effectiveFrom: "proxy_state",
    note: "管理 Crannia 的网络代理模式" },
  { id: "proxy_url", section: "safe", kind: "text", key: "proxy_url",
    needsEq: { key: "proxy_mode", value: "manual" },
    effectiveFrom: "proxy_state",
    label: "代理地址",
    note: "填主机和端口即可（如 127.0.0.1:7890）；需要认证写全 http://用户名:密码@主机:端口",
    ph: "127.0.0.1:7890" },
  { id: "cmd_all", section: "safe", kind: "toggle", key: "allowlist_command_all",
    allowlist: "command", label: "命令策略 · 全部放行",
    note: "开启后，命令审批会移交自动审查，绝大部分情况下，Agent 将不再就此询问你；但首次执行仍会询问。" },
  { id: "cmd_relax", section: "safe", kind: "toggle", key: "relax_command_tier",
    label: "危险命令降级", note: "危险档的指令保护降级，归为普通命令防护" },
  { id: "cmd_noask_readonly", section: "safe", kind: "toggle", key: "no_ask_readonly",
    label: "只读命令免问", note: "不再询问只读类型命令" },
  // 命令白名单是一个真编辑器，与 `net_list` 同一套 `kind:"allowlist"`：
  //   渲染、加、删、走 `/api/allowlist`。
  //   `note` 里那句是用户面：命令是整条精确匹配、不是前缀 ——
  //   所以「总是允许」永远不会落空，代价是 `git status --short` 还会问。
  { id: "cmd_list", section: "safe", kind: "allowlist", allowlist: "command",
    label: "命令白名单",
    note: "此处的命令不再询问；请注意，名单为精确匹配",
    ph: "整条命令，例如 git status",
    empty: "（名单为空：每条命令都会询问）" },
  // 自动审查的长期规则：两个方向都收（说"可以"的和说"不可以"的都要能
  //   表达，否则用户只能靠改代码表达偏好），分成三段。
  //   点「配置」新开一页（形状同记忆库）：顶栏三段切换，左上角返回。
  //   管不到限位闸（出网命令族 / 受保护路径）—— 那一层在分类器之前结算，
  //     所以那句话必须出现在界面上（否则用户以为写了规则就绕过了它）。
  //   规则写错是用户自己的授权决定：不加二次确认，也不在读取时
  //     "看起来危险就忽略" —— 只验形状（段名 / 条数 / 长度 / 单行）。
  { id: "auto_rules", section: "safe", kind: "act", act: "open_auto_rules",
    label: "自动审查配置", button: "配置",
    note: "允许 / 禁止 / 环境三段规则；限位闸（出网命令族、受保护路径）不受它影响" },
  // 工作区钩子的信任门。
  //   读侧是 `data/hooks.json` 与 `~/.claude/settings.json`（不需要这个开关），
  //   写进仓库的那几份（`.crannia/hooks.json` / `.claude/settings.json`）才要它。
  //   默认关：那几份配的是任意命令，而它们跟着仓库走 ——
  //   clone / 解压 / 同事发来的目录都会带一份，打开之后，别人写的脚本会在
  //   你的机器上跑。关着时它们不执行（控制台会印一行说明），不是静默忽略。
  //   改完当场生效（`core/hooks._project_allowed()` 每次现读），不用重启。
  { id: "hooks_project", section: "safe", kind: "toggle",
    key: "hooks_allow_project", label: "工作区钩子",
    note: "允许工作区里的 hooks 配置执行命令（仓库自带的脚本会在你的机器上运行）" },
  // 工作区 LSP 配置的信任门。
  //   判据与上面那条逐字同一条：`lsp.json` 里写的是 `command` / `args` / `env`,
  //   而那份文件跟着仓库走（clone / 解压 / 同事发来的目录都会带一份）
  //   所以默认关：关着时它不读（控制台印一行说明），不是静默忽略。
  //   全局的 `data/lsp.json` 与"本机装好了哪个语言服务器"的探测不受影响。
  //   改完当场生效（`core.code_intel._project_allowed()` 每次现读）。
  { id: "lsp_project", section: "safe", kind: "toggle",
    key: "lsp_allow_project", label: "工作区 LSP 配置",
    note: "允许工作区里的 lsp.json 启动语言服务器（它会按那份文件里的命令起进程）" },
  // 工作区插件包的信任门。
  //   判据同上：包根 `plugins/` 与 `.agents/plugins` 是相对工作区的，
  //   跟着仓库走；而包里能带 hooks（执行命令）与 `.mcp.json`（启动进程）。
  //   默认关：关着时这两样不生效（控制台印一行说明），不是静默忽略。
  //   用户自己机器上的包根（`~/.agents/plugins` 与两个缓存目录）不受它管 ——
  //   那些不是跟着别人的仓库进来的。
  //   改完当场生效（`core/plugin_packages.workspace_plugins_allowed()` 每次现读）。
  { id: "plugins_workspace", section: "safe", kind: "toggle",
    key: "plugins_allow_workspace", label: "工作区插件",
    note: "允许工作区里的插件包加载钩子与 MCP 服务器（它们会在你的机器上执行命令）" },
  { id: "backup_space", section: "safe", kind: "number", key: "file_history_max_mb",
    label: "自动备份 · 空间上限", unit: "MB", min: 16, max: 4096,
    note: "自动备份空间大小" },
  //:已经做了（大批删除固定询问 + 框架自己的删除进回收站）。
  //:这一格没有开关（机制恒开），用 `kind:"info"` 只展示"已启用"，
  //:   不画一个按了没反应的开关。
  //:它不是"删除安全了"：`run_command` 的 `rm` 照旧直接删（见 `core/recycle.py` 抬头）。
  //:一句一个字符串字面量：拆成两段 `"…" + "…"` 时扫描器收的是两半，
  //:而运行时 `t(row.note)` 查的是拼好的整句，永远查不到、永远回落中文
  //:   （英文界面截图：这一行的说明是中文）。
  { id: "del_guard", section: "safe", kind: "info", label: "删除保护",
    note: "对删除动作存档：框架自己的文件删除会进系统回收站（恒定生效；命令行里的删除不受它管）" },

  // ── 其他设置 ──────────────────────────────────────────────────────
  // 配色自定义是一个真编辑器。
  //   它不是后端设置：配色只存 GUI 自己的 localStorage（`theme.js::THEME_KEY`）。
  //   为什么不走 `/api/settings`：GUI 跑在 `crannia-app://`、WebUI 跑在
  //      `127.0.0.1:5000`，两个 origin 的 localStorage 天然不通；要共用只能存后端，
  //      那是另一件事（先各存各的）。
  //   样式表里1241 行 / 389 处写死的颜色，CSS 变量只有 8 个
  //      （`--ink` `--muted` `--line` `--side` `--mint` `--green` `--green-dark` `--clay`）
  //      所以"6 色主题"要真正管用，得先把那 389 处收成变量（对整张样式表做手术）。
  //   v1 的边界写在 `theme.js` 抬头：六个轴各驱动一个令牌，整表不跟着推导。
  { id: "theme", section: "other", kind: "theme", label: "配色自定义",
    note: "在此自定义界面配色（仅对本机生效）" },
  { id: "boot", section: "other", kind: "toggle", key: "boot_on_startup",
    label: "开机自启动", note: "开机时 Crannia 自行启动" },
  { id: "channels", section: "other", kind: "toggle", key: "auto_start_channel",
    label: "连带启动通道", note: "启动时自动拉起已配置的通道" },
  //:纯客户端动作（`local: true`）—— 只是把引导再开一次，
  //:   不删任何已保存的工作区 / 模型 / 通道，也不碰「首次」标记。
  { id: "onboarding_replay", section: "other", kind: "act",
    act: "onboarding_replay", local: true,
    label: "重新运行新手引导", button: "重新运行",
    note: "重新走一遍首次配置流程；不会删除已保存的工作区、模型和通道配置" },
  { id: "update_auto", section: "other", kind: "toggle", key: "update_auto_check",
    label: "自动更新" },
  { id: "update_manual", section: "other", kind: "act", act: "check_update",
    label: "手动检查更新", button: "检查" },
  { id: "logs", section: "other", kind: "act", act: "open_logs",
    label: "运行日志", note: "仅供测试反馈，请不要对外泄露你的运行日志",
    button: "查看" },
  // 把数据目录摆出来，并且能一键打开 —— 判据是"卸载会删掉它"。
  //   卸载程序对安装目录是 `RMDir /r`，而 `resources\app\data` 就在那里面。
  //   用户要在卸载之前把东西捞走，前提是他知道东西在哪、并且点得开。
  { id: "data_dir", section: "other", kind: "act", act: "open_data_dir",
    label: "数据目录",
    note: "对话、长期记忆、模型配置与凭据都在这里；卸载程序会连同它一起删除，请先备份",
    button: "打开" },
  { id: "restart", section: "other", kind: "act", act: "restart",
    label: "重启框架", note: "动过端配置之后要重启一次才生效", button: "重启" },
  //:对外文档的唯一入口：以后所有说明页都当条目
  //:放在这里。条目清单是 `HelpPanel.jsx::HELP_DOCS`（一处定义），
  //:   正文在 `docs/help/*.md`（改措辞改那儿，重新构建即生效）。
  //:它没有 `key` —— 不是设置项，不落 `settings.json`、
  //:   也不走 `/api/settings`（所以 `test_desktop_settings` 那两条不会误判）。
  { id: "help_docs", section: "other", kind: "help",
    label: "帮助文档", note: "自动模式与接管模式的完整说明" },
  //: 往期公告 —— 与记忆库同一个形状：点开之后设置弹窗整块换成独立页，
  //:   左上角返回。内容来自构建时打进包里的 `docs/updates/*.txt`
  //:   （`update-notice-content.js` 的 glob），纯客户端动作、没有端点。
  { id: "update_notices", section: "other", kind: "act", act: "open_notices",
    local: true,
    label: "往期公告", button: "查看",
    note: "历次版本更新了哪些内容" },
  // 恢复默认是一个真按钮，端点是 `POST /api/settings/restore-default`。
  { id: "reset", section: "other", kind: "act", act: "restore_defaults",
    label: "恢复默认", button: "恢复默认", danger: true,
    note: "重置你的改动",
    warnTitle: "恢复默认？",
    warnBody: "当前这一份设置会被出厂基准覆盖掉。" },
  // 这一格从 `notdone` 接成两颗真按钮。
  //   导出默认不含凭据；勾了才带，且口令加密（口令丢了没有找回通道 —— 界面要说清）。
  { id: "export", section: "other", kind: "act", act: "export_settings",
    label: "导出设置", button: "导出",
    note: "导出当前的偏好、白名单、模型配置与界面外观" },
  { id: "import", section: "other", kind: "act", act: "import_settings",
    label: "导入设置", button: "导入",
    note: "导入您的设置" },
  // 反馈口不走邮箱，一律引导到 GitHub issue。
  //   原实现是 `POST /api/feedback` —— 它拿用户自己配的邮箱账号把正文 +
  //   导出的运行日志发出去，等于让用户先把邮箱凭据填进来才提得了意见。
  //   判据：反馈通道不该要求用户先把邮箱凭据交出去。
  //   所以现在只开一条外链：`window.open` 交给系统浏览器，框架这一侧零外发。
  { id: "issues", section: "other", kind: "act", act: "open_issues", local: true,
    label: "问题反馈", button: "跳转",
    note: "前往 GitHub 项目页反馈 issue —— 需要能使用的 GitHub 账号，反馈时注意隐私保护" },
  // 下面不写说明：把 URL 摆在行里既挤又不像一条动作。
  { id: "project", section: "other", kind: "act", act: "open_project", local: true,
    label: "项目主页", button: "跳转" },
];

/** 上级开关开着时才显示的那一行。
 *
 *  判据只有一条：`needs` 那个键在当前载荷里为真。
 *  读不到（`undefined`）不算"关" —— 载荷还没到时整页本来就是空的，
 *    而把"读不到"当关会让先画一半、再补一半（闪一下）。由调用方保证
 *    载荷到位（`settings === null` 时页面显示"读取中"）。
 */
export function rowsFor(section, s) {
  const vals = s || {};
  return ROWS.filter((r) => r.section === section
    && (!r.needs || truthy(deepGet(vals, r.needs)))
    //:`needsEq` = 按值显示（`proxy_url` 只在选「指定」时才露出来）。
    //:   `needs` 只能判真假，而这一格要判的是"等于某一个值"。
    && (!r.needsEq
        || String(deepGet(vals, r.needsEq.key) ?? "") === String(r.needsEq.value)));
}

/** `"ice.enabled"` 这种带点的键要能取到嵌套的那一格（ICE 是嵌套的）。 */
export function deepGet(obj, path) {
  let cur = obj;
  for (const part of String(path || "").split(".")) {
    if (cur === null || cur === undefined || typeof cur !== "object") return undefined;
    cur = cur[part];
  }
  return cur;
}

function truthy(v) { return v === true || v === "true" || v === 1; }

/** 选项表：`from` 指向载荷里的那一格（不许在这里抄一份）。
 *
 *  三种来源各有一个归一函数：
 *   · `subagent_model_options` —— `[{id, name, card_label, model}]`（我们的形状）
 *   · `framework_presets` —— `[{name, label, …}]` + 一个 `framework_preset_custom`
 *   · `memory_continuity` —— 它没有表（三档是写死的字符串），这里给出的是
 *     文案，取值仍是那三个串（它们与 `core/window_estimate.py` 认的口径同源）
 */
export function optionsFor(s, row) {
  const vals = s || {};
  if (row.from === "subagent_model_options") {
    return (vals.subagent_model_options || []).map((o) => ({
      value: o.id, label: `${o.name}（${t(o.card_label)}）`,
    }));
  }
  if (row.from === "framework_presets") {
    //:预设表给的是 `{name, summary, settings}` —— 没有 `label`（名字就是
    //:`Fern`/`Willow`/`Ginkgo`），标签拿 `summary` 当说明、名字当标题。
    //:用户「选择框内只写 Fern/willow/ginkgo，不多写解释」。
    //:   三档的含义改由上面那一行 `note` 说；下拉里只留名字。
    const out = (vals.framework_presets || []).map((p) => ({
      value: p.name, label: p.name,
    }));
    const custom = vals.framework_preset_custom || "custom";
    if (!out.some((o) => o.value === custom)) {
      out.push({ value: custom, label: t("自定义") });
    }
    return out;
  }
  if (row.from === "modes") {
    // 默认档不许选完全权限（`default_allowed === false`）——
    //   只许在对话里 `/mode` 按会话开。判据由后端下发，界面只过滤。
    return (vals.modes || [])
      .filter((m) => m.implemented !== false && m.default_allowed !== false)
      .map((m) => ({ value: m.key || m.value, label: t(m.name || m.label) }));
  }
  if (row.from === "effort_levels") {
    return (vals.effort_levels || []).map((lv) => ({
      value: lv.level,
      label: `${lv.level} —— ${t(lv.name)}${lv.implemented ? "" : t("（未实现）")}`,
    }));
  }
  if (row.from === "response_languages") {
    //:表由后端下发（`/api/settings` 的 `response_languages`）——
    //:   加第三门语言时界面不用改（与 `effort_levels` 同一条）。
    return (vals.response_languages || []).map((o) => ({
      value: o.id, label: t(o.name),
    }));
  }
  if (row.from === "interface_languages") {
    //:下拉项由后端自动发现 locales 目录里的 json 包（`GET /api/settings` 的
    //:   `interface_languages`）—— 放一个 `ja.json` 进去就有它，界面不用改。
    return (vals.interface_languages || []).map((o) => ({
      value: o.id, label: t(o.name),
    }));
  }
  if (row.from === "proxy_modes") {
    //:三档由后端下发（`GET /api/settings` 的 `proxy_modes`），文案也由它翻。
    return (vals.proxy_modes || []).map((o) => ({ value: o.id, label: t(o.name) }));
  }
  if (row.from === "memory_continuity_options") {
    return [
      { value: "fresh", label: t("更新（完全不注入）") },
      { value: "balanced", label: t("均衡（适当注入）") },
      { value: "memory", label: t("记忆（大量注入）") },
    ];
  }
  return [];
}

/** 这一格现在是什么值（读不到，`undefined`，由组件显示成"—"）。 */
export function valueOf(s, row) {
  return deepGet(s || {}, row.key);
}

/** 这一格的改动要 POST 什么（`/api/settings` 的 body）。
 *
 *  带点的键（`ice.enabled`）要拼成后端收的那个形状（`{ice: {enabled: …}}`）
 *     —— 后端收的是一个对象（`core.ice.ICE_SWITCH_KEYS`），不是扁平键。
 *     这是本表里唯一一处"界面要把表里的名字翻译成线上的名字"，
 *        所以它只有这一个函数（别在组件里再拼一次）。
 */
export function saveBody(row, value) {
  const parts = String(row.key || "").split(".");
  if (parts.length === 1) return { [row.key]: value };
  return { [parts[0]]: { [parts[1]]: value } };
}

/** 那两颗「全部放行」走的不是 `/api/settings`，而是 `/api/allowlist`
 *  （它的判据是"名单里有没有那一行 `*`"，不是另存一个布尔 —— `core/allowlist.py` 判据 ②）。 */
export function allowlistBody(row, on) {
  return { kind: row.allowlist, action: on ? "add" : "remove", subject: "*" };
}

/** 不在四栏标准清单里、但确实有后端键的那几样 —— 如实标出来。
 *
 *  它们照样画（有真键、有真行为，藏起来只会让人以为没这功能），
 *    但界面上要说清"这是额外的一格"。
 */
export const EXTRA_IDS = ["approval_wait", "cmd_noask_readonly"];

/** 这一格是不是标准清单之外的额外项。
 *  判据只有一处：`row.extra` 或 `EXTRA_IDS` —— 组件只问这一个函数。 */
export function isExtra(row) {
  return !!(row && (row.extra || EXTRA_IDS.includes(row.id)));
}

// ── 项目指令文件那一行的判据 ─────────────────────────────────────
//
//   它同时出现在设置页与启动日志里；这一份只管设置页那一行的文案。
//
// 它是提示不是限制：项目指令文件不拦任何东西，所以文案里不许出现
//   "已达上限 / 无法保存 / 请精简"那一类吓唬人的话。
// 读不到 ≠ 0：本轮还没装载过时后端给 `loaded=false`，
//   界面要说"还没装载"，不许显示"0 字符"。
// 超没超由后端判（`over`）—— 这里不拿 `chars` 与 `warn_chars` 自己比。

/** 千分位（ASCII 逗号）—— 只此一处，别在 JSX 里各写各的。 */
function withCommas(n) {
  return String(Math.round(Number(n) || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** 那一行怎么显示。回 `{text, warn, ready}`：
 *
 *  · `ready === false`，还没装载过（不是"0 字符"，见文件头）
 *  · `warn === true`，超了软告警线（界面据此给它一个告警色）
 */
/** 数据格式比本程序新时那句话。
 *
 *  判据同 ICE 的「保护已降级」：这个事实要一直看得见，不是弹一次就走。
 *  备份没做成时不许写成"已备份"（标签在说谎）—— 那一支要红。
 */
export function dataSchemaNotice(ds) {
  if (!ds || !ds.compat) return null;
  const found = ds.found == null ? "?" : ds.found;
  const ours = ds.ours == null ? "?" : ds.ours;
  if (ds.complete === false) {
    return {
      warn: true,
      text: t("数据格式 {found} 比本地 {ours} 新，备份失败，请先手动复制整个数据目录再使用。",
              { found, ours }),
    };
  }
  return {
    warn: true,
    text: t("数据格式 {found} 比本地 {ours} 新，正以兼容方式运行；修改前请查看备份：{backup}",
            { found, ours, backup: ds.backup || "?" }),
  };
}

export function leafNotice(leaf) {
  if (!leaf || !leaf.loaded) {
    return { text: t("还没装载 —— 跑过一轮之后，这里会显示它的大小。"),
             warn: false, ready: false };
  }
  const chars = withCommas(leaf.chars);
  const limit = withCommas(leaf.warn_chars);
  if (leaf.over) {
    return {
      text: t("{chars} 字符 —— 已超过软告警线（{limit}）。每轮都在付费，值得回头删一批。",
               { chars, limit }),
      warn: true, ready: true,
    };
  }
  return {
    text: t("{chars} 字符（软告警线 {limit}；超了只提示，不限制）。", { chars, limit }),
    warn: false, ready: true,
  };
}
