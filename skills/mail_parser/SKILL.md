---
name: mail-parser
description: >
  Email skill — IMAP inbox search + SMTP send + email summarization.
  Use when the user asks to check email, read inbox, send a message,
  search for emails, or summarize recent mail.
metadata:
  kind: skill
  loader: manifest.yaml
  tools: list_folders, search_emails, get_email, send_email, summarize_emails
  mcp_note: >
    Directory name is mail_parser (underscore) for Python
    import compatibility.  SKILL.md name uses mail-parser
    (hyphen) per agentskills.io standard.
---

# Mail Parser

## When to use
Use this skill when the user:
- Asks to check their email or read their inbox
- Wants to search for specific emails (by sender, subject, keywords)
- Asks you to send an email on their behalf
- Wants a summary of recent/unread emails
- Needs to list available email folders

## Configuration needed
The user must provide:
- **IMAP server** (e.g. imap.qq.com, imap.163.com, imap.gmail.com)
- **SMTP server** (e.g. smtp.qq.com, smtp.163.com, smtp.gmail.com)
- **Email address** and **password/app password**

For Chinese email providers (QQ, 163), users must use an **authorization code**
(授权码), not their login password. Guide users to generate one in their email
settings if they haven't already.

## Tools

### list_folders
List all IMAP folders (mailboxes). Returns folder names with flags and
Chinese labels. Call first to discover folder structure.

### search_emails
Search emails by IMAP criteria. Supports folder selection, keyword search
(sender/subject/body), unread filter, and result limiting. Returns summaries
with UID — use `get_email` to read full content.

### get_email
Fetch a complete email by UID. Returns parsed headers (From/To/CC/Date/Subject),
plain text body, HTML body, and attachment metadata. Body truncated at 2000
chars to protect LLM context.

### send_email
Send an email via SMTP. Supports To/CC/BCC, and auto-detects HTML vs plain
text body. Uses SSL (port 465) or STARTTLS (port 587) based on configuration.

### summarize_emails
Quick inbox overview designed for the first tool call. Returns sender, subject,
date, and 300-char body preview for recent unread emails, plus total/unread
counts. No full body — use `get_email` for details.

## Notes
- All connections use SSL/TLS. Passwords are never stored or logged.
- The SecurityGuard audit log records send attempts (addresses only, no passwords).
- IMAP FETCH uses BODY.PEEK to avoid marking emails as read during search/summary.
- All tool results follow the unified `{"success": bool, ...}` format.
- Supports QQ邮箱, 163邮箱, Gmail, Outlook, and any standard IMAP/SMTP server.
- Zero extra dependencies — uses only Python stdlib (imaplib, smtplib, email).
