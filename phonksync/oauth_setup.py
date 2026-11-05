"""One-time YouTube OAuth flow to obtain a refresh token.

Run locally:
    python -m phonksync youtube-auth --client-json client_secret.json

Opens the browser, you approve access, and the refresh token is printed.
Store it as the YT_REFRESH_TOKEN GitHub secret. Requires only `requests`.
"""
from __future__ import annotations

import http.server
import json
import secrets as pysecrets
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import requests

_SCOPE = "https://www.googleapis.com/auth/youtube"
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


class _Handler(http.server.BaseHTTPRequestHandler):
    code: str | None = None
    state: str | None = None

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        _Handler.code = params.get("code", [None])[0]
        _Handler.state = params.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h2>Done! You can close this tab and return to the terminal.</h2></body></html>"
        )

    def log_message(self, *args):  # silence
        pass


def run_oauth_flow(client_json_path: str, port: int = 8765) -> int:
    path = Path(client_json_path)
    if not path.exists():
        print(f"❌ File not found: {path}")
        print("Download your OAuth client JSON from Google Cloud Console first.")
        return 2
    blob = json.loads(path.read_text(encoding="utf-8"))
    section = blob.get("installed") or blob.get("web") or blob
    client_id = section.get("client_id", "")
    client_secret = section.get("client_secret", "")
    token_uri = section.get("token_uri", "https://oauth2.googleapis.com/token")
    if not client_id or not client_secret:
        print("❌ Client JSON must contain client_id and client_secret")
        return 2

    redirect_uri = f"http://localhost:{port}/callback"
    state = pysecrets.token_urlsafe(16)
    auth_url = _AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )

    server = http.server.HTTPServer(("localhost", port), _Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("\nOpen this URL in your browser and approve access:\n")
    print(auth_url + "\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass
    print(f"Waiting for the redirect on {redirect_uri} ...")
    thread.join(timeout=300)
    server.server_close()

    if not _Handler.code:
        print("❌ No authorization code received (timeout).")
        return 1
    if _Handler.state != state:
        print("❌ State mismatch — aborting for safety.")
        return 1

    resp = requests.post(
        token_uri,
        data={
            "code": _Handler.code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"❌ Token exchange failed: {resp.status_code} {resp.text[:300]}")
        return 1
    data = resp.json()
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        print("❌ Google did not return a refresh token. Revoke the app grant and retry.")
        return 1

    print("\n✅ Success! Add these two GitHub repository secrets:\n")
    print("  YT_REFRESH_TOKEN =")
    print(f"  {refresh_token}\n")
    print("  YT_CLIENT_JSON = (paste the entire contents of your client JSON file)")
    print("\n⚠️  Treat the refresh token like a password. Never commit it to the repo.")
    return 0
