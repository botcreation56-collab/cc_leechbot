import re
import sys

def process_file(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    if "send_auto_delete_msg" not in content and "from bot.utils import" in content:
        content = content.replace("from bot.utils import", "from bot.utils import send_auto_delete_msg,")
    elif "send_auto_delete_msg" not in content:
        content = content.replace("from telegram import Update", "from telegram import Update\nfrom bot.utils import send_auto_delete_msg")

    # Replace update.message.reply_text("✅ ...")
    pattern_update = re.compile(
        r'await\s+update\.message\.reply_text\(\s*(f?"[✅❌][^"]+")\s*(?:,\s*parse_mode="Markdown"\s*)?\)'
    )
    content = pattern_update.sub(r'await send_auto_delete_msg(context.bot, update.effective_chat.id, \1, parse_mode="Markdown")', content)

    # Replace query.message.reply_text("✅ ...")
    pattern_query = re.compile(
        r'await\s+query\.message\.reply_text\(\s*(f?"[✅❌][^"]+")\s*(?:,\s*parse_mode="Markdown"\s*)?\)'
    )
    content = pattern_query.sub(r'await send_auto_delete_msg(context.bot, update.effective_chat.id, \1, parse_mode="Markdown")', content)

    # Replace msg.reply_text("✅ ...")
    pattern_msg = re.compile(
        r'await\s+msg\.reply_text\(\s*(f?"[✅❌][^"]+")\s*(?:,\s*parse_mode="Markdown"\s*)?\)'
    )
    content = pattern_msg.sub(r'await send_auto_delete_msg(context.bot, update.effective_chat.id, \1, parse_mode="Markdown")', content)

    # Note: edit_text is trickier, so we skip it to avoid creating new messages unnecessarily.
    # The user specifically mentioned permanent reply_text messages cluttering chat.

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Processed {file_path}")

process_file(r"d:\cc_leechbot\bot\handlers\files.py")
process_file(r"d:\cc_leechbot\bot\handlers\admin.py")
