---
name: diagram
description: Create flowcharts, mind maps and structure diagrams as SVG (optionally PNG). Use when the user asks for a flowchart, process diagram, mind map, brain map, org chart, or wants to visualize nodes and connections. Accepts structured nodes/edges or a Mermaid flowchart/mindmap subset. Rendering is local (graph-layout + hand-built SVG); no Mermaid CLI, Graphviz, or browser download.
metadata:
  kind: skill
  loader: manifest.yaml
  tools: create_flowchart, create_mindmap, render_diagram
---

# 结构化绘图（diagram）

把"节点 + 连线"变成**可交付的 SVG**（默认），可选 PNG。
流程图用分层布局（Sugiyama），思维导用整齐树布局（Reingold-Tilford）。

## 什么时候用

- 用户要**流程图 / 流程 / 决策树 / 依赖图 / 时序图** ⇒ `create_flowchart`
- 用户要**思维导图 / 脑图 / 目录树 / 组织架构** ⇒ `create_mindmap`
- 已经有 SVG 要转 PNG ⇒ `render_diagram`
- 用户要**统计图表**（柱/折/饼）⇒ **不用这里**，用 `plot_excel_data` / `create_excel_chart`
- 用户要**从无到有画一张图**（插画）⇒ 用 `media_gen`

## 两种输入

1. **结构化（推荐）**：`nodes` 数组 + `edges` 数组
   - node: `{id, label, kind}`，`kind` 可为 `rect`/`decision`/`start`/`end`
   - edge: `{source, target}` 或 `["A","B"]`
2. **Mermaid 文本子集**：`source` 参数
   - `flowchart TD` / `graph LR`：`A[开始] --> B{判断} --> C[结束]`
   - `mindmap`：按缩进建树，第一行是根
   - **只支持这个子集**；`sequenceDiagram`/`classDiagram` 等会明确报错

## 输出

- 默认 **SVG** 落在工作区，回执给路径，`image/svg+xml` 可直接内联/交付
- 要 PNG：`render_diagram(file_path, target="png")`
- 要 PPT：把 SVG 交给 office_parser 的 `svgs_to_pptx`

## 依赖与边界

- 需要 `graph-layout`（MIT，约 3 MB，**不需要 Graphviz 系统程序**）；
  没装时工具会明确说"请 pip install graph-layout"，不会假装成功
- SVG 只用白名单元素：节点是矩形/椭圆/菱形，连线是 line/polygon（箭头自画，
  不用 marker）；不支持 filter/shadow 等高级效果
- 图太复杂（SVG >100 KB）会要求减少节点或简化文字
- **不引** mermaid-cli（Node+Chromium）、Graphviz（EPL）、PlantUML（Java）——
  依赖链保持干净
