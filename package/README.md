# @franciscpd/calendar-mcp-server

[![CI](https://github.com/franciscpd/mcp-server-calendar/actions/workflows/ci.yml/badge.svg)](https://github.com/franciscpd/mcp-server-calendar/actions/workflows/ci.yml)
[![npm version](https://img.shields.io/npm/v/@franciscpd/calendar-mcp-server)](https://www.npmjs.com/package/@franciscpd/calendar-mcp-server)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server for Google Calendar. Read, search, create, update, and delete calendar events, check availability, and list calendars — powered by 3 environment variables.

## Quick Start

```bash
npx -y @franciscpd/calendar-mcp-server
```

Set these environment variables:

| Variable | Description |
|----------|-------------|
| `GOOGLE_CALENDAR_CLIENT_ID` | OAuth2 client ID from Google Cloud Console |
| `GOOGLE_CALENDAR_CLIENT_SECRET` | OAuth2 client secret |
| `GOOGLE_CALENDAR_REFRESH_TOKEN` | OAuth2 refresh token (see [Setup Guide](#setup-guide)) |

## Setup Guide

### 1. Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select an existing one)
3. Note your project name for the next steps

### 2. Enable the Google Calendar API

1. Navigate to **APIs & Services** → **Library**
2. Search for "Google Calendar API"
3. Click **Enable**

### 3. Create OAuth2 Credentials

1. Navigate to **APIs & Services** → **Credentials**
2. Click **Create Credentials** → **OAuth client ID**
3. If prompted, configure the **OAuth consent screen**:
   - User type: **External** (or Internal for Workspace)
   - Add the scopes:
     - `https://www.googleapis.com/auth/calendar.readonly`
     - `https://www.googleapis.com/auth/calendar.events`
   - Add your email as a test user
4. Application type: **Web application**
5. Add `https://developers.google.com/oauthplayground` as an authorized redirect URI
6. Save your **Client ID** and **Client Secret**

### 4. Get a Refresh Token

1. Go to [Google OAuth2 Playground](https://developers.google.com/oauthplayground)
2. Click the gear icon (top right) and check **Use your own OAuth credentials**
3. Enter your **Client ID** and **Client Secret**
4. In the left panel, find **Google Calendar API v3** and select:
   - `https://www.googleapis.com/auth/calendar.readonly`
   - `https://www.googleapis.com/auth/calendar.events`
5. Click **Authorize APIs** and grant access
6. Click **Exchange authorization code for tokens**
7. Copy the **Refresh token**

## Important: OAuth Consent Screen

If your OAuth consent screen is in **Testing** mode, refresh tokens expire after **7 days**. To avoid this:

- **Option A:** Publish your app (set status to "In production" in the OAuth consent screen settings). No Google review is needed for apps used only by you.
- **Option B:** Add specific Google accounts as test users in the OAuth consent screen settings. Tokens for test users still expire after 7 days, but you can re-authorize.

For personal use, **Option A** is recommended — publish the app so tokens don't expire.

See [Google's OAuth consent screen documentation](https://support.google.com/cloud/answer/10311615) for details.

## Configuration

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "google-calendar": {
      "command": "npx",
      "args": ["-y", "@franciscpd/calendar-mcp-server"],
      "env": {
        "GOOGLE_CALENDAR_CLIENT_ID": "your-client-id",
        "GOOGLE_CALENDAR_CLIENT_SECRET": "your-client-secret",
        "GOOGLE_CALENDAR_REFRESH_TOKEN": "your-refresh-token"
      }
    }
  }
}
```

### Cursor

Add to your Cursor MCP settings:

```json
{
  "mcpServers": {
    "google-calendar": {
      "command": "npx",
      "args": ["-y", "@franciscpd/calendar-mcp-server"],
      "env": {
        "GOOGLE_CALENDAR_CLIENT_ID": "your-client-id",
        "GOOGLE_CALENDAR_CLIENT_SECRET": "your-client-secret",
        "GOOGLE_CALENDAR_REFRESH_TOKEN": "your-refresh-token"
      }
    }
  }
}
```

### Generic MCP Client

```bash
GOOGLE_CALENDAR_CLIENT_ID=your-client-id \
GOOGLE_CALENDAR_CLIENT_SECRET=your-client-secret \
GOOGLE_CALENDAR_REFRESH_TOKEN=your-refresh-token \
npx -y @franciscpd/calendar-mcp-server
```

The server communicates over **stdio** using the MCP protocol.

## Tools

| Tool | Description |
|------|-------------|
| `calendar_list_calendars` | List all calendars with id, name, timezone, color, and access role |
| `calendar_list_events` | List events from a calendar within a date range (default: next 7 days) |
| `calendar_get_event` | Get full event details including attendees, recurrence, and reminders |
| `calendar_search_events` | Search events by text across summaries, descriptions, and locations |
| `calendar_get_freebusy` | Check free/busy availability for one or more calendars |
| `calendar_create_event` | Create a new calendar event (timed or all-day) |
| `calendar_update_event` | Update an existing event (partial update — only specified fields change) |
| `calendar_delete_event` | Delete a calendar event |

## Development

```bash
# Install dependencies
npm install

# Build
npm run build

# Run tests
npm test

# Run tests with coverage
npm run test:coverage

# Start the server
npm start
```

## License

MIT
