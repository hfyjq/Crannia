# Crannia 子 agent 定义规范

## 1. 放在哪里

| 位置 | 用途 |
|---|---|
| `agents/` | 仓库根，随项目走 |
| `.agents/agents/` | 项目级，当前目录 |
| `~/.agents/agents/` | 用户级，跨项目 |
| `~/.claude/agents/` | 读取其他工具的 agents 目录 |

扫描按上面的顺序进行，同名定义以先出现的为准。

## 2. 文件格式

一个定义文件由 YAML frontmatter 与正文组成：

```markdown
---
name: reviewer
description: 审查改动并列出问题
tools: [Read, Grep]
maxTurns: 4
---
你是一名代码审查员。只报告可复现的问题，不修改文件。
```

正文是角色说明，会加在派发指令前面。

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 否 | 缺省取文件名。只允许字母、数字、下划线、点和连字符，且不以符号开头 |
| `description` | 否 | 写入 `run_subagent` 的工具描述，供模型选择 |
| `tools` | 否 | 工具白名单，列表或逗号串。省略表示不限制 |
| `maxTurns` | 否 | 正整数。实际轮次为档位预算与该值的较小者 |

## 3. 工具名对照

`tools` 里可以使用常见写法的工具名，载入时转换为本框架的名字：

| 写法 | 转换后 |
|---|---|
| `Read` / `read` | `read_file` |
| `Write` / `write` | `write_file` |
| `Edit` / `MultiEdit` / `edit` | `edit_file` |
| `Bash` / `PowerShell` | `run_command` |
| `WebFetch` | `web_fetch` |
| `WebSearch` | `web_search` |
| `Skill` / `ToolSearch` | `search_tools` |
| `Task` / `Agent` | `run_subagent` |
| `AskUserQuestion` | `ask_user` |

其他名字原样保留，例如 MCP 工具名。写错的名字不会在载入时报错，调用时会被白名单拒绝。

## 4. 使用方法

模型调用 `run_subagent` 时指定 `agent`：

```json
{"agent": "reviewer", "task": "检查 parser.py 的边界条件", "readonly": true}
```

不填 `agent` 时使用通用子 agent，与原行为一致。存在定义文件时，工具描述会列出可用的名字与描述。

## 5. 生效方式与限制

- 白名单在执行侧生效，不改变发给模型的工具表。
- `maxTurns` 只能低于档位给出的预算，高于档位的值会被压低。
- 定义文件在框架启动时读取并写入工具描述，新增或修改后需要重启框架。
- 不支持 `model`、`effort`、`color`。子 agent 与父使用同一模型与思考模式，换模型会改变请求前缀。
- 子 agent 不能再派子 agent。
