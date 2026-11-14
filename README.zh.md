<div align="center">
  <img src="web/static/img/logo.svg" width="96" height="96" alt="Crannia">
  <h1>Crannia</h1>
  <p><strong>通用型 Agent</strong></p>
  <p>
    简体中文 |
    <a href="README.md">English</a>
  </p>
  <p>
    <img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-blue">
    <img alt="Python" src="https://img.shields.io/badge/python-3.12%2B-blue">
    <img alt="Platform" src="https://img.shields.io/badge/platform-Windows-lightgrey">
    <img alt="Status" src="https://img.shields.io/badge/status-beta-orange">
  </p>
</div>

Crannia 是一个开源的通用型 Agent，可以协助完成事项，或构筑您的想法。

> 当前是 Beta。核心循环、技能、通道和桌面壳可用；接口、提示词和磁盘格式在版本之间
> 仍可能变化。

## 快速开始

### 方式一：从安装包

从 Releases 下载最新的 `crannia-*-win-x64.exe` 运行。安装器会解包成一个普通的
应用目录并创建快捷方式；随包的 Python 运行时在 `resources/primary-runtime` 下，
不需要额外配置系统环境。

### 方式二：从源码运行

要求：Windows 10/11，Python **3.12+**，Node.js。

```powershell
git clone https://github.com/hfyjq/Crannia.git
cd Crannia
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# 编辑 .env，至少填一个 provider key，例如 DEEPSEEK_API_KEY
```

GUI 由 Electron 启动，运行如下命令以拉起：

```powershell
npm install
npm run dev
```

模型可以不改 `config.yaml`，直接在设置页的端配置里添加。

> 框架不会把 API key 写进仓库。key 放在 `.env`，或放在端配置页使用的凭据存储里。

## 互操作

Crannia 会读取 Claude Code、Codex、WorkBuddy 等应用的技能 / 钩子 / 插件等目录，现成的库可以直接使用。该行为是只读的，支持度详见下表。

| 组件 | 读取位置 | 支持度 |
|---|---|---|
| 技能 | `skills/` · `.agents/skills/` · `~/.agents/skills/` · `~/.claude/skills/` · `~/.codex/skills/.system` | `SKILL.md` 的 frontmatter（`name` / `description`）＋正文。注意，这里不包含带 `manifest.yaml` 的类型，此类型会被包含在其它方法中 |
| 子 agent | `agents/` · `.agents/agents/` · `~/.agents/agents/` · `~/.claude/agents/` | `name` · `description` · `tools` · `maxTurns`；正文为角色说明。只能压低当前档位的预算，不允许向上，使用 `maxTurns` 以变动。 |
| 钩子 | `data/hooks.json` · `~/.claude/settings.json` · `<工作区>/.crannia/hooks.json` · `<工作区>/.claude/settings.json` | 事件被认作一个子集，见下。 |
| 插件包 | `plugins/` · `.agents/plugins/` · `~/.agents/plugins/` · 两个插件缓存目录 | `plugin.json` · `.mcp.json` · `skills/` · `agents/` · `hooks/hooks.json`。`commands/`，在此类别的条目是可以读取的，但并没有对应的装载器 |
| MCP | `.mcp.json` · `manifest.yaml` | JSON Schema 2020-12 的工具定义，见 [`MCP_ADAPTATION.md`](MCP_ADAPTATION.md)。 |

### 钩子：子集覆盖范围

九类事件：`PreToolUse` · `PostToolUse` · `PostToolUseFailure` · `UserPromptSubmit` · `UserPromptExpansion` · `SessionStart` · `Stop` · `StopFailure` · `PermissionRequest`。`SubagentStart` / `SubagentStop` 会被识别、跳过并附有对应提示；其余事件名静默跳过。

钩子拿到的字段：`hook_event_name` · `session_id` · `cwd` · `timestamp`，以及各事件自己的（`tool_name` · `tool_input` · `tool_use_id` · `tool_response` · `stop_hook_active`）。退出码 2 = 拦下，stderr 当理由；其余非零码为非阻塞失败。退出 0 时可以打一段 JSON：`hookSpecificOutput.permissionDecision`（`allow` / `ask` / `deny`）· `permissionDecisionReason` · `updatedInput` · `additionalContext`，以及顶层的 `decision: "block"`。

`matcher` 是对工具名的正则，两种写法都是被允许的（`run_command` 也匹配 `Bash`）。`async: true` 在后台跑 —— 则它将不会被允许拦、问、送回任何结果。从插件包里来的钩子会替换 `${CLAUDE_PLUGIN_ROOT}`。

需要注意，此表以外的条目都是不兼容类型或存在故障，它们不会生效。

## 安全边界

Crannia 提供的是围栏，不是沙箱。在有重要数据的机器上运行之前，请先阅读下文：

- **能执行代码的入口不止一个。** `run_command`（跑 shell）与 `run_program`（子进程里
  跑 Python，带 `-I` 隔离）是两条任意代码路径；钩子、外部 MCP 服务器，以及技能与
  插件的 Python handler 也会在您的机器上执行代码。它们都不构成 OS 级隔离：shell 与
  Python 同样可以出网、能碰绝对路径。因此，请务必只安装信任的技能与 MCP 服务器。
- **出网需要经过硬性审查（针对有出网地板的工具）。** `web_fetch` 这类工具拒绝私有地址、
  回环、链路本地等非公网目标，跳转只在同站内跟随；而本机 / 局域网里的 MCP 服务器、
  以及您自己配置的本机模型属于正常部署，不在这条里。
- **写入按路径分级。** 读受保护路径之外的文件是放行的；写工作区之外需要审批。
- **完全权限是一次显式的解除。** 有关这方面的具体内容，详见
  [`SECURITY.zh.md`](SECURITY.zh.md)。

四条执行入口的完整清单、报告通道，以及明确没有做的隔离，详见
[`SECURITY.zh.md`](SECURITY.zh.md)。数据存储、生命周期、发送问题，详见
[`PRIVACY.zh.md`](PRIVACY.zh.md)。

## 卸载与数据

对话记录、长期记忆、模型配置与凭据等，都将被放置在**按用户的数据根**下面：

| 形态 | 数据根 | 默认工作区 |
|---|---|---|
| 安装版 | `%APPDATA%\Crannia` | `文档\Crannia\workspace` |
| 从源码 | 项目根目录 | 项目里的 `workspace` |

请注意，升级与卸载不会变更这个目录。卸载程序只负责删程序目录与快捷方式；如需清数据，需要自行删除 `%APPDATA%\Crannia`。

**卸载入口**：Windows「设置 → 应用 → 已安装的应用 → Crannia」，
或安装目录里的 `Uninstall Crannia.exe`。

**以下条目会被删除**：安装目录、快捷方式、注册表项。

通过应用内设置页「其他设置 → 数据目录」可以查看不会被删除的目录。

> 早先的版本把数据放在安装目录里；首次运行会把那里的 `data/`、`logs/`、
> `workspace/` 和 `config.yaml` 复制到数据根。目标已存在就跳过，不覆盖。

## 开发

测试并不含有 pytest：每个测试文件都自带 `__main__` 运行器。

```powershell
# 全量（并行）
.venv\Scripts\python.exe scripts\run_tests.py

# 单个测试文件
.venv\Scripts\python.exe tests\test_pipeline.py
```

桌面壳：

```powershell
npm install
npm run dev        # Vite + Electron，Electron 会拉起后端
npm run build      # 只构建前端
npm run package    # 构建并产出 Windows 安装包
```

随包运行时由 `apps/desktop/primary-runtime-lock.json` 钉住；依赖变化时用
`scripts/pin_wheels.py` + `scripts/prepare_runtime.py` 重建。

想写一个技能？从 [`SKILL_SPEC.md`](SKILL_SPEC.md) 与
[`MCP_ADAPTATION.md`](MCP_ADAPTATION.md) 开始。

## 许可

Apache-2.0。见 [`LICENSE`](LICENSE) 与 [`NOTICE`](NOTICE)。

第三方组件与义务：

- [`THIRD-PARTY.md`](THIRD-PARTY.md) —— 人读的声明、LGPL/MPL 履约、素材出处
- [`THIRD-PARTY-PYTHON.md`](THIRD-PARTY-PYTHON.md) —— 随包的全部 Python 发行包
- [`sbom.json`](sbom.json) —— CycloneDX SBOM
- [`licenses/`](licenses/) —— LGPL-3.0、LGPL-2.1、GPL-3.0、MPL-2.0 全文

## 商标

本项目出现的第三方平台名称与标志 —— 飞书、微信、Telegram、钉钉等 —— 归各自所有者。
这里只用来说明 Crannia 支持哪些集成（指示性使用）：不表示这些平台对本项目有任何
赞助、认可、隶属或背书关系，也不代表 Crannia 把任何平台的标志当成自己的品牌。

如果您是商标所有者，希望某个名称或标志被修改或移除，请在 issue 内提出。

## 贡献

欢迎 issue 和 pull request —— 完整规则见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。
新技能从 [`SKILL_SPEC.md`](SKILL_SPEC.md) 开始；新通道或 provider 按 `channels/`、
`providers/` 里现有的模块布局来。注释请写**为什么这样做**，不要写改动史。

安全问题**不要**用公开 issue 提出，请按 [`SECURITY.zh.md`](SECURITY.zh.md) 里的
私密通道提交（那份也列了安全边界）；数据边界详见 [`PRIVACY.zh.md`](PRIVACY.zh.md)。
