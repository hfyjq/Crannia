---
name: ppt-master
description: 做一份原生可编辑的 .pptx（不是 HTML 翻页）：策略 → 版式 → 视觉审查 → 导出，19 种视觉风格 + 4 种论证模式任选。做 PPT / 演示文稿 / 幻灯片时用。
---

# ppt-master —— 薄壳 + 按需读

这份壳只做一件事：告诉你权威在哪、按什么顺序读。

正文故意不在这儿（上游那份 78 KB）：一次性塞进来会挤掉判断 ——
一个"摘要"工具把"分析"挤掉了，是同一类失败。
而这条契约的原句就是 Load referenced resources only as needed。

## 基准目录

```
skills/office_parser/references/ppt-master/
```

用框架给的那句 `Base directory for this skill:` 里的绝对路径去拼它
（不要写死相对路径 —— 那要看当时的工作目录）。

## 按这个顺序读

| # | 什么时候读 | 读哪一份 |
|---|---|---|
| 0 | 每次都要先读 | `SOURCE.md` —— 搬了什么 / 没搬什么 / 路径映射 / 一个命名冲突。不读它，正文里那几十处 `scripts/xxx.py` 会让你去调根本不存在的东西，而扑空的症状看起来像你不听话 |
| 1 | 任何一次 | `SKILL.md`（上游正文，流程总纲） |
| 2 | 定「怎么论证」 | `references/modes/_index.md` |
| 3 | 定「长什么样」 | `references/visual-styles/_index.md` —— 19 种，其中 `swiss-minimal.md` 就是瑞士国际主义那一档 |
| 4 | 具体动手 | `references/executor-base.md` · `references/shared-standards.md` |
| 5 | 重排别人给的 pptx | `workflows/beautify-pptx.md` —— 它的两个前置我们已经有了：`pptx_to_outline`（内容）· `extract_pptx_identity`（视觉身份） |
| 6 | 收尾自查 | `references/visual-review.md` |
| 7 | 版式资产 | `references/ppt-master/references/` 下的 `template-designer.md` / `strategist.md` |

## 两条本仓库适配

1. 本仓库的出口是 `.pptx`，不是 HTML。上游正文里若提到 HTML / SVG 中间产物，
   按我们已有的 12 个 pptx 工具走（`add_slide` · `create_svg_slide` · `svgs_to_pptx` ·
   `set_slide_design` · `set_ppt_master_text_style` …）。
2. 这里没有 `scripts/` —— 上游让你跑的脚本我们一个都没搬（`SOURCE.md §4`）。
   要哪个能力，先看我们的工具里有没有；没有再想别的路。

## 交付前

对产物回读一遍再交：拿 `pptx_to_outline` 或 `extract_pptx_identity`
读回你刚写的那份 —— 不是"文件能打开"，是"里面的东西对不对"。
