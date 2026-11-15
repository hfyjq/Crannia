# -*- coding: utf-8 -*-
"""网络与执行的持久白名单。

## 它是什么

出网/执行的目标被批准后落盘，并在设置页里可看可删可加。

对话里的选项 5 不写这个文件（它是会话级的）。
这里的条目只能来自用户明确设置，不会因为点了一次弹窗就永久多出来一条。

## 上面这条在出网那一格已经破例

出网审批的选项表里有一格 `6`「信任此站点，以后不再询问」，
它直接写进本文件的 `egress` 格（落点在 `pre_process._settle_target`）。

  · 只有 `egress` —— 别的 kind 收到选项 6 一律拒、什么都不记；
  · 理由：那条旧规矩是为命令立的，而命令的"总是允许"做不了前缀
本来就是空头支票；站点是个稳定的身份，持久信任在这一格有内容；
  · 代价被另一条抵消：同一次要求它在设置页看得见、删得掉
    （`packages/ui` 的 `net_list` 从占位升级成真编辑器）。
  · ✅ 这条破例记在案，并带推翻条件：`egress` 那格堆到没人清理时重称。

写新 kind 的时候别照抄这一格 —— 先回去看它的破例条件。

来源，一个真相源 —— 就是这一个文件：

  1. 设置页（用户自己加 / 删）
  2. 设置页 UI 添加 / 删除
  3. 用户直接改文件

不做第二套。UI 与对话都读写它；"改配置有两个源，各自漂移"。

## 两条判据

① 文件即真相，每轮现读。
不缓存、不在内存里留副本 —— 用户手改文件之后，下一轮就该生效。
文件不到 1 KB，一轮读一次的开销可以忽略。

**② `"*"` 是"全部"的意思。**
设置页那个「全部放行」开关写的就是它，渲染成一行「全部」，删它就是关掉它 ——
不另设一个布尔开关，否则又变成两套机制表达同一件事。

## 「危险档」那条限制已取消

这里原来写着「`command_guarded` 的放行不在这张表里，因为它不给『别再问』」。
那一档整个取消了（「全部指令统一审批格式」）—— 所以：

  · 「关了安全询问开关」期间加进白名单的命令，之后会继续自动放行。
    这是明知的取舍（常见自动模式通常就是全自动批准）。
  · `deny` 档不受影响 —— 出网类命令（curl/wget/ssh 一族）是
    确定性拒绝，连问都不问，它们在 `core/command_policy.py`，
    不经这张表。这张表最多放到"用户点过同意"，放不到"绕过地板"。

仍然成立的那半：判定是按 kind 查的，两个域互不串台
（放行了域名不等于放行命令）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = ["KINDS", "SESSION_KINDS", "path", "load", "save", "list_for", "add",
           "remove", "ALL_WILDCARD", "matches", "matches_any"]

#: 能进白名单的域。
#:
#: 第三个是 `email_to` ——
#: 「**邮件白名单写在 skill 页的邮箱 skill 里 —— 用户可以往里面加不询问的地址**」。
#:
#: 这不是把 `destructive` 塞进文件域（下面那条禁令仍然成立，判据也仍然成立）：
#: `destructive` 是「这个工具没人给它做过审批发射点」这一整根轴 ——
#: 放行它等于持久放行全部不可逆操作。而 `email_to` 放的是收件人地址：
#: 它只回答"发给这个人之前还用不用问"，问不到的、改不掉的、发出去的
#: 那条照旧是有的（邮件本来也不在"不可逆操作"的判定里，它走兜底闸是因为
#: `destructive=True` 声明 —— 见 `core/tool_permission.py` 的第二根轴）。
#:粒度差一整个数量级，所以一个进文件域、一个不进。
#:
#: 而这一格正是本节开头那句"要动就该在设置页明说"的兑现：
#: 它只在 skill 页的邮箱界面里可加可删，不在任何弹窗里被顺手写进去。
KINDS: tuple[str, ...] = ("egress", "command", "email_to", "computer_app")

#: 会话级放行还认这些 kind。
#:
#: 它们没有持久白名单域（`data/allowlist.json` 里不该有它们，
#: `add()` 会拒），但审批面板的「本次对话内不再询问这个操作」（选项 2）
#: 会往会话载体（`ctx.session_target_grants`）里写它们
#:必须和 `KINDS` 一起被轮首投影（`pipeline/scheduler.py` 那一段）。
#:
#: ## 不加它会怎样（验证过的失败模式）
#:
#: 投影原来只遍历 `KINDS`，`destructive` 永远不被写进 `wm._target_session`
#:`has_target_grant("destructive", "send_email")` 恒 False
#:点了「本次对话内不再询问」，下一轮照样弹窗。
#: 而正上方那句注释写着「必须每个 kind 都过一遍」—— 注释对，循环没做到。
#:
#: 别把 `destructive` 塞进 `KINDS`：那是文件域，塞进去等于给
#: `data/allowlist.json` 开一格，可持久放行不可逆操作，那是另一条决定
#: （要动就该在设置页明说）。会话级那半不需要文件域。
#: 机械读者：`tests/test_approval_scope.py` 按源码反查「每个会写会话授权的 kind
#: 都在 `KINDS + SESSION_KINDS` 里」。
#: `write_outside` = 「工作区之外写文件」这一类。
#: 它同样只有会话级，理由与 `destructive` 那条同级但更硬：
#: 只给「 会话档（=「本次会话内始终允许」），
#: 不给「永久」档」—— 因为这一条天然有一条"永久"的路：
#: 把那个目录加进「工作区列表」（设置页）。那件事只能是用户在设置页
#: 明确做的，不许从一次弹窗里点出来（与选项 5 不写 `allowlist.json` 同一条判据）。
SESSION_KINDS: tuple[str, ...] = ("destructive", "write_outside")

# 接管的应用白名单。
#
# 选项「同意，且永久批准此应用」，然后在对话设置里有一个
# 接管应用列表，可以让用户移除批准或加入批准。
#
# · 它和 `egress` 的选项 6 是同一个形状（弹窗里点一次，写进本文件），
#   所以那条破例的代价在这里同样适用，也必须同样兑现：
#   在设置页看得见、删得掉（`settings-view.js` 的 `takeover_apps`）。
# · 值 = exe 小写 basename（如 `notepad.exe`）。`*` = 所有应用。
# · 判定只走 `matches_any`（整名匹配；带 `*` 时按域名那套段规则，别指望前缀）。

#: 通配：表示这个域全部放行。选项 5 写的就是它。
ALL_WILDCARD = "*"

#: 它不进仓（`.gitignore`）—— 那是这台机器的授权账本。
#: 仓库里那份是形状示例：`data/allowlist.example.json`（四个键都写全，
#: 含一行 `_comment`；`load()` 只认 `KINDS`，多余键会被忽略）。
#:新克隆没有这个文件是正常的：`load()` 按空表处理（全问），
#: 第一次写（设置页 / 选项 6）由 `save()` 建出来。
_PATH = Path("data/allowlist.json")


def path() -> Path:
    """白名单文件的位置（相对进程 cwd —— 与 `data/settings.json` 同一条规矩）。"""
    return _PATH


def _empty() -> dict:
    return {k: [] for k in KINDS}


def load() -> dict:
    """读白名单。坏了就当空（不是抛异常）。

    读失败必须朝"全问"那个方向倒：返回空表，每个目标都得问一次。
    反过来（读失败当"全都放行"）就是把一次 IO 故障变成一次静默的授权扩大。

    返回的形状永远是 `{kind: [str, ...]}`，两个键都在（哪怕是空表），
    免得调用方还要处理 `KeyError`。
    """
    out = _empty()
    try:
        raw = _PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return out
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        print(f"[allowlist] {_PATH} 解析失败（{type(exc).__name__}: {exc}）"
              f" —— 按空表处理，这一轮所有目标都会问")
        return out

    if not isinstance(data, dict):
        print(f"[allowlist] {_PATH} 顶层不是对象 —— 按空表处理")
        return out

    for kind in KINDS:
        vals = data.get(kind)
        if not isinstance(vals, list):
            continue
        seen: set[str] = set()
        cleaned: list[str] = []
        for v in vals:
            s = str(v).strip()
            if s and s not in seen:
                seen.add(s)
                cleaned.append(s)
        out[kind] = cleaned
    return out


def save(data: dict) -> None:
    """原子写（tmp + `os.replace`）—— 抄 `session_memory.save()` 的做法。

    不用"打开就写"：写一半被打断会留下一个解析不了的文件，
    而按 `load()` 的规矩那等于白名单全没了（用户会以为自己的条目消失了）。
    """
    payload = {k: sorted(set(str(x) for x in (data.get(k) or []))) for k in KINDS}
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, _PATH)  # atomic on Windows + POSIX


def list_for(kind: str) -> list[str]:
    """某个域当前的条目。未知 kind，空表（不抛）。"""
    return list(load().get(str(kind), []))


# 通配语义 —— 防钓鱼的官方语义
#
# 判据：
#
#   通配符只有出现在开头 `*.` 或单独一个 `*` 时才表示"任意子域"；
#   其他位置只匹配两个点之间的一段。这条防止结尾通配符匹配到攻击者能注册的域名。
#
#三句话，逐条落到代码：
#
#   ① 裸 `*`，全部（设置页那个「全部放行」开关写的就是它）
#   ② 前导 `*.`，任意深度子域， 且不匹配基址本身
#                          （`*.example.com` 不匹配 `example.com`）
#   ③ 其余位置的 `*`，只吃两个点之间的一段，`example.*` 匹配
#                          `example.com`，不匹配 `example.co.uk`
#                          —— 这一条正是"挡住攻击者能注册的那些域名"的机制：
#                          否则 `*.com` 式的写法会把别人注册的域名一起放进来。
#
# 不把这一套接进 `has_target_grant`（那一条同时服务 `command`）：
#    命令的判定是整条精确匹配（这也是常见实现的兜底写法，
#    "总是允许"因此永远不会变成空头支票）。一条命令里出现 `*` 是命令语法
#    （`rm *.txt`），把它读成通配就等于把那条不变量拆了。
#通配只属于域名域，消费点在 `core/egress_gate.py`。

def matches(pattern: str, value: str) -> bool:
    """`pattern` 能不能覆盖 `value` —— 通配语义见上面那一段（单一来源）。

    域名两侧都不区分大小写（DNS 本来就不区分）。
    """
    p = str(pattern or "").strip().lower()
    v = str(value or "").strip().lower()
    if not p or not v:
        return False
    if p == ALL_WILDCARD:
        return True
    if "*" not in p:
        return p == v

    # ② 前导 `*.` —— 任意深度子域，不含基址本身。
    if p.startswith("*."):
        base = p[2:]
        if base and "*" not in base:
            return v.endswith("." + base)

    # ③ 其余位置：按段比，`*` 只吃一整段（= 两个点之间那一段）。
    ps, vs = p.split("."), v.split(".")
    if len(ps) != len(vs):
        return False
    return all(a == "*" or a == b for a, b in zip(ps, vs))


def matches_any(patterns, value: str) -> bool:
    """`patterns` 里任何一条命中 `value`。空表，`False`（照问）。"""
    return any(matches(p, value) for p in (patterns or ()))


def add(kind: str, subject: str) -> bool:
    """加一条。已经在了，返回 `False`（幂等）。"""
    k, s = str(kind), str(subject or "").strip()
    if k not in KINDS or not s:
        return False
    data = load()
    if s in data[k]:
        return False
    data[k].append(s)
    save(data)
    return True


def remove(kind: str, subject: str) -> bool:
    """删一条。本来就没有，返回 `False`。"""
    k, s = str(kind), str(subject or "").strip()
    if k not in KINDS:
        return False
    data = load()
    if s not in data[k]:
        return False
    data[k] = [x for x in data[k] if x != s]
    save(data)
    return True
