#!/usr/bin/env python3
"""
Gmail OAuth Authentication Utility
Generates a fresh OAuth refresh token and automatically updates:
1. ~/.gmail-mcp/token.json (used by gmail-mcp-server)
2. ~/.gmail-mcp/credentials.json
3. .env (GMAIL_REFRESH_TOKEN)
"""

import os
import sys
import json
import re
from google_auth_oauthlib.flow import InstalledAppFlow
from dotenv import load_dotenv

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels"
]

def main():
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    env_file = os.path.join(repo_dir, ".env")
    load_dotenv(env_file)

    client_id = os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GMAIL_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("❌ Error: GMAIL_CLIENT_ID or GMAIL_CLIENT_SECRET missing in .env")
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"]
        }
    }

    print("\n🔐 ===================================================")
    print("   Gmail OAuth 2.0 Authentication Setup")
    print("===================================================\n")
    print("A browser window will open automatically.")
    print("Please select your Google account (savaliyapriyank19@gmail.com)")
    print("and click 'Allow' to grant Gmail permissions.\n")

    try:
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

        print("\n✅ Authentication Successful!")
        print(f"Refreshed Token: {creds.refresh_token[:15]}...")

        # 1. Update ~/.gmail-mcp/token.json
        config_dir = os.path.expanduser("~/.gmail-mcp")
        os.makedirs(config_dir, exist_ok=True)
        token_file = os.path.join(config_dir, "token.json")

        token_data = {
            "access_token": creds.token,
            "refresh_token": creds.refresh_token,
            "scope": " ".join(SCOPES),
            "token_type": "Bearer",
            "expiry_date": int(creds.expiry.timestamp() * 1000) if creds.expiry else None
        }
        with open(token_file, "w") as f:
            json.dump(token_data, f, indent=2)
        print(f"✅ Saved token to: {token_file}")

        # 2. Update ~/.gmail-mcp/credentials.json
        creds_file = os.path.join(config_dir, "credentials.json")
        with open(creds_file, "w") as f:
            json.dump(client_config, f, indent=2)
        print(f"✅ Saved credentials to: {creds_file}")

        # 3. Update .env
        if os.path.exists(env_file) and creds.refresh_token:
            with open(env_file, "r") as f:
                content = f.read()

            if "GMAIL_REFRESH_TOKEN=" in content:
                new_content = re.sub(
                    r"GMAIL_REFRESH_TOKEN=.*",
                    f"GMAIL_REFRESH_TOKEN={creds.refresh_token}",
                    content
                )
            else:
                new_content = content + f"\nGMAIL_REFRESH_TOKEN={creds.refresh_token}\n"

            with open(env_file, "w") as f:
                f.write(new_content)
            print(f"✅ Updated GMAIL_REFRESH_TOKEN in: {env_file}")

        print("\n🎉 Gmail is now fully authenticated and ready to send emails!")

    except Exception as e:
        print(f"\n❌ Authentication failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
