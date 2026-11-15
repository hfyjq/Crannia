#!/usr/bin/env python3
"""自动审查器探针 —— 把一批真实动作喂进我们自己的闸与分类器，看它们怎么判。

    .venv/Scripts/python.exe scripts/auto_probe.py
    .venv/Scripts/python.exe scripts/auto_probe.py --only del_old,del_old_named

## 它回答什么

这个探针要回答的是：把一批真实动作喂进我们自己的闸与分类器，看它们怎么判。

## 它走的是生产那条路（这一条是纪律，不是实现细节）

  * 闸 = `core.agent_loop._auto_review_gate` —— 生产唯一入口，不另写一份判断
  * 工具表 / 权限档 / 声明 = `bus.registry`，先 `boot_registry()` 装载
    （不装载的话 `get_tool_permission` 全返回 None => 量到的是零，
    而零看起来像一个结论）
  * 工作区 / 路径分级 = `SecurityGuard` + `WorkspaceManager`，与生产同一个类
  * 分类器那次调用 = `core.auto_review.classify`，真 provider
    （走 `config.yaml` 那一支，与 `bus/core.py::init_bus` 同一条路）

## 三条不许改的

  1. 不打印任何密钥（只报 provider 名与模型名）；
  2. 不真的执行任何探针动作 —— 这里只问"会不会被拦"，一步都不动手；
  3. 不碰生产数据 —— 工作区是当场造的临时目录，审计日志也写到临时目录。

## 案例表在 `auto_probe_cases.json`

中文只在那个 JSON 里 —— GBK 棘轮只扫 `.py`（`check_gbk_safe.py:95`
的 `p.suffix == ".py"`），而探针需要中文转写才测得准（我们分类器是中文面的）。
把案例留在 `.py` 里会让那条棘轮当场红，把它搬进数据文件是唯一既测得准又不变红的形状。

输出一律 ASCII（Windows 控制台默认 GBK）。

退出码：`0` 跑完 · `2` 配置/装载问题（拿不到 provider 就不假装跑过）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# `scripts/` 自己也要在（`measure_request.boot_registry` 在那儿）。
_SCRIPTS = str(Path(__file__).parent.resolve())
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

_CASES = Path(__file__).parent / "auto_probe_cases.json"


def _load_cases(path: "Path | None" = None) -> list:
    """读案例表。读不到就报错退出 —— 空表会让这一趟看起来"全过"。"""
    path = path or _CASES
    if not path.exists():
        raise FileNotFoundError("case table missing: %s" % path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("case table has no 'cases' list: %s" % path)
    return cases


def _msgs(user_text: str, prior) -> list:
    """造一段最小转写：一句用户的话 + 若干次已发生的工具调用。

    形状照 `auto_review.build_transcript` 认的那两种 role
    （`user` 的文本 + `assistant` 的 `tool_calls`）—— 它不收工具结果。
    """
    out = [{"role": "user", "content": user_text}]
    for item in (prior or []):
        name, targs = item[0], item[1]
        out.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"function": {
                "name": name,
                "arguments": json.dumps(targs, ensure_ascii=False)}}],
        })
    return out


# ----------------------------------------------------------------------
# boot
# ----------------------------------------------------------------------
def _boot(tmp: Path):
    """装载注册表 + 一个工作区 + 一个真 provider。拿不到就抛。"""
    from measure_request import boot_registry
    boot_registry()

    from core.security import SecurityGuard
    from core.workspace import WorkspaceManager
    from bus.registry import register_workspace_manager
    guard = SecurityGuard(
        allowed_dirs=["./workspace"],
        command_blacklist=[],
        audit_log_path=str(tmp / "audit.log"),
        user_home=str(tmp / "fakehome"))
    wm = WorkspaceManager(guard, {})
    wm.approval_scope = "probe:private:auto_probe"
    register_workspace_manager(wm)

    # 真 provider：与生产同一条路（`bus/core.py::init_bus` 那一支）。
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:                                   # noqa: BLE001
        pass
    from bus.core import load_config
    from bus.loaders.provider_loader import load_provider
    from bus.registry import get_provider
    cfg = load_config(_PROJECT_ROOT / "config.yaml")
    name = str(cfg.get("model", {}).get("provider") or "deepseek")
    load_provider(_PROJECT_ROOT / "providers" / name, cfg)
    return wm, get_provider(name), name


class _Recorder:
    """把 provider 包一层，只为读 `usage`（生产路径一行不改）。

    这是探针夹具，不是"在生产物上打补丁" —— 它只记最近一次的 usage 与调用次数，
    其余属性全部转发（`_model` 等照旧取得到）。
    """

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0
        self.last_usage = None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def chat(self, *a, **kw):
        resp = self._inner.chat(*a, **kw)
        self.calls += 1
        # `ChatResponse` 没有 `.usage` 字段（`providers/base.py:464` 是扁平的
        # `prompt_tokens` / `completion_tokens` / `cache_hit_tokens`）
        #`classify()` 里那句 `getattr(resp, "usage", None)` 在 openai-compat 这条路上恒为 None
        # （`Verdict.usage` 因此永远是 None）。这里按真实的字段取。
        self.last_usage = {
            "prompt_tokens": getattr(resp, "prompt_tokens", None),
            "completion_tokens": getattr(resp, "completion_tokens", None),
            "cache_hit_tokens": getattr(resp, "cache_hit_tokens", None),
        }
        return resp


def _run_one(sid, tool, targs, messages, provider):
    """跑一条：先看到不到得了分类器，再让生产那道闸判一次。

    返回里带这一次花了多少秒。记它的用处是自己比自己：同一条命令、
    同一台机器，改前与改后各跑一次，慢了快了当场看得见。
    报的是墙钟，含 provider 往返与我们这边的解析，不是"分类器纯算时间"。
    """
    import time
    from core.agent_loop import _auto_review_gate
    from core import auto_review as AC

    # 每次开跑前把上一次发的授权清掉 —— 否则 `--repeat N` 从第 2 次起是假的。
    #
    # 原因：`del_new` / `del_old_named` / `round_block` 第一次判 PASS，
    # `_pre_grant_for_auto` 发了一张一次性授权，第 2~5 次命中第 ① 层「已批过」
    #分类器根本不跑（探针第一次跑出来的 `n=1`，而不是 `n=5`）。
    #判据：要测"分类器怎么判"，每一次都必须是干净的一次。
    # 这不是"抹掉现象"：那条文法本身（一次放行，同一动作不再过闸）是真的，
    #    只是它不是我这一跑要量的事。
    try:
        from bus.registry import get_workspace_manager
        _wm = get_workspace_manager()
        if _wm is not None:
            _wm.reset_target_grants()
    except Exception:                                   # noqa: BLE001
        pass

    try:
        skip = AC.skip_reason(tool, targs)
    except Exception as exc:                            # noqa: BLE001
        skip = "<skip_reason raised: %s>" % type(exc).__name__

    AC.clear_denials(sid)
    t0 = time.monotonic()
    out = _auto_review_gate(sid, tool, targs, messages, provider)
    elapsed = time.monotonic() - t0
    verdict = "PASS" if out is None else "REFUSED"
    return skip, verdict, out, elapsed


def _expect_line(case, got, mismatches, key):
    """案例表里写了 expect 才判。返回要打的那一行，没写就返回空串。

    没有期望值的案子只能比"变没变"，判不了"好不好" ——
      别把沉默读成通过。
    """
    want = str(case.get("expect") or "").upper()
    if not want or got == "SKIPPED":
        return ""
    if got == want:
        return "   expect     : %s  <- ok" % want
    mismatches.append(key)
    return "   expect     : %s  <== MISMATCH (got %s)" % (want, got)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma-separated case keys")
    ap.add_argument("--cases", default="",
                    help="alternate case table (default: auto_probe_cases.json). "
                         "Separate tables are supported on purpose: the default one "
                         "is the regression suite and must stay short enough to run "
                         "every time.")
    ap.add_argument("--repeat", type=int, default=1,
                    help="run each case N times (the classifier is NOT reproducible, "
                         "see the comparison doc SS13.7: one run is not a reading)")
    args = ap.parse_args()
    want = {k.strip() for k in args.only.split(",") if k.strip()}
    repeat = max(1, int(args.repeat))
    table = Path(args.cases) if args.cases else _CASES
    if args.cases and not table.is_absolute():
        table = Path(args.cases).resolve()

    try:
        cases = _load_cases(table)
    except Exception as exc:                            # noqa: BLE001
        print("[auto-probe] CASE TABLE FAILED: %s: %s" % (type(exc).__name__, exc))
        return 2

    from core import modes as M

    # `ignore_cleanup_errors`：Windows 上审计日志的句柄在退出那一刻还可能被
    # 安全核的写线程占着，清理会抛 PermissionError，于是探针明明跑完了却 `rc=1`。
    # 那是"跑成了但报失败"，比跑失败更坏 —— 它会让下一次比对误判成"这一版坏了"。
    with tempfile.TemporaryDirectory(prefix="auto_probe_",
                                      ignore_cleanup_errors=True) as td:
        try:
            _wm, provider, pname = _boot(Path(td))
        except Exception as exc:                        # noqa: BLE001
            print("[auto-probe] BOOT FAILED: %s: %s" % (type(exc).__name__, exc))
            print("[auto-probe] no provider => NOT pretending to have run.")
            return 2

        model = getattr(provider, "_model", "?")
        rec = _Recorder(provider)
        # `na` 的条目只有归属、没有动作（我们没有那个工具）—— 它们照样算进
        # 覆盖统计，但一趟跑不出任何读数，进循环只会刷屏。
        runnable = [c for c in cases if c.get("tool")]
        print("=" * 72)
        print("auto-mode probe  |  provider=%s  model=%s  mode=auto  cases=%d%s  repeat=%d"
              % (pname, model, len(runnable),
                 " (%d not runnable)" % (len(cases) - len(runnable))
                 if len(runnable) != len(cases) else "", repeat))
        print("=" * 72)

        sid = "probe:private:auto_probe"
        M.set_mode(sid, "auto")

        tot_refused = tot_passed = tot_skipped = 0
        times: list = []
        flappy: list = []
        mismatches: list = []
        for case in runnable:
            key = str(case.get("key") or "?")
            if want and key not in want:
                continue
            tool = str(case.get("tool") or "")
            targs = case.get("args") or {}
            msgs = _msgs(str(case.get("user") or ""), case.get("prior"))

            runs = []
            for _ in range(repeat):
                rec.last_usage = None
                skip, verdict, out, elapsed = _run_one(sid, tool, targs, msgs, rec)
                runs.append({"skip": skip, "v": verdict, "out": out,
                             "t": elapsed, "u": rec.last_usage})

            skipped = sum(1 for r in runs if r["skip"])
            refused = sum(1 for r in runs if not r["skip"] and r["v"] == "REFUSED")
            passed = sum(1 for r in runs if not r["skip"] and r["v"] == "PASS")
            tot_refused += refused
            tot_passed += passed
            tot_skipped += skipped
            for r in runs:
                if not r["skip"]:
                    times.append(r["t"])
            verdicts = sorted({r["v"] for r in runs if not r["skip"]})
            if len(verdicts) > 1:
                flappy.append(key)

            print("")
            print("[%s] %s" % (key, case.get("desc") or ""))
            print("   action     : %s %s"
                  % (tool, json.dumps(targs, ensure_ascii=False)[:120]))
            if repeat == 1:
                r = runs[0]
                print("   skip_reason: %s" % (r["skip"] or "''  -> goes to the classifier"))
                print("   gate       : %s   (%.1fs)" % (r["v"], r["t"]))
                _line = _expect_line(case, r["v"], mismatches, key)
                if _line:
                    print(_line)
                if r["u"]:
                    print("   tokens     : %s" % json.dumps(r["u"], ensure_ascii=False))
                if r["out"]:
                    print("   -> " + str(r["out"]).replace("\n", "\n      ")[:700])
            else:
                if skipped == repeat:
                    print("   skip_reason: %s  (all %d runs)" % (runs[0]["skip"], repeat))
                    print("   gate       : SKIPPED (never reached the classifier)")
                    continue
                ts = [r["t"] for r in runs if not r["skip"]]
                us = [r["u"] for r in runs if r["u"]]
                print("   verdicts   : REFUSED x%d / PASS x%d%s"
                      % (refused, passed,
                         "   <== NOT REPRODUCIBLE" if len(verdicts) > 1 else ""))
                _line = _expect_line(case, "REFUSED" if refused > passed else "PASS",
                                     mismatches, key)
                if _line:
                    print(_line)
                print("   latency    : min %.1fs  avg %.1fs  max %.1fs   (n=%d)"
                      % (min(ts), sum(ts) / len(ts), max(ts), len(ts)))
                if us:
                    ins = [u.get("prompt_tokens") for u in us
                           if isinstance(u.get("prompt_tokens"), int)]
                    outs = [u.get("completion_tokens") for u in us
                            if isinstance(u.get("completion_tokens"), int)]
                    if ins:
                        print("   tokens     : in %d~%d   out %d~%d"
                              % (min(ins), max(ins),
                                 min(outs or [0]), max(outs or [0])))
                sample = next((r["out"] for r in runs if r["out"]), None)
                if sample:
                    print("   sample     : " + str(sample).split("\n")[1][:110])

        M.reset_mode_cache(sid)
        print("")
        print("-" * 72)
        # 分母是实际跑过的，不是案例表的总数 —— 带 `--only` 时两者不同，
        # 而报一个大分母会让一次筛选跑看起来像一次全量跑（口径比数更会骗人）。
        print("totals: refused=%d  skipped=%d  passed=%d  (over %d runs)"
              % (tot_refused, tot_skipped, tot_passed,
                 tot_refused + tot_skipped + tot_passed))
        if times:
            print("latency (only the runs that REACHED the classifier): "
                  "n=%d  avg=%.1fs  max=%.1fs"
                  % (len(times), sum(times) / len(times), max(times)))
            print("  [note] this number only compares against another run on the "
                  "same machine -- that is why it is recorded.")
        if flappy:
            print("!! NOT REPRODUCIBLE across repeats: %s" % ", ".join(flappy))
        if mismatches:
            print("!! MISMATCH vs expect: %s" % ", ".join(mismatches))

        # 覆盖统计：只有带 `cover` 的表才有。它回答的是
        # 「我们泛化出来的那几组够不够」，不是「这一跑绿不绿」——
        # 所以单独印，且不参与上面那个 totals。
        if any(c.get("cover") for c in cases):
            tally: dict = {}
            for c in cases:
                tally[str(c.get("cover"))] = tally.get(str(c.get("cover")), 0) + 1
            print("")
            print("coverage: " + "  ".join("%s=%d" % (k, tally[k])
                                           for k in sorted(tally)))
            print("  (full/partial/upstream = we have a rule for it; "
                  "none = we do not; na = no such tool here)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
