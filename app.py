"""
app.py — BBB Avatar Maker Stage 2  (Single Unified App)
=========================================================
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

APP_VERSION         = "2.0"
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
        return {"x-api-key": self.api_key, "Content-Type": "application/json"}

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

    def upload_context(self, shortname: str, opening_text: str,
                       prompt: str) -> Tuple[str, str, Dict[str, Any]]:
        """
        Find unique name, build payload, upload.
        Returns (context_name, status, response_dict).
        """
        context_name = self.find_unique_name(shortname)
        payload = {
            "name":               context_name,
            "opening_text":       opening_text,
            "prompt":             prompt,
            "interactive_style":  "conversational",
        }
        log.info("[AvatarAPI] Uploading context: name=%s  opening_text_len=%d  prompt_len=%d",
                 context_name, len(opening_text), len(prompt))
        resp   = self.create_context(payload)
        status = "created" if (resp.get("code") == 1000 or resp.get("id")) else "error"
        log.info("[AvatarAPI] Upload result: status=%s", status)
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
            f"## PERSONA\n"
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
# DEBUG UI HTML
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
.hdr{background:#161b22;border-bottom:2px solid #1f6feb;padding:14px 24px;
     display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.hdr h1{font-size:1.3rem;color:#58a6ff;flex:1}
.badge{background:#21262d;border:1px solid #30363d;border-radius:12px;
       padding:3px 12px;font-size:.78rem;color:#8b949e;white-space:nowrap}
.badge b{color:#c9d1d9}

/* ── Layout ── */
.wrap{max-width:1400px;margin:0 auto;padding:20px 16px;
      display:grid;grid-template-columns:1fr 1fr;gap:16px}
.full{grid-column:1/-1}

/* ── Cards ── */
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;overflow:hidden}
.ch{background:#21262d;padding:11px 16px;font-weight:700;font-size:.9rem;
    color:#79c0ff;display:flex;align-items:center;justify-content:space-between;gap:8px}
.cb{padding:14px 16px}

/* ── Tables ── */
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{background:#21262d;color:#8b949e;padding:7px 9px;text-align:left;
   border-bottom:1px solid #30363d;white-space:nowrap}
td{padding:6px 9px;border-bottom:1px solid #21262d;word-break:break-all}
tr:last-child td{border-bottom:none}
tr:hover td{background:#1c2128}
.last-row td{background:#132218!important;color:#3fb950}

/* ── Env check ── */
.env-grid{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:.82rem;align-items:center}
.ek{color:#8b949e;font-family:monospace;white-space:nowrap}
.ok{color:#3fb950;font-weight:700}
.miss{color:#ff7b72;font-weight:700}

/* ── Buttons ── */
.btn{padding:8px 18px;border:none;border-radius:7px;cursor:pointer;
     font-weight:700;font-size:.84rem;transition:.15s;white-space:nowrap}
.btn:disabled{opacity:.45;cursor:not-allowed}
.b-blue{background:#1f6feb;color:#fff}.b-blue:hover:not(:disabled){background:#388bfd}
.b-green{background:#238636;color:#fff}.b-green:hover:not(:disabled){background:#2ea043}
.b-orange{background:#9a3e00;color:#fff}.b-orange:hover:not(:disabled){background:#c04f00}
.b-red{background:#6e1a1a;color:#fff}.b-red:hover:not(:disabled){background:#8e2222}
.b-sm{padding:4px 12px;font-size:.76rem}
.btn-row{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
.spin{display:none;width:16px;height:16px;border:2px solid #30363d;
      border-top-color:#58a6ff;border-radius:50%;
      animation:sp .7s linear infinite;flex-shrink:0}
@keyframes sp{to{transform:rotate(360deg)}}

/* ── Log box ── */
#logbox{background:#0d1117;border:1px solid #30363d;border-radius:7px;
        padding:10px;height:420px;overflow-y:auto;
        font-family:'Courier New',monospace;font-size:.76rem;
        white-space:pre-wrap;word-break:break-word;line-height:1.55}
.li{color:#79c0ff}.lw{color:#e3b341}.le{color:#ff7b72}.ld{color:#6e7681}

/* ── Next run countdown ── */
#nextrun{font-size:.82rem;color:#8b949e}

/* ── Status bar ── */
.sbar{background:#161b22;border-top:1px solid #30363d;padding:7px 20px;
      font-size:.76rem;color:#6e7681;display:flex;gap:24px;flex-wrap:wrap}
</style>
</head>
<body>

<div class="hdr">
  <h1>🛠 {{ app_name }}</h1>
  <span class="badge">v{{ version }}</span>
  <span class="badge">Repo: <b>{{ github_repo }}</b></span>
  <span class="badge">Branch: <b>{{ github_branch }}</b></span>
  <span class="badge" id="clock"></span>
  <span class="badge" id="nextrun">Next auto-run: calculating…</span>
</div>

<div class="wrap">

  <!-- ── ENV CHECK ── -->
  <div class="card">
    <div class="ch">🔑 Environment Variables</div>
    <div class="cb">
      <div class="env-grid">
        {% for k,v in env_checks %}
        <span class="ek">{{ k }}</span>
        <span class="{{ 'ok' if v=='SET' else 'miss' }}">{{ '✔ SET' if v=='SET' else '✘ MISSING' }}</span>
        {% endfor %}
      </div>
    </div>
  </div>

  <!-- ── AVATAR API CONTEXTS ── -->
  <div class="card">
    <div class="ch">
      🎭 Avatar API — Existing Contexts
      <button class="btn b-blue b-sm" onclick="loadContexts()">↻ Refresh</button>
    </div>
    <div class="cb">
      <div id="ctx-tbl"><i style="color:#6e7681">Loading…</i></div>
    </div>
  </div>

  <!-- ── SUBMISSIONS CSV ── -->
  <div class="card full">
    <div class="ch">
      📋 submissions.csv
      <span id="csv-count" style="color:#6e7681;font-weight:400;font-size:.8rem"></span>
      <button class="btn b-blue b-sm" onclick="loadCsv()">↻ Refresh</button>
    </div>
    <div class="cb" style="overflow-x:auto">
      <div id="csv-tbl"><i style="color:#6e7681">Loading…</i></div>
    </div>
  </div>

  <!-- ── EMAIL SCAN ── -->
  <div class="card full">
    <div class="ch">
      📧 Gmail Inbox — Qualifying Emails
      <button class="btn b-blue b-sm" onclick="scanEmails()">↻ Scan Inbox</button>
    </div>
    <div class="cb">
      <p style="font-size:.8rem;color:#6e7681;margin-bottom:10px">
        Shows only emails from registered senders whose subject starts with:<br>
        <code style="color:#79c0ff;font-size:.78rem">{{ subject_prefix }}</code>
      </p>
      <div id="email-tbl"><i style="color:#6e7681">Click "Scan Inbox" to check Gmail.</i></div>
    </div>
  </div>

  <!-- ── LOG + ACTIONS ── -->
  <div class="card full">
    <div class="ch">⚡ Actions &amp; Live Debug Log</div>
    <div class="cb">
      <div class="btn-row">
        <button class="btn b-green" id="btn-test"   onclick="triggerRun('test')">▶ Run TEST Mode</button>
        <button class="btn b-orange" id="btn-normal" onclick="triggerRun('once')">▶ Run NORMAL Mode</button>
        <button class="btn b-red b-sm" onclick="clearLog()">🗑 Clear Log</button>
        <div class="spin" id="spin"></div>
        <span id="run-status" style="font-size:.8rem;color:#8b949e"></span>
      </div>
      <div style="font-size:.76rem;color:#6e7681;margin-bottom:8px">
        <b>TEST</b> = uses last CSV row, skips Gmail, uploads dummy context to verify Avatar API.<br>
        <b>NORMAL</b> = full production run: checks Gmail, extracts context, uploads real content.
      </div>
      <div id="logbox"></div>
    </div>
  </div>

</div>

<div class="sbar">
  <span>Gmail: <b>{{ gmail }}</b></span>
  <span>Avatar API: <b>{{ avatar_url }}</b></span>
  <span>Page loaded: <b>{{ load_time }}</b></span>
  <span><a href="/health">Health check</a></span>
</div>

<script>
// ── Clock ──
function tick(){
  const now = new Date();
  document.getElementById('clock').textContent =
    now.toLocaleString('en-SG',{timeZone:'Asia/Singapore',hour12:false}) + ' SGT';
  // Next run countdown (01:00 UTC = 09:00 SGT)
  const utc   = new Date(Date.UTC(now.getUTCFullYear(),now.getUTCMonth(),now.getUTCDate(),1,0,0));
  if(utc < now) utc.setUTCDate(utc.getUTCDate()+1);
  const diff  = Math.floor((utc - now)/1000);
  const h = Math.floor(diff/3600), m = Math.floor((diff%3600)/60), s = diff%60;
  document.getElementById('nextrun').textContent =
    `Next auto-run (09:00 SGT): ${h}h ${m}m ${s}s`;
}
setInterval(tick,1000); tick();

// ── CSV ──
async function loadCsv(){
  document.getElementById('csv-tbl').innerHTML = '<i style="color:#6e7681">Loading…</i>';
  try{
    const d = await (await fetch('/api/csv')).json();
    const rows = d.rows||[];
    document.getElementById('csv-count').textContent = `(${rows.length} rows)`;
    if(!rows.length){ document.getElementById('csv-tbl').innerHTML='<i style="color:#6e7681">No data</i>'; return; }
    const cols = Object.keys(rows[0]);
    let h = '<table><tr>'+cols.map(c=>`<th>${c}</th>`).join('')+'</tr>';
    rows.forEach((r,i)=>{
      const cls = i===rows.length-1 ? ' class="last-row"' : '';
      h += `<tr${cls}>`+cols.map(c=>`<td>${esc(r[c]||'')}</td>`).join('')+'</tr>';
    });
    h += '</table><p style="font-size:.72rem;color:#3fb950;margin-top:6px">★ Last row highlighted — used in TEST mode</p>';
    document.getElementById('csv-tbl').innerHTML = h;
  }catch(e){ document.getElementById('csv-tbl').innerHTML=`<span style="color:#ff7b72">Error: ${e}</span>`; }
}

// ── Avatar Contexts ──
async function loadContexts(){
  document.getElementById('ctx-tbl').innerHTML = '<i style="color:#6e7681">Loading…</i>';
  try{
    const d = await (await fetch('/api/contexts')).json();
    const ctxs = d.contexts||[];
    if(!ctxs.length){ document.getElementById('ctx-tbl').innerHTML='<i style="color:#6e7681">No contexts found</i>'; return; }
    let h = '<table><tr><th>Name</th><th>ID</th><th>Created</th></tr>';
    ctxs.forEach(c=>{
      if(c.error){ h+=`<tr><td colspan=3 style="color:#ff7b72">${esc(c.error)}</td></tr>`; return; }
      h+=`<tr><td><b>${esc(c.name||'')}</b></td><td style="font-size:.72rem;color:#6e7681">${esc(c.id||'')}</td><td style="white-space:nowrap">${esc(c.created_at||'')}</td></tr>`;
    });
    document.getElementById('ctx-tbl').innerHTML = h+'</table>';
  }catch(e){ document.getElementById('ctx-tbl').innerHTML=`<span style="color:#ff7b72">Error: ${e}</span>`; }
}

// ── Email Scan ──
async function scanEmails(){
  document.getElementById('email-tbl').innerHTML = '<i style="color:#6e7681">Scanning Gmail inbox… (may take 1–2 min)</i>';
  try{
    const d = await (await fetch('/api/emails')).json();
    const emails = d.emails||[];
    if(!emails.length){
      document.getElementById('email-tbl').innerHTML='<span style="color:#e3b341">⚠ No qualifying emails found. Check the log for details.</span>';
      return;
    }
    let h='<table><tr><th>From</th><th>Subject</th><th>Date</th><th>Body preview (500 chars)</th></tr>';
    emails.forEach(e=>{
      if(e.error){ h+=`<tr><td colspan=4 style="color:#ff7b72">${esc(e.error)}</td></tr>`; return; }
      h+=`<tr><td>${esc(e.from||'')}</td><td>${esc(e.subject||'')}</td><td style="white-space:nowrap">${esc(e.date||'')}</td><td style="color:#8b949e;font-size:.75rem">${esc(e.body_preview||'')}</td></tr>`;
    });
    document.getElementById('email-tbl').innerHTML = h+'</table>';
  }catch(e){ document.getElementById('email-tbl').innerHTML=`<span style="color:#ff7b72">Error: ${e}</span>`; }
}

// ── Trigger Run (SSE streaming) ──
let _es = null;
function triggerRun(mode){
  if(_es){ _es.close(); }
  const box = document.getElementById('logbox');
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

// ── Auto-load on page open ──
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
        github_repo    = GITHUB_REPO,
        github_branch  = GITHUB_DATA_BRANCH,
        gmail          = GMAIL_ADDRESS,
        avatar_url     = AVATAR_API_BASE_URL,
        subject_prefix = SUBJECT_PREFIX,
        load_time      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
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
