"""
╔══════════════════════════════════════════════════════════════════════════════╗
║       AI SOCIAL MEDIA ARMY  —  Unified Agent  v4  (Agentverse 2026)        ║
║──────────────────────────────────────────────────────────────────────────── ║
║  RENDER OAUTH SERVER — no local aiohttp server, no VM, no nginx needed!    ║
║                                                                              ║
║  HOW AUTH WORKS:                                                             ║
║  1. Agent calls Render /register to map state_param → sender address        ║
║  2. Agent sends OAuth link to user in chat                                   ║
║  3. Agent starts background polling task (every 5s, up to 60s)              ║
║  4. User clicks link → approves in browser                                  ║
║  5. Google/LinkedIn redirect to Render /callback/youtube or /callback/li    ║
║  6. Render exchanges code → stores token → shows success page → redirects  ║
║  7. Polling task detects token → saves it → advances stage proactively      ║
║  ✅ User NEVER sees or pastes any code.                                      ║
║                                                                              ║
║  CHANGES FROM v3:                                                            ║
║  • Local aiohttp server REMOVED — Render handles all OAuth callbacks        ║
║  • _register_state() calls Render POST /register before sending auth link   ║
║  • _fetch_token() calls Render GET /token to retrieve stored token          ║
║  • _state_param() fires asyncio.create_task(_register_state(...))           ║
║  • _check_yt_token() / _check_li_token() poll Render every 5s for 60s      ║
║  • _prompt_yt_auth / _prompt_li_auth start polling tasks                    ║
║  • startup_handler no longer starts aiohttp server                          ║
║  • OAUTH_SERVER_URL + SHARED_SECRET env vars required                       ║
║  • All other logic (NLU, pipeline, content gen, etc.) unchanged             ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from uuid import uuid4

import httpx
from openai import OpenAI
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    TextContent,
    chat_protocol_spec,
)

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

ASI1_API_KEY            = os.environ.get("ASI1_API_KEY", "")
AGENT_SEED              = os.environ.get("AGENT_SEED", "social-army-unified-prod-v4")
USE_MAILBOX             = os.environ.get("USE_MAILBOX", "true").lower() == "true"

# ── Render OAuth server ───────────────────────────────────────────────────────
# Strip whitespace / trailing slash so health checks and /register URLs stay valid.
OAUTH_SERVER_URL        = (os.environ.get("OAUTH_SERVER_URL", "") or "").strip().rstrip("/")
SHARED_SECRET           = (os.environ.get("SHARED_SECRET", "") or "").strip()

# ── Google / YouTube ─────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID        = (os.environ.get("GOOGLE_CLIENT_ID", "") or "").strip()
# Must match an "Authorized redirect URI" in Google Cloud exactly (no stray spaces).
GOOGLE_REDIRECT_URI     = (os.environ.get("GOOGLE_REDIRECT_URI", "") or "").strip().rstrip("/")

# ── LinkedIn ──────────────────────────────────────────────────────────────────
LINKEDIN_CLIENT_ID      = (os.environ.get("LINKEDIN_CLIENT_ID", "") or "").strip()
LINKEDIN_REDIRECT_URI   = (os.environ.get("LINKEDIN_REDIRECT_URI", "") or "").strip().rstrip("/")

# ── Misc ──────────────────────────────────────────────────────────────────────
CHAT_APP_BASE_URL       = os.environ.get("CHAT_APP_BASE_URL", "https://asi1.ai")
CHAT_SESSION_URL_TEMPLATE = os.environ.get("CHAT_SESSION_URL_TEMPLATE", "").strip()
YOUTUBE_PRIVACY_STATUS  = (os.environ.get("YOUTUBE_PRIVACY_STATUS", "unlisted") or "unlisted").strip().lower()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
)
logger = logging.getLogger("social-army")

# ── ASI:ONE client ────────────────────────────────────────────────────────────
_asi1_client: Optional[OpenAI] = None

def _asi1() -> OpenAI:
    global _asi1_client
    if _asi1_client is None:
        if not ASI1_API_KEY:
            raise RuntimeError("ASI1_API_KEY env var is not set.")
        _asi1_client = OpenAI(base_url="https://api.asi1.ai/v1", api_key=ASI1_API_KEY)
    return _asi1_client

# ── Agent ─────────────────────────────────────────────────────────────────────
_agent_kwargs: Dict[str, Any] = dict(
    name="social-army",
    seed=AGENT_SEED,
    publish_agent_details=True,
)
if USE_MAILBOX:
    _agent_kwargs["mailbox"] = True
else:
    _agent_kwargs["port"] = 8009

agent = Agent(**_agent_kwargs)


# ══════════════════════════════════════════════════════════════════════════════
#  IN-MEMORY STATE
#
#  _session_map:  sender_address → ctx.session  (for proactive sends)
#  _token_store:  sender_address → {"yt": token, "yt_refresh": ..., "li": token}
#  _agent_ctx:    the live Context object (set at startup)
#
#  NOTE: _state_map is now on Render, not here. We still keep a local copy
#        as a fallback so _state_param() can work without async context.
# ══════════════════════════════════════════════════════════════════════════════

_session_map: Dict[str, Any] = {}        # sender → ctx.session
_token_store: Dict[str, Dict] = {}       # sender → {yt, yt_refresh, li}
_agent_ctx:   Optional[Context] = None   # set in startup handler
_active_pipeline_tasks: set[str] = set() # sender addresses with running pipeline task


# ══════════════════════════════════════════════════════════════════════════════
#  ENUMS
# ══════════════════════════════════════════════════════════════════════════════

class Stage(str, Enum):
    INIT           = "init"
    AWAITING_LINKS = "awaiting_links"
    YT_AUTH        = "yt_auth"
    LI_AUTH        = "li_auth"
    REVIEW         = "review"
    RUNNING        = "running"
    DONE           = "done"


class Intent(str, Enum):
    GREET         = "greet"
    PROVIDE_LINKS = "provide_links"
    PROVIDE_AUTH  = "provide_auth"
    CONFIRM       = "confirm"
    CANCEL        = "cancel"
    STATUS        = "status"
    HELP          = "help"
    REPEAT        = "repeat"
    UNKNOWN       = "unknown"


# ══════════════════════════════════════════════════════════════════════════════
#  RENDER OAUTH SERVER HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _chat_return_url(sender: str) -> str:
    """
    Build deterministic return URL for the same chat session.
    Prefer explicit template (e.g. https://asi1.ai/chat/{chat_id}).
    Generic fallback returns the base chat app URL.
    """
    if CHAT_SESSION_URL_TEMPLATE:
        return CHAT_SESSION_URL_TEMPLATE.replace("{chat_id}", sender)
    return CHAT_APP_BASE_URL


async def _register_state(state_param: str, sender: str, provider: str = "") -> None:
    """
    Tell the Render OAuth server: this state_param belongs to this sender.
    Called before sending the auth link to the user so the callback can
    look up who authorized.
    """
    if not OAUTH_SERVER_URL or not SHARED_SECRET:
        logger.warning("[register_state] OAUTH_SERVER_URL or SHARED_SECRET not set — skipping registration.")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{OAUTH_SERVER_URL}/register",
                json={
                    "state": state_param,
                    "sender": sender,
                    "provider": provider,
                    "return_url": _chat_return_url(sender),
                },
                headers={"X-Secret": SHARED_SECRET},
            )
            if r.status_code == 200:
                logger.info(f"[register_state] Registered state={state_param[:12]}… for sender={sender[:20]}…")
            else:
                logger.warning(f"[register_state] Render returned {r.status_code}: {r.text[:100]}")
    except Exception as e:
        logger.warning(f"[register_state] Failed to register state with Render: {e}")


async def _fetch_token(sender: str, provider: str) -> str:
    """
    Fetch the stored OAuth token from the Render server after the user
    has completed the browser flow.
    provider: "yt" or "li"
    """
    if not OAUTH_SERVER_URL or not SHARED_SECRET:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{OAUTH_SERVER_URL}/token",
                params={"sender": sender, "provider": provider},
                headers={"X-Secret": SHARED_SECRET},
            )
            if r.status_code == 200:
                data = r.json()
                token = data.get("access_token", "")
                logger.info(f"[fetch_token] provider={provider} sender={sender[:20]}… found={'yes' if token else 'no'}")
                return token
    except Exception as e:
        logger.warning(f"[fetch_token] {e}")
    return ""


# ══════════════════════════════════════════════════════════════════════════════
#  PROACTIVE SEND HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def send_proactive(sender: str, text: str) -> bool:
    """Send a message to the user proactively using their stored session."""
    global _agent_ctx
    if not _agent_ctx:
        logger.warning("[Proactive] No agent context available yet.")
        return False

    stored_session = _session_map.get(sender)
    if not stored_session:
        logger.warning(f"[Proactive] No stored session for {sender[:28]}…")
        return False

    original_session = _agent_ctx._session
    _agent_ctx._session = stored_session
    try:
        await _agent_ctx.send(sender, _msg(text))
        logger.info(f"[Proactive] Sent to {sender[:28]}…")
        return True
    except Exception as e:
        logger.warning(f"[Proactive] Send failed: {e}")
        return False
    finally:
        _agent_ctx._session = original_session


async def _send_user(sender: str, text: str, end: bool = False) -> bool:
    """
    Session-aware send that works from both on_message handlers and
    long-running background tasks.  Always swaps in the user's stored
    session just for the duration of the send, so pipeline progress
    messages never get dropped when the original handler has returned.
    """
    global _agent_ctx
    if not _agent_ctx:
        logger.warning("[send_user] No agent context.")
        return False
    stored_session = _session_map.get(sender)
    # Fallback: try with whatever live session is currently on context.
    if not stored_session:
        logger.warning(f"[send_user] No stored session for {sender[:28]}… trying live context session.")
        try:
            await _agent_ctx.send(sender, _msg(text, end=end))
            return True
        except Exception as e:
            logger.warning(f"[send_user] live-session send failed: {e}")
            return False
    original_session = _agent_ctx._session
    _agent_ctx._session = stored_session
    try:
        await _agent_ctx.send(sender, _msg(text, end=end))
        return True
    except Exception as e:
        logger.warning(f"[send_user] stored-session send failed: {e}; retrying live session.")
        try:
            _agent_ctx._session = original_session
            await _agent_ctx.send(sender, _msg(text, end=end))
            return True
        except Exception as e2:
            logger.warning(f"[send_user] retry failed: {e2}")
            return False
    finally:
        _agent_ctx._session = original_session


# ══════════════════════════════════════════════════════════════════════════════
#  TOKEN POLLING TASKS
#  Poll the Render server every 5 seconds for up to 180 seconds waiting for
#  the user to complete the OAuth browser flow.
# ══════════════════════════════════════════════════════════════════════════════

async def _check_yt_token(sender: str, s: Dict[str, Any]) -> None:
    """
    Poll Render for the YouTube access token after sending the auth link.
    On success: saves token, advances to LI_AUTH.
    On timeout: sends a helpful message telling user to retry.
    """
    logger.info(f"[check_yt_token] Starting poll for sender={sender[:20]}…")
    for attempt in range(36):          # 36 × 5s = 180s max
        await asyncio.sleep(5)
        token = await _fetch_token(sender, "yt")
        if token and not _is_mock_token(token):
            s["yt_token"] = token
            save_session_global(sender, s)
            logger.info(f"[check_yt_token] Token found on attempt {attempt + 1}")
            await _advance_to_li_auth(sender, s)
            return

    # Timed out
    logger.warning(f"[check_yt_token] Timed out waiting for YouTube auth from {sender[:20]}…")
    await send_proactive(
        sender,
        "⏰ **YouTube auth timed out** (180 s).\n\n"
        "No worries — just reply **repeat** and I'll send the link again!"
    )


async def _check_li_token(sender: str, s: Dict[str, Any]) -> None:
    """
    Poll Render for the LinkedIn access token after sending the auth link.
    On success: saves token, advances to REVIEW.
    On timeout: sends a helpful message telling user to retry.
    """
    logger.info(f"[check_li_token] Starting poll for sender={sender[:20]}…")
    for attempt in range(36):
        await asyncio.sleep(5)
        token = await _fetch_token(sender, "li")
        if token and not _is_mock_token(token):
            s["li_token"] = token
            save_session_global(sender, s)
            logger.info(f"[check_li_token] Token found on attempt {attempt + 1}")
            await _advance_to_review(sender, s)
            return

    logger.warning(f"[check_li_token] Timed out waiting for LinkedIn auth from {sender[:20]}…")
    await send_proactive(
        sender,
        "⏰ **LinkedIn auth timed out** (180 s).\n\n"
        "No worries — just reply **repeat** and I'll send the link again!"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  PROACTIVE STAGE ADVANCE HELPERS
#  Called by polling tasks once a token lands.
# ══════════════════════════════════════════════════════════════════════════════

async def _advance_to_li_auth(sender: str, s: Dict[str, Any]) -> None:
    """Called after YouTube auth completes — proactively send LinkedIn auth prompt."""
    await asyncio.sleep(0.3)
    state_param = _build_state_param(sender, "li")
    # Register with Render (must await here since we're already in a task)
    await _register_state(state_param, sender, "li")
    url = li_auth_url(state_param)
    if url:
        s["stage"] = Stage.LI_AUTH.value
        save_session_global(sender, s)
        ok = await send_proactive(
            sender,
            f"✅ **YouTube connected!**\n\n"
            f"**Step 2 of 2 — Connect LinkedIn**\n\n"
            f"[Click here to authorize LinkedIn]({url})\n\n"
            "Approve in the browser — **no code needed!** 🎉"
        )
        if ok:
            # Re-load session (might have been updated) and start LI polling
            asyncio.create_task(_check_li_token(sender, get_session_global(sender)))
        else:
            logger.warning(f"[Advance] Proactive LI_AUTH send failed for {sender[:28]}…")
    else:
        # LinkedIn OAuth not configured — skip to review with mock token
        s["li_token"] = "mock-li"
        save_session_global(sender, s)
        await _advance_to_review(sender, s)


async def _advance_to_review(sender: str, s: Dict[str, Any]) -> None:
    """Called after LinkedIn auth completes — proactively start pipeline."""
    await asyncio.sleep(0.3)
    s["stage"] = Stage.REVIEW.value
    save_session_global(sender, s)
    ok = await send_proactive(
        sender,
        "✅ **Both platforms connected!**\n\n"
        "All auth steps are complete for this chat session.\n\n"
        "Starting upload workflow automatically:\n"
        "• Generate an AI-optimized title, description & caption (ASI:ONE)\n"
        "• Upload your video to YouTube\n"
        "• Publish a LinkedIn post with the YouTube link"
    )
    if ok and _agent_ctx and _session_map.get(sender):
        original_session = _agent_ctx._session
        try:
            _agent_ctx._session = _session_map[sender]
            _start_pipeline_background(_agent_ctx, sender, get_session_global(sender))
        finally:
            _agent_ctx._session = original_session


def _start_pipeline_background(ctx: Context, sender: str, s: Dict[str, Any]) -> bool:
    """
    Start pipeline in background so chat handlers don't hit Agentverse execution timeout.
    The runner does NOT rely on the passed ctx.session (which becomes stale after
    the triggering handler returns). Every message the pipeline sends is routed via
    _send_user which looks up the live session from _session_map.
    Returns True if a new task started, False if already running.
    """
    if sender in _active_pipeline_tasks:
        logger.info(f"[Pipeline] already running for {sender[:20]}… skipping duplicate start")
        return False
    _active_pipeline_tasks.add(sender)

    # Make sure the session is recorded even if the trigger came from a
    # proactive path that somehow lost it.
    if getattr(ctx, "_session", None):
        _session_map[sender] = ctx._session

    async def _runner() -> None:
        try:
            await run_pipeline(ctx, sender, s)
        except Exception as e:
            logger.exception(f"[Pipeline bg] unhandled error: {e}")
            try:
                await _send_user(sender, f"❌ Pipeline crashed: {e}", end=True)
            except Exception:
                pass
        finally:
            _active_pipeline_tasks.discard(sender)

    asyncio.create_task(_runner())
    logger.info(f"[Pipeline] background task started for {sender[:20]}…")
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  GLOBAL SESSION STORE
# ══════════════════════════════════════════════════════════════════════════════

_global_sessions: Dict[str, Dict[str, Any]] = {}

def _sk(sender: str) -> str:
    return hashlib.sha256(sender.encode()).hexdigest()[:24]

def _fresh_session(sender: str) -> Dict[str, Any]:
    return {
        "stage":         Stage.INIT.value,
        "user_id":       _sk(sender),
        "history":       [],
        "video_file_id": "",
        "video_url":     "",
        "script_text":   "",
        "yt_token":      "",
        "yt_refresh":    "",
        "li_token":      "",
        "job_id":        "",
        "result":        {},
    }


def _storage_key(sender: str) -> str:
    return f"session::{_sk(sender)}"


def _load_from_storage(sender: str) -> Optional[Dict[str, Any]]:
    """Load persisted session from uagents storage (survives agent restarts)."""
    if _agent_ctx is None:
        return None
    try:
        raw = _agent_ctx.storage.get(_storage_key(sender))
        if not raw:
            return None
        if isinstance(raw, dict):
            return raw
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"[storage] load failed for {sender[:20]}…: {e}")
        return None


def _save_to_storage(sender: str, s: Dict[str, Any]) -> None:
    if _agent_ctx is None:
        return
    try:
        _agent_ctx.storage.set(_storage_key(sender), json.dumps(s))
    except Exception as e:
        logger.warning(f"[storage] save failed for {sender[:20]}…: {e}")


def get_session_global(sender: str) -> Dict[str, Any]:
    key = _sk(sender)
    if key in _global_sessions:
        return _global_sessions[key]
    # Try durable storage first so we survive Agentverse process recycles.
    persisted = _load_from_storage(sender)
    if persisted:
        _global_sessions[key] = persisted
        return _global_sessions[key]
    _global_sessions[key] = _fresh_session(sender)
    return _global_sessions[key]


def save_session_global(sender: str, s: Dict[str, Any]) -> None:
    s["history"] = s.get("history", [])[-12:]
    _global_sessions[_sk(sender)] = s
    _save_to_storage(sender, s)


def get_session(ctx: Context, sender: str) -> Dict[str, Any]:
    return get_session_global(sender)


def save_session(ctx: Context, sender: str, s: Dict[str, Any]) -> None:
    save_session_global(sender, s)


def reset_session(ctx: Context, sender: str, s: Dict[str, Any]) -> Dict[str, Any]:
    fresh = _fresh_session(sender)
    fresh["user_id"] = s.get("user_id", fresh["user_id"])
    save_session_global(sender, fresh)
    return fresh

def push_history(s: Dict[str, Any], text: str) -> None:
    s.setdefault("history", []).append(text[:120])


# ══════════════════════════════════════════════════════════════════════════════
#  NLU ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class NLUResult:
    __slots__ = ("intent", "confidence", "entities", "reasoning")
    def __init__(self, intent: Intent, confidence: float,
                 entities: Dict[str, Any], reasoning: str = ""):
        self.intent     = intent
        self.confidence = confidence
        self.entities   = entities
        self.reasoning  = reasoning

_RE_DRIVE   = re.compile(r"https?://drive\.google\.com/\S+")
_RE_URL     = re.compile(r"https?://[^\s]+")


def _fast_classify(text: str, stage: Stage) -> Optional[NLUResult]:
    drive_urls = _RE_DRIVE.findall(text)
    if drive_urls and stage in (Stage.INIT, Stage.AWAITING_LINKS):
        return NLUResult(Intent.PROVIDE_LINKS, 0.97,
                         {"drive_urls": drive_urls, "all_urls": _RE_URL.findall(text)})
    return None


async def classify(text: str, stage: Stage, history: List[str]) -> NLUResult:
    fast = _fast_classify(text, stage)
    if fast:
        return fast

    history_ctx = "\n".join(f"  - {h}" for h in history[-4:]) if history else "  (none)"
    stage_hint = {
        Stage.REVIEW: "Both platforms are connected. Almost any positive response is confirm.",
    }.get(stage, "")

    prompt = f"""You are an intent classifier for a social media video publishing assistant.
Current stage: {stage.value}
{f"Stage context: {stage_hint}" if stage_hint else ""}
Recent messages:
{history_ctx}
User message: "{text}"

Respond with EXACTLY this JSON (no markdown):
{{
  "intent": "<greet|provide_links|confirm|cancel|status|help|repeat|unknown>",
  "confidence": <0.0-1.0>,
  "entities": {{"drive_urls": [], "wants_youtube": false, "wants_linkedin": false}},
  "reasoning": "<one sentence>"
}}

Intent definitions:
- greet: hello/hi, no task yet
- provide_links: sharing google drive URLs
- confirm: proceed, run, publish, yes, go, do it, ready, go ahead, absolutely
- cancel: stop, abort, reset, never mind
- status: asking where things stand
- help: asking how it works
- repeat: asking to re-show something
- unknown: genuinely unclear"""

    try:
        resp = _asi1().chat.completions.create(
            model="asi1-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.0,
        )
        raw  = resp.choices[0].message.content.strip()
        raw  = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("` \n")
        data = json.loads(raw)
        intent_str = data.get("intent", "unknown").lower().strip()
        try:
            intent = Intent(intent_str)
        except ValueError:
            intent = Intent.UNKNOWN
        entities = data.get("entities", {})
        if not entities.get("drive_urls"):
            entities["drive_urls"] = _RE_DRIVE.findall(text)
        entities.setdefault("all_urls", _RE_URL.findall(text))
        return NLUResult(intent=intent, confidence=float(data.get("confidence", 0.6)),
                         entities=entities, reasoning=data.get("reasoning", ""))
    except Exception as exc:
        logger.warning(f"[NLU] LLM failed ({exc}), using heuristic fallback")
        return _heuristic_fallback(text)


def _heuristic_fallback(text: str) -> NLUResult:
    t = text.lower()
    scores: Dict[Intent, float] = {i: 0.0 for i in Intent}
    kw: Dict[Intent, List[str]] = {
        Intent.GREET:   ["hi", "hello", "hey"],
        Intent.CONFIRM: ["yes", "go", "proceed", "confirm", "run", "do it", "ok", "okay",
                         "sure", "absolutely", "ready", "launch", "upload", "publish",
                         "fire", "execute", "sounds good", "let's go", "go ahead", "start"],
        Intent.CANCEL:  ["no", "cancel", "stop", "abort", "reset", "never mind", "scratch that"],
        Intent.STATUS:  ["status", "progress", "update", "where are we", "any news"],
        Intent.HELP:    ["help", "how", "what can", "instructions", "?"],
        Intent.REPEAT:  ["again", "repeat", "re-show", "resend"],
    }
    for intent, keywords in kw.items():
        for w in keywords:
            if w in t:
                scores[intent] += 1.0
    best = max(scores, key=lambda i: scores[i])
    if scores[best] == 0:
        return NLUResult(Intent.UNKNOWN, 0.2, {})
    return NLUResult(best, min(0.45 + scores[best] * 0.12, 0.80), {})


# ══════════════════════════════════════════════════════════════════════════════
#  OAUTH HELPERS
# ══════════════════════════════════════════════════════════════════════════════

_YT_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube "
    "https://www.googleapis.com/auth/userinfo.email"
).replace(" ", "%20")

# LinkedIn scopes vary by app approvals. Keep this configurable.
# Safer default for most apps: r_liteprofile + w_member_social.
LINKEDIN_SCOPES = (os.environ.get("LINKEDIN_SCOPES", "r_liteprofile w_member_social") or "").strip()


def _build_state_param(sender: str, salt: str = "") -> str:
    """
    Generate a unique signed state for each OAuth attempt.
    Format: v1.<base64(sender|salt|nonce)>.<sig>
    where sig = sha256(payload|SHARED_SECRET)[:24]
    This prevents state reuse collisions and allows callback-side sender recovery.
    """
    nonce = uuid4().hex[:12]
    payload = f"{sender}|{salt}|{nonce}"
    secret = SHARED_SECRET or "social-army-secret"
    sig = hashlib.sha256(f"{payload}|{secret}".encode()).hexdigest()[:24]
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"v1.{b64}.{sig}"


def _state_param(sender: str, salt: str = "") -> str:
    """
    Generate state param AND fire-and-forget register with Render.
    Safe to call from synchronous context (uses create_task).
    """
    raw = _build_state_param(sender, salt)
    asyncio.create_task(_register_state(raw, sender, salt))
    return raw


def yt_auth_url(state: str) -> str:
    if not GOOGLE_CLIENT_ID or not GOOGLE_REDIRECT_URI:
        return ""
    # OAuth 2.0 requires form-style query encoding; bare redirect_uri often works but
    # encoding avoids edge-case mismatches. Google compares decoded value to Console list.
    q = (
        f"client_id={quote(GOOGLE_CLIENT_ID, safe='')}"
        f"&redirect_uri={quote(GOOGLE_REDIRECT_URI, safe='')}"
        f"&response_type=code"
        f"&scope={_YT_SCOPES}"
        f"&access_type=offline&prompt=consent"
        f"&state={quote(state, safe='')}"
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{q}"


def li_auth_url(state: str) -> str:
    if not LINKEDIN_CLIENT_ID or not LINKEDIN_REDIRECT_URI:
        return ""
    scopes = LINKEDIN_SCOPES or "r_liteprofile w_member_social"
    q = (
        f"response_type=code"
        f"&client_id={quote(LINKEDIN_CLIENT_ID, safe='')}"
        f"&redirect_uri={quote(LINKEDIN_REDIRECT_URI, safe='')}"
        f"&scope={quote(scopes, safe='')}"
        f"&state={quote(state, safe='')}"
    )
    return f"https://www.linkedin.com/oauth/v2/authorization?{q}"


def _oauth_ready(client_id: str, redirect_uri: str) -> bool:
    return bool(client_id and redirect_uri and len(client_id) > 8
                and not client_id.startswith("your-"))


def _is_mock_token(tok: str) -> bool:
    t = (tok or "").strip().lower()
    return not t or t in {"none", "mock", "dummy", "test", "dev"} or \
           t.startswith(("mock-", "dev-", "backend-"))


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE PROMPT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _prompt_yt_auth(ctx: Context, sender: str, s: Dict[str, Any],
                          user_text: str = "") -> None:
    """
    Build YT auth URL, register state with Render, send link to user,
    then start background polling for the token.
    """
    existing_yt = s.get("yt_token", "")
    if not existing_yt or _is_mock_token(existing_yt):
        existing_yt = await _fetch_token(sender, "yt")
    if existing_yt and not _is_mock_token(existing_yt):
        s["yt_token"] = existing_yt
        save_session(ctx, sender, s)
        await _prompt_li_auth(ctx, sender, s, user_text)
        return

    state_param = _build_state_param(sender, "yt")
    # Register synchronously (we have an async context here)
    await _register_state(state_param, sender, "yt")
    url = yt_auth_url(state_param)

    if url:
        s["stage"] = Stage.YT_AUTH.value
        save_session(ctx, sender, s)
        await ctx.send(sender, _msg(
            f"**Step 1 of 2 — Connect YouTube**\n\n"
            f"[Click here to authorize YouTube]({url})\n\n"
            "Once you approve in the browser, I'll automatically continue — "
            "**no code needed!** 🎉"
        ))
        # Start background polling for the YT token
        asyncio.create_task(_check_yt_token(sender, get_session_global(sender)))
    else:
        # YouTube OAuth not configured — use mock and jump to LinkedIn
        s["yt_token"] = "mock-yt"
        s["yt_refresh"] = ""
        save_session(ctx, sender, s)
        await _prompt_li_auth(ctx, sender, s, user_text)


async def _prompt_li_auth(ctx: Context, sender: str, s: Dict[str, Any],
                          user_text: str = "") -> None:
    """
    Build LI auth URL, register state with Render, send link to user,
    then start background polling for the token.
    """
    existing_li = s.get("li_token", "")
    if not existing_li or _is_mock_token(existing_li):
        existing_li = await _fetch_token(sender, "li")
    if existing_li and not _is_mock_token(existing_li):
        s["li_token"] = existing_li
        save_session(ctx, sender, s)
        await _prompt_review(ctx, sender, s, user_text)
        return

    state_param = _build_state_param(sender, "li")
    await _register_state(state_param, sender, "li")
    url = li_auth_url(state_param)

    if url:
        s["stage"] = Stage.LI_AUTH.value
        save_session(ctx, sender, s)
        await ctx.send(sender, _msg(
            f"✅ YouTube connected!\n\n"
            f"**Step 2 of 2 — Connect LinkedIn**\n\n"
            f"[Click here to authorize LinkedIn]({url})\n\n"
            "Approve in the browser and I'll take it from there — **no code needed!** 🎉"
        ))
        # Start background polling for the LI token
        asyncio.create_task(_check_li_token(sender, get_session_global(sender)))
    else:
        s["li_token"] = "mock-li"
        save_session(ctx, sender, s)
        await _prompt_review(ctx, sender, s, user_text)


async def _prompt_review(ctx: Context, sender: str, s: Dict[str, Any],
                         user_text: str = "") -> None:
    s["stage"] = Stage.REVIEW.value
    save_session(ctx, sender, s)
    await ctx.send(sender, _msg(
        "✅ **Both platforms connected!**\n\n"
        "All auth steps are complete for this chat session.\n\n"
        "Starting upload workflow automatically:\n"
        "• Generate an AI-optimized title, description & caption (ASI:ONE)\n"
        "• Upload your video to YouTube\n"
        "• Publish a LinkedIn post with the YouTube link"
    ))
    if not _start_pipeline_background(ctx, sender, s):
        await ctx.send(sender, _msg("⏳ Pipeline already running for this chat session."))


# ══════════════════════════════════════════════════════════════════════════════
#  GOOGLE DRIVE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _drive_file_id(url: str) -> str:
    for pat in [r"/file/d/([a-zA-Z0-9_-]+)", r"id=([a-zA-Z0-9_-]+)",
                r"/d/([a-zA-Z0-9_-]+)", r"open\?id=([a-zA-Z0-9_-]+)"]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return ""


async def drive_read_text(file_id: str) -> str:
    base = f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            r = await c.get(base)
            if r.status_code != 200:
                return ""
            if b"virus scan warning" in r.content[:2000].lower():
                m = re.search(rb"confirm=([0-9A-Za-z_-]+)", r.content)
                if m:
                    r = await c.get(
                        f"https://drive.google.com/uc?export=download"
                        f"&id={file_id}&confirm={m.group(1).decode()}"
                    )
            ct = (r.headers.get("content-type", "") or "").lower()
            # Only accept plain/text-ish payloads as script input. Reject HTML/binary.
            if "text/html" in ct:
                return ""
            text = r.text.strip()
            if "<html" in text[:200].lower() or "<!doctype" in text[:200].lower():
                return ""
            return text if len(text) > 10 else ""
    except Exception as e:
        logger.error(f"[Drive text] {e}")
        return ""


async def drive_download_video(file_id: str) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    tmp.close()
    urls = [
        f"https://drive.google.com/uc?export=download&id={file_id}",
        f"https://drive.google.com/uc?id={file_id}&export=download",
        f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t",
    ]
    try:
        async with httpx.AsyncClient(timeout=600, follow_redirects=True) as c:
            for base_url in urls:
                r = await c.get(base_url)
                if r.status_code != 200:
                    continue
                ct = (r.headers.get("content-type", "") or "").lower()

                # Large Drive files often require a confirm token from HTML/cookie round-trip.
                if "text/html" in ct:
                    confirm = ""
                    for k, v in c.cookies.items():
                        if k.startswith("download_warning"):
                            confirm = v
                            break
                    if not confirm:
                        m = re.search(rb"confirm=([0-9A-Za-z_-]+)", r.content)
                        if m:
                            confirm = m.group(1).decode()
                    if confirm:
                        dl = f"https://drive.google.com/uc?export=download&id={file_id}&confirm={confirm}"
                        r = await c.get(dl)
                        ct = (r.headers.get("content-type", "") or "").lower()

                if r.status_code != 200 or "text/html" in ct:
                    continue

                with open(tmp.name, "wb") as f:
                    async for chunk in r.aiter_bytes(1024 * 1024):
                        if chunk:
                            f.write(chunk)

                size = os.path.getsize(tmp.name)
                if size < 1024:
                    continue
                # Guardrail: Drive sometimes returns non-video payloads with non-HTML
                # content-types; reject those before attempting upload.
                if not _looks_like_video_file(tmp.name):
                    continue
                logger.info(f"[Drive video] Downloaded file_id={file_id[:12]}… bytes={size}")
                return tmp.name

        try:
            os.remove(tmp.name)
        except Exception:
            pass
        return ""
    except Exception as e:
        logger.error(f"[Drive video] {e}")
        return ""


def _looks_like_video_file(path: str) -> bool:
    """
    Quick binary signature checks to avoid uploading HTML/error payloads as video.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(128)
        if not head:
            return False
        low = head.lower()
        if b"<html" in low or b"<!doctype" in low:
            return False
        # MP4/QuickTime family: box header then ftyp.
        if len(head) >= 12 and head[4:8] == b"ftyp":
            return True
        # Matroska/WebM
        if head.startswith(b"\x1a\x45\xdf\xa3"):
            return True
        # AVI / WAV container (RIFF)
        if head.startswith(b"RIFF"):
            return True
        # MPEG-TS packet sync byte
        if head[0] == 0x47:
            return True
        return False
    except Exception:
        return False


def _detect_video_mime(path: str) -> str:
    """Best-effort container detection for correct YouTube upload headers."""
    try:
        with open(path, "rb") as f:
            head = f.read(128)
        if len(head) >= 12 and head[4:8] == b"ftyp":
            brand = head[8:12]
            if brand in {b"qt  "}:
                return "video/quicktime"
            return "video/mp4"
        if head.startswith(b"\x1a\x45\xdf\xa3"):
            return "video/webm"
        if head.startswith(b"RIFF"):
            return "video/x-msvideo"
        if head[:1] == b"\x47":
            return "video/mp2t"
    except Exception:
        pass
    return "application/octet-stream"


# ══════════════════════════════════════════════════════════════════════════════
#  CONTENT GENERATION
# ══════════════════════════════════════════════════════════════════════════════

async def generate_content(script_text: str) -> Dict[str, Any]:
    prompt = f"""You are a YouTube SEO expert and LinkedIn content strategist.
Analyze this video script and generate fully optimized metadata.

SCRIPT:
{script_text[:3500]}

Return ONLY valid JSON (no markdown, no prose):
{{
  "title": "Compelling YouTube title, max 90 chars, primary keyword included",
  "description": "YouTube description 400-600 chars. Hook first. 3-5 hashtags at end.",
  "tags": ["tag1","tag2","tag3","tag4","tag5","tag6","tag7","tag8"],
  "linkedin_caption": "LinkedIn post 180-280 chars. Professional tone. 2-3 hashtags. {{youtube_url}} placeholder.",
  "category": "People & Blogs"
}}"""
    try:
        resp = _asi1().chat.completions.create(
            model="asi1-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("` \n")
        data = json.loads(raw)
    except Exception as e:
        logger.error(f"[Content gen] {e}")
        first = (script_text.split("\n")[0] if script_text else "New Video")[:80].strip()
        data = {
            "title": first or "New Video",
            "description": script_text[:500],
            "tags": ["video", "content", "socialmedia", "youtube"],
            "linkedin_caption": "Just published a new video! Watch here: {youtube_url}",
            "category": "People & Blogs",
        }

    return data


# ══════════════════════════════════════════════════════════════════════════════
#  YOUTUBE UPLOAD
# ══════════════════════════════════════════════════════════════════════════════

_YT_CATEGORIES: Dict[str, str] = {
    "Education": "27", "Science & Technology": "28", "People & Blogs": "22",
    "Entertainment": "24", "News & Politics": "25", "Howto & Style": "26",
    "Gaming": "20", "Music": "10", "Sports": "17", "Travel & Events": "19",
}


async def youtube_upload(
    video_path: str,
    content: Dict[str, Any],
    access_token: str,
) -> Dict[str, Any]:
    if _is_mock_token(access_token):
        fake = f"DEMO_{uuid.uuid4().hex[:10]}"
        return {"video_id": fake,
                "video_url": f"https://www.youtube.com/watch?v={fake}",
                "title": content.get("title", "Demo"), "simulated": True}

    if not video_path or not os.path.exists(video_path):
        raise RuntimeError("Video file not found.")

    filesize = os.path.getsize(video_path)
    if filesize < 1024:
        raise RuntimeError(f"Downloaded video is too small ({filesize} bytes) — likely a Drive error page.")
    if not _looks_like_video_file(video_path):
        raise RuntimeError(
            "Downloaded file does not look like a valid video container. "
            "Please verify the Drive link points to the original video file."
        )

    video_mime = _detect_video_mime(video_path)
    if video_mime == "application/octet-stream":
        # Permissive fallback — let YouTube do the detection rather than blocking.
        video_mime = "video/mp4"

    cat_id  = _YT_CATEGORIES.get(content.get("category", ""), "22")
    privacy = YOUTUBE_PRIVACY_STATUS if YOUTUBE_PRIVACY_STATUS in {"public", "unlisted", "private"} else "unlisted"
    body    = {
        "snippet": {"title": content.get("title", "Untitled")[:100],
                    "description": content.get("description", "")[:5000],
                    "tags": (content.get("tags") or [])[:500],
                    "categoryId": cat_id},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    headers = {"Authorization": f"Bearer {access_token}"}

    # 1. Initiate resumable upload
    async with httpx.AsyncClient(timeout=30) as c:
        init = await c.post(
            "https://www.googleapis.com/upload/youtube/v3/videos"
            "?uploadType=resumable&part=snippet,status",
            headers={**headers, "Content-Type": "application/json",
                     "X-Upload-Content-Type": video_mime,
                     "X-Upload-Content-Length": str(filesize)},
            json=body,
        )
    if init.status_code not in (200, 201):
        raise RuntimeError(f"YouTube initiate {init.status_code}: {init.text[:300]}")
    upload_url = init.headers.get("Location")
    if not upload_url:
        raise RuntimeError("YouTube returned no Location header.")

    # 2. Upload video bytes (read off-loop to avoid blocking)
    data = await asyncio.to_thread(_read_file_bytes, video_path)
    async with httpx.AsyncClient(timeout=900) as c:
        up = await c.put(
            upload_url,
            content=data,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": video_mime,
                "Content-Length": str(filesize),
            },
        )
    if up.status_code not in (200, 201):
        raise RuntimeError(f"YouTube upload {up.status_code}: {up.text[:300]}")

    video_id = up.json().get("id", "")
    if not video_id:
        raise RuntimeError("YouTube returned no video ID.")

    # 3. Poll status so we don't share a URL that's still processing
    resolved_privacy = privacy
    upload_status = ""
    async with httpx.AsyncClient(timeout=30) as c:
        for _ in range(6):
            vr = await c.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "status", "id": video_id},
                headers=headers,
            )
            if vr.status_code == 200:
                items = vr.json().get("items", [])
                if items:
                    st = items[0].get("status", {})
                    resolved_privacy = st.get("privacyStatus", resolved_privacy)
                    upload_status = st.get("uploadStatus", "")
                    if upload_status in {"processed", "uploaded"}:
                        break
                    if upload_status in {"failed", "rejected", "deleted"}:
                        raise RuntimeError(f"YouTube reported upload status: {upload_status}")
            await asyncio.sleep(2)

    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    return {
        "video_id": video_id,
        "video_url": watch_url,
        "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
        "privacy_status": resolved_privacy,
        "title": content.get("title", ""),
        "simulated": False,
    }


def _read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# ══════════════════════════════════════════════════════════════════════════════
#  LINKEDIN POST
# ══════════════════════════════════════════════════════════════════════════════

async def _li_urn(access_token: str) -> str:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://api.linkedin.com/v2/me",
                        headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code == 200:
            return f"urn:li:person:{r.json()['id']}"
        # OIDC-based apps may not have /v2/me scope; fallback to userinfo.
        u = await c.get("https://api.linkedin.com/v2/userinfo",
                        headers={"Authorization": f"Bearer {access_token}"})
        if u.status_code == 200:
            sub = u.json().get("sub", "")
            if sub:
                return f"urn:li:person:{sub}"
        raise RuntimeError(f"LinkedIn identity lookup failed (/me={r.status_code}, /userinfo={u.status_code})")


async def linkedin_post(content: Dict[str, Any], youtube_url: str,
                        access_token: str) -> Dict[str, Any]:
    if _is_mock_token(access_token):
        fake    = f"DEMO_{uuid.uuid4().hex[:10]}"
        caption = (content.get("linkedin_caption", "New video! {youtube_url}")
                   .replace("{youtube_url}", youtube_url)
                   .replace("{{youtube_url}}", youtube_url))
        return {"post_id": fake,
                "post_url": f"https://www.linkedin.com/feed/update/urn:li:share:{fake}/",
                "text": caption[:200], "simulated": True}

    caption = (content.get("linkedin_caption", "New video! {youtube_url}")
               .replace("{youtube_url}", youtube_url)
               .replace("{{youtube_url}}", youtube_url))
    if youtube_url and youtube_url not in caption:
        caption = f"{caption}\n\n{youtube_url}"

    person_urn = await _li_urn(access_token)
    body: Dict[str, Any] = {
        "author": person_urn,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary":    {"text": caption[:3000]},
                "shareMediaCategory": "ARTICLE" if youtube_url else "NONE",
            }
        },
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
    }
    if youtube_url:
        body["specificContent"]["com.linkedin.ugc.ShareContent"]["media"] = [{
            "status": "READY",
            "description": {"text": content.get("description", "")[:256]},
            "originalUrl": youtube_url,
            "title": {"text": content.get("title", "")[:200]},
        }]

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://api.linkedin.com/v2/ugcPosts",
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json",
                     "X-Restli-Protocol-Version": "2.0.0"},
            json=body,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"LinkedIn post {r.status_code}: {r.text[:300]}")
        post_id  = r.headers.get("x-restli-id", r.json().get("id", ""))
        post_url = (f"https://www.linkedin.com/feed/update/{post_id}/"
                    if post_id else "https://www.linkedin.com/feed/")
        return {"post_id": post_id, "post_url": post_url,
                "text": caption[:200], "simulated": False}


# ══════════════════════════════════════════════════════════════════════════════
#  PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

async def run_pipeline(ctx: Context, sender: str, s: Dict[str, Any]) -> None:
    """
    Main pipeline executed in a background task. Every outbound chat message
    must go through _send_user so it works after the triggering handler has
    returned and ctx.session is no longer valid.
    """
    job_id      = str(uuid.uuid4())
    s["job_id"] = job_id
    s["stage"]  = Stage.RUNNING.value
    save_session_global(sender, s)
    logger.info(f"[Pipeline {job_id[:8]}] STARTED for sender={sender[:20]}… video_fid={s.get('video_file_id', '')[:12]}…")

    async def _step(icon: str, text: str) -> None:
        ok = await _send_user(sender, f"{icon} {text}")
        logger.info(f"[Pipeline {job_id[:8]}] step send={ok}: {icon} {text[:80]}")

    await _step("🚀", f"Pipeline started! (Job `{job_id[:8]}`)")

    # ── 1. Download video from Drive ─────────────────────────────────────────
    await _step("⬇️", "Downloading your video from Google Drive…")
    video_path = ""
    fid = s.get("video_file_id", "")
    try:
        if not fid:
            raise RuntimeError("No video file was detected in your provided links.")
        video_path = await drive_download_video(fid)
        if not video_path:
            raise RuntimeError(
                "Could not download a valid video file from Google Drive. "
                "Ensure the video link is shared as **Anyone with the link → Viewer**."
            )
        size_mb = os.path.getsize(video_path) / (1024 * 1024)
        await _step("✅", f"Video downloaded ({size_mb:.1f} MB).")
    except Exception as e:
        logger.exception("[Pipeline] download failed")
        s["stage"] = Stage.DONE.value
        save_session_global(sender, s)
        await _send_user(sender, f"❌ {e}", end=True)
        return

    # ── 2. Generate content with ASI:ONE ──────────────────────────────────────
    await _step("✍️", "Generating optimized title, description & caption with ASI:ONE…")
    try:
        content = await generate_content(s.get("script_text", ""))
    except Exception as e:
        logger.exception("[Pipeline] content gen failed")
        content = {
            "title": "New Video",
            "description": (s.get("script_text", "") or "")[:500],
            "tags": ["video", "content"],
            "linkedin_caption": "New video! {youtube_url}",
            "category": "People & Blogs",
        }
    await _step(
        "✅",
        f"Content ready!\n"
        f"   📌 **Title:** {content.get('title', '')}\n"
        f"   🏷️ **Tags:** {', '.join(content.get('tags', [])[:5])}",
    )

    # ── 3. YouTube upload ────────────────────────────────────────────────────
    await _step("📤", "Uploading to YouTube…")
    yt_result: Dict[str, Any] = {}
    yt_error = ""
    try:
        yt_result = await youtube_upload(
            video_path,
            content,
            s.get("yt_token", ""),
        )
        sim = " _(demo)_" if yt_result.get("simulated") else ""
        privacy_note = f" ({yt_result.get('privacy_status', 'unknown')})" if not yt_result.get("simulated") else ""
        await _step(
            "✅",
            f"YouTube upload complete{sim}{privacy_note}!\n"
            f"   🎬 {yt_result['video_url']}",
        )
    except Exception as e:
        yt_error = str(e)
        logger.exception("[Pipeline] youtube_upload failed")
        await _step("❌", f"YouTube upload failed: {e}")

    # ── 4. LinkedIn post ─────────────────────────────────────────────────────
    await _step("📢", "Posting to LinkedIn…")
    li_result: Dict[str, Any] = {}
    li_error = ""
    try:
        li_result = await linkedin_post(content, yt_result.get("video_url", ""), s.get("li_token", ""))
        sim = " _(demo)_" if li_result.get("simulated") else ""
        await _step("✅", f"LinkedIn post live{sim}!\n   💼 {li_result['post_url']}")
    except Exception as e:
        li_error = str(e)
        logger.exception("[Pipeline] linkedin_post failed")
        await _step("❌", f"LinkedIn post failed: {e}")

    # ── 5. Cleanup ───────────────────────────────────────────────────────────
    if video_path and os.path.exists(video_path):
        try:
            os.remove(video_path)
        except Exception:
            pass

    s["stage"]  = Stage.DONE.value
    s["result"] = {"youtube": yt_result, "linkedin": li_result, "content": content}
    save_session_global(sender, s)

    # ── 6. Final summary ─────────────────────────────────────────────────────
    if yt_error and li_error:
        await _send_user(
            sender,
            "❌ Both platforms failed.\n\n"
            f"**YouTube:** {yt_error}\n**LinkedIn:** {li_error}\n\n"
            "Your credentials are saved — just let me know when to retry.",
            end=True,
        )
        return

    parts = ["🎉 **All done! Here's your summary:**\n"]
    if yt_result:
        sim = " _(demo)_" if yt_result.get("simulated") else ""
        parts.extend([
            f"🎬 **YouTube{sim}:** {yt_result.get('video_url', 'N/A')}",
            f"   Title: _{content.get('title', '')}_",
        ])
        if yt_result.get("studio_url"):
            parts.append(f"   Studio: {yt_result.get('studio_url')}")
    elif yt_error:
        parts.append(f"🎬 YouTube error: {yt_error}")
    parts.append("")
    if li_result:
        sim = " _(demo)_" if li_result.get("simulated") else ""
        parts.append(f"💼 **LinkedIn{sim}:** {li_result.get('post_url', 'N/A')}")
    elif li_error:
        parts.append(f"💼 LinkedIn error: {li_error}")
    parts.append("\nReady to publish another video? Just share your Drive links!")
    await _send_user(sender, "\n".join(parts), end=True)


# ══════════════════════════════════════════════════════════════════════════════
#  MESSAGE HELPER
# ══════════════════════════════════════════════════════════════════════════════

def _msg(text: str, end: bool = False) -> ChatMessage:
    content: List[Any] = [TextContent(type="text", text=text)]
    if end:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(timestamp=datetime.utcnow(), msg_id=uuid4(), content=content)


# ══════════════════════════════════════════════════════════════════════════════
#  REPROMPT HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def _reprompt(ctx: Context, sender: str, s: Dict[str, Any], stage: Stage) -> None:
    if stage == Stage.AWAITING_LINKS:
        await ctx.send(sender, _msg(
            "Please share two Google Drive links: one for your **video file** and one for your **script (.txt)**."
        ))
    elif stage == Stage.YT_AUTH:
        # Re-register fresh state and restart polling
        state_param = _build_state_param(sender, "yt")
        await _register_state(state_param, sender, "yt")
        url = yt_auth_url(state_param)
        await ctx.send(sender, _msg(
            f"Here's your YouTube auth link again:\n\n"
            f"[Authorize YouTube]({url})\n\n"
            "Click the link and approve — no code needed!"
        ))
        asyncio.create_task(_check_yt_token(sender, get_session_global(sender)))
    elif stage == Stage.LI_AUTH:
        state_param = _build_state_param(sender, "li")
        await _register_state(state_param, sender, "li")
        url = li_auth_url(state_param)
        await ctx.send(sender, _msg(
            f"Here's your LinkedIn auth link again:\n\n"
            f"[Authorize LinkedIn]({url})\n\n"
            "Click the link and approve — no code needed!"
        ))
        asyncio.create_task(_check_li_token(sender, get_session_global(sender)))
    elif stage == Stage.REVIEW:
        await _prompt_review(ctx, sender, s)
    elif stage == Stage.RUNNING:
        await ctx.send(sender, _msg(f"⏳ Pipeline still running (Job `{s.get('job_id', '')[:8]}`). Hang tight!"))
    elif stage == Stage.DONE:
        result = s.get("result", {})
        yt = result.get("youtube", {})
        li = result.get("linkedin", {})
        await ctx.send(sender, _msg(
            f"Your last pipeline:\n"
            f"🎬 YouTube: {yt.get('video_url', 'N/A')}\n"
            f"💼 LinkedIn: {li.get('post_url', 'N/A')}"
        ))
    else:
        await ctx.send(sender, _msg("Share your Google Drive links to begin!"))


# ══════════════════════════════════════════════════════════════════════════════
#  CHAT HANDLER
# ══════════════════════════════════════════════════════════════════════════════

chat_proto = Protocol(spec=chat_protocol_spec)


@chat_proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage) -> None:
    await ctx.send(sender, ChatAcknowledgement(
        timestamp=datetime.utcnow(),
        acknowledged_msg_id=msg.msg_id,
    ))

    # Store session for proactive sends
    _session_map[sender] = ctx.session

    text = " ".join(b.text for b in msg.content if isinstance(b, TextContent)).strip()
    text = re.sub(r"^@\w[\w._-]*\s*", "", text).strip()
    if not text:
        return

    ctx.logger.info(f"[chat] sender={sender[:28]}… text={text[:90]!r}")

    s     = get_session(ctx, sender)
    stage = Stage(s.get("stage", Stage.INIT.value))

    # ── Hydrate tokens from Render on every message ───────────────────────────
    # Agentverse recycles the process; in-memory tokens can disappear even when
    # Render still holds a fresh access_token from a recent auth. Pull them back
    # silently so the user never has to re-authorize just because the agent
    # bounced.
    if not s.get("yt_token") or _is_mock_token(s.get("yt_token", "")):
        live_yt = await _fetch_token(sender, "yt")
        if live_yt and not _is_mock_token(live_yt):
            s["yt_token"] = live_yt
            save_session(ctx, sender, s)
    if not s.get("li_token") or _is_mock_token(s.get("li_token", "")):
        live_li = await _fetch_token(sender, "li")
        if live_li and not _is_mock_token(live_li):
            s["li_token"] = live_li
            save_session(ctx, sender, s)

    push_history(s, text)

    # NLU
    nlu = await classify(text, stage, s.get("history", []))
    ctx.logger.info(
        f"[NLU] intent={nlu.intent.value} conf={nlu.confidence:.2f} "
        f"stage={stage.value} reason={nlu.reasoning!r}"
    )

    # ── GLOBAL INTENTS ────────────────────────────────────────────────────────

    if nlu.intent == Intent.CANCEL:
        s = reset_session(ctx, sender, s)
        await ctx.send(sender, _msg(
            "No problem — your session has been cleared. "
            "Whenever you're ready, share your Google Drive links and we'll start fresh!"
        ))
        return

    if nlu.intent == Intent.STATUS:
        status_msgs = {
            Stage.INIT:           "We haven't started yet. Share your Google Drive links (video + script) to begin!",
            Stage.AWAITING_LINKS: "I'm waiting for your two Google Drive links — one for the video, one for the script (.txt).",
            Stage.YT_AUTH:        "Waiting for your YouTube authorization. Click the link I sent — no code needed!",
            Stage.LI_AUTH:        "✅ YouTube is connected! Now I need your LinkedIn authorization. Click the link I sent!",
            Stage.REVIEW:         "✅ Both platforms are connected — just say **go** or **publish** to start!",
            Stage.RUNNING:        f"⏳ Pipeline is running right now (Job `{s.get('job_id', '?')[:8]}`). I'll notify you when done!",
            Stage.DONE:           "✅ Last pipeline finished! Share new Drive links to publish another video.",
        }
        await ctx.send(sender, _msg(status_msgs.get(stage, "Not sure — try sharing your Drive links again.")))
        return

    if nlu.intent in (Intent.HELP, Intent.GREET) and stage in (Stage.INIT, Stage.AWAITING_LINKS):
        await ctx.send(sender, _msg(
            "👋 I'm **AI Social Media Army** — your automated video publisher!\n\n"
            "Share your video + script from Google Drive and I'll handle:\n"
            "✍️ AI-generated title, description & caption\n"
            "🎬 YouTube upload\n"
            "💼 LinkedIn post\n\n"
            "Paste your two Google Drive links to get started!"
        ))
        s["stage"] = Stage.AWAITING_LINKS.value
        save_session(ctx, sender, s)
        return

    if nlu.intent == Intent.REPEAT:
        await _reprompt(ctx, sender, s, stage)
        return

    # ── STAGE MACHINE ─────────────────────────────────────────────────────────

    # ── INIT / AWAITING_LINKS ─────────────────────────────────────────────────
    if stage in (Stage.INIT, Stage.AWAITING_LINKS):
        drive_urls = nlu.entities.get("drive_urls", [])

        if not drive_urls:
            await ctx.send(sender, _msg(
                "To get started, share two Google Drive links: "
                "one for your **video file** and one for your **script (.txt)**."
            ))
            s["stage"] = Stage.AWAITING_LINKS.value
            save_session(ctx, sender, s)
            return

        await ctx.send(sender, _msg(f"Got {len(drive_urls)} link(s)! Reading your script… ⏳"))

        video_url = script_url = script_text = ""
        for url in drive_urls:
            fid = _drive_file_id(url)
            if not fid:
                continue
            try:
                maybe = await drive_read_text(fid)
            except Exception as e:
                ctx.logger.info(f"[Links] drive_read_text error for fid={fid[:12]}… err={e}")
                maybe = ""
            if maybe and len(maybe) > 30:
                script_url  = url
                script_text = maybe
            else:
                video_url = url

        if len(drive_urls) == 1 and not script_text:
            fid = _drive_file_id(drive_urls[0])
            if fid:
                try:
                    script_text = await drive_read_text(fid)
                except Exception:
                    script_text = ""
            video_url = drive_urls[0]

        if not script_text:
            # Continue anyway using a generic prompt so upload does not block.
            script_text = (
                "Create a strong YouTube title, description, and tags for this video. "
                "Focus on clarity, discoverability, and engagement."
            )
            await ctx.send(sender, _msg(
                "ℹ️ I couldn't read a script file, so I'll continue with auto-generated metadata "
                "and upload your video now."
            ))

        if not video_url:
            await ctx.send(sender, _msg(
                "⚠️ I found the script, but I could not identify a video file link.\n\n"
                "Please send the Google Drive **video file** link as well (mp4/mov), "
                "shared as Anyone with the link -> Viewer."
            ))
            return

        video_file_id = _drive_file_id(video_url) if video_url else ""
        s.update({
            "video_file_id": video_file_id,
            "video_url":     video_url,
            "script_text":   script_text,
            "script_url":    script_url,
        })
        save_session(ctx, sender, s)
        ctx.logger.info(f"[Links] video_fid={video_file_id} script_len={len(script_text)}")
        # If both tokens already exist (common after auth/process restarts),
        # bypass auth prompts and start publishing immediately.
        yt_ok = bool(s.get("yt_token")) and not _is_mock_token(s.get("yt_token", ""))
        li_ok = bool(s.get("li_token")) and not _is_mock_token(s.get("li_token", ""))
        if yt_ok and li_ok:
            s["stage"] = Stage.REVIEW.value
            save_session(ctx, sender, s)
            if _start_pipeline_background(ctx, sender, s):
                await ctx.send(sender, _msg(
                    "✅ Both platforms are already connected.\n\n"
                    "Starting upload workflow now:\n"
                    "• Generate AI-optimized title, description & caption\n"
                    "• Upload your video to YouTube\n"
                    "• Post on LinkedIn"
                ))
            else:
                await ctx.send(sender, _msg("⏳ Pipeline is already running for this chat session."))
            return
        try:
            await _prompt_yt_auth(ctx, sender, s, text)
        except Exception as e:
            ctx.logger.exception(f"[Flow] Failed to transition to YT auth: {e}")
            await ctx.send(sender, _msg(
                f"⚠️ Could not start YouTube authorization: {e}\n\n"
                "Please send **repeat** to retry."
            ))
        return

    # ── YT_AUTH ───────────────────────────────────────────────────────────────
    # Mostly passive — polling task advances it. Handle edge cases here.
    if stage == Stage.YT_AUTH:
        # Token might have landed between polling cycles — check local session first.
        if s.get("yt_token") and not _is_mock_token(s.get("yt_token", "")):
            await _prompt_li_auth(ctx, sender, s, text)
            return
        # If proactive message missed this exact chat session, recover by checking
        # Render token store synchronously on any incoming message.
        yt_live = await _fetch_token(sender, "yt")
        if yt_live and not _is_mock_token(yt_live):
            s["yt_token"] = yt_live
            save_session(ctx, sender, s)
            await _prompt_li_auth(ctx, sender, s, text)
            return

        if nlu.entities.get("drive_urls"):
            await ctx.send(sender, _msg(
                "Your Drive links are saved ✅\n"
                "Still waiting on YouTube auth — click the link in the previous message!"
            ))
            return
        # Avoid auth-link loops on casual user messages.
        await ctx.send(sender, _msg(
            "I am still waiting for YouTube authorization from the existing link.\n\n"
            "If you need a fresh link, reply **repeat**."
        ))
        return

    # ── LI_AUTH ───────────────────────────────────────────────────────────────
    if stage == Stage.LI_AUTH:
        if s.get("li_token") and not _is_mock_token(s.get("li_token", "")):
            await _prompt_review(ctx, sender, s, text)
            return
        li_live = await _fetch_token(sender, "li")
        if li_live and not _is_mock_token(li_live):
            s["li_token"] = li_live
            save_session(ctx, sender, s)
            await _prompt_review(ctx, sender, s, text)
            return

        if nlu.entities.get("drive_urls"):
            await ctx.send(sender, _msg(
                "Your Drive links are saved ✅  Just need LinkedIn auth now — click the link!"
            ))
            return
        await ctx.send(sender, _msg(
            "I am still waiting for LinkedIn authorization from the existing link.\n\n"
            "If you need a fresh link, reply **repeat**."
        ))
        return

    # ── REVIEW ────────────────────────────────────────────────────────────────
    if stage == Stage.REVIEW:
        if nlu.intent == Intent.CONFIRM or (
            nlu.confidence >= 0.55
            and nlu.intent not in (Intent.CANCEL, Intent.HELP, Intent.STATUS, Intent.REPEAT)
        ):
            if _start_pipeline_background(ctx, sender, s):
                await ctx.send(sender, _msg("🚀 Pipeline started in background. I will post progress updates here."))
            else:
                await ctx.send(sender, _msg("⏳ Pipeline is already running for this chat session."))
            return

        await ctx.send(sender, _msg(
            "Shall I go ahead and publish your video? Just say **go** or **publish**!"
        ))
        return

    # ── RUNNING ───────────────────────────────────────────────────────────────
    if stage == Stage.RUNNING:
        await ctx.send(sender, _msg(
            f"⏳ Still working on it! (Job `{s.get('job_id', '')[:8]}`)\n"
            "I'll message you the moment it's done!"
        ))
        return

    # ── DONE ──────────────────────────────────────────────────────────────────
    if stage == Stage.DONE:
        result  = s.get("result", {})
        yt      = result.get("youtube", {})
        li      = result.get("linkedin", {})
        await ctx.send(sender, _msg(
            f"🎉 Your video is live!\n\n"
            f"🎬 YouTube: {yt.get('video_url', 'N/A')}\n"
            f"💼 LinkedIn: {li.get('post_url', 'N/A')}\n\n"
            "Ready to publish another one? Share your Drive links anytime!",
            end=True,
        ))
        return

    # ── Fallback ──────────────────────────────────────────────────────────────
    s["stage"] = Stage.AWAITING_LINKS.value
    save_session(ctx, sender, s)
    await ctx.send(sender, _msg(
        "I'm here to help you publish your video! Share your Google Drive links to begin."
    ))


@chat_proto.on_message(ChatAcknowledgement)
async def on_ack(_ctx: Context, _sender: str, _msg: ChatAcknowledgement) -> None:
    return


# ══════════════════════════════════════════════════════════════════════════════
#  STARTUP — verify Render connectivity; no local server needed
# ══════════════════════════════════════════════════════════════════════════════

@agent.on_event("startup")
async def startup_handler(ctx: Context) -> None:
    global _agent_ctx
    _agent_ctx = ctx

    ctx.logger.info(f"🚀 Agent starting: {ctx.agent.name} at {ctx.agent.address}")
    ctx.logger.info(f"   Mailbox      : {USE_MAILBOX}")
    ctx.logger.info(f"   OAuth server : {OAUTH_SERVER_URL or '⚠ NOT SET'}")
    ctx.logger.info(f"   ASI1 key     : {'✓ set' if ASI1_API_KEY else '✗ MISSING'}")
    ctx.logger.info(f"   YouTube      : {'✓ configured' if _oauth_ready(GOOGLE_CLIENT_ID, GOOGLE_REDIRECT_URI) else '⚠ demo mode'}")
    ctx.logger.info(f"   LinkedIn     : {'✓ configured' if _oauth_ready(LINKEDIN_CLIENT_ID, LINKEDIN_REDIRECT_URI) else '⚠ demo mode'}")

    # Health-check the Render OAuth server
    if OAUTH_SERVER_URL:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{OAUTH_SERVER_URL}/")
                if r.status_code == 200:
                    ctx.logger.info(f"✅ Render OAuth server reachable at {OAUTH_SERVER_URL}")
                else:
                    hint = ""
                    if r.status_code == 404:
                        hint = (
                            " (GET / returned 404 — use the same base URL as in "
                            "GOOGLE_REDIRECT_URI / LINKEDIN_REDIRECT_URI, e.g. "
                            "https://<your-service>.onrender.com)"
                        )
                    ctx.logger.warning(f"⚠ Render OAuth server returned {r.status_code}{hint}")
        except Exception as e:
            ctx.logger.warning(f"⚠ Could not reach Render OAuth server: {e}")
    else:
        ctx.logger.warning("⚠ OAUTH_SERVER_URL not set — OAuth will run in demo/mock mode only")


# ══════════════════════════════════════════════════════════════════════════════
#  INCLUDE + RUN
# ══════════════════════════════════════════════════════════════════════════════

agent.include(chat_proto, publish_manifest=True)

if __name__ == "__main__":
    logger.info("=" * 64)
    logger.info("  AI Social Media Army — Unified Agent v4")
    logger.info(f"  Address      : {agent.address}")
    logger.info(f"  Mailbox      : {USE_MAILBOX}")
    logger.info(f"  OAuth server : {OAUTH_SERVER_URL or 'NOT SET'}")
    logger.info(f"  ASI1 key     : {'✓ set' if ASI1_API_KEY else '✗ MISSING'}")
    logger.info(f"  YouTube      : {'✓ configured' if _oauth_ready(GOOGLE_CLIENT_ID, GOOGLE_REDIRECT_URI) else '⚠ demo mode'}")
    logger.info(f"  LinkedIn     : {'✓ configured' if _oauth_ready(LINKEDIN_CLIENT_ID, LINKEDIN_REDIRECT_URI) else '⚠ demo mode'}")
    logger.info("=" * 64)
    agent.run()
