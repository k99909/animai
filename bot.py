import os
import asyncio
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


async def get_yuki_response(user_id: int, user_message: str, include_calendar: bool = True) -> str:
    if user_id not in conversations:
        conversations[user_id] = []

    cal_context = calendar.get_context_for_yuki() if include_calendar else ""
    system = YUKI_SYSTEM_PROMPT
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversations[user_id] = []
    reply = await get_yuki_response(user_id, "Introduce yourself to Kaz-kun and give him a warm greeting! Tell him about his day.")
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
        reply = await get_yuki_response(user_id, "Kaz approved! Confirm you're on it, cutely.", include_calendar=False)
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
