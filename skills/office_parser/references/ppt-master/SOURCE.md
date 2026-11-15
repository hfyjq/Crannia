# 这批文档是从哪来的 · 搬了什么 · 没搬什么

> 这份文件是判据的一部分，不是附件。没有它，下面那些内链里一大半会指向
> 不存在的东西，而读者分不出"我们没有这个能力"和"链接写错了"。

## 1. 来源

| | |
|---|---|
| 项目 | `hugohe3/ppt-master`（`https://github.com/hugohe3/ppt-master`） |
| 许可 | MIT（见同目录 `LICENSE`，Copyright (c) 2025-2026 Hugo He） |
| 版本 | 以本次副本为准。搬运时没有记录确切 tag —— 需要写版本号时，请对着上游 releases 页的日期核，不要照这份文件抄一个号 |

## 2. 搬了什么

本目录下的 `SKILL.md`、`references/`、`workflows/` 来自上游同名文件，逐字未改；
上游的生成代码（`svg_to_pptx` / `svg_finalize`）在本仓库的
`skills/office_parser/pptx_export/`。

`LICENSE` 原样保留 —— MIT 要求附带版权与许可文本。

## 3. 没搬什么（以及那些内链为什么是死的）

> 下表的左列，在上游是存在的（所以上游文档才会链它），在我们这儿不存在
> ⇒ 读到那些链接时不要以为文件被删了，是没搬。

| 没搬的 | 上游路径 | 为什么不搬 |
|---|---|---|
| 图片那一族 | `references/image-{base,generator,searcher,layout-patterns,layout-spec}.md` · `image-palettes/` · `image-renderings/` · `image-type-templates/` · `ai-image-comparison/` | 体积很大，而且依赖外部的图片生成/搜索服务 —— 我们没有那条线，搬了也是空头支票 |
| `templates/` 整个 | `templates/{brands,charts,decks,icons,layouts}` + 2 份 reference | 是素材不是方法 |
| `scripts/` 整个 | `scripts/**` | 但转换器我们有 —— 见下面的路径映射 |
| 其余 workflow | `workflows/{create-brand,create-template,failure-recovery,generate-audio,index,live-preview,refine-spec,resume-execute,routing,topic-research,verify-charts}.md` | 大多绑它自己的多角色流程、品牌制造与音频（我们没有音频那条线） |
| `modes/` | `references/modes/` | 绑它的运行模式 |
| 上游仓库的文档 | `../../docs/zh/...` | 本来就不在 skill 包里 |
| 正文里的示例文件名 | `001.md` · `slide_NN.svg` · `analysis/*.json` | 这些不是链接 —— 是正文里举例子的文件名。别把它们当断链去修 |

## 4. 路径映射（上游那个 → 我们这边是什么）

上游文档说"调用 `scripts/xxx.py`"时，我们这边该用什么：

| 上游的 | 我们的 | 状态 |
|---|---|---|
| `scripts/svg_to_pptx/**` | `skills/office_parser/pptx_export/svg_to_pptx/**` | 同一份代码（已随本目录一起收录） |
| `scripts/svg_finalize/**` | `skills/office_parser/pptx_export/svg_finalize/**` | 同上 |
| （写 SVG） | 工具 `create_svg_slide` | 我们有 |
| （合成 pptx） | 工具 `svgs_to_pptx` | 我们有 |
| （设定风格） | 工具 `set_slide_design` + `references/spec_lock.md` | 我们有 |
| （版式库） | `references/layout_library.md` | 我们有 |
| `scripts/source_to_md/ppt_to_md.py` | 工具 `pptx_to_outline` | 我们自己的口径（每页 标题/正文/备注/图/表/链接） |
| `scripts/beautify_identity.py` | 工具 `extract_pptx_identity` | 我们自己的口径（主题 12 色 + 字体 + 实际用量） |
| `scripts/beautify_inventory.py` | 没有 | 它拼的是上游两件脚本的产物，那两个我们没有 |
| `scripts/visual_review.py` | 没有（方法在 `references/visual-review.md`） | 方法能读，脚本没有 |
| `scripts/pptx_to_svg.py` | 没有 | 没有 |

### 那两件读工具为什么不照搬上游脚本

`ppt_to_outline` 与 `extract_pptx_identity` 是按我们的口径重写的，不是照搬：

- 上游 `ppt_to_md.py` 的 docstring 写着要 `pin python-pptx<0.7`，
  而本仓库装的是新的 python-pptx —— 照搬是在赌它没坏；
- 上游 `beautify_identity.py` 复用 `pptx_to_svg` 那一整套解析器，
  而那套我们没有，照搬等于连拖进来一整个子系统。

所以 `beautify-pptx.md` 里凡是指向那两个脚本（以及 `scripts/**` 下别的）的段落，
对我们不成立 —— 用它那条路时，把"提取内容"和"读身份"两步换成上面那两件工具，
其余照着读。

## 5. 一个命名冲突，记住它

| 词 | 上游的意思 | 我们的意思 |
|---|---|---|
| beautify / 美化 | 重新排版 —— 内容逐字不动、继承原视觉身份、产出一份新的 pptx | 我们那 5 件工具（`set_ppt_background` / `crop_ppt_picture` / `add_ppt_audio` / `set_ppt_master_text_style` / `set_ppt_background_graphics`）是就地改属性 |

上游那份文档里逐字写着 beautify "is not a patch over the original"，
而我们那 5 件恰恰就是 patch。两件事撞名了，读的时候别混。

## 6. 更新它怎么办

手工。没有同步脚本。

要更新：拿一份新的上游 → 按 §2 的口径逐项覆盖 → 重跑
`tests/test_ppt_master_docs.py`（它钉着这些文件真的在）。

不要写成自动同步：我们有 vendored 的代码、上游有它自己的演进节奏，
覆盖前必须人工看一眼 §3 的表有没有变。

### 覆盖之后必须做的一件事：把适配说明加回去

`SKILL.md` 与 `workflows/*.md`（6 个文件）的正文之前，有一块我们加的适配说明，
标记逐字是：

```
<!-- ▼ 本仓库适配说明 —— 这段不是上游内容，同步时别丢 ▼ -->
```

覆盖上游会把这块冲掉 —— 而冲掉的后果是：那几篇 workflow 加起来提到
`scripts/` 很多次，而我们一个脚本都没搬 ⇒ 模型读到
"跑 `python3 scripts/source_to_md/ppt_to_md.py`"会照做，然后扑空，
而扑空的症状看起来像它不听话。

判据：`tests/test_ppt_master_docs.py::test_the_adaptation_banner_is_kept`
—— 它逐字查这 6 个文件里有没有这个标记。冲掉了它当场红。
