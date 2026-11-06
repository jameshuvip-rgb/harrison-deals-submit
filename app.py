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
COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

for d in (DATA, INBOX, UPLOADS):
    d.mkdir(parents=True, exist_ok=True)

SEED_DEALS_PATH = ROOT / "deals.json"

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


def off_pct(price: float, was: float) -> int:
    if was <= 0 or price >= was:
        return 0
    return round(((was - price) / was) * 100)


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
    out["amazon_url"] = amazon_href(deal)
    out["initials"] = deal_initials(deal.get("title") or "")
    if deal.get("promo") == "bxgy":
        out["badge"] = f"BXGY ~{pct or 30}%"
    elif pct:
        out["badge"] = f"{pct}% OFF"
    else:
        out["badge"] = ""
    return out




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
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = PUBLIC_BASE.startswith("https://")


@app.context_processor
def inject_globals():
    return {"landing_url": LANDING_URL}


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
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_deals_owner ON deals(owner_email);
        """
    )
    db.commit()
    db.close()


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
    cover_url = (src.get("cover_url") or "").strip() or None

    return {
        "title": title,
        "asin": asin or None,
        "amazon_url": amazon_url,
        "deal_price": deal_price,
        "list_price": list_price,
        "department": department or None,
        "buyer_note": buyer_note,
        "cover_url": cover_url,
    }


def save_cover(user_id: int):
    f = request.files.get("cover")
    if not f or not f.filename:
        return None
    blob = f.read()
    if len(blob) > 8_000_000:
        raise ValueError("Cover image too large (max 8MB)")
    ext = Path(f.filename).suffix.lower() or ".jpg"
    if ext not in COVER_EXTS:
        ext = ".jpg"
    folder = UPLOADS / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time())}_{secrets.token_hex(4)}{ext}"
    (folder / name).write_bytes(blob)
    return f"{user_id}/{name}"


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
    db = get_db()
    row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    if row is None:
        abort(404)
    if row["owner_email"] != current_email():
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
    seeded = [enrich_deal(d) for d in load_seed_deals() if d.get("live") is not False]
    counts = {"all": len(seeded)}
    for item in seeded:
        key = str(item.get("category") or "")
        counts[key] = counts.get(key, 0) + 1
    needle = q.lower()
    feed = seeded
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
        ]
    feed = sorted(feed, key=lambda d: int(d.get("posted") or 0), reverse=True)
    featured = None
    if not needle and (not cat or cat == "all"):
        featured = next((d for d in seeded if d.get("featured")), seeded[0] if seeded else None)
    elif not needle:
        featured = next((d for d in feed if d.get("featured")), None)
    return render_template(
        "browse.html",
        deals=feed,
        nav_cats=NAV_CATS,
        counts=counts,
        cat=cat or "all",
        q=q,
        featured=featured,
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
        except ValueError as exc:
            error = str(exc)
        else:
            now = utc_now()
            cover_url = fields["cover_url"]
            if cover_path and PUBLIC_BASE:
                cover_url = f"{PUBLIC_BASE}/uploads/{cover_path}"
            db = get_db()
            cur = db.execute(
                """
                INSERT INTO deals (
                    owner_email, title, asin, amazon_url, deal_price, list_price,
                    department, buyer_note, cover_path, cover_url, notion_hint,
                    created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
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
    rows = db.execute(
        """
        SELECT * FROM deals
        WHERE owner_email = ? AND deleted_at IS NULL
        ORDER BY datetime(updated_at) DESC, id DESC
        """,
        (current_email(),),
    ).fetchall()
    return render_template("deals.html", deals=rows)


@app.route("/deals/<int:deal_id>/edit", methods=["GET", "POST"])
@login_required
def edit_deal(deal_id: int):
    deal = get_own_deal(deal_id)
    if deal["deleted_at"]:
        abort(404)
    error = None
    if request.method == "POST":
        try:
            fields = parse_deal_fields(request.form)
            cover_path = save_cover(current_user_id()) or deal["cover_path"]
        except ValueError as exc:
            error = str(exc)
        else:
            cover_url = fields["cover_url"] if fields["cover_url"] is not None else deal["cover_url"]
            if request.files.get("cover") and request.files.get("cover").filename and PUBLIC_BASE:
                cover_url = f"{PUBLIC_BASE}/uploads/{cover_path}"
            now = utc_now()
            db = get_db()
            db.execute(
                """
                UPDATE deals SET
                    title=?, asin=?, amazon_url=?, deal_price=?, list_price=?,
                    department=?, buyer_note=?, cover_path=?, cover_url=?, updated_at=?
                WHERE id=? AND owner_email=? AND deleted_at IS NULL
                """,
                (
                    fields["title"],
                    fields["asin"],
                    fields["amazon_url"],
                    fields["deal_price"],
                    fields["list_price"],
                    fields["department"],
                    fields["buyer_note"],
                    cover_path,
                    cover_url,
                    now,
                    deal_id,
                    current_email(),
                ),
            )
            db.commit()
            row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
            if row is None or row["owner_email"] != current_email():
                abort(403)
            write_inbox("update", row)
            flash("Deal updated.")
            return redirect(url_for("my_deals"))
    return render_template("edit.html", error=error, deal=deal, departments=DEPTS)


@app.post("/deals/<int:deal_id>/delete")
@login_required
def delete_deal(deal_id: int):
    deal = get_own_deal(deal_id)
    if deal["deleted_at"]:
        abort(404)
    now = utc_now()
    db = get_db()
    db.execute(
        "UPDATE deals SET deleted_at = ?, updated_at = ? WHERE id = ? AND owner_email = ?",
        (now, now, deal_id, current_email()),
    )
    db.commit()
    row = db.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    write_inbox("delete", row)
    flash("Deal removed from your list.")
    return redirect(url_for("my_deals"))


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
        cover_path = save_cover(current_user_id()) if not request.is_json else None
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    now = utc_now()
    cover_url = fields["cover_url"]
    if cover_path and PUBLIC_BASE:
        cover_url = f"{PUBLIC_BASE}/uploads/{cover_path}"
    db = get_db()
    cur = db.execute(
        """
        INSERT INTO deals (
            owner_email, title, asin, amazon_url, deal_price, list_price,
            department, buyer_note, cover_path, cover_url, notion_hint,
            created_at, updated_at, deleted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
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
