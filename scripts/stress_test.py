#!/usr/bin/env python3
"""
Crannia 压力测试 & 稳定性验证脚本.

用法:
    python scripts/stress_test.py <test> [--duration 3600]

测试场景:
    idle       — 空闲待机，周期性检查内存/响应
    chat       — 持续发送消息，测吞吐和 token 累加
    sleep      — 模拟系统休眠/唤醒（Windows: powercfg /sleepstudy）
    longrun    — 综合长跑（推荐 24h+），每 5 分钟记录健康指标

输出:
    data/stress_results/  — CSV 格式的指标日志
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PORT = 5000
TOKEN = ""
OUTPUT_DIR = PROJECT_ROOT / "data" / "stress_results"

# Test messages for chat scenario
CHAT_MESSAGES = [
    "你好，请介绍一下你自己",
    "帮我总结一下最近的工作进度",
    "现在几点了",
    "列出工作区的所有文件",
    "搜索包含'测试'的文档",
    "用一句话概括AI的发展历史",
]


def _get_token() -> str:
    """Read the launch token from a running instance (from .token cache or ping)."""
    # Try reading token from the running instance's ping endpoint
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/ping")
        with urllib.request.urlopen(req, timeout=3) as resp:
            text = resp.read().decode()
            # Response is "pong — token=<token>"
            if "token=" in text:
                return text.split("token=")[-1].strip()
    except Exception:
        pass
    # Fallback: try common env/config
    return os.environ.get("CRANNIA_TOKEN", "")


def _api(path: str, data: dict | None = None) -> dict:
    """Call a Crannia Web UI API endpoint."""
    url = f"http://127.0.0.1:{PORT}{path}?token={TOKEN}"
    headers = {"Content-Type": "application/json", "X-Crannia-Token": TOKEN}
    if data:
        req = urllib.request.Request(
            url, data=json.dumps(data).encode(), headers=headers, method="POST",
        )
    else:
        req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _record(output_csv: str, metrics: dict) -> None:
    """Append a row of metrics to the CSV log."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = OUTPUT_DIR / output_csv
    is_new = not filepath.exists()
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=metrics.keys())
        if is_new:
            writer.writeheader()
        writer.writerow(metrics)


# Test: idle — periodic health check

def test_idle(duration: int) -> None:
    """Monitor health metrics every 30 seconds while idle."""
    print(f"[idle] Monitoring health every 30s for {duration}s...")
    end = time.time() + duration
    while time.time() < end:
        try:
            h = _api("/api/health")
            mem = h.get("memory_mb", 0)
            tokens = h.get("tokens_total", 0)
            print(f"  [{datetime.now():%H:%M:%S}] "
                  f"mem={mem:.1f}MB tokens={tokens} "
                  f"busy={h.get('busy', False)} running={h.get('framework_running', False)}")
            _record("idle_metrics.csv", {
                "timestamp": datetime.now().isoformat(),
                "memory_mb": mem,
                "tokens_total": tokens,
                "api_calls": h.get("api_calls", 0),
                "framework_running": h.get("framework_running", False),
                "busy": h.get("busy", False),
            })
        except Exception as e:
            print(f"  [{datetime.now():%H:%M:%S}] ERROR: {e}")
        time.sleep(30)


# Test: chat — send messages continuously

def test_chat(duration: int) -> None:
    """Send chat messages and track response time + token usage."""
    print(f"[chat] Sending messages for {duration}s...")
    # Ensure framework is started
    try:
        s = _api("/api/status")
        if not s.get("framework_running"):
            print("[chat] Starting framework...")
            _api("/api/start", {})
            time.sleep(3)
    except Exception as e:
        print(f"[chat] Failed to start framework: {e}")
        return

    end = time.time() + duration
    msg_idx = 0
    while time.time() < end:
        msg = CHAT_MESSAGES[msg_idx % len(CHAT_MESSAGES)]
        msg_idx += 1
        t0 = time.time()
        try:
            resp = _api("/api/chat", {"text": msg})
            elapsed = time.time() - t0
            reply_len = len(resp.get("reply", ""))
            print(f"  [{datetime.now():%H:%M:%S}] "
                  f"'{msg[:20]}...' → {reply_len}chars in {elapsed:.1f}s")
            _record("chat_metrics.csv", {
                "timestamp": datetime.now().isoformat(),
                "message": msg[:50],
                "reply_chars": reply_len,
                "response_seconds": round(elapsed, 2),
            })
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  [{datetime.now():%H:%M:%S}] ERROR after {elapsed:.1f}s: {e}")
            _record("chat_metrics.csv", {
                "timestamp": datetime.now().isoformat(),
                "message": msg[:50],
                "reply_chars": 0,
                "response_seconds": round(elapsed, 2),
                "error": str(e)[:100],
            })
        time.sleep(min(5, max(1, end - time.time())))


# Test: longrun — comprehensive long-duration monitoring

def test_longrun(duration: int) -> None:
    """Long-run test: record health + send periodic chat messages."""
    print(f"[longrun] Comprehensive test for {duration}s "
          f"(~{duration/3600:.1f}h)...")
    end = time.time() + duration
    chat_interval = 300  # send a chat message every 5 min
    next_chat = time.time() + 60  # first chat after 1 min warmup
    msg_idx = 0

    while time.time() < end:
        now = time.time()
        try:
            h = _api("/api/health")
            mem = h.get("memory_mb", 0)
            tokens = h.get("tokens_total", 0)
            remaining = end - now
            print(f"  [{datetime.now():%H:%M:%S}] "
                  f"mem={mem:.1f}MB tokens={tokens} "
                  f"busy={h.get('busy')} remaining={remaining/3600:.1f}h")
            _record("longrun_metrics.csv", {
                "timestamp": datetime.now().isoformat(),
                "memory_mb": mem,
                "tokens_total": tokens,
                "api_calls": h.get("api_calls", 0),
                "framework_running": h.get("framework_running", False),
                "busy": h.get("busy", False),
            })

            # Periodic chat test
            if now >= next_chat and h.get("framework_running") and not h.get("busy"):
                msg = CHAT_MESSAGES[msg_idx % len(CHAT_MESSAGES)]
                msg_idx += 1
                t0 = time.time()
                try:
                    resp = _api("/api/chat", {"text": msg})
                    elapsed = time.time() - t0
                    print(f"    → chat: '{msg[:20]}...' → "
                          f"{len(resp.get('reply',''))}chars in {elapsed:.1f}s")
                except Exception as e:
                    print(f"    → chat ERROR: {e}")
                next_chat = now + chat_interval
        except Exception as e:
            print(f"  [{datetime.now():%H:%M:%S}] ERROR: {e}")
        time.sleep(60)  # check every minute

    # Final summary
    print("\n[longrun] Complete. Results saved to data/stress_results/")


TESTS = {
    "idle": test_idle,
    "chat": test_chat,
    "longrun": test_longrun,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Crannia stress test")
    parser.add_argument(
        "test", choices=list(TESTS),
        help="Test scenario to run",
    )
    parser.add_argument(
        "--duration", type=int, default=600,
        help="Duration in seconds (default: 600 = 10 min)",
    )
    parser.add_argument(
        "--port", type=int, default=5000,
        help="Web UI port (default: 5000)",
    )
    args = parser.parse_args()

    global PORT, TOKEN
    PORT = args.port
    TOKEN = _get_token()
    if not TOKEN:
        print("ERROR: Could not find launch token. Is Crannia running?")
        print("  Start: python crannia_tray.py")
        sys.exit(1)

    print(f"Crannia Stress Test — {args.test} ({args.duration}s)")
    print(f"  Results: {OUTPUT_DIR}/")
    print()

    TESTS[args.test](args.duration)


if __name__ == "__main__":
    main()
