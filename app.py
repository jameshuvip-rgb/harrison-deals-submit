#!/usr/bin/env python3
"""Harrison Deals submit app with email/password auth and owner-only edit."""
from __future__ import annotations

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
DATA = ROOT / "data"
INBOX = ROOT / "inbox"
UPLOADS = ROOT / "uploads"
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
COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# 8 MiB image cap. The request cap is a little higher so the multipart
# form around an 8MB file is not rejected before we can save it or show
# an on-page error.
COVER_MAX_BYTES = 8 * 1024 * 1024
REQUEST_MAX_BYTES = COVER_MAX_BYTES + 512 * 1024

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
    return out


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


def pick_featured(feed: list[dict], limit: int = 3) -> list[dict]:
    with_img = [d for d in feed if has_real_image(d)]
    featured = [d for d in with_img if d.get("featured")]
    if len(featured) >= limit:
        return featured[:limit]
    seen = {str(d.get("id")) for d in featured}
    for d in with_img:
        if str(d.get("id")) in seen:
            continue
        featured.append(d)
        seen.add(str(d.get("id")))
        if len(featured) >= limit:
            break
    return featured[:limit]


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
                "promo_code": str(deal.get("promo_code") or "").strip(),
                "code_note": str(deal.get("code_note") or "").strip(),
                "rel_time": relative_time_label(deal),
                "event": event,
                "is_new": is_new,
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


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
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
    db = sqlite3.connect(DB_PATH)
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


def save_cover(user_id: int):
    parsed = read_cover_upload()
    if parsed is None:
        return None
    blob, ext = parsed
    folder = UPLOADS / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time())}_{secrets.token_hex(4)}{ext}"
    (folder / name).write_bytes(blob)
    return f"{user_id}/{name}"


def save_seed_cover(seed_id: str) -> str | None:
    """Store a replacement seed cover and return the path written into the seed override."""
    parsed = read_cover_upload()
    if parsed is None:
        return None
    blob, ext = parsed
    safe = re.sub(r"[^A-Za-z0-9_-]", "", seed_id)[:40] or "seed"
    folder = UPLOADS / "seed" / safe
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time())}_{secrets.token_hex(4)}{ext}"
    (folder / name).write_bytes(blob)
    return f"/uploads/seed/{safe}/{name}"


def _deal_value(deal: sqlite3.Row | dict, key: str, default=None):
    if hasattr(deal, "keys") and key not in deal.keys():
        return default
    try:
        return deal[key]
    except (KeyError, IndexError):
        return default


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
        featured_list = pick_featured(pool, 3)
    nav = visible_nav_cats(counts)
    live_activity = build_live_activity(merged, 16)
    return render_template(
        "browse.html",
        deals=feed,
        nav_cats=nav,
        all_nav_cats=NAV_CATS,
        counts=counts,
        cat=cat or "all",
        q=q,
        sort=sort,
        featured=featured_list[0] if featured_list else None,
        featured_list=featured_list,
        live_activity=live_activity,
    )


@app.get("/health")
def health():
    return jsonify(ok=True)


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
            cover_path = save_cover(current_user_id())
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
            uploaded = save_cover(current_user_id())
            if uploaded:
                cover_path = uploaded
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
        cover_path = None if request.is_json else save_cover(current_user_id())
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
