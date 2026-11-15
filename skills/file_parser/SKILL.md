---
name: file-parser
description: Parse PDF and TXT files with streaming chunking and BM25 retrieval. Extracts text page-by-page (PDF) or line-by-line (TXT), never loading full document into memory. Use when working with PDF documents, text files, or when the user mentions PDF, TXT, reading files, document parsing, text extraction.
metadata:
  kind: skill
  loader: manifest.yaml
  tools: parse_pdf, parse_txt, search_chunks, summarize_document, pdf_info, merge_pdfs, split_pdf, extract_pages
  mcp_note: >
    Directory name is file_parser (underscore) for Python import compatibility.
    SKILL.md name uses file-parser (hyphen) per agentskills.io standard.
    A thin adapter layer (see MCP_ADAPTATION.md) will map these when
    publishing to the MCP ecosystem.
---

# File Parser Skill

## When to use
Use this skill when the user needs to read, search, or summarize PDF or TXT documents.
Supports Chinese and English content.

## Tools

### parse_pdf
Stream-parse a PDF file using PyMuPDF (fitz). Each page's text is extracted individually and
chunked into pieces of ≤1500 characters with page/position metadata. The full document text
is NEVER accumulated in memory.

### parse_txt
Stream-parse a TXT file. Lines are read one at a time (never `read()`), accumulated into
pseudo-pages (100 lines each), and chunked. Supports configurable encoding (default UTF-8).

### search_chunks
Search currently-loaded document chunks using BM25 keyword relevance with jieba Chinese
tokenization. Returns top-3 most relevant chunks. Accepts optional `file_path` to auto-parse
before searching.

### summarize_document
Generate a full-document summary via iterative chunk aggregation. Chunks are batch-summarized
in Round 1, then merged in groups of 3 recursively until one final summary remains.
Accepts optional `file_path` to auto-parse before summarizing.

### pdf_info
Extract PDF metadata and structure without parsing text content. Returns page count,
file size, title, author, creation date, encryption status, and table of contents
(bookmarks/outline). Use to decide whether a PDF needs parsing.

### merge_pdfs
Merge multiple PDF files into one. Files are combined in the order provided.
All inputs must be valid, accessible PDFs. Output saved to `workspace/`.

### split_pdf
Split a PDF into multiple files, each containing N pages (default: 1 page per file).
Output files named `{name}_part{n}.pdf`.

### extract_pages
Extract specific pages (1-indexed) from a PDF into a new file. Duplicate page
numbers allowed — each occurrence copies that page.

## Notes
- File path resolution: bare filenames are auto-resolved under `workspace/`
- All paths validated by SecurityGuard before any file is opened
- Chunked content is NEVER directly exposed to the LLM context — only via retrieval
