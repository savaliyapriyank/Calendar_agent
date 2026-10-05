import asyncio
import os
import re
import sys
import json
import time
import webbrowser
from contextlib import asynccontextmanager
from typing import Any, Optional
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from mcp import Client, StdioServerParameters

from main import (
    silence_stderr,
    CalendarState,
    create_langchain_tool,
    create_agent_node,
    should_continue,
    required_variables,
    optional_gmail_variables,
    gmail_configured,
    get_chat_model,
)

load_dotenv()

# Verify required environment keys
for var in required_variables:
    if not os.getenv(var):
        raise ValueError(f"{var} is missing in .env")


def sync_gmail_credentials():
    """Ensure ~/.gmail-mcp/ has valid credentials and token files synced from .env, with active access token."""
    config_dir = os.path.expanduser("~/.gmail-mcp")
    os.makedirs(config_dir, exist_ok=True)
    token_file = os.path.join(config_dir, "token.json")
    creds_file = os.path.join(config_dir, "credentials.json")

    client_id = os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GMAIL_CLIENT_SECRET")
    refresh_token = os.getenv("GMAIL_REFRESH_TOKEN")

    if client_id and client_secret:
        client_config = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost:44000/oauth2callback", "http://localhost"]
            }
        }
        with open(creds_file, "w") as f:
            json.dump(client_config, f, indent=2)

    if refresh_token and client_id and client_secret:
        existing_data = {}
        if os.path.exists(token_file):
            try:
                with open(token_file, "r") as f:
                    existing_data = json.load(f)
            except Exception:
                pass

        existing_data["refresh_token"] = refresh_token
        if not existing_data.get("scope"):
            existing_data["scope"] = (
                "https://www.googleapis.com/auth/gmail.send "
                "https://www.googleapis.com/auth/gmail.readonly "
                "https://www.googleapis.com/auth/gmail.modify "
                "https://www.googleapis.com/auth/gmail.labels"
            )
        existing_data["token_type"] = "Bearer"

        # Check if access_token needs to be refreshed
        need_refresh = False
        current_access = existing_data.get("access_token", "").strip()
        if not current_access or current_access == "initial_token":
            need_refresh = True
        else:
            expiry = existing_data.get("expiry_date", 0)
            if expiry and expiry < time.time() * 1000:
                need_refresh = True

        if need_refresh:
            try:
                import urllib.request
                import urllib.parse
                token_url = "https://oauth2.googleapis.com/token"
                post_data = urllib.parse.urlencode({
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token"
                }).encode("utf-8")
                req = urllib.request.Request(
                    token_url,
                    data=post_data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        payload = json.loads(resp.read().decode("utf-8"))
                        existing_data["access_token"] = payload.get("access_token")
                        existing_data["expiry_date"] = int((time.time() + payload.get("expires_in", 3600)) * 1000)
                        print("🔑 Successfully refreshed Gmail OAuth access token.")
            except Exception as e:
                print(f"⚠️ Could not auto-refresh Gmail access token: {e}")

        with open(token_file, "w") as f:
            json.dump(existing_data, f, indent=2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initializes persistent MCP server connections and compiled LangGraph agent."""
    sync_gmail_credentials()
    clients = []
    tools = []
    calendar_client = None
    gmail_client = None

    try:
        with silence_stderr():
            # 1. Calendar MCP Server
            calendar_server = StdioServerParameters(
                command="npx",
                args=["-y", "@franciscpd/calendar-mcp-server"],
                env={
                    "GOOGLE_CALENDAR_CLIENT_ID": os.environ["GOOGLE_CALENDAR_CLIENT_ID"],
                    "GOOGLE_CALENDAR_CLIENT_SECRET": os.environ["GOOGLE_CALENDAR_CLIENT_SECRET"],
                    "GOOGLE_CALENDAR_REFRESH_TOKEN": os.environ["GOOGLE_CALENDAR_REFRESH_TOKEN"],
                    "LOG_LEVEL": "error",
                },
            )
            calendar_client = Client(calendar_server)
            await calendar_client.__aenter__()
            clients.append(calendar_client)

            # 2. Gmail MCP Server (optional)
            if gmail_configured:
                try:
                    gmail_server = StdioServerParameters(
                        command="npx",
                        args=["-y", "gmail-mcp-server"],
                        env={
                            **os.environ,
                            "GMAIL_CLIENT_ID": os.environ["GMAIL_CLIENT_ID"],
                            "GMAIL_CLIENT_SECRET": os.environ["GMAIL_CLIENT_SECRET"],
                            "GMAIL_REFRESH_TOKEN": os.environ["GMAIL_REFRESH_TOKEN"],
                            "DEBUG": "0",
                            "NODE_ENV": "production",
                            "LOG_LEVEL": "error",
                        },
                    )
                    gmail_client = Client(gmail_server)
                    await gmail_client.__aenter__()
                    clients.append(gmail_client)
                except Exception:
                    gmail_client = None

            # 3. Create LangChain Tools (require_terminal_confirm=False for web)
            calendar_tools_mcp = await calendar_client.list_tools()
            calendar_tools = [
                create_langchain_tool(calendar_client, tool, require_terminal_confirm=False)
                for tool in calendar_tools_mcp.tools
            ]
            tools.extend(calendar_tools)

            if gmail_client:
                gmail_tools_mcp = await gmail_client.list_tools()
                gmail_tools = [
                    create_langchain_tool(gmail_client, tool, require_terminal_confirm=False)
                    for tool in gmail_tools_mcp.tools
                ]
                tools.extend(gmail_tools)

        # 4. Compile StateGraph Agent
        model = get_chat_model()
        model_with_tools = model.bind_tools(tools)
        agent_node = create_agent_node(model_with_tools)
        tool_node = ToolNode(tools)

        graph = StateGraph(CalendarState)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", tool_node)
        graph.add_edge(START, "agent")
        graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
        graph.add_edge("tools", "agent")
        compiled_agent = graph.compile()

        # Store in app state
        app.state.calendar_client = calendar_client
        app.state.gmail_client = gmail_client
        app.state.tools = tools
        app.state.clients = clients
        app.state.agent = compiled_agent

        print("⚡ Calendar & Email Web Assistant backend initialized successfully.")
        yield

    finally:
        for client in clients:
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass


app = FastAPI(title="Mindlytic AI Calendar Assistant", lifespan=lifespan)

@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Mount static files
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir, exist_ok=True)

app.mount("/static", StaticFiles(directory=static_dir), name="static")


class MessageItem(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[MessageItem] = []


@app.get("/")
async def get_index():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Mindlytic AI backend running. Place index.html in static/"}


@app.get("/api/status")
async def get_status():
    """Returns the operational status of Google Calendar, Gmail, and AI tools."""
    return {
        "status": "ready",
        "calendar_connected": getattr(app.state, "calendar_client", None) is not None,
        "gmail_connected": getattr(app.state, "gmail_client", None) is not None,
        "tools_count": len(getattr(app.state, "tools", [])),
    }


@app.get("/api/events")
async def get_events(start: Optional[str] = None, end: Optional[str] = None):
    """Fetches calendar events directly from Google Calendar for FullCalendar."""
    calendar_client = getattr(app.state, "calendar_client", None)
    if not calendar_client:
        return []

    try:
        args: dict[str, Any] = {"calendar_id": "primary"}
        if start:
            args["time_min"] = start
        if end:
            args["time_max"] = end

        raw_result = await calendar_client.call_tool("calendar_list_events", args)

        events_data = []
        if raw_result.content:
            text_payload = "\n".join(
                c.text for c in raw_result.content if hasattr(c, "text")
            )
            if text_payload:
                try:
                    parsed = json.loads(text_payload)
                    raw_events = parsed.get("events", []) if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else [])
                    for ev in raw_events:
                        start_val = ev.get("start")
                        end_val = ev.get("end")

                        if isinstance(start_val, dict):
                            ev_start = start_val.get("dateTime") or start_val.get("date")
                            is_all_day = "dateTime" not in start_val
                        elif isinstance(start_val, str):
                            ev_start = start_val
                            is_all_day = "T" not in start_val
                        else:
                            ev_start = None
                            is_all_day = False

                        if isinstance(end_val, dict):
                            ev_end = end_val.get("dateTime") or end_val.get("date")
                        elif isinstance(end_val, str):
                            ev_end = end_val
                        else:
                            ev_end = ev_start

                        events_data.append({
                            "id": ev.get("id"),
                            "title": ev.get("summary") or ev.get("title") or "(Untitled)",
                            "start": ev_start,
                            "end": ev_end,
                            "allDay": is_all_day,
                            "description": ev.get("description") or "",
                            "location": ev.get("location") or "",
                            "status": ev.get("status") or "confirmed",
                            "htmlLink": ev.get("htmlLink") or ev.get("hangoutLink") or ""
                        })
                except Exception as e:
                    print(f"Error parsing events JSON: {e}")

        return events_data

    except Exception as e:
        print(f"Error fetching events: {e}")
        return []


@app.post("/api/chat")
async def chat(request: ChatRequest):
    """Processes conversational messages through the LangGraph calendar assistant."""
    agent = getattr(app.state, "agent", None)
    if not agent:
        print("❌ [ERROR] Chat requested but agent is not ready yet.")
        raise HTTPException(status_code=503, detail="Agent is not ready yet.")

    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*70}")
    print(f"📥 [STEP 1: INCOMING REQUEST] {time_str}")
    print(f"   User Message: \"{request.message}\"")
    print(f"   History Context: {len(request.history)} prior message(s)")
    print(f"{'-'*70}")

    conversation: list[BaseMessage] = []

    # Reconstruct previous conversation history
    for item in request.history[-10:]:  # Keep recent context window
        if item.role == "user":
            conversation.append(HumanMessage(content=item.content))
        elif item.role == "assistant":
            conversation.append(AIMessage(content=item.content))

    # Add the current message
    conversation.append(HumanMessage(content=request.message))

    print(f"🧠 [STEP 2: AGENT THINKING] Starting agent processing...")

    try:
        result = await agent.ainvoke({"messages": conversation})
        messages = result.get("messages", [])
        last_message = messages[-1] if messages else None

        answer = last_message.content if last_message else "I couldn't process that request."
        
        # Collect tool outputs to filter out failed or auth-required tool calls
        tool_results = {}
        for msg in messages[len(conversation):]:
            if hasattr(msg, "tool_call_id") and getattr(msg, "content", None):
                tool_results[msg.tool_call_id] = str(msg.content)

        actions_taken = []
        seen_tools = set()
        for msg in messages[len(conversation):]:
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    tname = tc.get("name")
                    tid = tc.get("id")
                    t_content = tool_results.get(tid, "")
                    if "Authentication Required" in t_content or "❌" in t_content or "Failed" in t_content or "Error" in t_content:
                        continue
                    if tname and tname not in seen_tools:
                        seen_tools.add(tname)
                        actions_taken.append({
                            "tool": tname,
                            "args": tc.get("args")
                        })

        print(f"{'-'*70}")
        print(f"✅ [STEP 5: RESPONSE READY] Processing complete.")
        print(f"   Response Length: {len(answer)} chars")
        print(f"   Actions Taken: {len(actions_taken)}")
        print(f"{'='*70}\n")

        return {
            "success": True,
            "response": answer,
            "actions": actions_taken,
        }

    except Exception as e:
        print(f"\n{'='*70}")
        print(f"❌ [ERROR IN CHAT PROCESSING]")
        print(f"   Error Type: {type(e).__name__}")
        print(f"   Error Details: {str(e)}")
        print(f"{'-'*70}")
        import traceback
        traceback.print_exc()
        print(f"{'='*70}\n")
        return {
            "success": False,
            "response": f"An error occurred: {str(e)}",
            "actions": [],
        }


def find_available_port(default_port: int = 8000) -> int:
    import socket
    for port in range(default_port, default_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', port)) != 0:
                return port
    return default_port


def run():
    """Starts the FastAPI web server and opens the browser."""
    env_port = os.getenv("PORT")
    port = int(env_port) if env_port else find_available_port(8000)
    host = "0.0.0.0"
    url = f"http://{host}:{port}"
    print(f"\n🚀 Launching Mindlytic AI Calendar Web UI at {url}...")

    def open_browser():
        import time
        time.sleep(1.2)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    import threading
    threading.Thread(target=open_browser, daemon=True).start()

    uvicorn.run(app, host=host, port=port, reload=False, log_level="warning")


if __name__ == "__main__":
    run()

