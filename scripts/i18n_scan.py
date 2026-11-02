#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""扫出界面文案，生成翻译工作表（`locales/worksheet/*.csv`）。

    .venv/Scripts/python.exe scripts/i18n_scan.py             # 全部已登记块
    .venv/Scripts/python.exe scripts/i18n_scan.py --blocks 01-commands 02-api-messages
    .venv/Scripts/python.exe scripts/i18n_scan.py --langs en ja   # 预留多语言列

## 三条规矩（与 `locales/README.md` 一致）

1. 中文原文 = 键。表里一行 = 一句；同一句在多个位置出现，合成一行，
   `位置` 列写多个（`；` 分隔）。原文改了 = 新行，旧译文由 `i18n_apply.py` 报孤儿。
2. 白名单，不是全仓扫描。哪些函数/哪几个键算"用户面"，见 :data:`BLOCKS`。
   工具 schema、工具回执、日志、系统提示词、审批载荷一律不进来。
3. 拿不准的标 `保留=0` 并写原因（进 `skipped.csv`），不删证据。

## 它产出什么

    locales/source.json           {原文: {block, screen, where:[…], ph:[…]}}
    locales/worksheet/<块>.csv    给人填的表（UTF-8 BOM，Excel 直接开）
    locales/worksheet/00-all.csv  全量视图（只读）
    locales/skipped.csv           保留=0 的证据

输出是幂等的：同一份代码跑两次，表内容逐字相同（行按块内原文排序）。
   它不覆盖已填的译文：会先读旧表里同 `id` 行的 `译文_*` 列，原样带过来。

ASCII-only 输出（Windows 控制台 GBK）。
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
OUT = _ROOT / "locales"
WS = OUT / "worksheet"

CN = re.compile("[" + chr(0x4E00) + "-" + chr(0x9FFF) + "]")   # ASCII-only source
#: 占位符三种写法（`%s` / `%(name)s` / `{name}`）。
_PH = re.compile(r"%\([A-Za-z_]\w*\)[sdrf]|%[sdrf]|\{[A-Za-z_][\w.]*\}|\{\}")

#: 看起来像内部/开发的话 —— 标 `保留=0` 让人复核（不是自动排除）。
_SUSPECT = re.compile(
    r"(HTTP|JSON|Traceback|payload|session_key|ctx\.|None\b|\[\]|\{\}|::|"
    r"def |import |\.py\b|/api/|ws://|http://|https://)")
#: 看着像日志 / 内部标记的开头 —— 不是结论，标 0 让人复核。
_LOGISH = re.compile(r"^\s*(\[!\]|\[approval\]|\[|#|---|==)")
#: 直呼参数名的校验话术（`xxx_yyy 要是 …`）—— 那是开发者接口，不是用户文案。
_PARAMISH = re.compile(r"[a-z]{2,}_[a-z_]{2,}")
#: 以ASCII 参数名打头再跟校验动词的短句（`Key 不存在` / `effort 要给…`）。
_PARAM_SHORT = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,30}\s*(要是|要给|必须|不存在|不是)")

#: 写给模型的话（`ctx.user_message` / 工具回执）—— 界面上一个字都不出现，
#: 界面语言不该翻它（`locales/README.md §2`）。
#:
#: 为什么是名单而不是判据：它们与"回给用户的话"住在同一批函数里
#: （`_settle_*` / `exit_plan_mode`），语法上是同一种字符串 —— 判别只能靠
#: "这句话是祈使句、在给 agent 派活"。名单短、可 grep；加一句就加一行。
#: 插进正文的令牌 / 值，不是显示文案（与 `input` 那一类同族）。
#: `@全员` 是 `mention.js` 里写进输入框的 token；面板上显示的是 `t("全员")`。
_VALUE_ONLY = {"@全员"}

_MODEL_FACING = {
    "用户**不同意**把它给你 —— 这一步不要做。如果这件事非它不可，如实说明并停下，**不要绕**。",
    "用户**不同意**这份计划。请继续规划 —— 可以改计划文件，也可以问用户想怎么改。",
    "用户**同意**了，但那个句柄**已经不在**框架这边了（映射被清掉或属于别的对话）"
    "—— 所以没有值可以给你。请如实告诉用户，别猜。",
    "用户拒绝了删除请求。请告知用户文件未被删除。",
    "用户拒绝了跨区搜索请求。请告知用户你无法在工作区外查找此文件。",
    "用户拒绝了这次批量删除。请告知用户文件未被删除。",
    "用户不同意执行这一步。请告知用户它**没有被执行**，不要重试、也不要换一个工具去绕过同一个限制。",
}

#: API 契约报错 —— 前端正常路径发不出这种请求，用户见不到。
#: 它们包着 `tr()` 只是因为它和"给人看的报错"写在同一个 `jsonify` 家族里；
#: 翻了等于给一个永远不出现的句子写译文。
_DEV_FACING = {
    "要 name",
    "缺少 path",
    "要 name 和 config",
    "interface_language 只认 %s（或留空 = 跟随系统）；收到 %r",
}

# 白名单：块 = 标题 + 从哪取
#
# ① `funcs`  —— 只扫这些函数体（含嵌套）里的字符串字面量；
# ② `payload` —— 扫 dict 字面量里这些键的值（`web/server.py` 的 API 出口）；
# ③ `names`   —— 只扫这些具名常量 / 元组（如 `_COMMANDS` 的命令说明）。
BLOCKS: dict[str, dict] = {
    "01-commands": {
        "title": "命令回话",
        "screen": "聊天",
        "sources": [
            {"file": "core/pipeline/stages/pre_process.py",
             #: 这一族全部是回给用户的话（`ctx.response`）。按前缀收，
             #: 比手写名单可靠：`/model` `/change` `/remember` 的实现函数名
             #: 并不都叫 `_handle_*`（第一次手写名单就漏了三个）。
             "func_prefixes": ["_cmd_", "_handle_", "_settle_", "_switch_",
                               "_render_", "_describe_", "_validate_",
                               "_restore_", "_answer_ask", "_leave_plan_mode"],
             #: 斜杠命令的说明（`/` 候选列表里给用户看的那一句）。
             "names": ["_COMMANDS"]},
            {"file": "core/modes.py", "funcs": ["set_mode"]},
            {"file": "core/response_language.py", "funcs": ["prompt_line"]},
            #: `/compact` 没压成时那句原因由压缩器给（`last_skip_note`），
            #:  而 `pre_process` 只是把它原样回给用户 —— 所以文案的家在这里。
            #:  用 `tr_calls`：作者显式包了 `trf(...)` 就是用户面（不猜）。
            {"file": "core/memory/compressor.py", "tr_calls": True},
        ],
    },
    "02-api-messages": {
        "title": "通知 / 报错",
        "screen": "右上角 / 设置页",
        "sources": [
            {"file": "web/server.py",
             "payload": ["error", "message", "note", "hint", "reason"],
             # 凡是显式包了 `tr(...)` 的字面量就是界面文案 —— 这是定义，
             #    不是猜测。少了这一档，`GET /api/settings` 里那三个代理档名
             #    （`tr("跟随系统（默认）")` …）从来没进过表，英文界面上
             #    下拉框显示的是中文（数出来的）。
             "tr_calls": True},
        ],
    },
    # ── 下面 11 块是界面（React）。`js` 收整个文件；`js` + `js_funcs`
    #    只收某个顶层组件的区间 —— `App.jsx` 一屏里住着好几个"屏幕"：
    #    `AppInner` 聊天 · `Settings` 设置 · `ThreadTree` 左栏 · `TaskPanel` 后台任务。
    "03-settings": {
        "title": "设置页", "screen": "设置 · 对话",
        "sources": [
            {"js": "packages/ui/src/settings-view.js"},
            #: 自动模式的规则面板（允许 / 禁止 / 环境三段）。
            #:   它曾经一个块都没登记，那 12 句的英文译文在 `en.json` 里
            #:   是孤儿（下一次 `i18n_apply` 会把它们静默丢掉）。新文件不登记
            #:   没有任何东西会红 —— 这条注释就是那次现场。
            {"js": "packages/ui/src/AutoRulesPanel.jsx"},
            #: 记忆库子页（列表 / 改写 / 删除 / 封条）—— 与设置表同一屏。
            {"js": "packages/ui/src/MemoryLibrary.jsx"},
            #: 名单类子页（访问白名单 / 规避目录 / 监听目录）。
            {"js": "packages/ui/src/ListEditor.jsx"},
            {"js": "packages/ui/src/App.jsx", "js_funcs": ["Settings"]},
        ],
    },
    "04-chat": {
        "title": "聊天主界面", "screen": "聊天",
        "sources": [
            {"js": "packages/ui/src/App.jsx", "js_funcs": ["AppInner", "Message", "Boundary"]},
            {"js": "packages/ui/src/Composer.jsx"},
            {"js": "packages/ui/src/ActivityBlock.jsx"},
            {"js": "packages/ui/src/Artifacts.jsx"},
            {"js": "packages/ui/src/Markdown.jsx"},
            #: 改动文件浮窗（点「查看 X 个改动文件」里的一条）。
            {"js": "packages/ui/src/FileDiff.jsx"},
            #: 浮窗里那句 note 是后端铸的（`diff_for`），前端只是 `t(data.note)` ——
            #:   不把产者登记进来，这几句永远进不了翻译表（英文界面下是中文）。
            {"file": "core/file_history.py", "funcs": ["diff_for"]},
            {"js": "packages/ui/src/StatusBar.jsx"},
            {"js": "packages/ui/src/context.js"},
            # 步骤流（活动块）里那行「正在干什么」—— 后端生成、随事件下发。
            {"file": "core/activity.py", "names": ["_VERBS", "_PREFIX_VERBS"],
             "funcs": ["describe_tool"]},
            {"js": "packages/ui/src/user-text.js"},
            # 用户消息附件那一排 + 全屏看大图（新增文件，必须登记，
            #   否则它们的文案一个都不进表：界面上的提示会一直是中文，
            #   而"新文件忘了登记"这件事没有任何东西会红）。
            {"js": "packages/ui/src/attachments.js"},
            {"js": "packages/ui/src/Attachments.jsx"},
            {"js": "packages/ui/src/image-viewer.jsx"},
            {"js": "packages/ui/src/markdown.js"},
            # `/btw` 的浮窗。新文件不登记，它的文案一条都进不了表 ——
            #   界面上一直是中文，而这件事没有任何东西会红
            #   （2026-11-02 现场：`顺带一问` / `响应中…` 在 en.json 里有译文，
            #    但工作表里没有对应行，`i18n_apply` 会把它当孤儿删掉）。
            {"js": "packages/ui/src/BtwPanel.jsx"},
            # 压缩那几句话是后端铸的（`tr()` 包着模块常量），前端只负责显示 ——
            # 产者不登记，它们就永远进不了表，`i18n_apply` 会把已有译文当孤儿删掉。
            {"file": "core/memory/compressor.py",
             "names": ["_USER_DEGRADED", "_USER_BLOCKED", "_USER_BREAKER", "_USER_DONE"]},
        ],
    },
    "05-endpoint": {
        "title": "端配置页", "screen": "设置 · 端配置",
        "sources": [
            {"js": "packages/ui/src/Endpoint.jsx"},
            {"js": "packages/ui/src/endpoint-form.js"},
            {"js": "packages/ui/src/provider-icons.js"},
        ],
    },
    "06-schedules": {
        "title": "定时任务", "screen": "定时任务",
        "sources": [
            {"js": "packages/ui/src/SchedulesPanel.jsx"},
            # 面板那句「每天 08:30」是后端算的（`/api/cron/preview`）——
            #    它跟着界面语言走（`describe_i18n`），键也在这张表里。
            #    `describe()`（给模型看的工具回执）不翻，别把两边弄成一套。
            {"file": "core/cron/expr.py", "funcs": ["describe_i18n"],
             "names": ["_WEEK_DAYS"]},
            {"file": "core/cron/tools.py", "funcs": ["preview_rule"]},
        ],
    },
    "07-groups": {
        "title": "群聊", "screen": "群聊",
        "sources": [
            {"js": "packages/ui/src/Groups.jsx"},
            {"js": "packages/ui/src/group-form.js"},
            # `@` 面板的三句（`@全员` / `所有成员都回复` / `（未写职责）`）
            #   以前一个块都没收，英文界面里它们是中文。
            {"js": "packages/ui/src/mention.js"},
            {"js": "packages/ui/src/Avatar.jsx"},
            {"js": "packages/ui/src/avatar.js"},
        ],
    },
    "08-skills": {
        "title": "技能与连接器", "screen": "技能 · 连接器",
        "sources": [
            {"js": "packages/ui/src/Skills.jsx"},
            {"js": "packages/ui/src/skills-view.js"},
            # 单个技能的配置页（条目增删 / 默认条目 / 显示与隐藏）。
            #   漏了它，这个文件里的每一句都进不了表 —— 而英文界面照样在用它们，
            #   于是 `en.json` 里那几条永远对不上工作表（表现为"孤儿"）。
            {"js": "packages/ui/src/SkillConfig.jsx"},
            {"js": "packages/ui/src/McpPanel.jsx"},
            {"js": "packages/ui/src/Channels.jsx"},
            {"js": "packages/ui/src/channels-model.js"},
            # 技能声明的字段文案（`manifest.yaml` 的 `config.fields`）。
            #   它们不在代码里 —— 只扫 .jsx/.py 的话，这些 label / placeholder /
            #   help 永远进不了表，英文界面里一直是中文（邮箱地址 / 授权码 / 获取方式）。
            {"yaml": "skills/*/manifest.yaml"},
        ],
    },
    "09-onboarding": {
        "title": "引导 / 开屏", "screen": "开屏 · 引导",
        "sources": [
            {"js": "packages/ui/src/Onboarding.jsx"},
            {"js": "packages/ui/src/onboarding-model.js"},
            {"js": "packages/ui/src/Welcome.jsx"},
            {"js": "packages/ui/src/UpPicker.jsx"},
            {"js": "packages/ui/src/WorkdirPick.jsx"},
            {"js": "packages/ui/src/WechatLogin.jsx"},
        ],
    },
    "10-dialogs": {
        "title": "审批 / 提问 / 安全 / 接管", "screen": "弹窗",
        "sources": [
            {"js": "packages/ui/src/Approval.jsx"},
            {"js": "packages/ui/src/approval-model.js"},
            {"js": "packages/ui/src/AskUser.jsx"},
            {"js": "packages/ui/src/ask-user-model.js"},
            {"js": "packages/ui/src/SafetyNotice.jsx"},
            {"js": "packages/ui/src/TakeoverNotice.jsx"},
            # 说明页的外壳（标题 / 返回）。正文不在表里：它是整篇
            #   说明（`docs/help/*.md`，中英各一份），同 `Skills.jsx` 的 `setup_guide`。
            {"js": "packages/ui/src/HelpPanel.jsx"},
            {"js": "packages/ui/src/takeover-model.js"},
            {"js": "packages/ui/src/PluginNotice.jsx"},
            {"js": "packages/ui/src/plugin-notice-view.js"},
            {"js": "packages/ui/src/TestRunModal.jsx"},
            # 审批浮窗的文字来源：选项标签 / 默认正文 / 中间态问题 /
            #    收尾提示全在 `core/approval.py`（前端只负责摆）。这些是模块级常量
            #接线一律在取用处 `tr()`（`render_lines` / `build_payload` / `substate_question`）。
            {"file": "core/approval.py",
             "names": ["CROSS_ZONE_OPTIONS", "DELETE_OPTIONS", "DELETE_BULK_OPTIONS",
                       "EGRESS_OPTIONS", "COMMAND_OPTIONS", "WRITE_OUTSIDE_OPTIONS",
                       "ICE_REVEAL_OPTIONS", "PLAN_OPTIONS", "DESTRUCTIVE_OPTIONS",
                       "COMPUTER_APP_OPTIONS", "_DEFAULT_BODY", "_SUBSTATE_QUESTIONS",
                       "_FOOTER", "_COMMAND_RULE_LABEL"],
             "funcs": ["render_delete_body", "compact_file_list"]},
            # 审批载荷的正文（「想执行一个不可逆的操作」/「想写到工作区之外」）
            #   与自动模式那四句「为什么没替你答」。
            {"file": "core/agent_loop.py",
             "funcs": ["_destructive_pending_payload",
                       "_write_outside_pending_payload",
                       "_auto_review_gate"]},
            {"file": "core/shell.py", "funcs": ["_approval_prompt"],
             "names": ["_RELAX_NOTE"]},
            {"file": "core/ask_user.py", "funcs": ["pending_payload"]},
            # 审批载荷的正文产者：这三处是用户在弹窗里读到的整段话，
            #    以前只在 `core/approval.py` 收默认正文，这三个覆盖正文
            #    （计划批准 / 出网 / ICE 揭示）一个都没进表 —— 英文界面下
            #    弹窗正文整段是中文。
            # 用 `tr_calls` 而不是 `funcs`：同一个函数里既有弹窗正文（`tr`），
            #    也有写给模型的工具回执（裸字符串）—— 后者不翻（`locales/README §2`）。
            {"file": "core/plan.py", "tr_calls": True},
            {"file": "core/egress_gate.py", "tr_calls": True},
            {"file": "core/ice/reveal.py", "tr_calls": True},
        ],
    },
    "11-sidebar": {
        "title": "侧栏 / 历史 / 收藏", "screen": "左栏 · 右缘",
        "sources": [
            {"js": "packages/ui/src/App.jsx", "js_funcs": ["ThreadTree"]},
            {"js": "packages/ui/src/Rail.jsx"},
            {"js": "packages/ui/src/HistoryPanel.jsx"},
            {"js": "packages/ui/src/StarsPanel.jsx"},
            {"js": "packages/ui/src/TodosPanel.jsx"},
            {"js": "packages/ui/src/TracePanel.jsx"},
            {"js": "packages/ui/src/trace-view.js"},
            {"js": "packages/ui/src/thread-list.js"},
        ],
    },
    "12-misc": {
        "title": "加载句 / 构成面板 / 格式化", "screen": "各处",
        "sources": [
            {"js": "packages/ui/src/App.jsx",
             "js_funcs": ["TaskRow", "TaskPanel", "Stub"]},
            {"js": "packages/ui/src/loadingPhrases.js"},
            {"js": "packages/ui/src/format.js"},
            {"js": "packages/ui/src/toasts.js"},
            {"js": "packages/ui/src/clipboard.js"},
            {"js": "packages/ui/src/mode.js"},
            {"js": "packages/ui/src/theme.js"},
            {"js": "packages/ui/src/svg.js"},
            {"js": "packages/ui/src/MermaidPreview.jsx"},
            {"js": "packages/ui/src/SvgPreview.jsx"},
            {"js": "packages/ui/src/openpath.js"},
            # 连不上后端 / 拿不到令牌这类报错是直接显示给用户的（`toasts`）。
            {"js": "packages/ui/src/api.js"},
            # `Feedback.jsx` 已删除（反馈口改 GitHub issue），别加回这张表 ——
            # 留着它会让整张 12-misc 表空掉，而空表看着像"这屏没文案"。
        ],
    },
    "13-im": {
        "title": "IM 侧提示", "screen": "飞书 / 微信 / 电报 / 钉钉",
        "sources": [
            #: 通道回给用户的那几句：已中断 / 已收到 / 排队中 /
            #:   这一轮抛了。只有 `_COPY` 一处（七句），别在四个通道里各写一份。
            {"file": "core/channel_turns.py", "names": ["_COPY"]},
            {"file": "core/im_routing.py",
             "funcs": ["_render_question", "_tips", "_handle_slash_c",
                       "change_default", "route", "hot_sessions"]},
            {"file": "channels/dingtalk_channel/handler.py", "funcs": ["process"]},
            {"file": "channels/feishu_channel/handler.py", "funcs": ["to_internal"]},
            {"file": "channels/wechat_channel/handler.py",
             "funcs": ["_buffer_message", "_process_aggregated"]},
        ],
    },
}


# AST 取值

def _text_of(node, src: str):
    """把一个表达式还原成给人看的模板文本；认不出，`None`。

    `tr("…")` / `t("…")` 是透明的：接线之后 `tr("…") % x` 是一个
    `BinOp(Mod)`，左边是 `Call` —— 不认它的话，所有 `%` 写法的句子都会从表里消失
    （接线批 1 当场踩到：1157 → 1083，75 条译文变孤儿）。
    """
    # `tr(...)` / `t(...)` 与 `trf(模板, 值…)` 都是透明的：
    #   `trf` 只认第一参数是模板，后面是值。不认它，那批模板从表里消失。
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in ("tr", "t", "trf") and node.args):
        return _text_of(node.args[0], src)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            else:
                seg = ast.get_source_segment(src, part.value) or "{}"
                out.append("{%s}" % seg)
        return "".join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _text_of(node.left, src)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _text_of(node.left, src)
        right = _text_of(node.right, src)
        return None if left is None or right is None else left + right
    return None


def _docstring_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                for c in ast.walk(node):
                    if isinstance(c, ast.Constant) and c.value is d:
                        ids.add(id(c))
    return ids


def _print_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            for a in ast.walk(node):
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    ids.add(id(a))
    return ids


def _text_operand_ids(node) -> set:
    """`_text_of` 真正吃进去的那些字面量 id（只那些，不做整棵子树）。

    旧版是按 `ast.walk(node.left)` 整棵子树收的 —— 于是
    `(tr("大句子") % (x or tr("（没拿到目录）"))) + 尾巴` 里那个括号里的整句
    被当成"拼接碎片"一起排掉了，而它从没被任何一整句收过，表里根本没有它，
    界面上它永远是中文（数出来的）。
    """
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in ("tr", "t", "trf") and node.args):
        return _text_operand_ids(node.args[0])
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {id(node)}
    if isinstance(node, ast.JoinedStr):
        ids = set()
        for part in node.values:
            for sub in ast.walk(part):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    ids.add(id(sub))
        return ids
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _text_operand_ids(node.left)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _text_operand_ids(node.left) | _text_operand_ids(node.right)
    return set()


def _composed_child_ids(fn, src: str) -> set:
    """拼接表达式里的子字面量的 id —— 那些不要单独收。

    不排掉的话，`"前半" + "后半"` 会产出三行：整句 + 两个半句
    （第一版就这样，表里出现了以 `", scope="` 开头的残片）。

    两条闸，缺一条就丢句子（各丢过一批）：
    ① 只有拼得出来的时候才排 —— `body + tr("回复数字即可…")` 里 `body`
       是个变量，整句 `_text_of` 返回 `None`，那个 `tr(...)` 是唯一能收到的
       形态，再按"碎片"排掉就整句消失（`ask_user` 那句就是这样丢的）。
    ② 只排 `_text_of` 真吃进去的那几个（见 `_text_operand_ids`）——
       整棵子树会连值里的 `tr("（没拿到目录）")` 一起排掉。
    """
    ids = set()
    for node in ast.walk(fn):
        if not isinstance(node, (ast.JoinedStr, ast.BinOp)):
            continue
        if _text_of(node, src) is None:
            continue
        ids |= _text_operand_ids(node)
    return ids


def _non_display_ids(fn) -> dict[int, str]:
    """不是显示文案的字面量 —— 两类，都不是"语气问题"，是"翻了会坏"：

    · `input` —— 用户敲进来的别名（`/effort thinking 开` / `/model 默认`）。
      它们是 `x in ("开", "关")` 里的比较对象；翻了之后 `"开"` 变成英文，
      而用户敲的还是中文，别名当场失效。
    · `code`  —— 字符区间比较的操作数（`'一' <= c <= '鿿'`）。
      那是 CJK 码位边界，本来就不是给人看的字。

两者都标 `保留=0`：留证据、不删行（删了下次没人知道它们被看过）。
    判据按出现处给，按行汇总：同一个字可能既当别名又当显示文案
    （`"打开"` 就是 —— 它既是别名，又是 `当前思考开关为 %s` 的取值）
只要有一处是显示，这一行就保留=1。
    """
    out: dict[int, str] = {}
    for n in ast.walk(fn):
        if not isinstance(n, ast.Compare):
            continue
        for op, comp in zip(n.ops, n.comparators):
            if isinstance(op, (ast.In, ast.NotIn)):
                kind = "input"
            elif isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
                kind = "code"
            else:
                continue
            for sub in ast.walk(comp):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out[id(sub)] = kind
            if kind == "code":
                for sub in ast.walk(n.left):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        out[id(sub)] = kind
    return out


def _collect_in_func(fn, src, skip):
    """返回 `[(行, 文本, kind)]`，kind ∈ `display` / `input` / `code`（见 :func:`_non_display_ids`）。"""
    out = []
    skip = set(skip) | _composed_child_ids(fn, src)
    special = _non_display_ids(fn)
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skip:
                continue
            t = node.value
            if CN.search(t):
                out.append((getattr(node, "lineno", 0), t, special.get(id(node), "display")))
        elif isinstance(node, ast.JoinedStr):
            t = _text_of(node, src)
            if t and CN.search(t):
                out.append((getattr(node, "lineno", 0), t, "display"))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
            t = _text_of(node, src)
            if t and CN.search(t):
                out.append((getattr(node, "lineno", 0), t, "display"))
    return out


def _collect_payload(tree, src, keys):
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if not (isinstance(k, ast.Constant) and k.value in keys):
                continue
            t = _text_of(v, src)
            if t and CN.search(t):
                out.append((getattr(v, "lineno", 0), t))
    return out


# JS / JSX 取值
#
# 界面文案的另一半在 `packages/ui/src/**`（React）。它不是 Python，没有 AST
# 可用，这里是一个只做三件事的小词法器：
#   ① 扔掉注释 —— 本仓的注释里全是中文，混进来能把表撑大一倍；
#   ② 收字符串字面量（`"…"` / `'…'` / `` `…${x}…` ``，`{x}`）；
#   ③ 在骨架（字符串与注释已掏空）上认 JSX 文本节点（`>…<` 之间的裸中文）。
#
# 它不做 JS 语义分析 —— 也不需要：我们只要"这里有一句中文"这个事实。
# 正则字面量（`/…/`）要单独认：不认的话，`/"` 这样一个引号就能把
#    后面整段代码吃进"字符串"里。

#: 不含 `<` —— JSX 的 `</div>` 里那个 `/` 紧跟 `<`，把 `<` 当"可以起正则"
#: 的字符会让整个闭合标签被吃成一个"正则字面量"，后面的中文全丢。
_JS_REGEX_PREV = set("(,=:[!&|?{};+-*%~^")

#: 必须 `pattern.match(src, i)` —— 拿 `src[i:]` 去 match 是 O(n²)
#: （每个词都复制一遍剩余全文；`App.jsx` 15 万字符，跑不完，看起来像"卡死"）。
_JS_WORD = re.compile(r"[\w$]+")

_JS_ESC = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\",
           "'": "'", '"': '"', "`": "`", "$": "$"}


def _js_unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: _JS_ESC.get(m.group(1), "\\" + m.group(1)), s)


def _tpl_text(raw: str) -> str:
    """模板串 `` `a${b}c` ``，`a{b}c`。

    必须按花括号配平扫，不能用 `\\$\\{([^{}]*)\\}`：
    `` `${a ? {x: 1} : b}` `` 那种嵌套会让正则停在第一个 `}` 上，
    后半截 `}` 留在正文里，表里出现半截表达式（第一版就是这样）。
    """
    out, i, n = [], 0, len(raw)
    while i < n:
        if raw[i] == "$" and i + 1 < n and raw[i + 1] == "{":
            depth, j = 1, i + 2
            while j < n and depth:
                ch = raw[j]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                elif ch in "\"'`":
                    q, j = ch, j + 1
                    while j < n and raw[j] != q:
                        j += 2 if raw[j] == "\\" else 1
                j += 1
            out.append("{%s}" % raw[i + 2:j - 1].strip())
            i = j
        else:
            out.append(raw[i])
            i += 1
    return "".join(out)


def _strip_js(src: str):
    """返回 `(骨架, [(行, 文本, 类型)])`；类型 ∈ `str` / `tpl` / `cmp`。

    `cmp` = 这一句出现在 `===` / `!==` / `case` 的右边 —— 可能是值不是文案，
    由 `_keep` 挂一条备注（不当 0 处理：`"打开"` 那种两用的确实要翻）。
    """
    out: list[str] = []
    strings: list[tuple[int, str, str]] = []
    i, n, line = 0, len(src), 1
    prev_sig = ""
    prev_word = ""
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            out.append(c)
            i += 1
            prev_sig = "\n"
            continue
        if c.isspace():
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            seg = src[i:j]
            out.append("".join("\n" if ch == "\n" else " " for ch in seg))
            line += seg.count("\n")
            i = j
            continue
        # 自闭合标签的 `/>`：`/` 后面紧跟 `>` 时不是正则
        #    （`size={12} />` 里的 `}` 在集合里，会把 `/>置顶</` 吃成"正则"）。
        if (c == "/" and src[i + 1:i + 2] != ">"
                and (prev_sig in _JS_REGEX_PREV or prev_word == "return")):
            j, in_class = i + 1, False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                elif ch == "\n":
                    break
                j += 1
            k = j + 1
            while k < n and src[k].isalpha():
                k += 1
            out.append(" " * (k - i))
            i = k
            prev_sig = "/"
            continue
        if c in "\"'`":
            q, j, buf = c, i + 1, []
            while j < n:
                ch = src[j]
                if ch == "\\" and j + 1 < n:
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                if ch == q:
                    break
                buf.append(ch)
                j += 1
            raw = _js_unescape("".join(buf))
            text = raw if q != "`" else _tpl_text(raw)
            if CN.search(text):
                back = src[max(0, i - 12):i].rstrip()
                kind = "cmp" if re.search(r"(===|!==|==|!=|case)\s*$", back) else (
                    "tpl" if q == "`" else "str")
                strings.append((line, text, kind))
            line += "".join(buf).count("\n")
            out.append("".join("\n" if ch == "\n" else " " for ch in src[i:j + 1]))
            i = j + 1
            prev_sig = q
            continue
        out.append(c)
        prev_sig = c
        if c.isalpha() or c in "_$":
            m = _JS_WORD.match(src, i)
            prev_word = m.group(0) if m else c
            if m:
                out.append(m.group(0)[1:])
                i = m.end()
                continue
        i += 1
    return "".join(out), strings


#: `>…<` 之间的一段 JSX 文本；`{expr}` 留成占位。`(?![=])` 排掉 `>=`。
#: 字符类必须排除 `{}`：写成 `[^<>]` 的话，一个 `{` 既能被 `[^<>]` 吃掉、
#: 又能被 `\{[^{}]*\}` 吃掉，两条路重叠，灾难性回溯（`App.jsx` 上直接卡死）。
_JSX_TEXT = re.compile(r">((?:[^<>{}]|\{[^{}]*\})*)<(?![=])")


def _jsx_text(skel: str) -> list[tuple[int, str]]:
    hits = []
    for m in _JSX_TEXT.finditer(skel):
        raw = m.group(1)
        if not CN.search(raw):
            continue
        t = re.sub(r"\s+", " ", raw).strip()
        t = re.sub(r"\{([^{}]*)\}", lambda mm: "{%s}" % mm.group(1).strip(), t)
        # 接线之后占位符里会出现 `t("…")` —— 键要跟接线前逐字一致，
        #   否则每接一次线就换一批键（译文全成孤儿）。
        # 连花括号一起删：只删 `t(…)` 会剩下一个 `{}`，而 `_SUSPECT`
        #   把 `{}` 当“内部串”的特征，真 UI 文案被静默标成 `保留=0`。
        t = re.sub(r"\{t\([^(){}]*\)\}", "", t)
        # 空的 `{}` 一律拿掉 —— 它来自 `{/* 注释 */}`（`_strip_js` 把注释挖成空格），
        #   不是占位符。留着它会两头坏：键里多个 `{}`，而 `_SUSPECT` 又把 `{}` 当内部串。
        t = re.sub(r"\{\s*\}", "", t)
        t = re.sub(r"[ 	]{2,}", " ", t)
        if CN.search(t):
            hits.append((skel.count("\n", 0, m.start()) + 1, t))
    return hits


def _js_region(src: str, name: str):
    """取顶层 `function NAME(` / `const NAME =` 那一块 —— 返回 `(起始行, 文本)`。

    用符号名而不是行号：行号会漂，而 `App.jsx` 一屏里住着好几个"屏幕"
    （`AppInner` 聊天 / `Settings` 设置 / `ThreadTree` 左栏）。
    """
    lines = src.splitlines(keepends=True)
    head = re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+%s\b|^const\s+%s\s*=|^(?:export\s+)?class\s+%s\b"
                      % (re.escape(name), re.escape(name),
                         re.escape(name)))
    start = next((i for i, ln in enumerate(lines) if head.match(ln)), None)
    if start is None:
        return None
    decl = re.compile(r"^(?:export\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+[A-Za-z_$]")
    end = next((j for j in range(start + 1, len(lines)) if decl.match(lines[j])), len(lines))
    return start + 1, "".join(lines[start:end])


# 主流程

def scan_block(name: str, spec: dict) -> list[dict]:
    rows: dict[str, dict] = {}

    def add(text: str, where: str, kind: str, loc_file: str):
        text = (text or "").strip()
        if not text:
            return
        row = rows.get(text)
        if row is None:
            row = rows[text] = {"原文": text, "where": [], "funcs": [], "kinds": set()}
        # 只记文件，不记行号：行号会随每一次源码改动整批漂掉
        # （清一轮前端注释，1,786 行里有 367 行的行号当场就错了）。
        # 要上下文时按原文 grep —— 原文就是运行时键，比行号稳。
        loc = loc_file
        if loc not in row["where"]:
            row["where"].append(loc)
        if where not in row["funcs"]:
            row["funcs"].append(where)
        row["kinds"].add(kind)

    for src_spec in spec["sources"]:
        # ① JS / JSX —— 界面那一半
        if src_spec.get("js"):
            rel = src_spec["js"]
            p = _ROOT / rel
            if not p.exists():
                print("[scan] 缺文件：%s" % rel)
                continue
            src = p.read_text(encoding="utf-8")
            pieces = []
            if src_spec.get("js_funcs"):
                for fn in src_spec["js_funcs"]:
                    got = _js_region(src, fn)
                    if got is None:
                        print("[scan] 找不到函数 %s::%s" % (rel, fn))
                        continue
                    pieces.append((got[0], got[1], fn))
            else:
                pieces.append((1, src, p.stem))
            for _base, seg, where in pieces:
                skel, strings = _strip_js(seg)
                for _ln, t, kind in strings:
                    add(t, where, "display", rel)
                    if kind == "cmp":
                        rows[t.strip()]["kinds"].add("cmp")
                for _ln, t in _jsx_text(skel):
                    add(t, where, "display", rel)
            continue

        # ①b YAML —— 技能声明的用户面文案（manifest 的 `config.fields`）
        if src_spec.get("yaml"):
            import glob as _glob
            import yaml as _yaml
            for rel in sorted(_glob.glob(str(_ROOT / src_spec["yaml"]))):
                rp = Path(rel)
                try:
                    data = _yaml.safe_load(rp.read_text(encoding="utf-8")) or {}
                except Exception as exc:                          # noqa: BLE001
                    print("[scan] 读不动 %s（%s），跳过" % (rp.name, type(exc).__name__))
                    continue
                cfg = (data.get("config") or {}) if isinstance(data, dict) else {}
                for f in (cfg.get("fields") or []):
                    if not isinstance(f, dict):
                        continue
                    for key in ("label", "placeholder", "help"):
                        v = f.get(key)
                        if isinstance(v, str) and CN.search(v):
                            rel_posix = rp.relative_to(_ROOT).as_posix()
                            add(v, rel_posix, "display", rel_posix)
            continue

        # ② Python
        p = _ROOT / src_spec["file"]
        if not p.exists():
            print("[scan] 缺文件：%s" % src_spec["file"])
            continue
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src)
        skip = _docstring_ids(tree) | _print_ids(tree)
        found: list[tuple[int, str, str, str]] = []   # (line, text, where, kind)

        if src_spec.get("payload"):
            for line, t in _collect_payload(tree, src, set(src_spec["payload"])):
                found.append((line, t, "API payload", "display"))
        if src_spec.get("tr_calls"):
            # 显式包了 `tr(...)` / `t(...)` / `trf(...)` 的就是用户面。
            #   不看 funcs / payload 名单：这是"作者自己声明过"的那一类。
            for n in ast.walk(tree):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id in ("t", "tr", "trf") and n.args):
                    continue
                text = _text_of(n.args[0], src)
                if text and CN.search(text):
                    found.append((getattr(n, "lineno", 0), text, "tr()", "display"))
        for prefix in src_spec.get("func_prefixes", []):
            for n in ast.walk(tree):
                if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and n.name.startswith(prefix)):
                    for line, t, kind in _collect_in_func(n, src, skip):
                        found.append((line, t, n.name, kind))
        for nm in src_spec.get("names", []):
            for n in ast.walk(tree):
                tgt = []
                if isinstance(n, ast.Assign):
                    tgt = n.targets
                    val = n.value
                elif isinstance(n, ast.AnnAssign):
                    tgt = [n.target]
                    val = n.value
                else:
                    continue
                if not any(isinstance(t, ast.Name) and t.id == nm for t in tgt):
                    continue
                for c in ast.walk(val):
                    if (isinstance(c, ast.Constant) and isinstance(c.value, str)
                            and id(c) not in skip and CN.search(c.value)):
                        found.append((getattr(c, "lineno", 0), c.value, nm, "display"))
        for fname in src_spec.get("funcs", []):
            hits = [n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == fname]
            if not hits:
                print("[scan] 找不到函数 %s::%s" % (src_spec["file"], fname))
                continue
            for fn in hits:
                for line, t, kind in _collect_in_func(fn, src, skip):
                    found.append((line, t, fname, kind))

        for line, text, where, kind in found:
            add(text, where, kind, src_spec["file"])

    return [dict(v, id="%s-%s" % (name.split("-")[0],
                                  hashlib.sha1(v["原文"].encode("utf-8")).hexdigest()[:8]))
            for v in sorted(rows.values(), key=lambda r: r["原文"])]


def _keep(row: dict) -> tuple[int, str]:
    """默认 1；像内部/开发串的标 0（让人复核，不是自动排除）。"""
    if row["原文"] in _VALUE_ONLY:
        return 0, "[!] **写进正文的令牌**（不是显示文案）—— 翻了它就认不出来"
    if row["原文"] in _MODEL_FACING:
        return 0, ("[!] **写给模型的**（`ctx.user_message` / 工具回执）—— 界面上不出现，"
                   "界面语言不翻它")
    if row["原文"] in _DEV_FACING:
        return 0, ("[!] **API 契约报错**（前端正常路径发不出来）—— 用户见不到，"
                   "翻了是给一句永远不会出现的句子写译文")
    kinds = row.get("kinds") or set()
    # 第一档：根本不是文案的那些（翻了会坏，不是语气问题）
    if kinds and "display" not in kinds:
        if "input" in kinds:
            return 0, ("[!] **用户敲的别名**（`x in (\"开\", …)` 里的字面量），不是显示文案"
                       " —— 翻了别名当场失效；复核后改回 1")
        return 0, ("[!] **字符区间比较**的操作数（`'\\u4e00' <= c <= '\\u9fff'`），不是文案"
                   " —— 复核后改回 1")
    # 去掉占位符之后以中文开头，不是内部串。
    #   内部串总是拿 ASCII 技术词打头（HTTP 400 那一类 / payload / def）；
    #   而用户看的那句可以正好含同一个词（`通了：{tried}（HTTP {status}）`）。
    _probe = re.sub(r"\{[^{}]*\}", "", row["原文"]).strip()
    #   一个更硬的判据：中文字数 ≥ 2 就是人话（`https://你的中转站/v1`
    #   这种水印也得翻）—— 技术串里那几个中文字不会那么密。
    if (_SUSPECT.search(row["原文"]) and not CN.match(_probe)
            and len(CN.findall(row["原文"])) < 2):
        return 0, "[!] 看着像内部串（含 HTTP/JSON/payload/路径…）—— 复核后改回 1"
    if _LOGISH.search(row["原文"]):
        return 0, "[!] 看着像日志/内部标记（`[` / `#` / `---` 开头）—— 复核后改回 1"
    if ((_PARAMISH.search(row["原文"]) and ("要是" in row["原文"] or "必须" in row["原文"]))
            or _PARAM_SHORT.search(row["原文"])):
        return 0, "[!] 直呼参数名（像开发者接口的校验话术）—— 复核后改回 1"
    # 占位符里已经接了 `t()`（接线之后）：外层整句不用再翻 —— 它自己的
    #   那几段已经各自翻过了；再整句包一层会套娃。
    if ("t(" in row["原文"] or "&&" in row["原文"] or "||" in row["原文"]
            or "===" in row["原文"] or "!==" in row["原文"] or "=>" in row["原文"]
            or re.search(r"\|\|\s*\}", row["原文"])):
        return 0, "[!] 这是一段**表达式**（不是给人看的句子）—— 里面的文案各自翻过了"
        return 0, "[!] 占位符里已经接了 `t()` —— 外层不用整句翻"
    if len(CN.findall(row["原文"])) <= 1:
        return 1, ("[!] **碎片 / 单位**（只含一个汉字）—— 先看 `位置` 里的上下文再定；"
                   "拿不准就标 0（硬翻成英文会放错地方）")
    for ph in re.findall(r"\{([^{}]*)\}", row["原文"]):
        if CN.search(ph):
            return 1, ("[!] 占位符**里也有中文**（`{%s}`）—— 翻的时候要连里面一起处理，"
                       "别把整个占位符删掉" % ph.strip())
    if "cmp" in kinds:
        return 1, ("[!] 这句**同时被用作比较值**（`=== \"…\"` / `case`）——"
                   " 翻的时候两处必须一起改，或先把它换成稳定的键")
    return 1, ""


def _old_translations(path: Path, langs: list[str]) -> tuple[dict, dict]:
    """读旧表里已填的译文 —— 重扫不许冲掉人填的东西。

    返回 `(按 id, 按原文)` 两套索引。按原文那一套是必需的，不是保险：
    第一版只按 `id` 找回译文，而 `id` 里带着块名前缀，那个前缀换过一次
    （`_COMMANDS-…` → `01-…`），换过之后重扫会给每一行新 id，
    已填的 129 条译文一条都对不上，而症状只是"表里又空了"。
    真正的键是中文原文（`locales/README.md` 第一条）—— `id` 只是行地址。
    """
    if not path.exists():
        return {}, {}
    by_id: dict[str, dict] = {}
    by_text: dict[str, dict] = {}
    def _nl(x: str) -> str:
        #: `core.autocrlf` 检出时会把多行单元格里的换行变成 CRLF
        #:    （`01-commands.csv` 在 git 里是 LF，工作区是 CRLF）。
        #:    不归一的话，重扫会把回车符灌回译文 —— 一次「10 条改译」的假改动，
        #:    而真正变的那几行被埋掉。原文也要归一：它是找回译文的键。
        return (x or "").replace("\r\n", "\n").replace("\r", "\n")

    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            val = {l: _nl(r.get("译文_%s" % l)) for l in langs}
            #: 连 `保留` 一起带走 —— 那是人工判断，重扫不许冲掉它。
            val["保留"] = (r.get("保留") or "").strip()
            by_id[r.get("id") or ""] = val
            text = _nl(r.get("原文"))
            if text.strip():
                by_text[text] = val
    return by_id, by_text


def _pack_translations(langs: list[str]) -> dict[str, dict]:
    """`locales/<lang>.json` 里已经定稿的译文 —— 新行的第二个来源。

    为什么需要它：旧表只带得回"上一张表里已经有的行"。文件新纳入扫描、
    或者某一句是两次扫描之间才加进代码的，那一行对旧表来说就是新的
    ，译文为空 ，`apply` 于是把它当"没填"，而 `en.json` 里现成的译文被丢掉。
    把 `SkillConfig.jsx` 纳入扫描那次，16 条译文当场变成孤儿。

    键就是中文原文（`locales/README.md` 第一条），所以按原文直接接回来
    是安全的 —— 同一条原文在表里只有一行。
    """
    out: dict[str, dict] = {}
    for l in langs:
        p = OUT / ("%s.json" % l)
        try:
            got = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                  # noqa: BLE001
            got = {}
        out[l] = got if isinstance(got, dict) else {}
    return out


def _write_csv(path: Path, rows: list[dict], langs: list[str],
               keep_filter=None, pack: dict | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    by_id, by_text = _old_translations(path, langs)
    cols = (["id", "块", "屏幕", "位置", "原文"] + ["译文_%s" % l for l in langs]
            + ["备注", "保留", "中文字数"])
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        #: 显式给 `lineterminator` —— 不给的话 csv 在 Windows 上写 CRLF，
        #:    而仓库里这些表是 LF，每跑一次重扫，整张表都算"改过"
        #:    （一次三百多行的假改动，把真正变的那几行埋掉）。
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for r in rows:
            keep, note = _keep(r)
            prev = by_id.get(r["id"]) or by_text.get(r["原文"]) or {}
            #: 人改过的 `保留` 优先 —— 启发式只在这一行还没有过判断时生效。
            #: 不这么做的话，人把一行从 0 改成 1（或反过来）之后，下一次重扫
            #: 会把它翻回去，而 `i18n_apply` 随即把那条译文当孤儿删掉：
            #: 现场 2026-11-02，`#02 组件异常…`（看着像日志、其实是横幅）
            #: 每次重扫都被翻回 0，英文界面上它一直是中文。
            _prev_keep = str(prev.get("保留") or "")
            if _prev_keep in ("0", "1"):
                keep = int(_prev_keep)
            if keep_filter is not None and keep != keep_filter:
                continue
            out = {
                "id": r["id"], "块": r["block"], "屏幕": r["screen"],
                "位置": "；".join(r["where"]), "原文": r["原文"],
                "备注": "；".join(filter(None, [note, "出自 " + "、".join(r["funcs"])])),
                "保留": keep, "中文字数": len(r["原文"]),
            }
            for l in langs:
                got = prev.get(l, "")
                if not got and pack:
                    # 旧表里没有这一行，就去成品包里按原文找一次
                    got = (pack.get(l) or {}).get(r["原文"], "")
                out["译文_%s" % l] = got
            w.writerow(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", nargs="*", default=None,
                    help="只扫这些块（默认全部已登记块）")
    ap.add_argument("--langs", nargs="*", default=["en"],
                    help="译文列（以后加 ja 时：--langs en ja）")
    args = ap.parse_args()
    langs = [str(x).strip() for x in (args.langs or ["en"]) if str(x).strip()]

    names = args.blocks or [k for k in BLOCKS if k != "00-all"]
    pack = _pack_translations(langs)

    #: 旧表里人改过的 `保留`——按原文索引（`id` 只是行地址，
    #: 块名前缀换过一次，见 `_old_translations` 的 docstring）。
    #: 三处（主表、清单、跳过表）都用它，别让它们各算一遍。
    old_keep: dict[str, str] = {}
    for name in names:
        _, by_text = _old_translations(WS / ("%s.csv" % name), langs)
        for text, val in by_text.items():
            pk = str(val.get("保留") or "")
            if pk in ("0", "1"):
                old_keep[text] = pk

    def _keep_eff(row: dict) -> tuple[int, str]:
        keep, note = _keep(row)
        pk = old_keep.get(row["原文"])
        if pk is not None:
            keep = int(pk)
        return keep, note

    all_rows: list[dict] = []
    for name in names:
        spec = BLOCKS.get(name)
        if spec is None:
            print("[scan] 没登记这个块：%s" % name)
            continue
        rows = scan_block(name, spec)
        for r in rows:
            r["block"], r["screen"] = name, spec["screen"]
        all_rows.extend(rows)
        _write_csv(WS / ("%s.csv" % name), rows, langs, pack=pack)
        _write_csv(WS / ("%s.skipped.csv" % name), rows, langs,
                   keep_filter=0, pack=pack)
        print("[scan] %-18s %3d 行（保留=0：%d）"
              % (name, len(rows), sum(1 for r in rows if _keep_eff(r)[0] == 0)))

    if len(all_rows) > 1:
        _write_csv(WS / "00-all.csv", all_rows, langs, pack=pack)

    # 机读清单（回填脚本与覆盖率用）
    src_map = {r["原文"]: {"block": r["block"], "screen": r["screen"],
                           "where": r["where"], "ph": _PH.findall(r["原文"]),
                           "keep": _keep_eff(r)[0]} for r in all_rows}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "source.json").write_text(
        json.dumps(src_map, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n")

    skipped = [r for r in all_rows if _keep_eff(r)[0] == 0]
    skip_path = OUT / "skipped.csv"
    with skip_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["块", "原文", "位置", "原因"])
        for r in skipped:
            w.writerow([r["block"], r["原文"], "；".join(r["where"]),
                        _keep_eff(r)[1]])

    print("[scan] 共 %d 行 → %s" % (len(all_rows), WS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
