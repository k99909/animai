import os
from pathlib import Path
from datetime import datetime, timedelta
import pytz
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

SCOPES = ['https://www.googleapis.com/auth/calendar']
BASE_DIR = Path(__file__).parent
CREDENTIALS_FILE = os.getenv('GOOGLE_CALENDAR_CREDENTIALS', str(BASE_DIR / 'google-calendar-credentials.json'))
TOKEN_FILE = os.getenv('GOOGLE_CALENDAR_TOKEN', str(BASE_DIR / 'calendar_token.json'))
TZ = pytz.timezone(os.getenv('TIMEZONE', 'America/Los_Angeles'))


class CalendarClient:
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

        self.service = build('calendar', 'v3', credentials=creds)

    def _get_events(self, days_ahead=0, days_range=1, calendar_id='primary'):
        self._ensure_auth()
        now = datetime.now(TZ)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
        end = start + timedelta(days=days_range)

        result = self.service.events().list(
            calendarId=calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        return result.get('items', [])

    def get_today_events(self):
        return self._get_events(days_ahead=0, days_range=1)

    def get_week_events(self):
        return self._get_events(days_ahead=0, days_range=7)

    def format_events_for_yuki(self, events):
        if not events:
            return "nothing scheduled"

        lines = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            if 'T' in start:
                dt = datetime.fromisoformat(start)
                if dt.tzinfo is None:
                    dt = TZ.localize(dt)
                dt = dt.astimezone(TZ)
                time_str = dt.strftime('%-I:%M %p')
            else:
                time_str = 'all day'

            title = event.get('summary', 'Untitled event')
            lines.append(f"• {time_str} — {title}")

        return '\n'.join(lines)

    def create_event(self, title: str, start_iso: str, end_iso: str, description: str = "") -> str:
        self._ensure_auth()
        event = {
            'summary': title,
            'description': description,
            'start': {'dateTime': start_iso, 'timeZone': 'America/Los_Angeles'},
            'end': {'dateTime': end_iso, 'timeZone': 'America/Los_Angeles'},
        }
        created = self.service.events().insert(calendarId='primary', body=event).execute()
        return created.get('htmlLink', 'created!')

    def get_context_for_yuki(self, include_week=False):
        try:
            now = datetime.now(TZ)
            today_events = self.get_today_events()
            today_str = self.format_events_for_yuki(today_events)

            context = (
                f"Current time: {now.strftime('%A, %B %d, %Y at %-I:%M %p')} (Pacific)\n"
                f"Today's schedule:\n{today_str}"
            )

            if include_week:
                week_events = self.get_week_events()
                # Group events by date
                days = {}
                for event in week_events:
                    start = event['start'].get('dateTime', event['start'].get('date'))
                    date_key = start[:10]
                    days.setdefault(date_key, []).append(event)

                week_lines = []
                for date_key in sorted(days):
                    dt = datetime.fromisoformat(date_key)
                    day_label = dt.strftime('%A, %B %d')
                    day_events = self.format_events_for_yuki(days[date_key])
                    week_lines.append(f"\n{day_label}:\n{day_events}")

                context += "\n\n━━━ WEEK AHEAD ━━━" + "".join(week_lines)

            return context
        except Exception as e:
            return f"Calendar temporarily unavailable ({str(e)})"
