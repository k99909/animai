import os
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

VAULT_PATH = Path(os.getenv('OBSIDIAN_VAULT_PATH', '/Users/kazukidebear/Desktop/Obsidian'))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8').strip()
    except Exception:
        return ''


def get_daily_note(date: datetime = None) -> str:
    if date is None:
        date = datetime.now()
    filename = date.strftime('%Y-%m-%d') + '.md'
    content = _read(VAULT_PATH / 'Daily Notes' / filename)
    return content if content else ''


def get_recent_daily_notes(days: int = 3) -> str:
    sections = []
    for i in range(1, days + 1):
        date = datetime.now() - timedelta(days=i)
        content = get_daily_note(date)
        if content:
            sections.append(f"### {date.strftime('%A %b %d')}\n{content}")
    return '\n\n'.join(sections)


def get_content_context() -> str:
    pillars = _read(VAULT_PATH / 'Projects' / 'My Social Media' / 'Content Pillars.md')
    ideas = _read(VAULT_PATH / 'Projects' / 'My Social Media' / 'Content Ideas.md')
    parts = []
    if pillars:
        parts.append(f"**Content Pillars:**\n{pillars}")
    if ideas:
        parts.append(f"**Content Ideas (logged):**\n{ideas}")
    return '\n\n'.join(parts)


def get_client_context() -> str:
    clients_dir = VAULT_PATH / 'Projects' / 'Clients'
    if not clients_dir.exists():
        return ''
    lines = []
    for f in sorted(clients_dir.glob('*.md')):
        content = _read(f)
        if content:
            lines.append(f"### {f.stem}\n{content}")
    return '\n\n'.join(lines)


def get_mom_context() -> str:
    return _read(VAULT_PATH / 'Projects' / "Mom's Social Media" / 'Context.md')


def has_written_today() -> bool:
    """Returns True if today's daily note has meaningful content."""
    content = get_daily_note()
    # Ignore empty files or bare templates (less than 100 chars of real content)
    stripped = '\n'.join(
        line for line in content.splitlines()
        if line.strip() and not line.strip().startswith('#')
    )
    return len(stripped) > 100


def append_to_daily_note(text: str) -> str:
    """Append text to today's daily note, creating it if needed. Returns the file path."""
    today = datetime.now()
    filename = today.strftime('%Y-%m-%d') + '.md'
    note_path = VAULT_PATH / 'Daily Notes' / filename
    note_path.parent.mkdir(parents=True, exist_ok=True)

    timestamp = today.strftime('%-I:%M %p')
    entry = f"\n\n---\n*Added via Yuki at {timestamp}*\n{text.strip()}"

    if note_path.exists():
        with open(note_path, 'a', encoding='utf-8') as f:
            f.write(entry)
    else:
        # Create a minimal daily note with the entry
        header = f"# {today.strftime('%A, %B %d, %Y')}\n"
        with open(note_path, 'w', encoding='utf-8') as f:
            f.write(header + entry)

    return str(note_path)


def get_full_context() -> str:
    parts = []

    today = get_daily_note()
    if today:
        parts.append(f"━━━ TODAY'S DAILY NOTE ━━━\n{today}")

    recent = get_recent_daily_notes(days=3)
    if recent:
        parts.append(f"━━━ RECENT NOTES (last 3 days) ━━━\n{recent}")

    content = get_content_context()
    if content:
        parts.append(f"━━━ CONTENT STRATEGY ━━━\n{content}")

    clients = get_client_context()
    if clients:
        parts.append(f"━━━ CLIENT NOTES ━━━\n{clients}")

    mom = get_mom_context()
    if mom:
        parts.append(f"━━━ MOM'S SOCIAL MEDIA ━━━\n{mom}")

    return '\n\n'.join(parts)
