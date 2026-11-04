"""
greytHR -> Supabase multi-tenant sync agent  (v2, production)

Runs on ONE always-on machine. Every cycle it:
  1. closes any earlier day left ACTIVE/ON_BREAK (at that day's last OUT swipe)
  2. scrapes every opted-in user's swipes from greytHR (in parallel, capped)
  3. upserts one tracker_logs row per user for today
  4. writes a heartbeat row to sync_status for the admin page

HOW SWIPES ARE READ
  Primary   - the JSON the greytHR attendance page itself downloads is captured
              off the network and parsed. This sees every swipe, IN and OUT,
              regardless of how the page draws them (icons, collapsed cards,
              virtualised lists). This is the fix for "Last out" showing --:--.
  Fallback  - the page's visible text is parsed with a regex (the v1 method).

DIAGNOSING A USER
    python greyt_sync.py --probe FTRN-PUN-XXXX
  runs one scrape for that user (no DB writes) and saves every captured JSON
  response plus the page text to sync_debug/probe_<user>_<time>/ so you can
  see exactly what greytHR returned.

CONFIG  (environment variables or a .env file next to this script; see .env.example)
  SUPABASE_URL, SUPABASE_SERVICE_KEY   required. Service key = env only, never in HTML/git.
  SYNC_INTERVAL_SECONDS     default 60
  MAX_CONCURRENT_USERS      default 4   (0 = everyone at once)
  SYNC_WINDOW               default 06:00-23:59  (local time; outside it only close-out runs)
  IDLE_INTERVAL_SECONDS     default 600 (cycle length outside the window)
  TZ_NAME                   default Asia/Kolkata
  LOGIN_FAILURE_LIMIT       default 3   (consecutive failed logins before backing off)
  LOGIN_BACKOFF_MINUTES     default 30  (protects accounts from greytHR lockout)
  DEBUG_RETENTION_DAYS      default 3
  HEADLESS                  default 1   (set 0 to watch the browser)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import platform
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# .env loading (optional dependency)
# --------------------------------------------------------------------------
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://ouqeqgmgwcdesezpitlo.supabase.co").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SYNC_INTERVAL_SECONDS = _env_int("SYNC_INTERVAL_SECONDS", 60)
IDLE_INTERVAL_SECONDS = _env_int("IDLE_INTERVAL_SECONDS", 600)
MAX_CONCURRENT_USERS = _env_int("MAX_CONCURRENT_USERS", 4)
SYNC_WINDOW = os.environ.get("SYNC_WINDOW", "06:00-23:59")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Kolkata"))
LOGIN_FAILURE_LIMIT = _env_int("LOGIN_FAILURE_LIMIT", 3)
LOGIN_BACKOFF_MINUTES = _env_int("LOGIN_BACKOFF_MINUTES", 30)
DEBUG_RETENTION_DAYS = _env_int("DEBUG_RETENTION_DAYS", 3)
HEADLESS = os.environ.get("HEADLESS", "1") != "0"

DEBUG_DIR = BASE_DIR / "sync_debug"
SESSION_DIR = BASE_DIR / "sync_sessions"
LOG_DIR = BASE_DIR / "logs"

BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
STOP = threading.Event()

# --------------------------------------------------------------------------
# Logging: console + rotating file, thread-safe by design
# --------------------------------------------------------------------------
LOG_DIR.mkdir(exist_ok=True)
log = logging.getLogger("greyt_sync")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
_fh = RotatingFileHandler(LOG_DIR / "sync.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
_fh.setFormatter(_fmt)
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
log.addHandler(_fh)
log.addHandler(_ch)
try:  # Windows consoles default to cp1252; keep unicode names from crashing a log line
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def now_local() -> dt.datetime:
    return dt.datetime.now(TZ)


# --------------------------------------------------------------------------
# Supabase REST client with retries (one Session per thread)
# --------------------------------------------------------------------------
_tls = threading.local()


def http() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        retry = Retry(
            total=4,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST", "PATCH"}),
            raise_on_status=False,
        )
        s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=8))
        s.headers.update(
            {
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
            }
        )
        _tls.session = s
    return s


def rest(method: str, path: str, *, params=None, json_body=None, prefer: str | None = None, timeout=30):
    headers = {"Prefer": prefer} if prefer else {}
    res = http().request(
        method, f"{SUPABASE_URL}/rest/v1/{path}", params=params, json=json_body, headers=headers, timeout=timeout
    )
    if res.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {res.status_code}: {res.text[:300]}")
    if res.status_code == 204 or not res.content:
        return None
    return res.json()


# --------------------------------------------------------------------------
# Data shapes
# --------------------------------------------------------------------------
@dataclass(frozen=True, order=True)
class Swipe:
    at: dt.datetime  # tz-aware, local
    kind: str        # "IN" | "OUT"

    def to_json(self):
        return {"type": self.kind, "time": self.at.strftime("%H:%M:%S")}


@dataclass
class ScrapeResult:
    swipes: list[Swipe] = field(default_factory=list)
    source: str = ""          # "network" | "page" | ""
    error: str | None = None  # short machine-readable reason
    login_failed: bool = False


# --------------------------------------------------------------------------
# Swipe parsing — network JSON
# --------------------------------------------------------------------------
_DIR_KEYS = ("inoutindicator", "inout", "direction", "swipetype", "punchtype", "iotype", "inouttype", "type", "mode")
_TIME_KEYS = (
    "punchdatetime", "swipedatetime", "swipetime", "punchtime", "datetime",
    "logtime", "swipedate", "punchdate", "time", "timestamp",
)
_TIME_RE = re.compile(
    r"(?:(\d{4})-(\d{2})-(\d{2})[T ])?(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*([AaPp][Mm])?"
)


def _norm_dir(v):
    """Map a direction value to IN/OUT. Numbers come back as 'NUM:<n>' for later calibration."""
    if isinstance(v, bool):
        return "NUM:1" if v else "NUM:0"
    if isinstance(v, (int, float)):
        return f"NUM:{int(v)}"
    if not isinstance(v, str):
        return None
    s = v.strip().upper()
    if s in ("IN", "I", "ENTRY", "ENTER", "CHECKIN", "CHECK-IN", "CHECK IN", "PUNCHIN", "PUNCH IN"):
        return "IN"
    if s in ("OUT", "O", "EXIT", "CHECKOUT", "CHECK-OUT", "CHECK OUT", "PUNCHOUT", "PUNCH OUT"):
        return "OUT"
    if s.isdigit():
        return f"NUM:{int(s)}"
    return None


def _parse_when(v, day: dt.date) -> dt.datetime | None:
    if isinstance(v, (int, float)) and v > 10**9:  # epoch seconds or ms
        ts = v / 1000 if v > 10**12 else v
        return dt.datetime.fromtimestamp(ts, TZ)
    if not isinstance(v, str):
        return None
    s = v.strip()
    # Full ISO with offset / Z
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", s) and (s.endswith("Z") or re.search(r"[+-]\d{2}:?\d{2}$", s)):
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ)
        except ValueError:
            pass
    m = _TIME_RE.search(s)
    if not m:
        return None
    y, mo, d, h, mi, se, ap = m.groups()
    h, mi, se = int(h), int(mi), int(se or 0)
    if ap and h <= 12:
        ap = ap.lower()
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
    if h > 23 or mi > 59 or se > 59:
        return None
    the_day = dt.date(int(y), int(mo), int(d)) if y else day
    return dt.datetime(the_day.year, the_day.month, the_day.day, h, mi, se, tzinfo=TZ)


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def swipes_from_json(payloads: list, day: dt.date) -> list[Swipe]:
    raw: list[tuple[dt.datetime, str]] = []
    for payload in payloads:
        for node in _walk(payload):
            low = {k.lower(): v for k, v in node.items() if isinstance(k, str)}
            direction = next((_norm_dir(low[k]) for k in _DIR_KEYS if k in low and _norm_dir(low[k])), None)
            when = next((_parse_when(low[k], day) for k in _TIME_KEYS if k in low and _parse_when(low[k], day)), None)
            if direction and when and when.date() == day:
                raw.append((when, direction))
    if not raw:
        return []

    # Calibrate numeric indicators. greytHR normally uses 1 = IN, 0 = OUT, but
    # we verify against reality: the first swipe of a day is always an IN.
    raw.sort()
    nums = {d for _, d in raw if d.startswith("NUM:")}
    mapping = {"NUM:1": "IN", "NUM:0": "OUT"}
    if nums and raw[0][1] == "NUM:0" and nums <= {"NUM:0", "NUM:1"}:
        mapping = {"NUM:0": "IN", "NUM:1": "OUT"}
    out = []
    for when, d in raw:
        kind = mapping.get(d, d)
        if kind in ("IN", "OUT"):
            out.append(Swipe(when, kind))
    return sorted(set(out))


# --------------------------------------------------------------------------
# Swipe parsing — visible page text (fallback)
# --------------------------------------------------------------------------
_ROW_RE = re.compile(
    r"(?<![A-Za-z])(?<!First )(?<!Last )(?<!first )(?<!last )"
    r"(IN|OUT|In|Out|ENTRY|EXIT|Entry|Exit)\s+"
    r"(\d{1,2}):(\d{2}):(\d{2})\s*([AaPp][Mm])?\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})?"
)


def swipes_from_text(text: str, day: dt.date) -> list[Swipe]:
    out = set()
    for m in _ROW_RE.finditer(text):
        word, h, mi, se, ap, day_str = m.groups()
        if day_str:
            try:
                if dt.datetime.strptime(" ".join(day_str.split()), "%d %b %Y").date() != day:
                    continue
            except ValueError:
                pass
        when = _parse_when(f"{h}:{mi}:{se} {ap or ''}", day)
        if when:
            out.add(Swipe(when, "IN" if word.upper() in ("IN", "ENTRY") else "OUT"))
    return sorted(out)


# --------------------------------------------------------------------------
# Browser scrape
# --------------------------------------------------------------------------
READY_JS = r"""() => {
    const t = document.body ? document.body.innerText : '';
    return /Swipes\s*\(Total:/i.test(t) || /\b(IN|OUT)\s+\d{1,2}:\d{2}:\d{2}/.test(t);
}"""


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def _session_file(username: str) -> Path:
    return SESSION_DIR / f"{_safe(username)}.json"


def _block_heavy(route):
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def _is_sso(page) -> bool:
    return "idp.greythr.com" in page.url or "hydra/login" in page.url


def debug_dump(page, username: str, note: str, extra_dir: Path | None = None):
    try:
        target = extra_dir or DEBUG_DIR
        target.mkdir(parents=True, exist_ok=True)
        base = target / f"{_safe(username)}_{now_local():%Y%m%d_%H%M%S}"
        try:
            page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception:
            pass
        with open(base.with_suffix(".txt"), "w", encoding="utf-8") as f:
            f.write(f"note: {note}\nurl: {page.url}\n\n=== VISIBLE TEXT ===\n")
            f.write(page.evaluate("() => document.body ? document.body.innerText : ''")[:40000])
        log.info("[%s] debug saved: %s.(png|txt)", username, base)
    except Exception as e:
        log.warning("[%s] couldn't save debug artifacts: %s", username, e)


def scrape_user(domain: str, username: str, password: str, day: dt.date, probe_dir: Path | None = None) -> ScrapeResult:
    from playwright.sync_api import sync_playwright  # imported here so --help works without it

    result = ScrapeResult()
    attendance_url = f"https://{domain}/v3/portal/ess/attendance/attendance-info"
    state_file = _session_file(username)
    captured: list = []
    captured_lock = threading.Lock()

    def on_response(resp):
        try:
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            if "json" not in (resp.headers.get("content-type") or ""):
                return
            url = resp.url.lower()
            if not any(k in url for k in ("swipe", "attendance", "punch")):
                return
            body = resp.json()
            with captured_lock:
                captured.append(body)
            if probe_dir:
                probe_dir.mkdir(parents=True, exist_ok=True)
                n = len(list(probe_dir.glob("resp_*.json")))
                (probe_dir / f"resp_{n:02d}.json").write_text(
                    json.dumps({"url": resp.url, "body": body}, indent=2, default=str), encoding="utf-8"
                )
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, args=["--disable-gpu", "--disable-extensions", "--no-first-run"])
        ctx_args = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
            "viewport": {"width": 1600, "height": 1000},
            "timezone_id": str(TZ),
        }
        context = None
        if state_file.exists():
            try:
                context = browser.new_context(storage_state=str(state_file), **ctx_args)
            except Exception:
                context = None
        if context is None:
            context = browser.new_context(**ctx_args)
        context.route("**/*", _block_heavy)
        page = context.new_page()
        page.on("response", on_response)

        try:
            page.goto(attendance_url, wait_until="domcontentloaded", timeout=30000)
            swipes_hdr = page.locator("text=Swipes").first
            login_box = page.locator("input[name='username']").first
            try:
                swipes_hdr.or_(login_box).wait_for(timeout=25000)
            except Exception:
                pass

            if _is_sso(page):
                result.error = "sso"
                debug_dump(page, username, "sso-redirect")
                return result

            if not swipes_hdr.is_visible():
                if not login_box.is_visible():
                    page.goto(f"https://{domain}/", wait_until="domcontentloaded", timeout=30000)
                    if _is_sso(page):
                        result.error = "sso"
                        return result
                page.fill("input[name='username']", username, timeout=15000)
                page.fill("input[name='password']", password, timeout=15000)
                page.click("button[type='submit']")
                try:
                    page.wait_for_url(lambda u: "/v3/portal/" in u, timeout=45000, wait_until="commit")
                except Exception:
                    debug_dump(page, username, "login-failed")
                    state_file.unlink(missing_ok=True)
                    result.error, result.login_failed = "login_failed", True
                    return result
                try:
                    SESSION_DIR.mkdir(exist_ok=True)
                    context.storage_state(path=str(state_file))
                    try:
                        os.chmod(state_file, 0o600)
                    except OSError:
                        pass
                except Exception as e:
                    log.warning("[%s] couldn't save session: %s", username, e)
                if "attendance-info" not in page.url:
                    page.goto(attendance_url, wait_until="domcontentloaded", timeout=30000)
                swipes_hdr.wait_for(timeout=25000)

            # Make sure the swipes card is open and its data has loaded.
            try:
                page.wait_for_function(READY_JS, timeout=3000, polling=250)
            except Exception:
                swipes_hdr.click()
                try:
                    page.wait_for_function(READY_JS, timeout=20000, polling=250)
                except Exception:
                    swipes_hdr.click()
                    try:
                        page.wait_for_function(READY_JS, timeout=8000, polling=250)
                    except Exception:
                        pass
            # Give any trailing swipe XHRs a moment to land.
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass

            text = page.evaluate("() => document.body ? document.body.innerText : ''")
            with captured_lock:
                payloads = list(captured)
            net = swipes_from_json(payloads, day)
            dom = swipes_from_text(text, day)

            # Prefer the network list; if the page shows swipes the JSON missed, merge them.
            if net:
                merged = sorted(set(net) | set(dom))
                result.swipes, result.source = merged, "network"
            else:
                result.swipes, result.source = dom, "page"

            if probe_dir:
                probe_dir.mkdir(parents=True, exist_ok=True)
                (probe_dir / "page_text.txt").write_text(text, encoding="utf-8")
                (probe_dir / "parsed.json").write_text(
                    json.dumps(
                        {"network": [s.to_json() for s in net],
                         "page": [s.to_json() for s in dom],
                         "used": result.source},
                        indent=2,
                    ),
                    encoding="utf-8",
                )

            if not result.swipes:
                result.error = "no_swipes"
                if not re.search(r"Swipes\s*\(Total:\s*0", text):
                    debug_dump(page, username, "no-swipes-found")
        except Exception as e:
            result.error = f"navigation: {type(e).__name__}"
            log.error("[%s] portal error: %s", username, e)
            debug_dump(page, username, f"exception: {e}")
        finally:
            browser.close()
    return result


# --------------------------------------------------------------------------
# Day maths
# --------------------------------------------------------------------------
@dataclass
class DaySummary:
    first_in: dt.datetime
    active_ms: int
    break_ms: int
    state: str                 # ACTIVE | ON_BREAK
    last_change: dt.datetime
    last_in: dt.datetime | None
    last_out: dt.datetime | None
    missed_out: int            # IN followed by IN (an OUT swipe was never recorded)


def summarise(swipes: list[Swipe], now: dt.datetime) -> DaySummary:
    """
    Pairs IN->OUT. When two INs follow each other (missed OUT), the second IN
    starts a new active stretch and the gap counts as a break — the same way
    greytHR treats it — and the day is flagged so the UI can warn about it.
    """
    active_ms, open_in, missed = 0, None, 0
    for s in swipes:
        if s.kind == "IN":
            if open_in is not None:
                missed += 1
            open_in = s.at
        elif open_in is not None:
            active_ms += int((s.at - open_in).total_seconds() * 1000)
            open_in = None
    if open_in is not None:
        active_ms += max(0, int((now - open_in).total_seconds() * 1000))

    first_in = next((s.at for s in swipes if s.kind == "IN"), swipes[0].at)
    ins = [s.at for s in swipes if s.kind == "IN"]
    outs = [s.at for s in swipes if s.kind == "OUT"]
    last = swipes[-1]
    wall_ms = int((now - first_in).total_seconds() * 1000)
    return DaySummary(
        first_in=first_in,
        active_ms=max(0, active_ms),
        break_ms=max(0, wall_ms - active_ms),
        state="ACTIVE" if last.kind == "IN" else "ON_BREAK",
        last_change=last.at,
        last_in=ins[-1] if ins else None,
        last_out=outs[-1] if outs else None,
        missed_out=missed,
    )


def clock(d: dt.datetime | None) -> str:
    return d.strftime("%H:%M:%S") if d else "--:--"


# --------------------------------------------------------------------------
# Supabase operations
# --------------------------------------------------------------------------
def get_enabled_users() -> list[dict]:
    return rest("POST", "rpc/get_all_greythr_credentials", json_body={}) or []


def is_manual_override(user_id: str, day_str: str) -> bool:
    rows = rest(
        "GET", "tracker_logs",
        params={"user_id": f"eq.{user_id}", "date_tracked": f"eq.{day_str}", "select": "is_manual_override"},
    ) or []
    return bool(rows and rows[0].get("is_manual_override"))


def _parse_clock_on(day: dt.date, s: str | None) -> dt.datetime | None:
    s = (s or "").strip()
    return _parse_when(s, day) if s and s != "--:--" else None


def finalize_past_days(today_str: str):
    rows = rest(
        "GET", "tracker_logs",
        params={
            "date_tracked": f"lt.{today_str}",
            "current_state": "in.(ACTIVE,ON_BREAK)",
            "select": "user_id,date_tracked,first_in_time,accumulated_active_ms,last_in_str,last_out_str,is_manual_override",
        },
    ) or []
    closed = 0
    for r in rows:
        patch = {"current_state": "COMPLETED"}
        try:
            if not r.get("is_manual_override"):
                day = dt.date.fromisoformat(r["date_tracked"])
                first_in = dt.datetime.fromisoformat(r["first_in_time"]).astimezone(TZ) if r.get("first_in_time") else None
                last_out = _parse_clock_on(day, r.get("last_out_str"))
                last_in = _parse_clock_on(day, r.get("last_in_str"))
                if first_in and last_out and (not last_in or last_out >= last_in):
                    active = int(float(r.get("accumulated_active_ms") or 0))
                    patch["accumulated_break_ms"] = max(0, int((last_out - first_in).total_seconds() * 1000) - active)
                    patch["last_state_change_time"] = last_out.isoformat()
            rest("PATCH", "tracker_logs",
                 params={"user_id": f"eq.{r['user_id']}", "date_tracked": f"eq.{r['date_tracked']}"},
                 json_body=patch)
            closed += 1
        except Exception as e:
            log.warning("[%s] couldn't close %s: %s", r.get("user_id"), r.get("date_tracked"), e)
    if rows:
        log.info("Closed %d/%d past day(s).", closed, len(rows))


def push_heartbeat(started: dt.datetime, attempted: int, synced: int, skipped: int, failed: list[dict]):
    try:
        rest(
            "POST", "sync_status",
            json_body={
                "id": True,
                "last_cycle_started_at": started.isoformat(),
                "last_cycle_completed_at": now_local().isoformat(),
                "users_attempted": attempted,
                "users_synced": synced,
                "users_skipped": skipped,
                "users_failed": len(failed),
                "failed_users": failed,
                "agent_host": platform.node(),
                "agent_version": "2.0",
            },
            prefer="resolution=merge-duplicates,return=minimal",
        )
    except Exception as e:
        log.warning("Heartbeat failed: %s", e)


# --------------------------------------------------------------------------
# Per-user sync
# --------------------------------------------------------------------------
_login_failures: dict[str, int] = {}
_backoff_until: dict[str, dt.datetime] = {}
_state_lock = threading.Lock()

SYNCED, SKIPPED, FAILED = "synced", "skipped", "failed"


def sync_one_user(u: dict, day: dt.date) -> tuple[str, str | None]:
    user_id, domain, username, password = u["user_id"], u["domain"], u["username"], u["password"]
    day_str = day.isoformat()

    with _state_lock:
        until = _backoff_until.get(user_id)
    if until and now_local() < until:
        return SKIPPED, "login_backoff"

    if is_manual_override(user_id, day_str):
        return SKIPPED, "manual_override"

    t0 = time.monotonic()
    res = scrape_user(domain, username, password, day)

    with _state_lock:
        if res.login_failed:
            _login_failures[user_id] = _login_failures.get(user_id, 0) + 1
            if _login_failures[user_id] >= LOGIN_FAILURE_LIMIT:
                _backoff_until[user_id] = now_local() + dt.timedelta(minutes=LOGIN_BACKOFF_MINUTES)
                log.warning("[%s] %d failed logins in a row — pausing this user for %d min to avoid a lockout.",
                            username, _login_failures[user_id], LOGIN_BACKOFF_MINUTES)
        elif not res.error:
            _login_failures.pop(user_id, None)
            _backoff_until.pop(user_id, None)

    if res.error == "no_swipes":
        return SKIPPED, "no_swipes_yet"
    if res.error:
        return FAILED, res.error

    now = now_local()
    s = summarise(res.swipes, now)
    payload = {
        "user_id": user_id,
        "date_tracked": day_str,
        "current_state": s.state,
        "first_in_time": s.first_in.isoformat(),
        "last_state_change_time": s.last_change.isoformat(),
        "accumulated_active_ms": s.active_ms,
        "accumulated_break_ms": s.break_ms,
        "is_manual_override": False,
        "last_scraped_at": now.isoformat(),
        "last_in_str": clock(s.last_in),
        "last_out_str": clock(s.last_out),
        "swipes": [x.to_json() for x in res.swipes],
        "missed_out_count": s.missed_out,
        "swipe_source": res.source,
    }
    try:
        rest("POST", "tracker_logs", json_body=payload,
             params={"on_conflict": "user_id,date_tracked"},
             prefer="resolution=merge-duplicates,return=minimal")
    except RuntimeError as e:
        # Migration not run yet? Retry without the v2 columns so syncing never stops.
        if any(c in str(e) for c in ("swipes", "missed_out_count", "swipe_source")):
            for k in ("swipes", "missed_out_count", "swipe_source"):
                payload.pop(k, None)
            rest("POST", "tracker_logs", json_body=payload,
                 params={"on_conflict": "user_id,date_tracked"},
                 prefer="resolution=merge-duplicates,return=minimal")
            log.warning("Run supabase/migration_v2.sql — swipe list columns are missing.")
        else:
            raise

    trail = " ".join(f"{x.kind}@{x.at:%H:%M}" for x in res.swipes)
    warn = f" | {s.missed_out} missing OUT" if s.missed_out else ""
    log.info("[%s] %s | active %.2fh break %dm | %s via %s (%.1fs)%s",
             username, s.state, s.active_ms / 3.6e6, s.break_ms // 60000, trail, res.source,
             time.monotonic() - t0, warn)
    return SYNCED, None


# --------------------------------------------------------------------------
# Cycle / main loop
# --------------------------------------------------------------------------
def in_window(now: dt.datetime) -> bool:
    try:
        a, b = (dt.time.fromisoformat(x.strip()) for x in SYNC_WINDOW.split("-"))
    except ValueError:
        return True
    t = now.time()
    return a <= t <= b if a <= b else (t >= a or t <= b)


def cleanup_debug():
    cutoff = time.time() - DEBUG_RETENTION_DAYS * 86400
    if not DEBUG_DIR.exists():
        return
    for f in DEBUG_DIR.rglob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def run_cycle() -> bool:
    started = now_local()
    day = started.date()
    try:
        finalize_past_days(day.isoformat())
    except Exception as e:
        log.warning("Close-out failed: %s", e)

    if not in_window(started):
        return False

    try:
        users = get_enabled_users()
    except Exception as e:
        log.error("Could not fetch enabled users: %s", e)
        return True
    if not users:
        log.info("No opted-in users.")
        push_heartbeat(started, 0, 0, 0, [])
        return True

    workers = len(users) if MAX_CONCURRENT_USERS <= 0 else min(len(users), MAX_CONCURRENT_USERS)
    synced = skipped = 0
    failed: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="user") as ex:
        futs = {ex.submit(sync_one_user, u, day): u for u in users}
        for f in as_completed(futs):
            u = futs[f]
            try:
                status, reason = f.result()
            except Exception as e:
                status, reason = FAILED, f"error: {type(e).__name__}"
                log.exception("[%s] unexpected error", u.get("username"))
            if status == SYNCED:
                synced += 1
            elif status == SKIPPED:
                skipped += 1
                if reason not in ("manual_override", "no_swipes_yet"):
                    log.info("[%s] skipped: %s", u.get("username"), reason)
            else:
                failed.append({"user_id": u["user_id"], "username": u.get("username"), "reason": reason})
                hint = {
                    "sso": "account uses SSO — can't be automated, use Quick Correction",
                    "login_failed": "wrong/expired password, reset page or OTP prompt",
                }.get(reason or "", reason)
                log.error("[%s] failed: %s", u.get("username"), hint)

    push_heartbeat(started, len(users), synced, skipped, failed)
    log.info("Cycle done in %.1fs: %d synced, %d skipped, %d failed (of %d, %d at a time).",
             (now_local() - started).total_seconds(), synced, skipped, len(failed), len(users), workers)
    return True


def probe(username: str):
    """One-off diagnostic scrape for a single user. Writes nothing to the database."""
    users = [u for u in get_enabled_users() if u.get("username", "").lower() == username.lower()]
    if not users:
        sys.exit(f"No opted-in user with username {username!r}.")
    u = users[0]
    out = DEBUG_DIR / f"probe_{_safe(username)}_{now_local():%Y%m%d_%H%M%S}"
    res = scrape_user(u["domain"], u["username"], u["password"], now_local().date(), probe_dir=out)
    print(f"\nsource: {res.source or '-'}   error: {res.error or '-'}")
    for s in res.swipes:
        print(f"  {s.kind:<3}  {s.at:%H:%M:%S}")
    if res.swipes:
        d = summarise(res.swipes, now_local())
        print(f"\nstate {d.state} | active {d.active_ms/3.6e6:.2f}h | break {d.break_ms//60000}m | "
              f"last in {clock(d.last_in)} | last out {clock(d.last_out)} | missing OUT {d.missed_out}")
    print(f"\nRaw captures saved to: {out}")


def main():
    ap = argparse.ArgumentParser(description="greytHR -> Supabase sync agent")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--probe", metavar="USERNAME", help="diagnose one user's scrape (no DB writes)")
    args = ap.parse_args()

    if not SUPABASE_SERVICE_KEY:
        sys.exit("SUPABASE_SERVICE_KEY is not set. Put it in sync/.env or an environment variable (service_role key).")
    if SUPABASE_SERVICE_KEY.count(".") == 2:
        try:
            import base64
            claims = json.loads(base64.urlsafe_b64decode(SUPABASE_SERVICE_KEY.split(".")[1] + "=="))
            if claims.get("role") != "service_role":
                sys.exit("SUPABASE_SERVICE_KEY is not a service_role key (looks like the anon key).")
        except (ValueError, json.JSONDecodeError):
            pass

    if args.probe:
        probe(args.probe)
        return

    def _stop(*_):
        log.info("Stopping after the current cycle…")
        STOP.set()

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    cap = "no cap" if MAX_CONCURRENT_USERS <= 0 else f"max {MAX_CONCURRENT_USERS} at once"
    log.info("greytHR sync agent v2 started (%s, every %ss, window %s, %s)", cap, SYNC_INTERVAL_SECONDS, SYNC_WINDOW, TZ)
    cycles = 0
    while not STOP.is_set():
        busy = run_cycle()
        cycles += 1
        if cycles % 60 == 1:
            cleanup_debug()
        if args.once:
            break
        STOP.wait(SYNC_INTERVAL_SECONDS if busy else IDLE_INTERVAL_SECONDS)
    log.info("Agent stopped.")


if __name__ == "__main__":
    main()
