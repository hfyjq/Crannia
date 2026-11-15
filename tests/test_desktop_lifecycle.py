# -*- coding: utf-8 -*-
"""后端子进程的生命周期 —— 三层读者。

## 它从哪来

壳里那个后端子进程最初只有一句收尾：`will-quit` 里 `backend.kill()`。
现在补成体系：

    ① 终止三级降级：优雅 shutdown → 10s → SIGTERM → 5s → SIGKILL → 5s → 抛
    ② 退出前 `inspectQuit`：2 秒 deadline，超时/失败 -> 按"有任务在跑"处理
    ③ 诊断信息有界（64KB）· ④ `requireGraceful`（exitCode 0 但没 ack 也算不干净）

[!] 一处刻意不同的做法：若第一级走 IPC（`child.send`），而
Python 子进程没有 `process.send` -> 我们的第一级是
HTTP `POST /api/shutdown`（那边回一个 `ack: true`）。**降级链的形状照抄，
第一级的载体不是。**

## 三层

① 行为层（真子进程）：`apps/desktop/backend-lifecycle.cjs` 不 import electron，
   且只吃"像 child 的东西" -> 夹具真的 spawn 几个 node 子进程（包括
   赖着不走那种）来验降级链 —— 不是 mock。
② 结构层：`main.cjs` 那两关（`before-quit` / `will-quit`）跑不了 -> 源级读者钉住
   「问过」「收尾了」「不再有裸的 `backend.kill()`」。
③ HTTP 层：`POST /api/shutdown` 用真的 Flask test client 打，
   把 `_graceful_exit` 换成假的（不然测试进程会自己退出）。

## 它怎么证明自己不是恒真

`test_the_judgements_can_go_red`：四条变异（第一级不调 / fail-safe 写反 /
诊断不设上限 / 等待器谎报"它退了"），每条都要让某个具体用例变红。

用法：
    .venv/Scripts/python.exe tests/test_desktop_lifecycle.py

[!] 输出一律 ASCII 安全字符（Windows 控制台默认 GBK）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

from _sandbox import prod_diff, prod_snapshot, sandbox      # noqa: E402
from test_memory_ui import _strip_js_comments               # noqa: E402

_MAIN = _ROOT / "apps" / "desktop" / "main.cjs"
_LIFE = _ROOT / "apps" / "desktop" / "backend-lifecycle.cjs"
_SPLASH = _ROOT / "apps" / "desktop" / "splash.html"


# ① 行为层 —— 真 spawn 子进程

_NODE_HARNESS = r"""
"use strict";
const { spawn } = require("node:child_process");
const modPath = process.argv[2];
const checks = [];
let fatal = "";
function check(id, ok, detail) { checks.push({ id: id, ok: !!ok, detail: String(detail === undefined ? "" : detail) }); }
async function safe(id, fn) {
  try { const r = await fn(); check(id, r[0], r[1]); }
  catch (e) { check(id, false, "threw: " + ((e && e.message) || e)); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
//: ★★ 起过的子进程全部记账 —— 收尾时**一律 SIGKILL**。
//    判据是一次实测：变异版里"谎报它退了"会让降级链**提前返回、不真杀**，
//    于是那几条还活着的子进程把**夹具自己**吊住（node 的 event loop 不会在
//    还有活子进程时退出）-> 表现为"夹具超时"，看起来像**被测代码**有问题。
const SPAWNED = [];
function spawnIdle() {
  const c = spawn(process.execPath, ["-e", "setInterval(function(){},1000)"], { stdio: "ignore" });
  SPAWNED.push(c);
  return c;
}
function reap() {
  for (const c of SPAWNED) { try { c.kill("SIGKILL"); } catch (e) { /* 已经没了 */ } }
}

async function main() {
  const life = require(modPath);
  const FAST = [{ level: "graceful", ms: 250 }, { level: "SIGTERM", ms: 2500 },
                { level: "SIGKILL", ms: 2500 }];

  // ── ① 接了电话就走 -> 停在 graceful 那一级 ────────────────────
  await safe("graceful_level", async () => {
    const c = spawnIdle(); await sleep(150);
    const r = await life.stopChild(c, { graceful: async () => { c.kill("SIGTERM"); }, steps: FAST });
    check("graceful_gone", c.exitCode !== null || c.signalCode !== null, "还活着");
    return [r.ok && r.level === "graceful", JSON.stringify(r)];
  });

  // ── ② 不接电话（优雅那一级抛）-> 降到 SIGTERM ─────────────────
  await safe("sigterm_level", async () => {
    const c = spawnIdle(); await sleep(150);
    const r = await life.stopChild(c, {
      graceful: async () => { throw new Error("没人接"); }, steps: FAST });
    check("sigterm_gone", c.exitCode !== null || c.signalCode !== null, "还活着");
    return [r.ok && r.level === "SIGTERM", JSON.stringify(r)];
  });

  // ── ③ 三级都走完它还在 -> ok:false（**不假装成功**）────────────
  await safe("all_levels_failed", async () => {
    const fake = { exitCode: null, signalCode: null, kill() {}, once() {} };
    const r = await life.stopChild(fake, {
      graceful: async () => {}, steps: [{ level: "graceful", ms: 40 },
                                        { level: "SIGTERM", ms: 40 },
                                        { level: "SIGKILL", ms: 40 }] });
    return [!r.ok && r.level === "failed", JSON.stringify(r)];
  });

  // ── ④ 已经退了的 / 根本没有子进程 -> 不白等 ────────────────────
  await safe("already_exited", async () => {
    const c = spawn(process.execPath, ["-e", "0"], { stdio: "ignore" });
    await new Promise((r) => c.once("exit", r));
    const t0 = Date.now();
    const r = await life.stopChild(c, { steps: FAST });
    return [r.ok && r.level === "already-exited" && Date.now() - t0 < 500, JSON.stringify(r)];
  });
  await safe("no_process", async () => {
    const r = await life.stopChild(null, { steps: FAST });
    return [r.ok && r.level === "no-process", JSON.stringify(r)];
  });

  // ── ⑤ inspectQuit：四种回答，**三种必须算"有活"** ─────────────
  await safe("inspect_busy", async () => {
    const v = await life.inspectQuit(async () => ({ agent_busy: true, agent_task: "跑活" }));
    return [v.busy === true && v.reason.includes("跑活"), JSON.stringify(v)];
  });
  await safe("inspect_idle", async () => {
    // [!] `approval_pending` 必须**显式给空串** —— "确实没有待审批"与
    //   "那一格读不到"是两件事，只有前者算空闲。
    const v = await life.inspectQuit(async () => ({ agent_busy: false, approval_pending: "" }));
    return [v.busy === false, JSON.stringify(v)];
  });
  // ★★ 据反馈报的形状：**挂着的待审批也算"有活在跑"**。
  await safe("inspect_pending_is_busy", async () => {
    const v = await life.inspectQuit(async () => ({ agent_busy: false, approval_pending: "delete" }));
    return [v.busy === true && v.reason.includes("delete"), JSON.stringify(v)];
  });
  await safe("inspect_pending_unknown_is_busy", async () => {
    const v = await life.inspectQuit(async () => ({ agent_busy: false, approval_pending: "unknown" }));
    return [v.busy === true, JSON.stringify(v)];
  });
  await safe("inspect_missing_pending_is_busy", async () => {
    //: 那一格**缺失** -> 按"有"处理（读不到 ≠ 没有）。这一条是**负对照**：
    //: 少了它，"读不到"会被写成一个真值判断而**静静塌成"没有"**。
    const v = await life.inspectQuit(async () => ({ agent_busy: false }));
    return [v.busy === true, JSON.stringify(v)];
  });
  await safe("inspect_timeout_is_busy", async () => {
    const v = await life.inspectQuit(() => new Promise(() => {}), 120);
    return [v.busy === true, JSON.stringify(v)];
  });
  await safe("inspect_error_is_busy", async () => {
    const v = await life.inspectQuit(async () => { throw new Error("连不上"); }, 500);
    return [v.busy === true, JSON.stringify(v)];
  });
  await safe("inspect_garbage_is_busy", async () => {
    const v = await life.inspectQuit(async () => null, 500);
    return [v.busy === true, JSON.stringify(v)];
  });

  // ── ⑥ 诊断：**有界**，而且**丢了要说丢了** ────────────────────
  await safe("diag_bounded", async () => {
    const d = life.createDiagnostics(100);
    d.push("x".repeat(70)); d.push("y".repeat(70));
    check("diag_dropped", d.dropped() === 40, String(d.dropped()));
    check("diag_tail", d.text().slice(-3) === "yyy", d.text().slice(-6));
    return [d.text().length === 100, "len=" + d.text().length];
  });

  // ── ⑦ 更新交接那一档（`requireGraceful`）─────────────────────
  await safe("clean_exit", async () => {
    const clean = life.isCleanExit({ ok: true, level: "graceful" }, { ack: true });
    check("clean_needs_ack", life.isCleanExit({ ok: true, level: "graceful" }, { ack: false }) === false);
    check("clean_rejects_sigterm", life.isCleanExit({ ok: true, level: "SIGTERM" }, { ack: true }) === false);
    check("clean_rejects_failed", life.isCleanExit({ ok: false, level: "failed" }, { ack: true }) === false);
    return [clean === true, String(clean)];
  });
}

main().catch((e) => { fatal = String((e && e.stack) || e); })
  .then(() => { reap(); })
  .then(() => { process.stdout.write(JSON.stringify({ checks: checks, fatal: fatal })); });
"""

#: 夹具真的在跑（不是"零条检查也算通过"）。
_MUST_BE_PRESENT = (
    "graceful_level", "graceful_gone", "sigterm_level", "sigterm_gone",
    "all_levels_failed", "already_exited", "no_process",
    "inspect_busy", "inspect_idle", "inspect_timeout_is_busy",
    "inspect_error_is_busy", "inspect_garbage_is_busy",
    #: 三条（挂着的待审批 = 有活在跑）——
    #: [!] 必须列在这里：不列的话它们照样跑、结果没人看，
    #:   而"没红"与"没跑到"长得一模一样。
    "inspect_pending_is_busy", "inspect_pending_unknown_is_busy",
    "inspect_missing_pending_is_busy",
    "diag_bounded", "diag_dropped", "diag_tail", "clean_exit", "clean_needs_ack",
)


def _run_harness(module_path: Path):
    node = shutil.which("node")
    if not node:
        # [!] 必须 raise：正常返回会被印成 PASS
        #     -> 在没装 node 的机器上这就是"一条会自己跳过的护栏"。
        raise AssertionError(
            "没有 node —— 这条护栏无法运行，不算通过。"
            "（装 node，或从这里开始就别再靠肉眼看降级链那一层）")
    with tempfile.TemporaryDirectory() as tmp:      # 不落进工作区
        js = Path(tmp) / "life_harness.cjs"
        js.write_text(_NODE_HARNESS, encoding="utf-8")
        r = subprocess.run([node, str(js), str(module_path)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    out = (r.stdout or "").strip()
    try:
        payload = json.loads(out)
    except Exception:
        raise AssertionError(
            "夹具没有吐出 JSON（rc=%s）\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (r.returncode, out[-1200:], (r.stderr or "")[-1200:]))
    if payload.get("fatal"):
        raise AssertionError("夹具自己崩了：\n" + payload["fatal"][-1500:])
    return {c["id"]: (c["ok"], c["detail"]) for c in payload["checks"]}


def test_the_degradation_ladder_works():
    print("\n-- 1. 真跑一遍：三级降级 + inspectQuit + 有界诊断（真子进程）--")
    res = _run_harness(_LIFE)
    missing = [k for k in _MUST_BE_PRESENT if k not in res]
    assert not missing, "夹具少了这些检查：%r（夹具被改窄了？）" % (missing,)
    bad = ["%s -> %s" % (k, res[k][1]) for k, v in res.items() if not v[0]]
    assert not bad, "降级链不对：\n   " + "\n   ".join(sorted(bad))
    print("  [ok]   %d 条（含「优雅那一级真走通了」与「三级都走完就认输」）" % len(res))


# ② 结构层

#: 结构判据的输入是两段源码：`main.cjs`（接线）与 `lifecycle`（判据本体）。
def _structure_checks(raw: str, life_raw: str):
    src = _strip_js_comments(raw)
    out = []

    # 取 `splash:retry` 那一道闸自己的身子（到下一个 `ipcMain.` 为止），
    #   不拿 `[\s\S]{0,n}` 去够 —— 那个上界是猜的，够远一点就白拿别人的
    #   `stopBackend()` 当证据（否则负对照打不中）。
    def _handler(name):
        m = re.search(r'ipcMain\.\w+\(\s*"%s"\s*,' % re.escape(name), src)
        if not m:
            return ""
        nxt = re.search(r"ipcMain\.\w+\(", src[m.end():])
        return src[m.end():m.end() + (nxt.start() if nxt else len(src))]

    retry_body = _handler("splash:retry")

    out.append(("`main.cjs` require 了 `./backend-lifecycle.cjs`",
                'require("./backend-lifecycle.cjs")' in raw, ""))
    out.append(("`before-quit` 那一关**问过**（走 `askThenQuit`）",
                re.search(r'app\.on\("before-quit",\s*\(event\)\s*=>\s*\{[^}]*askThenQuit\(\)',
                          src) is not None, ""))
    out.append(("`will-quit` 那一关**收尾**（走 `stopBackend`，不是裸 kill）",
                re.search(r'app\.on\("will-quit",\s*\(event\)\s*=>\s*\{[^}]*stopBackend\(\)',
                          src) is not None, ""))
    out.append(("★ 裸 `backend.kill(` **已经拆掉**（锚点就在这一条上）",
                "backend.kill(" not in src, "还在"))
    out.append(("诊断**进了缓冲**（stdout / stderr 两处都 push）",
                src.count("backendDiag.push(chunk)") == 2, src.count("backendDiag.push(chunk)")))
    out.append(("★ `inspectQuit` 的 deadline 就是 2000ms（判据里的那个数）",
                re.search(r"api\(\"GET\", \"/api/status\"\)\.then\(\(r\) => r\.json\), 2000\)", src)
                is not None, ""))
    out.append(("★ `requireGraceful` 存在、且真的走 `isCleanExit`",
                "requireGraceful = false" in src and "life.isCleanExit" in src, ""))
    out.append(("★ 两级各有防重入的 flag（同步那一关不许 await -> 必须能重入）",
                src.count("quitChecked") >= 3 and src.count("backendStopped") >= 3,
                "quitChecked=%d backendStopped=%d" % (src.count("quitChecked"),
                                                      src.count("backendStopped"))))
    out.append(("★ 重试那条路**先收尾再重启**（`app.exit()` 会跳过 `will-quit`）",
                "stopBackend(" in retry_body
                and 0 <= retry_body.find("stopBackend(") < retry_body.find("app.exit("),
                "retry 身子 %d 字，stopBackend@%d app.exit@%d"
                % (len(retry_body), retry_body.find("stopBackend("),
                   retry_body.find("app.exit("))))

    # 判据本体那一侧
    body = _strip_js_comments(life_raw)
    out.append(("三级表是**照抄的那三个数**（10s / 5s / 5s）",
                all(x in body for x in ("10000", "5000", "5000")), ""))
    out.append(("诊断上限是 64KB",
                "64 * 1024" in body, ""))
    out.append(("★ `inspectQuit` 的兜底是 **busy:true**（fail-safe 的方向）",
                body.count("busy: true") >= 3, body.count("busy: true")))
    return out


_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + str(detail)) if detail else ""))


def test_the_wiring_is_structurally_present():
    print("\n-- 2. 接线：两关都接上了 / 裸 kill 没了 / 判据只在一处 --")
    for label, ok, detail in _structure_checks(
            _MAIN.read_text(encoding="utf-8"), _LIFE.read_text(encoding="utf-8")):
        check(label, ok, detail)


def test_the_renderer_recovers_once_before_it_gives_up():
    """渲染崩溃：先自恢复一次，第二次才弹框；`launch-failed` 不自动。

    三件事各是一个判据，缺一条的后果都不同：
    · 没有恢复 —— 用户看到白屏（修前的形状）
    · 没有上限 —— 可复现的崩溃会让窗口一直闪，比白屏还难查
    · `launch-failed` 也自动 —— 那是"渲染进程压根没启动"，重载只是把白屏延长 3 秒
    """
    print("\n-- 2b. 渲染崩溃的自恢复 --")
    src = _MAIN.read_text(encoding="utf-8")

    check("有 recoverRenderer", "function recoverRenderer" in src)
    check("launch-failed 不走自动恢复",
          'd.reason === "launch-failed"' in src, "")
    check("自动恢复有次数上限", "used.n >= 1" in src, "")
    check("恢复页文案是 #01", "#01 渲染组件异常" in src, "")
    check("3 秒后才回原地址", "}, 3000);" in src, "")

    # 顺序：崩溃处理里先试恢复，赌不了才弹框。
    idx = src.find('on("render-process-gone"')
    seg = src[idx:idx + 2000] if idx >= 0 else ""
    check("崩溃处理接上了 recoverRenderer", "recoverRenderer(" in seg, "")
    if "recoverRenderer(" in seg and "showRendererGoneNotice(" in seg:
        check("先试恢复、再弹框",
              seg.index("recoverRenderer(") < seg.index("showRendererGoneNotice("),
              "")
    else:
        check("先试恢复、再弹框", False, "两段之一不见了")


def test_startup_failures_are_triaged_before_the_message():
    """`#00` / `#03` 的判断：`00` 只认硬信号，其余先试着救一次。

    判错 `00` 的代价是让用户白重装一遍 —— 重装完还是同样的问题，
    比现在的"报个错"更差。所以 00 的判据必须窄，03 必须是兜底。
    """
    print("\n-- 2c. 启动失败的分诊 --")
    src = _MAIN.read_text(encoding="utf-8")

    check("有 classifyFailure", "function classifyFailure" in src)
    check("00 认 ENOENT", 'if (/ENOENT/.test(msg)) return "00";' in src)
    check("00 认缺模块",
          "ModuleNotFoundError|No module named|ImportError" in src)
    check("其余是 03（兜底）", 'return "03";' in src)

    check("有 restoreConfig", "function restoreConfig" in src)
    # 恢复必须走独立进程：要恢复的场景恰恰是后端没起来，API 一个都调不到。
    check("恢复走独立 python 进程（不走 HTTP）",
          "spawn(pythonExe()" in src and "last_good.restore()" in src,
          "")
    check("恢复不依赖 Flask",
          "/api/recover" not in src, "别把恢复做成一个 API —— 后端可能压根没起")

    idx = src.find("await startFramework();")
    seg = src[idx:idx + 700] if idx >= 0 else ""
    check("whenReady 的 catch 先分诊再决定",
          "recoverOrExplain(" in seg, seg[:160])
    check("救活了继续建主窗",
          "if (!(await recoverOrExplain(err))) return;" in src, "")


def test_backend_diag_is_used_through_its_real_api():
    """`backendDiag` 是对象，不是数组 —— 用错方法名只在出事那天才炸。

    `backendDiag` 没有 `join`。写成 `.join("")` 会当场抛 TypeError，
    而它在 `whenReady` 的 catch 里被 await —— 一条没人接的 Promise 拒绝，
    小窗永远停在「正在初始化」，一个字的提示都没有。

    这类错平时全绿：那条路只在启动失败时才走（同"跑了 ≠ 在生产会走的
    那条路上跑过"那一族）。所以判据只能是静态的：

    从 `createDiagnostics()` 的真实返回里读出方法名单（不在这里抄第二份
    —— 抄一份就会与它各自漂），再要求 `main.cjs` 里每一次
    `backendDiag.<名>` 都在那份名单里。
    """
    print("\n-- 2d. backendDiag 只用它真的有的方法 --")
    life = _LIFE.read_text(encoding="utf-8")
    src = _MAIN.read_text(encoding="utf-8")

    i = life.find("function createDiagnostics")
    check("backend-lifecycle.cjs 里有 createDiagnostics", i >= 0, "")
    body = life[i:] if i >= 0 else ""
    j = body.find("\n}\n")
    body = body[:j] if j > 0 else body[:4000]

    # 对象字面量的成员：普通方法 `push(chunk) {` 与箭头属性 `text: () => …`
    #   两种写法都要接住（只认 `(` 会漏掉后者，而那正是本次踩的那个）。
    api = set(re.findall(r"^ {4}(\w+)\s*[:(]", body, re.M))
    check("读到了真 API（至少 push / text）", {"push", "text"} <= api,
          repr(sorted(api)))

    used = set(re.findall(r"backendDiag\.(\w+)", src))
    bad = sorted(used - api)
    check("main.cjs 每一次用法都在名单里", not bad,
          "用了不存在的方法 %r（真 API：%s）" % (bad, sorted(api)))
    check("分诊那条路读的是 text()", "text" in used, repr(sorted(used)))


def test_the_failure_screen_tells_the_truth():
    """失败屏上的原因：不写死、不闪走、不吞掉真实那一行。

    三条判据：

    1. 写死一句「30 秒内没有就绪」只在等满 30 秒那一种失败里是真的。
       后端进程一退（端口被占、依赖缺失）失败是秒级的，那句话把原因说反。
    2. `#03` 那句话在秒级失败里一闪而过，用户只记住最后的 `#00`。
    3. `#00` 只说「请更换安装包」，而真实原因是 `PermissionError: WinError 10013`
       —— 一个指不到任何地方的话。
    """
    print("\n-- 2e. 失败屏说的是真话 --")
    splash = _SPLASH.read_text(encoding="utf-8")
    src = _MAIN.read_text(encoding="utf-8")
    # 注释里写着那句旧文案（记的是"为什么删"），它不是被显示的东西。
    #   同本仓库那条「'不许出现某字符串'的检查器会自己打自己」—— 判据要打在
    #   会被渲染的那一层上，所以先把 HTML 注释剥掉。
    shown = re.sub(r"<!--.*?-->", "", splash, flags=re.S)

    check("splash 不再写死「秒内没有就绪」",
          "秒内没有就绪" not in shown, "那句只在「等满 30 秒」时才是真的")
    check("原因那一格还在（id=f-why）", 'id="f-why"' in splash, "")
    check("而且带 why 类（样式挂得上）",
          re.search(r'id="f-why"[^>]*class="why"', splash) is not None, "")

    check("有 MIN_03_MS", re.search(r"const MIN_03_MS = \d+;", src) is not None, "")
    # 失败那条支路：先等够，再收计时器，最后才报 #00。
    # 只从 `catch (err2)` 起算 —— 成功那条支路也有一个 `clearInterval(tick)`，
    #   从 console.error 往回切会把它算进来，顺序断言就成了假红。
    c = src.find("} catch (err2) {")
    seg = src[c:c + 900] if c >= 0 else ""
    check("失败支路里等了 MIN_03_MS",
          "MIN_03_MS - (Date.now() - t0)" in seg, seg[:200])
    if "MIN_03_MS - (Date.now() - t0)" in seg and "clearInterval(tick)" in seg:
        check("先等、再收计时器（顺序反了倒计时会停在中间）",
              seg.index("MIN_03_MS - (Date.now() - t0)")
              < seg.index("clearInterval(tick)"), "")
    else:
        check("先等、再收计时器", False, "两段之一不见了")

    check("有 _failureDetail（把诊断尾巴最后一行带出来）",
          "function _failureDetail(" in src, "")
    n = len(re.findall(r"_failureDetail\((err|err2)\)", src))
    check("两条失败支路都用了它", n >= 2, "只用了 %d 次" % n)


def test_the_structure_guard_can_go_red():
    """负对照：合成坏样本，每一条都必须有它自己的红。"""
    print("\n-- 3. 判据本身：坏样本一条都躲不掉 --")
    good = _MAIN.read_text(encoding="utf-8")
    good_life = _LIFE.read_text(encoding="utf-8")

    def _reds(sample, life_sample=None):
        return [lab for lab, ok, _d in _structure_checks(
            sample, good_life if life_sample is None else life_sample) if not ok]

    cases = [
        ("`before-quit` 那关又变成一句话（不问）",
         good.replace("event.preventDefault();\n  askThenQuit();",
                      "quitting = true;", 1),
         "`before-quit` 那一关**问过**（走 `askThenQuit`）"),
        ("`will-quit` 改回裸 `backend.kill()`",
         good.replace("  event.preventDefault();\n  stopBackend()",
                      "  backend.kill();\n  return;\n  stopBackend()", 1),
         "★ 裸 `backend.kill(` **已经拆掉**（锚点就在这一条上）"),
        ("诊断不再进缓冲",
         good.replace("backendDiag.push(chunk); ", "", 2),
         "诊断**进了缓冲**（stdout / stderr 两处都 push）"),
        ("重试那条路又变回裸的 `app.exit(0)`（`stopBackend` 整个删掉）",
         re.sub(r"ipcMain\.on\(\"splash:retry\",[\s\S]*?\n\}\);",
                'ipcMain.on("splash:retry", (event) => {\n'
                '  if (rejectForeign(event, splash, ["shell"], "splash:retry")) return;\n'
                '  app.relaunch(); app.exit(0);\n});', good, count=1),
         "★ 重试那条路**先收尾再重启**（`app.exit()` 会跳过 `will-quit`）"),
    ]
    for name, sample, must_red in cases:
        reds = _reds(sample)
        check("坏样本被抓：%s" % name, must_red in reds, repr(reds[:4]))

    # 判据本体那一侧的坏样本
    life_cases = [
        ("诊断上限被悄悄改小（64KB 那条是独立取的）",
         good_life.replace("MAX_DIAGNOSTIC_CHARS = 64 * 1024;",
                           "MAX_DIAGNOSTIC_CHARS = 8 * 1024;", 1),
         "诊断上限是 64KB"),
    ]
    for name, sample, must_red in life_cases:
        reds = _reds(good, sample)
        check("坏样本被抓：%s" % name, must_red in reds, repr(reds[:4]))

    check("好样本（真源码）在结构层全绿",
          not _reds(good))


# ③ HTTP 层：`/api/shutdown`

def test_the_shutdown_route_acks_before_it_dies():
    print("\n-- 4. `POST /api/shutdown`：回执**先发**、收摊在后 --")
    import web.server as S

    c = S.app.test_client()
    r = c.post("/api/shutdown")
    check("没带 token -> 403（一个能关掉别人的端点不许裸奔）",
          r.status_code == 403, str(r.status_code))

    called = []
    real = S._graceful_exit
    # 把"真正结束进程"那一下换成假的 —— 不换的话，这条测试会**把测试进程
    #    自己送走**，而症状是"整个套件凭空少了一个文件"（最难查的那一种）。
    S._graceful_exit = lambda: called.append(time.time())
    try:
        t0 = time.time()
        r = c.post("/api/shutdown?token=" + S._LAUNCH_TOKEN)
        dt = time.time() - t0
        d = r.get_json() or {}
        check("带 token -> 200 + `ack: true`", r.status_code == 200 and d.get("ack") is True,
              "%s %r" % (r.status_code, d))
        check("★★ 收摊**不等人**：回执立刻返回（不是等 0.3 秒才回）", dt < 0.25, "%.3fs" % dt)
        check("★ 回执**先于**收摊（下手那一刻还没到）", not called, repr(called))
        time.sleep(0.55)
        check("★★ 0.3 秒后那一下**真的抬手了**", len(called) == 1, repr(called))
        check("★★ 而且它是在**回执之后**才抬的",
              bool(called) and called[0] >= t0, "%.3f vs %.3f" % (called[0] if called else -1, t0))
    finally:
        S._graceful_exit = real
        S._FRAMEWORK_RUNNING = False


def test_the_status_route_reports_a_pending_approval():
    """壳退出前那一问读的是 `/api/status` 的 `agent_busy`，而"挂起即停车"
    那一刻它已经翻回 false -> 挂着一条待审批时壳会一声不响地退
    （退出会把那条审批连同等着它的那一轮一起丢掉）。-> 后端必须把那一格报出来。

    [!] 三种回答必须分得开：`""` = 确实没有 · `"<kind>"` = 挂着一条 ·
    `"unknown"` = 读不到（读不到 ≠ 没有，壳那边按"有"处理）。
    """
    print("\n-- 4b. `GET /api/status`：**挂着的待审批**也要报出来 --")
    import web.server as S

    c = S.app.test_client()
    real_running, real_sched = S._FRAMEWORK_RUNNING, S._scheduler
    try:
        S._FRAMEWORK_RUNNING = False
        d = (c.get("/api/status?token=" + S._LAUNCH_TOKEN).get_json() or {})
        check("★ 那一格在（壳照名字取）", "approval_pending" in d, repr(sorted(d))[:200])
        check("★ 框架没跑 -> **空串**（那种状态下不可能有待审批，不是'读不到'）",
              d.get("approval_pending") == "", repr(d.get("approval_pending")))

        class _Sched:
            def pending_approval(self, key):
                return {"kind": "delete", "request_id": "rid-x"}

        S._FRAMEWORK_RUNNING, S._scheduler = True, _Sched()
        d = (c.get("/api/status?token=" + S._LAUNCH_TOKEN).get_json() or {})
        check("★★ 挂着 delete -> 报 `delete`（壳拿它写确认框那句）",
              d.get("approval_pending") == "delete", repr(d.get("approval_pending")))

        class _Boom:
            def pending_approval(self, key):
                raise RuntimeError("炸了")

        S._scheduler = _Boom()
        d = (c.get("/api/status?token=" + S._LAUNCH_TOKEN).get_json() or {})
        check("★★ 读不到 -> `unknown`（**不许**报成'没有'）",
              d.get("approval_pending") == "unknown", repr(d.get("approval_pending")))
    finally:
        S._FRAMEWORK_RUNNING, S._scheduler = real_running, real_sched


# ④ 变异

#: `(名字, 原文, 换成, 必须变红的检查 id)`。
_MUTATIONS = [
    ("三级降级里的**第一级不调**（等于退回两级）",
     '      if (!opts.graceful) continue;',
     "      continue;",
     "graceful_level"),
    ("`inspectQuit` 的 fail-safe **写反**（问不出来当没事）",
     "    return { busy: true, reason: `问不出来（${err && err.message ? err.message : err}）` };",
     "    return { busy: false, reason: `问不出来（${err && err.message ? err.message : err}）` };",
     "inspect_timeout_is_busy"),
    ("诊断缓冲**不截断**（64KB 那条形同虚设）",
     "        text = text.slice(-maxChars);",
     "",
     "diag_bounded"),
    ("等待器**谎报**「它退了」",
     "    const timer = setTimeout(() => finish(false), ms);",
     "    const timer = setTimeout(() => finish(true), ms);",
     "all_levels_failed"),
    # 两条（挂着的待审批 = 有活在跑）。
    ("「那一格读不到」被悄悄塌成「没有」（那条判据）",
     '  if (pend === undefined || pend === null) {',
     '  if (false) {',
     "inspect_missing_pending_is_busy"),
    ("真值判断**写反**（只有空串才算挂着）",
     '  if (pend) {',
     '  if (pend === "") {',
     "inspect_pending_is_busy"),
]


def test_the_judgements_can_go_red():
    """变异验证：四条判据，四具尸首。先看它红，再看它绿。"""
    print("\n-- 5. 变异：每条判据都得有它自己那一具尸首 --")
    src = _LIFE.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as mtmp:
        for name, old, new, must_red in _MUTATIONS:
            n = src.count(old)
            # 靶子自查（打偏的症状是"全绿"）。
            assert n == 1, "变异靶子打偏了：%r 出现 %d 次（要求恰好 1 次）" % (old[:60], n)
            mut = Path(mtmp) / "mutant.cjs"
            mut.write_text(src.replace(old, new, 1), encoding="utf-8")
            res = _run_harness(mut)
            ok, detail = res.get(must_red, (None, "这条检查压根没跑"))
            check("尸首：%s" % name, ok is False,
                  "预期 `%s` 变红，实得 ok=%r %s" % (must_red, ok, detail))
        check("原模块在同一批检查下全绿",
              all(v[0] for v in _run_harness(_LIFE).values()))


# ----------------------------------------------------------------------

def _main() -> int:
    global _FAIL
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    before = prod_snapshot()
    with sandbox():
        for _name, fn in tests:
            try:
                fn()
            except AssertionError as exc:
                check(_name, False, "AssertionError: %s" % (exc,))
            except Exception as exc:  # noqa: BLE001
                check(_name, False, "%s: %s" % (type(exc).__name__, exc))
    leaked = prod_diff(before, prod_snapshot())
    if leaked:
        _FAIL += 1
        print("\n  [FAIL] ★ 测试污染了生产目录：%r" % (leaked,))
    print("\n%d/%d passed" % (_PASS, _PASS + _FAIL))
    if _FAILED:
        print("失败项：")
        for x in _FAILED:
            print("  - " + x)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(_main())
