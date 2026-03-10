"""
app.py  —  BBB Avatar Maker  |  Stage 2  |  VERSION 3.2
═══════════════════════════════════════════════════════════════════════════════
VERSION HISTORY
  v1.0  Initial build — Flask + APScheduler + debug UI
  v2.0  Unified single-app (no separate Cron Job)
  v2.1  API fixes: X-API-Key header case + opening_intro field name
  v2.2  Restored normal email mode + enhanced debug + Gemini placeholder
  v3.0  v3 email headers (Dataset/Avatar/Web-scrap) + Type-1/Type-2 routing
  v3.1  Corrected API field names (Set-A payload: name/opening/prompt only)
  v3.2  Renamed HEYGEN_* env vars → AVATAR_KEY_CLASSIC / AVATAR_URL_CLASSIC
        Renamed HeyGenKBClient → ClassicKBClient
        No "HeyGen" brand names in code, config, or UI (counter-checked against live docs):
          TYPE-1 HeyGen Knowledge Base — confirmed from docs + real working code:
            ✅ Payload fields:  name, opening, prompt   (3 fields only)
            ✗ REMOVED:         description              (not a valid field)
            ✗ REMOVED:         knowledge_base           (not a valid field)
            ✅ List response:  data[] is a FLAT list    (not data.list nested)
            ✅ Create response: id is at data.id        (not knowledge_id)
          TYPE-2 LiveAvatar Context — unchanged from v2.2 (confirmed working):
            ✅ Payload fields:  name, opening_intro, opening_text, description, prompt
            ✅ Both opening_intro AND opening_text required (same value)
            ✅ Header: X-API-Key (exact case)

REFERENCE SOURCES USED FOR v3.1/v3.2 VERIFICATION
  SET-A Classic KB (Type-1):
    https://docs.heygen.com/reference/create-a-knowledge-base
    https://docs.heygen.com/reference/list-knowledge-bases
    Real working code: json={"name": KB_NAME, "opening": KB_OPENING, "prompt": KB_PROMPT}
                       _KB_ID = resp.json()["data"]["id"]
  SET-B LiveAvatar Context:
    https://docs.liveavatar.com/reference/create_context_v1_contexts_post
    https://docs.liveavatar.com/docs/liveavatar-vs-heygen-interactive-avatar
    https://docs.liveavatar.com/docs/configuring-full-mode
    Confirmed: Knowledge Base renamed to Context in LiveAvatar

NEW v3 EMAIL FORMAT
  Subject: [Web Scrapped] Context for Avatar Chat - <Company>

  # Dataset   = BESCON123        ← base name for the context/KB entry
  # Avatar    = Type-1           ← routing: Type-1 (HeyGen KB) | Type-2 (LiveAvatar)
  # Web-scrap = Success          ← scrape quality: Success | Difficult

  … rest of context body …

ARCHITECTURE
  • Flask web server  → /debug panel
  • APScheduler       → process_inbox() daily 09:00 SGT (01:00 UTC)
  • Single Render Web Service — no separate Cron Job

ENVIRONMENT VARIABLES (set on Render)
  AVATAR_API_KEY        Live Avatar API key          (Type-2)
  AVATAR_API_BASE_URL   https://api.liveavatar.com
  AVATAR_KEY_CLASSIC    Classic KB API key           (Type-1 / Set-A)
  AVATAR_URL_CLASSIC    https://api.heygen.com       (Type-1 / Set-A)
  GMAIL_ADDRESS         agentic.avai@gmail.com
  GMAIL_APP_PASSWORD    16-char Gmail App Password
  GITHUB_TOKEN_STAGE_2  GitHub PAT
  GITHUB_REPO           Krish1959/bbb-web
  GITHUB_DATA_BRANCH    data
  GEMINI_API_KEY        (optional) future agentic processing

DEPLOY (Render Web Service)
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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import (Flask, Response, jsonify, redirect,
                   render_template_string, request, stream_with_context)


# ═══════════════════════════════════════════════════════════════════════════
# LOGGING  —  console  +  in-memory queue (SSE streaming to browser)
# ═══════════════════════════════════════════════════════════════════════════
_log_queue: queue.Queue = queue.Queue(maxsize=2000)

class _QueueHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _log_queue.put_nowait(self.format(record))
        except queue.Full:
            pass  # never block — silently drop if full

_fmt = logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
_qh = _QueueHandler()
_qh.setFormatter(_fmt)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
logging.getLogger().addHandler(_qh)
log = logging.getLogger("bbb3")


# ═══════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════
APP_VERSION = "3.2"
APP_NAME    = "BBB Avatar Maker — Stage 2"

# ── Type-2: Live Avatar ────────────────────────────────────────────────────
AVATAR_API_KEY      = os.getenv("AVATAR_API_KEY",
                       os.getenv("LIVEAVATAR_API_KEY", "")).strip()
AVATAR_API_BASE_URL = os.getenv("AVATAR_API_BASE_URL",
                       os.getenv("LIVEAVATAR_BASE_URL",
                                 "https://api.liveavatar.com")).strip()

# ── Type-1: Classic Knowledge Base (Set-A) ───────────────────────────────
AVATAR_KEY_CLASSIC      = os.getenv("AVATAR_KEY_CLASSIC",
                           os.getenv("HEYGEN_API_KEY", "")).strip()   # old name fallback
AVATAR_URL_CLASSIC      = os.getenv("AVATAR_URL_CLASSIC",
                           os.getenv("HEYGEN_API_BASE_URL",
                                     "https://api.heygen.com")).strip()

# ── Gmail ──────────────────────────────────────────────────────────────────
GMAIL_ADDRESS      = os.getenv("GMAIL_ADDRESS",      "agentic.avai@gmail.com").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()

# ── GitHub ─────────────────────────────────────────────────────────────────
GITHUB_TOKEN       = os.getenv("GITHUB_TOKEN_STAGE_2", "").strip()
GITHUB_REPO        = os.getenv("GITHUB_REPO",           "Krish1959/bbb-web").strip()
GITHUB_DATA_BRANCH = os.getenv("GITHUB_DATA_BRANCH",    "data").strip()

# ── Gemini (optional — future agentic processing) ─────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL   = os.getenv("GEMINI_MODEL",   "gemini-1.5-flash").strip()

# ── Paths & constants ──────────────────────────────────────────────────────
CSV_PATH       = "submissions.csv"
LOG_PATH       = "stage2_log.csv"
SUBJECT_PREFIX = "[Web Scrapped] Context for Avatar Chat -"


# ═══════════════════════════════════════════════════════════════════════════
# FLASK APP
# ═══════════════════════════════════════════════════════════════════════════
app = Flask(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# GITHUB HELPERS
# ═══════════════════════════════════════════════════════════════════════════
def _gh_headers() -> Dict[str, str]:
    return {
        "Authorization":        f"Bearer {GITHUB_TOKEN}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent":           "bbb-stage2-v3",
    }


def gh_get(path: str) -> Optional[str]:
    """Fetch file text from GitHub data branch. Returns None if not found."""
    log.info("[GitHub] GET  %s  (branch=%s)", path, GITHUB_DATA_BRANCH)
    url = (f"https://api.github.com/repos/{GITHUB_REPO}"
           f"/contents/{path}?ref={GITHUB_DATA_BRANCH}")
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=30)
        log.info("[GitHub] HTTP %d", r.status_code)
        if r.status_code == 404:
            log.warning("[GitHub] Not found: %s", path)
            return None
        r.raise_for_status()
        b64  = r.json().get("content", "")
        text = base64.b64decode(b64).decode("utf-8", errors="replace") if b64 else ""
        log.info("[GitHub] Read %d chars from %s", len(text), path)
        return text
    except Exception as exc:
        log.error("[GitHub] GET error %s: %s", path, exc)
        raise


def gh_sha(path: str) -> Optional[str]:
    """Get current SHA for a file (needed for PUT updates)."""
    url = (f"https://api.github.com/repos/{GITHUB_REPO}"
           f"/contents/{path}?ref={GITHUB_DATA_BRANCH}")
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=30)
        return r.json().get("sha") if r.status_code == 200 else None
    except Exception as exc:
        log.error("[GitHub] SHA error %s: %s", path, exc)
        return None


def gh_put(path: str, text: str, sha: Optional[str], message: str) -> None:
    """Create or update a file on GitHub data branch."""
    log.info("[GitHub] PUT  %s  sha=%s  msg='%s'", path, sha, message)
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    body: Dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(text.encode()).decode(),
        "branch":  GITHUB_DATA_BRANCH,
    }
    if sha:
        body["sha"] = sha
    try:
        r = requests.put(url, headers=_gh_headers(),
                         data=json.dumps(body), timeout=30)
        log.info("[GitHub] PUT HTTP %d", r.status_code)
        r.raise_for_status()
        log.info("[GitHub] ✔ Wrote %s", path)
    except Exception as exc:
        log.error("[GitHub] PUT error %s: %s", path, exc)
        raise


# ═══════════════════════════════════════════════════════════════════════════
# CSV HELPERS
# ═══════════════════════════════════════════════════════════════════════════
def load_csv() -> List[Dict[str, str]]:
    log.info("[CSV] ══ Loading %s  repo=%s  branch=%s ══",
             CSV_PATH, GITHUB_REPO, GITHUB_DATA_BRANCH)
    text = gh_get(CSV_PATH)
    if not text:
        log.warning("[CSV] Empty or missing — returning []")
        return []
    rows = list(csv.DictReader(io.StringIO(text)))
    log.info("[CSV] Loaded %d data rows", len(rows))
    if rows:
        log.info("[CSV] Columns: %s", list(rows[0].keys()))
        log.info("[CSV] ── All rows ───────────────────────────────────────")
        for r in rows:
            log.info("[CSV]   #%-3s  %s  %-25s  %-30s  %s",
                     r.get("Sl_No", "?"), r.get("Date", "?"),
                     r.get("Company", "?"), r.get("Email", "?"),
                     r.get("Web_URL", "?"))
        log.info("[CSV] ── Last row (used in TEST mode) ─────────────────")
        for k, v in rows[-1].items():
            log.info("[CSV]   %-14s : %s", k, v)
        # Cross-check SP entry
        sp_rows = [r for r in rows
                   if "sp.edu.sg" in (r.get("Web_URL") or "").lower()
                   or (r.get("Company") or "").strip().upper() == "SP"]
        if sp_rows:
            log.info("[CSV] ✔ SP entry found:")
            for r in sp_rows:
                log.info("[CSV]   Sl_No=%s  Email=%s  Web=%s",
                         r.get("Sl_No"), r.get("Email"), r.get("Web_URL"))
        else:
            log.warning("[CSV] ⚠ No SP entry found (expected sp.edu.sg row)")
    return rows


def append_run_log(shortname: str, company: str, email_addr: str,
                   context_name: str, avatar_type: str, web_scrap: str,
                   status: str, response: Dict[str, Any]) -> None:
    """Append one result row to stage2_log.csv on GitHub.
    v3 adds AvatarType and WebScrap columns.
    """
    log.info("[RunLog] Writing: company=%s  context=%s  type=%s  scrap=%s  status=%s",
             company, context_name, avatar_type, web_scrap, status)
    try:
        existing = gh_get(LOG_PATH) or ""
        sha      = gh_sha(LOG_PATH)
        now      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        new_row  = (f'{now},"{company}","{shortname}","{email_addr}",'
                    f'"{context_name}","{avatar_type}","{web_scrap}","{status}"\n')
        if not existing.strip():
            header   = ("Timestamp,Company,ShortName,Email,"
                        "ContextName,AvatarType,WebScrap,Status\n")
            new_text = header + new_row
        else:
            new_text = existing.rstrip("\n") + "\n" + new_row
        gh_put(LOG_PATH, new_text, sha,
               f"v3 log: {shortname} type={avatar_type} {status}")
        log.info("[RunLog] ✔ stage2_log.csv updated on GitHub")
    except Exception as exc:
        log.error("[RunLog] Failed to write log: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
# URL → SHORTNAME
# ═══════════════════════════════════════════════════════════════════════════
def shortname_from_url(url: str) -> str:
    """https://www.bcaa.edu.sg/ → bcaa"""
    try:
        u     = url.strip().replace("https://", "").replace("http://", "")
        host  = u.split("/")[0]
        parts = [p for p in host.split(".") if p]
        if not parts:
            return "site"
        result = (parts[1].lower()
                  if parts[0].lower() == "www" and len(parts) >= 2
                  else parts[0].lower())
        log.info("[ShortName] %s  →  %s", url, result)
        return result
    except Exception as exc:
        log.error("[ShortName] Error: %s", exc)
        return "site"


# ═══════════════════════════════════════════════════════════════════════════
# v3 EMAIL HEADER PARSER  ← NEW
# ═══════════════════════════════════════════════════════════════════════════
def parse_v3_headers(body: str) -> Dict[str, str]:
    """
    Parse the three v3 control lines from the email body.
    Lines can appear anywhere; matching is case-insensitive.

    Expected format:
        # Dataset   = BESCON123
        # Avatar    = Type-1        (or Type-2)
        # Web-scrap = Success       (or Difficult)

    Returns dict with keys:
        dataset     — value after '=', stripped  (empty string if not found)
        avatar_type — "Type-1" | "Type-2"        (defaults to "Type-2")
        web_scrap   — raw value after '='         (defaults to "Unknown")
    """
    result: Dict[str, str] = {
        "dataset":     "",
        "avatar_type": "Type-2",   # safe default — existing Type-2 pipeline
        "web_scrap":   "Unknown",
    }

    log.info("[Parse] ══ Parsing v3 email control headers ═══════════════")

    # ── # Dataset = <name> ────────────────────────────────────────────────
    m = re.search(r"#\s*Dataset\s*=\s*(.+)", body, re.IGNORECASE)
    if m:
        result["dataset"] = m.group(1).strip()
        log.info("[Parse] ✔ Dataset    = '%s'", result["dataset"])
    else:
        log.warning("[Parse] ✘ '# Dataset' header NOT found in email body")

    # ── # Avatar = Type-1 | Type-2 ───────────────────────────────────────
    m = re.search(r"#\s*Avatar\s*=\s*(.+)", body, re.IGNORECASE)
    if m:
        raw = m.group(1).strip()
        # Normalise: accept "Type-1", "Type 1", "type1", "1" etc.
        if re.search(r"1", raw):
            result["avatar_type"] = "Type-1"
        elif re.search(r"2", raw):
            result["avatar_type"] = "Type-2"
        else:
            result["avatar_type"] = raw   # keep verbatim if unexpected
        log.info("[Parse] ✔ Avatar     = '%s'  (raw: '%s')",
                 result["avatar_type"], raw)
    else:
        log.warning("[Parse] ✘ '# Avatar' header NOT found — defaulting to Type-2")

    # ── # Web-scrap = Success | Difficult ────────────────────────────────
    m = re.search(r"#\s*Web-?scrap\s*=\s*(.+)", body, re.IGNORECASE)
    if m:
        result["web_scrap"] = m.group(1).strip()
        log.info("[Parse] ✔ Web-scrap  = '%s'", result["web_scrap"])
        if result["web_scrap"].lower() == "difficult":
            log.warning("[Parse] ⚠ Web-scrap = Difficult — content quality may be low")
            log.warning("[Parse]   (Logic for Difficult sites reserved for a future version)")
    else:
        log.warning("[Parse] ✘ '# Web-scrap' header NOT found")

    log.info("[Parse] ── Parsed result: dataset='%s'  type='%s'  scrap='%s'",
             result["dataset"], result["avatar_type"], result["web_scrap"])
    return result


# ═══════════════════════════════════════════════════════════════════════════
# CONTENT EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════
def extract_context_body(body: str, dataset: str,
                         shortname: str, company: str) -> Optional[str]:
    """
    1. Strip quoted reply lines  (lines starting with ">")
    2. Strip Gmail 'On ... wrote:' tail
    3. Find the content block using (in priority order):
         a. '# Dataset = <dataset>'   ← new v3 primary marker
         b. '# <shortname>'           ← legacy marker
         c. '# <company>'             ← company name fallback
         d. any '#' heading           ← last-resort fallback
    Returns the matched block or None.
    """
    log.info("[Extract] ══ Context body extraction ═════════════════════")
    log.info("[Extract] dataset='%s'  shortname='%s'  company='%s'",
             dataset, shortname, company)
    log.info("[Extract] Raw body: %d chars", len(body))
    log.info("[Extract] Raw (first 500): %s",
             body[:500].replace("\n", " ↵ "))

    # ── Step 1: strip quoted lines ────────────────────────────────────────
    lines       = body.splitlines()
    clean_lines = [l for l in lines if not l.strip().startswith(">")]
    removed     = len(lines) - len(clean_lines)
    clean_body  = "\n".join(clean_lines)
    log.info("[Extract] Step 1 — Removed %d quoted lines  →  %d chars remain",
             removed, len(clean_body))

    # ── Step 2: strip 'On ... wrote:' Gmail tail ─────────────────────────
    parts = re.split(r"\nOn .+? wrote:", clean_body, flags=re.DOTALL)
    if len(parts) > 1:
        log.info("[Extract] Step 2 — Stripped 'On...wrote:' tail  (%d chars removed)",
                 len(clean_body) - len(parts[0]))
        clean_body = parts[0]
    else:
        log.info("[Extract] Step 2 — No 'On...wrote:' tail found")

    log.info("[Extract] After cleaning: %d chars", len(clean_body))
    log.info("[Extract] Cleaned (first 600): %s",
             clean_body[:600].replace("\n", " ↵ "))

    # List all '#' lines for diagnostics
    hash_lines = [l.strip() for l in clean_body.splitlines()
                  if l.strip().startswith("#")]
    log.info("[Extract] All '#' lines in cleaned body: %s", hash_lines)

    # ── Step 3a: Primary — '# Dataset = <dataset>' ───────────────────────
    if dataset:
        log.info("[Extract] Step 3a — Searching for '# Dataset = %s'", dataset)
        pat = re.compile(
            r"(#\s*Dataset\s*=\s*" + re.escape(dataset) + r"\b.*)",
            re.IGNORECASE | re.DOTALL)
        m = pat.search(clean_body)
        if m:
            result = m.group(1).strip()
            log.info("[Extract] ✔ Dataset marker found at char %d  →  %d chars",
                     m.start(), len(result))
            log.info("[Extract] First 300: %s", result[:300].replace("\n", " ↵ "))
            return result
        log.warning("[Extract] ✘ '# Dataset = %s' not found", dataset)

    # ── Step 3b: Legacy — '# <shortname>' ───────────────────────────────
    log.info("[Extract] Step 3b — Searching for '# %s'", shortname)
    pat2 = re.compile(r"(#\s*" + re.escape(shortname) + r"\b.*)",
                      re.IGNORECASE | re.DOTALL)
    m2 = pat2.search(clean_body)
    if m2:
        result = m2.group(1).strip()
        log.info("[Extract] ✔ Shortname marker at char %d  →  %d chars",
                 m2.start(), len(result))
        return result
    log.warning("[Extract] ✘ '# %s' not found", shortname)

    # ── Step 3c: Company name fallback ───────────────────────────────────
    if company and company.lower() != shortname.lower():
        log.info("[Extract] Step 3c — Searching for '# %s'", company)
        pat3 = re.compile(r"(#\s*" + re.escape(company) + r"\b.*)",
                          re.IGNORECASE | re.DOTALL)
        m3 = pat3.search(clean_body)
        if m3:
            result = m3.group(1).strip()
            log.info("[Extract] ✔ Company marker at char %d  →  %d chars",
                     m3.start(), len(result))
            return result
        log.warning("[Extract] ✘ '# %s' not found", company)

    # ── Step 3d: Last resort — any '#' heading ───────────────────────────
    log.warning("[Extract] Step 3d — Fallback: any '#' heading")
    m4 = re.search(r"(#\s+\S+.*)", clean_body, re.DOTALL)
    if m4:
        result = m4.group(1).strip()
        log.warning("[Extract] ⚠ FALLBACK used at char %d  →  %d chars",
                    m4.start(), len(result))
        log.warning("[Extract] First 200: %s", result[:200].replace("\n", " ↵ "))
        return result

    # ── Total failure ─────────────────────────────────────────────────────
    log.error("[Extract] ✘ COMPLETE FAILURE — no marker of any kind found")
    chunk = 400
    for i in range(0, min(len(clean_body), 2000), chunk):
        log.error("[Extract] [%d] %s", i,
                  clean_body[i:i + chunk].replace("\n", " ↵ "))
    return None


def extract_opening_intro(context_text: str) -> str:
    """Return text inside '## Opening Intro' section (up to next '##')."""
    m = re.search(r"##\s*Opening Intro\s*\n(.*?)(?=\n##|\Z)",
                  context_text, re.IGNORECASE | re.DOTALL)
    if m:
        intro = m.group(1).strip()
        log.info("[Extract] Opening Intro: %d chars", len(intro))
        return intro
    log.warning("[Extract] No '## Opening Intro' section found")
    return ""


# ═══════════════════════════════════════════════════════════════════════════
# GMAIL HELPERS
# ═══════════════════════════════════════════════════════════════════════════
def gmail_connect() -> imaplib.IMAP4_SSL:
    log.info("[Gmail] Connecting as %s", GMAIL_ADDRESS)
    if not GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_APP_PASSWORD env var not set!")
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    log.info("[Gmail] Login successful")
    return imap


def gmail_fetch_all(imap: imaplib.IMAP4_SSL) -> List[Dict[str, Any]]:
    """Fetch all INBOX messages newest-first."""
    imap.select("INBOX")
    _, data   = imap.search(None, "ALL")
    uid_list  = data[0].split() if data[0] else []
    log.info("[Gmail] Total messages in INBOX: %d", len(uid_list))

    messages: List[Dict[str, Any]] = []
    for uid in reversed(uid_list):
        try:
            _, msg_data = imap.fetch(uid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)

            # Subject
            subj_parts  = email_lib.header.decode_header(msg.get("Subject", ""))
            subject_str = ""
            for part, enc in subj_parts:
                subject_str += (part.decode(enc or "utf-8", errors="replace")
                                if isinstance(part, bytes) else str(part))

            # From address
            from_raw   = msg.get("From", "")
            from_match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", from_raw)
            from_addr  = (from_match.group(0).lower()
                          if from_match else from_raw.lower())

            # Plain-text body
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        pl = part.get_payload(decode=True)
                        if pl:
                            body = pl.decode(
                                part.get_content_charset() or "utf-8",
                                errors="replace")
                        break
            else:
                pl = msg.get_payload(decode=True)
                if pl:
                    body = pl.decode(
                        msg.get_content_charset() or "utf-8",
                        errors="replace")

            messages.append({
                "uid":       uid,
                "subject":   subject_str,
                "from_addr": from_addr,
                "body_text": body,
                "date_str":  msg.get("Date", ""),
            })
        except Exception as exc:
            log.warning("[Gmail] Could not parse uid=%s: %s", uid, exc)

    log.info("[Gmail] Parsed %d messages", len(messages))
    return messages


def gmail_scan_matching(registered: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Scan INBOX, return one email per registered sender whose subject
    starts with SUBJECT_PREFIX. Newest email per sender wins.
    """
    log.info("[Gmail] ══ Inbox scan ════════════════════════════════════")
    log.info("[Gmail] Account        : %s", GMAIL_ADDRESS)
    log.info("[Gmail] Subject prefix : '%s'", SUBJECT_PREFIX)
    log.info("[Gmail] Registered senders (%d):", len(registered))
    for addr, r in registered.items():
        log.info("[Gmail]   %-35s → %-25s  %s",
                 addr, r.get("Company", "?"), r.get("Web_URL", "?"))

    try:
        imap = gmail_connect()
    except Exception as exc:
        log.error("[Gmail] Connection failed: %s", exc)
        return []

    try:
        messages = gmail_fetch_all(imap)
    finally:
        try:
            imap.logout()
            log.info("[Gmail] Logged out cleanly")
        except Exception:
            pass

    matched:             Dict[str, Dict[str, Any]] = {}
    cnt_unregistered   = 0
    cnt_subj_mismatch  = 0
    cnt_duplicate      = 0

    for msg in messages:
        sender = msg["from_addr"]
        subj   = msg["subject"]

        if sender not in registered:
            cnt_unregistered += 1
            continue

        # Registered sender — log every detail
        log.info("[Gmail] ── Registered sender: %s ──────────────────", sender)
        log.info("[Gmail]   Date    : %s", msg.get("date_str", "?"))
        log.info("[Gmail]   Subject : %s", subj)
        log.info("[Gmail]   Body    : %d chars", len(msg.get("body_text", "")))

        if not subj.startswith(SUBJECT_PREFIX):
            mismatch_at = next(
                (i for i, (a, b) in enumerate(zip(SUBJECT_PREFIX, subj)) if a != b),
                min(len(SUBJECT_PREFIX), len(subj)))
            log.warning("[Gmail] ✘ Subject MISMATCH")
            log.warning("[Gmail]   Expected : '%s'", SUBJECT_PREFIX)
            log.warning("[Gmail]   Got      : '%s'", subj)
            log.warning("[Gmail]   First diff at char %d", mismatch_at)
            cnt_subj_mismatch += 1
            continue

        if sender in matched:
            log.info("[Gmail] ℹ Already have newer email from %s — skipping older",
                     sender)
            cnt_duplicate += 1
            continue

        matched[sender] = msg
        log.info("[Gmail] ✔ MATCHED: from=%s", sender)
        preview = msg.get("body_text", "")[:400].replace("\n", " | ")
        log.info("[Gmail]   Body preview: %s", preview)

    log.info("[Gmail] ══ Scan summary ══════════════════════════════════")
    log.info("[Gmail]   Total messages  : %d", len(messages))
    log.info("[Gmail]   Unregistered   : %d", cnt_unregistered)
    log.info("[Gmail]   Subj mismatch  : %d", cnt_subj_mismatch)
    log.info("[Gmail]   Duplicate skip : %d", cnt_duplicate)
    log.info("[Gmail]   ✔ Matched      : %d", len(matched))

    if not matched:
        log.warning("[Gmail] ⚠ No qualifying emails found. Checklist:")
        log.warning("[Gmail]   1. Sender email present in submissions.csv?")
        log.warning("[Gmail]   2. Subject starts EXACTLY with: '%s'", SUBJECT_PREFIX)
        log.warning("[Gmail]   3. Email in Primary inbox tab (not Promotions/Social)?")
        log.warning("[Gmail]   4. Email not archived / deleted?")

    return list(matched.values())


# ═══════════════════════════════════════════════════════════════════════════
# AVATAR API — TYPE-2  (Live Avatar Context)
# ═══════════════════════════════════════════════════════════════════════════
class LiveAvatarClient:
    """
    Type-2: Live Avatar  —  https://api.liveavatar.com
    Dashboard: https://app.liveavatar.com/contexts

    Set-B payload format:
      name, opening_intro, opening_text, description, prompt
    """

    def __init__(self) -> None:
        self.key      = AVATAR_API_KEY
        self.base_url = AVATAR_API_BASE_URL.rstrip("/")
        log.info("[Type2] LiveAvatarClient  base=%s  key_set=%s",
                 self.base_url, bool(self.key))

    def _headers(self) -> Dict[str, str]:
        return {"X-API-Key": self.key, "Content-Type": "application/json"}

    def list_contexts(self) -> List[Dict[str, Any]]:
        log.info("[Type2] Listing existing contexts...")
        r = requests.get(f"{self.base_url}/v1/contexts",
                         headers=self._headers(), timeout=30)
        log.info("[Type2] list_contexts HTTP %d", r.status_code)
        r.raise_for_status()
        results = (r.json().get("data") or {}).get("results") or []
        log.info("[Type2] Found %d: %s",
                 len(results), [c.get("name") for c in results])
        return results

    def find_unique_name(self, base_name: str) -> str:
        """Try BASE1, BASE2, BASE3 … until a free name is found."""
        existing = [c.get("name", "") for c in self.list_contexts()]
        log.info("[Type2] Existing names: %s", existing)
        suffix = 1
        while True:
            candidate = f"{base_name.upper()}{suffix}"
            if candidate not in existing:
                log.info("[Type2] ✔ Unique name: %s", candidate)
                return candidate
            log.info("[Type2] %s taken — trying next", candidate)
            suffix += 1

    def upload(self, base_name: str,
               opening_intro: str,
               prompt: str) -> Tuple[str, str, Dict[str, Any]]:
        """
        Create a Live Avatar context.
        Returns (context_name, status, raw_response).
        """
        context_name = self.find_unique_name(base_name)

        # API requires both opening_intro AND opening_text (same value)
        payload = {
            "name":          context_name,
            "opening_intro": opening_intro,
            "opening_text":  opening_intro,   # mirror — both fields required
            "description":   prompt[:500],
            "prompt":        prompt,
        }

        log.info("[Type2] ── Payload ──────────────────────────────────")
        log.info("[Type2]   name          : %s", context_name)
        log.info("[Type2]   opening_intro : %s", opening_intro[:120])
        log.info("[Type2]   opening_text  : (same as above)")
        log.info("[Type2]   description   : %s…", prompt[:80])
        log.info("[Type2]   prompt length : %d chars", len(prompt))
        log.info("[Type2]   full payload  : %s", json.dumps(payload)[:400])
        log.info("[Type2] ─────────────────────────────────────────────")

        r = requests.post(f"{self.base_url}/v1/contexts",
                          headers=self._headers(), json=payload, timeout=60)
        log.info("[Type2] create_context HTTP %d", r.status_code)
        resp = r.json()
        log.info("[Type2] Response: %s", json.dumps(resp)[:400])
        r.raise_for_status()

        ctx_id = None
        if isinstance(resp.get("data"), dict):
            ctx_id = resp["data"].get("id")
        ctx_id = ctx_id or resp.get("id")

        status = ("created"
                  if (resp.get("code") == 1000 or ctx_id)
                  else "error"
                  if resp.get("code", 0) >= 400
                  else "unknown")
        log.info("[Type2] ✔ Done: name=%s  id=%s  status=%s",
                 context_name, ctx_id, status)
        return context_name, status, resp


# ═══════════════════════════════════════════════════════════════════════════
# AVATAR API — TYPE-1  (HeyGen Interactive Avatar Knowledge Base)
# ═══════════════════════════════════════════════════════════════════════════
class ClassicKBClient:
    """
    Type-1 / Set-A: Classic Interactive Avatar — Knowledge Base
    Dashboard: https://labs.heygen.com/knowledge-base

    Endpoints used:
      GET  /v1/streaming/knowledge_base/list
      POST /v1/streaming/knowledge_base/create

    Set-A payload format (confirmed from API docs + real working code):
      name, opening, prompt
      — NO description field
      — NO knowledge_base field
      — opening  = the avatar's opening line (spoken at session start)
      — prompt   = the full persona + knowledge text
    """

    def __init__(self) -> None:
        self.key      = AVATAR_KEY_CLASSIC
        self.base_url = AVATAR_URL_CLASSIC.rstrip("/")
        log.info("[Type1] ClassicKBClient  base=%s  key_set=%s",
                 self.base_url, bool(self.key))

    def _headers(self) -> Dict[str, str]:
        return {"X-API-Key": self.key, "Content-Type": "application/json"}

    def list_knowledge_bases(self) -> List[Dict[str, Any]]:
        """GET /v1/streaming/knowledge_base/list
        Response shape: { "data": [ {id, name, ...}, ... ] }
        Items are a flat list directly under "data" — NOT nested under "data.list".
        """
        log.info("[Type1] Listing existing knowledge bases...")
        r = requests.get(
            f"{self.base_url}/v1/streaming/knowledge_base/list",
            headers=self._headers(), timeout=30)
        log.info("[Type1] list HTTP %d", r.status_code)
        r.raise_for_status()
        data  = r.json()
        # "data" is a flat list:  { "data": [{...}, {...}] }
        raw   = data.get("data") or []
        items = raw if isinstance(raw, list) else []
        log.info("[Type1] Raw response keys: %s", list(data.keys()))
        log.info("[Type1] Found %d KBs: %s",
                 len(items), [i.get("name") for i in items])
        return items

    def find_unique_name(self, base_name: str) -> str:
        """Try BASE1, BASE2 … until a free Classic KB name is found."""
        existing = [i.get("name", "") for i in self.list_knowledge_bases()]
        log.info("[Type1] Existing KB names: %s", existing)
        suffix = 1
        while True:
            candidate = f"{base_name.upper()}{suffix}"
            if candidate not in existing:
                log.info("[Type1] ✔ Unique KB name: %s", candidate)
                return candidate
            log.info("[Type1] %s taken — trying next", candidate)
            suffix += 1

    def upload(self, base_name: str,
               opening: str,
               prompt: str) -> Tuple[str, str, Dict[str, Any]]:
        """
        POST /v1/streaming/knowledge_base/create
        Returns (kb_name, status, raw_response).

        Confirmed correct Set-A fields (3 fields only):
          name     — KB display name
          opening  — opening line spoken by avatar at session start
          prompt   — full persona + knowledge text

        Response: { "data": { "id": "<kb_id>" } }
          The ID field is "id" — confirmed from real working code.
        """
        kb_name = self.find_unique_name(base_name)

        # ✅ CORRECT: only 3 fields — name, opening, prompt
        # ✗ WRONG (removed): description, knowledge_base
        payload = {
            "name":    kb_name,
            "opening": opening,
            "prompt":  prompt,
        }

        log.info("[Type1] ── Payload ──────────────────────────────────")
        log.info("[Type1]   name          : %s", kb_name)
        log.info("[Type1]   opening       : %s", opening[:120])
        log.info("[Type1]   prompt length : %d chars", len(prompt))
        log.info("[Type1]   full payload  : %s", json.dumps(payload)[:500])
        log.info("[Type1] NOTE: only 3 fields sent — name, opening, prompt")
        log.info("[Type1] ─────────────────────────────────────────────")

        r = requests.post(
            f"{self.base_url}/v1/streaming/knowledge_base/create",
            headers=self._headers(), json=payload, timeout=60)
        log.info("[Type1] create HTTP %d", r.status_code)
        resp = r.json()
        log.info("[Type1] Response: %s", json.dumps(resp)[:400])
        r.raise_for_status()

        # Response: { "data": { "id": "..." } }  — id field only (not knowledge_id)
        kb_id = None
        if isinstance(resp.get("data"), dict):
            kb_id = resp["data"].get("id")
        kb_id = kb_id or resp.get("id")

        status = "created" if kb_id else "error"
        log.info("[Type1] ✔ Done: name=%s  id=%s  status=%s",
                 kb_name, kb_id, status)
        return kb_name, status, resp


# ═══════════════════════════════════════════════════════════════════════════
# CORE JOB — process one matched email row
# ═══════════════════════════════════════════════════════════════════════════
def _process_row(row: Dict[str, str], msg: Dict[str, Any]) -> None:
    """
    Full pipeline for one email:
      A. Parse v3 control headers  (Dataset / Avatar / Web-scrap)
      B. Determine base_name       (dataset value > url shortname)
      C. Extract context body
      D. Extract opening intro
      E. Route to Type-1 or Type-2 API
      F. Write run log
    """
    web_url    = (row.get("Web_URL")  or "").strip()
    company    = (row.get("Company")  or "").strip()
    email_addr = (row.get("Email")    or "").strip()
    shortname  = shortname_from_url(web_url) if web_url else company.lower()

    log.info("[Job] ══ Processing row ═════════════════════════════════")
    log.info("[Job]   Company   : %s", company)
    log.info("[Job]   Email     : %s", email_addr)
    log.info("[Job]   ShortName : %s", shortname)
    log.info("[Job]   Web_URL   : %s", web_url)
    log.info("[Job]   Msg Date  : %s", msg.get("date_str", "?"))
    log.info("[Job]   Subject   : %s", msg.get("subject",  "?"))

    body = msg.get("body_text", "")

    # ── A: Parse v3 headers ───────────────────────────────────────────────
    headers     = parse_v3_headers(body)
    dataset     = headers["dataset"]
    avatar_type = headers["avatar_type"]
    web_scrap   = headers["web_scrap"]

    # Base name for the context/KB:  Dataset value  >  URL shortname
    base_name = dataset if dataset else shortname
    log.info("[Job] base_name='%s'  (dataset='%s'  shortname='%s')",
             base_name, dataset, shortname)
    log.info("[Job] avatar_type='%s'  web_scrap='%s'", avatar_type, web_scrap)

    # ── B: Extract context body ───────────────────────────────────────────
    context_text = extract_context_body(body, dataset, shortname, company)
    if not context_text:
        log.error("[Job] ✘ Context extraction failed for '%s' — skipping", company)
        append_run_log(shortname, company, email_addr, "NONE",
                       avatar_type, web_scrap, "extract_failed", {})
        return
    log.info("[Job] Context extracted: %d chars", len(context_text))

    # ── C: Opening intro ──────────────────────────────────────────────────
    opening = extract_opening_intro(context_text)
    opening = opening or f"Welcome to the Q&A session for {company}"
    log.info("[Job] Opening intro: %s", opening[:120])

    # ── D: Route to correct API ───────────────────────────────────────────
    context_name = "NONE"
    status:  str = "not_attempted"
    resp:    Dict[str, Any] = {}

    if avatar_type == "Type-1":
        # ──────────────────────────────────────────────────────────────────
        # TYPE-1: Classic Knowledge Base (Set-A)
        # Dashboard: https://labs.heygen.com/knowledge-base
        # ──────────────────────────────────────────────────────────────────
        log.info("[Job] ── Routing → TYPE-1: HeyGen Knowledge Base ────")
        log.info("[Job]   Dashboard: https://labs.heygen.com/knowledge-base")
        if not AVATAR_KEY_CLASSIC:
            log.error("[Job] ✘ AVATAR_KEY_CLASSIC not set — cannot upload Type-1")
            status = "error_no_key"
        else:
            try:
                client1 = ClassicKBClient()
                context_name, status, resp = client1.upload(
                    base_name = base_name,
                    opening   = opening,
                    prompt    = context_text,
                    # ✅ Only 3 fields: name, opening, prompt
                    # ✗ Removed: knowledge_base_text, description
                )
                log.info("[Job] ✔ Type-1 upload complete: name=%s  status=%s",
                         context_name, status)
            except Exception as exc:
                log.error("[Job] ✘ Type-1 upload failed: %s", exc)
                context_name = f"{base_name.upper()}?"
                status       = "exception"
                resp         = {"error": str(exc)}

    elif avatar_type == "Type-2":
        # ──────────────────────────────────────────────────────────────────
        # TYPE-2: Live Avatar Context
        # Dashboard: https://app.liveavatar.com/contexts
        # ──────────────────────────────────────────────────────────────────
        log.info("[Job] ── Routing → TYPE-2: Live Avatar Context ───────")
        log.info("[Job]   Dashboard: https://app.liveavatar.com/contexts")
        if not AVATAR_API_KEY:
            log.error("[Job] ✘ AVATAR_API_KEY not set — cannot upload Type-2")
            status = "error_no_key"
        else:
            try:
                client2 = LiveAvatarClient()
                context_name, status, resp = client2.upload(
                    base_name    = base_name,
                    opening_intro= opening,
                    prompt       = context_text,
                )
                log.info("[Job] ✔ Type-2 upload complete: name=%s  status=%s",
                         context_name, status)
            except Exception as exc:
                log.error("[Job] ✘ Type-2 upload failed: %s", exc)
                context_name = f"{base_name.upper()}?"
                status       = "exception"
                resp         = {"error": str(exc)}

    else:
        log.error("[Job] ✘ Unknown avatar_type '%s' — no upload attempted",
                  avatar_type)
        status = "error_unknown_type"

    # ── E: Write run log ──────────────────────────────────────────────────
    append_run_log(shortname, company, email_addr,
                   context_name, avatar_type, web_scrap, status, resp)
    log.info("[Job] ══ Row complete: name=%s  type=%s  status=%s ══",
             context_name, avatar_type, status)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN JOB  (scheduler + /api/run endpoint both call this)
# ═══════════════════════════════════════════════════════════════════════════
def process_inbox(test_mode: bool = False) -> None:
    """
    test_mode=True  → Skip Gmail. Use last CSV row. Build dummy v3 body.
    test_mode=False → Production: Gmail → parse → route → upload.
    """
    log.info("━" * 60)
    log.info("[Job] ══ START  v%s  mode=%s  %s ══",
             APP_VERSION,
             "TEST" if test_mode else "NORMAL",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
    log.info("[Job] GitHub  : %s  branch=%s", GITHUB_REPO, GITHUB_DATA_BRANCH)
    log.info("[Job] Type-2  : base=%s  key_set=%s",
             AVATAR_API_BASE_URL, bool(AVATAR_API_KEY))
    log.info("[Job] Type-1  : base=%s  key_set=%s",
             AVATAR_URL_CLASSIC, bool(AVATAR_KEY_CLASSIC))
    log.info("[Job] Gmail   : %s  pwd_set=%s", GMAIL_ADDRESS, bool(GMAIL_APP_PASSWORD))
    log.info("[Job] Gemini  : key_set=%s  model=%s", bool(GEMINI_API_KEY), GEMINI_MODEL)

    # ── Load CSV ──────────────────────────────────────────────────────────
    rows = load_csv()
    if not rows:
        log.error("[Job] No CSV rows — aborting.")
        return
    log.info("[Job] CSV: %d rows loaded", len(rows))

    # ── TEST MODE ─────────────────────────────────────────────────────────
    if test_mode:
        last      = rows[-1]
        company   = (last.get("Company") or "Test Company").strip()
        web_url   = (last.get("Web_URL")  or "").strip()
        shortname = shortname_from_url(web_url) if web_url else company.lower()

        log.info("[Job] TEST MODE — using last CSV row: %s  (%s)", company, web_url)

        # Build a synthetic email body that matches v3 format
        dummy_body = (
            f"# Dataset   = {shortname.upper()}TEST\n"
            f"# Avatar    = Type-2\n"
            f"# Web-scrap = Success\n\n"
            f"# Dataset = {shortname.upper()}TEST\n\n"
            f"## Opening Intro\n"
            f"Welcome! This is a TEST context auto-generated for {company}.\n\n"
            f"## About\n"
            f"Company : {company}\n"
            f"Website : {web_url}\n\n"
            f"## PERSONA / ROLE\n"
            f"You are a helpful AI assistant representing {company}. "
            f"Answer all questions professionally.\n"
        )
        dummy_msg = {
            "from_addr": last.get("Email", ""),
            "subject":   f"{SUBJECT_PREFIX} {company}",
            "date_str":  datetime.now(timezone.utc).isoformat(),
            "body_text": dummy_body,
        }
        log.info("[Job] Dummy body (%d chars):\n%s",
                 len(dummy_body), dummy_body[:400])
        _process_row(last, dummy_msg)
        log.info("[Job] ══ TEST MODE COMPLETE ══")
        return

    # ── NORMAL MODE ───────────────────────────────────────────────────────

    # Build registered-email lookup (last CSV row per email wins)
    registered: Dict[str, Dict[str, str]] = {}
    for row in rows:
        key = (row.get("Email") or "").strip().lower()
        if key:
            registered[key] = row

    log.info("[Job] ── Registered senders (%d) ────────────────────────",
             len(registered))
    for addr, r in registered.items():
        log.info("[Job]   %-35s  Sl_No=%-3s  %-25s  %s",
                 addr, r.get("Sl_No", "?"),
                 r.get("Company", "?"), r.get("Web_URL", "?"))

    # SP cross-check
    sp = next((r for r in rows
               if (r.get("Company") or "").strip().upper() == "SP"), None)
    if sp:
        sp_email = (sp.get("Email") or "").strip().lower()
        flag = "✔ IS registered" if sp_email in registered else "✘ NOT in registered!"
        log.info("[Job] SP cross-check: Sl_No=%s  Email=%s  → %s",
                 sp.get("Sl_No"), sp_email, flag)

    # Scan Gmail
    matching = gmail_scan_matching(registered)
    if not matching:
        log.warning("[Job] ⚠ No qualifying emails found.")
        log.warning("[Job]   Subject must start EXACTLY with: '%s'", SUBJECT_PREFIX)
        log.info("[Job] ══ NORMAL MODE COMPLETE (no action taken) ══")
        return

    log.info("[Job] %d email(s) to process", len(matching))
    for msg in matching:
        row = registered[msg["from_addr"]]
        _process_row(row, msg)

    log.info("[Job] ══ NORMAL MODE COMPLETE ══")


# ═══════════════════════════════════════════════════════════════════════════
# SCHEDULER
# ═══════════════════════════════════════════════════════════════════════════
_scheduler = BackgroundScheduler(timezone="Asia/Singapore")
_scheduler.add_job(
    func    = lambda: process_inbox(test_mode=False),
    trigger = "cron",
    hour=9, minute=0,
    id   = "daily_job",
    name = "Daily inbox scan — 09:00 SGT",
)


# ═══════════════════════════════════════════════════════════════════════════
# DEBUG UI  HTML
# ═══════════════════════════════════════════════════════════════════════════
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{{ app_name }} v{{ version }} — Debug</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh}
a{color:#58a6ff;text-decoration:none}a:hover{text-decoration:underline}

/* ── Header ── */
.hdr{background:#161b22;border-bottom:2px solid #1f6feb;padding:13px 22px;
     display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.hdr h1{font-size:1.22rem;color:#58a6ff;flex:1;white-space:nowrap}
.badge{background:#21262d;border:1px solid #30363d;border-radius:12px;
       padding:3px 11px;font-size:.74rem;color:#8b949e;white-space:nowrap}
.badge b{color:#c9d1d9}

/* ── Layout ── */
.wrap{max-width:1440px;margin:0 auto;padding:16px;
      display:grid;grid-template-columns:1fr 1fr;gap:14px}
.full{grid-column:1/-1}

/* ── Cards ── */
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;overflow:hidden}
.ch{background:#21262d;padding:10px 15px;font-weight:700;font-size:.86rem;color:#79c0ff;
    display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.ch-r{margin-left:auto}
.cb{padding:13px 15px}

/* ── Tables ── */
table{width:100%;border-collapse:collapse;font-size:.78rem}
th{background:#21262d;color:#8b949e;padding:6px 8px;text-align:left;
   border-bottom:1px solid #30363d;white-space:nowrap}
td{padding:5px 8px;border-bottom:1px solid #21262d;word-break:break-all}
tr:hover td{background:#1c2128}
.last-row td{background:#102010!important;color:#3fb950}

/* ── Env grid ── */
.env-grid{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;
          font-size:.79rem;align-items:center}
.ek{font-family:monospace;color:#8b949e;white-space:nowrap}
.ok{color:#3fb950;font-weight:700}
.miss{color:#ff7b72;font-weight:700}
.opt{color:#6e7681}

/* ── Buttons ── */
.btn{padding:7px 16px;border:none;border-radius:7px;cursor:pointer;font-weight:700;
     font-size:.82rem;transition:background .15s;white-space:nowrap}
.btn:disabled{opacity:.4;cursor:not-allowed}
.bg {background:#238636;color:#fff}.bg:hover:not(:disabled){background:#2ea043}
.bo {background:#9a3e00;color:#fff}.bo:hover:not(:disabled){background:#c04f00}
.bb {background:#1f6feb;color:#fff}.bb:hover:not(:disabled){background:#388bfd}
.br {background:#5a1818;color:#fff}.br:hover:not(:disabled){background:#7a2020}
.sm {padding:4px 10px;font-size:.74rem}
.btn-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}

/* ── Spinner ── */
.spin{display:none;width:15px;height:15px;border:2px solid #30363d;
      border-top-color:#58a6ff;border-radius:50%;animation:sp .65s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}

/* ── Log box ── */
#logbox{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:10px;
        height:460px;overflow-y:auto;font-family:'Courier New',monospace;
        font-size:.73rem;white-space:pre-wrap;word-break:break-word;line-height:1.5}
.li{color:#79c0ff}.lw{color:#e3b341}.le{color:#ff7b72}.ld{color:#5a6470}

/* ── Info box ── */
.info{background:#0d1926;border:1px solid #1f4068;border-radius:7px;
      padding:11px 14px;font-size:.78rem;color:#8b949e;line-height:1.8;margin-bottom:12px}
.info code{color:#c9d1d9;background:#21262d;padding:1px 5px;border-radius:4px;font-size:.76rem}
.t1{color:#e3b341;font-weight:700} .t2{color:#3fb950;font-weight:700}

/* ── Status bar ── */
.sbar{background:#161b22;border-top:1px solid #30363d;padding:6px 18px;
      font-size:.72rem;color:#6e7681;display:flex;gap:18px;flex-wrap:wrap}
</style>
</head>
<body>

<div class="hdr">
  <h1>🛠 {{ app_name }}</h1>
  <span class="badge">v<b>{{ version }}</b></span>
  <span class="badge">Repo: <b>{{ github_repo }}</b></span>
  <span class="badge">Branch: <b>{{ github_branch }}</b></span>
  <span class="badge" id="clock">…</span>
  <span class="badge" id="nextrun">Next auto-run: …</span>
</div>

<div class="wrap">

  <!-- ENV VARS -->
  <div class="card">
    <div class="ch">🔑 Environment Variables</div>
    <div class="cb">
      <div class="env-grid">
        {% for k,v in env_checks %}
        <span class="ek">{{ k }}</span>
        <span class="{{ 'ok' if v=='SET' else 'opt' if v.startswith('not') else 'miss' }}">
          {{ '✔ SET' if v=='SET' else v }}
        </span>
        {% endfor %}
      </div>
    </div>
  </div>

  <!-- LIVE AVATAR CONTEXTS (Type-2) -->
  <div class="card">
    <div class="ch">
      🎭 Live Avatar Contexts
      <span class="t2">(Type-2)</span>
      <button class="btn bb sm ch-r" onclick="loadContexts()">↻</button>
    </div>
    <div class="cb">
      <p style="font-size:.72rem;color:#6e7681;margin-bottom:8px">
        <a href="https://app.liveavatar.com/contexts" target="_blank">
          app.liveavatar.com/contexts ↗</a>
      </p>
      <div id="ctx-tbl"><i style="color:#6e7681">Loading…</i></div>
    </div>
  </div>

  <!-- CSV TABLE -->
  <div class="card full">
    <div class="ch">
      📋 submissions.csv
      <span id="csv-count" style="color:#6e7681;font-weight:400;font-size:.76rem"></span>
      <button class="btn bb sm ch-r" onclick="loadCsv()">↻ Refresh</button>
    </div>
    <div class="cb" style="overflow-x:auto">
      <div id="csv-tbl"><i style="color:#6e7681">Loading…</i></div>
    </div>
  </div>

  <!-- EMAIL SCAN -->
  <div class="card full">
    <div class="ch">
      📧 Gmail Inbox — Qualifying Emails
      <button class="btn bb sm ch-r" onclick="scanEmails()">↻ Scan Inbox</button>
    </div>
    <div class="cb">
      <p style="font-size:.76rem;color:#6e7681;margin-bottom:9px">
        Subject must start with: &nbsp;
        <code style="color:#79c0ff">{{ subject_prefix }}</code>
      </p>
      <div id="email-tbl">
        <i style="color:#6e7681">Click "Scan Inbox" to check Gmail.</i>
      </div>
    </div>
  </div>

  <!-- ACTIONS + LOG -->
  <div class="card full">
    <div class="ch">⚡ Actions &amp; Live Debug Log</div>
    <div class="cb">

      <div class="info">
        <b>v3 Email Header Format</b> (add these lines at the top of the email body):<br>
        <code># Dataset   = BESCON123</code> &nbsp;← context/KB base name<br>
        <code># Avatar    = Type-1</code> &nbsp;&nbsp;&nbsp; ← routing:<br>
        &nbsp;&nbsp;
        <span class="t1">■ Type-1</span> → Classic Knowledge Base (Set-A) &nbsp;
          <a href="https://labs.heygen.com/knowledge-base" target="_blank">labs.heygen.com/knowledge-base ↗</a><br>
        &nbsp;&nbsp;
        <span class="t2">■ Type-2</span> → Live Avatar Context &nbsp;
          <a href="https://app.liveavatar.com/contexts" target="_blank">app.liveavatar.com/contexts ↗</a><br>
        <code># Web-scrap = Success</code> &nbsp;← scrape quality: <b>Success</b> or <b>Difficult</b>
      </div>

      <div class="btn-row">
        <button class="btn bg"  id="btn-test"   onclick="triggerRun('test')">▶ Run TEST Mode</button>
        <button class="btn bo"  id="btn-normal" onclick="triggerRun('once')">▶ Run NORMAL Mode</button>
        <button class="btn br sm" onclick="clearLog()">🗑 Clear Log</button>
        <div class="spin" id="spin"></div>
        <span id="run-status" style="font-size:.76rem;color:#8b949e;margin-left:4px">Idle</span>
      </div>

      <p style="font-size:.72rem;color:#6e7681;margin-bottom:9px;line-height:1.6">
        <b>TEST</b> uses last CSV row, builds dummy v3 email body, skips Gmail — verifies full pipeline.<br>
        <b>NORMAL</b> scans Gmail → parses v3 headers → routes Type-1 or Type-2 → uploads → logs result.
      </p>

      <div id="logbox"></div>
    </div>
  </div>

</div><!-- /wrap -->

<div class="sbar">
  <span>Gmail: <b>{{ gmail }}</b></span>
  <span>Type-2: <b>{{ avatar_url }}</b></span>
  <span>Set-A: <b>{{ classic_url }}</b></span>
  <span>Loaded: <b>{{ load_time }}</b></span>
  <span><a href="/health" target="_blank">Health check ↗</a></span>
</div>

<script>
/* ── Clock + countdown ── */
function tick(){
  const now=new Date();
  document.getElementById('clock').textContent=
    now.toLocaleString('en-SG',{timeZone:'Asia/Singapore',hour12:false})+' SGT';
  const utc=new Date(Date.UTC(now.getUTCFullYear(),now.getUTCMonth(),
                               now.getUTCDate(),1,0,0));
  if(utc<now) utc.setUTCDate(utc.getUTCDate()+1);
  const d=Math.floor((utc-now)/1000);
  document.getElementById('nextrun').textContent=
    `Next auto (09:00 SGT): ${Math.floor(d/3600)}h ${Math.floor(d%3600/60)}m ${d%60}s`;
}
setInterval(tick,1000); tick();

/* ── CSV ── */
async function loadCsv(){
  document.getElementById('csv-tbl').innerHTML='<i style="color:#6e7681">Loading…</i>';
  try{
    const d=await(await fetch('/api/csv')).json();
    const rows=d.rows||[];
    document.getElementById('csv-count').textContent=`(${rows.length} rows)`;
    if(!rows.length){document.getElementById('csv-tbl').innerHTML='<i>No data</i>';return;}
    const cols=Object.keys(rows[0]);
    let h='<table><tr>'+cols.map(c=>'<th>'+c+'</th>').join('')+'</tr>';
    rows.forEach((r,i)=>{
      const cls=i===rows.length-1?' class="last-row"':'';
      h+=`<tr${cls}>`+cols.map(c=>'<td>'+esc(r[c]||'')+'</td>').join('')+'</tr>';
    });
    h+='</table><p style="font-size:.7rem;color:#3fb950;margin-top:5px">★ Last row highlighted — used in TEST mode</p>';
    document.getElementById('csv-tbl').innerHTML=h;
  }catch(e){document.getElementById('csv-tbl').innerHTML='<span style="color:#ff7b72">Error: '+e+'</span>';}
}

/* ── Live Avatar contexts ── */
async function loadContexts(){
  document.getElementById('ctx-tbl').innerHTML='<i style="color:#6e7681">Loading…</i>';
  try{
    const d=await(await fetch('/api/contexts')).json();
    const ctxs=d.contexts||[];
    if(!ctxs.length){document.getElementById('ctx-tbl').innerHTML='<i style="color:#6e7681">No contexts found</i>';return;}
    let h='<table><tr><th>Name</th><th>ID</th><th>Created</th></tr>';
    ctxs.forEach(c=>{
      if(c.error){h+=`<tr><td colspan=3 style="color:#ff7b72">${esc(c.error)}</td></tr>`;return;}
      h+=`<tr><td><b>${esc(c.name||'')}</b></td>`+
         `<td style="font-size:.7rem;color:#6e7681">${esc(c.id||'')}</td>`+
         `<td style="white-space:nowrap;color:#8b949e">${esc(c.created_at||'')}</td></tr>`;
    });
    document.getElementById('ctx-tbl').innerHTML=h+'</table>';
  }catch(e){document.getElementById('ctx-tbl').innerHTML='<span style="color:#ff7b72">Error: '+e+'</span>';}
}

/* ── Email scan ── */
async function scanEmails(){
  document.getElementById('email-tbl').innerHTML=
    '<i style="color:#6e7681">Scanning Gmail (may take 30–90s)…</i>';
  try{
    const d=await(await fetch('/api/emails')).json();
    const emails=d.emails||[];
    if(!emails.length){
      document.getElementById('email-tbl').innerHTML=
        '<span style="color:#e3b341">⚠ No qualifying emails found. Check live log for details.</span>';
      return;
    }
    let h='<table><tr><th>From</th><th>Subject</th><th>Date</th><th>Body preview</th></tr>';
    emails.forEach(e=>{
      if(e.error){h+=`<tr><td colspan=4 style="color:#ff7b72">${esc(e.error)}</td></tr>`;return;}
      h+=`<tr><td>${esc(e.from)}</td><td>${esc(e.subject)}</td>`+
         `<td style="white-space:nowrap">${esc(e.date)}</td>`+
         `<td style="color:#8b949e;font-size:.72rem">${esc(e.body_preview)}</td></tr>`;
    });
    document.getElementById('email-tbl').innerHTML=h+'</table>';
  }catch(e){document.getElementById('email-tbl').innerHTML='<span style="color:#ff7b72">Error: '+e+'</span>';}
}

/* ── SSE run ── */
let _es=null;
function triggerRun(mode){
  if(_es)_es.close();
  setRunning(true,mode);
  appendLog('▶ Triggering '+mode.toUpperCase()+' at '+new Date().toISOString(),'li');
  _es=new EventSource('/api/run?mode='+mode);
  _es.onmessage=e=>{
    const line=e.data;
    if(line==='__DONE__'){_es.close();setRunning(false);loadContexts();loadCsv();return;}
    let cls='ld';
    if(line.includes('ERROR')||line.includes('✘'))cls='le';
    else if(line.includes('WARNING')||line.includes('⚠'))cls='lw';
    else if(line.includes('INFO')||line.includes('✔')||line.includes('══'))cls='li';
    appendLog(line,cls);
  };
  _es.onerror=()=>{_es.close();setRunning(false);appendLog('── stream ended ──','ld');loadContexts();};
}
function appendLog(line,cls){
  const b=document.getElementById('logbox');
  const s=document.createElement('span');
  s.className=cls; s.textContent=line+'\n'; b.appendChild(s);
  b.scrollTop=b.scrollHeight;
}
function setRunning(on,mode){
  document.getElementById('spin').style.display=on?'inline-block':'none';
  document.getElementById('btn-test').disabled=on;
  document.getElementById('btn-normal').disabled=on;
  document.getElementById('run-status').textContent=on?'Running '+( mode||'')+'…':'Idle';
}
function clearLog(){document.getElementById('logbox').innerHTML='';}
function esc(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

loadCsv(); loadContexts();
</script>
</body>
</html>
"""

# ═══════════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/")
def home() -> Response:
    return redirect("/debug")


@app.get("/debug")
def debug_page():
    env_checks = [
        ("AVATAR_API_KEY",       "SET" if AVATAR_API_KEY      else "MISSING"),
        ("AVATAR_API_BASE_URL",  "SET" if AVATAR_API_BASE_URL else "MISSING"),
        ("AVATAR_KEY_CLASSIC",   "SET" if AVATAR_KEY_CLASSIC  else "MISSING"),
        ("AVATAR_URL_CLASSIC",   "SET" if AVATAR_URL_CLASSIC  else "MISSING"),
        ("GMAIL_ADDRESS",        "SET" if GMAIL_ADDRESS       else "MISSING"),
        ("GMAIL_APP_PASSWORD",   "SET" if GMAIL_APP_PASSWORD  else "MISSING"),
        ("GITHUB_TOKEN_STAGE_2", "SET" if GITHUB_TOKEN        else "MISSING"),
        ("GITHUB_REPO",          "SET" if GITHUB_REPO         else "MISSING"),
        ("GITHUB_DATA_BRANCH",   "SET" if GITHUB_DATA_BRANCH  else "MISSING"),
        ("GEMINI_API_KEY",       "SET" if GEMINI_API_KEY
                                 else "not set (optional — future use)"),
    ]
    return render_template_string(
        _HTML,
        app_name       = APP_NAME,
        version        = APP_VERSION,
        env_checks     = env_checks,
        github_repo    = GITHUB_REPO,
        github_branch  = GITHUB_DATA_BRANCH,
        gmail          = GMAIL_ADDRESS,
        avatar_url     = AVATAR_API_BASE_URL,
        classic_url    = AVATAR_URL_CLASSIC,
        subject_prefix = SUBJECT_PREFIX,
        load_time      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


@app.get("/health")
def health():
    next_run = None
    try:
        job = _scheduler.get_job("daily_job")
        if job and job.next_run_time:
            next_run = job.next_run_time.isoformat()
    except Exception:
        pass
    return jsonify({
        "ok":           True,
        "version":      APP_VERSION,
        "time_utc":     datetime.now(timezone.utc).isoformat(),
        "scheduler":    _scheduler.running,
        "next_run_utc": next_run,
    })


@app.get("/api/csv")
def api_csv():
    try:
        rows = load_csv()
        return jsonify({"rows": rows, "count": len(rows)})
    except Exception as exc:
        log.error("[API/csv] %s", exc)
        return jsonify({"error": str(exc), "rows": []}), 500


@app.get("/api/contexts")
def api_contexts():
    """Return Live Avatar contexts (Type-2)."""
    try:
        client   = LiveAvatarClient()
        contexts = client.list_contexts()
        return jsonify({"contexts": contexts, "count": len(contexts)})
    except Exception as exc:
        log.error("[API/contexts] %s", exc)
        return jsonify({"error": str(exc), "contexts": []}), 500


@app.get("/api/emails")
def api_emails():
    try:
        rows       = load_csv()
        registered = {(r.get("Email") or "").strip().lower(): r
                      for r in rows if r.get("Email")}
        matched    = gmail_scan_matching(registered)
        result     = [{
            "from":         m["from_addr"],
            "subject":      m["subject"],
            "date":         m["date_str"],
            "body_preview": m["body_text"][:500].replace("\n", " "),
        } for m in matched]
        return jsonify({"emails": result, "count": len(result)})
    except Exception as exc:
        log.error("[API/emails] %s", exc)
        return jsonify({"error": str(exc), "emails": []}), 500


@app.get("/api/run")
def api_run():
    """SSE endpoint — runs process_inbox in background, streams log to browser."""
    mode = request.args.get("mode", "test")
    log.info("[API/run] Triggered: mode=%s", mode)

    def generate():
        # Drain stale log lines from a previous run
        drained = 0
        while not _log_queue.empty():
            try:
                _log_queue.get_nowait()
                drained += 1
            except queue.Empty:
                break
        if drained:
            yield f"data: [debug] Drained {drained} stale log lines\n\n"

        yield f"data: [debug] Starting {mode.upper()} run...\n\n"
        done = threading.Event()

        def _job():
            try:
                process_inbox(test_mode=(mode == "test"))
            except Exception as exc:
                log.error("[API/run] Unhandled exception: %s", exc)
            finally:
                done.set()

        threading.Thread(target=_job, daemon=True).start()

        # Stream until job finishes AND queue is empty
        while not done.is_set() or not _log_queue.empty():
            try:
                line = _log_queue.get(timeout=0.4)
                yield f"data: {line}\n\n"
            except queue.Empty:
                yield "data: .\n\n"    # SSE keepalive

        # Flush any last lines
        while not _log_queue.empty():
            try:
                yield f"data: {_log_queue.get_nowait()}\n\n"
            except queue.Empty:
                break

        yield "data: __DONE__\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ═══════════════════════════════════════════════════════════════════════════
# STARTUP  (runs when gunicorn imports this module)
# ═══════════════════════════════════════════════════════════════════════════
def _start_scheduler() -> None:
    if not _scheduler.running:
        _scheduler.start()
        log.info("[Scheduler] Started — daily job at 09:00 SGT (01:00 UTC)")
        try:
            job = _scheduler.get_job("daily_job")
            if job:
                log.info("[Scheduler] Next scheduled run: %s", job.next_run_time)
        except Exception:
            pass

_start_scheduler()

if __name__ == "__main__":
    log.info("[App] Dev mode — starting Flask directly")
    app.run(
        host    = "0.0.0.0",
        port    = int(os.getenv("PORT", "5000")),
        debug   = False,
        threaded= True,
    )
