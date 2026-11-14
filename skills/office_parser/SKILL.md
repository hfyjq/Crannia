---
name: office-parser
description: Parse Microsoft Office documents — Word (.docx), PowerPoint (.pptx), and Excel (.xlsx) — with streaming extraction and chunking. Use when working with Office documents, spreadsheets, presentations, or when the user mentions docx, pptx, xlsx, Word, Excel, PowerPoint, Office files.
metadata:
  kind: skill
  loader: manifest.yaml
  tools: parse_docx, parse_pptx, parse_xlsx, extract_document_structure, create_document, add_block, add_slide, export_document, get_workbook_metadata, read_excel_data, write_excel_data, apply_formula, validate_formula_syntax, format_excel_range, create_excel_chart, create_excel_pivot, create_excel_table, copy_worksheet, delete_worksheet, rename_worksheet, merge_excel_cells, unmerge_excel_cells, get_merged_cells, copy_excel_range, delete_excel_range, insert_excel_rows, insert_excel_columns, delete_excel_rows, delete_excel_columns, get_document_info, get_document_text, get_document_outline, list_available_documents, copy_document, merge_documents, add_word_heading, add_word_paragraph, apply_table_of_contents, delete_paragraph, search_and_replace_text, insert_content_near_text, format_paragraph_text, create_custom_style, format_word_table, set_table_cell_shading, apply_table_alternating_rows, highlight_table_header, merge_table_cells, merge_table_cells_horizontal, merge_table_cells_vertical, set_table_cell_alignment, set_table_alignment_all, set_table_column_width, set_table_width, auto_fit_table, format_table_cell_text, set_table_cell_padding, get_all_comments, get_comments_by_author, get_comments_for_paragraph, add_footnote, convert_to_pdf, convert_from_pdf, mock_image, create_svg_slide, svgs_to_pptx, set_slide_design, get_design_context, extract_archive, freeze_excel_panes, set_excel_conditional_format, add_excel_data_validation, profile_excel_data, find_excel_value, get_excel_comments, sort_excel_range, filter_excel_data, remove_excel_duplicates, protect_excel_sheet, set_excel_hyperlink, export_excel_to_csv, merge_excel_workbooks, recalculate_excel_formulas, plot_excel_data, add_table_rows, add_table_columns, delete_table_rows, delete_table_columns
  mcp_note: >
    Directory name is office_parser (underscore) for Python import compatibility.
    SKILL.md name uses office-parser (hyphen) per agentskills.io standard.
    A thin adapter layer (see MCP_ADAPTATION.md) will map these when
    publishing to the MCP ecosystem.
---

# Office Document Parser Skill

## When to use
Use this skill when the user needs to read, search, or summarize Microsoft Office documents.
Supports Word (.docx), PowerPoint (.pptx), and Excel (.xlsx) formats.

## Tools

### parse_docx
Stream-parse a Word document. Paragraphs are read one at a time via python-docx iterator,
accumulated into pseudo-pages (50 paragraphs each), and fed through the unified chunker.
Tables are also extracted and included inline.

### parse_pptx
Extract text from PowerPoint slides. Each slide is treated as one page. Text from all
shapes on each slide is collected, then chunked by the unified chunker.

### parse_xlsx
Stream-read an Excel spreadsheet using openpyxl read_only mode. Rows are read one at a time
via `iter_rows()`, accumulated into pseudo-pages (100 rows each), and chunked.
Optionally specify a sheet name; defaults to the active sheet.

### extract_document_structure
Extract document outline/structure from Word or PowerPoint files. For Word (.docx):
returns heading hierarchy (level, text, paragraph position). For PowerPoint (.pptx):
returns slide numbers, titles, layout types, and text preview. Use to quickly
understand document organization before doing a full parse.

## Write Tools (v0.8.0+)

### create_document
Initialize a new document session for creating Word, PowerPoint, or Excel files.
Returns a `session_id` used by all subsequent write operations.

### add_block
Append a content block to a Word document under construction. Supports: heading
(level 1-3), paragraph, table (rows of cells), list (ordered/unordered), image
(from file), and page_break. Max 20 blocks per document.

### add_slide
Append a slide to a PowerPoint presentation. Slide types: title (title+subtitle),
content (title+bullets), image (title+picture), table (title+data rows).

### write_sheet
Write rows of data to an Excel workbook sheet. Call repeatedly to add more data,
then `export_workbook` to render.

### finalize_document
Lock a document session for export. After this, no more `add_block`/`add_slide`
calls will succeed.

### export_document
Render a Word (.docx) or PowerPoint (.pptx) from accumulated blocks and save
to disk. Auto-finalizes if not already done.

### export_workbook
Render an Excel (.xlsx) workbook from accumulated sheets and save to disk.

## Shared tools
After parsing with any tool above, use these shared tools (from file-parser skill):
- **search_chunks**: BM25 keyword search across parsed content
- **summarize_document**: Iterative aggregation summary

## Notes
- File path resolution: bare filenames are auto-resolved under `workspace/`
- All paths validated by SecurityGuard before any file is opened
- openpyxl read_only mode ensures Excel files are never fully loaded into memory
- python-docx and python-pptx iterate one element at a time — no bulk reads
