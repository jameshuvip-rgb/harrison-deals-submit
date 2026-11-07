#!/usr/bin/env python3
"""Harrison Deals submit app with email/password auth and owner-only edit."""
from __future__ import annotations

import gzip
import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import urllib.error
import urllib.request

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).resolve().parent
# SQLite (app.db, seed_overrides) and uploaded covers share one persistent root.
# Unset locally: both stay next to this file (data/, uploads/).
# Production: HD_PERSIST_ROOT=/data with a Railway volume mounted at /data.
# The app creates those two directories on boot. Inbox JSON stays beside the app.
_persist_root = (os.environ.get("HD_PERSIST_ROOT") or "").strip()
PERSIST_ROOT = Path(_persist_root) if _persist_root else ROOT
DATA = PERSIST_ROOT / "data"
INBOX = ROOT / "inbox"
UPLOADS = PERSIST_ROOT / "uploads"
DB_PATH = DATA / "app.db"
SECRET_FILE = DATA / "secret_key"

HOST = os.environ.get("HD_SUBMIT_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT") or os.environ.get("HD_SUBMIT_PORT", "8788"))
PUBLIC_BASE = os.environ.get("HD_PUBLIC_BASE", "").rstrip("/")
LANDING_URL = os.environ.get(
    "HD_LANDING_URL",
    "https://cursor.com/canvas/store-c6bc41b9-1220-400b-8a10-1e09699984bc/068db308-d2d0-4aa9-95c1-28fd421e1511",
).rstrip("/")
INBOX_KEY = (os.environ.get("HD_INBOX_KEY") or "").strip()

DEPTS = ["Automotive", "Home", "Sports", "Electronics", "Tools", "Pets", "Other"]
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PROMO_CODE_RE = re.compile(r"^[A-Z0-9](?:[A-Z0-9_-]{0,31})$")
CODE_NOTE_MAX = 120
CONTACT_TITLE_MAX = 120
CONTACT_MESSAGE_MAX = 4000
CONTACT_EMAIL_MAX = 254
COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# 8 MiB image cap. The request cap is a little higher so the multipart
# form around an 8MB file is not rejected before we can save it or show
# an on-page error.
COVER_MAX_BYTES = 8 * 1024 * 1024
REQUEST_MAX_BYTES = COVER_MAX_BYTES + 512 * 1024
# Hot Deals: slot 1 is views in this window, slot 2 is biggest % off.
HOT_LIMIT = 3
VIEW_WINDOW_SEC = 30 * 60
VIEW_DEBOUNCE_SEC = 5 * 60
VIEW_KEEP_SEC = 24 * 60 * 60
LOOKUP_COVER_TTL = 30 * 60
AMAZON_HTML_TIMEOUT = 6
AMAZON_IMAGE_TIMEOUT = 5
AMAZON_HTML_MAX = 1_500_000
# One product-page GET, then at most two image GETs (best URL, then one fallback).
AMAZON_IMAGE_TRIES = 2
# One browser-like User-Agent, one attempt. A custom bot UA is blocked immediately.
AMAZON_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
MSG_FILLED = "Filled from link"
MSG_NO_PRICE = "Couldn't fetch price — enter manually"
MSG_NO_COVER = "Cover unavailable — upload your own"
MSG_DUPLICATE = "This product is already posted."
DEAL_KEY_RE = re.compile(r"^(?:user:[1-9]\d*|seed:[A-Za-z0-9_-]{1,64})$")
AMAZON_ASIN_RE = re.compile(
    r"(?:/dp/|/gp/product/|/gp/aw/d/|/product/)([A-Za-z0-9]{10})(?:[/?#]|$)",
    re.I,
)

for d in (DATA, INBOX, UPLOADS):
    d.mkdir(parents=True, exist_ok=True)

SEED_DEALS_PATH = ROOT / "deals.json"
DELETED_SEED_PATH = DATA / "deleted_seed_ids.json"
SEED_OVERRIDES_PATH = DATA / "seed_overrides.json"
DEFAULT_ADMIN_EMAIL = "jameshuvip@gmail.com"

DEPT_TO_CAT = {
    "Automotive": "automotive",
    "Home": "home",
    "Sports": "sports",
    "Electronics": "electronics",
    "Tools": "tools",
    "Pets": "pets",
    "Other": "other",
}

NAV_CATS = [
    {"id": "all", "label": "All", "icon": "◎", "blurb": "Every live deal"},
    {"id": "automotive", "label": "Automotive", "icon": "🚗", "blurb": "Filters & parts"},
    {"id": "home", "label": "Home", "icon": "🏠", "blurb": "Appliance parts & more"},
    {"id": "sports", "label": "Sports", "icon": "🏃", "blurb": "Bike & outdoor"},
    {"id": "electronics", "label": "Electronics", "icon": "📱", "blurb": "Coming soon"},
    {"id": "tools", "label": "Tools & DIY", "icon": "🔧", "blurb": "Coming soon"},
    {"id": "beauty", "label": "Beauty", "icon": "✨", "blurb": "Coming soon"},
    {"id": "baby", "label": "Baby", "icon": "🍼", "blurb": "Coming soon"},
    {"id": "clothing", "label": "Clothing", "icon": "👕", "blurb": "Coming soon"},
    {"id": "grocery", "label": "Grocery", "icon": "🛒", "blurb": "Coming soon"},
    {"id": "health", "label": "Health", "icon": "💊", "blurb": "Coming soon"},
    {"id": "pets", "label": "Pets", "icon": "🐾", "blurb": "Pond filter media"},
]


def load_seed_deals() -> list[dict]:
    if not SEED_DEALS_PATH.exists():
        return []
    raw = json.loads(SEED_DEALS_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return []
    return [d for d in raw if isinstance(d, dict)]


def _read_json_file(path: Path, default):
    if not path.exists():
        return default
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return raw if isinstance(raw, type(default)) else default


def load_deleted_seed_ids() -> set[str]:
    raw = _read_json_file(DELETED_SEED_PATH, [])
    return {str(x) for x in raw if x is not None}


def save_deleted_seed_ids(ids: set[str]) -> None:
    DELETED_SEED_PATH.write_text(
        json.dumps(sorted(ids), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_seed_overrides() -> dict:
    raw = _read_json_file(SEED_OVERRIDES_PATH, {})
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def save_seed_overrides(overrides: dict) -> None:
    SEED_OVERRIDES_PATH.write_text(
        json.dumps(overrides, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def apply_seed_overlay(deals: list[dict]) -> list[dict]:
    deleted = load_deleted_seed_ids()
    overrides = load_seed_overrides()
    out = []
    for deal in deals:
        sid = str(deal.get("id") or "")
        if not sid or sid in deleted:
            continue
        item = dict(deal)
        if sid in overrides:
            item.update(overrides[sid])
            item["id"] = sid
        item["source"] = "seed"
        item["owner_email"] = item.get("owner_email")
        item["db_id"] = None
        out.append(item)
    return out


def off_pct(price: float, was: float) -> int:
    if was <= 0 or price >= was:
        return 0
    return round(((was - price) / was) * 100)


def deal_badge(deal: dict, pct: int) -> str:
    """Card-face label. A coupon is `CODE`, never the full code string."""
    code = str(deal.get("promo_code") or "").strip()
    if code:
        if pct:
            return f"CODE · {pct}% OFF"
        return "CODE"
    if deal.get("promo") == "bxgy":
        return f"BXGY ~{pct or 30}%"
    if pct:
        return f"{pct}% OFF"
    return ""


def amazon_href(deal: dict) -> str:
    asin = str(deal.get("id") or "")
    base = str(deal.get("href") or f"https://www.amazon.ca/dp/{asin}")
    parsed = urlparse(base)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["utm_source"] = "harrison"
    query["utm_medium"] = "deals"
    query["utm_campaign"] = asin
    return urlunparse(parsed._replace(query=urlencode(query)))


def deal_initials(title: str) -> str:
    words = [w for w in str(title).split() if w]
    return "".join(w[0] for w in words[:2]).upper() or "HD"


def enrich_deal(deal: dict) -> dict:
    price = float(deal.get("price") or 0)
    was = float(deal.get("was") or 0)
    pct = off_pct(price, was)
    out = dict(deal)
    out["price"] = price
    out["was"] = was
    out["off"] = pct
    out["promo_code"] = str(deal.get("promo_code") or "").strip()
    out["code_note"] = str(deal.get("code_note") or "").strip()
    out["amazon_url"] = amazon_href(deal)
    out["initials"] = deal_initials(deal.get("title") or "")
    out["badge"] = deal_badge(out, pct)
    out.setdefault("source", "seed")
    out.setdefault("owner_email", None)
    out.setdefault("db_id", None)
    out.setdefault("can_manage", False)
    out.setdefault("manage_url", None)
    out.setdefault("delete_url", None)
    out["track_key"] = deal_track_key(out)
    out.setdefault("views_30m", 0)
    out.setdefault("favorites", 0)
    out.setdefault("favorited", False)
    return out


def deal_track_key(deal: dict) -> str:
    """Stable engagement key. User rows use the SQLite id; seeds use the catalog id."""
    source = str(deal.get("source") or "seed")
    if source == "user" and deal.get("db_id"):
        return f"user:{int(deal['db_id'])}"
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(deal.get("id") or ""))[:64]
    return f"seed:{safe}" if safe else ""


def admin_emails() -> set[str]:
    raw = (os.environ.get("HD_ADMIN_EMAILS") or "").strip()
    if not raw:
        return {DEFAULT_ADMIN_EMAIL}
    emails = {normalize_email(e) for e in raw.split(",") if e.strip()}
    return emails or {DEFAULT_ADMIN_EMAIL}


def is_admin() -> bool:
    email = current_email()
    return bool(email) and email in admin_emails()


def can_manage_owner(owner_email: str | None) -> bool:
    email = current_email()
    if not email:
        return False
    if is_admin():
        return True
    return bool(owner_email) and email == normalize_email(owner_email)


def can_manage_deal(row) -> bool:
    if row is None:
        return False
    if hasattr(row, "keys"):
        owner = row["owner_email"] if "owner_email" in row.keys() else None
    else:
        owner = row.get("owner_email")
    return can_manage_owner(owner)


def user_deal_to_card(row: sqlite3.Row) -> dict:
    dept = (row["department"] or "").strip()
    cat = DEPT_TO_CAT.get(dept, (dept or "other").lower())
    asin = (row["asin"] or "").strip().upper()
    deal_id = int(row["id"])
    card_id = asin or f"u{deal_id}"
    price = float(row["deal_price"] or 0)
    was = float(row["list_price"] or 0) or price
    href = (row["amazon_url"] or "").strip() or (f"https://www.amazon.ca/dp/{asin}" if asin else "#")
    image = (row["cover_url"] or "").strip() or None
    if not image and row["cover_path"]:
        image = f"/uploads/{row['cover_path']}"
    as_of = ""
    posted = 0
    try:
        ts = row["updated_at"] or row["created_at"] or ""
        if ts:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            as_of = dt.strftime("%b ") + str(dt.day)
            posted = int(dt.timestamp())
    except Exception:
        as_of = ""
        posted = deal_id
    note = (row["buyer_note"] or "").strip()
    keys = set(row.keys())
    promo_code = (row["promo_code"] or "").strip() if "promo_code" in keys else ""
    code_note = (row["code_note"] or "").strip() if "code_note" in keys else ""
    return {
        "id": card_id,
        "source": "user",
        "db_id": deal_id,
        "owner_email": row["owner_email"],
        "category": cat,
        "title": row["title"],
        "line": note or dept or "",
        "price": price,
        "was": was,
        "fit": dept or "",
        "detail": note or "",
        "promo_code": promo_code,
        "code_note": code_note,
        "href": href,
        "live": True,
        "promo": "percent",
        "ago": "",
        "posted": posted,
        "asOf": as_of,
        "featured": False,
        "image": image,
        "stale": False,
    }


def load_user_deal_cards() -> list[dict]:
    db = get_db()
    rows = db.execute(
        """
        SELECT * FROM deals
        WHERE deleted_at IS NULL
        ORDER BY datetime(updated_at) DESC, id DESC
        """
    ).fetchall()
    return [user_deal_to_card(r) for r in rows]


def attach_manage_flags(deals: list[dict]) -> list[dict]:
    admin = is_admin()
    email = current_email()
    out = []
    for deal in deals:
        item = dict(deal)
        source = item.get("source") or "seed"
        owner = item.get("owner_email")
        if source == "user" and item.get("db_id"):
            can = bool(email) and (admin or (owner and email == normalize_email(owner)))
            item["can_manage"] = can
            if can:
                item["manage_url"] = url_for("edit_deal", deal_id=int(item["db_id"]))
                item["delete_url"] = url_for("delete_deal", deal_id=int(item["db_id"]))
            else:
                item["manage_url"] = None
                item["delete_url"] = None
        else:
            # seed: only admins
            can = admin
            item["can_manage"] = can
            if can:
                sid = str(item.get("id") or "")
                item["manage_url"] = url_for("edit_seed_deal", seed_id=sid)
                item["delete_url"] = url_for("delete_seed_deal", seed_id=sid)
            else:
                item["manage_url"] = None
                item["delete_url"] = None
        out.append(item)
    return out


def has_real_image(deal: dict) -> bool:
    img = str(deal.get("image") or "").strip()
    return bool(img) and not img.startswith("data:")


def _hot_tie_key(deal: dict) -> tuple:
    """favorites desc, then updated/posted desc, then a real cover, then id."""
    favorites = int(deal.get("favorites") or 0)
    updated = int(deal.get("posted") or 0)
    image = 1 if has_real_image(deal) else 0
    return (favorites, updated, image, str(deal.get("id") or ""))


def _hot_winner(cands: list[dict]) -> dict:
    best = cands[0]
    best_key = _hot_tie_key(best)
    for deal in cands[1:]:
        key = _hot_tie_key(deal)
        if key > best_key:
            best = deal
            best_key = key
    return best


def _tag_hot(deal: dict, label: str) -> dict:
    item = dict(deal)
    item["hot_label"] = label
    return item


def pick_hot_deals(feed: list[dict], limit: int = HOT_LIMIT) -> list[dict]:
    """Up to three Hot Deals cards, each with a real cover image.

    Slot 1 is the most-viewed deal in the last 30 minutes.
    Slot 2 is the biggest % off.
    Slot 3 is the most saved.
    If a slot's leader was already used, that slot takes the next-best on the same axis.
    Ties break on favorites, then updated/posted, then a real cover.
    With no views and no saves, fill by biggest % off then latest, still at most three.
    """
    pool = [d for d in feed if has_real_image(d)]
    if not pool:
        return []
    limit = max(0, min(int(limit or HOT_LIMIT), HOT_LIMIT))
    if limit == 0:
        return []

    def views(deal: dict) -> int:
        return int(deal.get("views_30m") or 0)

    def off(deal: dict) -> int:
        return int(deal.get("off") or 0)

    def fav(deal: dict) -> int:
        return int(deal.get("favorites") or 0)

    def identity(deal: dict) -> str:
        return str(deal.get("track_key") or deal.get("id") or "")

    def ranked_fallback(taken: set[str]) -> list[dict]:
        rest = [d for d in pool if identity(d) not in taken]
        return sorted(rest, key=lambda d: (off(d),) + _hot_tie_key(d), reverse=True)

    def best_on(score, taken: set[str]):
        cands = [d for d in pool if identity(d) not in taken and score(d) > 0]
        if not cands:
            return None
        best = max(score(d) for d in cands)
        return _hot_winner([d for d in cands if score(d) == best])

    if not any(views(d) > 0 for d in pool) and not any(fav(d) > 0 for d in pool):
        chosen = ranked_fallback(set())[:limit]
        top_off = off(chosen[0]) if chosen else 0
        out = []
        for deal in chosen:
            label = "Biggest % off" if off(deal) > 0 and off(deal) == top_off else "Latest"
            out.append(_tag_hot(deal, label))
        return out

    taken: set[str] = set()
    result: list[dict] = []
    for score, label in (
        (views, "Most viewed"),
        (off, "Biggest % off"),
        (fav, "Most saved"),
    ):
        if len(result) >= limit:
            break
        winner = best_on(score, taken)
        if winner is None:
            continue
        taken.add(identity(winner))
        result.append(_tag_hot(winner, label))

    if len(result) < limit:
        extras = ranked_fallback(taken)
        top_off = off(extras[0]) if extras else 0
        discount_taken = any(item.get("hot_label") == "Biggest % off" for item in result)
        for deal in extras:
            if len(result) >= limit:
                break
            if not discount_taken and off(deal) > 0 and off(deal) == top_off:
                label = "Biggest % off"
            else:
                label = "Latest"
            result.append(_tag_hot(deal, label))
    return result


def sort_feed(feed: list[dict], sort: str) -> list[dict]:
    sort = (sort or "latest").strip().lower()
    if sort == "off":
        return sorted(feed, key=lambda d: int(d.get("off") or 0), reverse=True)
    if sort == "price":
        return sorted(feed, key=lambda d: float(d.get("price") or 0))
    # latest (default)
    return sorted(feed, key=lambda d: int(d.get("posted") or 0), reverse=True)



def relative_time_label(deal: dict) -> str:
    """Human relative time for live rail — prefer seed ago, else unix posted."""
    ago = str(deal.get("ago") or "").strip()
    if ago:
        return ago
    try:
        ts = int(deal.get("posted") or 0)
    except (TypeError, ValueError):
        return "just now"
    # Seed ranking ints are tiny; only treat real unix timestamps
    if ts < 1_000_000_000:
        return "just now"
    diff = max(0, int(time.time()) - ts)
    if diff < 45:
        return "just now"
    if diff < 3600:
        m = max(1, diff // 60)
        return f"{m}m ago"
    if diff < 86400:
        h = max(1, diff // 3600)
        return f"{h}h ago"
    days = max(1, diff // 86400)
    return "1d ago" if days == 1 else f"{days}d ago"


def build_live_activity(merged: list[dict], limit: int = 16) -> list[dict]:
    """Recent deal events for the right-rail live feed (no fake users/shares)."""
    limit = max(12, min(int(limit or 16), 20))
    ranked = sort_feed(list(merged), "latest")
    out: list[dict] = []
    for deal in ranked:
        if len(out) >= limit:
            break
        title = str(deal.get("title") or "").strip()
        if not title:
            continue
        short = title if len(title) <= 42 else (title[:39].rstrip() + "…")
        price = float(deal.get("price") or 0)
        event = "Just live" if deal.get("source") == "user" else "Newly posted"
        # freshest few tagged as new for red accent
        is_new = len(out) < 4
        out.append(
            {
                "id": deal.get("id"),
                "title": title,
                "short_title": short,
                "price": price,
                "was": float(deal.get("was") or 0),
                "off": int(deal.get("off") or 0),
                "badge": deal.get("badge") or "",
                "line": deal.get("line") or "",
                "detail": deal.get("detail") or deal.get("fit") or "",
                "fit": deal.get("fit") or "",
                "asOf": deal.get("asOf") or "",
                "stale": bool(deal.get("stale")),
                "image": deal.get("image") or "",
                "amazon_url": deal.get("amazon_url") or "#",
                "promo": str(deal.get("promo") or "").strip(),
                "promo_code": str(deal.get("promo_code") or "").strip(),
                "code_note": str(deal.get("code_note") or "").strip(),
                "rel_time": relative_time_label(deal),
                "event": event,
                "is_new": is_new,
                "track_key": deal.get("track_key") or "",
                "favorites": int(deal.get("favorites") or 0),
                "favorited": bool(deal.get("favorited")),
            }
        )
    return out


def visible_nav_cats(counts: dict) -> list[dict]:
    """Always list every department; empty ones keep Coming soon blurbs."""
    return list(NAV_CATS)



def _load_secret_key() -> str:
    env = os.environ.get("SECRET_KEY") or os.environ.get("HD_SECRET_KEY")
    if env:
        return env
    if SECRET_FILE.exists():
        key = SECRET_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_hex(32)
    SECRET_FILE.write_text(key, encoding="utf-8")
    try:
        SECRET_FILE.chmod(0o600)
    except OSError:
        pass
    return key


app = Flask(__name__)
app.config["SECRET_KEY"] = _load_secret_key()
app.config["MAX_CONTENT_LENGTH"] = REQUEST_MAX_BYTES
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = PUBLIC_BASE.startswith("https://")


@app.context_processor
def inject_globals():
    return {
        "landing_url": LANDING_URL,
        "session_email": current_email(),
        "is_admin": is_admin(),
    }


@app.before_request
def ensure_visitor():
    if request.path.startswith("/static") or request.path.startswith("/uploads") or request.path == "/health":
        return
    if not session.get("vid"):
        session["vid"] = secrets.token_hex(16)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = sqlite3.connect(DB_PATH, timeout=5)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_email TEXT NOT NULL,
            title TEXT NOT NULL,
            asin TEXT,
            amazon_url TEXT,
            deal_price REAL NOT NULL,
            list_price REAL,
            department TEXT,
            buyer_note TEXT,
            cover_path TEXT,
            cover_url TEXT,
            notion_hint TEXT,
            promo_code TEXT,
            code_note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_deals_owner ON deals(owner_email);
        CREATE TABLE IF NOT EXISTS contact_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            email TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_contact_messages_created
            ON contact_messages(created_at);
        CREATE TABLE IF NOT EXISTS deal_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_key TEXT NOT NULL,
            session_key TEXT NOT NULL,
            ip_key TEXT NOT NULL,
            viewed_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_deal_views_deal_time
            ON deal_views(deal_key, viewed_at);
        CREATE TABLE IF NOT EXISTS deal_favorites (
            deal_key TEXT NOT NULL,
            user_email TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (deal_key, user_email)
        );
        CREATE INDEX IF NOT EXISTS idx_deal_favorites_user
            ON deal_favorites(user_email);
        """
    )
    ensure_deal_columns(db)
    db.commit()
    db.close()


def ensure_deal_columns(db: sqlite3.Connection) -> None:
    """Add coupon columns on databases created before promo codes existed."""
    cols = {row[1] for row in db.execute("PRAGMA table_info(deals)")}
    if "promo_code" not in cols:
        db.execute("ALTER TABLE deals ADD COLUMN promo_code TEXT")
    if "code_note" not in cols:
        db.execute("ALTER TABLE deals ADD COLUMN code_note TEXT")


init_db()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("email"):
            if request.path.startswith("/api/"):
                return jsonify(error="Login required"), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def current_email() -> str:
    return (session.get("email") or "").strip().lower()


def current_user_id() -> int | None:
    uid = session.get("user_id")
    return int(uid) if uid is not None else None


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def parse_price(raw: str, label: str, required: bool = False):
    value = (raw or "").strip()
    if not value:
        if required:
            raise ValueError(f"{label} is required")
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number") from exc


def normalize_promo_code(raw: str) -> str | None:
    code = re.sub(r"\s+", "", (raw or "")).upper()
    if not code:
        return None
    if not PROMO_CODE_RE.fullmatch(code):
        raise ValueError(
            "Promo code must be 1–32 letters, digits, hyphens, or underscores"
        )
    return code


def normalize_code_note(raw: str) -> str | None:
    note = (raw or "").strip()
    if not note:
        return None
    if len(note) > CODE_NOTE_MAX:
        raise ValueError("Code note is too long")
    return note


def parse_deal_fields(src) -> dict:
    title = (src.get("title") or "").strip()
    if not title:
        raise ValueError("Product title is required")
    if len(title) > 200:
        raise ValueError("Title is too long")

    deal_price = parse_price(src.get("deal_price"), "Deal price", required=True)
    list_price = parse_price(src.get("list_price"), "List price")

    asin = (src.get("asin") or "").strip().upper()
    if asin and not ASIN_RE.match(asin):
        raise ValueError("ASIN must be 10 letters/digits")

    department = (src.get("department") or "").strip()
    if department and department not in DEPTS:
        raise ValueError("Invalid department")

    amazon_url = (src.get("amazon_url") or "").strip() or None
    buyer_note = (src.get("buyer_note") or "").strip() or None
    if buyer_note and len(buyer_note) > 500:
        raise ValueError("Buyer note is too long")
    promo_code = normalize_promo_code(src.get("promo_code") or "")
    code_note = normalize_code_note(src.get("code_note") or "")

    return {
        "title": title,
        "asin": asin or None,
        "amazon_url": amazon_url,
        "deal_price": deal_price,
        "list_price": list_price,
        "department": department or None,
        "buyer_note": buyer_note,
        "promo_code": promo_code,
        "code_note": code_note,
    }


def public_cover_url(cover_path: str | None) -> str | None:
    """URL the app already uses for a file stored under uploads/."""
    if not cover_path:
        return None
    rel = f"/uploads/{str(cover_path).lstrip('/')}"
    if PUBLIC_BASE:
        return f"{PUBLIC_BASE}{rel}"
    return rel


def deal_cover_src(deal) -> str:
    """Preview src for an existing deal. Keeps legacy cover_url and /static/deals paths."""
    url = str(_deal_value(deal, "cover_url") or "").strip()
    if url:
        return url
    path = str(_deal_value(deal, "cover_path") or "").strip()
    if path:
        return f"/uploads/{path.lstrip('/')}"
    return ""


def _cover_too_large() -> ValueError:
    return ValueError("Cover image is too large (max 8MB). Choose a smaller JPG, PNG, WEBP, or GIF.")


def _classify_cover(blob: bytes) -> tuple[bytes, str]:
    """Return stored bytes and extension. Accepts JPEG/PNG/GIF/WEBP, including a short junk prefix."""
    data = blob
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    data = data.lstrip(b"\t\r\n ")
    if data.startswith(b"8BPS"):
        raise ValueError(
            "This is a Photoshop PSD file. Export the artboard as a JPG or PNG under 8MB and upload that file."
        )
    if data.startswith(b"\xff\xd8\xff"):
        return data, ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return data, ".gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return data, ".webp"
    # Some exports prepend a short header before a normal JPEG.
    soi = data.find(b"\xff\xd8\xff", 0, 512)
    if soi > 0:
        return data[soi:], ".jpg"
    raise ValueError(
        "That file is not a JPG, PNG, WEBP, or GIF. Export it as a standard JPG or PNG under 8MB and try again."
    )


def cover_removal_requested() -> bool:
    return (request.form.get("remove_cover") or "").strip().lower() in {"1", "true", "yes", "on"}


def read_cover_upload():
    """Return (bytes, ext) for the cover file field, or None when no file was chosen.

    The original filename is ignored. Files are renamed on save, so spaces and
    non-ASCII names (such as a Photoshop "画板 1.jpg" export) are fine.
    """
    f = request.files.get("cover")
    if not f or not (f.filename or "").strip():
        return None
    blob = f.read(COVER_MAX_BYTES + 1)
    if not blob:
        raise ValueError("Cover image file is empty. Choose a JPG, PNG, WEBP, or GIF.")
    if len(blob) > COVER_MAX_BYTES:
        raise _cover_too_large()
    return _classify_cover(blob)


def store_user_cover_bytes(user_id: int, blob: bytes, ext: str) -> str:
    folder = UPLOADS / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time())}_{secrets.token_hex(4)}{ext}"
    (folder / name).write_bytes(blob)
    return f"{user_id}/{name}"


def store_seed_cover_bytes(seed_id: str, blob: bytes, ext: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", seed_id)[:40] or "seed"
    folder = UPLOADS / "seed" / safe
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time())}_{secrets.token_hex(4)}{ext}"
    (folder / name).write_bytes(blob)
    return f"/uploads/seed/{safe}/{name}"


def save_cover(user_id: int):
    parsed = read_cover_upload()
    if parsed is None:
        return None
    blob, ext = parsed
    return store_user_cover_bytes(user_id, blob, ext)


def save_seed_cover(seed_id: str) -> str | None:
    """Store a replacement seed cover and return the path written into the seed override."""
    parsed = read_cover_upload()
    if parsed is None:
        return None
    blob, ext = parsed
    return store_seed_cover_bytes(seed_id, blob, ext)


def _purge_old_lookup_files(folder: Path) -> None:
    if not folder.is_dir():
        return
    cutoff = time.time() - LOOKUP_COVER_TTL
    for path in folder.glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def save_lookup_cover(user_id: int, blob: bytes) -> tuple[str, str] | None:
    """Best-effort cover for the current form only. Not shared across users."""
    try:
        data, ext = _classify_cover(blob)
    except ValueError:
        return None
    if len(data) > COVER_MAX_BYTES:
        return None
    folder = UPLOADS / "_lookup" / str(int(user_id))
    folder.mkdir(parents=True, exist_ok=True)
    _purge_old_lookup_files(folder)
    token = secrets.token_hex(16)
    (folder / f"{token}{ext}").write_bytes(data)
    return token, f"/uploads/_lookup/{int(user_id)}/{token}{ext}"


def consume_lookup_cover(user_id: int | None) -> tuple[bytes, str] | None:
    """Take a scraped cover token posted with this form. A seller file upload wins instead."""
    if user_id is None:
        return None
    token = (request.form.get("scraped_cover") or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        return None
    folder = UPLOADS / "_lookup" / str(int(user_id))
    if not folder.is_dir():
        return None
    matches = [
        path
        for path in folder.glob(token + ".*")
        if path.is_file() and path.suffix.lower() in COVER_EXTS
    ]
    if len(matches) != 1:
        return None
    src = matches[0]
    try:
        if time.time() - src.stat().st_mtime > LOOKUP_COVER_TTL:
            src.unlink(missing_ok=True)
            return None
        blob = src.read_bytes()
    except OSError:
        return None
    try:
        data, ext = _classify_cover(blob)
    except ValueError:
        try:
            src.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    try:
        src.unlink(missing_ok=True)
    except OSError:
        pass
    return data, ext


def resolve_user_cover(user_id: int) -> str | None:
    uploaded = save_cover(user_id)
    if uploaded:
        return uploaded
    scraped = consume_lookup_cover(user_id)
    if not scraped:
        return None
    blob, ext = scraped
    return store_user_cover_bytes(user_id, blob, ext)


def _viewer_fingerprints() -> tuple[str, str]:
    vid = (session.get("vid") or "").strip()
    if not vid:
        vid = secrets.token_hex(16)
        session["vid"] = vid
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
    ip_key = "i:"
    if ip:
        digest = hashlib.sha256(f"{app.secret_key}|{ip}".encode()).hexdigest()[:32]
        ip_key = f"i:{digest}"
    return f"s:{vid}", ip_key


def same_origin_request() -> bool:
    fetch_site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if fetch_site in {"cross-site", "same-site"}:
        return False
    origin = (request.headers.get("Origin") or "").strip()
    if not origin:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return (parsed.hostname or "").lower() == request.host.split(":")[0].lower()


def live_deal_for_key(deal_key: str) -> bool:
    if not DEAL_KEY_RE.match(deal_key or ""):
        return False
    kind, ident = deal_key.split(":", 1)
    db = get_db()
    if kind == "user":
        row = db.execute(
            "SELECT 1 FROM deals WHERE id = ? AND deleted_at IS NULL",
            (int(ident),),
        ).fetchone()
        return row is not None
    if ident in load_deleted_seed_ids():
        return False
    return _find_seed_raw(ident) is not None


def attach_engagement(deals: list[dict]) -> None:
    keys = [str(d.get("track_key") or "") for d in deals if d.get("track_key")]
    if not keys:
        return
    db = get_db()
    now = int(time.time())
    marks = ",".join("?" * len(keys))
    view_rows = db.execute(
        f"""
        SELECT deal_key, COUNT(*) AS n
        FROM deal_views
        WHERE viewed_at >= ? AND deal_key IN ({marks})
        GROUP BY deal_key
        """,
        (now - VIEW_WINDOW_SEC, *keys),
    ).fetchall()
    fav_rows = db.execute(
        f"""
        SELECT deal_key, COUNT(*) AS n
        FROM deal_favorites
        WHERE deal_key IN ({marks})
        GROUP BY deal_key
        """,
        tuple(keys),
    ).fetchall()
    mine: set[str] = set()
    email = current_email()
    if email:
        mine_rows = db.execute(
            f"""
            SELECT deal_key FROM deal_favorites
            WHERE user_email = ? AND deal_key IN ({marks})
            """,
            (email, *keys),
        ).fetchall()
        mine = {str(row["deal_key"]) for row in mine_rows}
    views = {str(row["deal_key"]): int(row["n"]) for row in view_rows}
    favs = {str(row["deal_key"]): int(row["n"]) for row in fav_rows}
    for deal in deals:
        key = str(deal.get("track_key") or "")
        deal["views_30m"] = views.get(key, 0)
        deal["favorites"] = favs.get(key, 0)
        deal["favorited"] = key in mine


def record_deal_view(deal_key: str) -> bool:
    """Count one detail open. Same session or same IP inside a few minutes counts once."""
    now = int(time.time())
    session_key, ip_key = _viewer_fingerprints()
    db = get_db()
    db.execute("DELETE FROM deal_views WHERE viewed_at < ?", (now - VIEW_KEEP_SEC,))
    row = db.execute(
        """
        SELECT 1 FROM deal_views
        WHERE deal_key = ?
          AND viewed_at >= ?
          AND (session_key = ? OR (? != 'i:' AND ip_key = ?))
        LIMIT 1
        """,
        (deal_key, now - VIEW_DEBOUNCE_SEC, session_key, ip_key, ip_key),
    ).fetchone()
    if row:
        db.commit()
        return False
    db.execute(
        """
        INSERT INTO deal_views (deal_key, session_key, ip_key, viewed_at)
        VALUES (?, ?, ?, ?)
        """,
        (deal_key, session_key, ip_key, now),
    )
    db.commit()
    return True


def favorite_count(deal_key: str) -> int:
    row = get_db().execute(
        "SELECT COUNT(*) AS n FROM deal_favorites WHERE deal_key = ?",
        (deal_key,),
    ).fetchone()
    return int(row["n"] if row else 0)


def toggle_favorite(deal_key: str, email: str) -> bool:
    db = get_db()
    existing = db.execute(
        "SELECT 1 FROM deal_favorites WHERE deal_key = ? AND user_email = ?",
        (deal_key, email),
    ).fetchone()
    if existing:
        db.execute(
            "DELETE FROM deal_favorites WHERE deal_key = ? AND user_email = ?",
            (deal_key, email),
        )
        favorited = False
    else:
        db.execute(
            """
            INSERT INTO deal_favorites (deal_key, user_email, created_at)
            VALUES (?, ?, ?)
            """,
            (deal_key, email, utc_now()),
        )
        favorited = True
    db.commit()
    return favorited


def amazon_marketplace(url: str) -> tuple[str, str] | None:
    """Return (ASIN, 'ca'|'com') from an amazon.ca or amazon.com product URL."""
    raw = (url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower().strip(".")
    if host == "amazon.ca" or host.endswith(".amazon.ca"):
        market = "ca"
    elif host == "amazon.com" or host.endswith(".amazon.com"):
        market = "com"
    else:
        return None
    match = AMAZON_ASIN_RE.search(parsed.path or "")
    asin = match.group(1).upper() if match else ""
    if not asin:
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        asin = (query.get("asin") or query.get("ASIN") or "").strip().upper()
    if not ASIN_RE.match(asin):
        return None
    return asin, market


def normalize_asin(raw: str | None) -> str | None:
    asin = (raw or "").strip().upper()
    if ASIN_RE.match(asin):
        return asin
    return None


def product_identity(asin: str | None, amazon_url: str | None) -> tuple[set[str], set[tuple[str, str]]]:
    """ASINs plus (marketplace, ASIN) pairs that identify one product.

    The form ASIN and the ASIN parsed from an amazon.ca / amazon.com URL both count.
    URL identity is marketplace + ASIN, so tracking params and /dp/ vs /gp/product/ do not matter.
    """
    asins: set[str] = set()
    markets: set[tuple[str, str]] = set()
    normalized = normalize_asin(asin)
    if normalized:
        asins.add(normalized)
    parsed = amazon_marketplace(amazon_url or "")
    if parsed:
        asins.add(parsed[0])
        markets.add((parsed[1], parsed[0]))
    return asins, markets


def visible_seed_asins() -> set[str]:
    """Seed catalog ids currently shown on the site. Deleted or unpublished seeds are excluded."""
    found: set[str] = set()
    for deal in apply_seed_overlay(load_seed_deals()):
        if deal.get("live") is False:
            continue
        asin = normalize_asin(str(deal.get("id") or ""))
        if asin:
            found.add(asin)
    return found


def duplicate_product_conflict(
    asin: str | None,
    amazon_url: str | None,
    ignore_deal_id: int | None = None,
    ignore_seed_id: str | None = None,
) -> bool:
    """True when this product is already an active user deal or a visible seed.

    Editing a deal ignores that deal's current ASIN and URL, so saving the same
    product is allowed. Only a different deal or seed blocks the change.
    """
    asins, markets = product_identity(asin, amazon_url)
    if ignore_deal_id is not None:
        row = get_db().execute(
            "SELECT id, asin, amazon_url FROM deals WHERE id = ? AND deleted_at IS NULL",
            (int(ignore_deal_id),),
        ).fetchone()
        if row is not None:
            own_asins, own_markets = product_identity(row["asin"], row["amazon_url"])
            asins -= own_asins
            markets -= own_markets
            markets = {pair for pair in markets if pair[1] not in own_asins}
    if not asins and not markets:
        return False

    rows = get_db().execute(
        """
        SELECT id, asin, amazon_url FROM deals
        WHERE deleted_at IS NULL
        """
    ).fetchall()
    for row in rows:
        if ignore_deal_id is not None and int(row["id"]) == int(ignore_deal_id):
            continue
        row_asins, row_markets = product_identity(row["asin"], row["amazon_url"])
        if asins & row_asins or markets & row_markets:
            return True

    seeds = visible_seed_asins()
    if ignore_seed_id:
        seed_asin = normalize_asin(ignore_seed_id)
        if seed_asin:
            seeds.discard(seed_asin)
    return bool(asins & seeds)


def ensure_unique_product(fields: dict, ignore_deal_id: int | None = None) -> None:
    if duplicate_product_conflict(
        fields.get("asin"),
        fields.get("amazon_url"),
        ignore_deal_id=ignore_deal_id,
    ):
        raise ValueError(MSG_DUPLICATE)


def _manageable_deal_id(raw) -> int | None:
    """Deal id the current user may edit, used only to soften the lookup warning."""
    if raw is None or raw == "":
        return None
    try:
        deal_id = int(raw)
    except (TypeError, ValueError):
        return None
    if deal_id <= 0:
        return None
    row = get_db().execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    if row is None or row["deleted_at"]:
        return None
    if not can_manage_deal(row):
        return None
    return deal_id


def flag_duplicate_lookup(
    result: dict,
    raw_url: str,
    ignore_deal_id: int | None = None,
    ignore_seed_id: str | None = None,
) -> None:
    """Soft warning. Lookup still fills ASIN; submit is what rejects the duplicate."""
    if not result.get("ok"):
        return
    conflict = duplicate_product_conflict(
        result.get("asin"),
        raw_url,
        ignore_deal_id=ignore_deal_id,
        ignore_seed_id=ignore_seed_id,
    )
    result["duplicate"] = bool(conflict)
    if not conflict:
        return
    messages = list(result.get("messages") or [])
    if MSG_DUPLICATE not in messages:
        messages.append(MSG_DUPLICATE)
    result["messages"] = messages


def _html_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = (
        text.replace("&amp;", "&")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
        .replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return re.sub(r"\s+", " ", text).strip()


def _money(raw: str) -> float | None:
    try:
        value = float((raw or "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return round(value, 2)


def _first_money(patterns: list[str], html: str) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, html, re.I | re.S)
        if not match:
            continue
        value = _money(match.group(1))
        if value is not None:
            return value
    return None


def _price_in_chunk(chunk: str) -> float | None:
    """First visible price inside a bounded slice. Ignores blank a-offscreen nodes."""
    if not chunk:
        return None
    match = re.search(
        r'class="a-offscreen">\s*(?:CDN\s*)?\$?\s*([0-9][0-9,]*(?:\.[0-9]{2})?)',
        chunk,
        re.I,
    )
    if match:
        value = _money(match.group(1))
        if value is not None:
            return value
    whole = re.search(
        r'class="a-price-whole">\s*([0-9][0-9,]*)\s*<.*?class="a-price-fraction">\s*([0-9]{2})',
        chunk,
        re.I | re.S,
    )
    if not whole:
        return None
    return _money(f"{whole.group(1)}.{whole.group(2)}")


def _price_after_anchor(html: str, anchor: str, window: int = 700) -> float | None:
    start = 0
    while True:
        idx = html.find(anchor, start)
        if idx < 0:
            return None
        value = _price_in_chunk(html[idx : idx + window])
        if value is not None:
            return value
        start = idx + len(anchor)


def _widget_chunk(html: str, widget_id: str, window: int = 8000) -> str:
    marker = f'id="{widget_id}"'
    idx = html.find(marker)
    if idx < 0:
        return ""
    return html[idx : idx + window]


def _amazon_image_host_ok(host: str) -> bool:
    host = (host or "").lower().strip(".")
    suffixes = (
        "media-amazon.com",
        "ssl-images-amazon.com",
        "images-amazon.com",
        "amazon.com",
        "amazon.ca",
    )
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


_MAIN_IMAGE_ID_RE = re.compile(
    r"""\bid\s*=\s*(['"])(?:landingImage|imgBlkFront|ebooksImgBlkFront|main-image|img-canvas)\1""",
    re.I,
)
_AMAZON_I_PATH_RE = re.compile(
    r"^(?P<prefix>/images/I/)(?P<image_id>[A-Za-z0-9%+\-]+?)(?P<mod>\._[^/]+)?\.(?P<ext>jpg|jpeg|png|webp|gif)$",
    re.I,
)
_UNSIZED_AMAZON_I_RE = re.compile(
    r"/images/I/[A-Za-z0-9%+\-]+\.(?:jpg|jpeg|png|webp|gif)$",
    re.I,
)
_AMAZON_EDGE_RE = re.compile(r"(?:SL|UL|SX|SY|SS|US|UX|UY|SR)(\d{2,4})", re.I)
_SKIP_IMAGE_MARKERS = (
    "sprite",
    "play-button",
    "overlay-thumb",
    "transparent-pixel",
    "grey-pixel",
    "gray-pixel",
    "spinner",
    "loading",
    "pixel.gif",
    "blank.gif",
)


def _tag_attr(tag: str, name: str) -> str | None:
    match = re.search(
        rf"""\b{re.escape(name)}\s*=\s*(['"])(.*?)\1""",
        tag,
        re.I | re.S,
    )
    if not match:
        return None
    return match.group(2)


def _unescape_amazon_url(raw: str) -> str:
    text = (raw or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    text = (
        text.replace("\\/", "/")
        .replace("\\u002f", "/")
        .replace("\\u002F", "/")
        .replace("\\u0026", "&")
    )
    text = html.unescape(text).strip()
    if text.startswith("//"):
        text = "https:" + text
    return text


def _valid_amazon_image_url(raw: str) -> str | None:
    text = _unescape_amazon_url(raw)
    if not text or text.lower().startswith("http://"):
        return None
    text = text.split()[0].rstrip("\\',);>")
    parsed = urlparse(text)
    if parsed.scheme != "https" or not _amazon_image_host_ok(parsed.hostname or ""):
        return None
    path = (parsed.path or "").lower()
    if "/images/" not in path:
        return None
    if any(marker in path for marker in _SKIP_IMAGE_MARKERS):
        return None
    if not re.search(r"\.(?:jpg|jpeg|png|webp|gif)$", path):
        return None
    return urlunparse(parsed._replace(fragment=""))


def _declared_edge(path: str) -> int | None:
    if _UNSIZED_AMAZON_I_RE.search(path or ""):
        return None
    nums = [int(n) for n in _AMAZON_EDGE_RE.findall(path or "")]
    if not nums:
        return None
    return max(nums)


def _amazon_image_pixels(url: str) -> int:
    path = urlparse(url).path or ""
    if _UNSIZED_AMAZON_I_RE.search(path):
        return 2560 * 2560
    edge = _declared_edge(path)
    if edge:
        return edge * edge
    if "._ac_" in path.lower():
        return 1000 * 1000
    return 800 * 800


def _amazon_image_id(url: str) -> str | None:
    match = _AMAZON_I_PATH_RE.match(urlparse(url).path or "")
    if not match:
        return None
    return match.group("image_id").lower()


def _sl1500_variant(url: str) -> str | None:
    """Larger sibling of a sized /images/I/ URL. Unsized and already-large URLs stay put."""
    parsed = urlparse(url)
    match = _AMAZON_I_PATH_RE.match(parsed.path or "")
    if not match or not match.group("mod"):
        return None
    edge = _declared_edge(parsed.path or "")
    if edge is None or edge >= 1500:
        return None
    larger_path = (
        f"{match.group('prefix')}{match.group('image_id')}._AC_SL1500_.{match.group('ext')}"
    )
    if larger_path == parsed.path:
        return None
    return urlunparse(parsed._replace(path=larger_path, query="", fragment=""))


def _dynamic_image_pairs(raw: str) -> list[tuple[str, int]]:
    text = html.unescape(raw or "").replace("\\/", "/")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    pairs: list[tuple[str, int]] = []
    for key, dims in data.items():
        pixels = 0
        if isinstance(dims, (list, tuple)) and len(dims) >= 2:
            try:
                width = int(float(dims[0]))
                height = int(float(dims[1]))
            except (TypeError, ValueError):
                width = height = 0
            if width > 0 and height > 0:
                pixels = width * height
        pairs.append((str(key), pixels))
    return pairs


def _amazon_cover_urls(page_html: str) -> list[str]:
    """Product-image URLs, largest main image first.

    Prefers landingImage / data-old-hires / the first hiRes over later gallery
    or carousel images, and within that set prefers the biggest CDN size.
    A sized image under 1500px also yields one synthesized ._AC_SL1500_ sibling.
    """
    if not page_html:
        return []
    ranked: dict[str, int] = {}

    def add(raw: str | None, bonus: int, pixels: int | None = None) -> None:
        if not raw:
            return
        url = _valid_amazon_image_url(raw)
        if not url:
            return
        area = pixels if pixels and pixels > 0 else _amazon_image_pixels(url)
        source_bonus = 1_000 if area < 64 * 64 else bonus
        ranked[url] = max(ranked.get(url, 0), source_bonus + area)
        if source_bonus < 1_000_000:
            return
        larger = _sl1500_variant(url)
        if not larger or larger == url:
            return
        larger_area = _amazon_image_pixels(larger)
        if larger_area > area:
            ranked[larger] = max(ranked.get(larger, 0), source_bonus + larger_area)

    found_main = False
    for tag in re.findall(r"<img\b[^>]*>", page_html, flags=re.I | re.S):
        if not _MAIN_IMAGE_ID_RE.search(tag):
            continue
        found_main = True
        add(_tag_attr(tag, "data-old-hires"), 80_000_000)
        add(_tag_attr(tag, "data-a-hires"), 80_000_000)
        dynamic = _tag_attr(tag, "data-a-dynamic-image")
        if dynamic:
            for image_url, dims in _dynamic_image_pairs(dynamic):
                add(image_url, 50_000_000, pixels=dims or None)
        add(_tag_attr(tag, "src"), 50_000_000)

    if not found_main:
        dynamic_match = re.search(
            r"""data-a-dynamic-image\s*=\s*(['"])(.*?)\1""",
            page_html,
            re.I | re.S,
        )
        if dynamic_match:
            for image_url, dims in _dynamic_image_pairs(dynamic_match.group(2)):
                add(image_url, 40_000_000, pixels=dims or None)

    for attr_name in ("data-old-hires", "data-a-hires"):
        for match in re.finditer(
            rf"""\b{attr_name}\s*=\s*(['"])(https?:[^'"]+)\1""",
            page_html,
            re.I,
        ):
            add(match.group(2), 80_000_000)

    hires_matches = list(
        re.finditer(
            r"""['"]hiRes['"]\s*:\s*['"](https?:[^'"]+)['"]""",
            page_html,
            re.I,
        )
    )
    for index, match in enumerate(hires_matches):
        add(match.group(1), 50_000_000 if index == 0 else 20_000_000)

    large_matches = list(
        re.finditer(
            r"""['"]large['"]\s*:\s*['"](https?:[^'"]+)['"]""",
            page_html,
            re.I,
        )
    )
    for index, match in enumerate(large_matches):
        add(match.group(1), 45_000_000 if index == 0 else 15_000_000)

    for key, bonus in (
        ("landingImageHiRes", 80_000_000),
        ("landingImageUrl", 45_000_000),
        ("mainUrl", 40_000_000),
    ):
        for match in re.finditer(
            rf"""['"]{key}['"]\s*:\s*['"](https?:[^'"]+)['"]""",
            page_html,
            re.I,
        ):
            add(match.group(1), bonus)

    for tag in re.findall(r"<meta\b[^>]*>", page_html, flags=re.I | re.S):
        kind = (_tag_attr(tag, "property") or _tag_attr(tag, "name") or "").lower()
        if kind == "og:image":
            add(_tag_attr(tag, "content"), 40_000_000)
    for tag in re.findall(r"<link\b[^>]*>", page_html, flags=re.I | re.S):
        if (_tag_attr(tag, "rel") or "").lower() == "image_src":
            add(_tag_attr(tag, "href"), 35_000_000)

    ordered = sorted(ranked.items(), key=lambda item: item[1], reverse=True)
    urls = [url for url, _score in ordered]
    if len(urls) < 2:
        return urls
    best_id = _amazon_image_id(urls[0])
    if not best_id:
        return urls
    for index, url in enumerate(urls[1:], start=1):
        if _amazon_image_id(url) != best_id:
            continue
        if index != 1:
            urls.insert(1, urls.pop(index))
        break
    return urls


def parse_amazon_html(html: str) -> dict:
    """Pull title, current price, list price, and a main image URL. Empty on a block page."""
    result = {
        "blocked": False,
        "title": None,
        "deal_price": None,
        "list_price": None,
        "image_url": None,
        "image_candidates": [],
    }
    if not html:
        return result
    lowered = html.lower()
    blocked_markers = (
        "opfcaptcha",
        "validatecaptcha",
        "enter the characters you see below",
        "sorry, we just need to make sure you're not a robot",
        "robot check",
        "api-services-support@amazon.com",
    )
    if any(marker in lowered for marker in blocked_markers) and 'id="producttitle"' not in lowered:
        result["blocked"] = True
        return result

    title = None
    match = re.search(r'id="productTitle"[^>]*>(.*?)</span>', html, re.I | re.S)
    if match:
        title = _html_text(match.group(1))
    if not title:
        match = re.search(r'property="og:title"[^>]*content="([^"]+)"', html, re.I)
        if not match:
            match = re.search(r'content="([^"]+)"[^>]*property="og:title"', html, re.I)
        if match:
            title = _html_text(match.group(1))
    if title:
        title = re.sub(r"\s*[:|–-]\s*Amazon\.(ca|com)\s*$", "", title, flags=re.I).strip()
        title = re.sub(r"^Amazon\.(ca|com)\s*[:|–-]\s*", "", title, flags=re.I).strip()
        if len(title) > 200:
            title = title[:200].rstrip()
        result["title"] = title or None

    # Prefer the buy-box "price to pay". A page-wide search also hits recommendation carousels.
    deal = _price_after_anchor(html, "apex-pricetopay-value")
    if deal is None:
        deal = _first_money(
            [
                r'"priceToPay"\s*:\s*\{[^{}]*?"priceAmount"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
                r'id="priceblock_dealprice"[^>]*>\s*(?:CDN\s*)?\$?\s*([0-9][0-9,]*(?:\.[0-9]{2})?)',
                r'id="priceblock_ourprice"[^>]*>\s*(?:CDN\s*)?\$?\s*([0-9][0-9,]*(?:\.[0-9]{2})?)',
            ],
            html,
        )
    price_chunks = [
        _widget_chunk(html, "corePrice_feature_div"),
        _widget_chunk(html, "corePriceDisplay_desktop_feature_div"),
    ]
    if deal is None:
        for chunk in price_chunks:
            deal = _price_in_chunk(chunk)
            if deal is not None:
                break
    result["deal_price"] = deal
    list_price = _first_money(
        [
            r'"basisPrice"\s*:\s*\{[^{}]*?"priceAmount"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
            r'id="priceblock_listprice"[^>]*>\s*(?:CDN\s*)?\$?\s*([0-9][0-9,]*(?:\.[0-9]{2})?)',
        ],
        html,
    )
    if list_price is None:
        for chunk in price_chunks:
            strike = re.search(
                r'(?:a-text-price|data-a-strike="true")[^>]*>.*?class="a-offscreen">\s*(?:CDN\s*)?\$?\s*([0-9][0-9,]*(?:\.[0-9]{2})?)',
                chunk,
                re.I | re.S,
            )
            if strike:
                list_price = _money(strike.group(1))
                if list_price is not None:
                    break
    if list_price is not None and deal is not None and list_price <= deal:
        list_price = None
    result["list_price"] = list_price

    urls = _amazon_cover_urls(html)
    result["image_candidates"] = urls[:AMAZON_IMAGE_TRIES]
    result["image_url"] = urls[0] if urls else None
    return result


class _AmazonRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        host = (parsed.hostname or "").lower()
        allowed = parsed.scheme in {"http", "https"} and (
            host == "amazon.ca"
            or host.endswith(".amazon.ca")
            or host == "amazon.com"
            or host.endswith(".amazon.com")
            or host.endswith(".media-amazon.com")
            or host.endswith(".ssl-images-amazon.com")
            or host.endswith(".images-amazon.com")
        )
        if not allowed:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _amazon_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_AmazonRedirectHandler)


def fetch_amazon_html(url: str, accept_language: str) -> str | None:
    """One polite GET. Any timeout, block, or network error returns None. No retries."""
    host = urlparse(url).hostname or "www.amazon.com"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": AMAZON_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": accept_language,
            "Accept-Encoding": "identity",
            "Referer": f"https://{host}/",
        },
        method="GET",
    )
    try:
        with _amazon_opener().open(req, timeout=AMAZON_HTML_TIMEOUT) as resp:
            raw = resp.read(AMAZON_HTML_MAX + 1)
            if len(raw) > AMAZON_HTML_MAX:
                raw = raw[:AMAZON_HTML_MAX]
            if "gzip" in (resp.headers.get("Content-Encoding") or "").lower():
                raw = gzip.decompress(raw)
            charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, "replace")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, gzip.BadGzipFile):
        return None


def fetch_amazon_image(url: str, referer: str | None = None) -> bytes | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not _amazon_image_host_ok(parsed.hostname or ""):
        return None
    headers = {
        "User-Agent": AMAZON_UA,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Encoding": "identity",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )
    try:
        with _amazon_opener().open(req, timeout=AMAZON_IMAGE_TIMEOUT) as resp:
            blob = resp.read(COVER_MAX_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if not blob or len(blob) > COVER_MAX_BYTES:
        return None
    return blob


def lookup_amazon_product(url: str, user_id: int | None) -> dict:
    parsed = amazon_marketplace(url)
    if not parsed:
        return {
            "ok": False,
            "error": "Paste an amazon.ca or amazon.com product link.",
        }
    asin, market = parsed
    host = "www.amazon.ca" if market == "ca" else "www.amazon.com"
    language = "en-CA,en;q=0.9" if market == "ca" else "en-US,en;q=0.9"
    page = f"https://{host}/dp/{asin}"
    html = fetch_amazon_html(page, language)
    info = parse_amazon_html(html or "")
    cover_token = None
    cover_preview = None
    if user_id is not None and not info.get("blocked"):
        candidates: list[str] = []
        for image_url in info.get("image_candidates") or []:
            if image_url and image_url not in candidates:
                candidates.append(image_url)
        if info.get("image_url") and info["image_url"] not in candidates:
            candidates.insert(0, info["image_url"])
        for image_url in candidates[:AMAZON_IMAGE_TRIES]:
            blob = fetch_amazon_image(image_url, referer=page)
            if not blob:
                continue
            saved = save_lookup_cover(user_id, blob)
            if saved:
                cover_token, cover_preview = saved
                break
    messages = [MSG_FILLED if info.get("deal_price") is not None else MSG_NO_PRICE]
    if not cover_token:
        messages.append(MSG_NO_COVER)
    return {
        "ok": True,
        "asin": asin,
        "title": info.get("title"),
        "deal_price": info.get("deal_price"),
        "list_price": info.get("list_price"),
        "cover_token": cover_token,
        "cover_preview": cover_preview,
        "messages": messages,
    }


def _deal_value(deal: sqlite3.Row | dict, key: str, default=None):
    if hasattr(deal, "keys") and key not in deal.keys():
        return default
    try:
        return deal[key]
    except (KeyError, IndexError):
        return default


def parse_contact_fields(src) -> dict:
    title = " ".join((src.get("title") or "").split())
    message = (src.get("message") or "").strip()
    email = normalize_email(src.get("email") or "")
    if not title:
        raise ValueError("Title is required")
    if len(title) > CONTACT_TITLE_MAX:
        raise ValueError("Title is too long")
    if not message:
        raise ValueError("Message is required")
    if len(message) > CONTACT_MESSAGE_MAX:
        raise ValueError("Message is too long")
    if not email:
        raise ValueError("Email is required")
    if len(email) > CONTACT_EMAIL_MAX or not EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address")
    return {"title": title, "message": message, "email": email}


def write_contact_event(row: sqlite3.Row) -> str:
    """Inbox-style JSON under the persist root. Kept out of inbox/ so the deal drain ignores it."""
    event_id = str(uuid.uuid4())
    payload = {
        "id": event_id,
        "action": "contact",
        "local_id": row["id"],
        "title": row["title"],
        "message": row["message"],
        "email": row["email"],
        "created_at": row["created_at"],
        "submitted_at": utc_now(),
    }
    folder = DATA / "contact_events"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{event_id}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return event_id


def save_contact_message(fields: dict) -> int:
    now = utc_now()
    db = get_db()
    cur = db.execute(
        """
        INSERT INTO contact_messages (title, message, email, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (fields["title"], fields["message"], fields["email"], now),
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM contact_messages WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()
    try:
        write_contact_event(row)
    except OSError:
        pass
    return int(cur.lastrowid)


def write_inbox(action: str, deal: sqlite3.Row | dict) -> str:
    event_id = str(uuid.uuid4())
    cover_path = deal["cover_path"]
    cover_url = deal["cover_url"]
    if cover_path and PUBLIC_BASE and not cover_url:
        cover_url = f"{PUBLIC_BASE}/uploads/{cover_path}"
    payload = {
        "id": event_id,
        "action": action,
        "local_id": deal["id"],
        "title": deal["title"],
        "asin": deal["asin"],
        "amazon_url": deal["amazon_url"],
        "deal_price": deal["deal_price"],
        "list_price": deal["list_price"],
        "department": deal["department"],
        "buyer_note": deal["buyer_note"],
        "promo_code": _deal_value(deal, "promo_code"),
        "code_note": _deal_value(deal, "code_note"),
        "submitter": deal["owner_email"],
        "cover_url": cover_url,
        "cover_path": cover_path,
        "cover_file": cover_path,
        "created_at": deal["created_at"] if action != "delete" else utc_now(),
        "submitted_at": utc_now(),
        "notion_hint": deal["notion_hint"],
    }
    out = INBOX / f"{event_id}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return event_id


def get_own_deal(deal_id: int) -> sqlite3.Row:
    """Owner-only fetch (legacy). Prefer get_manageable_deal for edit/delete."""
    db = get_db()
    row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    if row is None:
        abort(404)
    if row["owner_email"] != current_email():
        abort(403)
    return row


def get_manageable_deal(deal_id: int) -> sqlite3.Row:
    db = get_db()
    row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    if row is None:
        abort(404)
    if not can_manage_deal(row):
        abort(403)
    return row


def wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


@app.get("/")
@app.get("/browse")
def home():
    cat = (request.args.get("cat") or "all").strip().lower()
    q = (request.args.get("q") or "").strip()
    sort = (request.args.get("sort") or "latest").strip().lower()
    if sort not in {"latest", "off", "price"}:
        sort = "latest"
    seeded = [
        enrich_deal(d)
        for d in apply_seed_overlay(load_seed_deals())
        if d.get("live") is not False
    ]
    user_cards = [enrich_deal(d) for d in load_user_deal_cards()]
    # User deals first (newest), then seed — stable unique by id
    merged: list[dict] = []
    seen: set[str] = set()
    for item in user_cards + seeded:
        key = str(item.get("id") or "")
        if not key or key in seen:
            continue
        # Public feed: skip cards without a real product image
        if not has_real_image(item):
            continue
        seen.add(key)
        merged.append(item)
    attach_engagement(merged)
    counts = {"all": len(merged)}
    for item in merged:
        key = str(item.get("category") or "")
        counts[key] = counts.get(key, 0) + 1
    needle = q.lower()
    feed = merged
    if cat and cat != "all":
        feed = [d for d in feed if d.get("category") == cat]
    if needle:
        feed = [
            d
            for d in feed
            if needle in str(d.get("title") or "").lower()
            or needle in str(d.get("line") or "").lower()
            or needle in str(d.get("fit") or "").lower()
            or needle in str(d.get("id") or "").lower()
            or needle in str(d.get("detail") or "").lower()
            or needle in str(d.get("promo_code") or "").lower()
        ]
    feed = sort_feed(feed, sort)
    feed = attach_manage_flags(feed)
    featured_list: list[dict] = []
    if not needle:
        if cat and cat != "all":
            pool = feed
        else:
            pool = attach_manage_flags(sort_feed(merged, "latest"))
        featured_list = pick_hot_deals(pool, HOT_LIMIT)
    nav = visible_nav_cats(counts)
    active_cat = cat or "all"
    live_scoped = active_cat != "all"
    # Homepage stays site-wide. A chosen department only shows that department,
    # so an empty category does not keep listing every other post.
    if live_scoped:
        live_source = [d for d in merged if str(d.get("category") or "") == active_cat]
    else:
        live_source = merged
    live_activity = build_live_activity(live_source, 16)
    cat_label = next((c["label"] for c in NAV_CATS if c["id"] == active_cat), active_cat)
    return render_template(
        "browse.html",
        deals=feed,
        nav_cats=nav,
        all_nav_cats=NAV_CATS,
        counts=counts,
        cat=active_cat,
        cat_label=cat_label,
        q=q,
        sort=sort,
        featured=featured_list[0] if featured_list else None,
        featured_list=featured_list,
        live_activity=live_activity,
        live_scoped=live_scoped,
    )


@app.route("/contact", methods=["GET", "POST"])
def contact():
    error = None
    title = ""
    message = ""
    email = current_email()
    if request.method == "POST":
        title = request.form.get("title") or ""
        message = request.form.get("message") or ""
        email = request.form.get("email") or ""
        try:
            fields = parse_contact_fields(request.form)
        except ValueError as exc:
            error = str(exc)
        else:
            save_contact_message(fields)
            flash("Thanks. Your message was sent.")
            return redirect(url_for("contact"))
    return render_template(
        "contact.html",
        error=error,
        title=title,
        message=message,
        email=email,
        title_max=CONTACT_TITLE_MAX,
        message_max=CONTACT_MESSAGE_MAX,
        email_max=CONTACT_EMAIL_MAX,
    )


@app.get("/health")
def health():
    return jsonify(ok=True)


def _request_track_key() -> str:
    body = request.get_json(silent=True) if request.is_json else None
    raw = ""
    if isinstance(body, dict):
        raw = body.get("track_key") or ""
    if not raw:
        raw = request.form.get("track_key") or ""
    return str(raw or "").strip()


@app.post("/api/deals/view")
def api_record_view():
    """Count a deal-detail open. Guests included. Debounced per session and IP."""
    if not same_origin_request():
        return jsonify(ok=False), 400
    deal_key = _request_track_key()
    if not live_deal_for_key(deal_key):
        return jsonify(ok=False), 404
    counted = record_deal_view(deal_key)
    return jsonify(ok=True, counted=counted)


@app.post("/api/deals/favorite")
@login_required
def api_toggle_favorite():
    if not same_origin_request():
        return jsonify(ok=False, error="Could not save that deal."), 400
    deal_key = _request_track_key()
    if not live_deal_for_key(deal_key):
        return jsonify(ok=False, error="That deal was not found."), 404
    favorited = toggle_favorite(deal_key, current_email())
    return jsonify(ok=True, favorited=favorited, favorites=favorite_count(deal_key))


@app.post("/api/amazon-lookup")
@login_required
def api_amazon_lookup():
    """Fill a seller form from one Amazon product URL. Fail soft. No shared cache."""
    if not same_origin_request():
        return jsonify(ok=False, error="Paste an amazon.ca or amazon.com product link."), 400
    body = request.get_json(silent=True) if request.is_json else None
    raw_url = ""
    ignore_raw = None
    seed_raw = None
    if isinstance(body, dict):
        raw_url = body.get("url") or ""
        ignore_raw = body.get("deal_id")
        seed_raw = body.get("seed_id")
    if not raw_url:
        raw_url = request.form.get("url") or ""
        if ignore_raw is None:
            ignore_raw = request.form.get("deal_id")
        if seed_raw is None:
            seed_raw = request.form.get("seed_id")
    result = lookup_amazon_product(str(raw_url), current_user_id())
    ignore_seed = str(seed_raw or "").strip() or None
    if ignore_seed and not is_admin():
        ignore_seed = None
    flag_duplicate_lookup(
        result,
        str(raw_url),
        ignore_deal_id=_manageable_deal_id(ignore_raw),
        ignore_seed_id=ignore_seed,
    )
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("email") and request.method == "GET":
        return redirect(url_for("my_deals"))
    error = None
    if request.method == "POST":
        email = normalize_email(request.form.get("email") or "")
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        if not EMAIL_RE.match(email):
            error = "Enter a valid email address"
        elif len(password) < 8:
            error = "Password must be at least 8 characters"
        elif confirm and password != confirm:
            error = "Passwords do not match"
        else:
            db = get_db()
            try:
                cur = db.execute(
                    "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                    (email, generate_password_hash(password), utc_now()),
                )
                db.commit()
            except sqlite3.IntegrityError:
                error = "An account with that email already exists"
            else:
                session.clear()
                session["user_id"] = cur.lastrowid
                session["email"] = email
                flash("Welcome - you are in. Post a deal anytime from the nav.")
                return redirect(url_for("my_deals"))
    return render_template("register.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("email") and request.method == "GET":
        return redirect(url_for("my_deals"))
    error = None
    if request.method == "POST":
        email = normalize_email(request.form.get("email") or "")
        password = request.form.get("password") or ""
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            error = "Invalid email or password"
        else:
            session.clear()
            session["user_id"] = user["id"]
            session["email"] = user["email"]
            nxt = request.args.get("next") or request.form.get("next") or ""
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("my_deals"))
    return render_template("login.html", error=error)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/post", methods=["GET", "POST"])
@login_required
def post_deal():
    error = None
    if request.method == "POST":
        try:
            fields = parse_deal_fields(request.form)
            ensure_unique_product(fields)
            cover_path = resolve_user_cover(current_user_id())
            if not cover_path:
                raise ValueError("Cover image is required")
        except ValueError as exc:
            error = str(exc)
        else:
            now = utc_now()
            cover_url = public_cover_url(cover_path)
            db = get_db()
            cur = db.execute(
                """
                INSERT INTO deals (
                    owner_email, title, asin, amazon_url, deal_price, list_price,
                    department, buyer_note, promo_code, code_note, cover_path, cover_url,
                    notion_hint, created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    current_email(),
                    fields["title"],
                    fields["asin"],
                    fields["amazon_url"],
                    fields["deal_price"],
                    fields["list_price"],
                    fields["department"],
                    fields["buyer_note"],
                    fields["promo_code"],
                    fields["code_note"],
                    cover_path,
                    cover_url,
                    None,
                    now,
                    now,
                ),
            )
            db.commit()
            deal_id = cur.lastrowid
            row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
            event_id = write_inbox("create", row)
            db.execute("UPDATE deals SET notion_hint = ? WHERE id = ?", (event_id, deal_id))
            db.commit()
            flash("Deal submitted for review.")
            return redirect(url_for("my_deals"))
    return render_template("post.html", error=error, departments=DEPTS)


@app.get("/deals")
@login_required
def my_deals():
    db = get_db()
    if is_admin():
        rows = db.execute(
            """
            SELECT * FROM deals
            WHERE deleted_at IS NULL
            ORDER BY datetime(updated_at) DESC, id DESC
            """
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT * FROM deals
            WHERE owner_email = ? AND deleted_at IS NULL
            ORDER BY datetime(updated_at) DESC, id DESC
            """,
            (current_email(),),
        ).fetchall()
    return render_template(
        "deals.html",
        deals=rows,
        viewing_as_admin=is_admin(),
    )


@app.route("/deals/<int:deal_id>/edit", methods=["GET", "POST"])
@login_required
def edit_deal(deal_id: int):
    deal = get_manageable_deal(deal_id)
    if deal["deleted_at"]:
        abort(404)
    error = None
    if request.method == "POST":
        try:
            fields = parse_deal_fields(request.form)
            ensure_unique_product(fields, ignore_deal_id=deal_id)
            uploaded = save_cover(current_user_id())
            if uploaded:
                cover_path = uploaded
                cover_url = public_cover_url(cover_path)
            else:
                scraped = consume_lookup_cover(current_user_id())
                if scraped:
                    blob, ext = scraped
                    cover_path = store_user_cover_bytes(current_user_id(), blob, ext)
                    cover_url = public_cover_url(cover_path)
                elif cover_removal_requested():
                    cover_path = None
                    cover_url = None
                else:
                    cover_path = deal["cover_path"]
                    cover_url = deal["cover_url"]
        except ValueError as exc:
            error = str(exc)
        else:
            now = utc_now()
            db = get_db()
            # Never change owner_email — even when an admin edits someone else's deal.
            db.execute(
                """
                UPDATE deals SET
                    title=?, asin=?, amazon_url=?, deal_price=?, list_price=?,
                    department=?, buyer_note=?, promo_code=?, code_note=?,
                    cover_path=?, cover_url=?, updated_at=?
                WHERE id=? AND deleted_at IS NULL
                """,
                (
                    fields["title"],
                    fields["asin"],
                    fields["amazon_url"],
                    fields["deal_price"],
                    fields["list_price"],
                    fields["department"],
                    fields["buyer_note"],
                    fields["promo_code"],
                    fields["code_note"],
                    cover_path,
                    cover_url,
                    now,
                    deal_id,
                ),
            )
            db.commit()
            row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
            if row is None or not can_manage_deal(row):
                abort(403)
            write_inbox("update", row)
            flash("Deal updated.")
            return redirect(url_for("my_deals"))
    return render_template(
        "edit.html",
        error=error,
        deal=deal,
        cover_src=deal_cover_src(deal),
        departments=DEPTS,
        editing_as_admin=is_admin() and deal["owner_email"] != current_email(),
    )


@app.post("/deals/<int:deal_id>/delete")
@login_required
def delete_deal(deal_id: int):
    deal = get_manageable_deal(deal_id)
    if deal["deleted_at"]:
        abort(404)
    now = utc_now()
    db = get_db()
    db.execute(
        "UPDATE deals SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
        (now, now, deal_id),
    )
    db.commit()
    row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    write_inbox("delete", row)
    flash("Deal removed.")
    nxt = request.form.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("my_deals"))


def _find_seed_raw(seed_id: str) -> dict | None:
    for deal in load_seed_deals():
        if str(deal.get("id") or "") == seed_id:
            return dict(deal)
    return None


@app.route("/seed/<seed_id>/edit", methods=["GET", "POST"])
@login_required
def edit_seed_deal(seed_id: str):
    if not is_admin():
        abort(403)
    seed_id = (seed_id or "").strip()
    raw = _find_seed_raw(seed_id)
    if raw is None:
        abort(404)
    if seed_id in load_deleted_seed_ids():
        abort(404)
    overrides = load_seed_overrides()
    current = dict(raw)
    if seed_id in overrides:
        current.update(overrides[seed_id])
        current["id"] = seed_id
    error = None
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        if not title:
            error = "Product title is required"
        else:
            try:
                price = parse_price(request.form.get("deal_price"), "Deal price", required=True)
                was = parse_price(request.form.get("list_price"), "List price")
                promo_code = normalize_promo_code(request.form.get("promo_code") or "")
                code_note = normalize_code_note(request.form.get("code_note") or "")
                uploaded_image = save_seed_cover(seed_id)
                if not uploaded_image:
                    scraped = consume_lookup_cover(current_user_id())
                    if scraped:
                        blob, ext = scraped
                        uploaded_image = store_seed_cover_bytes(seed_id, blob, ext)
            except ValueError as exc:
                error = str(exc)
            else:
                if uploaded_image:
                    image = uploaded_image
                elif cover_removal_requested():
                    image = ""
                else:
                    image = current.get("image") or ""
                patch = {
                    "title": title[:200],
                    "line": (request.form.get("line") or "").strip(),
                    "fit": (request.form.get("fit") or "").strip(),
                    "detail": (request.form.get("buyer_note") or request.form.get("detail") or "").strip(),
                    "price": price,
                    "was": was if was is not None else current.get("was"),
                    "href": (request.form.get("amazon_url") or current.get("href") or "").strip(),
                    "category": (request.form.get("category") or current.get("category") or "").strip().lower(),
                    "image": image,
                    "asOf": (request.form.get("as_of") or current.get("asOf") or "").strip(),
                    "promo_code": promo_code or "",
                    "code_note": code_note or "",
                    "live": True,
                }
                overrides[seed_id] = {**(overrides.get(seed_id) or {}), **{k: v for k, v in patch.items() if v is not None}}
                save_seed_overrides(overrides)
                flash("Seed deal updated.")
                return redirect(url_for("home"))
    # Shape for edit.html reuse
    deal_view = {
        "id": seed_id,
        "title": current.get("title") or "",
        "asin": seed_id if len(seed_id) == 10 else "",
        "amazon_url": current.get("href") or "",
        "deal_price": current.get("price") or "",
        "list_price": current.get("was"),
        "department": None,
        "buyer_note": current.get("detail") or current.get("line") or "",
        "cover_path": None,
        "cover_url": current.get("image") or "",
        "owner_email": "(seed)",
        "line": current.get("line") or "",
        "fit": current.get("fit") or "",
        "category": current.get("category") or "",
        "asOf": current.get("asOf") or "",
        "promo_code": current.get("promo_code") or "",
        "code_note": current.get("code_note") or "",
    }
    return render_template(
        "edit_seed.html",
        error=error,
        deal=deal_view,
        departments=DEPTS,
        nav_cats=NAV_CATS,
    )


@app.post("/seed/<seed_id>/delete")
@login_required
def delete_seed_deal(seed_id: str):
    if not is_admin():
        abort(403)
    seed_id = (seed_id or "").strip()
    if not seed_id or _find_seed_raw(seed_id) is None:
        abort(404)
    deleted = load_deleted_seed_ids()
    deleted.add(seed_id)
    save_deleted_seed_ids(deleted)
    flash("Seed deal removed from the feed.")
    nxt = request.form.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("home"))


@app.get("/uploads/<path:name>")
def uploaded(name: str):
    return send_from_directory(UPLOADS, name)




def _inbox_key_ok() -> bool:
    if not INBOX_KEY:
        return False
    provided = (request.headers.get("X-HD-Inbox-Key") or request.args.get("key") or "").strip()
    if not provided or len(provided) != len(INBOX_KEY):
        return False
    return secrets.compare_digest(provided, INBOX_KEY)


def _list_inbox_files() -> list[Path]:
    files = []
    for path in sorted(INBOX.glob("*.json")):
        if path.name.startswith("."):
            continue
        if path.name == ".keep":
            continue
        files.append(path)
    return files


@app.get("/api/inbox/pending")
def api_inbox_pending():
    """Pull inbox JSON events for Notion drain. Auth via HD_INBOX_KEY."""
    if not INBOX_KEY:
        return jsonify(error="HD_INBOX_KEY is not configured"), 503
    if not _inbox_key_ok():
        return jsonify(error="Unauthorized"), 401
    consume = (request.args.get("consume") or "").strip().lower() in {"1", "true", "yes"}
    events = []
    consumed = []
    for path in _list_inbox_files():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            events.append({"id": path.stem, "error": f"unreadable: {exc}", "path": path.name})
            continue
        if not isinstance(payload, dict):
            payload = {"id": path.stem, "raw": payload}
        payload.setdefault("id", path.stem)
        events.append(payload)
        if consume:
            try:
                path.unlink(missing_ok=True)
                consumed.append(path.stem)
            except OSError:
                pass
    return jsonify(ok=True, count=len(events), consumed=consumed if consume else [], events=events)


@app.post("/api/inbox/ack")
def api_inbox_ack():
    """Delete inbox files by id after Notion write."""
    if not INBOX_KEY:
        return jsonify(error="HD_INBOX_KEY is not configured"), 503
    if not _inbox_key_ok():
        return jsonify(error="Unauthorized"), 401
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or request.args.getlist("id")
    if isinstance(ids, str):
        ids = [ids]
    if not isinstance(ids, list):
        return jsonify(error="ids must be a list"), 400
    deleted = []
    missing = []
    for raw in ids:
        eid = str(raw or "").strip()
        if not eid or "/" in eid or "\\" in eid or ".." in eid:
            continue
        path = INBOX / f"{eid}.json"
        if not path.is_file():
            missing.append(eid)
            continue
        try:
            path.unlink()
            deleted.append(eid)
        except OSError:
            missing.append(eid)
    return jsonify(ok=True, deleted=deleted, missing=missing)

@app.post("/api/submit")
@login_required
def api_submit():
    src = request.get_json(silent=True) if request.is_json else request.form
    if src is None:
        return jsonify(error="Expected form data or JSON"), 400
    try:
        fields = parse_deal_fields(src)
        ensure_unique_product(fields)
        cover_path = None if request.is_json else resolve_user_cover(current_user_id())
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    now = utc_now()
    cover_url = public_cover_url(cover_path)
    db = get_db()
    cur = db.execute(
        """
        INSERT INTO deals (
            owner_email, title, asin, amazon_url, deal_price, list_price,
            department, buyer_note, promo_code, code_note, cover_path, cover_url,
            notion_hint, created_at, updated_at, deleted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            current_email(),
            fields["title"],
            fields["asin"],
            fields["amazon_url"],
            fields["deal_price"],
            fields["list_price"],
            fields["department"],
            fields["buyer_note"],
            fields["promo_code"],
            fields["code_note"],
            cover_path,
            cover_url,
            None,
            now,
            now,
        ),
    )
    db.commit()
    deal_id = cur.lastrowid
    row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    event_id = write_inbox("create", row)
    db.execute("UPDATE deals SET notion_hint = ? WHERE id = ?", (event_id, deal_id))
    db.commit()
    return jsonify(ok=True, id=event_id, local_id=deal_id)


@app.errorhandler(413)
def request_too_large(_e):
    message = (
        "That upload is too large. Cover images must be a JPG, PNG, WEBP, or GIF under 8MB. "
        "Go back, choose a smaller file, and submit again."
    )
    if wants_json():
        return jsonify(error=message), 413
    return render_template("error.html", code="Upload too large", message=message), 413


@app.errorhandler(403)
def forbidden(_e):
    if wants_json():
        return jsonify(error="You cannot modify another person's deal"), 403
    return render_template("error.html", code=403, message="You cannot modify another person's deal."), 403


@app.errorhandler(404)
def not_found(_e):
    if wants_json():
        return jsonify(error="Not found"), 404
    return render_template("error.html", code=404, message="That deal was not found."), 404


if __name__ == "__main__":
    print(
        f"[hd-auth] listening on http://{HOST}:{PORT} public_base={PUBLIC_BASE or '(unset)'}",
        flush=True,
    )
    app.run(host=HOST, port=PORT, threaded=True)
