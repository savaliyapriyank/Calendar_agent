import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events"
]

creds = None

# 1. Check if saved login exists
if os.path.exists("token.json"):
    creds = Credentials.from_authorized_user_file(
        "token.json",
        SCOPES
    )

# 2. If token is expired, refresh it
if creds and creds.expired and creds.refresh_token:
    creds.refresh(Request())

# 3. If no valid login exists, ask Google to login
if not creds or not creds.valid:
    flow = InstalledAppFlow.from_client_secrets_file(
        "credentials.json",
        SCOPES
    )

    creds = flow.run_local_server(port=0)

    # Save login for future runs
    with open("token.json", "w") as token:
        token.write(creds.to_json())

# 4. Connect to Google Calendar
service = build(
    "calendar",
    "v3",
    credentials=creds
)

print("Google Calendar connected!")