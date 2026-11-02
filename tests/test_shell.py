# -*- coding: utf-8 -*-
"""执行层的护栏：`core/shell.py`（探测 / 环境清洗 / cwd / 超时 / 输出）。

这个文件存在的理由：它守的四条全是"写错了不报错、只是静默地少了一层保护"。

## 四条

1. 环境清洗是白名单。判据来自一次实测：照抄黑名单正则
   `/KEY|PASSWORD|SECRET|TOKEN/i` 会漏掉本仓的 `FEISHU_APP_ID`。
   而 `.env` 的载荷是实的（`load_dotenv(override=True)`）——
   一条 `env` 就能把 7 个密钥全搬进模型上下文。
2. cwd 被限制在工作区。 bash 不过 `validate_path`，唯一能真的做成的
   那点约束就是工作目录（相对路径因此留在里面）。
3. 输出保尾部，且指针真能取回来。保头的那把刀在
   `core/agent_loop.py` 里（`_truncate_to_artifact`）—— 方向相反，
   所以工具必须自己先裁到位。
4. 「写了」不等于「接上了」。参考实现里有三处"声明但未实现"。
   所以这里既断言实现存在，也断言 `init_bus` 真的调用了那个注册函数。

## 跑法

    .venv/Scripts/python.exe tests/test_shell.py

输出一律 ASCII（Windows 控制台 GBK）。
没装 bash 时本文件必须红，不做 skip —— 静默 PASS 等于没测。
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

from _sandbox import prod_diff, prod_snapshot, sandbox                  # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


# 夹具

def _real_guard_and_wm():
    """真 `SecurityGuard` + 真 `WorkspaceManager`（必须先 chdir、后构造）。"""
    from core.security import SecurityGuard
    from core.workspace import WorkspaceManager
    guard = SecurityGuard(
        allowed_dirs=["./workspace"],
        command_blacklist=[],
        audit_log_path="./logs/audit_shell_test.log",
    )
    wm = WorkspaceManager(guard, {})
    return guard, wm


def _install_wm(wm):
    """把 wm 挂进总线（`run_command` 就是这么拿它的），返回还原函数。"""
    import bus.registry as R
    old = R.get_workspace_manager()
    R.register_workspace_manager(wm)
    return lambda: R.register_workspace_manager(old) if old else None


# 1. bash 探测

def test_bash_is_probed() -> None:
    print("\n-- 1. bash 用绝对路径探测（`bash` 不在 Windows PATH 上）--")
    from core.shell import _BASH_CANDIDATES, bash_path
    got = bash_path()
    check("探到了一个 bash", bool(got), "一个候选都不存在：%r" % (_BASH_CANDIDATES,))
    if not got:
        return
    check("它是**绝对路径**（不是靠 PATH 找）", Path(got).is_absolute(), got)
    check("它真的存在", Path(got).is_file(), got)
    check("候选表里第一条就是它（顺序即优先级）",
          _BASH_CANDIDATES[0].replace("\\", "/").lower()
          == got.replace("\\", "/").lower(), got)
    check("★ 探测结果是缓存的（两次调用同一个值）", bash_path() is got or bash_path() == got)


# 2. 环境清洗：白名单

#: 找不到 `.env` 时的兜底清单（7 个键）。
#: 两个来源都要求非空：一个空清单会让下面那条断言恒真地通过。
_CANNED_ENV_KEYS = (
    "DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN", "FEISHU_APP_SECRET",
    "FEISHU_APP_ID", "DINGTALK_APP_KEY", "DINGTALK_APP_SECRET",
    "GLM_API_KEY",
)


def _env_keys() -> tuple[list[str], str]:
    """从真的 `.env` 读键名；读不到就用兜底清单（并说明用了哪个）。"""
    try:
        raw = (_ROOT / ".env").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return list(_CANNED_ENV_KEYS), "canned"
    keys = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.append(line.split("=", 1)[0].strip())
    return (keys, "dotenv") if keys else (list(_CANNED_ENV_KEYS), "canned")


def test_env_whitelist_excludes_every_secret() -> None:
    """主断言：`.env` 里任何一个键都不许进白名单。

    这一条就是那条实测的机械化 —— 黑名单会漏掉 `FEISHU_APP_ID`，
    所以这里反过来查白名单。
    """
    print("\n-- 2. 环境清洗：白名单不许含任何密钥 --")
    from core.shell import ENV_WHITELIST
    keys, src = _env_keys()
    check("setup: 拿到了非空的密钥清单（来源=%s）" % src, bool(keys), repr(keys))

    allowed = {k.lower() for k in ENV_WHITELIST}
    leaked = sorted(k for k in keys if k.lower() in allowed)
    check("★ 白名单里没有任何一个 .env 键", not leaked,
          "这些密钥会被继承给子进程：%r" % (leaked,))
    check("  连 `FEISHU_APP_ID` 也不在（黑名单模式会漏的就是它）",
          "feishu_app_id" not in allowed, str(sorted(allowed)))


def test_env_whitelist_shape() -> None:
    print("\n-- 2b. 白名单自身的形状 --")
    from core.shell import ENV_WHITELIST
    check("全小写（Windows 环境变量名不区分大小写，比较前统一）",
          all(k == k.lower() for k in ENV_WHITELIST),
          repr([k for k in ENV_WHITELIST if k != k.lower()]))
    check("没有重复项", len(set(ENV_WHITELIST)) == len(ENV_WHITELIST),
          str(len(ENV_WHITELIST)))
    check("不是空的（空表会让一切断言恒真）", len(ENV_WHITELIST) > 10,
          str(len(ENV_WHITELIST)))
    # 起进程必需的几个必须在
    for need in ("path", "systemroot", "temp"):
        check("必需项 %r 在白名单里" % need, need in
              {k.lower() for k in ENV_WHITELIST}, "")


def test_clean_env_drops_a_planted_secret() -> None:
    """行为断言（不是读表）：真放一个密钥进 `os.environ`，看它过不过得去。"""
    print("\n-- 2c. 真放一个密钥进去，看它过不过得去 --")
    from core.shell import clean_env
    planted = "CRANNIA_TEST_SECRET_TOKEN"
    before_len = len(os.environ)
    os.environ[planted] = "should-not-be-inherited"
    try:
        env = clean_env()
        check("★ 种进去的密钥**没有**被子进程拿到", planted not in env,
              str(sorted(k for k in env if "CRANNIA" in k)))
        check("★ 也没有改到进程自己的 os.environ（那是全局）",
              os.environ.get(planted) == "should-not-be-inherited",
              repr(os.environ.get(planted)))
    finally:
        os.environ.pop(planted, None)
    check("原样还原（长度不变）", len(os.environ) <= before_len,
          "%d -> %d" % (before_len, len(os.environ)))


def test_python_resolves_to_the_frameworks_own_interpreter() -> None:
    """`python` / `pip` 必须先解析到跑框架的这个解释器。

    现场反馈：模型跑 `python -m pytest -q` 报 `No module named pytest` ——
    因为 PATH 上更早的是另一个项目的旧虚拟环境；而它据此对用户说
    "这个项目的测试我没法跑"。那是我们让他跑在了一个错的解释器上。
    """
    print("\n-- 2c2. 子进程里的 python 必须是我们的解释器 --")
    import sys as _sys
    from pathlib import Path as _P
    from core.shell import clean_env
    env = clean_env()
    key = next((k for k in env if k.lower() == "path"), "")
    check("★ PATH 还在（不然连 bash 都起不来）", bool(key), repr(list(env)[:8]))
    if key:
        sep = ";" if os.name == "nt" else ":"
        first = env[key].split(sep)[0]
        want = str(_P(_sys.executable).parent)
        check("★★ 第一段就是解释器所在目录", first == want, "%r vs %r" % (first, want))


def test_nc_a_whitelist_is_load_bearing() -> None:
    """负对照 A：把白名单放开，种进去的密钥立刻被继承。

    没有这一条，上面那三个断言可能是恒真的（比如 `clean_env` 恰好返回空 dict）。
    """
    print("\n-- 2d. 负对照 A：白名单放开之后密钥就漏了 --")
    from core import shell as SH
    planted = "CRANNIA_TEST_SECRET_TOKEN"
    os.environ[planted] = "x"
    real = SH._ENV_ALLOWED
    try:
        SH._ENV_ALLOWED = frozenset(set(real) | {planted.lower()})
        env = SH.clean_env()
        check("★ 放开之后它**真会**被继承（说明过滤确实在起作用）",
              planted in env, str(sorted(k for k in env if "CRANNIA" in k)))
    finally:
        SH._ENV_ALLOWED = real
        os.environ.pop(planted, None)
    check("还原之后又不漏了", planted not in SH.clean_env(), "")


# 3. cwd

def test_cwd_is_confined_to_workspace() -> None:
    print("\n-- 3. cwd 限制在工作区里 --")
    from core.shell import _resolve_cwd
    _, wm = _real_guard_and_wm()

    p, note = _resolve_cwd("workspace", wm)
    check("工作区内的目录可以当 cwd", not note and p.name == "workspace",
          "%r / %r" % (p, note))

    p2, note2 = _resolve_cwd(os.path.expanduser("~"), wm)
    check("★ 工作区**外**的目录被回退", bool(note2), repr(note2))
    check("  回退到工作区", p2.name == "workspace", str(p2))
    check("  而且**出声**（把理由告诉模型）", "工作区" in note2, repr(note2))

    p3, note3 = _resolve_cwd("no/such/dir", wm)
    check("不存在的目录也回退并出声", bool(note3) and p3.name == "workspace",
          "%r / %r" % (p3, note3))


def test_nc_b_the_confinement_is_load_bearing() -> None:
    """负对照 B：把 guard 摘掉，工作区外的目录会被接受。"""
    print("\n-- 3b. 负对照 B：摘掉 guard，限制就没了 --")
    from core.shell import _resolve_cwd
    _, wm = _real_guard_and_wm()
    outer = Path(os.path.expanduser("~"))

    class _NoGuard:
        security = None

    p, note = _resolve_cwd(str(outer), _NoGuard()) if outer.is_dir() else (None, "skip")
    if p is not None:
        check("★ 没有 guard 时它**不会**回退（证明承重的是那次类别判定）",
              p == outer.resolve(), "%r / %r" % (p, note))
    check("对照：有 guard 时同一个目录会被拒",
          bool(_resolve_cwd(str(outer), wm)[1]) if outer.is_dir() else True, "")


def test_cwd_memory_round_trip() -> None:
    """「框架记住 cwd」：`session_cwd` 投影被读，执行结果被写。"""
    print("\n-- 3c. cwd 记忆：读投影、写投影 --")
    from core.shell import _resolve_cwd, run_command
    _, wm = _real_guard_and_wm()
    restore = _install_wm(wm)
    try:
        wm.session_cwd = str((Path.cwd() / "workspace").resolve())
        p, note = _resolve_cwd("", wm)
        check("不传 cwd 时用 `wm.session_cwd` 那个投影",
              p.name == "workspace" and not note, "%r / %r" % (p, note))

        # 真跑一条 `cd`，看 bash 回报的目录有没有被写回投影
        # 默认 cwd 就是 工作区，所以不能再 `cd workspace`（那里没有
        # `workspace/workspace`）—— 造一个子目录，往下走。
        (Path.cwd() / "workspace" / "sub").mkdir(exist_ok=True)
        wm.session_cwd = ""
        wm._approval_scope = ""
        wm.grant_target_once("command", "cd sub", owner="")
        r = run_command("cd sub")
        check("命令跑成功了", r.get("success") is True, repr(r)[:220])
        check("★ bash 回报的 `$PWD` 被写回了 `wm.session_cwd`",
              str(r.get("cwd", "")).replace("\\", "/").endswith("/workspace/sub")
              and str(wm.session_cwd).replace("\\", "/").endswith("/workspace/sub"),
              "cwd=%r wm.session_cwd=%r" % (r.get("cwd"), wm.session_cwd))
        check("  结果是 Windows 风格路径（不是 MSYS 的 `/d/...`）",
              not str(r.get("cwd", "")).startswith("/"),
              repr(r.get("cwd")))
        # 记忆生效：下一条不传 cwd 的命令应当落在刚刚那个子目录里
        p2, note2 = _resolve_cwd("", wm)
        check("★ 记住了：下一条不传 cwd 的命令落在同处",
              p2.name == "sub" and not note2, "%r / %r" % (p2, note2))
    finally:
        restore()


# 4. 输出裁剪

def test_every_executed_command_is_audited() -> None:
    """执行过的命令必须留痕。

    没有留痕时的形状：查当天的 `logs/audit.log` ——
    23 次 `run_command`，0 条记录；单独跑一遍 `echo hi`，那个文件
    压根没被创建。

    两层叠加导致：
      ① `core/agent_loop.py` 对 write/admin 类工具只 `print()` 一行
      ② `run_command` 不走 `validate_path` —— bash 本来就不走

框架获得了"执行任意命令"的能力，而没有任何持久记录。

    断言打在磁盘上的那一行，不是"函数被调过" ——
    写了却取不回来等于没写。
    """
    print("\n-- 7. ★★ 执行的命令要留痕（审计）--")
    from core.security import SecurityGuard
    from core.workspace import WorkspaceManager
    from core.shell import run_command

    guard = SecurityGuard(allowed_dirs=["./workspace"], command_blacklist=[],
                          audit_log_path="./logs/audit_at_test.log")
    wm = WorkspaceManager(guard, {})
    restore = _install_wm(wm)
    log = Path("./logs/audit_at_test.log")
    try:
        wm.approval_scope = ""
        check("setup: 一开始没有审计文件", not log.exists(), str(log))

        for cmd, want in (("echo hi", "CMD_OK"), ("exit 3", "CMD_FAIL")):
            wm.grant_target_once("command", cmd, owner="")
            r = run_command(cmd)
            check("  %r 真的跑了" % cmd, r.get("success") is not None, repr(r)[:60])

        text = log.read_text(encoding="utf-8") if log.exists() else ""
        check("★ 审计文件被建出来了", log.exists(), str(log))
        check("★ `echo hi` 那一行在里面（带动作 + 命令原文）",
              "CMD_OK" in text and "echo hi" in text, repr(text[-200:]))
        check("★ 失败的那条也记了，而且**动作不同**",
              "CMD_FAIL" in text, repr(text[-200:]))
        check("  行数 = 执行次数（不多不少）",
              len([l for l in text.splitlines() if l.strip()]) == 2,
              repr(text))
    finally:
        restore()


def test_audit_records_the_whole_command() -> None:
    """审计内容不许截断 —— 截断等于"被审计的不是被执行的那条"。"""
    print("\n-- 7b. 审计记的是**完整**命令 --")
    from core.security import SecurityGuard
    from core.workspace import WorkspaceManager
    from core.shell import run_command

    guard = SecurityGuard(allowed_dirs=["./workspace"], command_blacklist=[],
                          audit_log_path="./logs/audit_long_test.log")
    wm = WorkspaceManager(guard, {})
    restore = _install_wm(wm)
    log = Path("./logs/audit_long_test.log")
    try:
        wm.approval_scope = ""
        cmd = 'echo "' + "x" * 900 + '"'
        wm.grant_target_once("command", cmd, owner="")
        run_command(cmd)
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        check("★ 900 字符的命令**整条**都在审计里（没有 `…` 截断）",
              cmd in text, "审计里只找到 %d 字符" % len(text))
    finally:
        restore()


def test_clip_keeps_tail_and_spills() -> None:
    print("\n-- 4. 输出保尾部 + 落盘指针 --")
    from core.shell import _clip
    body = "HEAD" + ("x" * 200) + "TAIL"
    shown, path = _clip(body, 50)
    check("超预算 -> 给了指针", bool(path), repr(path))
    check("★ 保留的是**尾部**（`TAIL` 在）", "TAIL" in shown, shown[-40:])
    check("★ 头部**不在**预览里（那把刀在别处，方向相反）",
          "HEAD" not in shown, shown[:40])
    check("明说了省略（不是静默丢弃）", "只保留" in shown, shown[:60])
    check("指针用正斜杠（给模型照着敲的）", "\\" not in path, path)
    check("落盘文件**真存在**", Path(path).is_file(), path)
    check("★ 落盘文件里是**完整**内容（指针才不是摆设）",
          Path(path).read_text(encoding="utf-8") == body, "")

    short, no_path = _clip("hello", 50)
    check("没超预算就不落盘、不裁剪", short == "hello" and not no_path,
          "%r / %r" % (short, no_path))


# 5. 闸门接线

def test_denied_command_never_asks() -> None:
    """硬拒那一档不许走到审批 —— 走了就是"确定性"降级成"策略"。"""
    print("\n-- 5. 出网类：直接拒，**不经过审批那条路** --")
    from core.shell import run_command
    _, wm = _real_guard_and_wm()
    restore = _install_wm(wm)

    calls = {"n": 0}
    real = wm.has_target_grant

    def _spy(kind, subject):
        calls["n"] += 1
        return real(kind, subject)

    wm.has_target_grant = _spy
    try:
        r = run_command("curl https://example.com")
        check("返回 denied", r.get("denied") is True, repr(r)[:200])
        check("success=False", r.get("success") is False, "")
        check("★ **没有**问过授权存储（一次都没调 `has_target_grant`）",
              calls["n"] == 0, "调了 %d 次" % calls["n"])
        check("没有 pending 载荷", "status" not in r, repr(r.get("status")))
        check("指了替代出路", "web_fetch" in str(r.get("hint", "")),
              repr(r.get("hint")))
    finally:
        wm.has_target_grant = real
        restore()


def test_gate_kinds_and_subject() -> None:
    """普通档 / 危险档的 `kind` 与载荷契约。"""
    print("\n-- 5b. 两档的 kind 与载荷 --")
    from core.shell import run_command
    _, wm = _real_guard_and_wm()
    wm._approval_scope = "sess-A"
    restore = _install_wm(wm)
    try:
        r = run_command("echo hi")
        check("普通档 => kind=command", r.get("kind") == "command", repr(r)[:150])
        check("status 是派发器认的那个串",
              r.get("status") == "pending_command_approval", repr(r.get("status")))
        check("被审批物 = 命令原文", r.get("subject") == "echo hi",
              repr(r.get("subject")))
        check("正文里有命令原文（用户要批的就是它）",
              "echo hi" in str(r.get("prompt", "")), repr(r.get("prompt"))[:120])

        g = run_command("cat core/security.py")
        check("★ 危险命令走**同一张表**（command 只有一种档位）",
              g.get("kind") == "command", repr(g)[:150])
        check("  ★ 但**理由仍然照说**（危险没消失，只是换了载体）",
              "受保护路径" in str(g.get("detail", "")), repr(g.get("detail"))[:120])

        # 空白折叠：同一个命令的两种写法共享同一条授权
        a = run_command("echo   hi")
        check("空白折叠（`echo   hi` 与 `echo hi` 同一个 subject）",
              a.get("subject") == "echo hi", repr(a.get("subject")))
    finally:
        restore()


def test_user_forbidden_dir_reaches_run_command() -> None:
    """端到端：用户划的规避目录真的走到执行闸那一层。

    为什么这一条必须存在（而不是只测 `classify_command`）：
    缺口不在判定里、在接线里 —— 判定本来就会做「提及升档」，
    缺的是调用方读不到用户那张表。只测纯函数 = 测了一条本来就没坏的东西
    （「写了」≠「接上了」）。
    """
    print("\n-- 5f. 规避目录端到端--")
    from core.shell import run_command
    guard, wm = _real_guard_and_wm()
    guard.forbidden_dirs = ["D:\\重要资料"]          # = 用户在设置页加的那一条
    restore = _install_wm(wm)
    try:
        r = run_command("rm /d/重要资料/合同.docx")
        check("★ 单文件删除（修前落 allow）-> 停下来问",
              r.get("status") == "pending_command_approval", repr(r)[:150])
        check("  ★ 理由点名是**用户划的那条线**，不是「框架自己的源码」",
              "规避目录" in str(r.get("detail", "")),
              repr(r.get("detail"))[:140])
        # 反向：不在那张表里的路径照旧走普通档
        # （收窄过头 = 少拦的那一侧，而它不会红、只会静默）
        n = run_command("echo /d/别的资料/x.txt")
        check("对照：别的路径照旧普通（不是全都升档）",
              n.get("status") == "pending_command_approval"
              and "规避目录" not in str(n.get("detail", "")), repr(n)[:140])
    finally:
        restore()


def test_grant_then_execute() -> None:
    print("\n-- 5c. 批过之后真的执行 --")
    from core.shell import run_command
    _, wm = _real_guard_and_wm()
    wm._approval_scope = "sess-A"
    restore = _install_wm(wm)
    try:
        wm.grant_target_once("command", "echo hello-from-shell", owner="sess-A")
        r = run_command("echo hello-from-shell")
        check("执行成功", r.get("success") is True, repr(r)[:300])
        check("★ stdout 里有输出", "hello-from-shell" in str(r.get("stdout", "")),
              repr(r.get("stdout")))
        check("exit_code 是 0", r.get("exit_code") == 0, repr(r.get("exit_code")))
        check("结果里带 tier", r.get("tier") == "allow", repr(r.get("tier")))

        # 属主对不上，不算数（`has_target_grant` 那半）
        wm._approval_scope = "sess-B"
        r2 = run_command("echo other-session")
        check("★ 别人的一次性授权不算数（属主比对）",
              r2.get("status") == "pending_command_approval", repr(r2)[:150])
    finally:
        restore()


def test_timeout_is_clamped() -> None:
    print("\n-- 5d. 超时额度 --")
    from core.shell import DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS, _run
    # 默认 30s 是有意定的值（实测中位 39ms、只有 1/163 撞满过 120s），
    #   改这个常量要同时改这里的期望值 —— 别把不一致读成"漂了"。
    check("默认 30s", DEFAULT_TIMEOUT_MS == 30_000, str(DEFAULT_TIMEOUT_MS))
    check("上限 600s", MAX_TIMEOUT_MS == 600_000, str(MAX_TIMEOUT_MS))
    r = _run("sleep 10", Path.cwd(), 1200)   # 1.2s，远小于 sleep
    check("★ 超时会被真的掐断", r.get("timed_out") is True, repr(r)[:200])
    check("  掐断时给模型一句话", "超时" in str(r.get("note", "")) or
          "秒" in str(r.get("note", "")), repr(r.get("note")))


# 6. 接线：「写了」≠「接上了」

def _calls_in(path: Path, func_name: str) -> bool:
    """`path` 里有没有真调用 `func_name()`（走 AST，不走子串）。

    为什么用 AST："不许出现某字符串"那类检查器
    会被描述它的注释打中。这里的反面同样成立：注释里写一句
    "下面该调 register_command_tool()" 会让 grep 式检查变成假绿。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name == func_name:
                return True
    return False


def test_registration_is_wired() -> None:
    print("\n-- 6. 接线：写了 且 init_bus 真的调了 --")
    import bus.registry as R
    check("`register_command_tool` 存在", hasattr(R, "register_command_tool"), "")
    check("★ `bus/core.py` 里**真的调用**它（AST，不是 grep）",
          _calls_in(_ROOT / "bus" / "core.py", "register_command_tool"), "")

    R.register_command_tool()
    check("调用之后 `run_command` 在注册表里",
          "run_command" in R.list_tools(), "")
    check("★ 它**常驻**（不常驻 = 模型不知道它存在就没法搜它）",
          "run_command" in R._CORE_TOOLS, str(sorted(R._CORE_TOOLS)))
    check("权限档是 admin（名字没有 read/write 前缀）",
          R.get_tool_permission("run_command") == "admin",
          R.get_tool_permission("run_command"))
    check("handler 是个可调用的", callable(R.get_tool_handler("run_command")), "")
    schemas = [s for s in R.get_tool_schemas()
               if s["function"]["name"] == "run_command"]
    check("wire 上有且只有一条 run_command schema", len(schemas) == 1,
          str(len(schemas)))
    if schemas:
        params = schemas[0]["function"]["parameters"]
        # `required` 是放开的，因为查后台作业那次调用
        # 只有 `job_id`、没有 `command`。"两个都不给"由工具自己拒（见下一条），
        # 不靠 schema —— 派发层本来就不执行 `required`。
        check("required 不再硬要求 command（查作业没有 command）",
              params.get("required") == [], repr(params.get("required")))
        check("schema 里有 job_id",
              "job_id" in (params.get("properties") or {}), "")
        check("★ wire 上不带 $schema",
              "$schema" not in params, "")
        body = schemas[0]["function"]["description"]
        check("描述里说明了出网类不执行", "web_fetch" in body, body[:120])


def test_tool_result_is_isolated() -> None:
    """命令输出是最不可控的一条 —— 它必须进隔离名单。"""
    print("\n-- 6b. 命令输出必须被当作不可信内容 --")
    from core import agent_loop as AL
    check("★ `run_command` 在 `_ISOLATED_DOC_TOOLS` 里",
          "run_command" in AL._ISOLATED_DOC_TOOLS,
          str(sorted(AL._ISOLATED_DOC_TOOLS)))
    # 它不落在反查家族里（名字不是 get_/read_/parse_/…），
    # 所以 `test_content_sanitizer` 的反查不会替它报警 —— 说清楚，
    # 免得下一个人以为"有机制看着它"。
    fam = ("get_", "read_", "search_", "summarize_", "parse_", "list_", "web_")
    check("  它不在反查家族里（所以这条断言是**手动**加的，不是机制逼出来的）",
          not "run_command".startswith(fam), "")
    isos = _ROOT / "tests" / "test_content_sanitizer.py"
    check("★ 但 `test_content_sanitizer` 的**点名清单**里有它（配了读者）",
          "run_command" in isos.read_text(encoding="utf-8"),
          "点名清单没带上它 —— 那条断言才是它的机械读者")


def test_activity_line_does_not_leak_basename() -> None:
    print("\n-- 6c. 活动行：刻意不带 {arg} --")
    from core.activity import _fallback, describe_tool
    line = describe_tool("run_command", {"command": "rm -rf workspace/报告.docx"})
    check("有中文活动行，没掉到 fallback", line != _fallback("run_command"), line)
    check("★ 命令行**不经过** `arg_hint`（它会取 basename，把危险的那半藏起来）",
          "报告.docx" not in line, line)
    check("  就是那句固定的", line == "正在执行命令", repr(line))


def test_relax_switch_changes_the_kind_and_says_so() -> None:
    """「关闭安全询问」开关：危险档降成普通档，而且如实说出来。

    两件事都要验：
    ① 行为 —— `relax_command_tier` 关着时危险命令的 `kind` 与开着时一样
       （只有一种 kind），差别在有没有 relaxed_from
    ② 告知 —— 载荷里必须带上"这条本来会走危险档"。
       降级之后 `tier` 已经是 `allow`，那件事在数据里看不见了 ——
       不额外说一句，用户就不知道现在批准它的门槛比默认低。
    """
    print("\n-- 5e. ★ 危险档降级开关（设置页那个）--")
    from core.shell import run_command
    _, wm = _real_guard_and_wm()
    wm._approval_scope = "sess-A"
    restore = _install_wm(wm)
    guard = wm.security
    try:
        guard.relax_command_tier = False
        g = run_command("cat core/security.py")
        check("  开关关着时 kind 也是 command（只有一种档位）",
              g.get("kind") == "command", repr(g.get("kind")))
        check("  载荷里带理由", bool(g.get("detail")), repr(g.get("detail"))[:80])
        check("  没有 relaxed_from", not g.get("relaxed_from"), "")

        guard.relax_command_tier = True
        r = run_command("cat core/security.py")
        check("★ 开关打开 -> 仍然是 kind=command",
              r.get("kind") == "command", repr(r.get("kind")))
        check("★ 载荷里**带上了**「本来会走危险档」（如实告知）",
              "危险档" in str(r.get("relaxed_from", "")),
              repr(r.get("relaxed_from"))[:120])
        check("  提示语正文里也有那句（文本通道看得见）",
              "危险档" in str(r.get("prompt", "")), repr(r.get("prompt"))[:160])
        check("  detail 也不为空（浮窗要显示点什么）",
              bool(r.get("detail")), repr(r.get("detail"))[:80])

        # 负对照①：普通命令不该被说成"本来会走危险档"
        n = run_command("echo hi")
        check("★ 负对照：普通命令不会被安上 relaxed_from",
              not n.get("relaxed_from"), repr(n.get("relaxed_from")))

        # 负对照②：deny 那一档碰都不碰
        d = run_command("curl https://example.com")
        check("★ 负对照：开关打开也拦得住 curl（deny 不受它影响）",
              d.get("denied") is True and d.get("tier") == "deny",
              repr(d)[:150])
    finally:
        guard.relax_command_tier = False
        restore()


def test_no_ask_readonly_switch() -> None:
    """免问集 —— 只放宽"看"，不放宽"改"。

    判据是两层：开关开着 且 这条命令够格（`core/command_policy.no_ask_reason`）。
    五组正负，缺一组就读不出结论：
      ① 开关关着 -> 连 `ls` 也照问（默认方向朝"问"倒）
      ② 开关开着 + `ls` -> 不弹窗、真的跑（`stdout` 在）
      ③ 带重定向的 `ls` -> 照问（整条被踢出免问集）
      ④ 带管道的 `cat` -> 照问（分段符 -> 段数 > 1）
      ⑤ `git status` -> 照问（它是 `guarded`，免问集只碰 `allow` 档）
    """
    print("\n-- 5g. ★★ 免问集（WB3）--")
    from core.shell import run_command
    _, wm = _real_guard_and_wm()
    wm._approval_scope = "sess-NA"
    restore = _install_wm(wm)
    guard = wm.security
    try:
        guard.no_ask_readonly = False
        r = run_command("ls")
        check("★ 开关关着 -> 连 `ls` 也照问（默认朝「问」倒）",
              r.get("status") == "pending_command_approval", repr(r)[:120])

        guard.no_ask_readonly = True
        r = run_command("ls")
        check("★★ 开关开着 + `ls` -> **不弹窗、真的跑了**（stdout 在）",
              r.get("status") != "pending_command_approval"
              and "stdout" in r, repr(r)[:140])

        r = run_command("ls > _wb3_probe_should_not_exist.txt")
        check("★★ 负对照：**重定向**把它整条踢出去 -> 照问",
              r.get("status") == "pending_command_approval", repr(r)[:150])

        r = run_command("cat a.txt | head -3")
        check("★★ 负对照：**管道**（分段符）-> 照问",
              r.get("status") == "pending_command_approval", repr(r)[:150])

        r = run_command("git status")
        check("★ 负对照：`git` 是 `guarded` -> 照问（免问集只碰 `allow` 档）",
              r.get("status") == "pending_command_approval", repr(r)[:150])

        # 这两条才是"档位闸"真正承重的地方（变异验出来的）：
        #    `git status` 那条不承重 —— `git` 本来就不在白名单里，
        #    所以把 `tier == TIER_ALLOW` 那道闸整个删掉，它照样绿。
        #    真正会被免问集放过去的，是**段首那个词在白名单里、而整条命令
        #    被升到了 `guarded`** 的那两条路径：
        r = run_command("cat core/security.py")
        check("★★ 负对照：**提到受保护路径** -> 升 `guarded` -> 照问"
              "（删掉那道档位闸，这一条就会红）",
              r.get("status") == "pending_command_approval", repr(r)[:150])

        r = run_command("ls curl")
        check("★★ 负对照：段首是白名单词、**后面藏着出网类** -> 也照问",
              r.get("status") == "pending_command_approval", repr(r)[:150])

        r = run_command("curl https://example.com")
        check("★ 负对照：出网类仍然**硬拒**（免问集管不着 `deny` 档）",
              r.get("denied") is True and r.get("tier") == "deny",
              repr(r)[:140])
    finally:
        guard.no_ask_readonly = False
        restore()


def test_tool_payload_reaches_the_real_detector() -> None:
    """闭环：工具真回的载荷 → 真的检测器 → `ctx.pending_command` 被设上。

    这是整个执行层里唯一一条端到端断言，也是最重要的一条：

        「我写了这个机制」≠「这条路径上会调它」
        （本仓的 `validate_command` 从 v0.1.0 起零调用点，是同一个形状）。

    上面那些断言各测一半：`run_command` 回什么、派发器找什么。
    两半各自绿、合起来对不上才是最常见的坏形状（改了串名、少一个字段、
    工具回的不是 JSON 对象……任一条都能让浮窗永远不出现，而没有任何东西会红）。

    载荷要走 `json.dumps`：`core/agent_loop` 通用分支就是那么发的，
    而检测器要 `content.startswith("{")` 才会去 parse。

    而且必须过 `_format_tool_result` 那一层。不过那一层会漏掉一个真 bug：
    `run_command` 同时在 `_ISOLATED_DOC_TOOLS` 里，
    工具结果会被包成 `<user_document trust="untrusted">{...}`，**不再以 `{`
    开头**，检测器连 parse 都不会去 parse，审批永远不出现。
    而这条测试当时全绿 —— 因为两半各自都对，坏的是中间那一层。
    """
    print("\n-- 6e. 闭环：真载荷 -> 真格式化 -> 真检测器 --")
    import json
    from core.agent_loop import _format_tool_result
    from core.pipeline.stages.agent_loop import _check_cross_zone_pending
    from core.shell import run_command
    _, wm = _real_guard_and_wm()
    wm._approval_scope = "sess-A"
    restore = _install_wm(wm)

    class _Ctx:
        def __init__(self):
            self.messages = []
            self.pending_cross_zone = {}
            self.pending_delete = {}
            self.pending_egress = {}
            self.pending_command = {}
            self.approval_ask_counts = {}
            self.response = ""
            self.user_message = ""
            self.metadata = {}
            self.session = type("S", (), {"platform": "cli"})()

    try:
        # 第三条是长命令 —— 它专门盯：载荷大过预算时被字符切半
        #`json.loads` 抛，静默失效。
        # 第三格是这次审批该带出去的前缀规则。
        #   `npm run build` 是普通档，给前缀；`git status --short` 是
        #   `guarded`，不给（「批准一条 `git status *`」不许顺手
        #   把别的 `git` 子命令也放行）—— 而提取器本来会给出 `git status *`，
        #   所以这两格分得开，不是同一句话说了两遍。
        for cmd, want_kind, want_rule in (# `echo hello` 也给前缀 —— 规则逐字是"前两个词 + `*`"，
                                           ("echo hello", "command", "echo hello *"),
                                          ("cat core/security.py", "command", ""),
                                          ("npm run build", "command", "npm run *"),
                                          ("git status --short", "command", ""),
                                          ("echo " + "y" * 2500, "command", "")):
            payload = run_command(cmd)
            wire = _format_tool_result(json.dumps(payload, ensure_ascii=False),
                                       "run_command", max_chars=6000)
            ctx = _Ctx()
            ctx.messages.append({"role": "tool", "tool_call_id": "c1",
                                 "content": wire})
            _check_cross_zone_pending(ctx, 0)
            if len(cmd) > 100:
                check("★ 长命令的载荷**没有被包进隔离标签**（包了就认不出来）",
                      wire.startswith("{"), wire[:80])
                check("  而且它仍然解析得开（没被字符切半）",
                      json.loads(wire).get("status")
                      == "pending_command_approval", wire[:80])
            pend = ctx.pending_command
            check("★ %r 的载荷把 pending_command 设成了 active" % cmd,
                  pend.get("active") is True, repr(pend)[:160])
            check("  kind 传对了（%s）" % want_kind, pend.get("kind") == want_kind,
                  repr(pend.get("kind")))
            check("  subject 传对了", pend.get("subject") == cmd,
                  repr(pend.get("subject")))
            check("  request_id 有值（浮窗靠它挡重放）", bool(pend.get("request_id")), "")
            # 规则随载荷走（浮窗靠它把选项 2 的标签换成规则）。
            check("  ★ 前缀规则传对了（%r）" % want_rule,
                  str(payload.get("rule") or "") == want_rule,
                  repr(payload.get("rule")))
            resp = str(ctx.response)
            if len(cmd) > 100:
                # 长命令的正文本来就只显示前 1200 字符（`_PROMPT_COMMAND_LIMIT`）
                check("  ★ 提示语写进了 ctx.response（前 100 字符对得上）",
                      cmd[:100] in resp, repr(resp)[:80])
                check("  ★ 而且**显式标注了省略**（砍了要说出来）",
                      "没有显示" in resp and "完整的那条" in resp, repr(resp)[-120:])
            else:
                check("  ★ 提示语被写进了 ctx.response", cmd in resp,
                      repr(resp)[:120])
            check("  第几次被问也记上了（升级项要用）",
                  ctx.approval_ask_counts.get(want_kind) == 1,
                  repr(ctx.approval_ask_counts))

        # 负对照：不走 json.dumps 的裸字符串，检测器应当一无所获
        # （钉住"载荷必须是 JSON 对象"这条契约，而不是靠运气）
        ctx2 = _Ctx()
        ctx2.messages.append({"role": "tool", "tool_call_id": "c2",
                              "content": "pending_command_approval"})
        _check_cross_zone_pending(ctx2, 0)
        check("★ 负对照：裸字符串不触发（契约是 JSON 对象，不是子串）",
              not ctx2.pending_command.get("active"), repr(ctx2.pending_command))
    finally:
        restore()


def test_pending_status_matches_the_dispatcher() -> None:
    """契约检查：工具回的那个 `status` 串必须真的是派发器在找的串。"""
    print("\n-- 6d. 载荷契约与派发器对得上 --")
    src = (_ROOT / "core" / "pipeline" / "stages" / "agent_loop.py").read_text(
        encoding="utf-8")
    check("派发器里认 `pending_command_approval`", "pending_command_approval" in src,
          "串改了但没同步 —— 工具会静默地不设 pending")
    from core.approval import ASK_COUNTED_KINDS, _TABLES
    check("`command` 在选项表里，且危险档那个 kind **没了**",
          "command" in _TABLES and "command_guarded" not in _TABLES,
          str(sorted(_TABLES)))
    check("`command` 在 ASK_COUNTED_KINDS 里（升级项要计数）",
          "command" in ASK_COUNTED_KINDS, str(sorted(ASK_COUNTED_KINDS)))


# main

def test_a_missing_command_gets_a_hint():
    """命令不存在时补一句"该用什么"。

    实测：模型连试 3 次 `python3` 才换 `python`（`CMD_FAIL` ×3）
    —— 每一次都是一整个 LLM 往返。修法是即时提示（不是往提示词里加一句常驻：
    那每条请求都付费，而这件事几天才撞一次）。
    """
    from core.shell import _add_not_found_hint as H

    r = {"success": False, "stderr": "bash: python3: command not found\n"}
    H(r, 'python3 -c "x"')
    check("bash 的 command not found -> 给了提示", "python" in (r.get("hint") or ""),
          repr(r.get("hint")))
    check("★ `stderr` **没被污染**（那是命令的输出，不许掺框架的话）",
          r["stderr"].strip() == "bash: python3: command not found", repr(r["stderr"]))

    r2 = {"success": False, "stderr": "'python3' is not recognized as an internal or"
                                    " external command"}
    H(r2, "python3 -V")
    check("cmd 那句 `is not recognized as an internal` 也认", r2.get("hint") is not None)

    # 四条负对照（少了它们，"加了提示"与"乱加提示"分不开）
    r3 = {"success": True, "stderr": "bash: python3: command not found"}
    H(r3, "python3 -c 1")
    check("负对照①：**成功**的不加（哪怕 stderr 里出现了那句）", "hint" not in r3)

    r4 = {"success": False, "stderr": "bash: foo: command not found"}
    H(r4, "foo --bar")
    check("负对照②：别名表外的命令不加（不猜）", "hint" not in r4)

    r5 = {"success": False, "stderr": "Traceback (most recent call last):\nNameError"}
    H(r5, "python3 -c bad")
    check("负对照③：失败但**不是「找不到」**的不加", "hint" not in r5)

    r6 = {"success": False, "stderr": "bash: python: command not found"}
    H(r6, "python x.py")
    check("负对照④：`python` 本身没找到时**不给「用 python」**"
          "（那会是个死循环的提示）", "hint" not in r6)


def test_missing_toolchain_says_what_to_do():
    """缺编译器/运行时时，提示要说"怎么做"，不只说"没有"。

    判据：模型的下一条命令通常是"再换一个名字撞一次"（`gcc`->`clang`->`cc`）。
      一句"要装 / 先问用户 / 装不上就直说"能省掉整串往返，
      也挡住"没编译过却说编译过了"。
    """
    from core.shell import _add_not_found_hint as H

    # 这里的 stderr 不带换行 —— 判据只看那两句，换行无关。
    r = {"success": False, "stderr": "bash: gcc: command not found"}
    H(r, "gcc -o a a.c")
    h = r.get("hint") or ""
    check("★ 缺 gcc -> 提示里有「装」和「问用户」", "装" in h and "问用户" in h, h)
    check("★ 也挡「假装编译过」", "别把没编译过" in h, h)

    # 报错点名的是别的命令（`make` 把 gcc 弄丢）—— 证据在 stderr，不在命令里
    r2 = {"success": False,
          "stderr": "make: gcc: command not found"}
    H(r2, "make all")
    check("★★ 从 stderr 里认出缺的是 gcc（不是 make）",
          "gcc" in (r2.get("hint") or ""), repr(r2.get("hint")))

    r3 = {"success": False, "stderr": "bash: go: command not found"}
    H(r3, "go build ./...")
    check("运行时缺了也给一句「先问用户装不装」",
          "问用户" in (r3.get("hint") or ""), repr(r3.get("hint")))

    # 负对照
    r4 = {"success": False, "stderr": "bash: foo: command not found"}
    H(r4, "echo success")          # 命令里含 "cc"，但不该命中
    check("★ 负对照：按词比，不按子串（`success` 不算 `cc`）",
          "hint" not in r4, repr(r4.get("hint")))

    r5 = {"success": False, "stderr": "bash: python: command not found"}
    H(r5, "python x.py")
    check("★ 负对照：`python` 本身缺了**不给**死循环提示",
          "hint" not in r5, repr(r5.get("hint")))


def test_default_cwd_always_exists():
    """默认工作区必须真的存在 —— 全新安装的数据根里没有 `workspace/`。

    失败形状（安装包里实测）：`_resolve_cwd` 回退到 `_default_cwd()`，
    而它曾是裸的相对 `Path("workspace")`，解析到数据根下面；
    那里没有这个目录，于是 `subprocess` 直接
    `NotADirectoryError (WinError 267)`，每一条 `run_command` 都起不来，
    而模型只看到"命令起不来"。
    """
    import os
    import tempfile
    from core import shell as S

    old = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        os.chdir(td)
        try:
            p = S._default_cwd()
            check("★★ 返回的目录真的存在（不是相对空壳）", p.is_dir(), str(p))

            class _WM:
                session_cwd = ""
                security = None

            got, _note = S._resolve_cwd("", _WM())
            check("★★ 回退出来的 cwd 也真的存在", got.is_dir(), str(got))
        finally:
            os.chdir(old)


def _run_all() -> None:
    test_default_cwd_always_exists()
    test_a_missing_command_gets_a_hint()
    test_missing_toolchain_says_what_to_do()
    test_bash_is_probed()
    test_env_whitelist_excludes_every_secret()
    test_env_whitelist_shape()
    test_clean_env_drops_a_planted_secret()
    test_python_resolves_to_the_frameworks_own_interpreter()
    test_nc_a_whitelist_is_load_bearing()
    test_cwd_is_confined_to_workspace()
    test_nc_b_the_confinement_is_load_bearing()
    test_cwd_memory_round_trip()
    test_every_executed_command_is_audited()
    test_audit_records_the_whole_command()
    test_clip_keeps_tail_and_spills()
    test_denied_command_never_asks()
    test_gate_kinds_and_subject()
    test_user_forbidden_dir_reaches_run_command()
    test_grant_then_execute()
    test_timeout_is_clamped()
    test_registration_is_wired()
    test_tool_result_is_isolated()
    test_activity_line_does_not_leak_basename()
    test_pending_status_matches_the_dispatcher()
    test_relax_switch_changes_the_kind_and_says_so()
    test_no_ask_readonly_switch()
    test_tool_payload_reaches_the_real_detector()
    test_bash_latch_is_written_after_the_cache()
    test_shell_kind_prefers_bash()
    test_wrap_ps_carries_the_three_parts()
    test_ps_syntax_error_gets_a_route()
    test_ps_missing_command_gets_a_route()
    test_powershell_really_runs()


def test_bash_latch_is_written_after_the_cache():
    """`_bash_probed` 必须是最后写的那个。

    不变量是「看到闩为真，缓存一定已经写好」—— 而它只能靠写序保证
    （两个格都是无锁的普通模块级变量）。闩若是第一句写的：另一条会话
    （调度器允许并行）在"闩已落、缓存还空"的窗口里读 `bash_path()`
拿到空路径，表现为「找不到 bash，命令执行工具不可用」——
    一次性的、看不出原因的失败。

    判据是源码次序：这件事没有别的观测量（一次探测是几十微秒，窗口抓不到）。
    同族：`test_file_tools` 那几条按 AST 钉"两条路逐字同一句"的。
    """
    import ast
    import inspect
    import textwrap

    from core import shell

    tree = ast.parse(textwrap.dedent(inspect.getsource(shell.bash_path)))
    lines = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in ("_bash_cache", "_bash_probed"):
                lines[t.id] = max(lines.get(t.id, 0), node.lineno)

    # `_bash_probed` 只有一处赋值（落闩）；`_bash_cache` 可能有多处
    #比的是最后一处：闩落下时缓存必然已经写过。
    check("bash 探测：闩(_bash_probed)写在缓存(_bash_cache)之后",
          lines.get("_bash_cache", -1) < lines.get("_bash_probed", -1),
          "行号 %r —— 闩先落，另一条会话会读到空的 bash 路径" % (lines,))


# ----------------------------------------------------------------------
# PowerShell 兜底（没有 Git Bash 的机器）
# ----------------------------------------------------------------------

def test_shell_kind_prefers_bash():
    """栈里同时有 bash 与 PowerShell 时，跑的是 bash。

    这一条防的是"按平台猜"：`os.name == "nt"` 在装了 Git Bash 的机器上
    也是 True，拿它当判据会让提示词告诉模型"你在 PowerShell" ——
    而实际跑的是 bash，模型照着错的语法写。
    """
    from core import shell as S

    if not S.bash_path():
        print("  [skip] 本机没有 bash —— 这一半的判据在 PS 机器上是反的")
        return
    check("有 bash 时 shell_kind() 是 bash",
          S.shell_kind() == "bash", S.shell_kind())


def test_wrap_ps_carries_the_three_parts():
    """`_wrap_ps` 的三块：UTF-8 输出、退出码、cwd 标记。

    纯文本检查，不需要本机有 PowerShell。三块各少一样都会静默失败：
    少编码 -> 中文输出乱码；少退出码 -> 失败报成成功；少标记 -> 目录回退。
    """
    from core import shell as S

    body = S._wrap_ps("Write-Output hi")
    check("先设 UTF-8（PS 5.1 默认 GBK）",
          "OutputEncoding" in body and "UTF8" in body)
    check("先抓 `$?` 再读 `$LASTEXITCODE`",
          body.index("$__crannia_ok") < body.index("$__crannia_rc"))
    check("cwd 用 [char]1 现拼，不把控制符塞进命令行",
          "[char]1" in body and "\x01" not in body)
    check("退出码真的传出去", "exit $__crannia_rc" in body)


def test_ps_syntax_error_gets_a_route():
    """`&&` 在 PowerShell 5.1 上是语法错 —— 回执要说清换什么。

    只喂一个 dict：判据是报错串，不是进程。这一条与下面那条分开，
    因为"语法错"和"命令不存在"是两种形状，合并会让命中哪一个靠猜。
    """
    from core import shell as S

    r = {"success": False, "exit_code": 1,
         "stderr": "At line:1 char:8\n+ echo a && echo b\n"
                   "The token '&&' is not a valid statement separator in this version."}
    S._add_syntax_hint(r)
    check("给出了分隔符的路线", "`;`" in (r.get("hint") or ""), r.get("hint"))

    # 其它语法错：通用出路是"先查官方文档"，而站点要写死在回执里
    #   （`learn.microsoft.com` 在预批准域名表里，查它不会弹审批）。
    saved = S.shell_kind
    S.shell_kind = lambda: "powershell"
    try:
        r2 = {"success": False, "exit_code": 1,
              "stderr": "At line:1 char:12\n+ if (\n+   ~\n"
                        "Missing closing ')' in expression.\n"
                        "    + CategoryInfo          : ParserError: (:) [], "
                        "ParentContainsErrorRecordException"}
        S._add_syntax_hint(r2)
        hint = r2.get("hint") or ""
        check("语法错 -> 指向官方文档并给出站点",
              "learn.microsoft.com/powershell" in hint, hint[:120])
        check("语法错 -> 说了别连着试同一种", "别连着试" in hint, hint[:140])
    finally:
        S.shell_kind = saved


def test_ps_missing_command_gets_a_route():
    """PowerShell 的两种措辞都要认，且要说"别重试"。

    这是「agent 大战 PowerShell」的解药：最常见的失败是照着 bash 语法写、
    报错、再把同一条发一次。报错回执里给路线，比往提示词里加常驻句子便宜。
    """
    from core import shell as S

    saved = S.shell_kind
    S.shell_kind = lambda: "powershell"
    try:
        for stderr, label in (
            ("nosuch : The term 'nosuch' is not recognized as the name of a "
             "cmdlet, function, script file", "英文"),
            ("无法将“nosuch”项识别为 cmdlet、函数、脚本文件或可运行程序的名称。", "中文"),
        ):
            r = {"success": False, "exit_code": 1, "stderr": stderr}
            S._add_not_found_hint(r, "nosuch")
            hint = r.get("hint") or ""
            check("%s措辞被认出来" % label, "PowerShell" in hint, hint[:80])
            check("%s：说了别重试" % label,
                  "别把同一条再发一次" in hint, hint[:120])
    finally:
        S.shell_kind = saved


def test_powershell_really_runs():
    """实际运行：能跑、能回传 cwd、失败不报成功。

    没有 PowerShell 就 skip —— 它是兜底路径，不是每台机器都有。
    这条只证明「这台机器上它能跑」；「没有 Git Bash 的机器」由用户侧验收
    （本机 PATH 上还有 Git 的 Unix 工具，mock 掉 `bash_path` 并不等于一台干净机器）。
    """
    from core import shell as S

    if not S.powershell_path():
        print("  [skip] 本机没有 PowerShell")
        return

    saved_bash = S.bash_path
    saved_probed, saved_cache = S._bash_probed, S._bash_cache
    S.bash_path = lambda: ""
    S._bash_probed = True
    S._bash_cache = ""
    try:
        ok = S._run("Write-Output hi; Set-Location ..; Write-Output done",
                    Path("."), 20000)
        check("命令跑成功", ok.get("success") is True, repr(ok)[:200])
        check("cwd 回传了（Set-Location 生效）",
              bool(ok.get("cwd")), repr(ok.get("cwd")))
        bad = S._run("nosuchcmd_xyz --flag", Path("."), 20000)
        check("不存在的命令 -> 失败（不是 rc=0）",
              bad.get("success") is False
              and bad.get("exit_code") not in (0, None),
              repr(bad)[:200])
    finally:
        S.bash_path = saved_bash
        S._bash_probed, S._bash_cache = saved_probed, saved_cache


def main() -> int:
    global _FAIL
    print("=" * 62)
    print("shell execution layer (core/shell.py)")
    print("=" * 62)

    # 自带机检
    # 这个文件会真的起进程、真的写文件（输出落盘），所以它必须自己证明
    # 没往生产目录里写东西 —— "要人记得跑的检查等于没有"。
    before = prod_snapshot()
    try:
        with sandbox():
            _run_all()
    finally:
        after = prod_snapshot()

    print("\n" + "=" * 62)
    leaked = prod_diff(before, after)
    if leaked:
        _FAIL += 1
        globals()["_FAILED"].append("测试污染了生产目录")
        print("!! 这次运行改动了生产目录：%r" % (leaked,))
    else:
        print("污染自查: 生产 workspace/data/logs 未被触碰")

    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
