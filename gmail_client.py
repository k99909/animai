import os
import base64
from pathlib import Path
from datetime import datetime
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send',
]
BASE_DIR = Path(__file__).parent

# Default to the same OAuth client file used for calendar unless overridden.
CREDENTIALS_FILE = os.getenv(
    'GMAIL_CREDENTIALS_FILE',
    os.getenv('GOOGLE_CALENDAR_CREDENTIALS', str(BASE_DIR / 'google-calendar-credentials.json'))
)
TOKEN_FILE = os.getenv('GMAIL_TOKEN_FILE', str(BASE_DIR / 'gmail_token.json'))


class GmailClient:
    def __init__(self):
        self.service = None

    def _ensure_auth(self):
        if self.service is None:
            self._authenticate()

    def _authenticate(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, 'w') as f:
                f.write(creds.to_json())

        self.service = build('gmail', 'v1', credentials=creds)

    def is_configured(self) -> bool:
        return bool(CREDENTIALS_FILE and os.path.exists(CREDENTIALS_FILE))

    @staticmethod
    def _header_value(message: dict, name: str) -> str:
        headers = message.get('payload', {}).get('headers', [])
        for header in headers:
            if header.get('name', '').lower() == name.lower():
                return header.get('value', '')
        return ''

    def get_recent_unread_messages(self, max_results: int = 5) -> list[dict]:
        self._ensure_auth()
        result = self.service.users().messages().list(
            userId='me',
            q='is:unread',
            maxResults=max_results
        ).execute()
        message_refs = result.get('messages', [])
        messages = []

        for ref in message_refs:
            full = self.service.users().messages().get(
                userId='me',
                id=ref['id'],
                format='metadata',
                metadataHeaders=['From', 'Subject', 'Date']
            ).execute()
            messages.append({
                'from': self._header_value(full, 'From') or '(unknown sender)',
                'subject': self._header_value(full, 'Subject') or '(no subject)',
                'date': self._header_value(full, 'Date') or '',
                'snippet': (full.get('snippet') or '').strip(),
            })

        return messages

    def get_context_for_yuki(self, max_results: int = 5) -> str:
        try:
            now = datetime.now().strftime('%A, %B %d, %Y at %-I:%M %p')
            unread = self.get_recent_unread_messages(max_results=max_results)
            if not unread:
                return f"Current time: {now}\nUnread Gmail: none."

            lines = []
            for item in unread:
                line = f"• From: {item['from']}\n  Subject: {item['subject']}"
                if item['snippet']:
                    line += f"\n  Snippet: {item['snippet'][:160]}"
                lines.append(line)

            return (
                f"Current time: {now}\n"
                f"Unread Gmail messages ({len(unread)}):\n" + "\n".join(lines)
            )
        except Exception as e:
            return f"Gmail temporarily unavailable ({str(e)})"

    def send_email(self, to: str, subject: str, body: str) -> str:
        self._ensure_auth()
        mime_message = MIMEText(body or "")
        mime_message['to'] = to
        mime_message['subject'] = subject
        encoded = base64.urlsafe_b64encode(mime_message.as_bytes()).decode()

        sent = self.service.users().messages().send(
            userId='me',
            body={'raw': encoded}
        ).execute()
        return sent.get('id', 'sent')
