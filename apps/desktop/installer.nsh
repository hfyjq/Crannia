; Crannia 的 NSIS 定制 —— **只有这一页**，别的什么都不动。
;
; ## 为什么允许这一个例外
;
; `electron-builder.config.mjs` 顶部写着「NSIS 深度定制整个跳过」（自绘圆角勾选框 + DWM
; + 自写解压是一整个 C++ 项目）。**这一页不是那个**：它只是在**已有的欢迎页**上
; 换一段文案，不碰解压、不碰复制、不碰安装流程。
;
; ## 它治什么（用户原话：「安装像卡住」）
;
; electron-builder 的安装分三段写（见 `scripts/patch-nsis.cjs` 抬头）：
;   ① 解压 370 MB 到 `%TEMP%`  ② 复制 1.3 GB 到安装目录  ③ 安装器自拷贝
; ★ 其中 **② 复制那一段没有逐文件进度回调**（NSIS 的 `CopyFiles` 不推进度条，
;   官方文档确认）⇒ 进度条会长时间停在原地。**那不是卡死**，但用户没法知道。
;
; ⇒ 判据：**在开始之前就把这件事说清楚**，而不是等它发生了再解释。
;   ⚠️ **不写死"3 分钟"** —— 机器差异很大（杀软实时扫描、安装到机械盘、
;   跨盘复制），写死一个数字只会让另一半人觉得被骗。只要求**不误导**。
;
; ★★ **两段文案都是多语言的**（用户 2026-10-25）：`LangString` + `$(…)`，
;   由 `electron-builder.config.mjs` 的 `multiLanguageInstaller` /
;   `installerLanguages: [zh_CN, en_US]` / `displayLanguageSelector: false`
;   决定 —— **跟随系统语言**，不弹语言选择框。
;   中文原文同时登记在 `locales/en.json`（那是**应用界面**的翻译表）；
;   改文案时两处一起改，否则安装器与界面会各说各的。
;
; 恢复办法：删掉 `electron-builder.config.mjs` 里的 `nsis.include` 即可。


; ── 两段文案的多语言声明（用户 2026-10-25）─────────────────────────────
;    ★ 中文原文同时登记在 `locales/en.json`（那边是**应用界面**的翻译表，
;      这两条是**安装器**的；两边都留一份，改文案时两处一起改）。
;    ★ `${LANG_SIMPCHINESE}` / `${LANG_ENGLISH}` 由 electron-builder 按
;      `nsis.installerLanguages` 编译进来；**跟随系统语言**（不弹选择框）。
;: ⚠️ **必须包在 `customHeader` 里**：`LangString` 只能在 `!insertmacro addLangs`
;:   （`installer.nsi:43`）**之后**声明，而本文件是**在 `installer.nsi` 之前**被处理的
;:   ⇒ 顶层写会编译失败。`customHeader` 正好在 addLangs 之后被调（`installer.nsi:45`）。
!macro customHeader
  LangString cranniaWelcomeTitle ${LANG_SIMPCHINESE} "欢迎安装 Crannia"
  LangString cranniaWelcomeTitle ${LANG_ENGLISH} "Welcome to Crannia"
  LangString cranniaWelcomeText ${LANG_SIMPCHINESE} "安装过程会涉及大量文件操作，进度缓慢时请耐心等候。"
  LangString cranniaWelcomeText ${LANG_ENGLISH} "The installation performs a large number of file operations. If it seems slow, please be patient."
  LangString cranniaUninstallData ${LANG_SIMPCHINESE} "此操作仅卸载程序本身。$\r$\n$\r$\n您的对话记录、长期记忆、模型配置与 API 凭据都在：$\r$\n$APPDATA\Crannia$\r$\n$\r$\n卸载不会移除此文件夹，如需删除请手动清理。"
  LangString cranniaUninstallData ${LANG_ENGLISH} "This only uninstalls the program itself.$\r$\n$\r$\nYour conversations, long-term memory, model settings and API credentials live in:$\r$\n$APPDATA\Crannia$\r$\n$\r$\nUninstalling does not remove this folder; delete it manually if you want to."
!macroend

!macro customWelcomePage
  !define MUI_WELCOMEPAGE_TITLE "$(cranniaWelcomeTitle)"
  !define MUI_WELCOMEPAGE_TEXT "$(cranniaWelcomeText)"
  !insertmacro MUI_PAGE_WELCOME
!macroend

; ---------------------------------------------------------------------
; 卸载前说明：程序会删掉，数据不会。
; ---------------------------------------------------------------------
;
; 数据住在 `%APPDATA%\Crannia`，`deleteAppDataOnUninstall` 没开
; （`uninstaller.nsh` 里那三条 `RMDir /r $APPDATA\…` 在开关关着时不执行），
; 所以卸载不会替用户清数据 —— 想彻底清掉得自己删那个目录。
; 说反了的文案会让用户白白备份一份，或者以为数据没了。
;
; 这段话里不许出现 Markdown 的星号：NSIS 的 MessageBox 不认 Markdown，
; `**程序本身**` 会原样连着星号显示出来。注释里可以写，正文不行。
; 只在非静默时弹：`/S` 静默卸载（自动化 / 企业部署）不该被一个模态框卡住。
!macro customUnInstall
  ${IfNot} ${Silent}
    ;: ⚠️ 只有 `MB_OK` 一个按钮 —— **文案不许写"点取消"**（那会指向一个不存在的按钮）。
    MessageBox MB_OK|MB_ICONINFORMATION "$(cranniaUninstallData)"
  ${EndIf}
!macroend
