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
