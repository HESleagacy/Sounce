from __future__ import annotations

import argparse
import json
import secrets
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from app.config import get_settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


def authorize() -> Path:
    settings = get_settings()
    client_id = settings.google_client_id
    client_secret = settings.google_client_secret.get_secret_value()
    if not client_id or not client_secret:
        raise RuntimeError("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET before authorization")
    state = secrets.token_urlsafe(32)
    redirect_uri = f"http://127.0.0.1:{settings.google_oauth_redirect_port}/callback"
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                self.send_error(400, "Invalid OAuth state")
                return
            result["code"] = query.get("code", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Google Calendar connected. You can close this tab.")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": CALENDAR_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    authorization_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print(f"Open this URL in your browser:\n\n{authorization_url}\n")
    webbrowser.open(authorization_url)
    server = HTTPServer(("127.0.0.1", settings.google_oauth_redirect_port), CallbackHandler)
    server.handle_request()
    code = result.get("code")
    if not code:
        raise RuntimeError("Google did not return an authorization code")
    response = httpx.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("refresh_token"):
        raise RuntimeError("Google returned no refresh token; revoke prior consent and try again")
    token_path = settings.google_token_path
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(
        json.dumps({"refresh_token": payload["refresh_token"]}),
        encoding="utf-8",
    )
    token_path.chmod(0o600)
    print(f"Refresh token saved securely to {token_path}")
    return token_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Authorize Sounce for Google Calendar")
    parser.parse_args()
    authorize()


if __name__ == "__main__":
    main()
