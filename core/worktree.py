# -*- coding: utf-8 -*-
"""子 agent 的隔离工作树：一次派活 = 一个 git worktree + 一条临时分支。

## 形状

    git worktree add -b crannia/<slug> <临时目录> HEAD

子 agent 的「这一段落点」临时指向那个目录（`session_workspace.use_path`），
它在里面读写；结束时把改动提交到那条分支、移除工作树，把分支名与改了哪些文件
交回父。父用普通命令决定收下还是丢掉：

    git merge crannia/<slug>        # 收下
    git branch -D crannia/<slug>    # 丢掉

## 为什么用临时目录，不在工作区里开子目录

工作区里的目录会被 `glob` / `grep` 扫到 —— 同一个文件出现两份，而
"查"那一侧没有任何东西能把它们分开。临时目录不在任何扫描根下。

## 边界

- 需要工作区在一个 git 仓库里；不是，拒，并说明怎么办。
- 提交身份：仓库没配 `user.name` / `user.email` 时用
  `Crannia <crannia@localhost>`，回执里说明。
- 移除工作树在提交之后：提交失败就留着那个目录（改动比目录整洁重要），
  回执给出路径。
"""

from __future__ import annotations

import secrets
import shutil
import tempfile
import time
from pathlib import Path

__all__ = ["create", "finish", "discard"]


def _git(argv: list, cwd) -> tuple[int, str]:
    from core.shell import run_quiet
    return run_quiet(["git", "-C", str(cwd)] + list(argv), cwd)


def _slug() -> str:
    return "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(3))


def create(workspace) -> dict:
    """从一个 git 仓库开一个隔离工作树。返回 `{success, repo, path, branch, slug}`。"""
    rc, root = _git(["rev-parse", "--show-toplevel"], workspace)
    if rc != 0 or not root:
        return {
            "success": False,
            "error": ("这个工作区不在 git 仓库里，没法开隔离工作树。"
                      "要么先 `git init` 并提交一次，要么不带 `isolate` 直接派。"),
        }
    repo = Path(root.splitlines()[0].strip())
    slug = _slug()
    branch = "crannia/%s" % slug
    holder = Path(tempfile.mkdtemp(prefix="crannia-wt-"))
    path = holder / "wt"
    # 清理上一次异常退出留下的元数据；失败不影响本次。
    _git(["worktree", "prune"], repo)
    rc, msg = _git(["worktree", "add", "-b", branch, str(path), "HEAD"], repo)
    if rc != 0:
        shutil.rmtree(holder, ignore_errors=True)
        return {"success": False,
                "error": "开隔离工作树失败：%s" % (msg or "git 报错")}
    return {"success": True, "repo": str(repo), "path": str(path),
            "branch": branch, "slug": slug, "holder": str(holder)}


def finish(repo, path, branch: str, slug: str) -> dict:
    """提交工作树里的改动，然后移除工作树。分支保留。"""
    wt = Path(path)
    rc, msg = _git(["add", "-A"], wt)
    if rc != 0:
        return {"success": False, "branch": branch, "path": str(wt),
                "error": "暂存失败：%s" % (msg or "git 报错")}

    rc, status = _git(["status", "--porcelain"], wt)
    if rc != 0:
        return {"success": False, "branch": branch, "path": str(wt),
                "error": "读工作树状态失败：%s" % (status or "git 报错")}
    if not status.strip():
        discard(repo, path, branch)
        return {"success": True, "changed": [], "branch": "", "discarded": True}

    changed = sorted({ln[3:].strip() for ln in status.splitlines()
                      if len(ln) > 3 and ln[3:].strip()})
    rc, msg = _git(["commit", "-m", "crannia: %s" % slug], wt)
    identity_note = ""
    if rc != 0:
        # 仓库没配提交身份。用一次性身份提交，并在回执里说明。
        rc, msg = _git(["-c", "user.name=Crannia",
                        "-c", "user.email=crannia@localhost",
                        "commit", "-m", "crannia: %s" % slug], wt)
        identity_note = "提交用了 `Crannia <crannia@localhost>`（仓库没配提交身份）。"
    if rc != 0:
        return {"success": False, "branch": branch, "path": str(wt),
                "changed": changed,
                "error": "提交失败（工作树保留在 %s）：%s" % (wt, msg or "git 报错")}

    rc2, _ = _git(["worktree", "remove", "--force", str(wt)], repo)
    shutil.rmtree(Path(path).parent, ignore_errors=True)
    return {"success": True, "changed": changed, "branch": branch,
            "removed": rc2 == 0, "note": identity_note}


def discard(repo, path, branch: str) -> None:
    """移除工作树并删掉分支（忽略失败）。"""
    _git(["worktree", "remove", "--force", str(path)], repo)
    shutil.rmtree(Path(path).parent, ignore_errors=True)
    _git(["branch", "-D", branch], repo)
