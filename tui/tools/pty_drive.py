"""用真伪终端驱动 TUI，验交互路径。

为什么需要它：`--once` 绕过了界面本身，而布局、输入框、状态行这些
"只能靠眼睛看"的部分，没有伪终端就只能靠人肉跑一遍再口头描述。

这里做四件事：开一个真 PTY（80×30）→ 按真人节奏敲键 → 用终端模拟器
把字节流还原成屏幕 → 把每个阶段的画面写进转录。

**为什么必须模拟而不是直接看字节**：全屏应用每一帧都是"光标移到某处、
覆盖这几行"，把字节去掉转义之后拼起来是一团重叠的文字，看不出界面长什么样。
`pyte` 按终端语义把这些指令执行一遍，得到的才是屏幕上真正显示的内容。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from pathlib import Path

import pyte

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))

import sandbox_run as SB  # noqa: E402

COLS, ROWS = 80, 30


class Reader:
    """后台线程读 PTY，主线程按时间片取。

    `PtyProcess.read()` 是阻塞的（内部 `recv`），所以"读一段时间就返回"
    没法直接实现 —— 直接调用会让超时判断永远轮不到。这里把读放到线程里，
    主线程只负责掐时间，两件事互不干扰。

    读到的东西顺手喂给终端模拟器：模拟器是有状态的，必须按到达顺序喂，
    事后拿一个拼接好的大字符串再喂会丢状态（光标位置、清屏）。
    """

    def __init__(self, proc, screen: pyte.Screen) -> None:
        self.proc = proc
        self.screen = screen
        self.stream = pyte.Stream(screen)
        self.q: queue.Queue[str | None] = queue.Queue()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self) -> None:
        while True:
            try:
                chunk = self.proc.read()
            except (EOFError, OSError, ValueError):
                self.q.put(None)
                return
            if not chunk:
                continue
            self.q.put(chunk)

    def pump(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            try:
                item = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self.stream.feed(item)
            except Exception:                                # noqa: BLE001
                pass

    def screen_text(self) -> str:
        """当前屏幕内容，去掉右侧空白和末尾空行。"""
        lines = [line.rstrip() for line in self.screen.display]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)


#: 发什么。默认那句不需要工具，跑一遍只验"连得上"；
#: 想验工具卡片就传一句会触发工具的，例如
#:   pty_drive.py "看一下工作区里有哪些文件"
ASK = sys.argv[1] if len(sys.argv) > 1 else "你好，用一句话介绍你自己"


def type_text(proc, text: str) -> None:
    """分小段写入，段与段之间留几十毫秒。

    ConPTY 的输入缓冲在对面正忙着重绘时，一次性灌一长串会丢尾巴
    （句尾少字、"/exit" 变成 "/exi"）。逐段 + 小睡模拟真人打字，绕开它。
    """
    for i in range(0, len(text), 4):
        proc.write(text[i:i + 4])
        time.sleep(0.04)
    proc.write("\r")


def main() -> int:
    from winpty import PtyProcess

    SB.backup()
    env = dict(os.environ)
    # 不写 setdefault(sys.executable)：驱动用的是本包 venv 的解释器，
    # 它没有 flask，设进去等于把"后端该用哪个 python"这件事替用户猜错。
    from crannia_tui.paths import python_exe
    env["CRANNIA_PYTHON"] = str(python_exe())
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    # 这个脚本跑在 git bash 里，bash 自带 LANG=en_US —— 后端按"跟随系统"
    # 解析时会把它当成英文系统，活动描述全变英文，界面成了半中半英。
    # 用户的 cmd 没有这个变量，解析出来是中文。这里钉成中文，
    # 让验证环境与用户实际看到的口径一致（变量本身就是后端留给测试的）。
    env.setdefault("CRANNIA_INTERFACE_LANGUAGE", "zh")

    cmd = [sys.executable, "-m", "crannia_tui"]
    print(f"[pty] 启动：{' '.join(cmd)}", flush=True)

    screen = pyte.Screen(COLS, ROWS)
    proc = None
    shots: list[tuple[str, str]] = []
    # spawn 必须在 try 里：它自己就会抛（找不到解释器、后端不支持），
    # 而快照已经建好了 —— 在外面抛，restore 永远轮不到，盘上就留一个孤儿快照。
    try:
        proc = PtyProcess.spawn(cmd, dimensions=(ROWS, COLS), env=env, cwd=str(HERE))
        rd = Reader(proc, screen)

        rd.pump(35)
        shots.append(("启动完成后", rd.screen_text()))

        if proc.isalive():
            type_text(proc, ASK)
            rd.pump(3)
            shots.append(("发送后 3s（横幅与运行中的卡）", rd.screen_text()))
            rd.pump(22)
            shots.append(("发一条消息后", rd.screen_text()))

        if proc.isalive():
            # 命令表：输入 "/" 再打两个字母，看候选、选中标记与窗口。
            proc.write("/"); time.sleep(0.2)
            type_text(proc, "co")
            rd.pump(1)
            shots.append(("命令表", rd.screen_text()))
            proc.write("\x1b")          # Esc 收起，字留在输入框
            rd.pump(0.5)

        if proc.isalive():
            type_text(proc, "/exit")
            # 退出时要收后端子进程，而 `backend.stop()` 最多等它 8 秒
            # （跑完一轮之后的进程不会立刻响应 terminate）。等不够就会
            # 在它还在收尾时被我们 terminate，exitstatus 变成无意义的数。
            rd.pump(20)
            shots.append(("退出后", rd.screen_text()))
    finally:
        if proc is not None and proc.isalive():
            proc.terminate()
        SB.restore()

    out = HERE / "pty_transcript.txt"
    with out.open("w", encoding="utf-8") as fh:
        for title, text in shots:
            fh.write(f"\n{'=' * 78}\n== {title}\n{'=' * 78}\n")
            fh.write(text + "\n")
    print(f"[pty] 转录已写 {out}", flush=True)
    if proc is not None:
        print(f"[pty] exitstatus={proc.exitstatus}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
