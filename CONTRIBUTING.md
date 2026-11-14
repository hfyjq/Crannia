# Contributing to Crannia

> **中文摘要**：每一处改动都要满足三件事 —— **代码改完 + 测试跑过 + 说失效了的话订正**。
> 测试**没有 pytest**，每个文件自带 `__main__`；跑全量用 `scripts/run_tests.py`。
> 安全问题走 [`SECURITY.md`](SECURITY.md)，**别开公开 issue**。

## Before you start

Read the two specs that the code actually follows:

1. [`SKILL_SPEC.md`](SKILL_SPEC.md) — what a skill is, and every field in
   `manifest.yaml`. The loader, the doctor and `create_skill` all enforce it.
2. [`AGENT_SPEC.md`](AGENT_SPEC.md) — the same for sub-agent definitions.

If you are touching the MCP side, [`MCP_ADAPTATION.md`](MCP_ADAPTATION.md) covers
the two supported skill formats and the tool-naming rules.

## Definition of done

> **A change is done when the code is changed, the tests have run, and anything the
> change made untrue has been corrected in the same commit.**

- **Stale sentences are bugs.** If a docstring or a README line said something that
  is no longer true, fix it in the same change. Nobody can run a stale sentence.
- **"It passed on my machine" is not a run.** Paste the summary line of the suite
  into the pull request.

## Running things

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt         # macOS / Linux

cp .env.example .env      # then fill in the keys you actually use
python -m web             # WebUI
npm install && npm run dev   # desktop shell (starts the backend itself)
```

> `requirements.txt` says **what** we need (lower bounds);
> `requirements.lock` pins the **direct** dependencies to the versions we
> tested against (exact). Transitive dependencies are not pinned — they still
> resolve against PyPI on the day you install. If you change
> `requirements.txt`, regenerate it:
> `python scripts/make_requirements_lock.py` — a test fails if you forget.

### Tests

There is **no pytest**. Every test file carries its own `__main__` and prints a
summary line.

```bash
# one file
.venv/Scripts/python.exe tests/test_tool_discovery.py

# everything, in parallel
.venv/Scripts/python.exe scripts/run_tests.py

# serial — required for the files marked `#@exclusive:` (they share real dirs)
.venv/Scripts/python.exe scripts/run_tests.py --serial
```

> Set `PYTHONIOENCODING=utf-8` before running anything on Windows. The default
> console is GBK, and an emoji in a *failure* message crashes the whole run — which
> then looks like "the tests failed".

A new test file must:

- run standalone (`if __name__ == "__main__": sys.exit(main())`);
- print **ASCII only** in its own output, for the reason above;
- fail loudly rather than silently skip — a guard that skips itself is not a guard.

## How to write a change

- **Say why, not what.** Comments explain the decision and the alternative that was
  rejected. Change history belongs in the commit message.
- **One fact, one place.** If the same number or sentence has to appear in two
  files, either point one at the other or add a mechanical reader that compares
  them. Two copies drifting apart is this repo's most common defect.
- **A guard you have to remember to run is not a guard.** If you write a check, make
  something fail when it is skipped — and prove it can go red (mutate a copy and
  watch it fail) before you trust it when it is green.
- **New UI copy** goes through the i18n worksheet (`scripts/i18n_scan.py`); add the
  source file there, not just the string.
- **Third-party code**: keep the upstream `LICENSE` next to it, and add an entry to
  [`THIRD-PARTY.md`](THIRD-PARTY.md). The two generated lists are rebuilt by
  `scripts/audit_licenses.py --all`; run it, do not hand-edit them.

## Pull requests

- Keep the diff to one item. If you fixed an unrelated thing on the way, say so in
  the description — do not bury it.
- Do not add a dependency without saying which license it brings and why that is
  acceptable.
- CI is not wired up yet. Run the suite yourself and paste the summary line.

## Reporting a problem

Use GitHub Issues: <https://github.com/hfyjq/Crannia/issues>. For security
problems, do not open an issue — [SECURITY.md](SECURITY.md) describes the
private channel.

Please include:

- what you did, what you expected, and what happened instead;
- the version shown in the app, and your OS;
- the smallest input that reproduces it;
- a log excerpt if there is one — **read it before you attach it** and strip
  anything you are not comfortable publishing (paths, names, keys).

Crannia never sends mail, logs or chat content on your behalf, so a report is the
only way maintainers see a problem.

Security problems do **not** go to the public issue tracker: see
[`SECURITY.md`](SECURITY.md).
