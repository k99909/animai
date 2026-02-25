import os
import asyncio
import json
import logging
import tempfile
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from anthropic import Anthropic
from groq import Groq
from dotenv import load_dotenv
from yuki import YUKI_SYSTEM_PROMPT
from calendar_client import CalendarClient
from memory_manager import load_memory, update_memory
from obsidian_client import get_full_context, has_written_today, append_to_daily_note

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')

# Only these Telegram user IDs can interact with Yuki
_raw = os.getenv('ALLOWED_USER_IDS', '')
ALLOWED_USER_IDS = set(int(uid.strip()) for uid in _raw.split(',') if uid.strip())

client = Anthropic(api_key=ANTHROPIC_API_KEY)
groq_client = Groq(api_key=os.getenv('GROQ_API_KEY'))
calendar = CalendarClient()

# Per-user conversation history
conversations = {}
# Pending approval actions
pending_actions = {}
# Message counter for memory updates
message_count = {}
# Users currently in note-writing mode (next message gets saved to Obsidian)
note_mode = {}


def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


async def safe_reply(update: Update, text: str, **kwargs):
    """Send a message, splitting at 4096 chars if needed."""
    limit = 4096
    for i in range(0, len(text), limit):
        await update.message.reply_text(text[i:i + limit], **kwargs)


async def get_yuki_response(user_id: int, user_message: str, include_calendar: bool = True) -> str:
    if user_id not in conversations:
        conversations[user_id] = []

    cal_context = calendar.get_context_for_yuki() if include_calendar else ""
    obsidian_context = get_full_context()
    memory = load_memory()
    system = YUKI_SYSTEM_PROMPT
    if memory:
        system += f"\n\n━━━ YUKI'S MEMORY ━━━\n{memory}"
    if cal_context:
        system += f"\n\n━━━ LIVE CALENDAR CONTEXT ━━━\n{cal_context}"
    if obsidian_context:
        system += f"\n\n━━━ KAZ'S OBSIDIAN NOTES ━━━\n{obsidian_context}"

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
        await safe_reply(update, clean, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await safe_reply(update, reply)


async def execute_approved_action(action_text: str) -> str:
    current_date = datetime.now().strftime('%Y-%m-%d')

    def extract():
        return client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            system=f"""Extract the action from the message and return ONLY a JSON object.
No explanation, just JSON.

For calendar events return:
{{"type": "create_event", "title": "...", "start": "2026-02-25T10:00:00", "end": "2026-02-25T11:00:00"}}

If it's not a calendar event or you can't extract it clearly return:
{{"type": "unknown"}}

Use ISO 8601 format for dates. Assume Pacific Time (America/Los_Angeles). Today is {current_date}.""",
            messages=[{"role": "user", "content": action_text}]
        )

    response = await asyncio.to_thread(extract)
    try:
        data = json.loads(response.content[0].text.strip())
        if data.get("type") == "create_event":
            calendar.create_event(
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
    if not is_allowed(user_id):
        return
    conversations[user_id] = []
    reply = await get_yuki_response(user_id, "Kaz-kun is back! Greet him warmly, reference something from your memory if relevant, and tell him about his day.")
    await send_yuki_reply(update, reply, user_id)


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    reply = await get_yuki_response(user_id, "Give Kaz-kun his full schedule briefing for today in your warm, kawaii style!")
    await send_yuki_reply(update, reply, user_id)


async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    reply = await get_yuki_response(user_id, "Walk Kaz-kun through his week ahead!")
    await send_yuki_reply(update, reply, user_id)


async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    text = ' '.join(context.args) if context.args else ''
    if text:
        append_to_daily_note(text)
        reply = await get_yuki_response(
            user_id,
            f"Kaz just added this to his Obsidian daily note: \"{text}\". Acknowledge briefly and warmly.",
            include_calendar=False
        )
        await send_yuki_reply(update, reply, user_id)
    else:
        # No text provided — enter note mode for next message
        note_mode[user_id] = True
        await update.message.reply_text("Ready! Send me what you want to add and Yuki will save it to your notes ✨")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    text = update.message.text

    # If user is in note mode, save to Obsidian then respond
    if note_mode.pop(user_id, False):
        append_to_daily_note(text)
        reply = await get_yuki_response(
            user_id,
            f"Kaz just shared this and Yuki saved it to his Obsidian daily note: \"{text}\". "
            "Acknowledge it warmly, maybe reflect on what he shared, and respond naturally.",
        )
        await send_yuki_reply(update, reply, user_id)
        return

    reply = await get_yuki_response(user_id, text)
    await send_yuki_reply(update, reply, user_id)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    voice = update.message.voice

    # Download voice file
    file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    await file.download_to_drive(tmp_path)

    # Transcribe with Groq Whisper
    def transcribe():
        with open(tmp_path, "rb") as audio:
            return groq_client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=("voice.ogg", audio),
            )

    try:
        result = await asyncio.to_thread(transcribe)
        text = result.text.strip()
        logger.info(f"Voice transcribed: {text}")
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        await update.message.reply_text("Yuki couldn't hear that clearly~ Try again? 🎙️")
        return
    finally:
        os.unlink(tmp_path)

    if not text:
        await update.message.reply_text("Yuki didn't catch anything~ 🎙️")
        return

    reply = await get_yuki_response(user_id, text)
    await send_yuki_reply(update, reply, user_id)


async def send_proactive(context: ContextTypes.DEFAULT_TYPE, prompt: str):
    """Send a scheduled message to Kaz."""
    user_id = int(os.getenv('ALLOWED_USER_IDS', '0').split(',')[0].strip())
    if not user_id:
        return
    try:
        reply = await get_yuki_response(user_id, prompt)
        # Proactive messages don't come from an Update, so send directly
        limit = 4096
        for i in range(0, len(reply), limit):
            await context.bot.send_message(chat_id=user_id, text=reply[i:i + limit])
    except Exception as e:
        logger.error(f"Proactive message error: {e}")


async def morning_briefing(context: ContextTypes.DEFAULT_TYPE):
    await send_proactive(context,
        "Good morning Kaz-kun! Give him his morning briefing: "
        "what's on his calendar today, anything notable from his Obsidian notes, "
        "and one content idea based on what he's been writing about lately. "
        "Keep it warm and energizing — set the tone for a good day."
    )


async def midday_checkin(context: ContextTypes.DEFAULT_TYPE):
    await send_proactive(context,
        "It's midday — check in on Kaz-kun. "
        "Ask how the morning went, remind him of any afternoon calendar events, "
        "and if something in his notes today looks postable, mention it naturally. "
        "Keep it short and light."
    )


async def evening_wrapup(context: ContextTypes.DEFAULT_TYPE):
    await send_proactive(context,
        "Good evening! Do Kaz-kun's evening wrap-up: "
        "acknowledge what he's accomplished today based on his notes and calendar, "
        "mention anything to prep for tomorrow, "
        "and pitch one content idea inspired by something he lived or wrote today. "
        "Warm and grounding — help him close the day well."
    )


async def note_nudge(context: ContextTypes.DEFAULT_TYPE):
    """At 2pm, nudge Kaz to write in Obsidian if he hasn't yet."""
    if has_written_today():
        return
    user_id = int(os.getenv('ALLOWED_USER_IDS', '0').split(',')[0].strip())
    if not user_id:
        return
    note_mode[user_id] = True
    try:
        reply = await get_yuki_response(
            user_id,
            "Kaz hasn't written in his Obsidian daily note yet today. "
            "Gently nudge him to jot something down — even just a few thoughts about his day, "
            "how he's feeling, what he's working on. Tell him whatever he sends back, "
            "Yuki will save it straight to his notes. Keep it light and encouraging.",
            include_calendar=False
        )
        limit = 4096
        for i in range(0, len(reply), limit):
            await context.bot.send_message(chat_id=user_id, text=reply[i:i + limit])
    except Exception as e:
        logger.error(f"Note nudge error: {e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not is_allowed(user_id):
        return
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
    app.add_handler(CommandHandler("note", note_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Scheduled proactive messages (Pacific Time)
    import pytz
    pt = pytz.timezone('America/Los_Angeles')
    jq = app.job_queue
    jq.run_daily(morning_briefing, time=datetime.strptime("09:00", "%H:%M").replace(tzinfo=pt).timetz())
    jq.run_daily(midday_checkin,   time=datetime.strptime("13:00", "%H:%M").replace(tzinfo=pt).timetz())
    jq.run_daily(note_nudge,       time=datetime.strptime("14:00", "%H:%M").replace(tzinfo=pt).timetz())
    jq.run_daily(evening_wrapup,   time=datetime.strptime("19:00", "%H:%M").replace(tzinfo=pt).timetz())

    print("✨ Yuki is awake and ready for Kaz-kun! ✨")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
