"""
Crannia v2 — Entry point.

Responsibilities (and ONLY these):
  1. Load configuration.
  2. Initialize the module bus (providers, skills, channels).
  3. Initialize MemoryManager → SessionPool.
  4. Build PipelineScheduler (PreProcess → AgentLoop → PostProcess).
  5. Get the active channel and run it.
"""

import warnings

# 没关的 sqlite3 连接在 GC 时可能报 ResourceWarning —— 那是噪声
# （进程退出时系统会收），压掉。
warnings.filterwarnings("ignore", category=ResourceWarning, message=".*unclosed.*sqlite3.*")

import asyncio
import signal
import sys
from pathlib import Path

# Ensure the project root is on sys.path so absolute imports work
# regardless of where the process is launched from.
_PROJECT_ROOT = Path(__file__).parent.resolve()

# 安装版：把数据根迁到用户目录并 chdir（开发树是空操作）。
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from core import paths as _paths

_paths.prepare()

from dotenv import load_dotenv

load_dotenv(override=True)

from bus.core import init_bus, load_config
from bus.registry import get_provider, get_channel
from core.memory.manager import MemoryManager
from core.pipeline.scheduler import PipelineScheduler
from core.prompt_assembler import set_project_root
from core.session_pool import SessionPool


async def main() -> None:
    # 1. Load configuration
    config = load_config(_paths.config_path())

    # 1.5 Initialize prompt assembler with project root
    set_project_root(_PROJECT_ROOT)

    # 2. Get the active provider
    provider_name = config.get("model", {}).get("provider", "deepseek")

    # 3. Initialize memory manager (shared across all sessions)
    memory_manager = None
    if config.get("memory", {}).get("enabled", True):
        memory_manager = MemoryManager(config, provider=None)
        # 登记给注册表（`history_search` 靠它拿 short_term）。
        # 与 `web/server.py` 成对 —— 少一处 = 那条通道上"查历史不可用"。
        from bus.registry import register_memory_manager
        register_memory_manager(memory_manager)
        print(
            f"[memory] MemoryManager initialized "
            f"(path={config.get('memory', {}).get('storage_path', 'data/memory')})"
        )

    session_pool = SessionPool(memory_manager) if memory_manager else None

    # 4. Initialize bus — loads providers, skills, channels
    #    Channels get session_pool and scheduler injected later
    init_bus(config, session_pool=session_pool)

    # Now we can get the provider (registered by init_bus)
    provider = get_provider(provider_name)

    # Patch the provider into the memory manager (needed for compression)
    if memory_manager is not None:
        memory_manager._provider = provider
        # Threshold self-check.  Printed once, right
        # after the provider — and therefore the window — is known, so an
        # unreachable threshold is visible at startup instead of showing up
        # in an audit months later.
        memory_manager.report_thresholds()

    # 5. Build PipelineScheduler
    scheduler = PipelineScheduler(
        session_pool=session_pool,
        provider=provider,
        config=config,
    )
    await scheduler.initialize()
    print("[pipeline] Scheduler initialized "
          f"({len(scheduler._stages)} stages)")

    # 周期任务的调度器。失败不许拖垮启动 ——
    #   它是一项附加能力，起不来只该是"定时任务不跑"，不是"框架起不来"。
    try:
        from core.cron import runner as cron_runner
        cr = cron_runner.configure(scheduler)
        print("[cron] 周期任务调度器已启动（%d 条任务）" % cr.job_count())
    except Exception as exc:  # noqa: BLE001
        print("[cron] 起调度器失败（不影响其他功能）：%s: %s"
              % (type(exc).__name__, exc))

    # 后台任务的完成通知。失败不拖垮启动。
    try:
        from core import tasks_notify as _tasks_notify
        _tasks_notify.install(scheduler)
        print("[jobs] 后台任务完成通知已接通")
    except Exception as exc:  # noqa: BLE001
        print("[jobs] 后台通知没接上（不影响启动）：%s: %s"
              % (type(exc).__name__, exc))

    # 任务事件 —— UI 两格计数的信号。失败不拖垮启动。
    try:
        from core import task_events as _task_events
        _task_events.install()
    except Exception as exc:  # noqa: BLE001
        print("[jobs] 任务事件没接上（不影响启动）：%s: %s"
              % (type(exc).__name__, exc))

    # 6. Get the active channel, inject scheduler, and run
    channel_name = config.get("channel", {}).get("default", "cli_channel")
    channel = get_channel(channel_name)
    channel._scheduler = scheduler
    channel._pool = session_pool

    print(f"[main] Starting channel: {channel_name}")
    await channel.run()


if __name__ == "__main__":
    # v1.0.0: Graceful shutdown on Ctrl+C / SIGTERM.
    _shutdown_flag = False

    def _on_shutdown(signum, frame):
        global _shutdown_flag
        if _shutdown_flag:
            print("\n[main] Force quit.")
            sys.exit(1)
        _shutdown_flag = True
        print("\n[main] Shutting down gracefully (press Ctrl+C again to force)...")

    signal.signal(signal.SIGINT, _on_shutdown)
    signal.signal(signal.SIGTERM, _on_shutdown)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[main] Interrupted. Exiting.")
    finally:
        print("[main] Clean exit — SQLite WAL checkpoint preserved.")
