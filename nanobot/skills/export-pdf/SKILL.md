---
name: export-pdf
description: "Export long responses as styled PDF files for WeChat. Use when response exceeds 500 chars or contains analysis/tables."
metadata: {"nanobot":{"emoji":"📄","always":true}}
---

# Export PDF

When replying via WeChat, long text messages often fail (clipboard/focus limitations).
Use the `export_pdf` tool to convert your response to a nicely formatted PDF and send it as a file attachment instead.

## When to use

- Your reply is **longer than 500 characters**
- Your reply contains **tables, code blocks, or structured analysis**
- The user explicitly asks for a document / report / PDF

If the reply is short plain text, just send it directly — don't over-use PDF export.

## How to use

1. Write your full response in Markdown (headings, lists, tables, code blocks all work).
2. Call `export_pdf`:
   ```
   export_pdf(content="# 分析报告\n\n## 概览\n...", title="量子计算成本分析")
   ```
   Returns: `PDF exported: C:\Users\...\nanobot_exports\量子计算成本分析_20260228_084700.pdf`
3. Send the PDF via the `message` tool using its `media` parameter:
   ```
   message(content="已将分析报告导出为PDF：", media=["C:\\...\\量子计算成本分析_20260228_084700.pdf"])
   ```

## Tips

- The `title` parameter becomes the filename prefix — use something descriptive.
- Chinese content is fully supported (uses system CJK fonts).
- Markdown extensions supported: tables, fenced code, line breaks, lists.
- You can still include a short summary in the `content` field of `message` alongside the file.
