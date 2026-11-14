# 项目说明（`vendor/ripgrep/`）

这是**随安装包一起发出去**的 `ripgrep` 二进制 —— 用户不需要另外装任何东西。

## 里面有什么

| 文件 | 是什么 |
|---|---|
| `win32-x64/rg.exe` | ripgrep **15.0.0**（`rev 3a612f88b8`，`features:+pcre2`） |
| `COPYING.txt` · `LICENSE-MIT.txt` · `UNLICENSE.txt` | ripgrep 本体自己的许可文本（上游原文） |
| `NOTICE-vscode-ripgrep.txt` | **打包来源**那一层的许可（见下） |
| `package-source.json` | 来源包的 `package.json`（名字 / 版本 / 许可），原样留档 |

## 两层许可（都要署）

1. **ripgrep 本体** —— **MIT OR Unlicense**，`Copyright (c) 2015 Andrew Gallant`
   （`COPYING.txt` 逐字：*"This project is dual-licensed under the Unlicense and
   MIT licenses. You may use this code under the terms of either license."*）。
   **我们按 MIT 那一支履约**（Unlicense 是"放弃版权"式声明，在部分法域效力不完整；
   而 MIT 的署名义务本来就要履）。两个文本都留着，与上游一致。
2. **打包来源** —— `@vscode/ripgrep-win32-x64@1.18.0`（**MIT**，Microsoft）。
   我们只是用它**取那个二进制**，没有改它。

## 来源与校验

| | |
|---|---|
| 来源 | `https://registry.npmjs.org/@vscode/ripgrep-win32-x64/-/ripgrep-win32-x64-1.18.0.tgz` |
| 包内路径 | `package/bin/rg.exe` |
| 大小 | **5,429,760** 字节 |
| **sha256** | `f9dde63498b3193f098355dbec97af99dc4f6b8fa0df5ed04114a03012c042cb` |
| 版本自报 | `rg --version` → `ripgrep 15.0.0 (rev 3a612f88b8)` · `features:+pcre2` |

★ **为什么要 `--pcre2`**：我们的 `grep` 工具对模型承诺的是"Python 正则"，
而 ripgrep 的默认引擎没有后行断言/反向引用。带 `pcre2` 的这个构建兜得住那条契约。

## 为什么是"随包"而不是"用时下载"

用户的判据：**首次安装走安装包，一个包解决问题，不要单独去装依赖。**
⇒ 二进制进仓库、进打包白名单（`apps/desktop/electron-builder.config.mjs` 的
`BACKEND_TREE`），运行期**绝不联网下载**。

## 怎么更新（换版本时）

1. 改这里和 `core/rg.py` 里钉的平台目录名（`win32-x64`）；
2. 从同一来源取新包，**重新记 sha256**；
3. 跑 `tests/test_rg_engine.py`（它会核二进制在、版本可读、`--pcre2` 可用）；
4. 同步 `THIRD-PARTY.md` 里那一条（若许可变了以新文本为准）。

⚠️ 只发 **Windows x64**。mac/linux 的目录等到真的要发那两个平台时再加 ——
现在**没有**它们，`core/rg.py` 会明确报"这个平台没有二进制"，不假装能用。
