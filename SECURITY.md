# Security Policy

[简体中文](SECURITY.zh.md) | English

## Reporting a vulnerability

**Do not open a public issue for a security problem.** Use the private channel:

1. Open the repository's **Security** tab, then **Report a vulnerability**.
   That opens a private advisory visible only to the maintainers.
2. If that form is not available to you, open a regular issue that says only
   "security report — please open a private channel", with no details. We will
   open an advisory and invite you to it.

**Before posting, check again that it contains nothing you would not want to
disclose**: API keys, chat logs, personal paths, or anything inside your `data/`
directory.

Please provide information in this order:

1. **Which boundary you crossed** — path access, the network check, command
   approval, the untrusted-content wrapper, or credential handling.
2. The **exact inputs** (a prompt, a skill, a file, a URL) and the observed result.
3. The version you ran (`core/version.py`) and how you installed it.

We will acknowledge your report; once the problem is confirmed, we will fix it in
an upcoming release.

## Supported versions

Only feedback on the **latest version** is supported. This project ships as a
desktop installer; there are no long-term-support branches.

## What can execute code on your machine

There are four entry points, listed in the table below.

| Entry | Code comes from | When it runs | Gate |
|---|---|---|---|
| `run_command` / `run_program` | the model, on your behalf | when you approve (or auto-mode decides) | deterministic command policy + approval |
| Hooks (`data/hooks.json`, workspace hook files) | you, or a repository | on tool events | workspace hook files require the **Workspace hooks** switch |
| External MCP servers (`data/mcp_servers.json`, plugin `.mcp.json`) | you, or a plugin package | at framework start (stdio servers start a process; HTTP servers open a connection) | plugin-provided servers from the workspace require the **Workspace plugins** switch |
| Skills (`skills/`, `~/.agents/skills`, `~/.claude/skills`, plugin `skills/`) | you, a package, or a repository | Python handlers are imported and executed on first call | skill roots you place yourself are not gated; skill packages found in the **workspace** require the **Workspace plugins** switch — same switch, same reason as hooks and MCP |
| Plugin instruction files (`SKILL.md`, `commands/*.md`, `agents/*.md`) | a package, or a repository | their text enters the model context | not gated — they are text, not code |

The table splits by **what a component can do**, not by **where it came from**:
anything that ends in executing code on your machine (a hook command, an MCP
process, a Python handler) sits behind the workspace switch; anything that only
supplies text does not, and is handled as untrusted content instead.

Worth noting:

- A skill's Python handler is **in-process code**. Once it is loaded, no content
  wrapper or tool permission restricts it.
- `SKILL.md` bodies are *instructions written for the agent*. They are cleaned of
  control characters and framework tags, but that is hygiene, not a trust
  boundary — a skill you did not write is a prompt you did not write.

## Project boundaries

Crannia runs an LLM agent that can read files, call tools and — with approval —
run commands on your machine.

- **It is not a sandbox.** `run_command` is gated by a deterministic policy plus a
  human approval step, but bash is still a complete route to the network and is
  not confined by the file-path guard. **OS-level isolation is not supported.**
- **Path guard.** File I/O performed by the framework goes through a three-tier
  path classifier; a command that **mentions** a protected path is raised to an
  approval tier.
- **Network check (for the tools that use it).** `web_fetch` refuses non-public
  addresses, embedded credentials and single-label hosts, and re-checks every
  redirect hop. External MCP endpoints are checked too; worth noting: loopback and
  LAN targets are allowed (a local MCP server is a normal deployment), while a
  **public** endpoint that redirects into the local network is rejected.
- **Untrusted content is wrapped — only for the tools we classify as reading
  third-party text.** That covers `web_fetch` / `web_search`, email bodies, user
  documents and comments, command output, and external MCP tool results (unless
  you mark a server `trusted_output`). Tools that return their own structured
  data — file operations, spreadsheet reads, memory lookups — are returned as-is.
  Detection of injection indicators is **advisory**: content is never rewritten,
  by design.
- **MCP annotations do not grant permissions.** An external server's
  `readOnlyHint` is shown to you but never selects the permission tier: every
  external MCP tool is treated as `write`, so it is not exempted from the
  auto-mode review, never runs concurrently, and is unavailable in plan mode or
  read-only sub-agents. A local trust switch for this is planned, not built.
  `destructiveHint` still maps to the `destructive` axis.
- **Credentials.** Keys live in `.env` / the endpoint-credentials file, are
  excluded from the environment passed to subprocesses, and are redacted from
  approval payloads. Skill configuration (`skill_configs` in
  `data/settings.json`) is stored as **plain JSON on your disk** — that includes
  mail authorisation codes. The same data *is* treated as a credential when you
  export settings (scrypt + AES-GCM), but not at rest.

## Out of scope

- Attacks that require an already-compromised machine or an already-malicious
  `config.yaml`.
- Prompts that make the model produce unlawful content.
- Anything that is agreed to in the approval dialog and then complained about
  afterwards.

## What we will never ask you for

We will never ask for your API keys, your chat logs, or a copy of your `data/`
directory. **Crannia has no telemetry, no feedback upload and no automatic
reporting of its own** — it never sends your mail, logs or chat content anywhere
on your behalf.

This does not mean it never talks to third parties: the features you use do. For
what is sent and how long it is kept, see [PRIVACY.md](PRIVACY.md).
