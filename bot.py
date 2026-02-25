import os
import asyncio
import json
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from anthropic import Anthropic
from dotenv import load_dotenv
from yuki import YUKI_SYSTEM_PROMPT
from calendar_client import CalendarClient
from memory_manager import load_memory, update_memory

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')

client = Anthropic(api_key=ANTHROPIC_API_KEY)
calendar = CalendarClient()

# Per-user conversation history
conversations = {}
# Pending approval actions
pending_actions = {}
# Message counter for memory updates
message_count = {}


async def get_yuki_response(user_id: int, user_message: str, include_calendar: bool = True) -> str:
    if user_id not in conversations:
        conversations[user_id] = []

    cal_context = calendar.get_context_for_yuki() if include_calendar else ""
    memory = load_memory()
    system = YUKI_SYSTEM_PROMPT
    if memory:
        system += f"\n\n━━━ YUKI'S MEMORY ━━━\n{memory}"
    if cal_context:
        system += f"\n\n━━━ LIVE CALENDAR CONTEXT ━━━\n{cal_context}"

    conversations[user_id].append({"role": "user", "content": user_message})

    # Keep last 20 messages
    if len(conversations[user_id]) > 20:
        conversations[user_id] = conversations[user_id][-20:]

    messages_snapshot = list(conversations[user_id])

    def call_api():
        return client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=system,
            messages=messages_snapshot
        )

    response = await asyncio.to_thread(call_api)
    reply = response.content[0].text
    conversations[user_id].append({"role": "assistant", "content": reply})

    # Update memory every 5 messages in the background
    message_count[user_id] = message_count.get(user_id, 0) + 1
    if message_count[user_id] % 5 == 0:
        asyncio.create_task(update_memory(conversations[user_id]))

    return reply


async def send_yuki_reply(update: Update, reply: str, user_id: int):
    if "[APPROVAL_NEEDED]" in reply:
        clean = reply.replace("[APPROVAL_NEEDED]", "").strip()
        keyboard = [[
            InlineKeyboardButton("✅ Do it!", callback_data="approve"),
            InlineKeyboardButton("✏️ Edit first", callback_data="edit"),
            InlineKeyboardButton("❌ Skip", callback_data="skip"),
        ]]
        pending_actions[user_id] = clean
        await update.message.reply_text(clean, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(reply)


async def execute_approved_action(action_text: str) -> str:
    def extract():
        return client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            system="""Extract the action from the message and return ONLY a JSON object.
No explanation, just JSON.

For calendar events return:
{"type": "create_event", "title": "...", "start": "2026-02-25T10:00:00", "end": "2026-02-25T11:00:00"}

If it's not a calendar event or you can't extract it clearly return:
{"type": "unknown"}

Use ISO 8601 format for dates. Assume Pacific Time (America/Los_Angeles). Current year is 2026.""",
            messages=[{"role": "user", "content": action_text}]
        )

    response = await asyncio.to_thread(extract)
    try:
        data = json.loads(response.content[0].text.strip())
        if data.get("type") == "create_event":
            link = calendar.create_event(
                title=data["title"],
                start_iso=data["start"],
                end_iso=data["end"]
            )
            return f"Calendar event '{data['title']}' created successfully!"
    except Exception as e:
        logger.error(f"Action execution error: {e}")
    return "action noted"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversations[user_id] = []
    reply = await get_yuki_response(user_id, "Kaz-kun is back! Greet him warmly, reference something from your memory if relevant, and tell him about his day.")
    await send_yuki_reply(update, reply, user_id)


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reply = await get_yuki_response(user_id, "Give Kaz-kun his full schedule briefing for today in your warm, kawaii style!")
    await send_yuki_reply(update, reply, user_id)


async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reply = await get_yuki_response(user_id, "Walk Kaz-kun through his week ahead!")
    await send_yuki_reply(update, reply, user_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    reply = await get_yuki_response(user_id, text)
    await send_yuki_reply(update, reply, user_id)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    action = query.data

    if action == "approve":
        pending = pending_actions.get(user_id, "")
        result = await execute_approved_action(pending)
        reply = await get_yuki_response(
            user_id,
            f"Kaz approved! You executed this action: {result}. Confirm cutely.",
            include_calendar=False
        )
        await query.edit_message_text(reply)
    elif action == "edit":
        await query.edit_message_text("Of course~! ✏️ What would you like to change, Kaz-kun? Tell Yuki! ♡")
    elif action == "skip":
        reply = await get_yuki_response(user_id, "Kaz skipped this one. Acknowledge briefly and sweetly.", include_calendar=False)
        await query.edit_message_text(reply)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("week", week_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    print("✨ Yuki is awake and ready for Kaz-kun! ✨")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
