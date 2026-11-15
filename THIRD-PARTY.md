# 第三方素材与许可

本项目包含下列第三方组件与素材。完整清单另见
[`THIRD-PARTY-PYTHON.md`](THIRD-PARTY-PYTHON.md)（随包的 Python 发行包）、
[`THIRD-PARTY-FRONTEND.md`](THIRD-PARTY-FRONTEND.md)（前端包）与
[`sbom.json`](sbom.json)（CycloneDX）。

---

## 1. 厂商图标（`web/static/img/provider-*.svg`，16 个）

| | |
|---|---|
| **来源** | **LobeHub** 的 [`@lobehub/icons`](https://github.com/lobehub/lobe-icons)（MIT） |
| **版权** | `Copyright (c) 2023 LobeHub` |
| **许可** | MIT（全文见下） |

验证出处：这 16 个文件都带 LobeHub `Icon` 组件的渲染指纹
`style="flex:none;line-height:1"`；带渐变的那几个还留着命名空间
`fill="url(#lobe-icons-…-0-_R_0_)"`（`_R_0_` 由 React `useId` 生成）。

```bash
grep -l 'lobe-icons' web/static/img/provider-*.svg          # 渐变类
grep -l 'style="flex:none;line-height:1"' web/static/img/provider-*.svg | wc -l   # 应当是 16
```

**商标**：图标里的图形为各家厂商的商标。仅以此来指代那一家（指名使用），并不等于得到
它们的背书或暗示任何的合作。

### MIT 全文

```
MIT License

Copyright (c) 2023 LobeHub

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 2. 通道图标（`web/static/img/channel-*`）

| 文件 | 来源 | 许可 |
|---|---|---|
| `channel-telegram.svg` · `channel-wechat.svg` · `channel-qq.svg` | [Simple Icons 16.35.0](https://github.com/simple-icons/simple-icons) | **CC0 1.0**（无需署名；此处仍记录出处） |
| `channel-feishu.svg` · `channel-dingtalk.svg`（源文件存 `packages/ui/assets-src/`；运行时 PNG 由它们生成） | [`@douyinfe/semi-icons` 2.104.0](https://www.npmjs.com/package/@douyinfe/semi-icons) 的 `src/svgs/feishu_logo.svg` · [`@ant-design/icons-svg` 4.6.0](https://www.npmjs.com/package/@ant-design/icons-svg) 的 `inline-namespaced-svg/outlined/dingtalk.svg` | **MIT**（版权声明见下；按各家官方配色上色） |

版权声明（MIT）：

- Feishu mark: `Copyright (c) 2019-present DouyinFE`（Semi Design）
- DingTalk mark: `Copyright (c) 2018-present Ant UED`（Ant Design Icons）
- MIT 全文见 §1。

商标另算：图形仍是各家的商标，用途只是**指代那一家**（指名使用），不暗示官方合作；
商标不在 MIT 或 CC0 的授权范围内。

`web/static/img/` 下那两个 128×128 的运行时图标由上述两份 SVG 生成（裁到内容边界、
等比缩放、透明底），对源文件所做的改动只有上色。另两份来自 Simple Icons，出处可查。

本项目不改变这四个文件的许可，也不对它们主张额外权利。本项目的 Apache-2.0 只覆盖
自己写的代码与 §6 那些自绘素材。

---

## 3. 前端运行依赖

| 包 | 许可 | 用途 | 义务 |
|---|---|---|---|
| [`dompurify`](https://github.com/cure53/DOMPurify) | **MPL-2.0 OR Apache-2.0**（双许可） | 模型写的 SVG / Mermaid 输出的消毒 | 选 **Apache-2.0** 那一支（与我们的许可兼容）；保留版权与许可文本 |
| [`mermaid`](https://github.com/mermaid-js/mermaid) | **MIT** | ` ```mermaid ` 围栏渲染 | 保留版权与许可文本 |
| `react` / `react-dom` | **MIT** | 前端组件（Vite 打进 dist） | 保留版权与许可文本 |
| [`lucide-react`](https://github.com/lucide-icons/lucide) | **ISC** | 界面图标 | 保留版权与许可文本 |

上表是**直接**依赖，真正进产物的是它整条闭包 —— 完整清单与逐包许可原文见
[`THIRD-PARTY-FRONTEND.md`](THIRD-PARTY-FRONTEND.md)（共 **117** 个包）。其中两家不是
纯宽松许可：`elkjs`（**EPL-2.0**，`mermaid` 的可选布局引擎，未修改上游文件，上游源码
<https://github.com/kieler/elkjs>）与 `dompurify`（MPL-2.0 OR Apache-2.0，按
Apache-2.0 那一支履约）。

---

## 4. 其余第三方

- **npm / Python 依赖**按各自包的许可，由 `package.json` / `requirements.txt` 记录；
  Electron 自身的许可由打包工具带进产物。
- **随包的 Python 运行时**（`python-build-standalone`）是 CPython 的再分发，许可随包。
- **全量清单**：`THIRD-PARTY-PYTHON.md`（136 个 Python 包）与 `sbom.json`
  （CycloneDX，266 个组件）。
单独点名的一支（直接依赖，`NOTICE` 里也写了）：

| 组件 | 许可 | 用途 | 义务 |
|---|---|---|---|
| `rank_bm25`（PyPI 名 `rank-bm25`） | **Apache-2.0** | 本地 BM25 检索 | 保留版权与许可声明 |

---

## 5. PDF 栈

| 包 | 许可 | 用途 | 义务 |
|---|---|---|---|
| [`pypdf`](https://github.com/py-pdf/pypdf) | **BSD-3-Clause** | 合并 / 拆分 / 提取页 / 重排 / 旋转 / 水印合并 / AES-256 加密 / 结构压缩 | 保留版权与许可文本 |
| [`pypdfium2`](https://github.com/pypdfium2-team/pypdfium2) | **BSD-3-Clause / Apache-2.0**（内含 PDFium） | PDF → 图片 | 保留它 wheel 里的 `LICENSES/`（Apache-2.0 · BSD-3-Clause · CC-BY-4.0） |
| [`pdfplumber`](https://github.com/jsvine/pdfplumber) | **MIT** | 文本 / 表格提取（`parse_pdf`） | 保留版权与许可文本 |
| `pdfminer.six` | **MIT** | `pdfplumber` 的引擎 | 保留版权与许可文本 |
| [`reportlab`](https://pypi.org/project/reportlab/) | **BSD** | 生成水印页（含中文 CID 字体） | 保留版权与许可文本 |
| [`docvortex`](https://github.com/myhloli/DocVortex) | **MIT**（METADATA 是复合表达式 `MIT AND Apache-2.0 AND BSD-3-Clause`） | PDF → Word | **保留它自己的 `LICENSE.md` 与内置字体 `NOTICE`**（Android 字体 = Apache-2.0；另有 BSD-3 组件） |
| `Pillow` | **MIT-CMU / HPND** | 图片 → PDF | 保留版权与许可文本 |

这一栈不含 AGPL 组件。`opencv-python-headless`（**Apache-2.0**）的视频抽帧用于图片输入，
它内置的 ffmpeg 动态库是 **LGPL-2.1**（见 §10）。`docvortex` 的许可表达式以其 wheel
`METADATA` 的原文为准，不只抄其中一个。

---

## 6. 自绘素材与语录

| 素材 | 来源 | 许可 |
|---|---|---|
| `web/static/img/logo.svg` · `crannia.ico` | **本项目自绘**的干净绿色像素 C（`scripts/make_brand_assets.py`，3×4 格、透明底） | 本项目 Apache-2.0 |
| `packages/ui/src/assets/avatar/slot-0..7.png` | **本项目自绘像素头像**（`scripts/prepare_avatars.py`，纯代码生成） | 本项目 Apache-2.0 |
| `web/static/audio/safety-mode.mp3` | **Pixabay**（pixabay.com/zh/sound-effects/） | **Pixabay Content License**：可商用、**无需署名**、不可把音频**原样单独再分发**；嵌入应用作为界面音效属于衍生使用。该许可没有署名要求，所以具体作品 URL 不记也不影响许可结论 |
| `apps/desktop/quotes.txt` | 逐条标注**作者 / 出处**的短引文 | **不是本项目授权内容**；版权归原作者。仅作为启动界面的**短引文展示**（署名随文可见），不构成对原文的再许可 |

上表里「本项目自绘」的每一样都能用对应脚本重画出来；引文的版权归原作者。

---

## 7. SVG → PNG

| 包 | 许可 | 用途 | 义务 |
|---|---|---|---|
| [`resvg_py`](https://github.com/baseplate-admin/resvg-py) | **MIT**（wrapper） | SVG → PNG（PPTX Office 兼容模式） | 保留版权与许可文本 |
| └ `resvg` 0.48.1（Rust 静态链进 wheel） | **Apache-2.0 OR MIT**（0.45 起重新许可；0.44 及以前是 MPL-2.0） | 实际渲染器 | 选 Apache-2.0 那一支；保留声明 |

`CairoSVG` / `svglib` 不在依赖里（LGPL）。

---

## 8. LGPL 系 —— 保留并履约

| 包 | 版本 | 许可 | 用途 | 上游源码 |
|---|---|---|---|---|
| `python-telegram-bot` | 22.8 | **LGPL-3.0-only**（wheel 同时带 GPLv3 文本，**选 LGPL 那一支**） | Telegram 通道 | <https://github.com/python-telegram-bot/python-telegram-bot> |
| `pystray` | 0.19.5 | **LGPLv3** | 系统托盘 | <https://github.com/moses-palmer/pystray> |
| `py7zr` | 1.1.4 | **LGPL-2.1-or-later** | .7z 解压 | <https://github.com/miurahr/py7zr> |
| `inflate64` | 1.0.4 | **LGPL-2.1-or-later** | `py7zr` 传递依赖 | <https://github.com/miurahr/inflate64> |
| `multivolumefile` | 0.2.3 | **LGPL-2.1-or-later** | 同上 | <https://github.com/miurahr/multivolume> |
| `pybcj` | 1.0.8 | **LGPL-2.1-or-later** | 同上 | <https://github.com/miurahr/pybcj> |
| `pyppmd` | 1.3.1 | **LGPL-2.1-or-later** | 同上 | <https://github.com/miurahr/pyppmd> |

履约状态：

1. **未修改**：以上全部是 PyPI 上游发布的原始 wheel，没有打过补丁。
2. **可替换**：提交物是 NSIS 安装器（`setup.exe`），安装后落成的是普通目录
   （`resources/primary-runtime/python/Lib/site-packages/<包名>`），用户可自行替换其中
   任何一个包，不需要重新编译本项目；本项目不发布便携单文件或 PyInstaller onefile
   形式的产物。
3. **许可文本**：`licenses/` 下的 `LGPL-3.0.txt`、`GPL-3.0.txt`（LGPL-3.0 正文引用
   它）、`LGPL-2.1.txt`，以及各包的 `dist-info/licenses/`。
4. **源码**：上表仓库加对应版本 tag；wheel 地址与 sha256 记在
   `apps/desktop/primary-runtime-lock.json`。
5. **无静态链接**：本项目不分发把 LGPL 库链进单一二进制的产物。

---

## 9. MPL-2.0 系 —— 保留并履约

MPL-2.0 是**文件级** copyleft：未修改的文件随包分发并保留声明即可。

| 包 | 版本 | 许可 | 用途 | 上游源码 |
|---|---|---|---|---|
| `certifi` | 2026.10.19 | **MPL-2.0** | CA 证书包（`requests` / `httpx` / `aiohttp` 依赖） | <https://github.com/certifi/python-certifi> |

`tqdm` 与 `orjson` 不在随包运行时里（它们是已移除的向量库的传递依赖），所以 MPL
的义务只剩 `certifi`。完整文本见 `licenses/MPL-2.0.txt` 与该包的
`dist-info/licenses/`。

---

## 10. 非 Python 组件（随安装包一起分发）

| 组件 | 版本 | 许可 | 许可文件 |
|---|---|---|---|
| **CPython**（自带解释器） | 3.13.16 | **PSF-2.0** | `python/LICENSE.txt` |
| **Tcl/Tk** | 8.6 | **Tcl/Tk license（BSD 风格）** | `python/tcl/tk8.6/license.terms` |
| **Electron** | 44.7.0 | **MIT** | `release/win-unpacked/LICENSE.electron.txt` |
| **Chromium**（Electron 内置） | 随 Electron 44.7.0 | **BSD-3-Clause + 第三方声明** | `release/win-unpacked/LICENSES.chromium.html` |
| **FFmpeg**（`opencv_videoio_ffmpeg500_64.dll`） | 5.0 | **LGPL-2.1** | `cv2/LICENSE-3RD-PARTY.txt` |
| **PDFium**（`pypdfium2` 内置） | 随 `pypdfium2` 5.14.0 | **Apache-2.0 / BSD-3 / CC-BY-4.0** | `pypdfium2-*.dist-info/licenses/LICENSES/` |
| **resvg**（Rust，经 `resvg_py`） | 0.48.1 | **Apache-2.0 OR MIT** | `resvg_py-*.dist-info/sboms/resvg_py.cyclonedx.json` |
| **DocVortex native core**（Rust） | 0.5.16 | **MIT AND Apache-2.0 AND BSD-3-Clause** | `docvortex-0.5.16.dist-info/licenses/LICENSE.md` |
| **React / React DOM** | 19.3.0 | **MIT** | 打进 `apps/desktop/dist`（源码不在包内） |
| **Mermaid** | 12.2.0 | **MIT** | 同上 |
| **Lucide React** | 1.52.0 | **ISC** | 同上 |
| **DOMPurify** | 3.4.17 | **MPL-2.0 OR Apache-2.0**（选 Apache） | 同上 |

### FFmpeg（LGPL-2.1）

| | |
|---|---|
| 二进制 | `cv2/opencv_videoio_ffmpeg500_64.dll`，随 `opencv-python-headless` 的 wheel 分发 |
| 版本 | **5.0**（依据 DLL 名 `opencv_videoio_ffmpeg500_64.dll`；`cv2.getBuildInformation()` 不印版本号） |
| 许可文本 | `cv2/LICENSE-3RD-PARTY.txt`（LGPL-2.1 全文），也在 `licenses/LGPL-2.1.txt` |
| 上游源码 | <https://ffmpeg.org/download.html> 的 **`n5.0`** 分支 |
| 修改 | **没有** —— 原样取自 OpenCV 官方发布的 wheel |
| 可替换 | 它是 `cv2/` 下的独立文件、按文件名加载，换成自编译的版本即可生效 |

---

## 11. 随包的 ripgrep（`vendor/ripgrep/`）

| | |
|---|---|
| **本体** | ripgrep **15.0.0**（`rev 3a612f88b8`，`features:+pcre2`） |
| **许可** | **MIT OR Unlicense** —— `Copyright (c) 2015 Andrew Gallant`，按 MIT 那一支履约 |
| 上游原文 | `vendor/ripgrep/COPYING.txt` · `LICENSE-MIT.txt` · `UNLICENSE.txt` |
| **打包来源** | `@vscode/ripgrep-win32-x64@1.18.0`（**MIT**，Microsoft），只用它取二进制 |
| 来源包许可 | `vendor/ripgrep/NOTICE-vscode-ripgrep.txt` · `package-source.json` |
| 校验 | `sha256 f9dde63498b3193f098355dbec97af99dc4f6b8fa0df5ed04114a03012c042cb` · 5,429,760 字节 |

它用于 `grep` 工具的执行层（`core/rg.py`）。两层许可都要署：ripgrep 本体（MIT，
Andrew Gallant）与打包来源（MIT，Microsoft）。只随包发布 Windows x64 的二进制。

---

## 12. PPT Master 的文档与代码（MIT）

| | |
|---|---|
| **本体** | `hugohe3/ppt-master`（<https://github.com/hugohe3/ppt-master>） |
| **许可** | **MIT** —— `Copyright (c) 2025-2026 Hugo He` |
| **内容** | `skills/office_parser/references/ppt-master/` 的文档，与 `skills/office_parser/pptx_export/` 的代码 |
| **许可文本** | `skills/office_parser/references/ppt-master/LICENSE` |

---

## 13. 移植的 Word 工具（MIT）

| | |
|---|---|
| **本体** | `GongRzhe/Office-Word-MCP-Server`（<https://github.com/GongRzhe/Office-Word-MCP-Server>） |
| **许可** | **MIT** —— `Copyright (c) 2025 GongRzhe` |
| **内容** | `skills/office_parser/word_tools.py` 中与上游同名的工具函数；其余为本项目自写 |
| **许可文本** | `skills/office_parser/references/LICENSE-Office-Word-MCP-Server.txt` |

---

## 14. 移植的 Excel 工具（MIT）

| | |
|---|---|
| **本体** | `haris-musa/excel-mcp-server`（<https://github.com/haris-musa/excel-mcp-server>） |
| **许可** | **MIT** —— `Copyright (c) 2025 Haris` |
| **内容** | `skills/office_parser/excel_tools.py` 中与上游同名的工具函数；其余为本项目自写 |
| **许可文本** | `skills/office_parser/references/LICENSE-excel-mcp-server.txt` |
| 归属 | 同名项目不止一个，归属按上游 `pyproject.toml` 的 `name` + `authors` + README 仓库链接三者一致确定 |

---

## 15. `MessageDeduplicator` 的实现模式（MIT）

| | |
|---|---|
| **本体** | `zhayujie/chat-on-wechat` |
| **许可** | **MIT** —— `Copyright (c) 2022 zhayujie` |
| **内容** | `core/dedup.py` 的 LRU + TTL 过期字典模式 |
| **许可文本** | `licenses/MIT-chat-on-wechat.txt` |

---

## 16. Luhn 校验的边界判据（MIT）

| | |
|---|---|
| **本体** | Microsoft **Presidio**（`presidio-analyzer`） |
| **许可** | **MIT** |
| **内容** | `core/ice/rules.py` 的 Luhn 边界判据；未复制代码，因此不附全文 |

---

## 17. 桌面壳与 skill 外壳（MIT）

| | |
|---|---|
| **本体** | DeepSeek 的 **dsh / deepseek-harness** |
| **许可** | **MIT** —— `Copyright (c) 2026 DeepSeek` |
| **内容** | `bus/loaders/skill_md.py` 的 skill 外壳；`apps/desktop/web-document.cjs` 与 `backend-lifecycle.cjs` 的对应实现 |
| **许可文本** | `licenses/MIT-deepseek-harness.txt` |

---

## 18. 微信 iLink 通道的协议事实

| | |
|---|---|
| **来源** | `zhuzhaoyun/Molio` · `zhayujie/chat-on-wechat` |
| **许可** | Molio：**modified Apache-2.0**，全文见 `licenses/Molio-LICENSE.txt` |
| **内容** | 未复制代码；仅对两条协议事实参考（媒体密钥的三种写法，以及 `getuploadurl` / `sendmessage` 的字段形状） |
| **说明** | 字段名与编码方式由 iLink 服务端决定，属协议事实；本条目即 Apache-2.0 的署名履约 |
