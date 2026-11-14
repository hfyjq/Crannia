"""并发探针 —— 用户轮 × 后台活到底重叠没有。

为什么要机械化
==============
这个窗口只有 1.6 ~ 5.7 秒，而且它从 `task.end` 前 5ms 开始（`maybe_extract`
是 `record_turn` 在轮末调的，调度器紧接着发 `task.end`）。人手抢不过：
WebUI 在途时禁用输入（`chat.html:1210`），IM 还有网络延迟 —— 两次尝试，
一次晚了 1.4 秒、一次早了。

脚本连着发两条：第一条跑完的瞬间发第二条。HTTP 往返是毫秒级，
重叠是必然的，不再是概率。

它做什么 / 不做什么
===================
它会真的往生产状态里写东西（`short_term.db` / 日报 / 可能写记忆）——
它就是在运行中的实例上跑几轮对话，不是只读探针。所以要 `--confirm`。

**它不发 `bg.*`，它只读**日志算区间 —— 观测点由日志负责。

用法::

    cd <仓库根>
    # 只看能不能连上、当前日志里有没有可用的区间（不发任何消息）
    .venv/Scripts/python.exe scripts/probe_bg_overlap.py
    # 真的发 3 条（每条之间不等待 —— 后一条落进前一条的 bg 窗口）
    .venv/Scripts/python.exe scripts/probe_bg_overlap.py --confirm --turns 3

token 从 `/ping` 取（它不需要鉴权且把 launch token 明文吐出来）——
顺带记一笔：这是个本地可达即泄漏的形状（只绑 127.0.0.1，所以是"低危但该知道"）。
"""

import argparse
import datetime
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
BASE = "http://127.0.0.1:5000"

# ASCII-only 输出（Windows 控制台 GBK）


def _get(path: str, token: str = "", timeout: float = 10.0):
    req = urllib.request.Request(BASE + path)
    if token:
        req.add_header("X-Crannia-Token", token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _post_chat(text: str, token: str, timeout: float = 300.0) -> dict:
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json",
                                          "X-Crannia-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # 把服务端那句有用的中文读出来 —— 只报 `HTTP Error 503` 等于吞掉
        #   了唯一的诊断信息（看不出"框架没启动"）。
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {exc.code} {exc.reason} -> {detail}") from exc


def _read_events():
    day = datetime.date.today().strftime("%Y-%m-%d")
    p = _PROJECT_ROOT / "data" / "events" / f"{day}.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").split("\n"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001
                pass
    return out


def _fmt(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


def _spans(evs, since: float):
    tasks, bgs = [], []
    ot, ob = {}, {}
    for e in evs:
        if e["ts"] < since:
            continue
        t = e["type"]
        if t == "task.start":
            ot[e["session"]] = e["ts"]
        elif t == "task.end" and e["session"] in ot:
            tasks.append((ot.pop(e["session"]), e["ts"], e["session"]))
        elif t == "bg.start":
            ob["b"] = e["ts"]
        elif t == "bg.end" and "b" in ob:
            bgs.append((ob.pop("b"), e["ts"]))
    return tasks, bgs


def report(evs, since: float, label: str) -> int:
    tasks, bgs = _spans(evs, since)
    print(f"  {label}：用户轮 {len(tasks)} 条 · 后台活 {len(bgs)} 条")
    for a, b, s in tasks:
        print(f"    task  {_fmt(a)} -> {_fmt(b)}  ({b - a:5.2f}s)  {s}")
    for a, b in bgs:
        print(f"    bg    {_fmt(a)} -> {_fmt(b)}  ({b - a:5.2f}s)")
    hits = []
    for a, b, _s in tasks:
        for c, d in bgs:
            ov = min(b, d) - max(a, c)
            if ov > 0.01:
                hits.append((ov, a, b, c, d))
    print()
    if hits:
        for ov, a, b, c, d in hits:
            print(f"  ** OVERLAP {ov:.2f}s **  task({_fmt(a)}..{_fmt(b)})  x  bg({_fmt(c)}..{_fmt(d)})")
        print()
        print("  => 并发确实发生了。接着要看的：")
        print("     1) 那一轮的 <memory_index> 有没有漏/重")
        print("     2) facts/ 在该轮进行中有没有变")
        print("     3) _turn_slots(上限 5) 有没有人排队")
        return 0
    print("  => 没有重叠。")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="用户轮 x 后台活 重叠探针")
    ap.add_argument("--confirm", action="store_true",
                    help="真的发消息（会写生产状态）；不给就只读")
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--prefix", default="\u5e76\u53d1\u63a2\u9488")  # 并发探针
    args = ap.parse_args()

    try:
        pong = _get("/ping", timeout=5)
    except Exception as exc:  # noqa: BLE001
        print(f"!! 连不上 {BASE}（{type(exc).__name__}: {exc}）")
        print("   WebUI 在跑吗？端口是 5000 吗？")
        return 2
    token = pong.split("token=")[-1].strip()
    print(f"ping ok，token 拿到（{len(token)} 字符）")

    if not args.confirm:
        print("（只读模式 —— 加 --confirm 才真发消息）")
        return report(_read_events(), 0.0, "今天全部")

    t0 = time.time()
    print(f"开始发 {args.turns} 条：**不等回复间隔** —— 后一条落进前一条的 bg 窗口")
    for i in range(1, args.turns + 1):
        text = f"{args.prefix} #{i}（这是自动探针发的一条短消息，不用回答任何事）"
        t = time.time()
        try:
            r = _post_chat(text, token)
            reply = (r.get("reply") or "")[:60].replace("\n", " ")
        except Exception as exc:  # noqa: BLE001
            print(f"  #{i} 失败：{type(exc).__name__}: {exc}")
            continue
        print(f"  #{i} 回来了 ({time.time() - t:.2f}s)：{reply}")

    print()
    return report(_read_events(), t0 - 1.0, "本次探针")


if __name__ == "__main__":
    sys.exit(main())
