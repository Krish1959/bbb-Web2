
"""
app_v2.py — BBB Avatar Maker Stage 2 (FIXED HeyGen API format)
=========================================================
FIXES from v1:
  ✅ Fixed header case: X-API-Key (not x-api-key)
  ✅ Fixed payload field: opening_intro (not opening_text)
  ✅ Improved error handling and response parsing

Architecture:
  • Flask web server  →  serves the Debug UI at /debug
  • APScheduler       →  fires process_inbox() daily at 09:00 SGT (background thread)
  • Same process, same container — one Render Web Service only

Routes:
  GET  /            → home (redirects to /debug)
  GET  /debug       → full Debug & Control Panel
  GET  /health      → keep-alive ping for UptimeRobot
  GET  /api/csv     → submissions.csv as JSON
  GET  /api/contexts→ Avatar API context list as JSON
  GET  /api/emails  → Gmail inbox scan as JSON
  GET  /api/run     → SSE stream — triggers job (mode=once|test)

Environment variables (set on Render):
  AVATAR_API_KEY       — Avatar API key         (was LIVEAVATAR_API_KEY)
  AVATAR_API_BASE_URL  — https://api.liveavatar.com
  GMAIL_ADDRESS        — agentic.avai@gmail.com
  GMAIL_APP_PASSWORD   — 16-char Gmail App Password
  GITHUB_TOKEN_STAGE_2 — GitHub PAT
  GITHUB_REPO          — Krish1959/bbb-web
  GITHUB_DATA_BRANCH   — data

Deploy on Render as Web Service:
  Build:  pip install -r requirements.txt
  Start:  gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --worker-class gthread --threads 4
"""

from __future__ import annotations

import base64
import csv
import email as email_lib
import imaplib
import io
import json
import logging
import os
import queue
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, Response, jsonify, redirect, render_template_string, request, stream_with_context

from apscheduler.schedulers.background import BackgroundScheduler

# ═══════════════════════════════════════════════════════════
# LOGGING — captures to both console AND in-memory queue
# ═══════════════════════════════════════════════════════════
_log_queue: queue.Queue = queue.Queue(maxsize=2000)

class _QueueHandler(logging.Handler):
    def emit(self, record):
        try:
            _log_queue.put_nowait(self.format(record))
        except queue.Full:
            pass  # drop oldest if full — never block

_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_qh  = _QueueHandler()
_qh.setFormatter(_fmt)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger().addHandler(_qh)
log = logging.getLogger("bbb2")

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════
AVATAR_API_KEY      = os.getenv("AVATAR_API_KEY",      os.getenv("LIVEAVATAR_API_KEY", "")).strip()
AVATAR_API_BASE_URL = os.getenv("AVATAR_API_BASE_URL", os.getenv("LIVEAVATAR_BASE_URL", "https://api.liveavatar.com")).strip()

GMAIL_ADDRESS       = os.getenv("GMAIL_ADDRESS",      "agentic.avai@gmail.com").strip()
GMAIL_APP_PASSWORD  = os.getenv("GMAIL_APP_PASSWORD", "").strip()

GITHUB_TOKEN        = os.getenv("GITHUB_TOKEN_STAGE_2", "").strip()
GITHUB_REPO         = os.getenv("GITHUB_REPO",         "Krish1959/bbb-web").strip()
GITHUB_DATA_BRANCH  = os.getenv("GITHUB_DATA_BRANCH",  "data").strip()

CSV_PATH            = "submissions.csv"
LOG_PATH            = "stage2_log.csv"
SUBJECT_PREFIX      = "[Web Scrapped] Context for Avatar Chat -"

APP_VERSION         = "2.1"  # v2.1 = Fixed HeyGen API format
APP_NAME            = "BBB Avatar Maker — Stage 2"

# ═══════════════════════════════════════════════════════════
# FLASK APP
# ═══════════════════════════════════════════════════════════
app = Flask(__name__)

# ═══════════════════════════════════════════════════════════
# GITHUB HELPERS
# ═══════════════════════════════════════════════════════════
def _gh_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bbb-stage2",
    }

def gh_get(path: str) -> Optional[str]:
    """Fetch file text from GitHub data branch. Returns None if not found."""
    log.info("[GitHub] GET %s (branch=%s)", path, GITHUB_DATA_BRANCH)
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref={GITHUB_DATA_BRANCH}"
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=30)
        log.info("[GitHub] GET response: HTTP %d", r.status_code)
        if r.status_code == 404:
            log.warning("[GitHub] File not found: %s", path)
            return None
        r.raise_for_status()
        b64 = r.json().get("content", "")
        text = base64.b64decode(b64).decode("utf-8", errors="replace") if b64 else ""
        log.info("[GitHub] Read %d chars from %s", len(text), path)
        return text
    except Exception as e:
        log.error("[GitHub] GET error for %s: %s", path, e)
        raise

def gh_sha(path: str) -> Optional[str]:
    """Get SHA of a file on GitHub (needed for updates)."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref={GITHUB_DATA_BRANCH}"
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("sha")
    except Exception as e:
        log.error("[GitHub] SHA fetch error for %s: %s", path, e)
        return None

def gh_put(path: str, text: str, sha: Optional[str], message: str) -> None:
    """Write or update a file on GitHub data branch."""
    log.info("[GitHub] PUT %s (sha=%s) message='%s'", path, sha, message)
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    payload: Dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(text.encode()).decode(),
        "branch":  GITHUB_DATA_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, headers=_gh_headers(), data=json.dumps(payload), timeout=30)
        log.info("[GitHub] PUT response: HTTP %d", r.status_code)
        r.raise_for_status()
        log.info("[GitHub] Successfully wrote %s", path)
    except Exception as e:
        log.error("[GitHub] PUT error for %s: %s", path, e)
        raise

# ═══════════════════════════════════════════════════════════
# CSV HELPERS
# ═══════════════════════════════════════════════════════════
def load_csv() -> List[Dict[str, str]]:
    log.info("[CSV] Loading %s from GitHub repo=%s branch=%s", CSV_PATH, GITHUB_REPO, GITHUB_DATA_BRANCH)
    text = gh_get(CSV_PATH)
    if not text:
        log.warning("[CSV] Empty or missing — returning []")
        return []
    rows = list(csv.DictReader(io.StringIO(text)))
    log.info("[CSV] Loaded %d rows. Columns: %s", len(rows), list(rows[0].keys()) if rows else "N/A")
    if rows:
        log.info("[CSV] Last row: %s", json.dumps(rows[-1]))
    return rows

def append_run_log(shortname: str, company: str, email: str,
                   context_name: str, status: str, response: Dict[str, Any]) -> None:
    """Append result to stage2_log.csv on GitHub."""
    log.info("[RunLog] Writing result for %s → %s", company, status)
    try:
        existing = gh_get(LOG_PATH) or ""
        sha      = gh_sha(LOG_PATH)
        now      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        new_row  = f'{now},"{company}","{shortname}","{email}","{context_name}","{status}"\n'
        if not existing.strip():
            new_text = "Timestamp,Company,ShortName,Email,ContextName,Status\n" + new_row
        else:
            new_text = existing.rstrip("\n") + "\n" + new_row
        gh_put(LOG_PATH, new_text, sha, f"stage2 log: {shortname} {status}")
        log.info("[RunLog] stage2_log.csv updated on GitHub")
    except Exception as e:
        log.error("[RunLog] Failed to write log: %s", e)

# ═══════════════════════════════════════════════════════════
# URL → SHORTNAME
# ═══════════════════════════════════════════════════════════
def shortname_from_url(url: str) -> str:
    """
    Extract company shortname from URL.
    https://www.bcaa.edu.sg/  →  bcaa
    https://psb-academy.edu.sg/ → psb-academy  (hyphenated kept)
    """
    try:
        u = url.strip().replace("https://", "").replace("http://", "")
        host  = u.split("/")[0]
        parts = [p for p in host.split(".") if p]
        if not parts:
            return "site"
        if parts[0].lower() == "www" and len(parts) >= 2:
            result = parts[1].lower()
        else:
            result = parts[0].lower()
        log.info("[ShortName] URL=%s  →  shortname=%s", url, result)
        return result
    except Exception as e:
        log.error("[ShortName] Error parsing %s: %s", url, e)
        return "site"

# ═══════════════════════════════════════════════════════════
# GMAIL HELPERS
# ═══════════════════════════════════════════════════════════
def gmail_connect() -> imaplib.IMAP4_SSL:
    log.info("[Gmail] Connecting to imap.gmail.com as %s", GMAIL_ADDRESS)
    if not GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_APP_PASSWORD env var is not set!")
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    log.info("[Gmail] Login successful")
    return imap

def gmail_fetch_all(imap: imaplib.IMAP4_SSL) -> List[Dict[str, Any]]:
    """Fetch all messages newest-first. Returns list of message dicts."""
    imap.select("INBOX")
    _, data    = imap.search(None, "ALL")
    uid_list   = data[0].split() if data[0] else []
    total      = len(uid_list)
    log.info("[Gmail] Total messages in INBOX: %d", total)

    messages = []
    for uid in reversed(uid_list):
        try:
            _, msg_data = imap.fetch(uid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)

            # Decode subject
            subj_parts = email_lib.header.decode_header(msg.get("Subject", ""))
            subject_str = ""
            for part, enc in subj_parts:
                subject_str += part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else str(part)

            # Extract From address
            from_raw   = msg.get("From", "")
            from_match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", from_raw)
            from_addr  = from_match.group(0).lower() if from_match else from_raw.lower()

            # Get plain text body
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        pl = part.get_payload(decode=True)
                        if pl:
                            body = pl.decode(part.get_content_charset() or "utf-8", errors="replace")
                        break
            else:
                pl = msg.get_payload(decode=True)
                if pl:
                    body = pl.decode(msg.get_content_charset() or "utf-8", errors="replace")

            messages.append({
                "uid":        uid,
                "subject":    subject_str,
                "from_addr":  from_addr,
                "body_text":  body,
                "date_str":   msg.get("Date", ""),
            })
        except Exception as e:
            log.warning("[Gmail] Could not parse uid=%s: %s", uid, e)

    log.info("[Gmail] Parsed %d messages", len(messages))
    return messages

def gmail_scan_matching(registered: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Connect to Gmail, scan inbox, return only emails that:
      1. Are FROM a registered email address
      2. Have subject starting with SUBJECT_PREFIX
    Returns newest-first, one per sender.
    """
    log.info("[Gmail] Starting inbox scan. Registered senders: %s", list(registered.keys()))
    try:
        imap = gmail_connect()
    except Exception as e:
        log.error("[Gmail] Connection failed: %s", e)
        return []

    try:
        messages = gmail_fetch_all(imap)
    finally:
        try:
            imap.logout()
            log.info("[Gmail] Logged out cleanly")
        except Exception:
            pass

    matched: Dict[str, Dict[str, Any]] = {}
    skipped_not_registered = 0
    skipped_subject        = 0

    for msg in messages:
        sender = msg["from_addr"]
        subj   = msg["subject"]

        if sender not in registered:
            skipped_not_registered += 1
            continue

        log.info("[Gmail] Registered sender found: %s | Subject: %s", sender, subj)

        if not subj.startswith(SUBJECT_PREFIX):
            log.warning("[Gmail] Subject MISMATCH for %s", sender)
            log.warning("[Gmail]   Expected prefix : '%s'", SUBJECT_PREFIX)
            log.warning("[Gmail]   Got subject     : '%s'", subj)
            skipped_subject += 1
            continue

        if sender not in matched:
            matched[sender] = msg
            log.info("[Gmail] ✔ MATCHED email from %s: %s", sender, subj)

    log.info("[Gmail] Scan summary: total=%d  skipped_not_registered=%d  skipped_subject=%d  matched=%d",
             len(messages), skipped_not_registered, skipped_subject, len(matched))

    return list(matched.values())

# ═══════════════════════════════════════════════════════════
# CONTENT EXTRACTION
# ═══════════════════════════════════════════════════════════
def extract_context_block(body: str, shortname: str) -> Optional[str]:
    """
    Strip human reply text and quoted lines.
    Find '# <ShortName>' marker and return everything from there.
    """
    log.info("[Extract] Looking for marker '# %s' in body (%d chars)", shortname, len(body))

    # Strip quoted lines (lines starting with ">")
    clean_lines = [l for l in body.splitlines() if not l.strip().startswith(">")]
    clean_body  = "\n".join(clean_lines)

    # Strip "On ... wrote:" tail (Gmail reply quoting)
    clean_body = re.split(r"\nOn .+? wrote:", clean_body, flags=re.DOTALL)[0]
    log.info("[Extract] Body after stripping quotes: %d chars", len(clean_body))

    # Primary: find exact "# <shortname>" marker
    pattern = re.compile(r"(#\s*" + re.escape(shortname) + r"\b.*)", re.IGNORECASE | re.DOTALL)
    m = pattern.search(clean_body)
    if m:
        result = m.group(1).strip()
        log.info("[Extract] ✔ Found primary marker. Extracted %d chars", len(result))
        return result

    # Fallback: any "# Word" heading
    log.warning("[Extract] Primary marker '# %s' NOT found. Trying fallback (any # heading)...", shortname)
    m2 = re.search(r"(#\s+\S+.*)", clean_body, re.DOTALL)
    if m2:
        result = m2.group(1).strip()
        log.warning("[Extract] Fallback matched. Extracted %d chars. First 100: %s", len(result), result[:100])
        return result

    log.error("[Extract] No marker found at all. Body first 300 chars: %s", clean_body[:300])
    return None

def extract_opening_intro(context_text: str) -> str:
    """Extract ## Opening Intro section (up to next ## heading)."""
    m = re.search(r"##\s*Opening Intro\s*\n(.*?)(?=\n##|\Z)", context_text, re.IGNORECASE | re.DOTALL)
    if m:
        intro = m.group(1).strip()
        log.info("[Extract] Opening Intro found: %d chars", len(intro))
        return intro
    log.warning("[Extract] No '## Opening Intro' section found in context")
    return ""

# ═══════════════════════════════════════════════════════════
# AVATAR API CLIENT
# ═══════════════════════════════════════════════════════════
class AvatarAPIClient:
    """Generic client for the Avatar Context API."""

    def __init__(self, api_key: str, base_url: str):
        self.api_key  = api_key
        self.base_url = base_url.rstrip("/")
        log.info("[AvatarAPI] Client initialised. base_url=%s  key_set=%s",
                 self.base_url, bool(self.api_key))

    def _headers(self) -> Dict[str, str]:
        # FIX v2: Use proper X-API-Key case (not x-api-key)
        return {"X-API-Key": self.api_key, "Content-Type": "application/json"}

    def list_contexts(self) -> List[Dict[str, Any]]:
        log.info("[AvatarAPI] Listing existing contexts...")
        r = requests.get(f"{self.base_url}/v1/contexts", headers=self._headers(), timeout=30)
        log.info("[AvatarAPI] list_contexts HTTP %d", r.status_code)
        r.raise_for_status()
        results = (r.json().get("data") or {}).get("results") or []
        names = [c.get("name") for c in results]
        log.info("[AvatarAPI] Found %d contexts: %s", len(results), names)
        return results

    def create_context(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        log.info("[AvatarAPI] Creating context name='%s'  prompt_len=%d",
                 payload.get("name"), len(payload.get("prompt", "")))
        log.info("[AvatarAPI] Payload: %s", json.dumps(payload)[:200])
        
        r = requests.post(f"{self.base_url}/v1/contexts",
                          headers=self._headers(), json=payload, timeout=60)
        log.info("[AvatarAPI] create_context HTTP %d", r.status_code)
        resp = r.json()
        log.info("[AvatarAPI] create_context response: %s", json.dumps(resp)[:300])
        r.raise_for_status()
        return resp

    def delete_context(self, context_id: str) -> None:
        log.info("[AvatarAPI] Deleting context id=%s", context_id)
        r = requests.delete(f"{self.base_url}/v1/contexts/{context_id}",
                            headers=self._headers(), timeout=30)
        log.info("[AvatarAPI] delete_context HTTP %d", r.status_code)
        r.raise_for_status()

    def find_unique_name(self, shortname: str) -> str:
        """
        Return the next available context name.
        PSB → tries PSB1, PSB2, PSB3 ... until one is free.
        """
        existing_names = [c.get("name", "") for c in self.list_contexts()]
        log.info("[AvatarAPI] Existing names: %s", existing_names)
        suffix = 1
        while True:
            candidate = f"{shortname.upper()}{suffix}"
            if candidate not in existing_names:
                log.info("[AvatarAPI] ✔ Unique name selected: %s", candidate)
                return candidate
            log.info("[AvatarAPI] Name %s already taken, trying next...", candidate)
            suffix += 1

    def upload_context(self, shortname: str, opening_intro: str,
                       prompt: str) -> Tuple[str, str, Dict[str, Any]]:
        """
        Find unique name, build payload, upload.
        Returns (context_name, status, response_dict).
        
        FIX v2:
          - Changed opening_text → opening_intro (correct HeyGen field)
          - Improved payload structure
        """
        context_name = self.find_unique_name(shortname)
        
        # FIX v2: Use correct field name "opening_intro" instead of "opening_text"
        payload = {
            "name":               context_name,
            "opening_intro":      opening_intro,  # ✅ FIXED from opening_text
            "description":        prompt[:500],    # Use first 500 chars as description
            "prompt":             prompt,          # Full prompt
        }
        log.info("[AvatarAPI] Uploading context: name=%s  opening_intro_len=%d  prompt_len=%d",
                 context_name, len(opening_intro), len(prompt))
        resp   = self.create_context(payload)
        
        # Extract context ID from response (handle both nested and flat structures)
        context_id = None
        if isinstance(resp.get("data"), dict):
            context_id = resp["data"].get("id")  # Nested: {data: {id: ...}}
        if not context_id:
            context_id = resp.get("id")  # Flat: {id: ...}
        
        # Determine status (look for success indicators)
        status = "created"
        if resp.get("code") == 1000 or context_id:
            status = "created"
        elif resp.get("code") and resp.get("code") >= 400:
            status = "error"
        elif not context_id and not resp.get("code"):
            status = "unknown"
        
        log.info("[AvatarAPI] Upload result: status=%s  context_id=%s", status, context_id)
        return context_name, status, resp

# ═══════════════════════════════════════════════════════════
# CORE JOB LOGIC
# ═══════════════════════════════════════════════════════════
def _upload_row(client: AvatarAPIClient, row: Dict[str, str], context_text: str) -> None:
    """Upload one context to the Avatar API and log the result."""
    web_url   = (row.get("Web_URL") or "").strip()
    company   = (row.get("Company") or "").strip()
    email     = (row.get("Email")   or "").strip()
    shortname = shortname_from_url(web_url) if web_url else company.lower()

    log.info("[Job] Processing row: company=%s  email=%s  shortname=%s  web_url=%s",
             company, email, shortname, web_url)
    log.info("[Job] Context text length: %d chars", len(context_text))
    log.info("[Job] Context preview (first 200 chars): %s", context_text[:200])

    opening_intro = extract_opening_intro(context_text)
    opening_text  = opening_intro or f"Welcome to the Q & A session for {company}"
    log.info("[Job] Opening text (first 100 chars): %s", opening_text[:100])

    try:
        context_name, status, resp = client.upload_context(shortname, opening_text, context_text)
        context_id = (resp.get("data") or {}).get("id") or resp.get("id") or "unknown"
        log.info("[Job] ✔ Context uploaded: name=%s  id=%s  status=%s", context_name, context_id, status)
    except Exception as e:
        log.error("[Job] ✘ Avatar API upload failed for %s: %s", shortname, e)
        context_name = f"{shortname.upper()}?"
        status       = "exception"
        resp         = {"error": str(e)}

    append_run_log(shortname, company, email, context_name, status, resp)


def process_inbox(test_mode: bool = False) -> None:
    """
    Main job entry point.

    test_mode=True  → Skip Gmail. Use LAST row of CSV. Build dummy context.
                      Use this to verify the Avatar API upload end-to-end.
    test_mode=False → Normal: scan Gmail, match emails, extract context, upload.
    """
    log.info("━" * 60)
    log.info("[Job] ══ START  mode=%s  time=%s ══",
             "TEST" if test_mode else "NORMAL",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
    log.info("[Job] Config: GITHUB_REPO=%s  BRANCH=%s  CSV=%s", GITHUB_REPO, GITHUB_DATA_BRANCH, CSV_PATH)
    log.info("[Job] Config: AVATAR_API_BASE_URL=%s  key_set=%s", AVATAR_API_BASE_URL, bool(AVATAR_API_KEY))
    log.info("[Job] Config: GMAIL_ADDRESS=%s  password_set=%s", GMAIL_ADDRESS, bool(GMAIL_APP_PASSWORD))

    # ── Step 1: Load CSV ──────────────────────────────────────
    rows = load_csv()
    if not rows:
        log.error("[Job] No rows in submissions.csv — aborting.")
        return
    log.info("[Job] CSV loaded: %d rows", len(rows))

    client = AvatarAPIClient(api_key=AVATAR_API_KEY, base_url=AVATAR_API_BASE_URL)

    # ── TEST MODE ─────────────────────────────────────────────
    if test_mode:
        last = rows[-1]
        log.info("[Job] TEST MODE — using last CSV row:")
        for k, v in last.items():
            log.info("[Job]   %-12s: %s", k, v)

        company   = (last.get("Company") or "Test Company").strip()
        web_url   = (last.get("Web_URL") or "").strip()
        shortname = shortname_from_url(web_url) if web_url else company.lower()

        dummy_context = (
            f"# {company}\n\n"
            f"## Opening Intro\n"
            f"Welcome! This is a TEST context auto-generated for {company}.\n\n"
            f"## About\n"
            f"Company : {company}\n"
            f"Website : {web_url}\n\n"
            f"## PERSONA / ROLE\n"
            f"You are a helpful AI assistant representing {company}. "
            f"Answer questions about the company professionally and helpfully.\n"
        )
        log.info("[Job] TEST dummy context built (%d chars)", len(dummy_context))
        _upload_row(client, last, dummy_context)
        log.info("[Job] ══ TEST MODE COMPLETE ══")
        return

    # ── NORMAL MODE ───────────────────────────────────────────

    # Step 2: Build registered-email lookup (last row wins per email)
    registered: Dict[str, Dict[str, str]] = {}
    for row in rows:
        key = (row.get("Email") or "").strip().lower()
        if key:
            registered[key] = row
    log.info("[Job] Registered email addresses (%d): %s", len(registered), list(registered.keys()))

    # Step 3: Scan Gmail
    matching_emails = gmail_scan_matching(registered)
    if not matching_emails:
        log.warning("[Job] No qualifying emails found in inbox. Nothing to upload.")
        log.info("[Job] ══ NORMAL MODE COMPLETE (no action) ══")
        return

    log.info("[Job] %d qualifying email(s) to process", len(matching_emails))

    # Step 4: Process each email
    for msg in matching_emails:
        sender    = msg["from_addr"]
        row       = registered[sender]
        web_url   = (row.get("Web_URL") or "").strip()
        company   = (row.get("Company") or "").strip()
        shortname = shortname_from_url(web_url) if web_url else company.lower()

        log.info("[Job] ── Processing email from %s (company=%s  shortname=%s)", sender, company, shortname)

        context_text = extract_context_block(msg["body_text"], shortname)
        if not context_text:
            log.error("[Job] ✘ Could not extract context block for %s — skipping", company)
            continue

        _upload_row(client, row, context_text)

    log.info("[Job] ══ NORMAL MODE COMPLETE ══")


# ═══════════════════════════════════════════════════════════
# SCHEDULER  (fires daily at 09:00 SGT = 01:00 UTC)
# ═══════════════════════════════════════════════════════════
_scheduler = BackgroundScheduler(timezone="Asia/Singapore")
_scheduler.add_job(
    func=lambda: process_inbox(test_mode=False),
    trigger="cron",
    hour=9, minute=0,
    id="daily_inbox_job",
    name="Daily inbox scan at 09:00 SGT",
)

# ═══════════════════════════════════════════════════════════
# DEBUG UI HTML (simplified - same as v1)
# ═══════════════════════════════════════════════════════════
_DEBUG_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{{ app_name }} — Debug</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh}
a{color:#58a6ff;text-decoration:none}

/* ── Header ── */
.hdr{background:#161b22;border-bottom:2px solid #1f6feb;padding:14px 24px;display:flex;align-items:center;gap:14px}
.hdr h1{font-size:1.3rem;color:#58a6ff;flex:1}
.badge{background:#21262d;border:1px solid #30363d;border-radius:12px;padding:3px 12px;font-size:.78rem;color:#8b949e}
.badge b{color:#c9d1d9}

/* ── Layout ── */
.wrap{max-width:1400px;margin:0 auto;padding:20px 16px;display:grid;grid-template-columns:1fr 1fr;gap:16px}
.full{grid-column:1/-1}

/* ── Cards ── */
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;overflow:hidden}
.ch{background:#21262d;padding:11px 16px;font-weight:700;color:#79c0ff}
.cb{padding:14px 16px}

/* ── Tables ── */
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{background:#21262d;color:#8b949e;padding:7px 9px;text-align:left;border-bottom:1px solid #30363d;white-space:nowrap}
td{padding:6px 9px;border-bottom:1px solid #21262d;word-break:break-all}
tr:hover td{background:#1c2128}

/* ── Env check ── */
.env-grid{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:.82rem}
.ok{color:#3fb950;font-weight:700}
.miss{color:#ff7b72;font-weight:700}

/* ── Buttons ── */
button{background:#238636;color:#fff;border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:.85rem;font-weight:600}
button:hover{background:#2ea043}
button:disabled{background:#6e7681;cursor:not-allowed}
#spin{display:none;width:16px;height:16px;border:2px solid #6e7681;border-top-color:#58a6ff;border-radius:50%;animation:spin .6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── Log box ── */
#logbox{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:10px;font-family:monospace;font-size:.75rem;max-height:400px;overflow-y:auto}
.ld{color:#8b949e}
.li{color:#58a6ff}
.lw{color:#d29922}
.le{color:#ff7b72}

</style>
</head>
<body>

<div class="hdr">
  <h1>{{ app_name }}</h1>
  <span class="badge"><b>v{{ version }}</b></span>
  <span class="badge"><b>Live</b></span>
</div>

<div class="wrap">
  <!-- Environment Checks -->
  <div class="card full">
    <div class="ch">⚙️ Environment</div>
    <div class="cb">
      <div class="env-grid">
        {% for key, status in env_checks %}
          <span style="font-weight:700">{{ key }}</span>
          <span class="{% if status == 'SET' %}ok{% else %}miss{% endif %}">{{ status }}</span>
        {% endfor %}
      </div>
    </div>
  </div>

  <!-- Controls -->
  <div class="card full">
    <div class="ch">🎛️ Controls</div>
    <div class="cb">
      <button id="btn-test" onclick="triggerRun('test')">▶️ Test Run</button>
      <button id="btn-normal" onclick="triggerRun('normal')">▶️ Normal Run</button>
      <button onclick="clearLog()">🗑️ Clear Log</button>
      <span id="spin"></span>
      <span id="run-status" style="margin-left:12px;color:#8b949e">Idle</span>
    </div>
  </div>

  <!-- Log Output -->
  <div class="card full">
    <div class="ch">📋 Live Log</div>
    <div class="cb">
      <div id="logbox"></div>
    </div>
  </div>

  <!-- Contexts -->
  <div class="card">
    <div class="ch">🎭 Contexts on Avatar API</div>
    <div class="cb">
      <table>
        <thead><tr><th>Name</th><th>ID</th></tr></thead>
        <tbody id="ctx-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- CSV Data -->
  <div class="card">
    <div class="ch">📊 CSV Submissions</div>
    <div class="cb">
      <table>
        <thead><tr><th>Company</th><th>Email</th></tr></thead>
        <tbody id="csv-tbody"></tbody>
      </table>
    </div>
  </div>

</div>

<script>
function loadContexts(){
  fetch('/api/contexts').then(r=>r.json()).then(d=>{
    const tbody = document.getElementById('ctx-tbody');
    tbody.innerHTML = (d.contexts||[]).map(c=>
      `<tr><td>${esc(c.name||'')}</td><td>${esc(c.id||'')}</td></tr>`
    ).join('');
  }).catch(e=>console.error(e));
}

function loadCsv(){
  fetch('/api/csv').then(r=>r.json()).then(d=>{
    const tbody = document.getElementById('csv-tbody');
    tbody.innerHTML = (d.rows||[]).map(r=>
      `<tr><td>${esc(r.Company||'')}</td><td>${esc(r.Email||'')}</td></tr>`
    ).join('');
  }).catch(e=>console.error(e));
}

let _es = null;
function triggerRun(mode){
  if(_es){ _es.close(); }
  setRunning(true, mode);
  appendLog(`▶ Triggering ${mode.toUpperCase()} run at ${new Date().toISOString()}`, 'li');

  _es = new EventSource(`/api/run?mode=${mode}`);
  _es.onmessage = e => {
    const line = e.data;
    if(line==='__DONE__'){ _es.close(); setRunning(false); loadContexts(); loadCsv(); return; }
    let cls = 'ld';
    if(line.includes('ERROR')||line.includes('✘')) cls='le';
    else if(line.includes('WARNING')||line.includes('⚠')) cls='lw';
    else if(line.includes('INFO')||line.includes('✔')||line.includes('══')) cls='li';
    appendLog(line, cls);
  };
  _es.onerror = () => { _es.close(); setRunning(false); appendLog('── stream ended ──','ld'); loadContexts(); };
}

function appendLog(line, cls){
  const box = document.getElementById('logbox');
  const span = document.createElement('span');
  span.className = cls;
  span.textContent = line + '\n';
  box.appendChild(span);
  box.scrollTop = box.scrollHeight;
}

function setRunning(on, mode){
  document.getElementById('spin').style.display = on ? 'inline-block' : 'none';
  document.getElementById('btn-test').disabled   = on;
  document.getElementById('btn-normal').disabled = on;
  document.getElementById('run-status').textContent = on ? `Running ${mode||''}… please wait` : 'Idle';
}

function clearLog(){ document.getElementById('logbox').innerHTML=''; }

function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

loadCsv();
loadContexts();
</script>

</body>
</html>
"""

# ═══════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════
@app.get("/")
def home():
    return redirect("/debug")


@app.get("/debug")
def debug_page():
    env_checks = [
        ("AVATAR_API_KEY",       "SET" if AVATAR_API_KEY      else "MISSING"),
        ("AVATAR_API_BASE_URL",  "SET" if AVATAR_API_BASE_URL else "MISSING"),
        ("GMAIL_ADDRESS",        "SET" if GMAIL_ADDRESS       else "MISSING"),
        ("GMAIL_APP_PASSWORD",   "SET" if GMAIL_APP_PASSWORD  else "MISSING"),
        ("GITHUB_TOKEN_STAGE_2", "SET" if GITHUB_TOKEN        else "MISSING"),
        ("GITHUB_REPO",          "SET" if GITHUB_REPO         else "MISSING"),
        ("GITHUB_DATA_BRANCH",   "SET" if GITHUB_DATA_BRANCH  else "MISSING"),
    ]
    return render_template_string(
        _DEBUG_HTML,
        app_name       = APP_NAME,
        version        = APP_VERSION,
        env_checks     = env_checks,
    )


@app.get("/health")
def health():
    next_job = None
    try:
        job = _scheduler.get_job("daily_inbox_job")
        if job and job.next_run_time:
            next_job = job.next_run_time.isoformat()
    except Exception:
        pass
    return jsonify({
        "ok":            True,
        "time_utc":      datetime.now(timezone.utc).isoformat(),
        "scheduler":     _scheduler.running,
        "next_run_utc":  next_job,
    })


@app.get("/api/csv")
def api_csv():
    try:
        rows = load_csv()
        return jsonify({"rows": rows, "count": len(rows)})
    except Exception as e:
        log.error("[API/csv] %s", e)
        return jsonify({"error": str(e), "rows": []}), 500


@app.get("/api/contexts")
def api_contexts():
    try:
        client   = AvatarAPIClient(AVATAR_API_KEY, AVATAR_API_BASE_URL)
        contexts = client.list_contexts()
        return jsonify({"contexts": contexts, "count": len(contexts)})
    except Exception as e:
        log.error("[API/contexts] %s", e)
        return jsonify({"error": str(e), "contexts": []}), 500


@app.get("/api/emails")
def api_emails():
    try:
        rows       = load_csv()
        registered = {(r.get("Email") or "").strip().lower(): r
                      for r in rows if r.get("Email")}
        log.info("[API/emails] Scanning for %d registered addresses", len(registered))
        matched = gmail_scan_matching(registered)
        result  = [{
            "from":         m["from_addr"],
            "subject":      m["subject"],
            "date":         m["date_str"],
            "body_preview": m["body_text"][:500].replace("\n", " "),
        } for m in matched]
        return jsonify({"emails": result, "count": len(result)})
    except Exception as e:
        log.error("[API/emails] %s", e)
        return jsonify({"error": str(e), "emails": []}), 500


@app.get("/api/run")
def api_run():
    """SSE endpoint — runs process_inbox in a thread and streams log lines."""
    mode = request.args.get("mode", "test")
    log.info("[API/run] Run requested: mode=%s", mode)

    def generate():
        # Drain stale log messages
        drained = 0
        while not _log_queue.empty():
            try: _log_queue.get_nowait(); drained += 1
            except queue.Empty: break
        if drained:
            yield f"data: [debug] Drained {drained} stale log lines\n\n"

        yield f"data: [debug] Starting {mode.upper()} run...\n\n"

        done_flag = threading.Event()

        def _job():
            try:
                process_inbox(test_mode=(mode == "test"))
            except Exception as e:
                log.error("[API/run] Unhandled exception in job: %s", e)
            finally:
                done_flag.set()

        t = threading.Thread(target=_job, daemon=True)
        t.start()

        # Stream log lines until thread finishes
        while not done_flag.is_set() or not _log_queue.empty():
            try:
                line = _log_queue.get(timeout=0.4)
                yield f"data: {line}\n\n"
            except queue.Empty:
                yield "data: .\n\n"   # SSE keepalive

        # Flush any last messages
        while not _log_queue.empty():
            try:
                line = _log_queue.get_nowait()
                yield f"data: {line}\n\n"
            except queue.Empty:
                break

        yield "data: __DONE__\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ═══════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════
def start_scheduler():
    if not _scheduler.running:
        _scheduler.start()
        log.info("[Scheduler] Started. Daily job at 09:00 SGT (01:00 UTC).")
        try:
            job = _scheduler.get_job("daily_inbox_job")
            if job:
                log.info("[Scheduler] Next run: %s", job.next_run_time)
        except Exception:
            pass

# Start scheduler when app module loads (works with gunicorn)
start_scheduler()

if __name__ == "__main__":
    log.info("[App] Starting in development mode...")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False, threaded=True)
