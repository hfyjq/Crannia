# crannia-tui

English | [简体中文](README.zh.md)

crannia's terminal interface, **full screen**: a scrollable conversation area in the
middle, with the input box and status bar fixed at the bottom; the startup header is
shown only when the TUI is launched, and scrolls with the content.

The TUI and the desktop app share the same backend and the same data: when the desktop
app is already running, the TUI attaches to it; when no running backend is found, it
starts one.

## Installation

### Windows

- **Bundled with the desktop installer**: the TUI is installed automatically. Open a
  **new** Command Prompt, go to any directory and type `crannia` to start it.
- **Standalone package**: extract it and run `install.cmd`. The TUI is installed to
  `%LOCALAPPDATA%\Programs\CranniaTUI`, and `crannia` is added to
  `%LOCALAPPDATA%\Microsoft\WindowsApps` (already on PATH; the registry is not
  modified). Uninstall with `uninstall.cmd`; your data is not removed automatically.

When both are installed, `crannia` points to the one installed last; uninstalling only
removes that package's own entry.

Windows Terminal is required. The old cmd console does not interpret ANSI; when the
launcher detects it (`WT_SESSION`), it reopens the TUI in a new window. `--once` does
not reopen a window.

### Linux

- **Standalone package**: extract `crannia-tui-<version>-linux-x64.tar.gz` and run
  `./install.sh` in the extracted directory. It installs to
  `${XDG_DATA_HOME:-~/.local/share}/crannia-tui` and links `crannia` into
  `~/.local/bin` (change the location with `CRANNIA_HOME` / `CRANNIA_BIN_DIR`).
  Uninstall with `uninstall.sh`; your data lives in
  `${XDG_DATA_HOME:-~/.local/share}/crannia` and is not removed.
- **AppImage**: the bundle also contains the TUI (`bin/crannia.sh`), but an AppImage
  installs nothing and does not put the command on PATH — run `--appimage-extract`
  first, then `squashfs-root/bin/crannia.sh`.

Pasting images needs `wl-clipboard` on Wayland or `xclip` on X11. A Chinese UI needs a
terminal font with CJK glyphs (for example `fonts-noto-cjk`).

### Running from source

Add `tui/` to PATH:

```cmd
set PATH=%PATH%;<repo>\tui
crannia
```

```bash
export PATH="$PATH:<repo>/tui"    # Linux
crannia
```

When the source lives at `tui/` in the main repository, no extra configuration is
needed: the launcher finds the repository's `.venv`. For a custom layout, create
`tui/.env` with the backend interpreter:

```
CRANNIA_PYTHON=D:\path\to\crannia\.venv\Scripts\python.exe
```

## Usage

```cmd
crannia                  open the interface
crannia --once "hello"   send one message only (output can be redirected)
```

`Ctrl+C` quits.

## Interface

```
                             ▄▄▄▄
                            █
                            █
                             ▀▀▀▀
                           Crannia v1.2.0
                       deepseek-flash | xhigh
  #- < . . . > / * { _ } - | #> = \ @ ( ) ) - < *** / [ _ ^ _ \ - < \ - > ; | · · · =
                          >_ D:\work\crannia    ← header: printed once at startup, scrolls with content

 【User】把第 7 页的价格带改成柱状图      ← conversation area, scrolls
 ● 改写表格  价格表.docx                  ← tool card: green=done, red=failed, yellow=running
   ⎿  2,140 字符 · 1.2s                     failures add a line; long stacks fold to "+N lines"
 ● 读取模板  template.pptx
   好的，先看一下现有版式。

 ⋯ 思考中   3s                             ← status line: what it is doing, for how long
 ╭────────────────────────────────────────╮
 │  我可以怎么帮助您？                    │  ← input box
 ╰────────────────────────────────────────╯
 mode: 手动 · effort: high        2个后台  上下文：3%/85%
```

Bottom bar: the current mode and effort, background-task and sub-agent counts (shown
only when each exists), and context usage / compaction threshold (turns yellow near
the threshold).

## Keys

| Key | Action |
|---|---|
| Enter | Send |
| Shift+Enter / Ctrl+J | New line |
| `/` | Command list: ↑↓ to choose, Enter to insert, Esc to close |
| Enter while running | Queue the message |
| Ctrl+V / drag a file into the terminal | Paste text or a file path |
| Alt+V | Paste an image from the clipboard: leaves a `【图片n】` placeholder at the cursor; deleting it withdraws that image |
| Ctrl+B | Move the foreground task to the background (only when the card says it can) |
| ↓ while the bottom bar has tasks | Move focus to the bottom bar: ←→ to switch cells, Enter for details, ↑ / Esc to return |
| Esc | Request an interrupt for the current turn (no effect while an approval is pending) |
| Ctrl+C | Quit |

If a key does nothing, try `crannia --keys` to see whether the key reaches the app.

## Commands

### Command line

These commands do not open the interface; they only perform the query.

```
crannia setup                       configure a model API (first-run guide)
crannia doctor                      self-check
crannia config path                 print the settings file path
crannia profile                     list model configs (<name|id> sets the default)
crannia update                      show the current version and usage
crannia channel list                list channel entries
crannia channel add <platform> [name]   add an entry (credentials are entered field by field; the key is not echoed)
crannia channel config <name|id>    edit an entry (Enter keeps a field unchanged)
crannia channel remove <name|id>    remove an entry (switch the default first when removing the default one)
crannia channel default <name|id>   make an entry the default for its platform
crannia channel start|stop <platform>   start or stop a channel (backend must be online)
crannia channel login wechat        WeChat QR login (backend must be online)
```

### Inside the interface

```
/model                     current model + first 8 configs (with numbers)
/model <number|name|id>    switch to that entry (takes effect next turn)
/model 默认                follow the default again
/model more                full config list
/model set <number|name|id>
                           edit panel: entry name / model name / API key
                           (empty Enter skips a field, Esc exits)
```

## Settings

`<data root>/data/settings.json`, shared with the desktop app; `crannia config path`
prints the location. On first start the TUI seeds it with factory values (only when
the file is missing); a corrupt file is backed up to `settings.json.bad-<time>` and
rebuilt from defaults.

| Key | Type | Purpose |
|---|---|---|
| `effort_level` | integer | effort tier 1–6 |
| `default_mode` | string | default mode |
| `approval_timeout_seconds` | integer | approval timeout (0 = off) |
| `auto_approve` · `cross_zone_enabled` · `relax_command_tier` · `no_ask_readonly` · `full_access_unlock` | boolean | approval and security switches |
| `interface_language` · `response_language` | string or empty | UI / answer language |
| `trace_full` | boolean | full trace |
| `workspace` | string | default workspace |
| `background_services` | boolean | whether channels and scheduled tasks start with the backend |

## Tool cards and approvals

While a tool is running, its card shows the in-progress action and arguments.

| Outcome | Dot |
|---|---|
| Completed | green |
| Waiting for approval | yellow |
| Failed / not executed / not adopted | white |
| Denied / blocked by auto-review | red |

When an approval is requested, the input box becomes the approval panel: use the
number keys or ↑↓ to choose, Enter to confirm; choose "I have something else to say"
to explain to the model instead.

## Images

`Alt+V` pastes an image from the clipboard (`Ctrl+V` is taken by the terminal). The
image is left as a `【图片n】` placeholder at the cursor and becomes part of the
message: deleting the placeholder withdraws that image. Commands (`/…`) carry no
images; a message with images is queued directly.

## Links in replies

Clicking a file link in a reply locates it on the backend: the relative path is
resolved against the workspace root and the file is **selected** in the file manager
(a directory opens as a folder); it is only located, never executed.

## Disconnects and reconnects

If the step stream drops, a red bar appears and the TUI reconnects with backoff,
replaying the events it can; the bar disappears once the connection is restored.

## Development

- Directory layout, API contract, rendering conventions and the UI probe live in the
  `tui/` sources and docstrings.
- Tests: `.venv/Scripts/python run_tests.py`; from the repository root you can also run
  `scripts/run_tests.py`.
- Contribution workflow: see [`CONTRIBUTING.md`](../CONTRIBUTING.md).
