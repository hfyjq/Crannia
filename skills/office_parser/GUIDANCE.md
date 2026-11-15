**权威**（docx / xlsx / pptx）：OOXML（ECMA-376 Part 1）规范 ·
`.venv` 里 `openpyxl` / `python-docx` / `python-pptx` 的源码与 docstring（比凭记忆填枚举可靠）·
本机 Excel 的类型库 · 官方 readthedocs。

- **求值的权威是引擎本身**：`openpyxl` 读回的是**公式串**（`=J2-F2`），不是值 ——
  当值用只会得到一片"算不了"却看不出错。让引擎算一遍，或按定义独立算。
- ★★ **交付前把产物「看」一遍**（核的是长什么样，不是里面的值）：版式错位 ·
  文本溢出 · 元素出界 · 被裁的边，核代码看不出来。路子：`convert_to_pdf` →
  `pdf_to_images` → `read_file` 读图（真图，不是描述）。
- 本 skill 的工具**直接改原件**；要副本用 `copy_document` / `copy_worksheet`。
- ★★ **只写「两边都有的」字体族**（`Arial` / `Microsoft YaHei`）。写了那台机器没有的
  （`Inter` / `DejaVu` 类），渲染器拿别的字体顶替 —— 常拿中文字体排拉丁字符 ⇒
  按全角宽排 ⇒ 整页折行、重叠。判据是「**对方机器上有没有**」。
- ★★ **中文要显式落到 `w:eastAsia`**（`font.name` 只管 ASCII）。主题里
  `<a:ea typeface=""/>` 是空的 ⇒ Word **逐字回退**（宋体 / MS Mincho / 雅黑混排、
  粗体合成不一），像字号歪曲。自写脚本写 `w:rFonts w:eastAsia` +
  `w:lang w:eastAsia="zh-CN"`；`create_document` / `add_word_*` 已替你做。
- ★★ **试错在副本上；正式那一次「从原样」整段重跑**。别把"改一点、跑一次"
  叠在产物上 —— 后续步骤会悄悄改掉前面的（冻结窗格与居中被重置过）。
  跑第二遍前先问：**这一步是不是已经做过了**。
