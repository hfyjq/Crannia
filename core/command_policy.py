# -*- coding: utf-8 -*-
"""执行命令的判定 —— 纯函数，不执行任何东西。

## 一、这个模块不做什么

不写命令解析器。解析器一旦判错，带 `ruleContent` 的 `deny`/`ask`
规则会一并失效；而模型仍能构造出解析器认不出的等价命令（子树 / 引号 /
变量展开）。不解析，默认全问，连"解析漏判"这个风险都不承担。
这里做的事只有一件：看这条命令的原文里出现了什么，然后决定它该走哪一档。

## 二、三档

| 档 | 含义 | 用户看到什么 |
|---|---|---|
| `TIER_DENY` | 确定性拒绝，不问 | "这类命令框架不执行，请改用 `web_fetch`" |
| `TIER_GUARDED` | 危险：每次都问，且不给前缀规则 | 命令审批表（提示语正文会说清为什么危险） |
| `TIER_ALLOW` | 普通：问，但可以记住 | 命令审批表（含升级项） |

GUARDED 档为什么没有前缀规则：判据是「批准一条 `git status *`」不能顺手
把 `rm -rf core/` 也放行。这一档只认整条精确匹配。

## 三、 判据：危险的是「目标路径」，不是「命令名」

要判的是这条命令碰到了哪个文件，不是它叫什么名字。
`rm -rf /` 那种静态字符串检测只能标着仅供参考，不影响权限逻辑与自动批准。

但我们不解析，怎么知道它碰了哪个文件？

改成"提到"（mention）而不是"碰到"（touch）。

    命令原文里出现了受保护路径的写法，升到危险档。

这不是妥协，是把误差方向掰到安全那边：

    · 误报（`grep -rn core/ .` 其实只是在读）—— 代价是多问一次
    · 漏报（`rm -rf core/` 没被认出来）—— 代价是围栏没了

同族判据：检测可以错，打码不能错。
这里换成了：升档可以错，降档不能错。

## 四、 它不是沙箱（写清楚，别让下一个人以为堵上了）

这一段只做两件事：**（1）从命令原文里认出受保护路径的提及；（2）认出
出网类命令名。** 它挡不住下面这些，而且没打算挡：

  1. 任何程度的字符串伪装。`cat ~/.clau''de/x` 靠"去引号视图"还认得出来，
     但 `cat ~/.cl$(echo ude)/x`、变量拼接、base64 之后 `eval` —— 一个都不认得。
它提供的性质是「老实的模型会被问」，不是「对抗的模型被关住」。
  2. bash 本身就是一条完整的出网通道。硬拒 `curl`/`wget` 只是堵住
     最直的那条路；`python -c "import urllib.request; ..."`、
     `pip install`、`npm i`、甚至 `git clone` 都能出网，且都不经过出网地板
     （那一层是 `web_fetch` 的，不是进程的）。
     真正的解法是 OS 沙箱（Linux/macOS 上就是 bwrap / seatbelt / landlock 那一套）。

     **准确的说法是「Windows 没有 bwrap/seatbelt/landlock
     那种内核级原语」**，而不是"做不了沙箱"：Windows 上确实有产品做过一套 ——
     用户态 API 挂钩（不是内核/驱动）+ 五层 shim（假 coreutils · `rm` 改成进回收站 ·
     ~30 个 fs API 钩子 · `sitecustomize.py` 钩 `os.remove`/`shutil.rmtree` ·
     Windows Job Objects 管进程树）。可学的是「按面扫一遍」这个动作，
     不是它的形状：用户态挂钩不是边界 —— 它自己的 fs shim 连接失败时
     静默退化成原生调用（fail-open），而任何不经过 shim 的进程直接绕过。
     Windows 上真有的内核原语是 Job Objects（进程树围栏 + 资源限制）
     与 AppContainer / WDAC（完整性级别），但没有一个是 bwrap 的等价替代。
结论不变（这边仍然只有"提及升档 + 询问"），但理由要写对：
     不是"平台上没有这条路"，是"**这条路在 Windows 上只能长成用户态挂钩，
     而它给不了我们要的那种保证**"。
  3. bash 的写权限不受 `validate_path` 约束。三级路径分级只管框架自己的
     file I/O，管不了子进程。`cwd` 被限制在工作区内（相对路径留在里面），
     但一条绝对路径就能出去；能拦住它的只有上面那个"提及"名单 + 询问。

## 五、判定的顺序不可重排

`deny` → `guarded`（含 `git`）→ 受保护路径 → `allow`。

这个顺序的判据是：「deny/ask 规则必须先于 path constraints」——
任何「先 allow 再补 deny」的重排都是漏洞。

另一半是allow 与 deny 的语义不对称 ——
`allow` 要求链上每一段命中（`every`），`deny`/`ask` 任一段命中即成立
（`some`）。本模块里 `DENY` 与 `GUARDED` 都是 "任一段命中即成立"。
"""

from __future__ import annotations

import re

__all__ = [
    "TIER_ALLOW", "TIER_GUARDED", "TIER_DENY",
    "PROTECTED_PATHS", "EGRESS_COMMANDS", "GUARDED_COMMANDS",
    "DELETE_COMMANDS", "BULK_DELETE_THRESHOLD",
    "classify_command", "mentions_protected_path", "path_fragments_in",
    "forbidden_dir_needles", "bulk_delete_reason",
    "NO_ASK_COMMANDS", "no_ask_reason",
    # 只用于回执里的收尾提醒（不参与判定）
    "LINGER_MENTIONS", "linger_reason",
]

# 三档

TIER_ALLOW = "allow"        # 普通：问，但可以记住（白名单 / 选项 5）
TIER_GUARDED = "guarded"    # 危险：每次都问，且不给「别再问」
TIER_DENY = "deny"          # 确定性拒绝，不问

# 受保护路径（判据见模块 docstring 第三节）
#
# 三条入名单的理由，每条都要说得出为什么
# （一个要人记得改的名单会烂，所以每条都得有读者能看懂的理由）：
#
#   A. 能拆掉围栏的东西 —— 改它 = 以后所有围栏失效
#   B. 我们的东西（见清单里那段 ）
#   C. 密钥与凭据
#
# 一律小写、一律用正斜杠。匹配在归一化之后进行（见 `_views`）。
# 末尾带 `/` 的条目表示"目录"，匹配时不检查后一个字符
#    （否则 `data/memory` 会因为后面跟着 `m` 而被判不中）。
PROTECTED_PATHS: tuple[str, ...] = (
    # A. 能拆掉围栏的
    # 框架自己的源码：下一次请求就会执行它。改 `core/security.py`、
    # `core/allowlist.py`、`bus/registry.py` 之后，所有闸门都是摆设。
    "core/",
    # `data/` —— 一条顶五条：`data/allowlist.json`（白名单自己）、
    # `data/settings.json`、`data/memory/`、`data/events/`（事件日志）。
    # 不逐条枚举 —— 枚举会烂（判据同"分类，不枚举"）。
    # 代价是 `data/workdata/` 的读回也被升档，接受。
    "data/",
    "logs/",              # 审计日志（擦掉痕迹）
    # 外部 agent 工具的配置：`settings.json` 里能写放行规则
    ".claude",
    # B. 我们的东西
    # 指令文件。全仓其余命中全是注释，没有一处是"读" ——
    # bash 是第一条能真读它的路径。
    "claude.md",
    # 上面那条 `.claude` 同时覆盖 `~/.claude/projects/**/memory/`
    # （我们的会话记忆）—— 它就在 `.claude` 下面，不必另写一条。
    # C. 密钥与凭据
    # `.env` 的载荷是实的：`load_dotenv(override=True)`，子进程默认能拿到
    # 全部 7 个键。另一个口子在 `core/shell.py::clean_env()` ——
    # 那个才是主防线，这条只是"提到了就别想自动放行"。
    ".env",
    # 版本控制与 shell 的启动 / 配置文件 —— 它们不是"内容"，是下次会执行的东西：
    # `.git/config` 能挂 hook、改 remote；`.bashrc` / `.profile` 下次开 shell 就执行；
    # `.ripgreprc` 能改动 `rg` 的参数；`.mcp.json` / `.claude.json` / `.vscode` /
    # `.idea` 是别的 agent 工具与 IDE 的运行配置。
    # 判据与 A 类同一条（改它 = 改将来的行为），只是改的不是我们的围栏。
    ".git",
    ".gitconfig", ".gitmodules",
    ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile",
    ".ripgreprc",
    ".mcp.json", ".claude.json",
    ".vscode", ".idea",
    # 同族补充（同一个判据的其余成员）
    ".ssh",               # 私钥 + `authorized_keys`（放进去等于开后门）
    ".gnupg", ".aws",
    # C-2. 按工具族扫一遍
    #
    # 名单要按族枚举 ——
    # 云 CLI / 包管理器 / 版本控制凭据各扫一遍，而不是凭印象挑几个显眼的。
    # 只放 `.aws` / `.gnupg` / `.ssh` 就属于后者：同一族的其余会全漏。
    #下一批照着族加，别再凭印象。
    #
    # 判据是「提及即升档」，补名单的代价是多问一次，不是误伤；
    #    而漏掉的代价是 `cat ~/.gcp/...` 落在 allow 档、不问 ——
    #    误差方向必须朝"问"那边倒（同族判据：检测可以错，打码不能错）。
    # 一律不带末尾 `/` —— 带斜杠的条目匹配不到"只提了目录名"那种写法
    #    （见 `_mentions` 的末尾斜杠规则），而这些名字文件和目录都可能是。
    #    代价见下：`.docker` 会连 `.docker-compose.yml` 一起升档 —— 接受。
    #
    # 族 1｜云 CLI（拿的是云账号的操作权限）
    ".gcp", ".azure", ".config/gcloud", ".kube", ".terraform.d",
    # 族 2｜包管理器 / 容器（registry token 一泄漏 = 供应链）
    ".npmrc", ".pypirc", ".gem", ".docker", ".cargo",
    # 族 3｜版本控制凭据
    ".git-credentials", ".config/gh",
    # 族 4｜数据库客户端（`.pgpass` 是明文口令表）
    ".pgpass", ".my.cnf",
    # 族 5｜通用凭据
    ".netrc", ".gpg",
)


# 用户配的「规避目录」
#
# `PROTECTED_PATHS` 那 38 条是出厂的、写死的。而 `SecurityGuard` 上还有
# 一张活的表（`forbidden_dirs`，设置页可加「规避目录」）——
# 它在工具侧是真的执行的（命中，`classify_path` 判 `FORBIDDEN`，
# `validate_path` 任何 op 都抛：读不到、写不了、也删不掉）。
#
# bash 也必须读它，否则形状是：
#
#   | 模型怎么删 | 结果 |
#   |---|---|
#   | `delete_files` | ✅ 删不掉（`validate_path` 抛） |
#   | `run_command` → `rm -rf /d/重要资料/…` | ❌ 出厂那 38 条里没有它，不升档，落 `allow` 档，第一次问、第二次静默删 |
#
#这正是「bash 的写不受 `validate_path` 约束」的后果：
#   用户划了一条线，工具老老实实守着，命令行从那道线旁边绕过去。
#
# 不新造正则表：不写命令解析器 —— 那条路走不通
#   「解析失败时 deny/ask 一并失效」。照旧只做"提到就升档"。
#
# 一条路有两种写法，所以一条目录要出两张 needle：
#
#     `D:\重要资料`  →  `d:/重要资料`（正斜杠）  与  `/d/重要资料`（MSYS / Git-Bash）
#
# 命令原文的视图①只把 `\` 换成 `/`，不会把 `D:/` 变成 `/d/`，少一张就漏一半
# （而误差方向必须朝"问"倒，不是朝"漏"倒）。两张都留的代价只是多问一次。
#
# 不做的事：不展开 `~`、不解析相对路径、不做 glob —— 那些都是解析，
#   而解析正是这一整个模块不干的事（`_views` 那两个视图就是全部的归一化）。

_DRIVE_RE = re.compile(r"^([a-z]):/(.*)$")


def forbidden_dir_needles(paths) -> tuple[str, ...]:
    """把规避目录（`SecurityGuard.forbidden_dirs`）变成匹配用的 needle。

    纯函数、可复现（判据见上）。只归一化写法，不碰语义 ——
    空串丢掉、去重、保持调用方给的次序。
    """
    out: list[str] = []
    for raw in paths or ():
        p = str(raw or "").strip().lower().replace("\\", "/")
        if not p:
            continue
        if p not in out:
            out.append(p)
        m = _DRIVE_RE.match(p)
        if m:
            alt = "/%s/%s" % (m.group(1), m.group(2))
            if alt not in out:
                out.append(alt)
    return tuple(out)


def path_fragments_in(command: str, extra_protected=()) -> list[str]:
    """这条命令提到了哪些受保护路径。返回命中的条目（表里的原样）。

    纯函数、可复现。判定与呈现共用它 —— 危险档的理由要能说出"因为它提到了
    `data/`"，而不是一句"系统认为这很危险"。

    *extra_protected* 是活的那张表（`SecurityGuard.forbidden_dirs`）。
    两张表走同一套匹配（`_views` + `_mentions`），用户配的一条与出厂的一条
    行为完全一样；分开写两份匹配才是真会漂移的那种写法。
    """
    views = _views(command)
    needles = list(PROTECTED_PATHS) + list(forbidden_dir_needles(extra_protected))
    return [p for p in needles if any(_mentions(v, p) for v in views)]


def mentions_protected_path(command: str, extra_protected=()) -> bool:
    return bool(path_fragments_in(command, extra_protected))


# 会一直活着的东西 —— 只用来在回执里提醒收尾，不参与任何判定
#
# 为什么是"提醒"而不是"问"：这几种的危害在「没停」，不在「开始」。
#   拦在开始那一端拦不到"忘了关"，而且一千条真实命令里一次没出现过
#   （问了也没人可问）。所以它不进 `deny` / `guarded`，也不进分类器 ——
#   分类器只判"只有模型才判得出来的东西"，而"这条命令里有没有 ngrok"
#   是字符串匹配。
#
# 为什么只有这两类：改配置里的地址、自己写一个能被远程执行的入口，
#   提及式认不出来（`python app.py` 和任何脚本一模一样）。那几条
#   明确没做，记在待办里，不要在这里硬凑 —— 凑出来的会是噪声。
LINGER_MENTIONS: tuple = (
    (("http.server", "flask run", "uvicorn", "streamlit run",
      "jupyter notebook", "gradio"),
     "起了一个本地服务"),
    (("ngrok", "cloudflared", "localtunnel", "tailscale funnel", "chisel"),
     "开了一条通往公网的隧道"),
)


def linger_reason(command: str) -> str:
    """这条命令会不会留下一个不自己退出的东西？返回一句人话，没有就空串。

    纯函数、零 IO、可复现。调用方只在执行之后问它 —— 没跑起来的东西
    没有收尾可言。匹配与受保护路径同一条（`_views` + `_mentions`），
    所以 `xngrok` 不会命中。
    """
    views = _views(command)
    for needles, why in LINGER_MENTIONS:
        for n in needles:
            if any(_mentions(v, n) for v in views):
                return why
    return ""


# 出网类命令（硬拒）
#
# 为什么是硬拒而不是"问"：出网地板（`check_url_egress`）是
# 确定性的 —— "非公网目标没有任何正当用途，不问用户，直接拒"。
# 若 `curl` 只走"问"，那么用户点一次同意就把确定性降级成了策略，
# 而地板恰好是给"用户看不清目标"那种情况准备的。
# 判据：「出网类命令硬拒（`curl`/`wget`/`ssh`/`scp`，否则出网地板被绕）」。
#
# 代价是有正当出路：模型要用网络就调 `web_fetch` —— 那条路走地板 + 闸门。
# 所以这不是"封印能力"，是"把网络访问收进同一条通道"。
EGRESS_COMMANDS: frozenset[str] = frozenset({
    "curl", "wget", "wget2", "aria2c", "lftp", "cadaver",
    "ssh", "scp", "sftp", "ftp", "telnet",
    "nc", "netcat", "ncat", "socat",
    "rsync", "rclone",
})

# 单独一档的命令
#
# `git` 会改仓库状态：`reset --hard` / `clean -f` / `checkout .` 是不可逆的。
# 有的实现把它们做成提示（"仅供参考"），这里做成问。
#
# 不只是"危险"那一面：`git` 还能出网（`clone` / `fetch` / `push`）。
# 危险档"不给别再问"这条性质顺带把它一起管住了 —— 这也是为什么它单独一档
# 而不是并进 `EGRESS_COMMANDS`：出网的那几个是拒，`git` 是问。
GUARDED_COMMANDS: frozenset[str] = frozenset({"git"})


# 删除保护：大批（>30）删除时固定询问
#
# 这是"提及升档"那条判据的第二次落地（第一次在 `PROTECTED_PATHS`）：
#   不解析命令，所以判不了"它到底删了几个"，只能数它写了几个要删的东西。
#   误差方向照旧朝"问"那边倒 —— 见下面三条刻意多问的规则。
#
# 名单只收不可逆的那几个：走回收站的（`trash` / `trash-put` / `gio trash`）
#   不收 —— 它们删得回来。
#名单一旦混进它们，"删除保护"会去护一个本来就不危险的动作。
#
# 回收站只管框架自己的文件删除（`core/recycle.py`，只有
#   `skills/file_parser/handler.py::delete_files` 走它）。
#   `rm` 留在名单里 —— `run_command` 那条路
#   压根不经手回收站（bash 的写权限不受 `validate_path` 约束）。
#别把这两件事读成一件：一个走系统 API、一个走命令行；前者可还原，
#   后者还是永久删。"有回收站了所以 `rm` 可以放行了"是错的。
DELETE_COMMANDS: frozenset[str] = frozenset({
    "rm", "rmdir", "rd", "del", "erase", "unlink", "shred", "srm",
})

#: 超过几个就每次都问（`TIER_GUARDED` = 只认整条精确匹配，不给前缀规则）。
BULK_DELETE_THRESHOLD = 30

#: 通配符 —— 出现在操作数里就意味着"几个"是展开之后才知道的（我们不展开）。
_GLOB_CHARS = "*?["


# 归一化与匹配
#
# 命令原文要按两种方式各归化一遍，任一个视图命中就算命中：
#
#   ① 路径视图 —— `\` 换成 `/`：`data\memory` → `data/memory`（Windows 写法）
#   ② 去引号视图 —— 删掉 `\ ' " ` $ { } ^`：`.clau''de` → `.claude`
#
# 为什么两个都要：`\` 在两个视图里含义相反 ——
# 当作路径分隔符时该换成 `/`，当作转义符时该删掉。
# 只做一个必然漏一半，而两个都做只是多问一次（误差方向仍然是安全的）。

#: 去引号视图里要删掉的字符。`^` 是 cmd.exe 的转义符，`$`/`{`/`}` 挡 `${VAR}` 写法。
_DEQUOTE = str.maketrans({c: "" for c in "\\'\"`${}^"})


def _views(command: str) -> tuple[str, str]:
    """命令原文的两个归一化视图（都转小写）。"""
    low = (command or "").lower()
    return low.replace("\\", "/"), low.translate(_DEQUOTE)


def _mentions(view: str, needle: str) -> bool:
    """*needle* 在 *view* 里作为一个路径片段出现。

    为什么不能直接 `needle in view` —— 那是子串不是片段：

        `data/`  ⊂  `metadata/`        ← 假命中
        `.git`   ⊂  `.github/`         ← 假命中（而且 `.gitignore` 同族）

    规则：前后两边都不能紧挨着字母/数字/下划线。

    例外：以 `.` 开头的条目不看前一个字符。因为点开头的名字
    本身就是个后缀：`foo.git`（worktree 的裸库目录）、`prod.env`、
    `backup/.ssh` 都该命中，而"前一个字符是字母"这条会一个不漏地把它们放过去
    （`cat foo.git/config` 落到 allow）。放开的代价只是多问一次 ——
    `x.gitignore` 仍然被后一个字符那条挡住。

    末尾带 `/` 的条目不检查后一个字符 —— 它本身就是个目录前缀，
    `data/memory` 里 `data/` 后面当然跟着 `m`。
    """
    n = len(needle)
    dot_entry = needle.startswith(".")
    start = 0
    while True:
        i = view.find(needle, start)
        if i < 0:
            return False
        before = view[i - 1] if i else ""
        after = view[i + n] if i + n < len(view) else ""
        ok_before = dot_entry or not (before.isalnum() or before == "_")
        ok_after = (needle.endswith("/")
                    or not (after.isalnum() or after == "_"))
        if ok_before and ok_after:
            return True
        start = i + 1


# 切词（这不是解析器）
#
# 只做两件事：按 shell 元字符切段、把段首的词认出来。
# 不做引号配对、不做变量展开、不做重定向、不做语法树 —— 一件都不做。
#
# 切不干净的方向是安全的：一个词没被认成"段首"，
# 它就落不到 `deny`（硬拒）那一档，而是落到"出现在别处，guarded"（问）。
#包装剥不干净 = 多问一次，而不是少拦一次。

#: 段分隔符：命令连接符 + 子 shell + 命令替换 + 换行。
_SEGMENT_SPLIT = re.compile(r"[;|&\n\r(){}`]+")

#: 段首可能出现的包装词（剥掉之后才看真正的命令名）。
_WRAPPERS = frozenset({"sudo", "env", "nohup", "time", "command", "exec", "nice"})

#: 工具链的官方安装入口（精确匹配主机名，不做后缀 —— 否则
#: `sh.rustup.rs.evil.example` 会被误放行）。
_TOOLCHAIN_HOSTS = frozenset({
    "sh.rustup.rs", "bootstrap.pypa.io", "astral.sh", "bun.sh",
    "deb.nodesource.com", "get.docker.com", "brew.sh",
})

_FETCHERS = ("curl", "wget", "wget2")


def _head_is_fetcher(words: list[str]) -> bool:
    """段首（跳过环境赋值与不含选项的包装词）是不是 curl / wget。

    带选项的包装（`sudo -u root curl …`）一律不算 —— 那要解析选项吃几个参数，
    而我们不解析命令。误判方向朝"拒绝"。
    """
    i = 0
    while i < len(words):
        w = _basename(words[i])
        if _ASSIGN.match(words[i]):
            i += 1
            continue
        if w in _WRAPPERS:
            nxt = words[i + 1] if i + 1 < len(words) else ""
            if nxt.startswith("-"):
                return False
            i += 1
            continue
        return w in _FETCHERS
    return False


def _official_toolchain_installer(words: list[str]) -> bool:
    """这一段是不是"从官方入口取安装器"。判据：取的人对、全部 URL 都对。"""
    if not _head_is_fetcher(words):
        return False
    from urllib.parse import urlsplit
    urls = [w for w in words if "://" in w]
    if not urls:
        return False
    for raw in urls:
        try:
            u = urlsplit(raw)
        except ValueError:
            return False
        if u.scheme != "https":
            return False
        if (u.hostname or "").lower() not in _TOOLCHAIN_HOSTS:
            return False
    return True

#: `VAR=value` 形式的环境变量赋值（可以出现在段首）。
#: 大小写都要认 —— `X=1 curl …` 里的 `X` 是大写；只写 `[a-z_]` 时
#: 它没被当成赋值、`X=1` 被当成了命令名，`curl` 落到"出现在别处"那一圈
#: 变成 guarded（降了一档）。
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _words(segment: str) -> list[str]:
    """把一段切成词，去掉引号并去掉空词。"""
    out: list[str] = []
    for w in segment.split():
        w = w.translate(_DEQUOTE)
        if w:
            out.append(w)
    return out


def _segments(command: str) -> list[list[str]]:
    """`[[词, ...], ...]` —— 每个非空段一个词表。"""
    segs = [_words(s) for s in _SEGMENT_SPLIT.split(command or "")]
    return [s for s in segs if s]


def _basename(word: str) -> str:
    """`/usr/bin/curl` → `curl`；`C:/x/curl.exe` → `curl`。"""
    w = word.replace("\\", "/").rsplit("/", 1)[-1]
    if w.endswith(".exe"):
        w = w[:-4]
    return w


def _command_candidates(words: list[str]) -> list[str]:
    """这一段里可能是命令名的词。

    两种情形，判据是"段首能不能被信任"：

    · 段首没有被包装（`echo curl`、`ls -la`）—— 只认第一个词。
      精确，所以 `echo curl` / `grep curl .` 不会被当成 curl。

    · 段首是包装词或环境变量赋值（`sudo …` / `env …` / `X=1 …`）——
      整段的词都算候选。理由：包装词后面跟着什么是解析问题
      （`sudo -u root curl …` 里 `-u` 吃掉了 `root`；`env -i A=b curl …`
      同理），而我们不解析。
      代价是 `sudo echo curl` 也会判 deny —— 那是个构造出来的形状，很少见；
      换来的是 `sudo -u root curl …` 一定 deny。
误差方向仍然朝"多拦"，不是"少拦"。

    一个真实的退化形状：只剥"紧跟的短选项"时，
    `sudo -u root curl` 的段首会被认成 `root`，掉到 guarded（降了一档）。
    """
    if not words:
        return []
    head = _basename(words[0])
    wrapped = head in _WRAPPERS or bool(_ASSIGN.match(words[0]))
    if wrapped:
        return [_basename(w) for w in words]
    return [head]


# 唯一入口

def _is_recursive_flag(word: str) -> bool:
    """`rm -r` / `rm -R` / `rm -rf` / `rm --recursive` / `del /s` 都算递归。

    两个坑（直接拿 `{"-r","-R"}` 比字符串会同时踩到）：

      · 短选项是能合并的 —— `rm -rf x` 里那个词是 `-rf`，
        既不等于 `-r` 也不等于 `-R`，整个漏掉，
        而 `rm -rf` 正是最常见的形状（漏了 = 这条保护在最该响时不响）。
      · `/s` 不能走 `_basename` —— 那个函数按 `/` 切目录，
        `_basename("/s")` 得到的是 `s`（在 Windows 上它把 `/s` 当成根目录下的文件）。
        反过来说：`/x` 形状与绝对路径（`C:/tmp`）在这一层分不开 ——
        这里不解析。误差方向朝"多问"倒（一个 `/q` 被当成操作数没什么代价）。

    只在已经确认是删除命令的段里调它，`ls -R` 那种不会被误判。
    """
    w = (word or "").strip()
    if not w:
        return False
    if w.lower() in ("--recursive", "/s"):
        return True
    # 短选项簇：`-rf` / `-r` / `-Rf` …… 只在单横线且不是 `--` 长选项时按字符看
    if w.startswith("-") and not w.startswith("--") and len(w) > 1:
        return any(ch in "rR" for ch in w[1:])
    return False


def bulk_delete_reason(segs: list[list[str]]) -> str:
    """这条命令是不是大批删除；是，给一句理由，否，空串。

    一条判据，四条落地：能数出来、且 ≤ 阈值，放行；数不出来、或 > 阈值，问。

      | # | 判据 | 为什么算 |
      |---|---|---|
      | 1 | 操作数 > `BULK_DELETE_THRESHOLD` | 字面就是"大批" |
      | 2 | 操作数里有通配符 | `rm *` 删几个要展开才知道 —— 这里不展开 |
      | 3 | 段里有递归开关 | `rm -rf 目录` 一个操作数，删的是一整棵树 |
      | 4 | 删除命令名不在段首 | `xargs rm` / `find … -exec rm` —— 数目在 stdin / `find` 手里 |

    后三条是刻意的多问（照 `PROTECTED_PATHS` 那条：`.docker` 连
    `.docker-compose.yml` 一起升档 —— 接受）。理由：**它们恰恰是最常见的
    "大批删除"形状**，漏掉它们 = 这条保护在最该响的时候不响。
    代价如实说：`grep rm .` / `echo rm` 这类也会被问（第 4 条）。
    与 ④ 那条 `xargs curl` 逐字同形 —— 那一档接受了同样的代价。

    数的是"写了几个"，不是"删了几个" —— 这里不解析、不展开。
    `find . -delete` / `python -c "shutil.rmtree(...)"` 这两类
    数不到（它们的命令名不在 `DELETE_COMMANDS` 里）：那是本条的已知残余，
    不是缺陷 —— 补它们只能靠解析，而解析这一条路已知实现都没走通。
    如实标，不假装覆盖了。
    """
    total = 0
    glob_seen = False
    recursive_seen = False
    indirect_seen = False
    for seg in segs:
        cands = _command_candidates(seg)
        if not any(c in DELETE_COMMANDS for c in cands):
            # 第四条：删除命令名出现在非段首（`xargs rm` / `find … -exec rm`）
            #   —— 要删几个数不出来（数目在 stdin / `find` 手里）。
            #   与 ④ 那条（`xargs curl`）逐字同形，代价也一样：
            #   `grep rm .` 会多问一次。接受 —— 见下面 。
            if any(_basename(w) in DELETE_COMMANDS for w in seg[1:]):
                indirect_seen = True
            continue
        # 数操作数：跳过选项词，但 `--` 之后一律算路径
        # （`rm -- -weird-name` 里那个 `-weird-name` 是一个真文件）。
        after_terminator = False
        for w in seg[1:]:
            if not after_terminator and w == "--":
                after_terminator = True
                continue
            if not after_terminator and w.startswith("-"):
                if _is_recursive_flag(w):
                    recursive_seen = True
                continue
            # `/s` 那类 cmd.exe 开关不以 `-` 开头，所以必须在计入操作数之前
            #    单独认一次（认出来就不算操作数，也不算通配符）。
            if not after_terminator and _is_recursive_flag(w):
                recursive_seen = True
                continue
            total += 1
            if any(ch in w for ch in _GLOB_CHARS):
                glob_seen = True

    if not (total > BULK_DELETE_THRESHOLD or glob_seen or recursive_seen
            or indirect_seen):
        return ""
    why = []
    if total > BULK_DELETE_THRESHOLD:
        why.append("一次要删 %d 个（超过 %d 个）" % (total, BULK_DELETE_THRESHOLD))
    if glob_seen:
        why.append("用了通配符（`*` / `?` / `[`）—— 要删几个得展开才知道，"
                   "而框架**不展开**")
    if recursive_seen:
        why.append("带了递归开关（`-r` / `-R` / `--recursive`）—— "
                   "一个操作数可能是一整棵树")
    if indirect_seen:
        why.append("删除命令**不在段首**（`xargs rm` / `find … -exec rm` 那种）"
                   "—— 要删几个在别人手里，框架数不出来")
    return "；".join(why)


def classify_command(command: str, *,
                     relax_guarded: bool = False,
                     extra_protected=()) -> tuple[str, str]:
    """这条命令该走哪一档。返回 `(tier, reason)`。

    *reason* 是给用户看的一句话 —— 它进审批面板的 `detail` 字段。
    危险档必须说得出为什么危险：一句"系统认为这很危险"会让用户
    要么盲目点同意、要么干脆不用（两种都是失败）。

    *extra_protected* 是活的那张规避目录表（`SecurityGuard.forbidden_dirs`）
    —— 出厂那 38 条写死在 `PROTECTED_PATHS` 里，这一份是用户在设置页
    自己划的（外加 `DEFAULT_FORBIDDEN_DIRS` 那几个系统目录）。
    读不到就传空，只保留出厂名单（出厂那 38 条一个字都不受影响）。

    *relax_guarded* 是设置页那个「关闭安全询问」开关：
    打开时②③④⑤ 四档降级成 `allow` —— 仍然会问，只是问完之后可以记住。

    它不碰 ①（`deny`）。出网类是确定性拒绝：连问都不问的东西，
    没有"关掉询问"可言。一个开关不能把地板关掉。
    降级不是取消 —— 「危险命令降级成普通档
    （走同一条梯子），不是"不问"」。区别在第一次仍然会问。
    `allow` 档的 *reason* 恒为空串（调用方靠它判"要不要说点什么"）——
    所以降级之后也是空串，"这条本来会问"那件事由调用方自己再判一次
    （`core/shell.py::run_command` 就是这么如实告诉用户的）。

    顺序不可重排（见模块 docstring 第五节）。
    """
    raw = (command or "").strip()
    if not raw:
        return TIER_DENY, "命令是空的。"

    segs = _segments(raw)
    if not segs:
        return TIER_DENY, "命令是空的。"

    # ① deny：段首命令名命中出网类，不问，直接拒
    # 这一段在 `relax_guarded` 之外 —— 见 docstring 那两条 。
    for seg in segs:
        egress = [n for n in _command_candidates(seg) if n in EGRESS_COMMANDS]
        if not egress:
            continue
        # 工具链的官方安装入口是唯一的例外：段首是 curl/wget 且所有 URL
        #   都是 https + 官方主机名，放进审批/分类器那一层（不是免问）。
        #   其余一律确定性拒绝（SSH/scp/任意站点/混合 URL 都还在这一档）。
        if _official_toolchain_installer(seg):
            continue
        name = egress[0]
        return TIER_DENY, (
            f"`{name}` 是出网类命令，框架不执行它 —— "
            f"这一档是**确定性拒绝**（不问），因为出网地板"
            f"（非公网目标一律拒）只对框架自己的联网工具生效，"
            f"放开 `{name}` 等于把地板绕过去。"
            f"要用网络请改用 `web_fetch`。"
        )

    if relax_guarded:
        return TIER_ALLOW, ""

    # ② guarded：段首命令名是 `git`
    for seg in segs:
        for name in _command_candidates(seg):
            if name in GUARDED_COMMANDS:
                return TIER_GUARDED, (
                    f"`{name}` 会改动仓库状态（`reset --hard` / `clean -f` / "
                    f"`checkout .` 不可逆），也能出网。这一档**每次都问**，"
                    f"不提供「本次对话内不再询问」。"
                )

    # ③ guarded：命令原文里提到了受保护路径
    hits = path_fragments_in(raw, extra_protected)
    if hits:
        # 理由要说得出哪一张表 —— 用户划的那条线被提到了，
        #   却只回一句"框架自己的源码/密钥"，用户会以为误报
        #   （标签在说谎）。两张表都中时如实说两张。
        mine = set(forbidden_dir_needles(extra_protected))
        user_hits = [h for h in hits if h in mine]
        if user_hits and len(user_hits) < len(hits):
            who = "其中**有你划的规避目录**，也有框架自己的源码/数据/密钥"
        elif user_hits:
            who = "**是你划的规避目录**（设置页 → 规避目录）"
        else:
            who = "是框架自己的源码/数据、密钥、或仓库配置"
        return TIER_GUARDED, (
            f"命令里出现了受保护路径：`{'`、`'.join(hits)}` —— {who}。"
            f"**不解析命令**（不判断它到底是在读还是在改），"
            f"提到就升档：这一档每次都问，不提供「本次对话内不再询问」。"
        )

    # ④ guarded：出网类命令名出现在非段首（`xargs curl` 那种）
    for seg in segs:
        for w in seg[1:]:
            if _basename(w) in EGRESS_COMMANDS:
                return TIER_GUARDED, (
                    f"命令里出现了出网类命令 `{_basename(w)}`（不在段首，"
                    f"可能是 `xargs` / 变量拼接一类）。升到危险档问一次。"
                )

    # ⑤ guarded：大批删除
    # 放在 ④ 之后、`allow` 之前。在 `relax_guarded` 之下是有意的：
    #   那个开关的标签逐字是「关闭安全询问」，用户点了它 = "别再拿安全问我"，
    #   而这条也是一条安全询问。让它在开关关掉后仍然拦，就是标签在说谎。
    #   `deny` 那一档不受影响，理由不同（那条是确定性的，
    #   没有"问"可言 —— 见 ① 上面那段）。
    bulk = bulk_delete_reason(segs)
    if bulk:
        return TIER_GUARDED, (
            f"这会是一次**大批删除**：{bulk}。"
            f"这一档**每次都问**，不提供「本次对话内不再询问」—— "
            f"一次删掉的东西越多，越值得看一眼再说。"
        )

    # ⑥ allow
    return TIER_ALLOW, ""


# 免问集 —— 审批疲劳的另一半
#
# 它补的是什么：把"这一类不用问"划出来。
# ⇔ 它与"一次意图两次浮窗"不是同一件事：那条在"问得更聪明"
#   上打转，这一条补的是"这一类压根不用问"。
#
# 为什么它是独立函数、不是 `classify_command` 的第四档：
# 那张梯子的顺序是承重的（`deny → guarded → allow`，见模块抬头第五节），
# 而免问集是一条疲劳策略，不参与危险判定。插进梯子里会让下一个读的人
# 以为"它跟 `deny` 是一类东西"。
#
# 判据（保守），两条都要满足：
#   ① 命令名的白名单（不解析）；
#   ② 整条命令里不含任何"能重定向 / 能取变量"的字符。
#误差方向朝"多问"：`cat a b > c` 的 `cat` 明明在白名单里，
#   而 `>` 把整条踢出去。结构上不可能漏写文件。
#代价如实记：`cat a | grep b` 这种也照问（管道是分段符）。
#
# 这张表里不许有"带一个执行参数"的命令 —— 这是本条最该记住的一句。
#   典型反例 `find`：`find . -exec rm {} +` 不含任何被禁字符
#   （`;` 有、`+` 没有），`find` 若在表里，它就会静默变成一条免问的删除。
#`find` 不在表里。凡是"看起来只读、但有一个能执行东西的兄弟参数"的
#     命令（`find -exec` / `xargs` / `awk system()` / `git`），一律不进。
#   同理不进的是读 stdin 会挂住的那几个（`sort` / `uniq` / `cut` / `nl`
#     —— 没有参数时它们等输入，免问之下没人看得见它在等）。
NO_ASK_COMMANDS: frozenset[str] = frozenset({
    # 列目录 / 看当前位置 / 看自己是谁
    "ls", "dir", "pwd", "tree", "which", "where", "whoami", "hostname", "date",
    # 看文件内容（只读）
    "cat", "type", "head", "tail", "wc", "file", "stat", "du", "df",
    # 在文件里找
    "grep", "findstr",
})

#: 出现任何一个就不进免问集。判的是整条命令，不是某一段。
#: 分段符（`; | & ( ) { }` 反引号 · 换行）由 `_SEGMENT_SPLIT` 管 ——
#: 它们会让段数 > 1，同样落入"照常问"。这里只剩重定向与取变量两类。
_NO_ASK_DISQUALIFIERS: tuple[str, ...] = (">", "<", "$")


def no_ask_reason(command: str) -> str:
    """这条命令能不能不问？能就返回一句给人看的理由，否则空串。

    纯函数、零 IO、不执行任何东西（与 `classify_command` 同一条纪律）。
    调用方还要自己判那个开关（`guard.no_ask_readonly`）——
    这一层只管"这条命令够不够格"，不管"用户要不要这个功能"（两件事）。
    """
    raw = (command or "").strip()
    if not raw:
        return ""
    # ① 重定向 / 取变量，一律出局（整条命令判，不看段）
    hit = [c for c in _NO_ASK_DISQUALIFIERS if c in raw]
    if hit:
        return ""
    # ② 只许一段 —— `cat a | grep b` / `ls && rm x` 都落这儿（照常问）
    segs = _segments(raw)
    if len(segs) != 1:
        return ""
    words = segs[0]
    if not words:
        return ""
    # ③ 段首必须就是那个命令名。包装词（`sudo …`）与变量赋值（`X=1 …`）
    #    天然出局 —— 它们的 `_basename` 不在表里（不需要额外判一次）。
    name = _basename(words[0])
    if name not in NO_ASK_COMMANDS:
        return ""
    return f"`{name}` 在**只读免问集**里（命令名白名单 + 无重定向 / 无管道）"


# ---------------------------------------------------------------------------
# 命令前缀规则 —— 「以后不再问」记的是一条规则
#
# 「不再问」= 写一条规则，而那条规则常常是前缀（`${cmd} *`），
# 而前缀不是猜的 —— 有一个小、保守的命令头提取器。
#
# 拿不准一律退回整条精确匹配。它不是失败：
# 空串的意思是"这条命令只认整条"，行为与只用精确匹配时一致。
# ---------------------------------------------------------------------------

#: 段首是这些，不给前缀（它们是 shell / 包装器，要做的事在后面那串里）。
#: `python` 故意不在表里 —— 它由第 ⑤ 条天然挡住：
#: `python -m pytest …`（第二个词是旗标）与 `python solve.py`（第二个词是文件）
#: 都给不出前缀，而 `python` 本身不该被当成包装器。
_SHELL_HEADS = frozenset({
    "sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh",
    "cmd", "cmd.exe", "powershell", "powershell.exe",
    "pwsh", "pwsh.exe", "wsl", "busybox",
    "env", "sudo", "doas", "su", "runas",
    "time", "nohup", "nice", "ionice", "xargs",
    "eval", "exec", "start", "call", "source", ".",
})

#: 含这些，不给前缀（语义不在前两个词里）。
_META_CHARS = (";", "&", ">", "<", "|", "`", "$(", "${")


def command_prefix_rule(command: str) -> str:
    """这条命令的前缀规则（形状 `"<词1> <词2> *"`）；拿不准，空串。

    空串 = 只能整条精确匹配（不是失败）。

    | # | 挡掉什么 | 为什么 |
    |---|---|---|
    | ① | 空、或只有一个词 | 单 token 不给 —— `ls` 记成 `ls *` 太宽 |
    | ② | 含 shell 元字符（分号 · `&` · 重定向 · 竖线 · 反引号 · `$(` · `${`） | 那种命令的语义不在前两个词里 |
    | ③ | 不是一段（`a && b`、`a` 竖线 `b`） | 同 ② |
    | ④ | 段首带路径分隔符 / `=`（`./deploy`、`FOO=1`） | 那不是"一个命令名" |
    | ⑤ | 段首是 shell / 包装器（`sudo` `env` `sh` `xargs` …） | 要做的事在后面 |
    | ⑥ | 第二个词不像子命令（以 `-` 开头 / 含 `=` / 含 `.` / 带路径） | 它是旗标、赋值或文件路径 |

六关全过才给 `"<词1> <词2> *"`。
    """
    raw = (command or "").strip()
    if not raw:
        return ""
    if any(c in raw for c in _META_CHARS):
        return ""
    segs = _segments(raw)
    if len(segs) != 1:
        return ""
    words = segs[0]
    if len(words) < 2:
        return ""
    head = words[0]
    if "/" in head or "\\" in head or "=" in head:
        return ""
    if _basename(head).lower() in _SHELL_HEADS:
        return ""
    second = words[1]
    if (second.startswith("-") or "=" in second or "." in second
            or "/" in second or "\\" in second):
        return ""
    # ⑦ 长度上限 —— 一个"子命令名"不会长到 40 字符。
    #    这条是实际逼出来的：`echo yyyy…(2500 个 y)` 会生成一条
    #    2500 字符的"规则"，而它要随审批载荷一起走，把载荷顶过预算
    #缩字段缩不下去，退到最小集，连 `prompt` 都没了。
    #规则是有界的小东西，这里就是它的界。
    if len(second) > 40 or len(head) > 40:
        return ""
    return "%s %s *" % (head, second)


def rule_matches(rule: str, command: str) -> bool:
    """命令 *command* 命不命中规则 *rule*。

    · 规则**以 ` *` 结尾**，前缀规则：命令开头逐字相同即命中
      （`npm run *` 命中 `npm run build --mode prod`）；
    · 其余，整条精确匹配（不猜前缀）。

    两侧都先折叠空白 —— 与 `core/shell.py` 归一化 `subject` 那一下
    同一个口径（大小写不动：它可能改变语义）。
    """
    r = " ".join(str(rule or "").split())
    c = " ".join(str(command or "").split())
    if not r or not c:
        return False
    if r.endswith(" *"):
        head = r[:-2]
        return bool(head) and (c == head or c.startswith(head + " "))
    return c == r


def any_rule_matches(rules, command: str) -> bool:
    """*rules* 里有没有一条命中 *command* —— `WorkspaceManager` 用的那一半。"""
    for r in rules or ():
        if rule_matches(r, command):
            return True
    return False
