"""工具调用 → 人话活动描述。

纯展示层，没有副作用。为什么要放服务端而不是前端：

1. 落盘的 `data/events/*.jsonl` 因此自带可读性 —— 事后翻日志能直接看懂
   agent 在做什么，不必反查工具名。
2. 后续阶段的 TUI / IM 通道复用同一张表，措辞不会漂移。

没有命名冲突的风险：本模块只读工具名，不 import 任何 skill。
"""

from __future__ import annotations

import os
from typing import Any
from core.i18n import t as tr

# ------------------------------------------------------------------
# 参数 → 展示用的短标识
# ------------------------------------------------------------------

#: 按优先级取第一个命中的字段。覆盖了本仓工具的参数命名习惯。
#:
#: `output_name` 必须在表里：`create_document` / `export_document`
#: 用它命名产物。缺了它，界面上最重要的那一步只显示
#: "正在新建文档"、没有文件名（事件日志里 `"arg_hint": ""`），
#: 而旁边每个读文件都带文件名。它放在 `filename` 之后：产出类工具里
#: 产物名比来源名更该露出来。
_ARG_KEYS: tuple[str, ...] = (
    "file_path", "path", "filename", "output_name", "file", "file_hint",
    "doc_path", "source", "target", "workbook",
    "query", "keyword", "pattern", "title", "name", "email_id",
)

#: 这些键的值是长文本（整篇文档内容），不适合拿来当标识。
_SKIP_KEYS: frozenset[str] = frozenset({
    "content", "text", "old_string", "new_string", "markdown", "body",
})


def arg_hint(args: dict | None, *, max_len: int = 32) -> str:
    """从工具参数里取一个人类可读的短标识（文件名 / 查询词）。

    只取 basename —— 完整路径在气泡里读起来太吵，而且会泄漏用户目录结构。
    """
    if not isinstance(args, dict):
        return ""
    for key in _ARG_KEYS:
        if key in _SKIP_KEYS:
            continue
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            s = val.strip().replace("\\", "/")
            s = s.rsplit("/", 1)[-1]          # basename
            if not s:
                continue
            return s if len(s) <= max_len else s[:max_len] + "…"
        # 有些工具把路径放在列表里
        if isinstance(val, list) and val and isinstance(val[0], str):
            return arg_hint({key: val[0]}, max_len=max_len)
    return ""


#: 步骤流里那一行可读的参数（今天只有 `run_command` 有）。
#:
#: 与 `arg_hint` 不是一回事：`arg_hint` 取 basename（`a/b/c.py` → `c.py`），
#:   那是给"读 / 写某个文件"那类工具用的。而命令要的是原文 ——
#:   「正在执行命令后面那一行空隙很大，把指令内容压成一行显示」。
#:
#: 为什么以前刻意不给：`arg_hint` 会把 `rm -rf workspace/报告.docx`
#:   渲染成「正在执行命令 报告.docx」—— 把危险的那半藏起来，比不显示更坏。
#:这一格给的是整条命令（只折叠空白、超长截断），
#:     界面单行省略号 + 悬停看全文；不做"从中挑一个像文件名的片段"。
_SUB_HINT_KEYS: dict[str, str] = {"run_command": "command"}


def sub_hint(name: str, args: dict | None, *, max_len: int = 300) -> str:
    """步骤下面那行小字（工具参数的原文形态）。没有就返回空串。"""
    key = _SUB_HINT_KEYS.get(str(name or ""))
    if not key or not isinstance(args, dict):
        return ""
    val = args.get(key)
    if not isinstance(val, str):
        return ""
    s = " ".join(val.split())                 # 换行 / 多空格，一行
    if not s:
        return ""
    return s if len(s) <= max_len else s[:max_len] + "…"


# ------------------------------------------------------------------
# 工具名 → 活动描述模板
# ------------------------------------------------------------------

#: `{arg}` 会被 :func:`arg_hint` 的结果替换；没有 `{arg}` 的模板原样输出。
#: 措辞刻意保持"正在…"的进行时 —— 它描述的是此刻，不是已完成。
_VERBS: dict[str, str] = {
    # 查找
    "list_workspace_files":      "正在查看工作区",
    "list_available_documents":  "正在查看可用文档",
    "list_folders":              "正在查看目录",
    # `search_tools` 若落进 `_PREFIX_VERBS` 的 `("search_", "正在查找 {arg}")`，
    # 就会与 `search_chunks` 渲染成逐字相同的一行 —— 从界面上分不清模型是在
    # 找工具还是在找文档内容（两种调用经常交错出现）。
    "search_tools":              "正在查找可用工具 {arg}",
    "search_chunks":             "正在查找 {arg}",
    "search_and_replace_text":   "正在替换 {arg} 中的文本",
    "request_cross_zone_search": "正在跨区查找 {arg}",
    "search_emails":             "正在搜索邮件",
    "search_statements":         "正在查找记录",
    "search_song":               "正在搜索歌曲",
    # 刻意不带 `{arg}`。带上它会走 `arg_hint`，
    # 而那个函数取 basename —— `rm -rf workspace/报告.docx` 会渲染成
    # "正在执行命令 报告.docx"，把危险的那半藏起来。
    # 一条会误导人的活动行比一条笼统的活动行差得多：活动行是给人扫一眼的，
    # 命令原文该读的地方是审批面板和审计日志，不是这里。
    "run_command":               "正在执行命令",
    # 接管。不给 `{arg}` —— 参数是 action 枚举 +
    #   窗口 id + 文本，`arg_hint` 挑到 `text` 就把用户输入的内容糊在活动行上。
    "computer":                  "正在操作电脑",
    # 子 agent。活动行要给用户一个可等的印象 —— 它可能跑几十秒，
    # 而这段时间父这一轮是停住的（串行）。
    "run_subagent":              "正在派子 agent 处理子任务",
    # 工作流 —— 与"派一个子"分开写。
    "run_workflow":              "正在编排一组子任务",
    # 群成员派活。与上面那条分开写 ——
    # 两件事在界面上必须分得开（一个是"派了个没名字的子"，一个是"叫了群里的人"）。
    # 不给 `{arg}`：参数是 `name`/`task`，`task` 可能是一整句话
    # （同 `run_subagent`：那段话在工具回执里，不该糊在活动行上）。
    "ask_member":                "正在找群里的成员",
    # 任务清单。不给 `{arg}` —— 参数是 `todos`（一个数组），
    # `arg_hint` 取不出一个能看的短标识（`content` 还在 `_SKIP_KEYS` 里，
    # 那是对的：整份清单不该出现在活动行上）。件数在工具回执里，不在这里。
    "todo_write":                "正在更新任务清单",
    # 后台任务：查 / 等 / 取消。参数是 task_id + action 枚举，不给 `{arg}`
    # （task_id 对人没有意义，action 是枚举 —— 同 `computer` 那条判据）。
    "task":                      "正在查看后台任务",
    # 上下文压缩的请求口。没有参数，不给 `{arg}`；活动行只说"正在做什么"。
    "request_compaction":        "正在压缩上下文",
    # 周期任务三件。三行都要有 —— 与上面那五件同一条
    # 判据（家族不逐个给条目就会渲染成同一行，界面上分不出它在做什么）。
    # 不给 `{arg}`：`cron_create` 的参数是 `title`/`prompt`/`rule`，
    # `arg_hint` 挑出来的那一格不稳定（挑到 `rule` 就是一串 JSON 糊在活动行上）。
    "cron_create":               "正在建定时任务",
    "cron_list":                 "正在查看定时任务",
    "cron_delete":               "正在删除定时任务",
    # 提问面板。不给 `{arg}` —— 参数是
    # `question` + 一个选项数组，糊在活动行上会很长（正文在浮窗里）。
    "ask_user":                  "正在等你选一下",
    # 文件原语五件套。五个都要有自己的一行 ——
    # 上面 `parse_*` 那段的判据：家族不逐个给条目就会渲染成同一行，
    # 而界面上分不出模型到底在做什么（换过工具也看不出来）。
    # 这五件尤其会连续交错出现（读 → 找 → 搜 → 改 → 写），
    # 一行笼统的"正在处理文件"等于把整条链路糊成一格。
    # 带 `{arg}` 的都走 `arg_hint`（取 basename）—— 路径会被截成文件名，
    # 这是刻意的：活动行是给人扫一眼的，完整路径在工具回执和审计里。
    "read_file":                 "正在读取 {arg}",
    "write_file":                "正在写入 {arg}",
    "edit_file":                 "正在修改 {arg}",
    "glob":                      "正在按模式查找文件 {arg}",
    "grep":                      "正在搜索文件内容 {arg}",
    "code_intel":                "正在查代码符号 {arg}",
    "run_program":               "正在运行工具编排脚本",
    # 造 skill。刻意带 `{arg}` —— 它取的是 `name`
    # （一个短标识），而"它在造哪个 skill"正是用户最想看到的那一格。
    "create_skill":              "正在制作技能 {arg}",
    # 记忆三件。读/搜带 `{arg}`（`name`/`query` 都是短串，
    # `arg_hint` 取得到）；写件不带 —— 参数是 `content`（长正文），
    # 同 `todo_write` 那条判据：活动行是给人扫一眼的，不是放正文的地方。
    "memory_read":               "正在读取记忆 {arg}",
    "memory_search":             "正在检索记忆 {arg}",
    "memory_delete":             "正在删除一条记忆 {arg}",
    "memory_write":              "正在写入记忆",
    # 查历史。带 `{arg}`（`query` 是短串）。
    "history_search":            "正在检索对话历史 {arg}",
    # 把受保护的值读给模型看。
    #   措辞要说清它做了什么：这是一次「把真实内容取出来」，
    #   不是"正在计算"之类的中性词。
    "pii_reveal":                "正在读取受保护的值 {arg}",
    # 计划模式两件。两行分开写（判据同文件原语五件）：
    # "进了计划模式"与"申请批准"是两件不同的事，渲染成同一行就分不出
    # 模型是在规划还是在等你点头 —— 而"在等你点头"正是用户最需要看见的那一格。
    # 两件都不带 `{arg}`：它们的参数是空的（计划从磁盘读，见
    # `core/plan.py` 的工具注释）—— 带 `{arg}` 会渲染出一个空括号或 `{arg}` 字面量。
    "enter_plan_mode":             "正在进入计划模式",
    "exit_plan_mode":              "正在申请批准计划",

    # 读取 / 解析
    # `parse_*` 家族必须逐个给条目：只落进 `("parse_", "正在读取 {arg}")`
    # 会让 6 个工具渲染成同一行（例如模型先用 `parse_txt` 读 PDF
    # 拿到乱码、再换 `parse_pdf` 重读 —— 界面上那两行长得一模一样，看不出它换过工具）。
    # 顺带：无参调用 `describe_tool(name)` 时 `{arg}` 会被剥掉，兜底模板只剩下
    # "正在读取" 三个字，对 `search_tools` 的中文打分面零贡献。
    # 逐个给条目同时补上了「解析 / PDF / Excel / 表格 / 幻灯片」这些真实检索词。
    "parse_pdf":                 "正在解析 PDF {arg}",
    "parse_docx":                "正在解析 Word 文档 {arg}",
    "parse_txt":                 "正在读取文本文件 {arg}",
    "parse_csv":                 "正在解析 CSV 表格 {arg}",
    "parse_xlsx":                "正在解析 Excel 表格 {arg}",
    "parse_pptx":                "正在解析 PPT 幻灯片 {arg}",
    "get_document_text":         "正在读取 {arg}",
    "get_document_outline":      "正在梳理 {arg} 的结构",
    "get_document_info":         "正在查看 {arg} 的信息",
    "get_workbook_metadata":     "正在查看 {arg} 的结构",
    # 措辞里带上「表格/单元格/跨表/工作表」—— 它同时是中文检索面
    #   （`search_tools` 按它 +2/词）。模型说「读一下这个表格 /
    #   9 张表对比」时要搜得到。
    "read_excel_data":           "正在读取 {arg} 的表格数据（单元格 / 区域 / 多张工作表对比）",
    "extract_document_structure": "正在解析 {arg} 的结构",
    "extract_archive":           "正在解压 {arg}",

    # 撰写
    "create_document":           "正在新建文档",
    "add_block":                 "正在撰写内容",
    "add_word_heading":          "正在撰写标题",
    "add_word_paragraph":        "正在撰写段落",
    "add_footnote":              "正在添加脚注",
    "format_paragraph_text":     "正在调整段落格式",
    "format_word_table":         "正在调整表格格式",

    # PPT
    # 措辞兼顾显示与检索 —— 这张表也参与
    # `search_tools` 的打分（工具移出常驻后，"中文查得到"成了硬要求）。
    # "幻灯片" 是用户/模型会用的词，"ppt" 是工具名里的词，两个都要有。
    "add_slide":                 "正在加一页幻灯片（ppt）",
    # 补目的词 —— 只留"版面"这个机制词时，
    #   「做一份 PPT」「生成演示文稿」这类目的词查询够不着它
    #   （那两条查询的前 4 名里一个制作工具都没有）。
    #   但改这一行掰不动排序 —— 名字片段 8 分 vs 描述/活动行各 2 分，
    #   `set_ppt_*` 天生赢 4 倍。它买的是"至少能被搜到"，不是"排第一"。
    #   真正让模型接上的是工具描述里那句"我不生成"。
    "create_svg_slide":          "正在绘制 PPT 版面（演示文稿）",
    "set_slide_design":          "正在设计 PPT 版面",
    "svgs_to_pptx":              "正在合成 PPT 演示文稿",
    "get_design_context":        "正在准备设计素材",

    # 表格 / Excel
    "create_excel_table":        "正在创建表格",
    "create_excel_chart":        "正在生成图表",
    # 这一批 11 件一个前缀兜底都落不到
    #   （`set_excel_` / `set_word_` / `set_ppt_` / `add_word_` / `crop_ppt_` /
    #    `add_ppt_` 全不在 `_PREFIX_VERBS` 里），不给精确条目就会掉到第四级
    #   把注册表里的英文摘要当活动描述（同一形状本文件已出现两次）。
    #   `crop_ppt_picture` 连 `add_` / `set_` 都没有，是这一批里唯一
    #   彻底没有前缀的一个（`test_activity_hints` 会盯这一类）。
    #   措辞同时服务显示与中文检索：打印/页眉/母版/音频这些词都要能命中。
    "set_excel_print_setup":     "正在设置打印选项",
    "set_excel_outline":         "正在设置分级显示",
    "set_word_page_setup":       "正在设置页面",
    "add_word_section":          "正在插入分节符",
    "set_word_header_footer":    "正在设置页眉页脚",
    "set_document_properties":   "正在设置文档属性",
    # 题注 + 交叉引用（手写 SEQ/REF 域）—— 见 `word_tools.py` 那段 docstring
    "add_word_caption":          "正在插入题注 {arg}",
    "add_word_cross_reference":  "正在插入交叉引用 {arg}",
    # 把已有的手写编号换成题注域。
    #   刻意不带 `{arg}` —— 它取的是文件名，而这里真正要找的是
    #   「题注 / 编号 / 替换 / 图目录」这几个中文词（`search_tools` 的中文面）。
    "replace_manual_captions":   "正在把图表的编号替换成题注",
    "set_ppt_background":        "正在设置幻灯片背景 {arg}",
    "set_ppt_background_graphics": "正在设置幻灯片背景图形",
    "crop_ppt_picture":          "正在裁剪幻灯片图片 {arg}",
    "add_ppt_audio":             "正在插入音频 {arg}",
    "set_ppt_master_text_style": "正在设置母版文字样式",
    # 读侧两件 —— "重排用户给的那份 pptx"缺的入口
    "extract_pptx_identity":     "正在读取 PPT 的配色与字体",
    "pptx_to_outline":           "正在摊开 PPT 的逐页内容",
    "create_excel_pivot":        "正在生成透视表",
    "format_excel_range":        "正在设置单元格格式",

    # 产出 / 交付
    # 只写「正在导出 {arg}」时，中文面只有"导出"一个词（+2 分），
    # 拼不过同查询里名字命中的工具，「把文档导出成 docx 文件」搜不到它。
    # 补上描述里本来就有的话（文档 / 文件），不另造同义词。
    "export_document":           "正在把文档导出成文件 {arg}",
    "deliver_file":              "正在交付 {arg}",
    "summarize_document":        "正在总结 {arg}",
    "summarize_emails":          "正在总结邮件",
    "merge_documents":           "正在合并文档",
    "merge_pdfs":                "正在合并 PDF",
    "copy_document":             "正在复制文档",
    "copy_to_workspace":         "正在复制文件到工作区",
    "delete_files":              "正在删除文件",
    "send_email":                "正在发送邮件",
    "send_outbound":             "正在往其它会话发送消息",
    "get_email":                 "正在收取邮件",

    # 联网
    "web_search":                "正在联网搜索 {arg}",
    # GitHub（原生集成）
    # 每个工具一行：读与写要分得开（"正在看 issue"与"正在建 issue"
    # 是两件事，合成一行就看不出它在动你的仓库）。
    "github_whoami":             "正在确认 GitHub 身份",
    "github_get_file":           "正在读 GitHub 上的文件 {arg}",
    "github_search_code":        "正在 GitHub 上搜索代码 {arg}",
    "github_list_issues":        "正在列 GitHub issue {arg}",
    "github_get_issue":          "正在看 GitHub issue {arg}",
    "github_create_issue":       "正在新建 GitHub issue {arg}",
    "github_list_pulls":         "正在列 GitHub 拉取请求 {arg}",
    "github_get_pull":           "正在看 GitHub 拉取请求 {arg}",
    "github_create_pull":        "正在新建 GitHub 拉取请求 {arg}",
    "github_list_commits":       "正在看 GitHub 提交记录 {arg}",
    "web_fetch":                 "正在读取网页 {arg}",

    # 其他
    "get_weather":               "正在查询天气",
    "get_forecast":              "正在查询天气",

    # 兜底缺口补齐
    # 这一批若三级回退全部落空，`describe_tool` 就掉到第四级 ——
    # 直接把注册表里的英文摘要当活动描述。（`scripts/check_skill_reach.py` +
    # 一次真实运行记录）：20 个工具在活动行里显示成
    #     [READ] Extract PDF metadata and structur…
    #     [ADMIN ] 向单元格应用 Excel 公式（如 SUM/AVERAGE…
    # 两个后果，同一个根因：
    #   1. 界面上是一行英文（新长框里这些行是主要内容）
    #   2. 中文检索面为零，`search_tools` 的 jieba 分词永远匹配不上英文，
    #      描述里没有中文的那些工具，搜中文就永远搜不到 —— 就是这一形状。
    # 措辞就照各自注册表描述里本来就有的中文取词，不另造同义词。
    "pdf_info":                  "正在读取 PDF 信息 {arg}",
    "split_pdf":                 "正在拆分 PDF {arg}",
    # PDF 页面操作族 + 图片族
    # 这 11 件一个前缀兜底都落不到（`pdf_` 不在 `_PREFIX_VERBS` 里，
    # `compress_`/`resize_`/`rotate_` 也没有），不给精确条目就会掉到第四级
    # 把注册表里的英文摘要当活动描述（同一形状见上一段）。
    # 措辞同时服务显示与检索：中文查询里的"图片/PDF/水印/压缩"都要能命中。
    "images_to_pdf":             "正在把图片合成 PDF",
    "pdf_to_images":             "正在把 PDF 转成图片",
    "rotate_pdf_pages":          "正在旋转 PDF 页面 {arg}",
    "reorder_pdf_pages":         "正在重排 PDF 页面 {arg}",
    "pdf_watermark":             "正在给 PDF 加水印 {arg}",
    "compress_pdf":              "正在压缩 PDF {arg}",
    "encrypt_pdf":               "正在给 PDF 加密 {arg}",
    "compress_image":            "正在压缩图片 {arg}",
    "resize_image":              "正在缩放图片 {arg}",
    "convert_image_format":      "正在转换图片格式 {arg}",
    "rotate_image":              "正在旋转图片 {arg}",
    "convert_from_pdf":          "正在把 PDF 转成 Office 文档 {arg}",
    "convert_to_pdf":            "正在把文档转成 PDF {arg}",
    "write_excel_data":          "正在写入 Excel 数据 {arg}",
    "insert_excel_rows":         "正在插入行",
    "insert_excel_columns":      "正在插入列",
    "unmerge_excel_cells":       "正在取消合并单元格",
    "rename_worksheet":          "正在重命名工作表 {arg}",
    "apply_formula":             "正在应用 Excel 公式",
    "validate_formula_syntax":   "正在校验公式语法",
    "auto_fit_table":            "正在自适应表格列宽",
    "highlight_table_header":    "正在高亮表头",
    "apply_table_alternating_rows": "正在设置表格斑马纹",
    # 这一行同时收图目录 / 表目录（`TOC \c` 域）——
    #   中文词面写全，否则「插入图目录」这句话在 `search_tools` 里落不到它头上。
    "apply_table_of_contents":   "正在插入目录 / 图目录 / 表目录",
    "insert_content_near_text":  "正在插入段落 {arg}",
    "record_note":               "正在记录日程 {arg}",
    "mark_done":                 "正在标记日程完成",
    "mock_image":                "正在生成占位图片",
    # Excel 读侧查询三件
    # 措辞同时是检索面（`search_tools` 按它 +2/词）。三件都一个前缀都落不到
    #   （`profile_`/`find_`/`get_excel_` 不在 `_PREFIX_VERBS`），而且彼此同族不同事
    #必须精确条目，否则界面分不出在做什么、模型也搜不准。
    # 词面照用户会怎么问写，不是照工具名翻译：
    #   模型/用户说的是「这张表什么规律 / 哪列是什么分布」「这个值在哪」「表里有没有备注」。
    "profile_excel_data":   "正在分析 {arg} 的表格（每列分布 / 统计 / 多少行 / 唯一值）",
    "find_excel_value":     "正在表格里查找 {arg}（按值定位第几行）",
    "get_excel_comments":   "正在读 Excel 的批注备注（{arg}）",

    # Excel 写侧九件
    # 同一条判据：这些措辞同时是中文检索面。前三行按"用户会怎么问"写 ——
    # 用户说的是「排一下序」「把不及格的挑出来」「按编号去重」「导成 csv」
    # 「画个价格走势图」，而不是工具名。
    "sort_excel_range":             "正在给表格排序（{arg}）",
    "filter_excel_data":            "正在筛选表格数据（挑出符合条件的行 / 挂筛选器）",
    "remove_excel_duplicates":      "正在按列删除表格里的重复行",
    "protect_excel_sheet":          "正在给工作表加保护（锁定 / 加密）",
    "set_excel_hyperlink":          "正在给单元格加超链接",
    "export_excel_to_csv":          "正在把表格导出成 CSV 文件",
    "merge_excel_workbooks":        "正在合并多个 Excel 工作簿",
    "recalculate_excel_formulas":   "正在重算 Excel 公式（LibreOffice）",
    "plot_excel_data":              "正在给表格画图（走势图 / 折线 / 柱状 / 饼图）",

    # 生图
    # 措辞同时是中文检索面：用户会说的是「画一张」「生成图片」「画个 logo」，
    #   而这些词在英文描述里一个都没有，漏了这行，工具就在、但按真实话术搜不到。
    "generate_image":               "正在生成图片（AI 画图 / 文生图）",

    # Excel 收尾三件 + Word 表格行列四件
    # 同样一个前缀都落不到（`freeze_`/`delete_table_` 不在 `_PREFIX_VERBS`；
    # `add_table_` 会落到 `("add_", "正在编辑 {arg}")`，而四件里两件都是
    # 表格行列、同族不同事，还是要精确条目，否则界面上分不出在做什么）。
    "freeze_excel_panes":            "正在冻结窗格",
    # 这一行的措辞同时是检索面（`search_tools` 的 `cn_l` 命中给 +2）：
    # 只写「正在设置条件格式」时，「分数大于60的标红」搜不到它
    # —— 用户真会打的词是「大于 / 标红」，而那两个词在英文描述与这行里都没有。
    # 这是「工具在，但按真实话术找不到」的典型形状，
    # 所以补的是用户真会打的词，不是同义词表的发明。
    "set_excel_conditional_format":  "正在设置条件格式（大于/小于某个数就标红标绿）",
    "add_excel_data_validation":     "正在添加数据验证",
    "add_table_rows":                "正在给表格加行",
    "add_table_columns":             "正在给表格加列",
    "delete_table_rows":             "正在删除表格行",
    "delete_table_columns":          "正在删除表格列",
}

# ------------------------------------------------------------------
# 检索面 —— 只给检索用，一个字都不渲染
# ------------------------------------------------------------------
#
# ## 为什么它和 `_VERBS` 是两张表（而不是把词塞进 `_VERBS` 的句子里）
#
# `_VERBS` 那句会显示给用户（活动行 + `data/events/*.jsonl`），
# 所以它必须是一句人话；而检索需要的是用户自己会打的口语词——
# 「居中」「标红」「去重」「下拉列表」。两者塞成一句 = 活动行变成词串。
#
# 这个拆法照的是一个通用做法：每个工具一个可选的检索提示
# （策展过的能力短语），打分权重 4，而 description 只有 2；
# 并且明文不渲染 —— 判据：检索提示只参与打分，
# 不进任何用户可见的文本（A/B 实际无收益）。
#
# 它不是「同义词表」 —— 那张表是把 A 词映射到 B 词，需要一直有人维护；
#   这里没有映射，是这个工具自己会做的事、用用户的话说一遍。
#   而打分的底仍然是 `_VERBS`（权重 2）：没写 hint 的工具一分不少。
#
# ## 什么时候要写
#
# 当 `_VERBS` 那句话换一种说法就说不到时。反例：
# `format_excel_range` 的活动行是「正在设置单元格格式」，而用户会说
# 「把这列居中」—— `居中` 这两个字在中文面和英文面里都不存在，
# 于是它掉到 93 / 158 名（`search_tools` 只回前 5）。
#判据：写完拿用户的真话术量一次名次，别照着自己想的词填。
#
# 只修"那一条原话"是不够的。
# 修「A 到 Y 列居中」之后，同一能力换个说法又会掉出去。
# （`limit=5` 才看得见，所以名次 > 5 == 不可达）：
#   · `A 到 Y 列居中` 1 · `给这列加个边框` 1 · `设置单元格底色` 1
#   · `把这一列居中` 8  ✗ · `把列宽调大一点` 38 ✗
# 两个各要一半的病因（都不是这条提示"写短了"）：
#   ① 子串命中：`一列` 这两字命中了 `sort_excel_range` 提示里的
#      「按某一列从高到低」，+4，把真正的能力词 `居中`(+4) 挤到并列第 5 名开外。
#   ② jieba 把「列宽」切成 `列`+`宽`，提示里写的 `列宽` 永远匹配不上，
#      而 `单字兜底` 只看 desc/cn、不看 hint，这一条提示对它是全盲的
#      （实际总分 2，而那 2 分来自 `大`/`一` 撞上 description 的噪声）。
#这张表要治 ②：补上用户真会打的拆分说法（列宽调大调小 · 把这一列调宽调窄 ·
#   行高调高调低 · 字号调大调小 · 把这一列居中）。实际 8 条话术 7 条第 1、2 条第 2
#   （全部进前 5）。①（子串命中 + CJK 单字噪声地板）不归这张表管。
_SEARCH_HINTS: dict[str, str] = {
    "task":
        "查看后台任务、查询任务状态 / 进度、等它跑完、取消后台任务、读任务输出",

    # Excel：写侧 / 分析侧（检索缺口最先暴露的一族）
    "format_excel_range":
        "把表格的单元格区域设置成居中、把这一列居中、水平/垂直对齐、加边框、"
        "填底色背景色、改字体字号颜色、字号调大调小、调行高列宽、"
        "把这一列调宽调窄、列宽调大调小、行高调高调低、"
        "设数字格式（小数位数 / 百分比 / 日期 / 货币）",
    "sort_excel_range":
        "给表格排序：按某一列从高到低 / 从低到高、按成绩排名、按日期先后排",
    "filter_excel_data":
        "筛选表格：只显示符合条件的行、把大于/小于某个数的挑出来、挂筛选器",
    "remove_excel_duplicates":
        "去重：把表格里重复的行删掉、按某一列去重",
    "create_excel_pivot":
        "做数据透视表、按分类汇总、分组统计、求和计数",
    "create_excel_chart":
        "生成图表：柱状图、折线图、饼图、条形图、散点图",
    "plot_excel_data":
        "给表格里的数据画图看走势：走势图、折线、柱状、饼图",
    "set_excel_conditional_format":
        "条件格式：数字大于 / 小于多少就标红标绿、数据条、色阶、突出显示重复值",
    "add_excel_data_validation":
        "给单元格加下拉列表 / 下拉选项、限制只能填某几个值、限制数字范围",
    "freeze_excel_panes":
        "冻结窗格、冻结表头，往下滚动时表头不动",
    "recalculate_excel_formulas":
        "重算表格里的公式、把公式算出的值求出来（VLOOKUP / SUM 这类）",
    "apply_formula":
        "往单元格里写公式（VLOOKUP / IF / SUMIF 这类）",
    "validate_formula_syntax":
        "检查公式写得对不对、语法有没有错",
    "merge_excel_cells":
        "把几个单元格合并成一个、合并居中",
    "unmerge_excel_cells":
        "取消合并单元格、拆开合并区",
    "merge_excel_workbooks":
        "多个 Excel 表格合并成一个 / 多张 sheet 并起来",
    "protect_excel_sheet":
        "给工作表加保护、锁定不让改、加密码",
    "set_excel_hyperlink":
        "给单元格加超链接、链到别的表或网址",
    "set_excel_outline":
        "设置分级显示、把若干行折叠成一组、展开收起",
    "set_excel_print_setup":
        "设置打印区域、页边距、纸张方向、打印标题行",
    "export_excel_to_csv":
        "导出成 CSV / 逗号分隔的文本",
    "insert_excel_rows":
        "在表格里插入一行 / 几行",
    "insert_excel_columns":
        "在表格里插入一列 / 几列",
    "delete_excel_rows":
        "删掉表格里的某几行",
    "delete_excel_columns":
        "删掉表格里的某一列 / 几列",
    "delete_excel_range":
        "删掉一块单元格区域并让后面的格子补上来",
    "copy_excel_range":
        "复制一块单元格区域（连格式一起）",
    "copy_worksheet":
        "复制工作表、复制一个 sheet",
    "delete_worksheet":
        "删掉一张工作表 / sheet",
    "rename_worksheet":
        "给工作表 / sheet 改名",
    "create_excel_table":
        "把一片区域变成 Excel 表格、套用表格样式（带筛选箭头的那种）",
    "write_excel_data":
        "往 Excel 单元格 / 区域里填数据、写数字和文字",
    "read_excel_data":
        "读取 Excel 表格里的单元格数据、看看有哪些工作表、几行几列、跨表对比",
    "profile_excel_data":
        "看看这张表有几行几列、每一列都是什么、最大最小、有多少种取值",
    "find_excel_value":
        "在表格里找某个值在第几行第几列、定位到它",
    "get_excel_comments":
        "读 Excel 单元格上的批注 / 备注",
    # Word / PPT：同一族的写法（表格行列、页眉页脚、页码）
    "add_table_rows":      "给 Word 表格加一行 / 几行",
    "add_table_columns":   "给 Word 表格加一列 / 几列",
    "delete_table_rows":   "删掉 Word 表格里的某几行",
    "delete_table_columns": "删掉 Word 表格里的某一列 / 几列",
    "highlight_table_header": "把表格的表头高亮 / 加底色",
    "auto_fit_table":      "让表格列宽自适应、把列宽调合适",
    # PDF / 文件那一族
    #
    # 这一族若一条 hint 都没有，而它们全是"名字里带 `pdf` / `file`"的
    # 同族工具，打分的名字段（+8）把它们一起顶到并列 13，
    # 谁排前面全靠字母序。实际「把这个 pdf 拆开」7 个 pdf 工具并列，
    # `split_pdf` 排第 6（`limit=5`，不可达）。
    #
    # 而收敛它们的只能是这里：`合并` / `拆分` 写在 description 里，
    # 用户说的是 `合成` / `拆开` —— 要弥合这个缺口就得有一张同义词表，
    # 而那正是本仓明确不建的第二套真相源（见 `bus/registry.py` 那段）。
    #   人工看着工具写一句"用户会怎么说"，就是这张表本来就有的职责。
    #
    # 打分面的两处噪声（整词匹配 + 区分力闸，见 `bus/registry.py`）
    # 修好之后，这张表补的是露出来的真缺口 —— 它们能通过检索电池，
    # 靠的是单字噪声给的并列断，不是真信号。
    "parse_pdf":  "读一下这个 pdf、解析 pdf 里的文字和表格、看看 pdf 写了什么",
    "split_pdf":  "把这个 pdf 拆开、按页拆分、每几页切成一个文件",
    "merge_pdfs": "把几个 pdf 合成一个、合并 pdf、把 pdf 拼起来",
    "pdf_to_images": "把 pdf 的每一页导成图片文件、pdf 拆成一张张图",
    "images_to_pdf": "把几张图片做成一个 pdf、图片合成 pdf",
    "convert_to_pdf": "把 word / excel / ppt 转成 pdf",
    "convert_from_pdf": "把 pdf 转成 word 或 excel",
    "compress_pdf": "把 pdf 压小一点、减小 pdf 的体积",
    "encrypt_pdf": "给 pdf 加密码、把 pdf 加密",
    "parse_csv":  "解析 csv 表格、读一下这个 csv 里有什么",
    "extract_archive": "解压这个压缩包、把 zip / rar / 7z 解开",
    "copy_to_workspace": "把文件复制到工作区、从桌面或下载目录拿文件进来",
    "export_document": "把文档保存成文件、导出成 docx 或 pdf",
    "add_slide":  "加一页幻灯片、新增一页 ppt",
    "read_file":  "读一下这个文件的内容、把文件打开看看",
    # 邮件那一族 —— 同 `pdf` 那族：这张表里原本没有它们的 hint
    "search_emails": "看看最近的邮件、搜索邮件、按发件人 / 关键词 / 日期找邮件",
    "list_folders": "看看邮箱里有哪些文件夹、收件箱 / 发件箱目录",
    "get_email":  "打开这封邮件、看邮件的完整正文",
    "summarize_emails": "把最近的邮件总结一下、邮件摘要",
    "send_email": "发一封邮件、给某人写封信",
    # ── 图片那一族（同上）—— `pdf_to_images` 的 hint 写"转成图片"会抢
    #    「把图片转成 jpg」这类查询，所以那一族两个方向都要写清各自的动词 ──
    "convert_image_format": "把图片转成 jpg / png、改图片的格式",
    "compress_image": "把图片压小一点、减小图片体积",
    "resize_image": "改图片的尺寸、把图缩放成多大",
    "rotate_image": "把图片旋转、转正过来",
    # 工作流
    "run_workflow": "把大任务拆成几步并行做、分头查多个文件 / 多个角度、"
                    "交叉核对、汇总多份结论",
    # 提问
    "ask_user": "让用户选一下、问用户要哪个方案、让用户决定、"
                "给几个选项让人选、需要用户决定",
}


def search_hint(name: str) -> str:
    """返回工具的检索用能力短语（没有就空串）。

    唯一读者是 `bus/registry.py::search_tools` 的打分面。
    它绝不进任何给用户看的字符串 —— 判据见 `_SEARCH_HINTS` 那段。
    任何一级都不抛异常（检索面的失败不该让工具发现整个不能用）。
    """
    if not name:
        return ""
    try:
        return _SEARCH_HINTS.get(name, "")
    except Exception:
        return ""


#: 前缀族的兜底模板 —— 表里没有精确命中时按前缀匹配。
#: 顺序重要：更具体的前缀在前。
_PREFIX_VERBS: tuple[tuple[str, str], ...] = (
    ("add_word_",     "正在撰写内容"),
    ("add_",          "正在编辑 {arg}"),
    ("set_table_",    "正在调整表格"),
    ("set_",          "正在设置 {arg}"),
    ("format_",       "正在调整格式"),
    ("delete_",       "正在删除 {arg}"),
    ("create_",       "正在创建 {arg}"),
    ("copy_",         "正在复制 {arg}"),
    ("merge_",        "正在合并 {arg}"),
    ("parse_",        "正在读取 {arg}"),
    ("get_",          "正在查看 {arg}"),
    ("list_",         "正在列出 {arg}"),
    ("extract_",      "正在提取 {arg}"),
    ("search_",       "正在查找 {arg}"),
    ("summarize_",    "正在总结 {arg}"),
    ("export_",       "正在导出 {arg}"),
)


def describe_tool(name: str, args: dict | None = None) -> str:
    """返回一句中文活动描述。

    三级回退：精确表 → 前缀族 → 工具注册表的一行描述 → 裸工具名。
    任何一级都不抛异常 —— 活动描述失败不该影响工具执行。
    """
    if not name:
        return tr("正在处理")

    # 接管：若四个动作都渲染成同一句
    #   「正在操作电脑」，用户分不出它是在看还是在点。
    #按 `action` 枚举分；不许把参数原文（text / key / 窗口标题）
    #   糊上来：那是用户内容，活动行不是放正文的地方（同 `run_command` 那条）。
    if name == "computer":
        act = str((args or {}).get("action") or "")
        verbs = {
            "list_windows": tr("列出窗口"),
            "get_window_state": tr("查看窗口"),
            "click": tr("点击"),
            "type_text": tr("输入文字"),
            "press_key": tr("按键"),
            "scroll": tr("滚动"),
            "drag": tr("拖拽"),
            "set_value": tr("写入内容"),
            "activate_window": tr("切换窗口"),
            "launch_app": tr("启动应用"),
        }
        return (tr("正在操作电脑（%s）") % verbs[act]) if act in verbs else tr("正在操作电脑")

    # 浏览器接管 —— 同样按 action 分句。
    if name == "browser":
        act = str((args or {}).get("action") or "")
        verbs = {
            "list_tabs": tr("列出标签页"),
            "open_tab": tr("打开标签页"),
            "switch_tab": tr("切换标签页"),
            "navigate": tr("导航"),
            "snapshot": tr("查看页面"),
            "click": tr("点击页面"),
            "type_text": tr("输入文字"),
            "press_key": tr("按键"),
            "scroll": tr("滚动页面"),
            "screenshot": tr("截图"),
            "wait": tr("等待加载"),
        }
        return (tr("正在操作浏览器（%s）") % verbs[act]) if act in verbs else tr("正在操作浏览器")

    template = _VERBS.get(name)
    if template is None:
        for prefix, tmpl in _PREFIX_VERBS:
            if name.startswith(prefix):
                template = tmpl
                break

    if template is None:
        return _fallback(name)

    # 在这里翻、不在词典里翻：`_VERBS` 是模块级数据，`tr()` 写在值里会在
    #    import 那一刻定死语言 —— 用户在设置页改成英文，命令回话立刻变、
    #    而步骤流要重启才变（同一屏两套语言）。
    #词典保持中文（它同时是检索面的措辞来源），翻译发生在取用时。
    template = tr(template)

    if "{arg}" not in template:
        return template
    hint = arg_hint(args)
    # 拿不到参数标识时去掉 "{arg}" 连同它前面的空格，避免出现"正在读取 …"
    return template.format(arg=hint) if hint else template.replace(" {arg}", "")


def _fallback(name: str) -> str:
    """从工具注册表取一行描述。取不到就退回工具名本身。"""
    try:
        from bus.registry import get_tool_summaries
        for line in get_tool_summaries():
            # 注册表的格式是 "<name>: <description>"（可能带 [READ] 等前缀）
            head, sep, tail = line.partition(":")
            if sep and head.strip().lstrip("- ").strip() == name:
                desc = tail.strip()
                if desc:
                    return desc if len(desc) <= 40 else desc[:40] + "…"
    except Exception:
        pass
    return f"正在执行 {name}"


__all__ = ["describe_tool", "arg_hint", "sub_hint", "search_hint"]
