"""One-time YouTube OAuth flow to obtain a refresh token.

Run locally:
    python -m phonksync youtube-auth --client-json client_secret.json

Opens the browser, you approve access, and the refresh token is printed.
Store it as the YT_REFRESH_TOKEN GitHub secret. Requires only `requests`.
"""
from __future__ import annotations

import http.server
import json
import os
import secrets as pysecrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

_SCOPE = "https://www.googleapis.com/auth/youtube"
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Device-flow scope: the narrower one is what desktop/CLI apps are allowed to ask
# for (youtube.force-ssl is also accepted for the same write endpoints).
_DEVICE_SCOPE = "https://www.googleapis.com/auth/youtube"


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


_CLIENT_ID_SUFFIX = ".apps.googleusercontent.com"


def is_client_id(value: str) -> bool:
    """Google client ids: <digits/letters>-<chars>.apps.googleusercontent.com."""
    value = (value or "").strip()
    if " " in value or not value.endswith(_CLIENT_ID_SUFFIX):
        return False
    head = value[: -len(_CLIENT_ID_SUFFIX)]
    return len(head) >= 20 and ("-" in head)


def client_section(
    client_json: str = "", client_id: str = "", client_secret: str = ""
) -> dict:
    """Build the OAuth client config from whatever the user actually has.

    Accepted, in order of preference:

    1. a path to `client_secret.json`
    2. the JSON itself, pasted as one string
    3. a bare **client id** (+ optional secret) — the only thing you can read
       straight off the Cloud Console credentials row
    """
    raw = (client_json or "").strip()
    if raw and not raw.startswith("{"):
        path = Path(raw)
        if path.exists():
            raw = path.read_text(encoding="utf-8")
        else:
            raise FileNotFoundError(raw)
    if raw:
        blob = json.loads(raw)
        section = blob.get("installed") or blob.get("web") or blob
        if not section.get("client_id"):
            raise ValueError(
                "That JSON has no client_id — is it the OAuth client file? "
                "(It should contain \"installed\" or \"web\".)"
            )
        section.setdefault("token_uri", _TOKEN_URL)
        return section

    client_id = (client_id or "").strip()
    if not client_id:
        raise ValueError(
            "No OAuth client given. Pass --client-json <file>, --client-id <id>, "
            "or --client-id <id> --client-secret <secret>."
        )
    if not is_client_id(client_id):
        raise ValueError(
            f"{client_id!r} does not look like a Google OAuth client id "
            "(it ends with .apps.googleusercontent.com)."
        )
    return {
        "client_id": client_id,
        "client_secret": (client_secret or "").strip(),
        "token_uri": _TOKEN_URL,
    }


def _client_section(client_json_path: str) -> dict:
    """Back-compat wrapper: a JSON path/string, falling back to an id."""
    return client_section(client_json=client_json_path)


def _print_secrets(client_id: str, client_secret: str, token_uri: str, refresh_token: str) -> None:
    condensed = json.dumps(
        {"installed": {"client_id": client_id, "client_secret": client_secret,
                       "token_uri": token_uri, "redirect_uris": ["http://localhost"]}},
        separators=(",", ":"),
    )
    print("\n✅ Success! Add these GitHub repository secrets (Settings → Secrets → Actions):\n")
    print("  YT_REFRESH_TOKEN =")
    print(f"  {refresh_token}\n")
    print("  YT_CLIENT_JSON = (either your whole client_secret.json, or this one-liner):")
    print(f"  {condensed}\n")
    print("⚠️  The refresh token does not expire on its own. It only dies if you")
    print("   revoke the app, change the Google account password, or leave the app")
    print("   in \"Testing\" mode for more than 7 days (then: publish the app, or")
    print("   re-run this once). Treat it like a password — never commit it.")


def run_device_flow(client_json_path: str) -> int:
    """OAuth without any redirect URI: paste a code on a Google page.

    This is the flow to use when Cloud Console refuses the redirect URI or when
    the run happens on a machine without a browser.
    """
    try:
        section = client_section(client_json_path)
    except FileNotFoundError:
        print(f"❌ File not found: {client_json_path}")
        return 2
    except ValueError as exc:
        print(f"❌ {exc}")
        return 2
    client_id = section["client_id"]
    client_secret = section.get("client_secret", "")
    token_uri = section.get("token_uri", _TOKEN_URL)

    resp = requests.post(
        _DEVICE_CODE_URL,
        data={"client_id": client_id, "scope": _DEVICE_SCOPE},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"❌ Device flow is not enabled for this client ({resp.status_code}).")
        print("   In Cloud Console → OAuth client → it must be type \"TVs and Limited")
        print("   Input devices\", or use `youtube-auth --auth-url` / `--code`.")
        print(f"   {resp.text[:300]}")
        return 2
    data = resp.json()
    device_code = data.get("device_code", "")
    user_code = data.get("user_code", "")
    verify_url = data.get("verification_url") or data.get("verification_uri", "")
    interval = int(data.get("interval", 5))
    expires_in = int(data.get("expires_in", 1800))
    print("\n1) Open this page in any browser:")
    print(f"   {verify_url}")
    print(f"2) Enter this code:  {user_code}\n")
    print("Waiting for you to approve ...")
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        poll = requests.post(
            _TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=30,
        )
        payload = poll.json() if poll.content else {}
        error = payload.get("error", "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "invalid_client" and not client_secret:
            print("❌ Google says this client needs a **client secret** too.")
            print("   Cloud Console → APIs & Services → Credentials → your OAuth client")
            print("   → the 'Client secret' value (or 'Download JSON'). Then re-run with")
            print("   `--client-id <id> --client-secret <secret> --device`.")
            return 1
        if error:
            print(f"❌ {error}: {payload.get('error_description', '')}")
            return 1
        refresh_token = payload.get("refresh_token", "")
        if not refresh_token:
            print("❌ Google returned no refresh token.")
            return 1
        _print_secrets(client_id, client_secret, token_uri, refresh_token)
        return 0
    print("❌ Timed out waiting for approval.")
    return 1


def auth_url(client_json_path: str, redirect_uri: str = "http://localhost:8765/callback") -> int:
    """Print the consent URL for a redirect URI you registered yourself."""
    try:
        section = client_section(client_json_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"❌ {exc}")
        return 2
    url = _AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": section["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    print("1) Register this exact redirect URI on the OAuth client (Cloud Console):")
    print(f"   {redirect_uri}")
    print("2) Open this URL, approve, then copy the `code=` value from the address")
    print("   bar you land on (the page itself may show an error — that is fine):\n")
    print(url + "\n")
    print("3) Exchange it:")
    print(f"   python -m phonksync youtube-auth --client-json {client_json_path} \\")
    print(f"       --code <CODE> --redirect-uri {redirect_uri}")
    return 0


def exchange_code(client_json_path: str, code: str, redirect_uri: str) -> int:
    """Turn a pasted authorization code into a refresh token."""
    try:
        section = client_section(client_json_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"❌ {exc}")
        return 2
    if not section.get("client_secret"):
        print("❌ Exchanging a code needs the client **secret** as well as the id.")
        print("   Cloud Console → Credentials → your OAuth client → 'Client secret',")
        print("   then re-run with --client-id <id> --client-secret <secret> --code ...")
        return 2
    token_uri = section.get("token_uri", _TOKEN_URL)
    resp = requests.post(
        token_uri,
        data={
            "code": code.strip(),
            "client_id": section["client_id"],
            "client_secret": section["client_secret"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"❌ Token exchange failed: {resp.status_code} {resp.text[:300]}")
        print("   The usual causes: the redirect URI does not match EXACTLY the one")
        print("   used in the consent URL, the code was already used, or it is older")
        print("   than 10 minutes.")
        return 1
    data = resp.json()
    refresh_token = data.get("refresh_token", "")
    if not refresh_token:
        print("❌ Google returned no refresh token (it only sends one on the first")
        print("   consent for a client). Revoke the app at")
        print("   https://myaccount.google.com/permissions and run this again.")
        return 1
    _print_secrets(
        section["client_id"], section["client_secret"], token_uri, refresh_token
    )
    return 0


def run_oauth_flow(client_json_path: str, port: int = 8765, redirect_uri: str = "") -> int:
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

    redirect_uri = redirect_uri or f"http://127.0.0.1:{port}/callback"
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

    bind_host = "127.0.0.1"
    server = http.server.HTTPServer((bind_host, port), _Handler)
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

    _print_secrets(client_id, client_secret, token_uri, refresh_token)
    return 0


def doctor(client_json: str = "", client_id: str = "", client_secret: str = "") -> int:
    """Say exactly what is missing for YouTube writes — and nothing else."""
    print("🩺 YouTube write setup\n")
    ok = True
    if client_json or client_id:
        try:
            section = client_section(client_json, client_id, client_secret)
        except (FileNotFoundError, ValueError) as exc:
            print(f"❌ OAuth client: {exc}")
            return 2
        kind = "client JSON" if (client_json or "").strip().startswith("{") or "\"installed\"" in (client_json or "") else "client id"
        print(f"✅ OAuth client: {section['client_id'][:18]}… ({kind})")
        if not section.get("client_secret"):
            print("⚠️  No client secret — the device flow can work without one, but")
            print("   exchanging an authorization code cannot. Console → Credentials →")
            print("   your OAuth client → 'Client secret'.")
            ok = False
    else:
        print("❌ OAuth client: nothing configured")
        print("   Set YT_CLIENT_ID (…apps.googleusercontent.com) + YT_CLIENT_SECRET,")
        print("   or YT_CLIENT_JSON with the downloaded file.")
        ok = False

    refresh = os.environ.get("YT_REFRESH_TOKEN", "").strip()
    access = os.environ.get("YT_ACCESS_TOKEN", "").strip()
    if refresh:
        print(f"✅ Refresh token present ({refresh[:6]}…, never expires)")
    elif access:
        print("⚠️  Only an access token: writes work ~1 hour, then stop.")
        print("   Run `phonksync youtube-auth --device` once for a refresh token.")
        ok = False
    else:
        print("❌ No refresh token — nothing can be added to the YouTube playlist.")
        print("   Run `phonksync youtube-auth --device` (needs the OAuth client above).")
        ok = False

    if os.environ.get("YOUTUBE_TARGET_PLAYLIST", "").strip():
        print("✅ Target playlist configured")
    else:
        print("⚠️  YOUTUBE_TARGET_PLAYLIST is not set in this environment")

    print("\n" + ("✅ Writes should work." if ok else "👉 Fix the ❌/⚠️ lines above, then re-run `phonksync check`."))
    return 0 if ok else 1
