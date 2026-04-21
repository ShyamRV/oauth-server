"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  AI Social Media Army — OAuth Callback Server                               ║
║  Deploy this to Render (free tier).                                         ║
║                                                                              ║
║  Routes:                                                                     ║
║    POST /register              ← agent registers state_param → sender       ║
║    GET  /token                 ← agent polls for stored token                ║
║    GET  /callback/youtube      ← Google OAuth2 redirect                     ║
║    GET  /callback/linkedin     ← LinkedIn OAuth2 redirect                   ║
║    GET  /                      ← health check                               ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os
import logging
from aiohttp import web
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [oauth-server] %(levelname)s — %(message)s",
)
logger = logging.getLogger("oauth-server")

# ── Config ────────────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID        = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET    = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REDIRECT_URI     = os.environ["GOOGLE_REDIRECT_URI"]

LINKEDIN_CLIENT_ID      = os.environ["LINKEDIN_CLIENT_ID"]
LINKEDIN_CLIENT_SECRET  = os.environ["LINKEDIN_CLIENT_SECRET"]
LINKEDIN_REDIRECT_URI   = os.environ["LINKEDIN_REDIRECT_URI"]

CHAT_APP_BASE_URL       = os.environ.get("CHAT_APP_BASE_URL", "https://asi1.ai")
SHARED_SECRET           = os.environ["SHARED_SECRET"]   # random secret, same value in agent env

# ── In-memory stores ──────────────────────────────────────────────────────────
# state_param (str) → sender_address (str)
_state_map:  dict[str, str]  = {}

# sender_address (str) → {"yt": token, "yt_refresh": token, "li": token}
_token_store: dict[str, dict] = {}


# ══════════════════════════════════════════════════════════════════════════════
#  HTML TEMPLATES
# ══════════════════════════════════════════════════════════════════════════════

def _success_html(platform: str, chat_url: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{platform} Connected — AI Social Media Army</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:linear-gradient(135deg,#6366f1 0%,#4f46e5 100%);
  min-height:100vh; display:flex; align-items:center; justify-content:center; padding:20px;
}}
.card {{
  background:white; border-radius:16px; padding:40px; max-width:420px;
  text-align:center; box-shadow:0 20px 60px rgba(0,0,0,.2);
}}
.icon  {{ font-size:64px; margin-bottom:16px; }}
h1     {{ color:#4f46e5; font-size:24px; margin-bottom:12px; }}
p      {{ color:#555; line-height:1.6; font-size:15px; }}
.tip   {{ background:#f0f0ff; border-radius:8px; padding:16px; margin-top:24px; font-size:14px; color:#333; }}
.link  {{ display:inline-block; margin-top:16px; color:#4f46e5; text-decoration:none; font-weight:600; }}
</style>
</head>
<body>
<div class="card">
  <div class="icon">✅</div>
  <h1>{platform} Connected!</h1>
  <p>Authorization complete.</p>
  <div class="tip">Go back to the same chat tab where you opened this link so the agent can continue your current session.</div>
  <a class="link" href="{chat_url}">Return to chat →</a>
</div>
</body>
</html>"""


def _error_html(error: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Auth Error — AI Social Media Army</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:linear-gradient(135deg,#ef4444 0%,#b91c1c 100%);
  min-height:100vh; display:flex; align-items:center; justify-content:center; padding:20px;
}}
.card {{
  background:white; border-radius:16px; padding:40px; max-width:420px;
  text-align:center; box-shadow:0 20px 60px rgba(0,0,0,.2);
}}
.icon {{ font-size:64px; margin-bottom:16px; }}
h1    {{ color:#ef4444; font-size:24px; margin-bottom:12px; }}
p     {{ color:#555; line-height:1.6; font-size:15px; }}
</style>
</head>
<body>
<div class="card">
  <div class="icon">❌</div>
  <h1>Authentication Error</h1>
  <p>{error}</p>
  <p style="margin-top:16px;font-size:13px;color:#999;">Close this window and try again from chat.</p>
</div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
#  AUTH MIDDLEWARE
# ══════════════════════════════════════════════════════════════════════════════

def _check_secret(request: web.Request) -> bool:
    """Verify the shared secret header on agent-to-server calls."""
    return request.headers.get("X-Secret") == SHARED_SECRET


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

async def register_state(request: web.Request) -> web.Response:
    """
    Called by the agent BEFORE sending the OAuth link to the user.
    Maps state_param → sender_address so the callback can identify the user.

    POST /register
    Headers: X-Secret: <SHARED_SECRET>
    Body:    {"state": "<state_param>", "sender": "<sender_address>"}
    """
    if not _check_secret(request):
        logger.warning("[register] Unauthorized attempt")
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    state  = body.get("state", "").strip()
    sender = body.get("sender", "").strip()

    if not state or not sender:
        return web.json_response({"error": "missing state or sender"}, status=400)

    _state_map[state] = sender
    logger.info(f"[register] state={state[:12]}… → sender={sender[:20]}…")
    return web.json_response({"ok": True})


async def get_token(request: web.Request) -> web.Response:
    """
    Called by the agent after sending the OAuth link to check if the user
    has completed the browser flow.

    GET /token?sender=<address>&provider=yt|li
    Headers: X-Secret: <SHARED_SECRET>
    Returns: {"access_token": "...", "refresh_token": "..."}
             access_token is "" if auth not yet complete.
    """
    if not _check_secret(request):
        logger.warning("[token] Unauthorized attempt")
        return web.json_response({"error": "unauthorized"}, status=401)

    sender   = request.query.get("sender", "").strip()
    provider = request.query.get("provider", "").strip()

    if not sender or provider not in ("yt", "li"):
        return web.json_response({"error": "missing or invalid sender/provider"}, status=400)

    data    = _token_store.get(sender, {})
    token   = data.get(provider, "")
    refresh = data.get(f"{provider}_refresh", "")

    logger.info(f"[token] provider={provider} sender={sender[:20]}… found={'yes' if token else 'no'}")
    return web.json_response({"access_token": token, "refresh_token": refresh})


async def yt_callback(request: web.Request) -> web.Response:
    """
    Google/YouTube OAuth2 redirect target.
    GET /callback/youtube?code=...&state=...
    """
    q     = dict(request.query)
    code  = q.get("code", "")
    state = q.get("state", "")
    error = q.get("error", "")

    if error or not code:
        logger.warning(f"[yt_callback] Error from Google: {error or 'no code'}")
        return web.Response(
            text=_error_html(error or "No authorization code received."),
            content_type="text/html",
        )

    sender = _state_map.pop(state, None)
    if not sender:
        logger.warning(f"[yt_callback] Unknown state: {state[:12]}…")
        return web.Response(
            text=_error_html("Invalid state parameter. Please try again from chat."),
            content_type="text/html",
        )

    # Exchange authorization code for access + refresh tokens
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
                raise RuntimeError(f"Google token exchange {r.status_code}: {r.text[:200]}")
            tokens = r.json()
    except Exception as e:
        logger.error(f"[yt_callback] Token exchange failed: {e}")
        return web.Response(text=_error_html(f"Token exchange failed: {e}"), content_type="text/html")

    _token_store.setdefault(sender, {})
    _token_store[sender]["yt"]         = tokens.get("access_token", "")
    _token_store[sender]["yt_refresh"] = tokens.get("refresh_token", "")
    logger.info(f"[yt_callback] Token stored for sender={sender[:20]}…")

    return web.Response(
        text=_success_html("YouTube", CHAT_APP_BASE_URL),
        content_type="text/html",
    )


async def li_callback(request: web.Request) -> web.Response:
    """
    LinkedIn OAuth2 redirect target.
    GET /callback/linkedin?code=...&state=...
    """
    q     = dict(request.query)
    code  = q.get("code", "")
    state = q.get("state", "")
    error = q.get("error", "")

    if error or not code:
        logger.warning(f"[li_callback] Error from LinkedIn: {error or 'no code'}")
        return web.Response(
            text=_error_html(error or "No authorization code received."),
            content_type="text/html",
        )

    sender = _state_map.pop(state, None)
    if not sender:
        logger.warning(f"[li_callback] Unknown state: {state[:12]}…")
        return web.Response(
            text=_error_html("Invalid state parameter. Please try again from chat."),
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
                raise RuntimeError(f"LinkedIn token exchange {r.status_code}: {r.text[:200]}")
            tokens = r.json()
    except Exception as e:
        logger.error(f"[li_callback] Token exchange failed: {e}")
        return web.Response(text=_error_html(f"Token exchange failed: {e}"), content_type="text/html")

    _token_store.setdefault(sender, {})
    _token_store[sender]["li"] = tokens.get("access_token", "")
    logger.info(f"[li_callback] Token stored for sender={sender[:20]}…")

    return web.Response(
        text=_success_html("LinkedIn", CHAT_APP_BASE_URL),
        content_type="text/html",
    )


async def home(request: web.Request) -> web.Response:
    """Health check."""
    return web.Response(
        text=(
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>🎬 AI Social Media Army — OAuth Server</h2>"
            "<p>✅ Running</p>"
            "<ul>"
            "<li>POST /register — agent registers OAuth state</li>"
            "<li>GET  /token   — agent polls for stored token</li>"
            "<li>GET  /callback/youtube  — Google redirect</li>"
            "<li>GET  /callback/linkedin — LinkedIn redirect</li>"
            "</ul>"
            "</body></html>"
        ),
        content_type="text/html",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  APP
# ══════════════════════════════════════════════════════════════════════════════

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
