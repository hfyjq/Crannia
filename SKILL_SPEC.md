> 建议：使用 agent 浏览此文件，并配合开发。

# Skill 开发规范

一个技能就是一个目录。工具型技能用 `manifest.yaml` 定义工具、用 `handler.py` 实现；`SKILL.md` 是给生态看的元数据。放进目录就启用，不需要改注册代码。

## 1. 放在哪里与目录结构

| 位置 | 谁放的 |
|---|---|
| `skills/<名字>/` | 随项目发布。同名的它优先 |
| `data/skills/<名字>/` | 用户自己装的（技能页「添加技能」装到这里），升级不动 |
| `~/.agents/skills/` · `.agents/skills/` | 本机共享 / 当前项目级 |
| `~/.claude/skills/` · `~/.codex/skills/.system/` | 别的 agent 的目录，原样可用 |

按上表顺序扫描，同名以先出现的为准 —— **随包的那份赢**。所以自己写的技能不要跟内置的重名：重名的那个会被跳过（启动日志里有一行说明），而不是覆盖内置实现。换个名字即可。

工具型（`manifest.yaml` + `handler.py`）从前两行装载：handler 按 `skills.<目录名>.handler` 导入，框架会把那个根挂进包路径，所以 `data/skills/` 也成立。后面几行只收只有 `SKILL.md` 的指令型技能 —— 其他应用的目录里本来也不会跑我们这一套 handler。

目录长这样：

```
skills/<skill_name>/
├── manifest.yaml        # 工具定义，权威来源
├── handler.py           # 工具实现
├── SKILL.md             # agentskills.io 兼容元数据
└── __init__.py          # 包标记，可为空
```

需要时再加：

```
├── GUIDANCE.md          # 领域纪律，只在这个技能激活时进模型上下文
├── storage.py           # 独立存储
├── scheduler.py         # 后台定时任务
└── models.py            # 数据模型
```

包内模块用相对导入：

```python
from skills.<skill_name>.storage import get_store
```

`SKILL.md` 与 `GUIDANCE.md` 不是一回事。前者给人看、给生态读，不进模型上下文，缺了不影响装载；后者进模型上下文，而且只在该技能被激活时进。

## 2. manifest.yaml

### 2.1 顶层字段

```yaml
kind: skill               # 固定值
name: <skill_name>        # 必须与目录名一致
version: "0.1.0"          # 语义化版本
description: >            # 一句话描述
  功能描述
```

### 2.2 可选字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `api` | int | 契约版本。框架不支持时不装载，并说明要求的版本 |
| `requires` | list[str] | 必须有的框架能力。缺任一项则不装载 |
| `optional` | list[str] | 希望有的框架能力。缺了照常装载，用 `ctx.has()` 走简版实现，装载报告里记一行 |
| `external` | list | 需要的外部程序。缺了照常装载，技能页显示缺什么。见 §3.5 |
| `settings` | object | 声明式配置。`x-secret: true` 的字段在界面打码 |
| `summary` | string | 技能卡上的短简介，与 `description` 分开 |
| `data_version` | int | 数据目录版本。更旧时调用 `migrate(ctx, old, new)`，更新时拒绝启动 |
| `store_version` | int | `ctx.store` 的存储版本，默认 1 |
| `review_input` | string | 值为 `none` 时自动模式不检查该工具的调用参数 |
| `draft` | bool | 值为 `true` 时不装载 |
| `load_strategy` | string | `on_demand`（默认）或 `eager` |

框架能力名：`artifacts` · `artifacts.inline` · `workspace` · `i18n` · `jobs` · `jobs.skill` · `context` · `storage` · `memory` · `llm` · `approval`。

### 2.3 tools

每个工具声明这些字段：

| 字段 | 必须 | 说明 |
|---|---|---|
| `name` | 是 | 必须与 `handler.py` 里的函数名一致 |
| `description` | 是 | 一句话说明用途，支持 YAML `>` 折叠 |
| `inputSchema` | 是 | JSON Schema 2020-12 |
| `permission` | 是 | `read` / `write` / `admin` |
| `destructive` | 是 | 布尔。改得回来吗 |
| `output_kind` | 读家族必须 | `prose` / `structured` |
| `concurrency_safe` | 否 | 布尔。不写则由 `permission` 派生 |

这四个字段都不上 wire：它们只在装载期决定调度与审批，不进模型看到的工具表，改它们不改变请求体积。

#### permission

| 档 | 含义 |
|---|---|
| `read` | 只查，不改任何东西 |
| `write` | 会改东西：写盘、改共享状态 |
| `admin` | 系统级 |

`read` 是唯一有实际约束力的一档：只读工具会被同批并发执行，也会被只读子 agent 放行。**把写工具写成 `read` 是危险的方向**，而且不会有任何报错。写错成 `write` 或 `admin` 只是少一次并发、多问一次，代价看得见。

拿不准就写 `write`。

#### destructive

只收布尔。`"true"`、`1`、`yes` 都不算声明。

判据一条：**结果里有没有一部分是调用者没要求的损失。**

- `write_file` 是 `false`：覆盖的是调用者点名的路径。
- `delete_files`、`send_email`、`run_command` 是 `true`：删了不会回来，发出去收不回，能做出没人要求过的事。

没声明这个字段的工具按 `false` 处理，也就是被当成可逆。装载时会记一行，体检会报出来，用 `create_skill` 造技能时这一项必须显式填 —— 但那都只是提醒，真正决定行为的是你写下的那个值。

拿不准就写 `true`。

#### output_kind

框架对工具返回的文本有一层不可信内容隔离：命中的先净化，再包上 `<user_document trust="untrusted">`。要不要隔离取决于这段文字可能出自第三人之手。

| 取值 | 含义 | 结果 |
|---|---|---|
| `prose` | 可能含第三人写的自由文本：网页正文、邮件、文档内容、命令输出 | 净化并加隔离标签 |
| `structured` | 装不下一条可执行指令：路径、计数、枚举、你自己拼的回执 | 不隔离 |

名字以 `get_` / `read_` / `search_` / `summarize_` / `parse_` / `list_` / `web_` 开头的工具必须声明。没声明按 `prose` 处置：装载放行并打一行警告，体检报 WARN，技能工厂直接拒绝生成。

声明解决的是「让隔离这道守卫继续有意义」，不是「让技能变安全」。恶意技能可以声明 `structured` 却返回网页正文；但它 import 之后本来就是进程内的 Python，隔离拦不住它。

一个 `structured` 要说得出为什么。说不出来就写 `prose`。

#### concurrency_safe

能不能和同批其它调用同时跑。判定次序：manifest 声明 → 框架显式表 → 由 `permission` 派生。

| 情形 | 结果 |
|---|---|
| `read` 且没声明 | 并发 |
| 其余档且没声明 | 串行 |
| 非法值 | 不登记，落回派生 |

并发错了的代价是共享状态串味，串行错了的代价只是慢。拿不准就写 `false`。

它只管同批 handler 能不能重叠。写工具本来就有别的闸，`true` 不会让它绕过任何一条。

### 2.4 inputSchema

```yaml
inputSchema:
  $schema: "https://json-schema.org/draft/2020-12/schema"
  type: object
  properties:
    <param_name>:
      type: string                # string / number / integer / boolean / array / object
      description: "参数说明"      # 必须，模型靠它理解参数
      enum: ["a", "b"]            # 可选
      default: "a"                # 可选
      items: {type: string}       # array 必须写
      minimum: 1                  # number / integer 可选
      maximum: 20
  required: ["<param_name>"]
```

`type` 只能是 object；`required` 里的名字必须在 `properties` 里定义；`$schema` 用 2020-12。

### 2.5 完整示例

```yaml
kind: skill
name: my_skill
version: "0.1.0"
description: "我的自定义技能 — 处理特定领域任务"

tools:
  - name: my_tool
    permission: write
    destructive: false
    output_kind: structured
    description: >
      工具功能的一句话描述。说明何时使用、输入输出格式。
    inputSchema:
      $schema: "https://json-schema.org/draft/2020-12/schema"
      type: object
      properties:
        text:
          type: string
          description: "要处理的文本内容"
        mode:
          type: string
          enum: ["fast", "thorough"]
          default: "fast"
          description: "处理模式"
        limit:
          type: integer
          minimum: 1
          maximum: 100
          default: 10
          description: "返回结果数量上限"
      required: ["text"]
```

## 3. handler.py

### 3.1 结构

```python
from core.security import SecurityGuard

_security: SecurityGuard | None = None


def init(security: SecurityGuard) -> None:
    """装载器在 import 之后调用一次。"""
    global _security
    _security = security


def my_tool(text: str, mode: str = "fast", limit: int = 10) -> dict:
    return {"success": True, "result": "..."}
```

`init` 的签名固定：接收一个 `SecurityGuard`，返回 `None`。用不到就 `pass`，签名不要改。它适合放三件事：保存 `SecurityGuard`、启动后台线程、初始化存储。

### 3.2 拿框架上下文

```python
from core.extension import get_context

def my_tool(text: str) -> dict:
    ctx = get_context("my_skill")            # 名字 = 目录名
    cfg = ctx.config                         # manifest.settings.storage 指定的那份，实时读
    ctx.store.set("last_run", 1700000000)    # data/runtime/skills/<名>/store.json
    job_id = ctx.jobs.start(lambda: work())  # 进统一任务表，kind = skill
```

- 每次调用返回新对象，`config` 每次访问重新读文件，改配置立即生效。
- 可写目录只有 `ctx.data_dir`，源码目录不可写。
- `ctx.config` 返回原样配置，可能含明文凭据。只用于实现，不要写进回执或日志。

### 3.3 给别的会话发消息

模型可以直接用框架工具 `send_outbound`，收件人格式 `platform:kind:id`，附件路径相对工作区。这是默认方式，不用写代码。

技能需要自己的协议时，在 `init()` 里注册出站动作：

```python
from core.outbound import register_outbound_action

def my_send(to: str, text: str = "", **kwargs) -> dict:
    return {"ok": True}

def init(security):
    register_outbound_action("my_send", my_send, description="发一条自定义消息")
```

审批通过后以 `handler(**params)` 调用，参数里带 `to`。同名动作重复注册会就地替换。注册返回一个撤销函数，在这次运行内有效。

### 3.4 后台服务

在 `handler.py` 里定义 `start(ctx)` 与 `stop(ctx)`，`start` 跑在守护线程里：

```python
def start(ctx):
    while not ctx.store.get("stop"):
        ...
        time.sleep(30)

def stop(ctx):
    ctx.store.set("stop", True)
```

每个技能只启动一次，框架重启或退出时调用 `stop`。启动异常记进装载报告，不影响其它技能。

数据目录带版本。启动前核对 `data_dir/.data-version`：更旧时调用可选的 `migrate(ctx, old, new)` 再启动，更新时拒绝启动并说明原因。

### 3.5 外部程序

技能要用的不一定是 Python 包，也可能是 `ffmpeg`、一个反编译器、一个 JAR，或者一个能在命令行驱动的桌面软件（`blender --background --python`、`afterfx -r script.jsx` 这类）。写进 manifest，框架就能回答「这台机器现在能做什么、还差什么」：

```yaml
external:
  - ffmpeg                                   # 只在 PATH 里找
  - name: ghidra
    env: GHIDRA_INSTALL_DIR                  # 环境变量指向一个存在的路径
    hint: 装 Ghidra 12.x 并设置 GHIDRA_INSTALL_DIR
  - name: jadx
    path: tools/jadx/bin/jadx                # 相对技能目录
  - name: mycli
    command: /opt/mycli/bin/mycli            # 指定可执行文件
```

```python
def my_tool(**kwargs):
    if not ctx.has_external("ffmpeg"):
        return {"success": False, "error": "本机未发现 ffmpeg；安装后重试"}
    ...
```

找法优先级：`command` > `path` > `env` > 按名字查 PATH。缺了不阻止装载；每次当场查，不缓存；技能页会显示缺什么，`hint` 进悬停提示。

### 3.6 只用稳定面

技能只应 import 这些模块：

`core.extension` · `core.workspace` · `core.artifacts` · `core.jobs` · `core.i18n` · `core.security` · `core.chunker` · `core.pdf_text_repair`

引用其它内部模块会被体检标成 WARN（不拒装），升级时那些模块可能变。

### 3.7 返回值

所有 handler 返回 `dict`：

```python
{"success": True, ...}                       # 成功
{"success": False, "error": "错误描述"}       # 失败
```

失败时 `error` 要写成模型读得懂的一句话。可以再加 `hint`，给它下一步的提示。

单条结果超过 50,000 字符时，框架会把完整内容写盘并回一个指针，handler 不用自己处理。工具若要自己裁剪，必须保住尾部：框架那把刀保的是头部。

### 3.8 文件操作

涉及路径的工具，在 `open()` 之前调 `SecurityGuard.validate_path()`：

```python
def _resolve_path(file_path: str) -> Path:
    p = Path(file_path)
    if p.exists():
        return p.resolve()
    alt = Path("workspace") / p.name
    return (alt if alt.exists() else p).resolve()

safe_path = _security.validate_path(str(_resolve_path(file_path)))
with open(str(safe_path), ...) as f:
    ...
```

## 4. SKILL.md

遵循 [agentskills.io](https://agentskills.io) 格式。

### 4.1 frontmatter

| 字段 | 必须 | 说明 |
|---|---|---|
| `name` | 是 | 连字符格式，匹配 `[a-z0-9][a-z0-9-]*[a-z0-9]` |
| `description` | 是 | 一句话，用于生态里的技能匹配 |
| `metadata.kind` | 是 | 固定 `skill` |
| `metadata.loader` | 是 | 固定 `manifest.yaml` |
| `metadata.tools` | 是 | 逗号分隔的工具名 |
| `metadata.mcp_note` | 否 | 生态适配说明 |

```markdown
---
name: my-skill
description: 一句话说明何时使用
metadata:
  kind: skill
  loader: manifest.yaml
  tools: my_tool, other_tool
---

# 我的技能

## When to use

## Tools

### my_tool

## Notes
```

### 4.2 命名

| 位置 | 格式 | 例 |
|---|---|---|
| Python 模块路径 | 下划线 | `skills.file_parser.handler` |
| 目录名 | 下划线 | `skills/file_parser/` |
| SKILL.md `name` | 连字符 | `file-parser` |

目录名用下划线是 Python 导入的要求，`name` 用连字符是 agentskills.io 的要求。这个差异由 `MCP_ADAPTATION.md` 描述的适配层处理。

### 4.3 allowed-tools

`SKILL.md` 与 `commands/*.md` 都可以写：

```yaml
allowed-tools: Read, Grep
```

名字可以用常见写法（`Read`、`Bash`、`WebFetch`），载入时转换成框架工具名。用这条命令时，这一轮只能调名单里的工具，下一条普通消息不受影响。不写就不限制。

### 4.4 GUIDANCE.md

这是唯一一个「只在该技能被激活时才进模型上下文」的载体。没有它，技能没有办法给调用者一句忠告：`description` 是每请求的索引税，`SKILL.md` 不进上下文，工具描述只能说工具本身。

**什么时候进**：该技能有任意一个工具被模型检索到。之后每条请求都把它挂在最后一条用户消息的尾部（副本，不进历史、不落库、不参与压缩）。没有激活，一个字都不挂。

代价要说清：模型如果不检索工具，这段文字一次都不会出现。

**写什么**：写「在这个领域，权威在哪」，不写「遇到某某就怎么做」。

| 该写 | 不该写 |
|---|---|
| 拿不准一个常量，去查官方文档、类型库、规范或源码 | PDF 乱码就试 `0x303E` |
| 先在副本上小范围试，再动原件 | 段前间距错了就改成 24 磅 |

场景映射换个任务就哑火，还会把技能锁死在那几个例子上。权威在哪是领域知识，只有作者知道。

**怎么写得合适**：

- 纯 Markdown，没有 frontmatter。
- 越短越好。它按每条请求计费，只在激活期间付。
- 上限 1,200 字符，超了截断并打印一行。
- 文件缺失或读不动不报错，只是这一轮不带领域片段。

框架会自带一段通用纪律（不确定先查、先小样本、别动原件），你只需要写领域那一半。

## 5. 自动发现与启用

`skills/<name>/` 只要满足三条就会被装载：有 `manifest.yaml`、`kind: skill`、`handler.py` 里定义了与 `tools[].name` 同名的函数。不需要改注册代码，创建目录即注册。

自己写好的目录放进 `skills/` 直接生效。模型用 `create_skill` 造出来的包带 `draft: true`，不会被装载，要有人在技能页按一下才生效 —— 这一条是给「让模型自己改自己」踩刹车用的，手写技能的作者不受影响。

技能页每张卡片右下角有启用与停用。

- **停用**：工具不进入模型的工具表，执行侧同时拒绝；指令型技能从模型索引与 `/` 命令里消失。
- **启用**：立即恢复，不需要重启。

停用不摘注册表。用户自己放进来的技能默认启用，不需要先按一次按钮。

## 6. 硬约束

1. **流式处理**：大文件逐页或逐行处理，不要一次性 `read()` 进内存。
2. **强制分块**：超过 `max_chars`（默认 1500）的文本用 `core.chunker.chunk_stream()` 分块。
3. **上下文隔离**：chunk 不进模型消息，只按检索结果暴露前几条。
4. **迭代聚合**：批量处理用自适应批量，装得下就走单次调用。
5. **安全核**：文件操作之前调 `validate_path()`。
6. **无密钥泄露**：密钥走环境变量或 `ctx.config`，不要硬编码，也不要写进回执。

## 7. 脚手架

```bash
python scripts/create_skill.py <skill_name> --desc "描述"
```

生成 `skills/<name>/` 四件套：`__init__.py`、`manifest.yaml`、`handler.py`、`SKILL.md`。`permission`、`destructive`、`output_kind` 会留成 `TODO` —— 它们没有安全默认值，得你自己填。

`--with-storage` 生成存储骨架，`--with-scheduler` 生成后台任务骨架，`--force` 覆盖已有目录。

示例只有这一份。仓库里不再存第二份手写的示例目录：两份东西迟早不一致，而照着过时那份抄出来的包，要到装载时才报错。
