import asyncio
import os
import sys
import re
from typing import Any, Annotated, TypedDict
from contextlib import contextmanager
import json
from dotenv import load_dotenv
from mcp import Client, StdioServerParameters
from datetime import datetime
from pydantic import Field, create_model
from langchain_openai import ChatOpenAI
from langchain_core.tools import StructuredTool
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

load_dotenv()

@contextmanager
def silence_stderr():
    """Silences stderr at the OS file descriptor level to suppress noisy subprocess banners."""
    try:
        old_stderr = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
        os.close(devnull)
        try:
            yield
        finally:
            os.dup2(old_stderr, 2)
            os.close(old_stderr)
    except Exception:
        yield

required_variables = [
    "GOOGLE_CALENDAR_CLIENT_ID",
    "GOOGLE_CALENDAR_CLIENT_SECRET",
    "GOOGLE_CALENDAR_REFRESH_TOKEN",
]

optional_gmail_variables = [
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_REFRESH_TOKEN",
]

for variable in required_variables:
    if not os.getenv(variable):
        raise ValueError(f"{variable} is missing in .env")

if not os.getenv("GROQ_API_KEY") and not os.getenv("OPENAI_API_KEY"):
    raise ValueError("Neither GROQ_API_KEY nor OPENAI_API_KEY is found in .env. Please set GROQ_API_KEY.")

gmail_configured = all(os.getenv(var) for var in optional_gmail_variables)


def get_chat_model():
    """Initializes LLM: uses GROQ_API_KEY if present (free), otherwise OPENAI_API_KEY."""
    groq_api_key = os.getenv("GROQ_API_KEY")
    openai_api_key = os.getenv("OPENAI_API_KEY")

    if groq_api_key:
        groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
        print(f"🧠 Using Groq LLM ({groq_model})...")
        return ChatOpenAI(
            model=groq_model,
            api_key=groq_api_key,
            base_url="https://api.groq.com/openai/v1",
            temperature=0,
        )
    elif openai_api_key:
        print("🧠 Using OpenAI LLM (gpt-4o-mini)...")
        return ChatOpenAI(
            model="gpt-4o-mini",
            api_key=openai_api_key,
            temperature=0,
        )
    else:
        raise ValueError("Neither GROQ_API_KEY nor OPENAI_API_KEY is set in .env")

gmail_client_instance = None
_google_cal_service = None
last_event_context: dict[str, Any] = {}

def get_calendar_service():
    """Initializes Google Calendar API v3 service with OAuth refresh token for Google Meet generation."""
    global _google_cal_service
    if _google_cal_service:
        return _google_cal_service
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        
        client_id = os.getenv("GOOGLE_CALENDAR_CLIENT_ID")
        client_secret = os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET")
        refresh_token = os.getenv("GOOGLE_CALENDAR_REFRESH_TOKEN")
        
        if client_id and client_secret and refresh_token:
            creds = Credentials(
                None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret
            )
            _google_cal_service = build("calendar", "v3", credentials=creds)
            return _google_cal_service
    except Exception as e:
        print(f"⚠️ Could not initialize Google Calendar service for Meet: {e}")
    return None

class CalendarState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]



def json_type_to_python(schema: dict[str, Any]):
    schema_type = schema.get("type")

    if schema_type == "string":
        return str
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "array":
        return list[Any]
    if schema_type == "object":
        return dict[str, Any]

    return Any


def create_args_schema(mcp_tool):

    schema = mcp_tool.input_schema

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    fields = {}

    for name, field_schema in properties.items():

        python_type = json_type_to_python(field_schema)
        description = field_schema.get("description", name)

        if name in required:
            fields[name] = (
                python_type,
                Field(..., description=description)
            )
        else:
            fields[name] = (
                python_type | None,
                Field(None, description=description)
            )

    model = create_model(
        f"{mcp_tool.name}Input",
        **fields
    )

    return model

def fix_iso_date(dt_str: str) -> str:
    """Ensures an ISO 8601 date string includes time and local timezone offset for timed events."""
    if not dt_str or not isinstance(dt_str, str):
        return dt_str
    s = dt_str.strip()
    if "T" not in s:
        return s  # All-day format: YYYY-MM-DD
    # Add seconds if missing (e.g. 2026-10-03T14:00 -> 2026-10-03T14:00:00)
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", s):
        s += ":00"
    # Check if timezone is missing
    tz_match = re.search(r"([+-]\d{2}:\d{2}|Z)$", s)
    if not tz_match:
        local_tz = datetime.now().astimezone().strftime("%z")
        offset = f"{local_tz[:3]}:{local_tz[3:]}" if len(local_tz) == 5 else "+00:00"
        s += offset
    return s


def build_professional_invite_html(
    title: str,
    start_str: str = "",
    end_str: str = "",
    meet_link: str | None = None,
    organizer: str | None = None,
    attendees: list[str] | None = None,
    message_note: str | None = None,
) -> str:
    """Builds a company-grade, responsive HTML invitation email matching Google Calendar style."""
    time_short = "Upcoming Meeting"
    time_long = "Scheduled Event"

    if start_str:
        try:
            s_clean = start_str.replace("Z", "+00:00")
            s_dt = datetime.fromisoformat(s_clean)
            e_dt = None
            if end_str:
                e_clean = end_str.replace("Z", "+00:00")
                e_dt = datetime.fromisoformat(e_clean)

            day_short = s_dt.strftime("%a %-d %b · %H:%M")
            if e_dt:
                time_short = f"{day_short} – {e_dt.strftime('%H:%M')} (IST)"
                time_long = f"{s_dt.strftime('%A %b %-d, %Y · %-I:%M%p')} – {e_dt.strftime('%-I:%M%p')} (India Standard Time - Kolkata)"
            else:
                time_short = f"{day_short} (IST)"
                time_long = f"{s_dt.strftime('%A %b %-d, %Y · %-I:%M%p')} (India Standard Time - Kolkata)"
        except Exception:
            time_short = start_str
            time_long = f"{start_str} to {end_str}" if end_str else start_str

    org_email = organizer or os.getenv("GMAIL_SENDER_EMAIL", "tulsisavani02@gmail.com")

    # Build guest list HTML
    guest_items = [f'<div style="margin: 2px 0;">{org_email} – <span style="color: #5f6368;">organizer</span></div>']
    if attendees:
        for a in attendees:
            if a and a != org_email:
                guest_items.append(f'<div style="margin: 2px 0;">{a}</div>')
    guests_html = "".join(guest_items)

    # Google Meet Block (included ONLY if meet_link is present for meeting/interview)
    meet_block = ""
    if meet_link:
        meet_clean = meet_link.replace("https://", "").replace("http://", "")
        meet_block = f"""
        <div style="background-color: #f8f9fa; border: 1px solid #e8eaed; border-radius: 8px; padding: 18px 20px; margin: 20px 0; text-align: center;">
          <a href="{meet_link}" target="_blank" style="background-color: #1a73e8; color: #ffffff; padding: 10px 26px; text-decoration: none; border-radius: 4px; font-weight: 500; font-size: 14px; display: inline-block; letter-spacing: 0.25px; box-shadow: 0 1px 2px rgba(0,0,0,0.15);">
            Join with Google Meet
          </a>
          <div style="margin-top: 10px; font-size: 13px; color: #5f6368;">
            Meeting link: <a href="{meet_link}" target="_blank" style="color: #1a73e8; text-decoration: none; font-weight: 500;">{meet_clean}</a>
          </div>
        </div>
        """

    # Note / message block from user
    note_block = ""
    if message_note and message_note.strip():
        note_clean = message_note.strip()
        if meet_link and meet_link in note_clean:
            note_clean = note_clean.replace(f"Join with Google Meet: {meet_link}", "").replace(meet_link, "").strip()
        if note_clean:
            note_formatted = note_clean.replace("\n", "<br>")
            note_block = f"""
            <div style="background-color: #f1f3f4; border-left: 4px solid #1a73e8; border-radius: 0 4px 4px 0; padding: 12px 16px; margin: 18px 0;">
              <div style="font-size: 11px; text-transform: uppercase; color: #5f6368; font-weight: 600; margin-bottom: 4px; letter-spacing: 0.5px;">Message from Organizer</div>
              <div style="font-size: 14px; color: #202124; line-height: 1.5;">{note_formatted}</div>
            </div>
            """

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
</head>
<body style="margin: 0; padding: 20px 10px; background-color: #f1f3f4; font-family: 'Google Sans', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
  <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border: 1px solid #dadce0; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(60,64,67,0.12);">
    
    <!-- Top Card Header -->
    <div style="padding: 24px 24px 16px 24px;">
      <div style="font-size: 13px; color: #5f6368; font-weight: 500; margin-bottom: 6px;">{time_short}</div>
      <h1 style="font-size: 22px; font-weight: 600; color: #202124; margin: 0 0 10px 0; line-height: 1.3;">{title}</h1>
      <div style="font-size: 13px; color: #5f6368; margin-bottom: 16px;">
        <span>{org_email} – Organiser</span>
      </div>

      <!-- Quick RSVP Buttons -->
      <div style="display: flex; gap: 8px; margin-top: 14px; margin-bottom: 4px;">
        <span style="display: inline-block; padding: 6px 18px; border: 1px solid #dadce0; border-radius: 16px; font-size: 13px; font-weight: 500; color: #1a73e8; background-color: #ffffff; text-decoration: none;">Yes</span>
        <span style="display: inline-block; padding: 6px 18px; border: 1px solid #dadce0; border-radius: 16px; font-size: 13px; font-weight: 500; color: #5f6368; background-color: #ffffff; text-decoration: none;">Maybe</span>
        <span style="display: inline-block; padding: 6px 18px; border: 1px solid #dadce0; border-radius: 16px; font-size: 13px; font-weight: 500; color: #5f6368; background-color: #ffffff; text-decoration: none;">No</span>
      </div>
    </div>

    <!-- Divider -->
    <div style="height: 1px; background-color: #e8eaed; margin: 0 24px;"></div>

    <!-- Middle Section: Google Meet Button (if meeting) -->
    <div style="padding: 16px 24px 24px 24px;">
      {meet_block}
      {note_block}

      <!-- Detailed Info Table -->
      <table style="width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 14px; color: #202124;">
        <tr>
          <td style="width: 75px; vertical-align: top; padding: 8px 0; color: #5f6368; font-weight: 500;">When</td>
          <td style="vertical-align: top; padding: 8px 0; color: #202124;">{time_long}</td>
        </tr>
        <tr>
          <td style="width: 75px; vertical-align: top; padding: 8px 0; color: #5f6368; font-weight: 500;">Guests</td>
          <td style="vertical-align: top; padding: 8px 0; color: #202124; line-height: 1.5;">{guests_html}</td>
        </tr>
      </table>

      <!-- RSVP Footer Pill Row -->
      <div style="margin-top: 24px; padding-top: 16px; border-top: 1px solid #f1f3f4; font-size: 13px; color: #5f6368;">
        <span style="margin-right: 12px; font-weight: 500;">Going?</span>
        <span style="display: inline-block; padding: 4px 12px; border: 1px solid #dadce0; border-radius: 12px; font-size: 12px; font-weight: 500; color: #1a73e8; margin-right: 6px;">Yes</span>
        <span style="display: inline-block; padding: 4px 12px; border: 1px solid #dadce0; border-radius: 12px; font-size: 12px; font-weight: 500; color: #5f6368; margin-right: 6px;">Maybe</span>
        <span style="display: inline-block; padding: 4px 12px; border: 1px solid #dadce0; border-radius: 12px; font-size: 12px; font-weight: 500; color: #5f6368;">No</span>
      </div>
    </div>

    <!-- Micro Footer -->
    <div style="background-color: #fafafa; border-top: 1px solid #e8eaed; padding: 12px 24px; font-size: 11px; color: #80868b; text-align: center;">
      Invitation sent by Mindlytic AI Calendar Assistant
    </div>
  </div>
</body>
</html>"""


def create_langchain_tool(mcp_client, mcp_tool, require_terminal_confirm: bool = True):

    args_schema = create_args_schema(mcp_tool)

    write_tools = {
        "calendar_create_event",
        "calendar_update_event",
        "calendar_delete_event",
    }

    async def send_reminder_email(to_emails: str, event_title: str, event_start: str, event_description: str = ""):
        global gmail_client_instance

        if not gmail_client_instance:
            print("⚠️  Gmail not available. Skipping email send.")
            return False

        try:
            from_email = os.getenv("GMAIL_SENDER_EMAIL", "savaliyapriyank19@gmail.com")

            email_list = [e.strip() for e in to_emails.split(",") if e.strip()]

            html_body = f"""
            <html>
                <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                    <div style="max-width: 600px; margin: 0 auto; border: 1px solid #ddd; border-radius: 8px; padding: 20px; background-color: #f9f9f9;">
                        <h2 style="color: #0066cc; margin-top: 0;">📅 Event Reminder</h2>

                        <div style="background-color: white; padding: 15px; border-radius: 5px; margin: 15px 0;">
                            <p style="margin: 10px 0;"><strong>Event:</strong> {event_title}</p>
                            <p style="margin: 10px 0;"><strong>Time:</strong> {event_start}</p>
                            {f'<p style="margin: 10px 0;"><strong>Description:</strong> {event_description}</p>' if event_description else ''}
                        </div>

                        <div style="background-color: #f0f8ff; padding: 12px; border-left: 4px solid #0066cc; margin: 15px 0;">
                            <p style="margin: 5px 0; font-size: 12px; color: #666;">
                                <strong>From:</strong> {from_email}<br>
                                <strong>Calendar:</strong> Google Calendar
                            </p>
                        </div>

                        <p style="color: #999; font-size: 12px; text-align: center; margin-top: 20px;">
                            This is an automated reminder from your calendar assistant.
                        </p>
                    </div>
                </body>
            </html>
            """

            print(f"\n📧 Sending reminder emails to: {', '.join(email_list)}")
            print(f"   From: {from_email}")
            print(f"   Event: {event_title}")
            print(f"   Time: {event_start}\n")

            for recipient in email_list:
                try:
                    result = await gmail_client_instance.call_tool(
                        "gmail_send_email",
                        {
                            "to": recipient,
                            "subject": f"📅 Reminder: {event_title}",
                            "body": html_body
                        }
                    )

                    if not result.is_error:
                        print(f"   ✅ Email sent to {recipient}")
                    else:
                        print(f"   ❌ Failed to send to {recipient}")
                except Exception as e:
                    print(f"   ❌ Error sending to {recipient}: {e}")

            return True

        except Exception as e:
            print(f"❌ Email sending error: {e}")
            return False

    async def call_mcp_tool(**arguments):

        arguments = {
            key: value
            for key, value in arguments.items()
            if value is not None
        }

        print(f"\n🔧 [STEP 4: TOOL EXECUTION] Executing: {mcp_tool.name}")
        print(f"   Arguments: {arguments}")

        # Normalize attendees to list of email strings
        if "attendees" in arguments:
            att = arguments["attendees"]
            if isinstance(att, list):
                clean_att = []
                for item in att:
                    if isinstance(item, dict) and "email" in item:
                        clean_att.append(str(item["email"]).strip())
                    elif isinstance(item, str) and item.strip():
                        clean_att.append(item.strip())
                arguments["attendees"] = clean_att
            elif isinstance(att, str):
                arguments["attendees"] = [e.strip() for e in att.split(",") if e.strip()]

        # Normalize Gmail recipient 'to' to clean list of email strings
        if "to" in arguments:
            to_val = arguments["to"]
            if isinstance(to_val, str):
                arguments["to"] = [e.strip() for e in to_val.split(",") if e.strip()]
            elif isinstance(to_val, list):
                clean_to = []
                for item in to_val:
                    if isinstance(item, str):
                        for sub in item.split(","):
                            if sub.strip():
                                clean_to.append(sub.strip())
                    elif isinstance(item, dict) and "email" in item:
                        clean_to.append(str(item["email"]).strip())
                arguments["to"] = clean_to

        # Normalize Gmail email body aliases (body, message, content -> text)
        if mcp_tool.name == "gmail_send_email":
            if "body" in arguments and "text" not in arguments and "html" not in arguments:
                arguments["text"] = arguments.pop("body")
            if "message" in arguments and "text" not in arguments and "html" not in arguments:
                arguments["text"] = arguments.pop("message")
            if "content" in arguments and "text" not in arguments and "html" not in arguments:
                arguments["text"] = arguments.pop("content")

            to_recipients = arguments.get("to", [])
            if isinstance(to_recipients, str):
                to_recipients = [e.strip() for e in to_recipients.split(",") if e.strip()]

            event_id = last_event_context.get("id")
            cal_service = get_calendar_service()

            subj = str(arguments.get("subject", "")).lower()
            txt = str(arguments.get("text", "")).lower()
            is_invite_action = (
                bool(event_id)
                and (
                    any(kw in f"{subj} {txt}".lower() for kw in ["invit", "meeting", "interview", "exam", "event", "calendar", "join", "schedule", "rsvp", "time", "please", "attend"])
                    or bool(last_event_context.get("summary") and last_event_context["summary"].lower() in f"{subj} {txt}".lower())
                    or not subj
                )
            )

            # If sending an invitation for the scheduled event, dispatch official Google default invitation
            if is_invite_action and cal_service and to_recipients:
                print(f"📧 [NATIVE GOOGLE INVITATION] Adding attendees {to_recipients} to event '{last_event_context.get('summary')}' with sendUpdates='all'...")
                try:
                    current_ev = cal_service.events().get(calendarId="primary", eventId=event_id).execute()
                    existing_attendees = current_ev.get("attendees", [])
                    existing_emails = {a.get("email").lower() for a in existing_attendees if a.get("email")}

                    new_attendees = list(existing_attendees)
                    for rec in to_recipients:
                        if rec.lower() not in existing_emails:
                            new_attendees.append({"email": rec})
                            existing_emails.add(rec.lower())

                    patch_payload = {"attendees": new_attendees}

                    user_note = arguments.get("text", "").strip()
                    if user_note:
                        meet_lnk = last_event_context.get("hangoutLink", "")
                        clean_note = user_note
                        if meet_lnk and meet_lnk in clean_note:
                            clean_note = clean_note.replace(f"Join with Google Meet: {meet_lnk}", "").replace(meet_lnk, "").strip()
                        existing_desc = current_ev.get("description", "")
                        if clean_note:
                            if existing_desc and clean_note not in existing_desc:
                                patch_payload["description"] = f"{existing_desc}\n\nNote: {clean_note}"
                            elif not existing_desc:
                                patch_payload["description"] = clean_note

                    hangout_link = current_ev.get("hangoutLink") or last_event_context.get("hangoutLink")
                    conf_version = 0
                    if last_event_context.get("is_meeting") and not hangout_link:
                        import uuid
                        patch_payload["conferenceData"] = {
                            "createRequest": {
                                "requestId": str(uuid.uuid4()),
                                "conferenceSolutionKey": {"type": "hangoutsMeet"}
                            }
                        }
                        conf_version = 1

                    import time
                    updated_ev = None
                    for attempt in range(2):
                        try:
                            updated_ev = cal_service.events().patch(
                                calendarId="primary",
                                eventId=event_id,
                                body=patch_payload,
                                conferenceDataVersion=conf_version,
                                sendUpdates="all"
                            ).execute()
                            break
                        except Exception as e:
                            if attempt == 0 and "rateLimit" in str(e).lower():
                                time.sleep(1.5)
                                continue
                            raise e

                    if updated_ev:
                        hangout_link = updated_ev.get("hangoutLink") or hangout_link
                        last_event_context["hangoutLink"] = hangout_link
                        last_event_context["attendees"] = [a.get("email") for a in updated_ev.get("attendees", [])]

                        print(f"✅ [NATIVE GOOGLE INVITATION DISPATCHED] Google Calendar sent native invite to {to_recipients}")

                        meet_str = f"\nGoogle Meet: {hangout_link}" if hangout_link else ""
                        return (
                            f"Official Google Calendar invitation successfully sent to {', '.join(to_recipients)} "
                            f"for event '{updated_ev.get('summary', 'Meeting')}'{meet_str} with interactive RSVP options (Yes/Maybe/No) and direct calendar synchronization."
                        )
                except Exception as patch_e:
                    print(f"⚠️ Error sending native Google invitation via Calendar API: {patch_e}")

        # Ensure start and end always include valid ISO 8601 with timezone
        if "start" in arguments and isinstance(arguments["start"], str):
            arguments["start"] = fix_iso_date(arguments["start"])
        if "end" in arguments and isinstance(arguments["end"], str):
            arguments["end"] = fix_iso_date(arguments["end"])

        if mcp_tool.name == "calendar_get_freebusy":
            for k in ("time_min", "time_max"):
                val = arguments.get(k)
                if val and isinstance(val, str):
                    arguments[k] = fix_iso_date(val)

        if mcp_tool.name in {"calendar_create_event", "calendar_update_event"}:
            start_str = arguments.get("start")
            end_str = arguments.get("end")

            if start_str:
                try:
                    time_min = start_str
                    time_max = end_str or start_str

                    availability = await mcp_client.call_tool(
                        "calendar_get_freebusy",
                        {
                            "time_min": time_min,
                            "time_max": time_max,
                            "calendar_ids": ["primary"]
                        }
                    )

                    if not availability.is_error and availability.content:
                        availability_text = "\n".join(
                            content.text
                            for content in availability.content
                            if hasattr(content, "text")
                        )
                        if availability_text.strip():
                            try:
                                availability_data = json.loads(availability_text)
                                busy = availability_data.get("calendars", {}).get("primary", {}).get("busy", [])
                                if busy:
                                    return (
                                        "CONFLICT: The requested time is already occupied on your primary calendar. "
                                        "Ask the user if they'd like to choose another time or proceed anyway."
                                    )
                            except (json.JSONDecodeError, KeyError, TypeError):
                                pass
                except Exception:
                    pass

        if mcp_tool.name in write_tools:

            if require_terminal_confirm:
                confirmation = input(
                    "Confirm? (yes/no): "
                ).strip().lower()

                if confirmation not in {"yes", "y"}:
                    return "User cancelled the action."

            if mcp_tool.name in {"calendar_create_event", "calendar_update_event"}:
                reminder_emails = arguments.get("reminder_emails", "").strip()

                if reminder_emails and reminder_emails not in {"", '""', "''"}:
                    event_title = arguments.get("title", "Event")
                    event_start = arguments.get("start", "")
                    event_description = arguments.get("description", "")

                    await send_reminder_email(reminder_emails, event_title, event_start, event_description)

        try:
            result = await mcp_client.call_tool(
                mcp_tool.name,
                arguments
            )
            if getattr(result, "is_error", False):
                print(f"❌ [TOOL ERROR] Tool '{mcp_tool.name}' returned error status.")
                if getattr(result, "content", None):
                    err_texts = [c.text for c in result.content if hasattr(c, "text")]
                    print(f"   Details: {' '.join(err_texts)}")
            else:
                print(f"✅ [TOOL SUCCESS] Tool '{mcp_tool.name}' completed.")

            # If calendar event created, check if it's a meeting/interview/exam and attach Google Meet conference
            if mcp_tool.name == "calendar_create_event" and not getattr(result, "is_error", False):
                try:
                    if result.content and hasattr(result.content[0], "text"):
                        event_data = json.loads(result.content[0].text)
                        event_id = event_data.get("id")
                        summary = event_data.get("summary", "") or arguments.get("summary", "")
                        description = event_data.get("description", "") or arguments.get("description", "")
                        location = event_data.get("location", "") or arguments.get("location", "")

                        meeting_keywords = [
                            "meeting", "interview", "exam online", "online exam", "exam", "test",
                            "call", "sync", "discussion", "meet", "connect", "conference"
                        ]
                        is_meeting = any(
                            kw in f"{summary} {description} {location}".lower()
                            for kw in meeting_keywords
                        )

                        hangout_link = event_data.get("hangoutLink")

                        cal_service = get_calendar_service()
                        if cal_service and event_id:
                            try:
                                import uuid
                                patch_body = {}
                                conf_version = 0

                                # Attach Google Meet if meeting/interview/exam and hangoutLink is missing
                                if is_meeting and not hangout_link:
                                    patch_body["conferenceData"] = {
                                        "createRequest": {
                                            "requestId": str(uuid.uuid4()),
                                            "conferenceSolutionKey": {"type": "hangoutsMeet"}
                                        }
                                    }
                                    conf_version = 1

                                # If attendees were provided, ensure they receive native Google Calendar invites
                                attendees_raw = arguments.get("attendees", [])
                                if attendees_raw:
                                    patch_body["attendees"] = [
                                        {"email": a if isinstance(a, str) else a.get("email")}
                                        for a in attendees_raw if a
                                    ]

                                if patch_body:
                                    import time
                                    patched = None
                                    for attempt in range(2):
                                        try:
                                            patched = cal_service.events().patch(
                                                calendarId=arguments.get("calendar_id", "primary"),
                                                eventId=event_id,
                                                conferenceDataVersion=conf_version,
                                                body=patch_body,
                                                sendUpdates="all"
                                            ).execute()
                                            break
                                        except Exception as e:
                                            if attempt == 0 and "rateLimit" in str(e).lower():
                                                time.sleep(1.5)
                                                continue
                                            raise e

                                    if patched:
                                        hangout_link = patched.get("hangoutLink") or hangout_link
                                        if hangout_link:
                                            event_data["hangoutLink"] = hangout_link
                                            result.content[0].text = json.dumps(event_data, indent=2)
                                            print(f"📹 [GOOGLE MEET GENERATED] Link: {hangout_link}")
                            except Exception as patch_err:
                                print(f"⚠️ Could not patch event: {patch_err}")

                        # Update last event context
                        last_event_context.clear()
                        last_event_context.update({
                            "id": event_id,
                            "summary": summary,
                            "start": arguments.get("start", event_data.get("start", "")),
                            "end": arguments.get("end", event_data.get("end", "")),
                            "hangoutLink": hangout_link,
                            "attendees": arguments.get("attendees", []),
                            "description": description,
                            "is_meeting": is_meeting
                        })
                except Exception as parse_err:
                    print(f"⚠️ Error updating event context: {parse_err}")
        except Exception as e:
            print(f"\n❌ [ERROR DURING TOOL EXECUTION: {mcp_tool.name}]")
            print(f"   Error Type: {type(e).__name__}")
            print(f"   Error Details: {str(e)}")
            import traceback
            traceback.print_exc()
            raise e

        if result.content:

            text_parts = []

            for content in result.content:

                if hasattr(content, "text"):
                    text_parts.append(content.text)
                else:
                    text_parts.append(str(content))

            return "\n".join(text_parts)

        return str(result)

    return StructuredTool.from_function(
        coroutine=call_mcp_tool,
        name=mcp_tool.name,
        description=mcp_tool.description or mcp_tool.name,
        args_schema=args_schema,
    )

def create_agent_node(model):

    async def agent_node(state: CalendarState):

        today = datetime.now().astimezone().strftime(
            "%Y-%m-%d %A %H:%M (Timezone: %Z / %z)"
        )
        sender_email = os.getenv("GMAIL_SENDER_EMAIL", "")

        system_prompt = f"""
You are Mindlytic, an intelligent, thoughtful, human-like Calendar AI Assistant.
You help the user manage their schedule and calendar effortlessly through natural, polite, and interactive conversation.

Current local date and time: {today}

Capabilities:
1. `calendar_list_events`: Fetch events from Google Calendar within a date range.
2. `calendar_get_freebusy`: Check availability and free/busy time blocks.
3. `calendar_create_event`: Create new events in Google Calendar.
4. `calendar_update_event` & `calendar_delete_event`: Modify or delete existing events.
5. `gmail_send_email`: Send emails via Gmail.

Core Rules & Guidelines:

1. TRUTH & ZERO HALLUCINATION (STRICT):
   - NEVER invent events, times, or meetings!
   - When asked about events for any day/week/month: ALWAYS call `calendar_list_events`.
   - If `calendar_list_events` returns NO events (empty list `[]`), you MUST explicitly inform the user that their schedule is completely clear and there are NO events on that date.
   - NEVER assume an event exists just because it was discussed previously in chat history unless it is confirmed by `calendar_list_events`.

2. CLEAN FORMATTING FOR EVENT LISTS:
   - NEVER output plain-text columns with spaces that break and jump around.
   - Always format events as clean, organized Markdown bullet points:
     📅 **[Event Title]**
     - **Time:** [Start Time] – [End Time]
     - **Location / Link:** [Meeting link or location, if any]
     - **Details:** [Description or notes, if any]
   - If no events are found, state clearly: "You have no events scheduled for [Date/Month]. Your schedule is completely clear!"

3. HUMAN-INTERACTIVE EVENT SCHEDULING (PRE-CHECK & CONFIRMATION):
   - NEVER create an event on Google Calendar blindly or immediately without timing clarification, slot verification, and explicit user confirmation!
   
   A. GATHER TIMING & CHECK FREE SLOTS (DO NOT GUESS):
      - When the user asks to schedule, add, or create an event (e.g. "create event meeting tomorrow") without a specific time:
        - DO NOT invent, assume, or pick an arbitrary time!
        - First, check the user's availability for that date using `calendar_get_freebusy` or `calendar_list_events`.
        - Politely ask the user what time they prefer, and suggest 2-3 open slots that are completely free on their calendar (e.g. "What time would you prefer tomorrow? Your calendar is open at 10:00 AM or 2:00 PM.").
        - Also ask for the attendee's email address if not already provided so an invite can be sent.
   
   B. CONFLICT & AVAILABILITY VERIFICATION:
      - When a specific time is provided by the user (or initially requested):
        - ALWAYS check if that time block is free using `calendar_get_freebusy`.
        - If the slot is occupied/has a conflict:
          - Inform the user of the conflicting schedule and suggest nearby free alternative slots.
   
   C. PRE-CONFIRMATION SUMMARY:
      - When the time is verified to be completely FREE and no conflicts exist:
        - DO NOT call `calendar_create_event` yet!
        - Present a clean, structured preview summary of the proposed event:
          I've checked your schedule and that slot is completely open!
          
          Proposed Event Details:
          - 📅 **Title:** [Event Title]
          - 🕒 **Date & Time:** [Date and Time] (IST)
          - 👥 **Attendees:** [Attendee email(s) or "None"]
          - 📹 **Google Meet:** [Yes, video link will be generated (for meetings, interviews, online exams) / Not applicable (for personal tasks)]
          
          "Should I go ahead and schedule this event on your calendar?"
   
   D. CONFIRMATION & EXECUTION:
      - ONLY call `calendar_create_event` after the user replies with ANY affirmative confirmation!
      - Affirmative confirmation words include: "yes", "ok", "okay", "create", "execute", "go ahead", "schedule it", "sure", "proceed", "fine", "looks good", "do it", "confirm", "yep", "yeah", "done", etc.
      - When the user uses ANY of these affirmative words/phrases:
        - IMMEDIATELY call `calendar_create_event`!
        - If the event is a meeting, interview, or online exam, the official default Google Meet link is automatically attached.
        - Once created, output ONLY the crisp confirmation below with NO extra conversational text:
          ✅ **Event Scheduled!**
          - **Title:** [Event Title]
          - **Date & Time:** [Date and Time] (IST)
          - **Google Meet:** [Include the generated Google Meet link (e.g. https://meet.google.com/...) ONLY for meetings, interviews, or online exams; do NOT include for personal events]
          - **Attendees:** [Attendee email(s) or "None"]
          
          (If Attendees is "None", add only: "Would you like me to invite anyone?")
          (If Attendees were provided, the official native Google Calendar invitation has been automatically dispatched!)

4. DATES & TIMEZONES:
   - Convert relative dates ("today", "tomorrow", "this Friday", "next week") accurately using current date: {today}.
   - By default, assume a 1-hour duration for meetings unless the user specifies otherwise.

5. OFFICIAL GOOGLE CALENDAR INVITATIONS:
   - When the user asks to invite attendees or send an email/invite (e.g. "To: tech.darius.ai@gmail.com, message: please join on time" or "send invitation to ..."):
     - IMMEDIATELY call `gmail_send_email` with `to` recipient(s) and any message.
     - Google Calendar automatically dispatches the OFFICIAL native Google default invitation email with real-time interactive RSVP buttons (`Yes`, `No`, `Maybe`, `Propose a new time`), the working Google Meet button, and automatic calendar sync (matching Google's native standard format).
     - NEVER send custom static HTML email cards.
     - Once sent, confirm to the user with ONLY essential details:
       📧 **Invitation Sent!**
       - **To:** [Recipient email(s)]
       - **Event:** [Event Title]
       - **Google Meet:** [Meet link if meeting/interview/exam online]

6. DOCUMENT & PHOTO KNOWLEDGE Q&A (RAG & MULTIMODAL):
   - When the user uploads a document (e.g. company rules book, employee handbook, PDF policy, notes, CSV) or photo (e.g. calendar photo, schedule screenshot, festival flyer):
     - The extracted document text and visual inspection findings will be provided in the message under `=== ATTACHED MEDIA & DOCUMENT KNOWLEDGE ===`.
     - ACCURACY & CITATIONS: Answer questions directly and truthfully based on the uploaded content. When answering from a rules book or document, quote or cite specific sections, rules, or pages (e.g. "According to Section 4.2 of the Company Rules...").
     - CALENDAR & PHOTO INSPECTION: When asked about events, birthdays, or festivals from a calendar photo, clearly enumerate them with dates, times, and descriptions as clean Markdown bullet points.
     - PROACTIVE CALENDAR SCHEDULING: If the user asks or confirms to schedule any events detected from the photo or document into their Google Calendar, IMMEDIATELY call `calendar_create_event` with the extracted title, date, and time!

7. ULTRA-CONCISE RESPONSES (STRICT - NO EXTRA INFO):
   - Keep all responses crisp, brief, and directly relevant to the user's request.
   - Mention ONLY essential details: Title, Date & Time, Google Meet link (when applicable), Attendees.
   - DO NOT include conversational rambling, unneeded tips, explanations of how calendars work, or repetitive closing filler.
   - For meetings, interviews, and online exams, ALWAYS provide the default Google Meet link.
   - For personal non-meeting events (e.g. dentist, gym, flight, personal reminder), DO NOT mention or generate a Google Meet link.
"""

        model_name = getattr(model, "model_name", None) or getattr(model, "model", "LLM")
        print(f"   [AGENT] Invoking model ({model_name}) with {len(state['messages'])} message(s)...")

        try:
            response = await model.ainvoke(
                [
                    {
                        "role": "system",
                        "content": system_prompt
                    }
                ] + state["messages"]
            )
        except Exception as e:
            print(f"\n❌ [ERROR IN AGENT THINKING / LLM CALL]")
            print(f"   Model: {model_name}")
            print(f"   Error Type: {type(e).__name__}")
            print(f"   Error Details: {str(e)}")
            import traceback
            traceback.print_exc()
            raise e

        tool_calls = getattr(response, "tool_calls", None)
        if tool_calls:
            print(f"⚙️  [STEP 3: AGENT DECISION] Agent requested {len(tool_calls)} tool call(s):")
            for tc in tool_calls:
                print(f"   👉 Tool: {tc.get('name')} | Arguments: {tc.get('args')}")
        else:
            print(f"💬 [STEP 3: AGENT DECISION] Agent formulated direct response (no tools needed).")

        return {
            "messages": [response]
        }

    return agent_node

def should_continue(state: CalendarState):

    last_message = state["messages"][-1]

    if getattr(last_message, "tool_calls", None):
        print(f"🔄 [ROUTING] Executing requested tool(s)...")
        return "tools"

    print(f"🏁 [ROUTING] No further tools needed. Completing turn.")
    return END

async def main():

    tools = []
    clients = []
    global gmail_client_instance

    try:
        with silence_stderr():
            # Calendar MCP Server
            calendar_server = StdioServerParameters(
                command="npx",
                args=[
                    "-y",
                    "@franciscpd/calendar-mcp-server"
                ],
                env={
                    "GOOGLE_CALENDAR_CLIENT_ID":
                        os.environ["GOOGLE_CALENDAR_CLIENT_ID"],

                    "GOOGLE_CALENDAR_CLIENT_SECRET":
                        os.environ["GOOGLE_CALENDAR_CLIENT_SECRET"],

                    "GOOGLE_CALENDAR_REFRESH_TOKEN":
                        os.environ["GOOGLE_CALENDAR_REFRESH_TOKEN"],
                    "LOG_LEVEL": "error"
                },
            )

            calendar_client = Client(calendar_server)
            await calendar_client.__aenter__()
            clients.append(calendar_client)

            # Gmail MCP Server (optional)
            if gmail_configured:
                try:
                    gmail_server = StdioServerParameters(
                        command="npx",
                        args=[
                            "-y",
                            "gmail-mcp-server"
                        ],
                        env={
                            **os.environ,
                            "GMAIL_CLIENT_ID": os.environ["GMAIL_CLIENT_ID"],
                            "GMAIL_CLIENT_SECRET": os.environ["GMAIL_CLIENT_SECRET"],
                            "GMAIL_REFRESH_TOKEN": os.environ["GMAIL_REFRESH_TOKEN"],
                            "DEBUG": "0",
                            "NODE_ENV": "production",
                            "LOG_LEVEL": "error"
                        }
                    )

                    gmail_client_instance = Client(gmail_server)
                    await gmail_client_instance.__aenter__()
                    clients.append(gmail_client_instance)

                except Exception:
                    gmail_client_instance = None

            # Now create tools with Gmail available
            calendar_tools = await calendar_client.list_tools()

            calendar_langchain_tools = [
                create_langchain_tool(
                    calendar_client,
                    mcp_tool
                )
                for mcp_tool in calendar_tools.tools
            ]
            tools.extend(calendar_langchain_tools)

            # Add Gmail tools if available
            if gmail_client_instance:
                gmail_tools = await gmail_client_instance.list_tools()

                gmail_langchain_tools = [
                    create_langchain_tool(
                        gmail_client_instance,
                        mcp_tool
                    )
                    for mcp_tool in gmail_tools.tools
                ]
                tools.extend(gmail_langchain_tools)

        # Setup and run agent
        await setup_and_run_agent(tools)

    finally:
        # Close all clients
        for client in clients:
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass


async def setup_and_run_agent(tools):
    model = get_chat_model()

    model_with_tools = model.bind_tools(tools)

    agent_node = create_agent_node(model_with_tools)

    tool_node = ToolNode(tools)

    graph = StateGraph(CalendarState)

    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)

    graph.add_edge(START, "agent")

    graph.add_conditional_edges(
        "agent",
        should_continue,
        {
            "tools": "tools",
            END: END
        }
    )

    graph.add_edge("tools", "agent")

    app = graph.compile()

    print("\n🤖 Calendar & Email Agent Ready!")
    print("Type 'exit' to stop.\n")

    conversation = []

    while True:

        user_input = input("You: ").strip()

        if user_input.lower() in {
            "exit",
            "quit",
            "bye"
        }:
            break

        if not user_input:
            continue

        conversation.append(
            HumanMessage(
                content=user_input
            )
        )

        result = await app.ainvoke(
            {
                "messages": conversation
            }
        )

        conversation = result["messages"]

        answer = conversation[-1].content

        answer = re.sub(r"\*\*(.*?)\*\*", r"\1", answer)

        print("\nAgent:", answer)
        print()


if __name__ == "__main__":
    if "--ui" in sys.argv:
        import app as web_app
        web_app.run()
    else:
        asyncio.run(main())

