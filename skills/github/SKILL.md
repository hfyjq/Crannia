---
name: github
description: Query and act on GitHub through its REST API — your identity, repository files and directory listings, code search, issues (list/read/create), pull requests (list/read/create) and recent commits. Use when the user asks about a GitHub repo, wants to look at code hosted on GitHub, file or triage an issue, or open a pull request. Requires a GitHub token in the environment.
metadata:
  kind: skill
  loader: manifest.yaml
  tools: github_whoami, github_get_file, github_search_code, github_list_issues, github_get_issue, github_create_issue, github_list_pulls, github_get_pull, github_create_pull, github_list_commits
---

# GitHub

直接走 `api.github.com`，不经过任何第三方 MCP 服务。

## 凭据

环境变量 `CRANNIA_GITHUB_TOKEN`（优先）或 `GITHUB_PERSONAL_ACCESS_TOKEN`。
细粒度令牌只需要目标仓库的 **Contents 只读**；要建 issue / PR 再给对应读写。

**没配令牌时**：工具照常出现在工具表里，调用会回一句「没有配置 GitHub 令牌」——
那不是说"功能不存在"，是"这一步需要一个令牌"。

## 什么时候用它

- 用户给的是一个 **GitHub 上**的链接 / `owner/repo` ⇒ 用它（本机的同名目录不是它）
- 要读**别人的仓库**（本机没有那份代码）⇒ 用它
- 只是读**本机**文件 ⇒ 用 `read_file`，不要绕道 GitHub

## 错误怎么读

- `401` 令牌无效或过期
- `403` 权限不足 **或** 触发限流（回执里有剩余次数与重置时间戳）
- `404` 仓库/文件/编号不对，**或者令牌看不到它**（私有仓库要选对 repository access）
