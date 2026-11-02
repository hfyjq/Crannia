# Privacy

[简体中文](PRIVACY.zh.md) | English

## Data storage

Everything under the data directory (`%APPDATA%\Crannia` on Windows):

| What | Where | Notes |
|---|---|---|
| Chat history (messages, tool results) | `memory/short_term.db` (SQLite) | plain file, no application-level encryption |
| Session notes | `memory/sessions/*.md` | written by the agent |
| Long-term facts | `memory/facts/<scope>/*.md` | written by the agent or by you |
| Daily logs | `logs/YYYY-MM-DD.md` | one section per turn, kept 90 days |
| Plans, file history, tool-output artifacts | `plans/`, `workhistory/`, `workdata/` | see the data directory |
| Credentials (API keys) | `.env`, endpoint-credentials file | the latter is excluded from read tools |

There is **no application-level encryption** on any of these. Anyone who can read
your user profile or the disk can read them too. Use OS account permissions and
full-disk encryption if that matters for your threat model.

## Retention and deletion

- **Not in the context any more** — `/clear` moves the conversation pointer; the
  old rows stay in the database.
- **Compressed** — history summarisation **appends** a summary and a boundary row.
  The original messages are not deleted; that is what makes history search work.
- **Deleted from disk** — only deleting the conversation thread (or the data
  directory) removes the underlying rows.

Retention that *is* automatic: daily logs are pruned after **90 days**; archived
session notes and plan files after **30 days** by default (configurable in
settings). Note that uninstalling the application does **not** remove your data
directory.

## Outbound data

Only through features you use:

| Feature | What is sent | To |
|---|---|---|
| Talking to the model | your messages, context, attachments | the model provider you configured |
| Web search | the query text | the search backend in use (Parallel by default; DuckDuckGo as fallback) |
| Fetching a page | the corresponding URL | that site |
| Song search | the keywords | NetEase Cloud Music's public search endpoint |
| Sending mail / IM messages | what you ask to send | your mail server / the IM platform |
| External MCP servers | tool arguments | the server you configured (and, for HTTP servers, that endpoint) |

With a **local model** (Ollama, LM Studio) the conversation itself does not leave
the machine; web search and other tools still contact their own services when you
use them.

Crannia has **no telemetry and no feedback upload of its own**: it does not send
usage data, crash reports or chat content to the project. Update checks fetch a
static version manifest (no identifiers attached).

## Personal data in tool output (ICE)

Tool results can carry personal data — a spreadsheet of employees, a PDF full of
phone numbers. ICE (`core/ice/`) scans that text before it reaches the model and,
for the shapes it recognises, can swap in stable pseudo-values.

**What it detects**:

| Shape | Validator | Reversible |
|---|---|---|
| PRC resident ID (18 digits) | GB 11643 check digit | yes, when the check digit passes |
| Mobile number | allocation-prefix shape only | **no — deliberately** (a prefix only means "looks like") |
| Bank card | Luhn | yes |

**Current boundary** — email addresses, WeChat IDs, postal addresses, passports.
There is no rule for them, and this page is the boundary: an exhaustive PII scanner
is not what this is.

Note that ICE is still a beta feature; more protection will come in later versions.

## Your controls

- Settings → Security: workspace hooks, workspace plugins, workspace LSP config,
  auto-mode, network and command allow-lists.
- Settings → Memory: what the agent may write, and the memory library (read,
  delete, restore).
- `/clear`: start a fresh conversation without deleting stored data.
- Deleting a conversation thread, or the data directory itself, deletes the data.
