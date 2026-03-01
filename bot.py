import os
import asyncio
import json
import re
import logging
import tempfile
from datetime import datetime, timedelta, time as dtime
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
from gmail_client import GmailClient
from memory_manager import load_memory, update_memory
from obsidian_client import get_full_context, has_written_today, get_available_destinations, append_to_file
from notion_client import NotionClient

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
gmail = GmailClient()
notion = NotionClient()

BASE_DIR = os.path.dirname(__file__)
CONVERSATIONS_FILE = os.path.join(BASE_DIR, "conversations.json")
PENDING_SUGGESTIONS_FILE = os.path.join(BASE_DIR, "pending_suggestions.json")
SUGGESTION_DAYS = {0, 2, 4}  # Mon, Wed, Fri


def _load_json(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return {int(k): v for k, v in json.load(f).items()}
        except Exception:
            pass
    return {}


def _save_json(path: str, data: dict):
    try:
        with open(path, "w") as f:
            json.dump({str(k): v for k, v in data.items()}, f)
    except Exception as e:
        logger.error(f"Failed to save {path}: {e}")


# Per-user conversation history (persisted across restarts)
conversations = _load_json(CONVERSATIONS_FILE)
# Pending approval actions
pending_actions = {}
# Message counter for memory updates
message_count = {}
# Users currently in note-writing mode (next message gets saved to Obsidian)
note_mode = {}
# Pending resource suggestions awaiting a choice (1/2/3)
pending_suggestions = _load_json(PENDING_SUGGESTIONS_FILE)


def should_include_gmail_context(user_message: str) -> bool:
    text = (user_message or "").lower()
    keywords = (
        "gmail", "email", "inbox", "subject", "reply", "respond", "follow up",
        "follow-up", "send to", "@"
    )
    return any(keyword in text for keyword in keywords)


def is_calendar_read_request(user_message: str) -> bool:
    text = (user_message or "").lower()
    read_patterns = (
        r"\bwhat(?:'s| is)\s+(?:on|in)\s+(?:my|the)\s+(?:calendar|schedule)\b",
        r"\bwhat(?:'s| is)\s+my\s+schedule\b",
        r"\btoday(?:'s)?\s+schedule\b",
        r"\bthis\s+week(?:'s)?\s+schedule\b",
        r"\bdo i have\b.*\b(?:calendar|schedule|event|events)\b",
        r"\bam i free\b",
        r"\bcan you\b.*\b(?:read|see|access|view)\b.*\b(?:calendar|schedule)\b",
        r"\b(?:read|see|access|view)\b.*\b(?:my|the)\s+(?:calendar|schedule)\b",
        r"\bcan you check\s+(?:my|the)\s+(?:calendar|schedule)\b",
        r"\bcheck\s+(?:my|the)\s+(?:calendar|schedule)\b",
        r"\bwhat(?:'s| is)\s+on\s+my\s+calendar\b",
    )
    return any(re.search(pattern, text) for pattern in read_patterns)


def should_include_week_calendar_context(user_message: str) -> bool:
    text = (user_message or "").lower()
    week_markers = (
        r"\bthis week\b",
        r"\bnext week\b",
        r"\bweek ahead\b",
        r"\bupcoming week\b",
        r"\bcoming week\b",
        r"\bnext 7 days\b",
        r"\bnext seven days\b",
        r"\brest of (?:the )?week\b",
    )
    return any(re.search(pattern, text) for pattern in week_markers)


def is_calendar_action_request(user_message: str) -> bool:
    text = (user_message or "").lower()
    implicit_action_patterns = (
        r"\bremind me\b",
        r"\bset (?:a )?reminder\b",
    )
    if any(re.search(pattern, text) for pattern in implicit_action_patterns):
        return True

    calendar_terms = (
        r"\bcalendar\b",
        r"\bschedule\b",
        r"\bevent\b",
        r"\breminder\b",
        r"\bcheck-?ins?\b",
    )
    action_terms = (
        r"\badd\b",
        r"\bcreate\b",
        r"\bcan you schedule\b",
        r"\bschedule\s+(?:a|an|the|this|that|my|it|me)\b",
        r"\bset up\b",
        r"\bput\b",
        r"\bbook\b",
        r"\bblock\b",
        r"\bremind me\b",
        r"\breschedule\b",
        r"\bmove\b",
        r"\bcancel\b",
        r"\bdelete\b",
        r"\bremove\b",
        r"\bbacktrack\b",
        r"\brecurr(?:ing|ence)?\b",
        r"\bevery\s+(mon|monday|tue|tuesday|wed|wednesday|thu|thursday|fri|friday|sat|saturday|sun|sunday|day|week)\b",
    )
    has_calendar_term = any(re.search(pattern, text) for pattern in calendar_terms)
    has_action_term = any(re.search(pattern, text) for pattern in action_terms)
    return has_calendar_term and has_action_term


def _strip_code_fences(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.split('\n')
        text = '\n'.join(lines[1:-1] if lines[-1] == '```' else lines[1:])
    return text.strip()


def _parse_json_from_text(raw: str):
    """
    Parse JSON robustly even if the model wraps it with prose or code fences.
    Returns dict/list or None.
    """
    text = _strip_code_fences(raw)
    candidates = [text]

    obj_start = text.find("{")
    obj_end = text.rfind("}")
    if obj_start != -1 and obj_end != -1 and obj_end > obj_start:
        candidates.append(text[obj_start:obj_end + 1])

    arr_start = text.find("[")
    arr_end = text.rfind("]")
    if arr_start != -1 and arr_end != -1 and arr_end > arr_start:
        candidates.append(text[arr_start:arr_end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _parse_recurrence_fallback(action_text: str):
    """
    Fallback parser for common recurring-schedule phrasing, e.g.
    "Mon/Wed/Fri at 10am".
    """
    text = (action_text or "").lower()
    if not is_calendar_action_request(text):
        return None

    weekday_map = {
        "monday": "MO", "mon": "MO",
        "tuesday": "TU", "tue": "TU", "tues": "TU",
        "wednesday": "WE", "wed": "WE",
        "thursday": "TH", "thu": "TH", "thurs": "TH",
        "friday": "FR", "fri": "FR",
        "saturday": "SA", "sat": "SA",
        "sunday": "SU", "sun": "SU",
    }
    found_days = []
    for token, code in weekday_map.items():
        if re.search(rf"\b{token}\b", text) and code not in found_days:
            found_days.append(code)

    if not found_days and "multiple times a week" in text:
        found_days = ["MO", "WE", "FR"]
    if not found_days:
        return None

    time_match = re.search(r"\bat\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        ampm = time_match.group(3)
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
    else:
        hour, minute = 10, 0

    title = "Trainerize client check-ins" if "trainerize" in text else "Recurring check-in"

    day_to_idx = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
    targets = [day_to_idx[d] for d in found_days]

    import pytz
    pt = pytz.timezone('America/Los_Angeles')
    now = datetime.now(pt)
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    for i in range(8):
        candidate = start + timedelta(days=i)
        if candidate.weekday() in targets and candidate > now:
            start = candidate
            break
    else:
        start = start + timedelta(days=7)

    end = start + timedelta(minutes=30)
    return {
        "type": "create_event",
        "title": title,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={','.join(found_days)}"],
    }


async def propose_calendar_action_for_approval(user_message: str) -> str:
    """Force a clear approval step before any calendar execution."""
    current_date = datetime.now().strftime('%Y-%m-%d')

    def call_api():
        return client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=220,
            system=f"""You are preparing a calendar action proposal for Kaz.
Never claim anything is already scheduled or done.

Output rules:
- Be concise and clear.
- Propose one specific calendar action based on his message.
- If recurrence is implied but vague, choose a reasonable default and state it clearly.
- Include concrete timing details.
- End with [APPROVAL_NEEDED] on its own line.
- Do NOT output JSON.

Today is {current_date} in America/Los_Angeles.""",
            messages=[{"role": "user", "content": user_message}]
        )

    response = await asyncio.to_thread(call_api)
    reply = response.content[0].text.strip()
    if "[APPROVAL_NEEDED]" not in reply:
        reply = f"{reply}\n\n[APPROVAL_NEEDED]"
    return reply


def find_latest_calendar_request(user_id: int) -> str:
    """Find the latest user message that looks like a calendar request."""
    history = conversations.get(user_id, [])
    for msg in reversed(history):
        if msg.get("role") != "user":
            continue
        content = (msg.get("content") or "").strip()
        if content and is_calendar_action_request(content):
            return content
    return ""


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        logger.warning("ALLOWED_USER_IDS is empty — denying all access. Set it in .env.")
        return False
    return user_id in ALLOWED_USER_IDS


def get_primary_user_id() -> int:
    raw = os.getenv('ALLOWED_USER_IDS', '0')
    first = raw.split(',')[0].strip()
    return int(first) if first.isdigit() else 0


async def safe_reply(update: Update, text: str, **kwargs):
    """Send a message, splitting at 4096 chars if needed."""
    limit = 4096
    for i in range(0, len(text), limit):
        await update.message.reply_text(text[i:i + limit], **kwargs)


async def route_and_save(text: str) -> tuple[str, str]:
    """
    Use Claude haiku to classify the note and route it to the right Obsidian file.
    Returns (relative_path, human_label).
    """
    destinations = get_available_destinations()
    dest_list = "\n".join(
        f"- {key}: {v['label']}" for key, v in destinations.items()
    )

    def classify():
        return client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            system=f"""You route notes to the correct file in an Obsidian vault.
Given a note, return ONLY the single best destination key from this list — nothing else.

{dest_list}

Rules:
- Mentions a client by name → that client's key
- Content idea, hook, post concept, caption, reel idea → content_ideas
- About mom Michiko's social media → mom_social
- About a specific project by name → that project's key
- Anything else → daily_note""",
            messages=[{"role": "user", "content": text}]
        )

    response = await asyncio.to_thread(classify)
    key = response.content[0].text.strip()
    dest = destinations.get(key, destinations["daily_note"])
    append_to_file(dest["path"], text)
    return dest["path"], dest["label"]


async def get_yuki_response(
    user_id: int,
    user_message: str,
    include_calendar: bool = True,
    include_gmail: bool = True,
    include_week: bool = False,
) -> str:
    if user_id not in conversations:
        conversations[user_id] = []

    cal_context = calendar.get_context_for_yuki(include_week=include_week) if include_calendar else ""
    gmail_context = ""
    if include_gmail and gmail.is_configured() and should_include_gmail_context(user_message):
        gmail_context = await asyncio.to_thread(gmail.get_context_for_yuki)
    obsidian_context = get_full_context()
    notion_context = ""
    if notion.is_configured():
        try:
            notion_context = await asyncio.to_thread(notion.get_relevant_context, user_message)
        except Exception as e:
            logger.error(f"Notion context error: {e}")
    memory = load_memory()
    system = YUKI_SYSTEM_PROMPT
    if memory:
        system += f"\n\n━━━ YUKI'S MEMORY ━━━\n{memory}"
    if cal_context:
        system += f"\n\n━━━ LIVE CALENDAR CONTEXT ━━━\n{cal_context}"
    if gmail_context:
        system += f"\n\n━━━ LIVE GMAIL CONTEXT ━━━\n{gmail_context}"
    if obsidian_context:
        system += f"\n\n━━━ KAZ'S OBSIDIAN NOTES ━━━\n{obsidian_context}"
    if notion_context:
        system += f"\n\n━━━ KAZ'S NOTION CLIENT NOTES ━━━\n{notion_context}"

    conversations[user_id].append({"role": "user", "content": user_message})

    # Keep last 10 messages to control API costs
    if len(conversations[user_id]) > 10:
        conversations[user_id] = conversations[user_id][-10:]

    messages_snapshot = list(conversations[user_id])

    def call_api():
        return client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=messages_snapshot
        )

    response = await asyncio.to_thread(call_api)
    reply = response.content[0].text
    conversations[user_id].append({"role": "assistant", "content": reply})
    _save_json(CONVERSATIONS_FILE, conversations)

    # Update memory every 2 messages in the background
    message_count[user_id] = message_count.get(user_id, 0) + 1
    if message_count[user_id] % 2 == 0:
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
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=f"""Extract the action from the message and return ONLY a JSON object.
No explanation, just JSON.

For calendar events return:
{{"type": "create_event", "title": "...", "start": "2026-02-25T10:00:00", "end": "2026-02-25T11:00:00"}}

For recurring calendar events return:
{{"type": "create_event", "title": "...", "start": "2026-02-26T10:00:00", "end": "2026-02-26T10:30:00", "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"]}}

For creating a Notion page return:
{{"type": "create_notion_page", "title": "...", "content": "..."}}

For sending an email return:
{{"type": "send_email", "to": "person@example.com", "subject": "...", "body": "..."}}

If it's not a calendar event or you can't extract it clearly return:
{{"type": "unknown"}}

Use ISO 8601 format for dates. Assume Pacific Time (America/Los_Angeles). Today is {current_date}.""",
            messages=[{"role": "user", "content": action_text}]
        )

    try:
        response = await asyncio.to_thread(extract)
        parsed = _parse_json_from_text(response.content[0].text)

        if parsed is None:
            fallback = _parse_recurrence_fallback(action_text)
            if fallback:
                parsed = fallback
            else:
                return "Action could not be parsed."

        items = []
        if isinstance(parsed, dict):
            items = [parsed]
        elif isinstance(parsed, list):
            items = [item for item in parsed if isinstance(item, dict)]
        if not items:
            fallback = _parse_recurrence_fallback(action_text)
            if fallback:
                items = [fallback]
            else:
                return "Action could not be parsed."

        results = []
        for data in items:
            if data.get("type") == "create_event":
                title = data.get("title", "").strip()
                start_iso = data.get("start", "").strip()
                end_iso = data.get("end", "").strip()

                recurrence = data.get("recurrence")
                if isinstance(recurrence, str):
                    recurrence = [recurrence]
                if recurrence is not None and not (
                    isinstance(recurrence, list) and all(isinstance(r, str) for r in recurrence)
                ):
                    recurrence = None

                if not title or not start_iso or not end_iso:
                    fallback = _parse_recurrence_fallback(action_text)
                    if fallback:
                        title = fallback["title"]
                        start_iso = fallback["start"]
                        end_iso = fallback["end"]
                        recurrence = fallback["recurrence"]
                    else:
                        results.append("Calendar event not created: missing title/start/end.")
                        continue

                link = calendar.create_event(
                    title=title,
                    start_iso=start_iso,
                    end_iso=end_iso,
                    recurrence=recurrence
                )
                if recurrence:
                    results.append(f"Recurring calendar event '{title}' created successfully: {link}")
                else:
                    results.append(f"Calendar event '{title}' created successfully: {link}")
                continue

            if data.get("type") == "create_notion_page":
                title = data.get("title", "").strip() or "Untitled"
                content = data.get("content", "").strip()
                url = notion.create_client_page(title=title, content=content)
                results.append(f"Notion page '{title}' created successfully: {url}")
                continue

            if data.get("type") == "send_email":
                to = data.get("to", "").strip()
                subject = data.get("subject", "").strip()
                body = data.get("body", "").strip()
                if not to or "@" not in to:
                    results.append("Email not sent: no valid recipient address detected.")
                    continue
                if not gmail.is_configured():
                    results.append("Email not sent: Gmail is not configured yet.")
                    continue
                message_id = gmail.send_email(to=to, subject=subject, body=body)
                results.append(f"Email sent to {to} (message id: {message_id})")
                continue

            results.append("Action type unknown.")

        return "\n".join(results) if results else "Action could not be parsed."
    except Exception as e:
        logger.error(f"Action execution error: {e}")
        return f"Action failed: {e}"


# ── RESOURCE SUGGESTION SYSTEM ─────────────────────────────────────────────────

async def generate_suggestions() -> list[dict]:
    """Generate 3 client resource suggestions based on active clients and existing resources."""
    if not notion.is_configured():
        return []
    clients = await asyncio.to_thread(notion.fetch_active_clients)
    existing = await asyncio.to_thread(notion.fetch_existing_resource_titles)
    if not clients:
        return []
    client_block = "\n".join(f"- {c['name']}: Goals: {c['goals']}. Notes: {c['notes']}" for c in clients)
    existing_block = "\n".join(f"- {t}" for t in existing) if existing else "None yet."

    def call_api():
        return client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system="You help a fitness coach decide what client resource to build next. Be specific and practical.",
            messages=[{"role": "user", "content": (
                f"Active clients:\n{client_block}\n\n"
                f"Resources already built (don't repeat):\n{existing_block}\n\n"
                "Suggest exactly 3 specific resource ideas based on what these clients actually need right now.\n\n"
                "Reply in this exact format — nothing else:\n"
                "1. [Title] — [one sentence: who it's for and why it's timely]\n"
                "2. [Title] — [one sentence: who it's for and why it's timely]\n"
                "3. [Title] — [one sentence: who it's for and why it's timely]"
            )}]
        )

    resp = await asyncio.to_thread(call_api)
    suggestions = []
    for line in resp.content[0].text.strip().splitlines():
        line = line.strip()
        if line and line[0].isdigit() and ". " in line:
            rest = line[line.index(". ") + 2:]
            title, desc = (rest.split(" — ", 1) + [""])[:2]
            suggestions.append({"title": title.strip(), "desc": desc.strip()})
    return suggestions[:3]


async def build_and_save_resource(suggestion: dict) -> str:
    """Generate a full resource and save it to Notion."""
    clients = await asyncio.to_thread(notion.fetch_active_clients)
    client_block = "\n".join(f"- {c['name']}: Goals: {c['goals']}. Notes: {c['notes']}" for c in clients)

    def call_api():
        return client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system="You write practical, shareable fitness coaching resources.",
            messages=[{"role": "user", "content": (
                f"Create a client resource titled \"{suggestion['title']}\".\n"
                f"Context: {suggestion['desc']}\n\n"
                f"Client profiles:\n{client_block}\n\n"
                "Write a complete, well-structured resource a coach can share directly with clients. "
                "Clear sections, actionable steps, no filler. Plain text."
            )}]
        )

    resp = await asyncio.to_thread(call_api)
    content = resp.content[0].text
    url = await asyncio.to_thread(notion.create_resource_page, suggestion["title"], content)
    return url


async def suggest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    await update.message.reply_text("Let me check your clients~ ✨")
    suggestions = await generate_suggestions()
    if not suggestions:
        await update.message.reply_text("Couldn't pull suggestions right now, sorry!")
        return
    pending_suggestions[user_id] = suggestions
    _save_json(PENDING_SUGGESTIONS_FILE, pending_suggestions)
    lines = ["Here are some resource ideas for your clients — pick one to build, or say skip!\n"]
    for i, s in enumerate(suggestions, 1):
        lines.append(f"{i}. {s['title']}\n   {s['desc']}")
    await update.message.reply_text("\n".join(lines))


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
    reply = await get_yuki_response(user_id, "Walk Kaz-kun through his week ahead!", include_week=True)
    await send_yuki_reply(update, reply, user_id)


async def inbox_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    if not gmail.is_configured():
        await update.message.reply_text(
            "Gmail isn't configured yet. Set GMAIL_CREDENTIALS_FILE (or GOOGLE_CALENDAR_CREDENTIALS) and restart Yuki."
        )
        return
    reply = await get_yuki_response(
        user_id,
        "Give Kaz-kun a concise summary of his unread Gmail messages and tell him what needs action first.",
        include_calendar=False,
        include_gmail=True
    )
    await send_yuki_reply(update, reply, user_id)


async def backtrack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    previous_request = find_latest_calendar_request(user_id)
    if not previous_request:
        await update.message.reply_text(
            "I couldn't find a previous calendar request to backtrack. "
            "Tell me what to schedule and I'll queue it for approval."
        )
        return

    proposal = await propose_calendar_action_for_approval(
        f"Backtrack and schedule this previous request now:\n{previous_request}"
    )
    await send_yuki_reply(update, proposal, user_id)


async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    text = ' '.join(context.args) if context.args else ''
    if text:
        _, label = await route_and_save(text)
        reply = await get_yuki_response(
            user_id,
            f"Kaz just sent a note and Yuki saved it to {label}: \"{text}\". Acknowledge briefly and warmly.",
            include_calendar=False
        )
        await send_yuki_reply(update, reply, user_id)
    else:
        note_mode[user_id] = True
        await update.message.reply_text("Ready! Send me what you want to add and Yuki will save it to the right place ✨")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    text = update.message.text

    # Handle pending resource choice (1/2/3/skip)
    if user_id in pending_suggestions:
        if text.strip().lower() in ("skip", "no", "pass", "none"):
            del pending_suggestions[user_id]
            _save_json(PENDING_SUGGESTIONS_FILE, pending_suggestions)
            reply = await get_yuki_response(user_id, "Kaz skipped the resource suggestions. Acknowledge briefly.", include_calendar=False)
            await safe_reply(update, reply)
            return
        if text.strip() in ("1", "2", "3"):
            idx = int(text.strip()) - 1
            suggestions = pending_suggestions.pop(user_id)
            _save_json(PENDING_SUGGESTIONS_FILE, pending_suggestions)
            if idx < len(suggestions):
                chosen = suggestions[idx]
                await update.message.reply_text(f"On it! Building \"{chosen['title']}\"~ ✨")
                try:
                    url = await asyncio.wait_for(build_and_save_resource(chosen), timeout=300)
                    reply = await get_yuki_response(
                        user_id,
                        f"Yuki just built and saved a client resource called \"{chosen['title']}\" to Notion. "
                        "Let Kaz know it's done in your warm style.",
                        include_calendar=False
                    )
                    await safe_reply(update, reply)
                    if url:
                        await update.message.reply_text(url)
                except Exception as e:
                    await safe_reply(update, f"Something went wrong building it~ {e}")
                return

    # If user is editing a pending action, re-propose with their changes
    if user_id in pending_actions:
        original = pending_actions.pop(user_id)
        reply = await get_yuki_response(
            user_id,
            f"Kaz wants to modify this action:\n\nOriginal: {original}\n\nHis edit request: {text}\n\n"
            "Re-propose the updated action with [APPROVAL_NEEDED] so he can approve the new version.",
        )
        await send_yuki_reply(update, reply, user_id)
        return

    # If user is in note mode, save to Obsidian then respond
    if note_mode.pop(user_id, False):
        _, label = await route_and_save(text)
        reply = await get_yuki_response(
            user_id,
            f"Kaz just shared this and Yuki saved it to {label}: \"{text}\". "
            "Acknowledge it warmly, mention where it was saved, maybe reflect on what he shared, and respond naturally.",
        )
        await send_yuki_reply(update, reply, user_id)
        return

    # Force approval-first workflow for calendar requests to avoid false "scheduled" claims
    if is_calendar_action_request(text) and not is_calendar_read_request(text):
        proposal = await propose_calendar_action_for_approval(text)
        await send_yuki_reply(update, proposal, user_id)
        return

    include_week = should_include_week_calendar_context(text)
    reply = await get_yuki_response(user_id, text, include_week=include_week)
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

    include_week = should_include_week_calendar_context(text)
    reply = await get_yuki_response(user_id, text, include_week=include_week)
    await send_yuki_reply(update, reply, user_id)


async def send_proactive(context: ContextTypes.DEFAULT_TYPE, prompt: str):
    """Send a scheduled message to Kaz."""
    user_id = get_primary_user_id()
    if not user_id:
        return
    try:
        reply = await get_yuki_response(user_id, prompt)
        for i in range(0, len(reply), 4096):
            await context.bot.send_message(chat_id=user_id, text=reply[i:i + 4096])
    except Exception as e:
        logger.error(f"Proactive message error: {e}")


async def morning_briefing(context: ContextTypes.DEFAULT_TYPE):
    import pytz
    pt = pytz.timezone('America/Los_Angeles')
    is_suggestion_day = datetime.now(pt).weekday() in SUGGESTION_DAYS

    if is_suggestion_day and notion.is_configured():
        suggestions = await generate_suggestions()
        user_id = get_primary_user_id()
        if suggestions and user_id:
            pending_suggestions[user_id] = suggestions
            _save_json(PENDING_SUGGESTIONS_FILE, pending_suggestions)
            suggestion_block = "\n".join(
                f"{i}. {s['title']} — {s['desc']}"
                for i, s in enumerate(suggestions, 1)
            )
            await send_proactive(context,
                "Good morning Kaz-kun! Give him his morning briefing: "
                "what's on his calendar today, anything notable from his Obsidian notes, "
                "and one content idea based on what he's been writing about lately. "
                "Then tell him you've got 3 client resource ideas for him to pick from — "
                "present them naturally as part of the message, numbered 1-3. "
                f"Here are the ideas:\n{suggestion_block}\n"
                "Tell him to reply with 1, 2, or 3 to build one out, or skip. Keep it all warm and energizing."
            )
            return

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


async def client_resource_suggestion(context: ContextTypes.DEFAULT_TYPE):
    """Three times a week, suggest a useful resource for one client."""
    import pytz
    pt = pytz.timezone('America/Los_Angeles')
    # Monday, Wednesday, Friday
    if datetime.now(pt).weekday() not in {0, 2, 4}:
        return

    await send_proactive(
        context,
        "Suggest one helpful resource for one client this week. "
        "Use what you know from Obsidian/Notion client notes if available. "
        "Be specific: include who it's for, what the resource is, and why it helps now. "
        "Keep it concise and actionable."
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
    user_id = get_primary_user_id()
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
        for i in range(0, len(reply), 4096):
            await context.bot.send_message(chat_id=user_id, text=reply[i:i + 4096])
    except Exception as e:
        logger.error(f"Note nudge error: {e}")


async def pre_event_checkin(context: ContextTypes.DEFAULT_TYPE):
    """15 minutes before an event — brief prep nudge."""
    event = context.job.data
    user_id = get_primary_user_id()
    if not user_id:
        return
    try:
        reply = await get_yuki_response(
            user_id,
            f"Kaz has '{event['title']}' starting in 15 minutes at {event['start_str']}. "
            "Give him a warm, brief heads-up. If you know anything about this event or the people "
            "involved from his notes or memory, mention it in one sentence. 2-3 sentences max.",
            include_calendar=False
        )
        for i in range(0, len(reply), 4096):
            await context.bot.send_message(chat_id=user_id, text=reply[i:i + 4096])
    except Exception as e:
        logger.error(f"Pre-event check-in error: {e}")


async def post_event_checkin(context: ContextTypes.DEFAULT_TYPE):
    """30 minutes after an event ends — debrief + auto-save reply to notes."""
    event = context.job.data
    user_id = get_primary_user_id()
    if not user_id:
        return
    note_mode[user_id] = True
    try:
        reply = await get_yuki_response(
            user_id,
            f"'{event['title']}' just finished. Ask Kaz warmly how it went — "
            "brief and natural, like a friend checking in. "
            "Let him know his reply will be saved to his notes automatically.",
            include_calendar=False
        )
        for i in range(0, len(reply), 4096):
            await context.bot.send_message(chat_id=user_id, text=reply[i:i + 4096])
    except Exception as e:
        logger.error(f"Post-event check-in error: {e}")


async def schedule_event_checkins(context: ContextTypes.DEFAULT_TYPE):
    """Scan today's calendar and schedule pre/post check-ins for each timed event."""
    import pytz
    pt = pytz.timezone('America/Los_Angeles')
    now = datetime.now(pt)

    try:
        events = calendar.get_today_events()
    except Exception as e:
        logger.error(f"Failed to fetch events for scheduling: {e}")
        return

    scheduled = 0
    for event in events:
        start_str = event['start'].get('dateTime')
        end_str = event['end'].get('dateTime')

        # Skip all-day events
        if not start_str or not end_str:
            continue

        title = event.get('summary', 'an event')
        start_dt = datetime.fromisoformat(start_str).astimezone(pt)
        end_dt = datetime.fromisoformat(end_str).astimezone(pt)

        event_data = {
            'title': title,
            'start_str': start_dt.strftime('%-I:%M %p'),
            'end_str': end_dt.strftime('%-I:%M %p'),
        }

        pre_time = start_dt - timedelta(minutes=15)
        post_time = end_dt + timedelta(minutes=30)

        if pre_time > now:
            context.job_queue.run_once(
                pre_event_checkin,
                when=pre_time,
                data=event_data,
                name=f"pre|{title}|{start_dt.date()}"
            )
            scheduled += 1

        if post_time > now:
            context.job_queue.run_once(
                post_event_checkin,
                when=post_time,
                data=event_data,
                name=f"post|{title}|{end_dt.date()}"
            )
            scheduled += 1

    logger.info(f"Scheduled {scheduled} event check-in jobs for today")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Callback acknowledge failed: {e}")
    user_id = query.from_user.id
    if not is_allowed(user_id):
        return
    action = query.data

    pending = pending_actions.pop(user_id, "")

    if action == "approve":
        if not pending:
            await query.edit_message_text("Nothing pending to approve~")
            return
        result = await execute_approved_action(pending)
        reply = await get_yuki_response(
            user_id,
            f"Kaz approved! You executed this action: {result}. Confirm cutely.",
            include_calendar=False
        )
        await query.edit_message_text(reply)
    elif action == "edit":
        # Re-store so the next message can reference and modify it
        pending_actions[user_id] = pending
        await query.edit_message_text(
            "Of course~! ✏️ Tell Yuki what to change, and she'll re-propose it! ♡"
        )
    elif action == "skip":
        reply = await get_yuki_response(user_id, "Kaz skipped this one. Acknowledge briefly and sweetly.", include_calendar=False)
        await query.edit_message_text(reply)


async def post_init(application):
    """On startup: if there's a saved conversation, update memory from it."""
    user_id = get_primary_user_id()
    if user_id and user_id in conversations and len(conversations[user_id]) >= 2:
        logger.info("Startup: updating memory from saved conversation history")
        asyncio.create_task(update_memory(conversations[user_id]))


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("week", week_command))
    app.add_handler(CommandHandler("inbox", inbox_command))
    app.add_handler(CommandHandler("backtrack", backtrack_command))
    app.add_handler(CommandHandler("note", note_command))
    app.add_handler(CommandHandler("suggest", suggest_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Scheduled proactive messages (Pacific Time)
    import pytz
    pt = pytz.timezone('America/Los_Angeles')
    jq = app.job_queue
    jq.run_daily(morning_briefing,       time=dtime(9,  0,  tzinfo=pt))
    jq.run_daily(client_resource_suggestion, time=dtime(11, 0, tzinfo=pt))
    jq.run_daily(midday_checkin,         time=dtime(13, 0,  tzinfo=pt))
    jq.run_daily(note_nudge,             time=dtime(14, 0,  tzinfo=pt))
    jq.run_daily(evening_wrapup,         time=dtime(19, 0,  tzinfo=pt))
    # Refresh event check-ins every midnight, and once 10s after startup
    jq.run_daily(schedule_event_checkins, time=dtime(0, 1, tzinfo=pt))
    jq.run_once(schedule_event_checkins, when=10)

    print("✨ Yuki is awake and ready for Kaz-kun! ✨")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
