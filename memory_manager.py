import os
import asyncio
from datetime import datetime
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

MEMORY_FILE = '/Users/kazukidebear/anime-assistant/memory.md'
client = Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))


def load_memory() -> str:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'r') as f:
            return f.read()
    return ""


def save_memory(content: str):
    with open(MEMORY_FILE, 'w') as f:
        f.write(content)


async def update_memory(conversation_history: list):
    if len(conversation_history) < 2:
        return

    current_memory = load_memory()
    today = datetime.now().strftime('%Y-%m-%d %I:%M %p')

    recent = conversation_history[-10:]
    convo_text = "\n".join([
        f"{'Kaz' if m['role'] == 'user' else 'Yuki'}: {m['content']}"
        for m in recent
    ])

    def call_api():
        return client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system="""You are Yuki, a kawaii anime assistant. Your job is to update your memory file
based on a recent conversation with Kaz-kun.

Update the memory markdown file by:
1. Adding a brief summary under "Recent Conversations" with today's date
2. Updating "Things to Follow Up On" with anything Kaz mentioned he's working on or needs
3. Updating "Patterns & Observations" if you notice anything about his mood, habits, or themes
4. Updating "Kaz's Current Focus" based on what he's talking about most

Keep it concise. Write in first person as Yuki. Preserve all existing memory — only add/update, never delete important info.
Return the COMPLETE updated memory file.""",
            messages=[{
                "role": "user",
                "content": f"Current memory:\n{current_memory}\n\nRecent conversation ({today}):\n{convo_text}\n\nPlease update my memory file."
            }]
        )

    response = await asyncio.to_thread(call_api)
    updated = response.content[0].text.strip()

    # Strip markdown code fences if Claude wrapped it
    if updated.startswith("```"):
        lines = updated.split('\n')
        updated = '\n'.join(lines[1:-1] if lines[-1] == '```' else lines[1:])

    save_memory(updated)
