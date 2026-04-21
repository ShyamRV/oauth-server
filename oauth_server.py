"""
Lightweight OAuth callback server — deploy this to Render (free).
Handles YouTube + LinkedIn OAuth2 callbacks.
Stores tokens in memory (use Redis add-on for persistence across restarts).
"""

import os
import hashlib
import logging
from aiohttp import web
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("oauth-server")

# ── Config from env vars ──────────────────────────────────────────────────────
GOOGLE_CLIENT_ID       = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET   = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REDIRECT_URI    = os.environ["GOOGLE_REDIRECT_URI"]

LINKEDIN_CLIENT_ID     = os.environ["LINKEDIN_CLIENT_ID"]
LINKEDIN_CLIENT_SECRET = os.environ["LINKEDIN_CLIENT_SECRET"]
LINKEDIN_REDIRECT_URI  = os.environ["LINKEDIN_REDIRECT_URI"]

CHAT_APP_BASE_URL      = os.environ.get("CHAT_APP_BASE_URL", "https://asi1.ai")
SHARED_SECRET          = os.environ["SHARED_SECRET"]   # random string you generate

# ── In-memory stores ──────────────────────────────────────────────────────────
# state_param → sender_address
_state_map: dict[str, str] = {}

# sender_address → {yt, yt_refresh, li}
_token_store: dict[str, dict] = {}


# ── HTML templates ────────────────────────────────────────────────────────────
def _success_html(platform: str, chat_url: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{platform} Connected</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:linear-gradient(135deg,#6366f1,#4f46e5);
  min-height:100vh; display:flex; align-items:center; justify-content:center;
}}
.card {{ background:#fff; border-radius:16px; padding:40px; max-width:400px;
         text-align:center; box-shadow:0 20px 60px rgba(0,0,0,.2); }}
h1 {{ color:#4f46e5; margin-bottom:12px; }}
p  {{ color:#555; line-height:1.6; }}
a  {{ display:inline-block; margin-top:20px; color:#4f46e5; font-weight:600; text-decoration:none; }}
</style>
</head>
<body>
<div class="card">
  <div style="font-size:56px;margin-bottom:12px">✅</div>
  <h1>{platform} Connected!</h1>
  <p>Authorization complete. Returning you to chat…</p>
  <a href="{chat_url}">Return to chat →</a>
</div>
<script>setTimeout(()=>window.location.href="{chat_url}",1200)</script>
</body>
</html>"""


def _error_html(error: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Auth Error</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:linear-gradient(135deg,#ef4444,#b91c1c);
  min-height:100vh; display:flex; align-items:center; justify-content:center;
}}
.card {{ background:#fff; border-radius:16px; padding:40px; max-width:400px;
         text-align:center; box-shadow:0 20px 60px rgba(0,0,0,.2); }}
h1 {{ color:#ef4444; margin-bottom:12px; }}
p  {{ color:#555; line-height:1.6; }}
</style>
</head>
<body>
<div class="card">
  <div style="font-size:56px;margin-bottom:12px">❌</div>
  <h1>Authentication Error</h1>
  <p>{error}</p>
  <p style="margin-top:12px;font-size:13px;color:#999">Close this and try again from chat.</p>
</div>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────────────────

async def register_state(request: web.Request) -> web.Response:
    """
    Agent calls this to register a state_param → sender mapping
    before sending the auth link to the user.
    POST /register  body: {state, sender, secret}
    """
    if request.headers.get("X-Secret") != SHARED_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    body = await request.json()
    state  = body.get("state")
    sender = body.get("sender")
    if not state or not sender:
        return web.json_response({"error": "missing state or sender"}, status=400)
    _state_map[state] = sender
    logger.info(f"[register] state={state[:12]}… sender={sender[:20]}…")
    return web.json_response({"ok": True})


async def get_token(request: web.Request) -> web.Response:
    """
    Agent calls this after callback fires to retrieve the stored token.
    GET /token?sender=<addr>&provider=yt|li   Header: X-Secret
    """
    if request.headers.get("X-Secret") != SHARED_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    sender   = request.query.get("sender", "")
    provider = request.query.get("provider", "")
    data     = _token_store.get(sender, {})
    token    = data.get(provider, "")
    refresh  = data.get(f"{provider}_refresh", "")
    logger.info(f"[token] provider={provider} sender={sender[:20]}… found={'yes' if token else 'no'}")
    return web.json_response({"access_token": token, "refresh_token": refresh})


async def yt_callback(request: web.Request) -> web.Response:
    """YouTube OAuth2 callback."""
    q     = dict(request.query)
    code  = q.get("code")
    state = q.get("state", "")
    error = q.get("error")

    if error or not code:
        return web.Response(
            text=_error_html(error or "No authorization code received."),
            content_type="text/html",
        )

    sender = _state_map.pop(state, None)
    if not sender:
        return web.Response(
            text=_error_html("Invalid state. Please try again from chat."),
            content_type="text/html",
        )

    # Exchange code for tokens
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code":          code,
                    "client_id":     GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uri":  GOOGLE_REDIRECT_URI,
                    "grant_type":    "authorization_code",
                },
            )
            if r.status_code != 200:
                raise RuntimeError(f"Google {r.status_code}: {r.text[:200]}")
            tokens = r.json()
    except Exception as e:
        logger.error(f"[yt_callback] {e}")
        return web.Response(text=_error_html(str(e)), content_type="text/html")

    _token_store.setdefault(sender, {})
    _token_store[sender]["yt"]         = tokens.get("access_token", "")
    _token_store[sender]["yt_refresh"] = tokens.get("refresh_token", "")
    logger.info(f"[yt_callback] token stored for {sender[:20]}…")

    return web.Response(
        text=_success_html("YouTube", CHAT_APP_BASE_URL),
        content_type="text/html",
    )


async def li_callback(request: web.Request) -> web.Response:
    """LinkedIn OAuth2 callback."""
    q     = dict(request.query)
    code  = q.get("code")
    state = q.get("state", "")
    error = q.get("error")

    if error or not code:
        return web.Response(
            text=_error_html(error or "No authorization code received."),
            content_type="text/html",
        )

    sender = _state_map.pop(state, None)
    if not sender:
        return web.Response(
            text=_error_html("Invalid state. Please try again from chat."),
            content_type="text/html",
        )

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://www.linkedin.com/oauth/v2/accessToken",
                data={
                    "grant_type":    "authorization_code",
                    "code":          code,
                    "redirect_uri":  LINKEDIN_REDIRECT_URI,
                    "client_id":     LINKEDIN_CLIENT_ID,
                    "client_secret": LINKEDIN_CLIENT_SECRET,
                },
            )
            if r.status_code != 200:
                raise RuntimeError(f"LinkedIn {r.status_code}: {r.text[:200]}")
            tokens = r.json()
    except Exception as e:
        logger.error(f"[li_callback] {e}")
        return web.Response(text=_error_html(str(e)), content_type="text/html")

    _token_store.setdefault(sender, {})
    _token_store[sender]["li"] = tokens.get("access_token", "")
    logger.info(f"[li_callback] token stored for {sender[:20]}…")

    return web.Response(
        text=_success_html("LinkedIn", CHAT_APP_BASE_URL),
        content_type="text/html",
    )


async def home(request: web.Request) -> web.Response:
    return web.Response(
        text="<html><body><h2>AI Social Media Army — OAuth Server</h2><p>Running ✅</p></body></html>",
        content_type="text/html",
    )


# ── App ───────────────────────────────────────────────────────────────────────
app = web.Application()
app.router.add_post("/register",           register_state)
app.router.add_get("/token",               get_token)
app.router.add_get("/callback/youtube",    yt_callback)
app.router.add_get("/callback/linkedin",   li_callback)
app.router.add_get("/",                    home)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    logger.info(f"OAuth server starting on port {port}")
    web.run_app(app, host="0.0.0.0", port=port)
