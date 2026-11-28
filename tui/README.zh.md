# crannia-tui

简体中文 | [English](README.md)

crannia 的终端界面，**全屏**：中间为可滚动的对话区，底部输入框与状态栏固定；
启动抬头仅在呼出TUI时展示，会随内容滚动。

TUI 与桌面端共用同一个后端与同一份数据，因此当桌面端已在运行时，TUI 会直接接入它；
未找到已在运行的后端时会自启一个。

## 安装

### Windows

- **随桌面端安装包**：TUI会自动安装。打开一个**新的**命令提示符，定位至任意目录后键入 `crannia`以启用。
- **独立包**：解压后运行 `install.cmd`。TUI会装载至 `%LOCALAPPDATA%\Programs\CranniaTUI`，
  并将 `crannia` 放入已经在 PATH 上的 `%LOCALAPPDATA%\Microsoft\WindowsApps`
  （不更改注册表）。卸载时使用运行 `uninstall.cmd`；需要注意，用户的数据不在自动卸载范围内。

两种方案均使用时，`crannia` 会指向**后装**的条目；且卸载仅会删除指向自己的入口。

需要 Windows Terminal。cmd 的旧控制台不解释 ANSI，启动器检测到（`WT_SESSION`）时
会自动更换窗口唤起；`--once` 不会更换窗口。

### Linux

- **独立包**：解压 `crannia-tui-<版本>-linux-x64.tar.gz`，在解压目录运行
  `./install.sh`。装载至 `${XDG_DATA_HOME:-~/.local/share}/crannia-tui`，并把
  `crannia` 链接到 `~/.local/bin`（可使用 `CRANNIA_HOME` / `CRANNIA_BIN_DIR` 更改位置）。
  卸载需要运行 `uninstall.sh`；用户数据存放于 `${XDG_DATA_HOME:-~/.local/share}/crannia`，不会被卸载移除。
- **AppImage**：包中附带 TUI（`bin/crannia.sh`），但 AppImage 不安装、亦不会把命令放进
  PATH —— 需先 `--appimage-extract`，再运行 `squashfs-root/bin/crannia.sh`。

粘贴图片需要 Wayland 的 `wl-clipboard` 或 X11 的 `xclip`。中文界面需要终端字体内含有
CJK 字形（如 `fonts-noto-cjk`）

### 从源码运行

把 `tui/` 加进 PATH 即可：

```cmd
set PATH=%PATH%;<仓库>\tui
crannia
```

```bash
export PATH="$PATH:<仓库>/tui"    # Linux
crannia
```

源码在主仓的 `tui/` 里时无需额外配置：启动器会找到主仓的 `.venv`。自定义布局时建一个
`tui/.env`，填后端的解释器：

```
CRANNIA_PYTHON=D:\path\to\crannia\.venv\Scripts\python.exe
```

## 使用

```cmd
crannia                  进入界面
crannia --once "你好"     仅发送一句（输出可以重定向）
```

`Ctrl+C` 退出。

## 界面示例

```
                             ▄▄▄▄
                            █
                            █
                             ▀▀▀▀
                           Crannia v1.2.0
                       deepseek-flash | xhigh
  #- < . . . > / * { _ } - | #> = \ @ ( ) ) - < *** / [ _ ^ _ \ - < \ - > ; | · · · =
                          >_ D:\work\crannia    ← 抬头：启动时打印一次，随内容滚走

 【User】把第 7 页的价格带改成柱状图      ← 对话区，滚动
 ● 改写表格  价格表.docx                  ← 工具卡：绿=成功，红=失败，黄=正在运行
   ⎿  2,140 字符 · 1.2s                     失败时多一行，长栈折成"+N 行"
 ● 读取模板  template.pptx
   好的，先看一下现有版式。

 ⋯ 思考中   3s                             ← 状态行：跑到哪一步、多久
 ╭────────────────────────────────────────╮
 │  我可以怎么帮助您？                    │  ← 输入框
 ╰────────────────────────────────────────╯
 mode: 手动 · effort: high        2个后台  上下文：3%/85%
```

底栏：当前模式与档位、后台任务数与子代理数（仅在各自存在实例时可见）、
上下文占用 / 压缩阈值（接近阈值时变黄并提示）。

## 按键

| 键 | 作用 |
|---|---|
| 回车 | 发送 |
| Shift+Enter / Ctrl+J | 换行 |
| `/` | 命令表：↑↓ 选择、回车填入、Esc 收起 |
| 运行中回车 | 进入排队队列|
| Ctrl+V / 拖文件进终端 | 粘贴文本或文件路径 |
| Alt+V | 粘贴剪贴板内图片：光标处留 `【图片n】` 占位符，删掉即撤回 |
| Ctrl+B | 把前台任务转到后台（仅在提示可转入后台时可用） |
| 底栏存在任务时 ↓ | 焦点移到底栏：←→ 换格、回车看详细页、↑ / Esc 返回 |
| Esc | 中断当前轮（存在审批时不响应） |
| Ctrl+C | 退出 |

存在按键无反应时，尝试使用 `crannia --keys` 查看按键是否到达应用。

## 命令

### 命令行

以下命令不会拉起界面，仅作为查询命令

```
crannia setup                       配置模型 API（首次引导）
crannia doctor                      自检
crannia config path                 设置文件路径
crannia profile                     列出模型配置（<名字|id> 设为默认）
crannia update                      查看当前版本与用法
crannia channel list                列出各通道的配置条目
crannia channel add <平台> [名字]    新建一条（凭据逐格输入，密钥不回显）
crannia channel config <名字|id>     更改条目（回车表示不改）
crannia channel remove <名字|id>     删除条目（删除默认条目时需要优先更换默认）
crannia channel default <名字|id>    设为该平台的默认条目
crannia channel start|stop <平台>    启停一个通道（需要后端在线）
crannia channel login wechat         微信扫码登录（需要后端在线）
```

### 对话内

```
/model                 当前使用 + 前 8 条配置（含编号）
/model <编号|名字|id>   切换至目标条目（次轮生效）
/model 默认            切回跟随默认
/model more            完整配置单
/model set <编号|名字|id>
                       编辑面板：条目名 / 模型名 / API key
                       （空回车跳过，Esc 退出）
```


## 设置

`<数据根>/data/settings.json`，与桌面端共用；`crannia config path`
打印路径。TUI 首次启动会按出厂值播种（仅在文件缺失时写）；文件损坏时会优先备份成
`settings.json.bad-<时间>` ，随后按默认重建。

| 键 | 类型 | 作用 |
|---|---|---|
| `effort_level` | 整数 | 档位 1–6 |
| `default_mode` | 字符串 | 默认模式 |
| `approval_timeout_seconds` | 整数 | 审批超时（0 = 关闭） |
| `auto_approve` · `cross_zone_enabled` · `relax_command_tier` · `no_ask_readonly` · `full_access_unlock` | 布尔 | 审批与安全开关 |
| `interface_language` · `response_language` | 字符串或空 | 界面 / 回答语言 |
| `trace_full` | 布尔 | 完整轨迹 |
| `workspace` | 字符串 | 默认工作区 |
| `background_services` | 布尔 | 通道与定时任务是否随后端启动 |

## 工具卡与审批

工具卡调起时显示进行中的动作与参数

| 结局 | 圆点 |
|---|---|
| 已完成 | 绿 |
| 待审批 | 黄 |
| 失败 / 未执行 / 未采纳 | 白 |
| 被拒绝 / 自动审查拦截 | 红 |


审批发生时，输入框会变更为审批面板：使用数字或 ↑↓ 完成选择，
回车确认。或选择「我有别的话说」向模型解释您的想法。

## 图片

`Alt+V` 以粘贴剪贴板内图片（`Ctrl+V` 被终端占用）。粘贴对象会以
`【图片n】` 落在光标处，成为正文的一部分：删掉占位符即撤回对应图。命令（`/…`）
不带图；带图消息会直接进入排队队列

## 回复内链接

回复内的文件链接点击后即可在后端定位：按工作区根解析相对路径，在文件管理器内
**选中**目标文件（为目录则打开文件夹）；仅定位不执行。

## 断线与重连

步骤流断开时会出现红条并自动退避重连，并带上最后收到的序号
补发可重放事件；恢复后红条自动收起。


## 开发

- 目录结构、接口契约、渲染约定与界面探针见 `tui/` 下源码与 docstring。
- 测试：`.venv/Scripts/python run_tests.py`；仓库根内亦可运行
  `scripts/run_tests.py`
- 贡献流程见 [`CONTRIBUTING.md`](../CONTRIBUTING.md)。
