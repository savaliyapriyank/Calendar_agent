#!/usr/bin/env node

// src/index.ts
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";

// src/auth/env.ts
var REQUIRED_VARS = [
  "GOOGLE_CALENDAR_CLIENT_ID",
  "GOOGLE_CALENDAR_CLIENT_SECRET",
  "GOOGLE_CALENDAR_REFRESH_TOKEN"
];
var EnvValidationError = class extends Error {
  constructor(missing) {
    const varList = missing.map((v) => `  - ${v}`).join("\n");
    super(
      `Missing required environment variables:
${varList}

Set these variables and restart.`
    );
    this.missing = missing;
    this.name = "EnvValidationError";
  }
};
function validateEnv() {
  const missing = REQUIRED_VARS.filter((v) => !process.env[v]);
  if (missing.length > 0) {
    throw new EnvValidationError([...missing]);
  }
  return {
    clientId: process.env.GOOGLE_CALENDAR_CLIENT_ID,
    clientSecret: process.env.GOOGLE_CALENDAR_CLIENT_SECRET,
    refreshToken: process.env.GOOGLE_CALENDAR_REFRESH_TOKEN
  };
}

// src/auth/client.ts
import { OAuth2Client } from "google-auth-library";
import { google } from "googleapis";
import { Mutex } from "async-mutex";

// src/utils/logger.ts
var LEVELS = {
  error: 0,
  warn: 1,
  info: 2,
  debug: 3
};
var currentLevel = process.env.LOG_LEVEL in LEVELS ? process.env.LOG_LEVEL : "info";
function log(level, message) {
  if (LEVELS[level] <= LEVELS[currentLevel]) {
    const timestamp = (/* @__PURE__ */ new Date()).toISOString();
    process.stderr.write(
      `[calendar-mcp] ${timestamp} ${level.toUpperCase()} ${message}
`
    );
  }
}
var logger = {
  error: (msg) => log("error", msg),
  warn: (msg) => log("warn", msg),
  info: (msg) => log("info", msg),
  debug: (msg) => log("debug", msg)
};

// src/auth/client.ts
var refreshMutex = new Mutex();
function createCalendarClient(credentials) {
  const auth = new OAuth2Client(credentials.clientId, credentials.clientSecret);
  auth.setCredentials({ refresh_token: credentials.refreshToken });
  const originalRefresh = auth.refreshAccessToken.bind(auth);
  auth.refreshAccessToken = () => refreshMutex.runExclusive(() => originalRefresh());
  auth.on("tokens", () => {
    logger.debug("Access token refreshed");
  });
  const calendar = google.calendar({ version: "v3", auth });
  return { calendar, auth };
}
var CredentialValidationError = class extends Error {
  constructor() {
    super(
      "Failed to validate Google Calendar credentials. Check that your OAuth2 credentials are correct and the Calendar API is enabled in your Google Cloud project."
    );
    this.name = "CredentialValidationError";
  }
};
async function validateCredentials(calendar) {
  try {
    const response = await calendar.calendarList.list({ maxResults: 10 });
    const primary = response.data.items?.find((c) => c.primary);
    const email = primary?.id ?? response.data.items?.[0]?.id ?? "unknown";
    logger.info(`Authenticated as ${email}`);
    return email;
  } catch {
    throw new CredentialValidationError();
  }
}

// src/server.ts
import { createRequire } from "module";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

// src/utils/errors.ts
var ErrorCategory = {
  AUTH: "Authentication failed. Check your OAuth credentials.",
  FORBIDDEN: "Insufficient permissions. Ensure the Calendar API is enabled and the correct scopes are granted.",
  RATE_LIMIT: "Calendar API rate limit exceeded. Try again in a few seconds.",
  BAD_REQUEST: (detail) => `Bad request: ${detail}`,
  NOT_FOUND: (resource) => `${resource} not found.`,
  VALIDATION: (detail) => `Invalid parameter: ${detail}`,
  NETWORK: "Failed to connect to Calendar API. Check your network."
};
function toolError(message) {
  return {
    isError: true,
    content: [{ type: "text", text: message }]
  };
}
function sanitizeMessage(message) {
  let sanitized = message.replace(/ya29\.[^\s]*/g, "[REDACTED]");
  sanitized = sanitized.replace(/1\/\/[^\s]*/g, "[REDACTED]");
  if (sanitized.length > 200) {
    sanitized = sanitized.slice(0, 197) + "...";
  }
  return sanitized;
}
function categorizeError(error) {
  const gaxios = error;
  const status = gaxios.response?.status;
  if (status === 400) {
    const detail = gaxios.response?.data?.error?.message ?? error.message;
    return ErrorCategory.BAD_REQUEST(detail);
  }
  if (status === 401) return ErrorCategory.AUTH;
  if (status === 403) return ErrorCategory.FORBIDDEN;
  if (status === 404) return ErrorCategory.NOT_FOUND("Resource");
  if (status === 429) return ErrorCategory.RATE_LIMIT;
  if (gaxios.code === "ENOTFOUND" || gaxios.code === "ECONNREFUSED") {
    return ErrorCategory.NETWORK;
  }
  return sanitizeMessage(error.message);
}
async function safeToolHandler(fn) {
  try {
    return await fn();
  } catch (error) {
    if (error instanceof Error) {
      return toolError(categorizeError(error));
    }
    return toolError("Unknown error");
  }
}

// src/utils/format.ts
function extractTime(dt) {
  if (!dt) return null;
  return dt.dateTime ?? dt.date ?? null;
}
function truncate(text, max) {
  if (!text) return null;
  if (text.length <= max) return text;
  return text.slice(0, max) + "...";
}
function formatEvent(event) {
  return {
    id: event.id ?? null,
    summary: event.summary ?? null,
    start: extractTime(event.start),
    end: extractTime(event.end),
    location: event.location ?? null,
    description: truncate(event.description, 200),
    attendeesCount: event.attendees?.length ?? 0,
    status: event.status ?? null,
    hangoutLink: event.hangoutLink ?? null
  };
}
function formatEventDetail(event) {
  return {
    id: event.id ?? null,
    summary: event.summary ?? null,
    start: extractTime(event.start),
    end: extractTime(event.end),
    location: event.location ?? null,
    description: event.description ?? null,
    attendees: (event.attendees ?? []).map((a) => ({
      email: a.email ?? "",
      responseStatus: a.responseStatus ?? "needsAction"
    })),
    recurrence: event.recurrence ?? null,
    reminders: event.reminders ?? null,
    status: event.status ?? null,
    hangoutLink: event.hangoutLink ?? null
  };
}
function formatCalendar(cal) {
  return {
    id: cal.id ?? null,
    summary: cal.summary ?? null,
    primary: cal.primary ?? false,
    timeZone: cal.timeZone ?? null,
    backgroundColor: cal.backgroundColor ?? null,
    accessRole: cal.accessRole ?? null
  };
}
function buildPagination(nextPageToken) {
  return {
    next_page_token: nextPageToken ?? null,
    has_more: !!nextPageToken
  };
}
function formatFreeBusy(data) {
  const calendars = {};
  for (const [calId, calData] of Object.entries(data.calendars ?? {})) {
    calendars[calId] = {
      busy: (calData.busy ?? []).map((block) => ({ start: block.start ?? null, end: block.end ?? null }))
    };
  }
  return { calendars };
}

// src/tools/list-calendars.ts
function registerListCalendars(server, calendar) {
  server.tool(
    "calendar_list_calendars",
    "List all calendars the user has access to. Returns id, name, timezone, color, and access role (owner/reader/writer).",
    {},
    () => safeToolHandler(async () => {
      const response = await calendar.calendarList.list();
      const calendars = (response.data.items ?? []).map(formatCalendar);
      return {
        content: [{ type: "text", text: JSON.stringify({ calendars }, null, 2) }]
      };
    })
  );
}

// src/tools/list-events.ts
import { z } from "zod";
function registerListEvents(server, calendar) {
  server.tool(
    "calendar_list_events",
    "List events from a calendar within a date range. Defaults to next 7 days from primary calendar. Recurring events are expanded into individual instances. Use page_token for pagination.",
    {
      calendar_id: z.string().default("primary").describe("Calendar ID to list events from"),
      time_min: z.string().optional().describe("Start of time range (ISO 8601). Defaults to now"),
      time_max: z.string().optional().describe("End of time range (ISO 8601). Defaults to 7 days from time_min"),
      max_results: z.number().min(1).max(2500).default(10).describe("Max events per page (1-2500)"),
      page_token: z.string().optional().describe("Page token for fetching next page")
    },
    (params) => safeToolHandler(async () => {
      const timeMin = params.time_min ?? (/* @__PURE__ */ new Date()).toISOString();
      const timeMax = params.time_max ?? new Date(new Date(timeMin).getTime() + 7 * 24 * 60 * 60 * 1e3).toISOString();
      const response = await calendar.events.list({
        calendarId: params.calendar_id,
        timeMin,
        timeMax,
        maxResults: params.max_results,
        singleEvents: true,
        orderBy: "startTime",
        pageToken: params.page_token
      });
      const events = (response.data.items ?? []).map(formatEvent);
      return {
        content: [{
          type: "text",
          text: JSON.stringify({ events, ...buildPagination(response.data.nextPageToken) }, null, 2)
        }]
      };
    })
  );
}

// src/tools/get-event.ts
import { z as z2 } from "zod";
function registerGetEvent(server, calendar) {
  server.tool(
    "calendar_get_event",
    "Get full details of a calendar event by ID. Returns description, attendees with RSVP status, recurrence rules, and reminders. Use calendar_list_events or calendar_search_events to find event IDs.",
    {
      calendar_id: z2.string().default("primary").describe("Calendar ID containing the event"),
      event_id: z2.string().describe("Event ID to retrieve")
    },
    (params) => safeToolHandler(async () => {
      const response = await calendar.events.get({
        calendarId: params.calendar_id,
        eventId: params.event_id
      });
      return {
        content: [{
          type: "text",
          text: JSON.stringify(formatEventDetail(response.data), null, 2)
        }]
      };
    })
  );
}

// src/tools/search-events.ts
import { z as z3 } from "zod";
function registerSearchEvents(server, calendar) {
  server.tool(
    "calendar_search_events",
    "Search events by text query across summaries, descriptions, locations, and attendee names. Searches all time by default. Use time_min/time_max to limit the range. Use page_token for pagination.",
    {
      calendar_id: z3.string().default("primary").describe("Calendar ID to search"),
      query: z3.string().describe("Free-text search query"),
      time_min: z3.string().optional().describe("Optional start of time range (ISO 8601)"),
      time_max: z3.string().optional().describe("Optional end of time range (ISO 8601)"),
      max_results: z3.number().min(1).max(2500).default(10).describe("Max events per page (1-2500)"),
      page_token: z3.string().optional().describe("Page token for fetching next page")
    },
    (params) => safeToolHandler(async () => {
      const response = await calendar.events.list({
        calendarId: params.calendar_id,
        q: params.query,
        timeMin: params.time_min,
        timeMax: params.time_max,
        maxResults: params.max_results,
        singleEvents: true,
        orderBy: "startTime",
        pageToken: params.page_token
      });
      const events = (response.data.items ?? []).map(formatEvent);
      return {
        content: [{
          type: "text",
          text: JSON.stringify({ events, ...buildPagination(response.data.nextPageToken) }, null, 2)
        }]
      };
    })
  );
}

// src/tools/get-freebusy.ts
import { z as z4 } from "zod";
function registerGetFreebusy(server, calendar) {
  server.tool(
    "calendar_get_freebusy",
    "Check free/busy availability for one or more calendars within a time range. Returns busy time blocks per calendar. Calendars with no events return empty busy arrays.",
    {
      time_min: z4.string().describe("Start of time range (ISO 8601)"),
      time_max: z4.string().describe("End of time range (ISO 8601)"),
      calendar_ids: z4.array(z4.string()).default(["primary"]).describe("Calendar IDs to check availability for")
    },
    (params) => safeToolHandler(async () => {
      const response = await calendar.freebusy.query({
        requestBody: {
          timeMin: params.time_min,
          timeMax: params.time_max,
          items: params.calendar_ids.map((id) => ({ id }))
        }
      });
      return {
        content: [{
          type: "text",
          text: JSON.stringify(formatFreeBusy(response.data), null, 2)
        }]
      };
    })
  );
}

// src/tools/create-event.ts
import { z as z5 } from "zod";

// src/utils/date.ts
var ALL_DAY_REGEX = /^\d{4}-\d{2}-\d{2}$/;
var TIMED_REGEX = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)$/;
function isAllDay(value) {
  return !value.includes("T");
}
function validateDateInput(value) {
  if (ALL_DAY_REGEX.test(value) || TIMED_REGEX.test(value)) {
    return;
  }
  throw new Error(
    `Invalid date format: '${value}'. Expected ISO 8601: '2026-03-20' (all-day) or '2026-03-20T14:00:00-03:00' (timed).`
  );
}
function buildEventDateTime(value) {
  if (isAllDay(value)) {
    return { date: value };
  }
  return { dateTime: value };
}
function calculateAllDayEnd(startDate) {
  const date = /* @__PURE__ */ new Date(startDate + "T00:00:00Z");
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString().split("T")[0];
}

// src/utils/validation.ts
function validateEmail(email) {
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    throw new Error(`Invalid email address: '${email}'`);
  }
}
function validateNotEmpty(value, field) {
  if (!value.trim()) {
    throw new Error(`Field '${field}' cannot be empty`);
  }
}
function validateTimeRange(start, end) {
  if (new Date(start).getTime() >= new Date(end).getTime()) {
    throw new Error(`Event start must be before end: start='${start}' end='${end}'`);
  }
}
function validateDateTypeMatch(start, end) {
  if (isAllDay(start) !== isAllDay(end)) {
    throw new Error("Start and end must both be dates or both be date-times.");
  }
}

// src/tools/create-event.ts
function registerCreateEvent(server, calendar) {
  server.tool(
    "calendar_create_event",
    "Create a new calendar event. Supports timed events (ISO 8601 with timezone) and all-day events (date string). When attendees are provided, Google sends invitation emails automatically (unless send_notifications is false).",
    {
      calendar_id: z5.string().default("primary").describe("Calendar ID to create event in"),
      summary: z5.string().describe("Event title"),
      start: z5.string().describe('Start time (ISO 8601). All-day: "2026-03-25". Timed: "2026-03-25T14:00:00-03:00"'),
      end: z5.string().optional().describe("End time (ISO 8601). Required for timed events. All-day: auto-calculated as start+1 day if omitted"),
      description: z5.string().optional().describe("Event description"),
      location: z5.string().optional().describe("Event location"),
      attendees: z5.array(z5.string()).optional().describe("Attendee email addresses"),
      recurrence: z5.array(z5.string()).optional().describe("RRULE strings for recurring events"),
      timezone: z5.string().optional().describe('IANA timezone (e.g. "America/Sao_Paulo")'),
      reminders: z5.array(z5.object({
        method: z5.enum(["email", "popup"]),
        minutes: z5.number().int().min(0).max(40320)
      })).optional().describe("Override reminders"),
      visibility: z5.enum(["default", "public", "private", "confidential"]).optional().describe("Event visibility"),
      color_id: z5.string().optional().describe("Google Calendar color ID"),
      send_notifications: z5.boolean().default(true).describe("Send invitation emails to attendees")
    },
    (params) => safeToolHandler(async () => {
      validateNotEmpty(params.summary, "summary");
      validateDateInput(params.start);
      let end = params.end;
      if (end) {
        validateDateInput(end);
        validateDateTypeMatch(params.start, end);
        if (!isAllDay(params.start)) {
          validateTimeRange(params.start, end);
        }
      } else if (isAllDay(params.start)) {
        end = calculateAllDayEnd(params.start);
      } else {
        throw new Error("End time is required for timed events. Provide an end time or use date-only format for all-day events.");
      }
      if (params.attendees) {
        for (const email of params.attendees) {
          validateEmail(email);
        }
      }
      const startDt = { ...buildEventDateTime(params.start), ...params.timezone ? { timeZone: params.timezone } : {} };
      const endDt = { ...buildEventDateTime(end), ...params.timezone ? { timeZone: params.timezone } : {} };
      const requestBody = {
        summary: params.summary,
        start: startDt,
        end: endDt
      };
      if (params.description) requestBody.description = params.description;
      if (params.location) requestBody.location = params.location;
      if (params.attendees) requestBody.attendees = params.attendees.map((email) => ({ email }));
      if (params.recurrence) requestBody.recurrence = params.recurrence;
      if (params.reminders) {
        requestBody.reminders = { useDefault: false, overrides: params.reminders };
      }
      if (params.visibility) requestBody.visibility = params.visibility;
      if (params.color_id) requestBody.colorId = params.color_id;
      const response = await calendar.events.insert({
        calendarId: params.calendar_id ?? "primary",
        sendUpdates: params.send_notifications ? "all" : "none",
        requestBody
      });
      return {
        content: [{ type: "text", text: JSON.stringify(formatEventDetail(response.data), null, 2) }]
      };
    })
  );
}

// src/tools/update-event.ts
import { z as z6 } from "zod";
function registerUpdateEvent(server, calendar) {
  server.tool(
    "calendar_update_event",
    "Update an existing calendar event. Only specified fields are changed (patch semantics). Omitted fields remain unchanged. Use calendar_list_events or calendar_search_events to find event IDs.",
    {
      calendar_id: z6.string().default("primary").describe("Calendar ID containing the event"),
      event_id: z6.string().describe("Event ID to update"),
      summary: z6.string().optional().describe("New event title"),
      start: z6.string().optional().describe("New start time (ISO 8601)"),
      end: z6.string().optional().describe("New end time (ISO 8601)"),
      description: z6.string().optional().describe("New description"),
      location: z6.string().optional().describe("New location"),
      attendees: z6.array(z6.string()).optional().describe("Replace attendee list (email addresses)"),
      recurrence: z6.array(z6.string()).optional().describe("Replace recurrence rules"),
      timezone: z6.string().optional().describe("IANA timezone"),
      reminders: z6.array(z6.object({
        method: z6.enum(["email", "popup"]),
        minutes: z6.number().int().min(0).max(40320)
      })).optional().describe("Replace reminders"),
      visibility: z6.enum(["default", "public", "private", "confidential"]).optional().describe("New visibility"),
      color_id: z6.string().optional().describe("New color ID"),
      send_notifications: z6.boolean().default(true).describe("Send update emails to attendees")
    },
    (params) => safeToolHandler(async () => {
      if (params.summary !== void 0) validateNotEmpty(params.summary, "summary");
      if (params.start) validateDateInput(params.start);
      if (params.end) validateDateInput(params.end);
      if (params.start && params.end) {
        validateDateTypeMatch(params.start, params.end);
        if (!isAllDay(params.start)) {
          validateTimeRange(params.start, params.end);
        }
      }
      if (params.attendees) {
        for (const email of params.attendees) {
          validateEmail(email);
        }
      }
      const requestBody = {};
      if (params.summary !== void 0) requestBody.summary = params.summary;
      if (params.start) {
        requestBody.start = { ...buildEventDateTime(params.start), ...params.timezone ? { timeZone: params.timezone } : {} };
      }
      if (params.end) {
        requestBody.end = { ...buildEventDateTime(params.end), ...params.timezone ? { timeZone: params.timezone } : {} };
      }
      if (params.description !== void 0) requestBody.description = params.description;
      if (params.location !== void 0) requestBody.location = params.location;
      if (params.attendees) requestBody.attendees = params.attendees.map((email) => ({ email }));
      if (params.recurrence) requestBody.recurrence = params.recurrence;
      if (params.reminders) {
        requestBody.reminders = { useDefault: false, overrides: params.reminders };
      }
      if (params.visibility) requestBody.visibility = params.visibility;
      if (params.color_id) requestBody.colorId = params.color_id;
      const calendarId = params.calendar_id ?? "primary";
      const response = await calendar.events.patch({
        calendarId,
        eventId: params.event_id,
        sendUpdates: params.send_notifications ?? true ? "all" : "none",
        requestBody
      });
      return {
        content: [{ type: "text", text: JSON.stringify(formatEventDetail(response.data), null, 2) }]
      };
    })
  );
}

// src/tools/delete-event.ts
import { z as z7 } from "zod";
function registerDeleteEvent(server, calendar) {
  server.tool(
    "calendar_delete_event",
    "Delete a calendar event by ID. Google sends cancellation emails to attendees automatically (unless send_notifications is false). Use calendar_list_events or calendar_search_events to find event IDs.",
    {
      calendar_id: z7.string().default("primary").describe("Calendar ID containing the event"),
      event_id: z7.string().describe("Event ID to delete"),
      send_notifications: z7.boolean().default(true).describe("Send cancellation emails to attendees")
    },
    (params) => safeToolHandler(async () => {
      const calendarId = params.calendar_id ?? "primary";
      const sendNotifications = params.send_notifications ?? true;
      let summary;
      try {
        const event = await calendar.events.get({
          calendarId,
          eventId: params.event_id
        });
        summary = event.data.summary ?? "(no title)";
      } catch (error) {
        const gaxios = error;
        if (gaxios.response?.status === 404) {
          return toolError(`Event not found: eventId='${params.event_id}' in calendar '${calendarId}'`);
        }
        throw error;
      }
      await calendar.events.delete({
        calendarId,
        eventId: params.event_id,
        sendUpdates: sendNotifications ? "all" : "none"
      });
      return {
        content: [{
          type: "text",
          text: JSON.stringify({ deleted: true, event_id: params.event_id, summary }, null, 2)
        }]
      };
    })
  );
}

// src/tools/register.ts
function registerTools(server, calendar) {
  registerListCalendars(server, calendar);
  registerListEvents(server, calendar);
  registerGetEvent(server, calendar);
  registerSearchEvents(server, calendar);
  registerGetFreebusy(server, calendar);
  registerCreateEvent(server, calendar);
  registerUpdateEvent(server, calendar);
  registerDeleteEvent(server, calendar);
}

// src/server.ts
var require2 = createRequire(import.meta.url);
var { version } = require2("../package.json");
function createServer(client) {
  const server = new McpServer({
    name: "calendar-mcp-server",
    version
  });
  registerTools(server, client.calendar);
  return server;
}

// src/index.ts
async function main() {
  const credentials = validateEnv();
  const client = createCalendarClient(credentials);
  await validateCredentials(client.calendar);
  const server = createServer(client);
  const transport = new StdioServerTransport();
  await server.connect(transport);
  logger.info("Calendar MCP Server started");
  const gracefulShutdown = async () => {
    logger.info("Calendar MCP Server shutting down");
    await server.close();
    process.exit(0);
  };
  process.on("SIGINT", gracefulShutdown);
  process.on("SIGTERM", gracefulShutdown);
}
main().catch((error) => {
  logger.error(
    error instanceof Error ? error.message : "Unknown error"
  );
  process.exit(1);
});
