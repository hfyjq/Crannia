> 建议：使用 agent 浏览此文件，并配合开发。

# 与 MCP 生态的对接

## 先选一条路

| 你想做的事 | 走哪条 |
|---|---|
| 本框架里多几个工具，自己能改 | 写一个技能，见 `SKILL_SPEC.md` |
| 已经有一个 MCP 服务器，或别人写好了 | 接进来，见下面的「作为客户端」 |
| 让别人在别的客户端里用你的工具 | 写 MCP 服务器；本框架不作为服务器对外，见文末 |
| 驱动一个桌面软件（PS / Blender / AE / C4D…） | 先看它有没有命令行或脚本 API，有就按「外部程序」写技能；没有才考虑别的 |

## 两种技能

| 类型 | 文件 | 作用 |
|---|---|---|
| 工具型 | `manifest.yaml` + `handler.py` + `SKILL.md` | 注册工具，模型可以调用 |
| 内容型 | 只有 `SKILL.md` | 提供流程与知识，不注册工具 |

判断依据只有一个：目录里有没有 `manifest.yaml`。所以「双格式」说的是工具型技能，不是每个目录都有两个文件。

## 命名

| 位置 | 用 | 例 |
|---|---|---|
| 目录名 | 下划线 | `skills/file_parser/` |
| Python 模块路径 | 下划线 | `skills.file_parser.handler` |
| `SKILL.md` 的 `name` | 连字符 | `file-parser` |

目录名用下划线，是因为 Python 导入不接受连字符；`SKILL.md` 用连字符，是 agentskills.io 的要求。这个差异对使用者不可见：技能页、模型看到的工具名都用 `manifest.yaml` 里的 `name`。

## 已经对齐的部分

- 工具定义用 JSON Schema 2020-12，声明 `$schema`。
- `SKILL.md` 的 frontmatter 符合 agentskills.io 格式。
- 工具的 `name` / `description` 满足生态里的匹配要求。
- 工具表的形状与 MCP `tools/list` 一致，可以直接投影。

## 移植到别的客户端时

- 目录名用下划线，生态标准用连字符。
- handler 是 Python 函数，不是 MCP 端点。
- 没有生态规范里的 `scripts/` 子目录。

这些差异只在把技能直接摊给外部客户端时显现。技能在本框架里按自己的约定加载，对外按需做投影，源目录不动 —— 所以同一份技能可以两边都放，不需要为迁移改结构。

## 作为客户端接入外部 MCP 服务器

`data/mcp_servers.json`，标准 MCP 配置格式：

```json
{
  "mcpServers": {
    "example": {
      "command": "npx",
      "args": ["-y", "@example/mcp-server"],
      "env": {}
    },
    "remote": {
      "type": "http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer ${TOKEN}"}
    }
  }
}
```

- 支持 stdio 与 HTTP 两种传输。HTTP 的请求头支持 `${VAR}` 占位符，展开发生在解析那一刻，不落盘、不打印。
- 外部工具统一以 `mcp__<服务器名>__<工具名>` 注册；名字超长或拼起来会产生歧义时，追加一段哈希尾巴。发给服务器的一直是原始工具名。
- 外部工具的权限档**不看**服务器注解：一律 `write`（注解只作展示）。
  `destructive` 仍取自 `destructiveHint`，方向只会更严、不会更松。
- 外部工具的输出默认按**不可信内容**处置：`<user_document trust="untrusted">` 包裹、剥控制字符、把框架保留标签中和成 `<`。理由与 `web_fetch` / `get_email` 一致 —— 这段正文是第三方写的。
- 某个服务器确实只回结构化结果时，在它的配置里写 `"trusted_output": true` 关掉包裹（只影响输出形态，不改权限档）。判据在本地：不看服务器自己的注解。
- 一个服务器的工具不会全部常驻：它们按需检索，中文查询拿不到英文描述时会提示换英文关键词。

## 对外提供 MCP 服务器

尚未实现。要做的形状是：把本框架的工具表投影成 `tools/list`，把调用投影成 `tools/call`，注册表已经有了需要的全部信息。

在那之前，外部集成走两条路：接入对方的 MCP 服务器，或者写一个技能。
