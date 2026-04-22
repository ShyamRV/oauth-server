import base64
import hashlib
import os
from typing import Dict

import httpx
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()


GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")

LINKEDIN_CLIENT_ID = os.environ.get("LINKEDIN_CLIENT_ID", "")
LINKEDIN_CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
LINKEDIN_REDIRECT_URI = os.environ.get("LINKEDIN_REDIRECT_URI", "")

SHARED_SECRET = os.environ.get("SHARED_SECRET", "dev-secret")
CHAT_APP_BASE_URL = os.environ.get("CHAT_APP_BASE_URL", "https://asi1.ai")
AUTO_REDIRECT_SUCCESS = os.environ.get("AUTO_REDIRECT_SUCCESS", "false").lower() == "true"

_state_map: Dict[str, Dict] = {}
_used_states: set[str] = set()
_token_store: Dict[str, Dict] = {}


def _success_html(platform: str, chat_url: str) -> str:
    redirect_block = ""
    if AUTO_REDIRECT_SUCCESS:
        redirect_block = f"""
        <p>Redirecting back to chat...</p>
        <script>
        setTimeout(function() {{
            window.location.href = "{chat_url}";
        }}, 1500);
        </script>
        """
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{platform} Connected</title>
  <style>
    body {{
      margin: 0;
      font-family: Arial, sans-serif;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
    }}
    .card {{
      background: white;
      border-radius: 16px;
      padding: 32px;
      max-width: 460px;
      box-shadow: 0 20px 40px rgba(0,0,0,0.2);
      text-align: center;
    }}
    .icon {{
      font-size: 52px;
      margin-bottom: 10px;
    }}
    h1 {{ margin: 8px 0 12px; }}
    a {{
      display: inline-block;
      margin-top: 8px;
      text-decoration: none;
      color: #4f46e5;
      font-weight: bold;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">✅</div>
    <h1>{platform} Connected!</h1>
    <p>You can safely close this page.</p>
    <a href="{chat_url}">Return to chat</a>
    {redirect_block}
  </div>
</body>
</html>"""


def _error_html(message: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Authentication Error</title>
  <style>
    body {{
      margin: 0;
      font-family: Arial, sans-serif;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: linear-gradient(135deg, #dc2626, #ef4444);
    }}
    .card {{
      background: white;
      border-radius: 16px;
      padding: 32px;
      max-width: 520px;
      box-shadow: 0 20px 40px rgba(0,0,0,0.2);
      text-align: center;
    }}
    .icon {{
      font-size: 52px;
      margin-bottom: 10px;
    }}
    h1 {{ margin: 8px 0 12px; }}
    p {{ color: #333; white-space: pre-wrap; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">❌</div>
    <h1>Authentication Error</h1>
    <p>{message}</p>
  </div>
</body>
</html>"""


def _sender_from_state_fallback(state: str) -> str:
    # This lets callback still recover sender after server restarts as long as
    # the signed state format is intact.
    try:
        parts = state.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            return ""
        b64_payload = parts[1]
        sig = parts[2]
        padding = "=" * (-len(b64_payload) % 4)
        payload = base64.urlsafe_b64decode((b64_payload + padding).encode()).decode()
        expected = hashlib.sha256(f"{payload}|{SHARED_SECRET}".encode()).hexdigest()[:24]
        if expected != sig:
            return ""
        sender = payload.split("|", 1)[0].strip()
        return sender
    except Exception:
        return ""


def _require_secret(request: web.Request) -> bool:
    return request.headers.get("X-Secret", "") == SHARED_SECRET


def _provider_norm(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p == "youtube":
        return "yt"
    if p == "linkedin":
        return "li"
    return p


async def health(_request: web.Request) -> web.Response:
    return web.Response(
        text="AI Social Media Army OAuth Server — Running",
        content_type="text/html",
    )


async def register_state(request: web.Request) -> web.Response:
    if not _require_secret(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)
    state = (body.get("state") or "").strip()
    sender = (body.get("sender") or "").strip()
    provider = _provider_norm(body.get("provider", ""))
    return_url = (body.get("return_url") or CHAT_APP_BASE_URL).strip()
    if not state or not sender or provider not in {"yt", "li"}:
        return web.json_response({"ok": False, "error": "bad payload"}, status=400)
    _state_map[state] = {"sender": sender, "provider": provider, "return_url": return_url}
    return web.json_response({"ok": True})


async def token_lookup(request: web.Request) -> web.Response:
    if not _require_secret(request):
        return web.json_response({"access_token": "", "refresh_token": ""}, status=401)
    sender = (request.query.get("sender") or "").strip()
    provider = _provider_norm(request.query.get("provider", ""))
    slot = _token_store.get(sender, {})
    if provider == "yt":
        return web.json_response(
            {"access_token": slot.get("yt", ""), "refresh_token": slot.get("yt_refresh", "")}
        )
    if provider == "li":
        return web.json_response(
            {"access_token": slot.get("li", ""), "refresh_token": slot.get("li_refresh", "")}
        )
    return web.json_response({"access_token": "", "refresh_token": ""})


async def _exchange_google_code(code: str) -> Dict:
    payload = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://oauth2.googleapis.com/token", data=payload)
    r.raise_for_status()
    return r.json()


async def _exchange_linkedin_code(code: str) -> Dict:
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": LINKEDIN_CLIENT_ID,
        "client_secret": LINKEDIN_CLIENT_SECRET,
        "redirect_uri": LINKEDIN_REDIRECT_URI,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://www.linkedin.com/oauth/v2/accessToken", data=payload)
    r.raise_for_status()
    return r.json()


async def callback_youtube(request: web.Request) -> web.Response:
    if request.query.get("error"):
        return web.Response(
            text=_error_html(f"Google error: {request.query.get('error')}"),
            content_type="text/html",
        )
    code = (request.query.get("code") or "").strip()
    state = (request.query.get("state") or "").strip()
    if not code or not state:
        return web.Response(text=_error_html("Missing code or state"), content_type="text/html")

    meta = _state_map.get(state) or {}
    sender = meta.get("sender") or _sender_from_state_fallback(state)
    return_url = meta.get("return_url") or CHAT_APP_BASE_URL

    if state in _used_states:
        return web.Response(
            text=_success_html("YouTube", return_url),
            content_type="text/html",
        )

    if not sender:
        return web.Response(
            text=_error_html("Unknown or expired state; please retry auth from chat."),
            content_type="text/html",
        )

    try:
        token_data = await _exchange_google_code(code)
        bucket = _token_store.setdefault(sender, {})
        bucket["yt"] = token_data.get("access_token", "")
        bucket["yt_refresh"] = token_data.get("refresh_token", "")
        _used_states.add(state)
        return web.Response(text=_success_html("YouTube", return_url), content_type="text/html")
    except Exception as exc:
        return web.Response(text=_error_html(str(exc)), content_type="text/html")


async def callback_linkedin(request: web.Request) -> web.Response:
    if request.query.get("error"):
        return web.Response(
            text=_error_html(f"LinkedIn error: {request.query.get('error')}"),
            content_type="text/html",
        )
    code = (request.query.get("code") or "").strip()
    state = (request.query.get("state") or "").strip()
    if not code or not state:
        return web.Response(text=_error_html("Missing code or state"), content_type="text/html")

    meta = _state_map.get(state) or {}
    sender = meta.get("sender") or _sender_from_state_fallback(state)
    return_url = meta.get("return_url") or CHAT_APP_BASE_URL

    if state in _used_states:
        return web.Response(
            text=_success_html("LinkedIn", return_url),
            content_type="text/html",
        )

    if not sender:
        return web.Response(
            text=_error_html("Unknown or expired state; please retry auth from chat."),
            content_type="text/html",
        )

    try:
        token_data = await _exchange_linkedin_code(code)
        bucket = _token_store.setdefault(sender, {})
        bucket["li"] = token_data.get("access_token", "")
        bucket["li_refresh"] = token_data.get("refresh_token", "")
        _used_states.add(state)
        return web.Response(text=_success_html("LinkedIn", return_url), content_type="text/html")
    except Exception as exc:
        return web.Response(text=_error_html(str(exc)), content_type="text/html")


def _build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post("/register", register_state)
    app.router.add_get("/token", token_lookup)
    app.router.add_get("/callback/youtube", callback_youtube)
    app.router.add_get("/callback/linkedin", callback_linkedin)
    return app


if __name__ == "__main__":
    app = _build_app()
    port = int(os.environ.get("PORT", 8001))
    web.run_app(app, host="0.0.0.0", port=port)
