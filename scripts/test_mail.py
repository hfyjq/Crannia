#!/usr/bin/env python3
"""
Quick test script for mail_parser skill with QQ email.
Run: .venv/Scripts/python scripts/test_mail.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills.mail_parser import handler
from core.security import SecurityGuard

# Init security guard
sg = SecurityGuard(
    allowed_dirs=["./workspace"],
    command_blacklist=[],
    audit_log_path="./logs/audit.log"
)
handler.init(sg)

# QQ email config
IMAP_SERVER = "imap.qq.com"
SMTP_SERVER = "smtp.qq.com"
EMAIL = "you@qq.com"
PASSWORD = "YOUR_QQ_AUTH_CODE"  # 替换为你的授权码

print("=" * 60)
print("mail_parser QQ 邮箱测试")
print("=" * 60)

# ── Test 1: list_folders ──
print("\n[1/5] list_folders — 列出文件夹...")
r = handler.list_folders(IMAP_SERVER, EMAIL, PASSWORD)
if r["success"]:
    print(f"  ✅ {r['count']} 个文件夹:")
    for f in r["folders"]:
        label = f" ({f['label']})" if f.get("label") else ""
        print(f"     - {f['name']}{label}")
else:
    print(f"  ❌ 失败: {r['error']}")

# ── Test 2: summarize_emails (unread) ──
print("\n[2/5] summarize_emails — 未读邮件摘要...")
r = handler.summarize_emails(IMAP_SERVER, EMAIL, PASSWORD, limit=5, unread_only=True)
if r["success"]:
    print(f"  ✅ 总邮件 {r.get('total_messages', '?')} 封, 未读 {r.get('unread_count', '?')} 封")
    print(f"  返回 {r['count']} 封摘要:")
    for e in r["emails"]:
        flag = "🔵" if e.get("unread") else "  "
        print(f"     {flag} [{e['uid']}] {e['from']} — {e['subject'][:50]}")
        if e.get("preview"):
            print(f"        预览: {e['preview'][:80]}...")
else:
    print(f"  ❌ 失败: {r['error']}")

# ── Test 3: search_emails (最近10封) ──
print("\n[3/5] search_emails — 最近10封邮件...")
r = handler.search_emails(IMAP_SERVER, EMAIL, PASSWORD, folder="INBOX", limit=10)
if r["success"]:
    print(f"  ✅ 返回 {r['count']} 封:")
    for e in r["emails"][:5]:
        flag = "🔵" if e.get("unread") else "📖"
        print(f"     {flag} [{e['uid']}] {e['from'][:30]} — {e['subject'][:50]}")
    if r["count"] > 5:
        print(f"     ... 还有 {r['count'] - 5} 封")
else:
    print(f"  ❌ 失败: {r['error']}")

# ── Test 4: get_email (first from search) ──
print("\n[4/5] get_email — 获取最新一封完整内容...")
r_search = handler.search_emails(IMAP_SERVER, EMAIL, PASSWORD, folder="INBOX", limit=1)
if r_search["success"] and r_search["emails"]:
    uid = r_search["emails"][0]["uid"]
    r = handler.get_email(IMAP_SERVER, EMAIL, PASSWORD, uid)
    if r["success"]:
        body_len = len(r.get("body_text", ""))
        att_count = len(r.get("attachments", []))
        print(f"  ✅ 主题: {r['subject'][:60]}")
        print(f"     发件人: {r['from']}")
        print(f"     日期: {r['date']}")
        print(f"     正文长度: {body_len} 字符")
        print(f"     附件: {att_count} 个")
        if r["body_text"]:
            print(f"     正文前100字: {r['body_text'][:100]}...")
    else:
        print(f"  ❌ 失败: {r['error']}")
else:
    print("  ⏭ 跳过（无可获取的邮件）")

# ── Test 5: send_email (发给自己) ──
print("\n[5/5] send_email — 发送测试邮件给自己...")
r = handler.send_email(
    SMTP_SERVER, EMAIL, PASSWORD,
    to=EMAIL,
    subject="[Crannia 测试] mail_parser Skill 发信验证",
    body="这是一封由 Crannia mail_parser Skill 自动发送的测试邮件。\n\n"
         "如果你收到这封邮件，说明 SMTP 发信功能正常。\n\n"
         f"发送时间: 测试时间\n"
         "Skill 版本: v0.1.0"
)
if r["success"]:
    print(f"  ✅ {r['message']}")
    print(f"     收件人: {r['to']}")
    print(f"     主题: {r['subject']}")
else:
    print(f"  ❌ 失败: {r['error']}")

print("\n" + "=" * 60)
print("测试完成！请检查收件箱是否收到了测试邮件。")
print("=" * 60)
