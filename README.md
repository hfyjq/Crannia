<div align="center">
  <img src="web/static/img/logo.svg" width="96" height="96" alt="Crannia">
  <h1>Crannia</h1>
  <p><strong>A general-purpose agent.</strong></p>
  <p>
    <a href="README.zh.md">简体中文</a> |
    English
  </p>
  <p>
    <img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-blue">
    <img alt="Python" src="https://img.shields.io/badge/python-3.12%2B-blue">
    <img alt="Platform" src="https://img.shields.io/badge/platform-Windows-lightgrey">
    <img alt="Status" src="https://img.shields.io/badge/status-beta-orange">
  </p>
</div>

Crannia is an open-source general-purpose agent. It helps you get things done, or
build what you have in mind.

> Crannia is in beta. The core loop, skills, channels and desktop shell are usable;
> interfaces, prompts and on-disk formats may still change between releases.

## Quick start

### Option A — installer

Download the latest `crannia-*-win-x64.exe` from Releases and run it. The installer
unpacks a normal application directory and creates shortcuts; the bundled Python
runtime lives under `resources/primary-runtime`, so no extra system setup is needed.

### Option B — from source

Requirements: Windows 10/11, Python **3.12+**, Node.js.

```powershell
git clone https://github.com/hfyjq/Crannia.git
cd Crannia
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# edit .env and set at least one provider key, e.g. DEEPSEEK_API_KEY
```

The GUI is started by Electron; run the commands below to bring it up:

```powershell
npm install
npm run dev
```

Models can be added from the endpoint settings page instead of editing
`config.yaml`.

> The framework never writes API keys into the repository. Keys belong in `.env`, or
> in the credential store used by the endpoint settings page.

## Interoperability

Crannia reads the skill / hook / plugin directories of Claude Code, Codex, WorkBuddy
and others, so an existing library can be used as-is. This behaviour is read-only;
see the table below for how far each one goes.

| Component | Where we read | Support |
|---|---|---|
| Skills | `skills/` · `.agents/skills/` · `~/.agents/skills/` · `~/.claude/skills/` · `~/.codex/skills/.system` | `SKILL.md` frontmatter (`name` / `description`) plus the body. Note that this does not include types with a `manifest.yaml`; those are covered by another method. |
| Subagents | `agents/` · `.agents/agents/` · `~/.agents/agents/` · `~/.claude/agents/` | `name` · `description` · `tools` · `maxTurns`; the body is the role description. It can only lower the current tier's budget, never raise it — use `maxTurns` to change it. |
| Hooks | `data/hooks.json` · `~/.claude/settings.json` · `<workspace>/.crannia/hooks.json` · `<workspace>/.claude/settings.json` | The events are treated as a subset — see below. |
| Plugins | `plugins/` · `.agents/plugins/` · `~/.agents/plugins/` · two plugin caches | `plugin.json` · `.mcp.json` · `skills/` · `agents/` · `hooks/hooks.json`. Entries under `commands/` can be read, but there is no corresponding loader. |
| MCP | `.mcp.json` · `manifest.yaml` | Tool definitions in JSON Schema 2020-12; see [`MCP_ADAPTATION.md`](MCP_ADAPTATION.md). |

### Hooks: what the subset covers

Nine events: `PreToolUse` · `PostToolUse` · `PostToolUseFailure` ·
`UserPromptSubmit` · `UserPromptExpansion` · `SessionStart` · `Stop` ·
`StopFailure` · `PermissionRequest`. `SubagentStart` / `SubagentStop` are recognised,
skipped, and reported with a note; any other event name is skipped silently.

A hook receives `hook_event_name` · `session_id` · `cwd` · `timestamp`, plus the
event's own fields (`tool_name` · `tool_input` · `tool_use_id` · `tool_response` ·
`stop_hook_active`). Exit code 2 = blocked, with stderr as the reason; any other
non-zero code is a non-blocking failure. Exiting 0 may print a JSON block:
`hookSpecificOutput.permissionDecision` (`allow` / `ask` / `deny`) ·
`permissionDecisionReason` · `updatedInput` · `additionalContext`, plus a top-level
`decision: "block"`.

`matcher` is a regular expression over the tool name; both spellings are allowed
(`run_command` also matches `Bash`). With `async: true` the hook runs in the
background — it will then not be allowed to block, ask, or return any result.
`${CLAUDE_PLUGIN_ROOT}` is substituted for hooks that come from a plugin package.

Note that anything outside this table is an incompatible type or is broken, and will
not take effect.

## Security boundaries

Crannia provides guardrails, not a sandbox. Read the following before running it on
a machine that holds important data:

- **More than one entry point runs code.** `run_command` (shell) and `run_program`
  (Python in a subprocess, `-I` isolated) are two arbitrary-code paths; hooks,
  external MCP servers, and the Python handlers of skills and plugins also execute
  code on your machine. None of them is OS-level isolation: shell and Python can
  also reach the network and touch absolute paths. So only install skills and MCP
  servers you trust.
- **Outbound traffic goes through a hard check (for the tools behind the floor).**
  `web_fetch` and its peers refuse private, loopback, link-local and other non-public
  targets, and follow redirects only within the same site. An MCP server on your
  machine or LAN, and a local model you configured, are normal deployments and are
  not covered by this rule.
- **Writes are classified by path.** Reads outside protected paths are allowed;
  writes outside the workspace require approval.
- **Full access is an explicit unlock.** For the specifics, see
  [`SECURITY.md`](SECURITY.md).

The complete list of the four code-execution entry points, the reporting channel,
and the isolation that is deliberately not implemented: see
[`SECURITY.md`](SECURITY.md). Data storage, lifecycle and sending: see
[`PRIVACY.md`](PRIVACY.md).

## Uninstalling and your data

Conversations, long-term memory, model configuration and credentials are all placed
under the **per-user data root**:

| Build | Data root | Default workspace |
|---|---|---|
| Installed | `%APPDATA%\Crannia` | `Documents\Crannia\workspace` |
| From source | the project directory | `workspace` inside the project |

Note that upgrading and uninstalling do not change this directory. The uninstaller
only removes the program directory and shortcuts; to clear the data, delete
`%APPDATA%\Crannia` yourself.

**Uninstall via**: Windows *Settings → Apps → Installed apps → Crannia*, or
`Uninstall Crannia.exe` in the install directory.

**The following will be deleted**: the install directory, shortcuts, registry
entries.

The settings page (“Other settings → Data directory”) shows the directories that
will not be deleted.

> Older builds kept the data inside the installation directory; on first run the
> framework copies `data/`, `logs/`, `workspace/` and `config.yaml` from there
> into the data root. Existing targets are skipped, never overwritten.

## Development

Testing does not use pytest: every test file has its own `__main__` runner.

```powershell
# full suite (parallel)
.venv\Scripts\python.exe scripts\run_tests.py

# one test file
.venv\Scripts\python.exe tests\test_pipeline.py
```

Desktop shell:

```powershell
npm install
npm run dev        # Vite + Electron; Electron starts the backend
npm run build      # build the frontend only
npm run package    # build and produce the Windows installer
```

The bundled runtime is pinned by `apps/desktop/primary-runtime-lock.json`; rebuild it
with `scripts/pin_wheels.py` + `scripts/prepare_runtime.py` when dependencies change.

Want to write a skill? Start with [`SKILL_SPEC.md`](SKILL_SPEC.md) and
[`MCP_ADAPTATION.md`](MCP_ADAPTATION.md).

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

Third-party components and their obligations:

- [`THIRD-PARTY.md`](THIRD-PARTY.md) — human-readable notices, LGPL/MPL obligations,
  asset provenance
- [`THIRD-PARTY-PYTHON.md`](THIRD-PARTY-PYTHON.md) — every bundled Python distribution
- [`sbom.json`](sbom.json) — CycloneDX SBOM
- [`licenses/`](licenses/) — full texts of LGPL-3.0, LGPL-2.1, GPL-3.0, MPL-2.0

## Trademarks

The names and logos of third-party platforms shown in this project — Feishu, WeChat,
Telegram, DingTalk and others — are trademarks of their respective owners. They are
used here only to identify which integrations Crannia supports (*nominative use*):
nothing is claimed to be sponsored, endorsed by, affiliated with, or approved by
those owners, and no logo is used as this project's own branding.

If you are a trademark owner and want a name or logo changed or removed, please
raise it in an issue.

## Contributing

Issues and pull requests are welcome — the full rules are in
[`CONTRIBUTING.md`](CONTRIBUTING.md). For a new skill, start with
[`SKILL_SPEC.md`](SKILL_SPEC.md); for a channel or provider, follow the existing
module layout under `channels/` and `providers/`. Please keep comments describing
**why** a decision was made, not the history of the change.

Security issues do **not** go through a public issue: use the private channel
described in [`SECURITY.md`](SECURITY.md), which also lists the security
boundaries. Data boundaries: [`PRIVACY.md`](PRIVACY.md).
