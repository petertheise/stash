"""Stash: the family's home inventory. Flask + SQLite, plain templates, no build step.

Items live in nested locations (house > garage > shelf 2), carry labels, photos, receipts,
warranty and purchase facts, custom fields and a maintenance log. Modeled on Homebox.
"""
import csv
import io
import ipaddress
import json
import logging
import os
import queue
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, send_file, send_from_directory, url_for)
from werkzeug.utils import secure_filename

import ai
import recalls

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("STASH_PORT", "5189"))
HOST = os.environ.get("STASH_HOST", "127.0.0.1")   # loopback; reach it via tailscale serve (security review 2026-10-07)
DB_PATH = os.environ.get("STASH_DB", os.path.join(HERE, "stash.db"))
UPLOADS = os.environ.get("STASH_UPLOADS", os.path.join(HERE, "uploads"))
THUMBS = os.path.join(UPLOADS, "thumbs")
MAX_UPLOAD = 25 * 1024 * 1024
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "heic"}
DOC_EXT = {"pdf", "txt", "csv", "doc", "docx", "xls", "xlsx", "numbers", "pages", "rtf", "md"}
ALLOWED_EXT = IMAGE_EXT | DOC_EXT
LABEL_COLORS = ["#2563eb", "#16a34a", "#d97706", "#dc2626", "#7c3aed", "#0891b2", "#db2777", "#4b5563"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stash")

try:
    from PIL import Image, ImageOps
    HAVE_PIL = True
except ImportError:  # thumbnails degrade to the original file
    HAVE_PIL = False

try:  # iPhone photos: HEIC is converted to JPEG on upload so every browser can show it
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HAVE_HEIF = True
except ImportError:
    HAVE_HEIF = False

ORPHANS = os.environ.get("STASH_ORPHANS", os.path.join(HERE, "orphans"))
ORPHAN_GRACE = 3600          # never touch a file younger than this (an upload may be mid-request)
ORPHAN_KEEP_DAYS = 30        # quarantined orphans are purged after this

app = Flask(__name__, root_path=HERE, instance_path=os.path.join(HERE, "instance"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD
app.config["SECRET_KEY"] = os.environ.get("STASH_SECRET", "stash-local-" + str(uuid.getnode()))
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'none'")

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT '#2563eb'
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL DEFAULT 1,
    location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    serial TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    manufacturer TEXT NOT NULL DEFAULT '',
    purchase_from TEXT NOT NULL DEFAULT '',
    purchase_price REAL,
    purchase_date TEXT,
    warranty_until TEXT,
    lifetime_warranty INTEGER NOT NULL DEFAULT 0,
    warranty_notes TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    fields TEXT NOT NULL DEFAULT '{}',
    archived INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_items_location ON items(location_id);
CREATE INDEX IF NOT EXISTS ix_items_updated ON items(updated DESC);
CREATE TABLE IF NOT EXISTS item_labels (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, label_id)
);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'photo',
    filename TEXT NOT NULL,
    original_name TEXT NOT NULL,
    mime TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    is_primary INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attachments_item ON attachments(item_id);
CREATE TABLE IF NOT EXISTS maintenance (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    cost REAL,
    scheduled TEXT,
    completed TEXT,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_maintenance_item ON maintenance(item_id);
CREATE TABLE IF NOT EXISTS ebay (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    data TEXT NOT NULL DEFAULT '{}',
    checked REAL NOT NULL
);
"""

# Columns added after launch; init_db adds any that are missing (additive only).
ITEM_COLUMNS = [
    ("replacement_cost", "REAL"),
    ("replacement_product", "TEXT NOT NULL DEFAULT ''"),
    ("replacement_url", "TEXT NOT NULL DEFAULT ''"),
    ("replacement_note", "TEXT NOT NULL DEFAULT ''"),
    ("replacement_checked", "REAL"),
    ("stock_off", "INTEGER NOT NULL DEFAULT 0"),  # set when someone removes a stock photo: do not refill
]
ATTACHMENT_COLUMNS = [
    ("source", "TEXT NOT NULL DEFAULT ''"),       # stock photos: where the picture came from
]
EBAY_LABEL = "eBay"


# ---------- database ----------

def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    os.makedirs(THUMBS, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    conn.executescript(recalls.SCHEMA)
    for table, cols in (("items", ITEM_COLUMNS), ("attachments", ATTACHMENT_COLUMNS)):
        have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        for col, decl in cols:
            if col not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
                log.info("added %s.%s", table, col)
    repair_location_cycles(conn)
    conn.commit()
    conn.close()


def repair_location_cycles(conn):
    """Detach any location whose parent chain loops back on itself (it would vanish from the tree)."""
    parents = dict(conn.execute("SELECT id, parent_id FROM locations").fetchall())
    for start in parents:
        seen, cur = set(), start
        while cur is not None and cur in parents:
            if cur in seen:  # cur is on the loop itself; cutting its parent breaks it
                conn.execute("UPDATE locations SET parent_id = NULL WHERE id = ?", (cur,))
                parents[cur] = None
                log.warning("location %s was in a parent cycle; moved to the top level", cur)
                break
            seen.add(cur)
            cur = parents[cur]


ALLOWED_HOSTS = {"127.0.0.1", "localhost"}  # add LAN and tailnet names with STASH_ALLOWED_HOSTS
ALLOWED_HOSTS |= {h.strip().lower() for h in os.environ.get("STASH_ALLOWED_HOSTS", "").split(",") if h.strip()}


@app.before_request
def _request_guard():
    """Host allowlist (DNS rebinding) and same-origin writes (cross-site form posts). Security review 2026-10-07."""
    host = (request.host or "").split(":")[0].lower()
    if host and host not in ALLOWED_HOSTS:
        abort(421)
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
        ohost = origin.split("//", 1)[-1].split("/", 1)[0].split(":")[0].lower()
        if not ohost or ohost not in ALLOWED_HOSTS:
            abort(403)


def safe_next(default):
    """Only same-site relative paths come back from `next`/referrer (open-redirect guard)."""
    nxt = request.form.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt
    return default


@app.after_request
def headers(resp):
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


# ---------- helpers ----------

def _str(value, limit=500):
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _int(value, default=None, lo=None):
    if value is None or str(value).strip() == "":
        return default
    try:
        n = int(str(value).strip())
    except ValueError:
        return default
    if lo is not None and n < lo:
        n = lo
    return n


def _money(value):
    if value is None:
        return None
    s = str(value).strip().replace("$", "").replace(",", "")
    if s == "":
        return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _date(value):
    """Accept YYYY-MM-DD (native date inputs) or MM/DD/YYYY; store ISO or NULL."""
    s = _str(value, 20)
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _fields_from_form(form):
    keys = form.getlist("field_key")
    vals = form.getlist("field_value")
    out = {}
    for k, v in zip(keys, vals):
        k = _str(k, 60)
        v = _str(v, 500)
        if k:
            out[k] = v
    return out


def all_locations():
    rows = get_db().execute("SELECT * FROM locations ORDER BY name COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


def location_index():
    return {loc["id"]: loc for loc in all_locations()}


def location_path(loc_id, index=None):
    """Ancestors first, the location itself last."""
    index = index if index is not None else location_index()
    path = []
    seen = set()
    while loc_id and loc_id in index and loc_id not in seen:
        seen.add(loc_id)
        loc = index[loc_id]
        path.append(loc)
        loc_id = loc["parent_id"]
    path.reverse()
    return path


def location_path_text(loc_id, index=None):
    return " / ".join(l["name"] for l in location_path(loc_id, index))


def descendant_ids(loc_id, index=None):
    index = index if index is not None else location_index()
    children = {}
    for loc in index.values():
        children.setdefault(loc["parent_id"], []).append(loc["id"])
    out, stack = [], [loc_id]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.append(cur)
        stack.extend(children.get(cur, []))
    return out


def location_tree(counts=None):
    """Nested list of {loc, children, count} sorted by name."""
    index = location_index()
    children = {}
    for loc in index.values():
        children.setdefault(loc["parent_id"], []).append(loc)
    counts = counts or {}

    def build(parent_id, depth=0):
        nodes = []
        for loc in sorted(children.get(parent_id, []), key=lambda l: l["name"].lower()):
            if depth > 20:
                break
            nodes.append({"loc": loc, "children": build(loc["id"], depth + 1),
                          "count": counts.get(loc["id"], 0)})
        return nodes

    return build(None)


def flat_location_choices():
    """[(id, 'Home / Garage / Shelf 2'), ...] for select boxes, depth-first."""
    out = []

    def walk(nodes, prefix):
        for n in nodes:
            text = (prefix + " / " if prefix else "") + n["loc"]["name"]
            out.append((n["loc"]["id"], text, len(prefix.split(" / ")) if prefix else 0))
            walk(n["children"], text)

    walk(location_tree(), "")
    return out


def item_counts_by_location():
    rows = get_db().execute(
        "SELECT location_id, COUNT(*) AS n FROM items WHERE archived = 0 GROUP BY location_id").fetchall()
    return {r["location_id"]: r["n"] for r in rows}


def labels_for_items(item_ids):
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    rows = get_db().execute(
        "SELECT il.item_id, l.id, l.name, l.color FROM item_labels il JOIN labels l ON l.id = il.label_id "
        "WHERE il.item_id IN (%s) ORDER BY l.name COLLATE NOCASE" % marks, item_ids).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["item_id"], []).append({"id": r["id"], "name": r["name"], "color": r["color"]})
    return out


def primary_photos(item_ids):
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    rows = get_db().execute(
        "SELECT id, item_id, kind FROM attachments WHERE kind IN ('photo', 'stock') AND item_id IN (%s) "
        "ORDER BY kind = 'photo' DESC, is_primary DESC, created ASC" % marks, item_ids).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["item_id"], (r["id"], r["kind"] == "stock"))
    return out


def decorate_items(rows):
    items = [dict(r) for r in rows]
    ids = [i["id"] for i in items]
    labels = labels_for_items(ids)
    photos = primary_photos(ids)
    index = location_index()
    for it in items:
        it["labels"] = labels.get(it["id"], [])
        it["photo_id"], it["photo_stock"] = photos.get(it["id"], (None, False))
        it["location_text"] = location_path_text(it["location_id"], index) if it["location_id"] else ""
    return items


def get_item_or_404(item_id):
    row = get_db().execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def days_until(iso):
    if not iso:
        return None
    try:
        return (datetime.strptime(iso, "%Y-%m-%d").date() - date.today()).days
    except ValueError:
        return None


def ext_of(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def save_upload(item_id, fs, kind):
    """Store one uploaded file under a random name; return attachment id or None."""
    original = fs.filename or ""
    ext = ext_of(original)
    if not original or ext not in ALLOWED_EXT:
        return None
    stored = "%s.%s" % (uuid.uuid4().hex, ext)
    fs.save(os.path.join(UPLOADS, stored))
    return _register_file(item_id, stored, original, kind, fs.mimetype or "")


def attach_bytes(item_id, data, original, kind, mime=""):
    """Same as an upload, for files Stash fetched or staged itself (manuals, walkthrough photos)."""
    ext = ext_of(original)
    if ext not in ALLOWED_EXT:
        return None
    stored = "%s.%s" % (uuid.uuid4().hex, ext)
    with open(os.path.join(UPLOADS, stored), "wb") as fh:
        fh.write(data)
    return _register_file(item_id, stored, original, kind, mime)


def _register_file(item_id, stored, original, kind, mime):
    """Validate and clean a file already written to UPLOADS, then add its attachments row."""
    path = os.path.join(UPLOADS, stored)
    ext = ext_of(stored)
    size = os.path.getsize(path)
    if size == 0:
        os.remove(path)
        return None
    if kind == "photo" and ext not in IMAGE_EXT:
        kind = "document"
    if ext in IMAGE_EXT:  # photos, and receipts or manuals that are pictures
        if not image_is_valid(path, ext):
            os.remove(path)
            log.info("rejected %s: not a readable %s image", secure_filename(original), ext)
            return None
        if ext == "heic" and HAVE_HEIF:
            stored, path = heic_to_jpeg(path, stored)
            if stored.endswith(".jpg"):
                original, mime = original.rsplit(".", 1)[0] + ".jpg", "image/jpeg"
        else:
            strip_exif(path, ext)
        size = os.path.getsize(path)
        if kind in ("photo", "stock"):
            make_thumb(path, stored)
    elif ext == "pdf":
        with open(path, "rb") as fh:
            if fh.read(5) != b"%PDF-":
                os.remove(path)
                log.info("rejected %s: not a PDF", secure_filename(original))
                return None
    db = get_db()
    has_primary = db.execute(
        "SELECT 1 FROM attachments WHERE item_id = ? AND kind = 'photo' AND is_primary = 1", (item_id,)).fetchone()
    cur = db.execute(
        "INSERT INTO attachments (item_id, kind, filename, original_name, mime, size, is_primary, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, kind, stored, secure_filename(original) or stored, mime, size,
         1 if (kind == "photo" and not has_primary) else 0, time.time()))
    return cur.lastrowid


def image_is_valid(path, ext):
    """True when Pillow can parse the file as an image. HEIC passes untested if pillow-heif is missing."""
    if not HAVE_PIL or (ext == "heic" and not HAVE_HEIF):
        return True
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


def heic_to_jpeg(path, stored):
    """Re-encode an iPhone HEIC as a metadata-free JPEG; returns (stored, path) of the new file."""
    new_stored = stored.rsplit(".", 1)[0] + ".jpg"
    new_path = os.path.join(UPLOADS, new_stored)
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(new_path, "JPEG", quality=90)
    except Exception as exc:
        log.info("heic conversion failed for %s: %s", stored, exc)
        return stored, path
    os.remove(path)
    return new_stored, new_path


def strip_exif(path, ext):
    """Re-save photos without metadata: originals carry GPS of the house (security review 2026-10-07)."""
    if not HAVE_PIL or ext in ("gif", "heic"):
        return
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            fmt = "PNG" if ext == "png" else ("WEBP" if ext == "webp" else "JPEG")
            if fmt == "JPEG" and im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(path, fmt, quality=92) if fmt != "PNG" else im.save(path, fmt)
    except Exception as exc:
        log.info("exif strip skipped for %s: %s", os.path.basename(path), exc)


def make_thumb(path, stored):
    if not HAVE_PIL:
        return
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((480, 480))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(os.path.join(THUMBS, stored.rsplit(".", 1)[0] + ".jpg"), "JPEG", quality=82)
    except Exception as exc:  # HEIC without a plugin, corrupt file, and so on
        log.info("no thumbnail for %s: %s", stored, exc)


def thumb_name(stored):
    return stored.rsplit(".", 1)[0] + ".jpg"


def delete_attachment_files(rows):
    """Remove files for attachment rows. Call only after the DB delete has committed."""
    for row in rows:
        for p in (os.path.join(UPLOADS, row["filename"]), os.path.join(THUMBS, thumb_name(row["filename"]))):
            try:
                os.remove(p)
            except OSError:
                pass


def sweep_orphans(now=None):
    """Move upload files no attachment row points at into ORPHANS; purge quarantine after 30 days.

    Covers uploads saved by a request that failed before commit, and thumbs of deleted photos.
    Returns (moved, purged).
    """
    now = now or time.time()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    known = {r[0] for r in conn.execute("SELECT filename FROM attachments")}
    conn.close()
    known_thumbs = {thumb_name(f) for f in known}
    os.makedirs(ORPHANS, exist_ok=True)
    moved = purged = 0
    for folder, keep, prefix in ((UPLOADS, known, ""), (THUMBS, known_thumbs, "thumb-")):
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if not os.path.isfile(path) or name.startswith(".") or name in keep:
                continue
            if now - os.path.getmtime(path) < ORPHAN_GRACE:
                continue
            dest = os.path.join(ORPHANS, prefix + name)
            os.replace(path, dest)
            os.utime(dest, (now, now))  # the 30-day clock starts at quarantine, not at upload
            moved += 1
    staging = os.path.join(UPLOADS, "staging")  # walkthroughs started and never finished
    if os.path.isdir(staging):
        for name in os.listdir(staging):
            path = os.path.join(staging, name)
            if os.path.isdir(path) and now - os.path.getmtime(path) > 86400:
                shutil.rmtree(path, ignore_errors=True)
                purged += 1
    for name in os.listdir(ORPHANS):
        path = os.path.join(ORPHANS, name)
        if os.path.isfile(path) and now - os.path.getmtime(path) > ORPHAN_KEEP_DAYS * 86400:
            os.remove(path)
            purged += 1
    return moved, purged


# ---------- template filters ----------

@app.template_filter("money")
def money_filter(v):
    if v is None:
        return ""
    return "${:,.2f}".format(v)


@app.template_filter("nice_date")
def nice_date(iso):
    if not iso:
        return ""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %d, %Y")
    except ValueError:
        return iso


@app.template_filter("ago")
def ago(ts):
    if not ts:
        return ""
    d = datetime.fromtimestamp(ts)
    return d.strftime("%b %d, %Y")


@app.context_processor
def inject_globals():
    return {"days_until": days_until, "today": date.today().isoformat(), "have_pil": HAVE_PIL,
            "ai_ready": ai.available()}


# ---------- pages ----------

@app.route("/")
def index():
    db = get_db()
    counts = {
        "total": db.execute("SELECT COALESCE(SUM(quantity), 0) FROM items WHERE archived = 0").fetchone()[0],
        "records": db.execute("SELECT COUNT(*) FROM items WHERE archived = 0").fetchone()[0],
        "locations": db.execute("SELECT COUNT(*) FROM locations").fetchone()[0],
        "labels": db.execute("SELECT COUNT(*) FROM labels").fetchone()[0],
        "value": db.execute(
            "SELECT COALESCE(SUM(purchase_price * quantity), 0) FROM items WHERE archived = 0").fetchone()[0],
    }
    recent = decorate_items(db.execute(
        "SELECT * FROM items WHERE archived = 0 ORDER BY updated DESC LIMIT 8").fetchall())
    horizon = (date.today() + timedelta(days=90)).isoformat()
    warranties = decorate_items(db.execute(
        "SELECT * FROM items WHERE archived = 0 AND lifetime_warranty = 0 AND warranty_until IS NOT NULL "
        "AND warranty_until <= ? ORDER BY warranty_until ASC LIMIT 10", (horizon,)).fetchall())
    due = db.execute(
        "SELECT m.*, i.name AS item_name FROM maintenance m JOIN items i ON i.id = m.item_id "
        "WHERE m.completed IS NULL AND m.scheduled IS NOT NULL AND m.scheduled <= ? "
        "ORDER BY m.scheduled ASC LIMIT 10", (horizon,)).fetchall()
    tree = location_tree(item_counts_by_location())
    labels = db.execute(
        "SELECT l.*, (SELECT COUNT(*) FROM item_labels il JOIN items i ON i.id = il.item_id "
        "WHERE il.label_id = l.id AND i.archived = 0) AS n FROM labels l ORDER BY n DESC, l.name COLLATE NOCASE "
        "LIMIT 12").fetchall()
    return render_template("index.html", counts=counts, recent=recent, warranties=warranties, due=due,
                           loc_tree=tree, labels=labels, recall_hits=recalls.open_hits(db))


@app.route("/items")
def items_page():
    db = get_db()
    q = _str(request.args.get("q"), 120)
    loc = _int(request.args.get("location"))
    label = _int(request.args.get("label"))
    sort = request.args.get("sort", "updated")
    archived = request.args.get("archived") == "1"
    where = ["i.archived = ?"]
    params = [1 if archived else 0]
    if q:
        like = "%" + q + "%"
        where.append("(i.name LIKE ? OR i.description LIKE ? OR i.serial LIKE ? OR i.model LIKE ? "
                     "OR i.manufacturer LIKE ? OR i.notes LIKE ? OR i.fields LIKE ?)")
        params += [like] * 7
    if loc:
        ids = descendant_ids(loc)
        where.append("i.location_id IN (%s)" % ",".join("?" * len(ids)))
        params += ids
    if label:
        where.append("i.id IN (SELECT item_id FROM item_labels WHERE label_id = ?)")
        params.append(label)
    order = {"updated": "i.updated DESC", "name": "i.name COLLATE NOCASE ASC", "created": "i.created DESC",
             "price": "i.purchase_price DESC NULLS LAST", "warranty": "i.warranty_until ASC NULLS LAST"}.get(
        sort, "i.updated DESC")
    rows = db.execute("SELECT i.* FROM items i WHERE %s ORDER BY %s LIMIT 500" % (" AND ".join(where), order),
                      params).fetchall()
    items = decorate_items(rows)
    total_value = sum((it["purchase_price"] or 0) * (it["quantity"] or 1) for it in items)
    labels = db.execute("SELECT * FROM labels ORDER BY name COLLATE NOCASE").fetchall()
    return render_template("items.html", items=items, q=q, loc=loc, label=label, sort=sort, archived=archived,
                           locations=flat_location_choices(), labels=labels, total_value=total_value)


def _item_form_context(item=None, item_labels=None):
    db = get_db()
    return {
        "item": item,
        "fields": json.loads(item["fields"]) if item and item["fields"] else {},
        "item_label_ids": set(item_labels or []),
        "locations": flat_location_choices(),
        "labels": db.execute("SELECT * FROM labels ORDER BY name COLLATE NOCASE").fetchall(),
    }


def _apply_item_form(form, item_id=None):
    """Validate the item form; returns (values dict, error)."""
    name = _str(form.get("name"), 200)
    if not name:
        return None, "Give the item a name."
    v = {
        "name": name,
        "description": _str(form.get("description"), 2000),
        "quantity": _int(form.get("quantity"), 1, lo=0),
        "location_id": _int(form.get("location_id")),
        "serial": _str(form.get("serial"), 120),
        "model": _str(form.get("model"), 120),
        "manufacturer": _str(form.get("manufacturer"), 120),
        "purchase_from": _str(form.get("purchase_from"), 120),
        "purchase_price": _money(form.get("purchase_price")),
        "purchase_date": _date(form.get("purchase_date")),
        "warranty_until": _date(form.get("warranty_until")),
        "lifetime_warranty": 1 if form.get("lifetime_warranty") else 0,
        "warranty_notes": _str(form.get("warranty_notes"), 1000),
        "notes": _str(form.get("notes"), 5000),
        "fields": json.dumps(_fields_from_form(form)),
        "archived": 1 if form.get("archived") else 0,
    }
    if v["location_id"] and get_db().execute(
            "SELECT 1 FROM locations WHERE id = ?", (v["location_id"],)).fetchone() is None:
        v["location_id"] = None
    return v, None


def _set_labels(item_id, label_ids):
    db = get_db()
    db.execute("DELETE FROM item_labels WHERE item_id = ?", (item_id,))
    for lid in label_ids:
        lid = _int(lid)
        if lid:
            db.execute("INSERT OR IGNORE INTO item_labels (item_id, label_id) VALUES (?, ?)", (item_id, lid))


@app.route("/items/new", methods=["GET", "POST"])
def item_new():
    if request.method == "POST":
        v, err = _apply_item_form(request.form)
        if err:
            flash(err)
            return render_template("item_form.html", **_item_form_context()), 400
        now = time.time()
        db = get_db()
        cur = db.execute(
            "INSERT INTO items (name, description, quantity, location_id, serial, model, manufacturer, "
            "purchase_from, purchase_price, purchase_date, warranty_until, lifetime_warranty, warranty_notes, "
            "notes, fields, archived, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (v["name"], v["description"], v["quantity"], v["location_id"], v["serial"], v["model"],
             v["manufacturer"], v["purchase_from"], v["purchase_price"], v["purchase_date"], v["warranty_until"],
             v["lifetime_warranty"], v["warranty_notes"], v["notes"], v["fields"], v["archived"], now, now))
        item_id = cur.lastrowid
        _set_labels(item_id, request.form.getlist("labels"))
        for fs in request.files.getlist("photos"):
            save_upload(item_id, fs, "photo")
        db.commit()
        if needs_stock(db, item_id):
            queue_stock_photo(item_id)
        flash("Added %s." % v["name"])
        if request.form.get("another"):
            return redirect(url_for("item_new", location_id=v["location_id"] or ""))
        return redirect(url_for("item_page", item_id=item_id))
    ctx = _item_form_context()
    ctx["preset_location"] = _int(request.args.get("location_id"))
    return render_template("item_form.html", **ctx)


@app.route("/items/<int:item_id>")
def item_page(item_id):
    db = get_db()
    row = get_item_or_404(item_id)
    item = decorate_items([row])[0]
    item["fields_dict"] = json.loads(row["fields"] or "{}")
    photos = db.execute("SELECT * FROM attachments WHERE item_id = ? AND kind = 'photo' "
                        "ORDER BY is_primary DESC, created ASC", (item_id,)).fetchall()
    docs = db.execute("SELECT * FROM attachments WHERE item_id = ? AND kind NOT IN ('photo', 'stock') "
                      "ORDER BY created ASC", (item_id,)).fetchall()
    stock = db.execute("SELECT * FROM attachments WHERE item_id = ? AND kind = 'stock' ORDER BY created DESC LIMIT 1",
                       (item_id,)).fetchone()
    stock_info = json.loads(stock["source"]) if stock and stock["source"] else {}
    maint = db.execute("SELECT * FROM maintenance WHERE item_id = ? ORDER BY completed IS NOT NULL, "
                       "scheduled ASC, created DESC", (item_id,)).fetchall()
    maint_cost = sum(m["cost"] or 0 for m in maint)
    path = location_path(row["location_id"]) if row["location_id"] else []
    on_ebay = any(l["name"].lower() == EBAY_LABEL.lower() for l in item["labels"])
    has_manual = any(d["kind"] == "manual" for d in docs)
    return render_template("item.html", item=item, photos=photos, docs=docs, maint=maint, maint_cost=maint_cost,
                           path=path, recall_hits=recalls.open_hits(db, item_id), on_ebay=on_ebay,
                           has_manual=has_manual, stock=stock, stock_info=stock_info)


@app.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
def item_edit(item_id):
    db = get_db()
    row = get_item_or_404(item_id)
    current_labels = [r["label_id"] for r in db.execute(
        "SELECT label_id FROM item_labels WHERE item_id = ?", (item_id,)).fetchall()]
    if request.method == "POST":
        v, err = _apply_item_form(request.form, item_id)
        if err:
            flash(err)
            return render_template("item_form.html", **_item_form_context(row, current_labels)), 400
        db.execute(
            "UPDATE items SET name=?, description=?, quantity=?, location_id=?, serial=?, model=?, manufacturer=?, "
            "purchase_from=?, purchase_price=?, purchase_date=?, warranty_until=?, lifetime_warranty=?, "
            "warranty_notes=?, notes=?, fields=?, archived=?, updated=? WHERE id=?",
            (v["name"], v["description"], v["quantity"], v["location_id"], v["serial"], v["model"],
             v["manufacturer"], v["purchase_from"], v["purchase_price"], v["purchase_date"], v["warranty_until"],
             v["lifetime_warranty"], v["warranty_notes"], v["notes"], v["fields"], v["archived"], time.time(),
             item_id))
        _set_labels(item_id, request.form.getlist("labels"))
        for fs in request.files.getlist("photos"):
            save_upload(item_id, fs, "photo")
        db.commit()
        if needs_stock(db, item_id):
            queue_stock_photo(item_id)
        flash("Saved.")
        return redirect(url_for("item_page", item_id=item_id))
    return render_template("item_form.html", **_item_form_context(row, current_labels))


@app.route("/items/<int:item_id>/delete", methods=["POST"])
def item_delete(item_id):
    db = get_db()
    row = get_item_or_404(item_id)
    atts = db.execute("SELECT * FROM attachments WHERE item_id = ?", (item_id,)).fetchall()
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    delete_attachment_files(atts)
    flash("Deleted %s." % row["name"])
    return redirect(url_for("items_page"))


@app.route("/items/<int:item_id>/duplicate", methods=["POST"])
def item_duplicate(item_id):
    db = get_db()
    row = get_item_or_404(item_id)
    now = time.time()
    cur = db.execute(
        "INSERT INTO items (name, description, quantity, location_id, serial, model, manufacturer, purchase_from, "
        "purchase_price, purchase_date, warranty_until, lifetime_warranty, warranty_notes, notes, fields, archived, "
        "created, updated) SELECT name || ' (copy)', description, quantity, location_id, '', model, manufacturer, "
        "purchase_from, purchase_price, purchase_date, warranty_until, lifetime_warranty, warranty_notes, notes, "
        "fields, archived, ?, ? FROM items WHERE id = ?", (now, now, item_id))
    new_id = cur.lastrowid
    db.execute("INSERT INTO item_labels (item_id, label_id) SELECT ?, label_id FROM item_labels WHERE item_id = ?",
               (new_id, item_id))
    db.commit()
    flash("Duplicated. Update the serial number if it has one.")
    return redirect(url_for("item_edit", item_id=new_id))


@app.route("/items/<int:item_id>/files", methods=["POST"])
def item_files(item_id):
    get_item_or_404(item_id)
    kind = request.form.get("kind", "photo")
    if kind not in ("photo", "receipt", "manual", "warranty", "document"):
        kind = "document"
    n, new_ids = 0, []
    for fs in request.files.getlist("files"):
        att_id = save_upload(item_id, fs, kind)
        if att_id:
            n += 1
            new_ids.append(att_id)
    get_db().commit()
    if request.form.get("read") and kind in ("receipt", "warranty") and new_ids and ai.available():
        return receipt_review(new_ids[0])
    flash("Added %d file%s." % (n, "" if n == 1 else "s") if n else "No usable files were attached.")
    return redirect(url_for("item_page", item_id=item_id))


@app.route("/attachments/<int:att_id>")
def attachment_file(att_id):
    row = get_db().execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
    if row is None:
        abort(404)
    # Pictures open in the browser; everything else downloads so a hostile PDF or text file
    # never renders on Stash's origin (security review 2026-10-07).
    inline = ext_of(row["filename"]) in IMAGE_EXT
    return send_from_directory(UPLOADS, row["filename"], download_name=row["original_name"],
                               as_attachment=not inline, max_age=86400, conditional=True)


@app.route("/attachments/<int:att_id>/thumb")
def attachment_thumb(att_id):
    row = get_db().execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
    if row is None:
        abort(404)
    t = thumb_name(row["filename"])
    if os.path.exists(os.path.join(THUMBS, t)):
        return send_from_directory(THUMBS, t, max_age=86400, conditional=True)
    return send_from_directory(UPLOADS, row["filename"], max_age=86400, conditional=True)


@app.route("/attachments/<int:att_id>/primary", methods=["POST"])
def attachment_primary(att_id):
    db = get_db()
    row = db.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute("UPDATE attachments SET is_primary = 0 WHERE item_id = ? AND kind = 'photo'", (row["item_id"],))
    db.execute("UPDATE attachments SET is_primary = 1 WHERE id = ?", (att_id,))
    db.commit()
    return redirect(url_for("item_page", item_id=row["item_id"]))


@app.route("/attachments/<int:att_id>/delete", methods=["POST"])
def attachment_delete(att_id):
    db = get_db()
    row = db.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute("DELETE FROM attachments WHERE id = ?", (att_id,))
    if row["is_primary"]:
        nxt = db.execute("SELECT id FROM attachments WHERE item_id = ? AND kind = 'photo' ORDER BY created LIMIT 1",
                         (row["item_id"],)).fetchone()
        if nxt:
            db.execute("UPDATE attachments SET is_primary = 1 WHERE id = ?", (nxt["id"],))
    db.commit()
    delete_attachment_files([row])
    return redirect(url_for("item_page", item_id=row["item_id"]))


# ---------- maintenance ----------

@app.route("/items/<int:item_id>/maintenance", methods=["POST"])
def maintenance_add(item_id):
    get_item_or_404(item_id)
    name = _str(request.form.get("name"), 200)
    if not name:
        flash("Maintenance entries need a name.")
        return redirect(url_for("item_page", item_id=item_id))
    db = get_db()
    db.execute("INSERT INTO maintenance (item_id, name, notes, cost, scheduled, completed, created) "
               "VALUES (?,?,?,?,?,?,?)",
               (item_id, name, _str(request.form.get("notes"), 2000), _money(request.form.get("cost")),
                _date(request.form.get("scheduled")), _date(request.form.get("completed")), time.time()))
    db.execute("UPDATE items SET updated = ? WHERE id = ?", (time.time(), item_id))
    db.commit()
    return redirect(url_for("item_page", item_id=item_id))


@app.route("/maintenance/<int:m_id>/complete", methods=["POST"])
def maintenance_complete(m_id):
    db = get_db()
    row = db.execute("SELECT * FROM maintenance WHERE id = ?", (m_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute("UPDATE maintenance SET completed = ? WHERE id = ?", (date.today().isoformat(), m_id))
    db.commit()
    return redirect(safe_next(url_for("item_page", item_id=row["item_id"])))


@app.route("/maintenance/<int:m_id>/delete", methods=["POST"])
def maintenance_delete(m_id):
    db = get_db()
    row = db.execute("SELECT * FROM maintenance WHERE id = ?", (m_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute("DELETE FROM maintenance WHERE id = ?", (m_id,))
    db.commit()
    return redirect(safe_next(url_for("item_page", item_id=row["item_id"])))


@app.route("/maintenance")
def maintenance_page():
    db = get_db()
    rows = db.execute(
        "SELECT m.*, i.name AS item_name FROM maintenance m JOIN items i ON i.id = m.item_id "
        "ORDER BY m.completed IS NOT NULL, m.scheduled IS NULL, m.scheduled ASC, m.created DESC").fetchall()
    today = date.today().isoformat()
    overdue = [r for r in rows if not r["completed"] and r["scheduled"] and r["scheduled"] < today]
    upcoming = [r for r in rows if not r["completed"] and (not r["scheduled"] or r["scheduled"] >= today)]
    done = [r for r in rows if r["completed"]]
    horizon = (date.today() + timedelta(days=90)).isoformat()
    warranties = decorate_items(db.execute(
        "SELECT * FROM items WHERE archived = 0 AND lifetime_warranty = 0 AND warranty_until IS NOT NULL "
        "AND warranty_until <= ? ORDER BY warranty_until ASC", (horizon,)).fetchall())
    return render_template("maintenance.html", overdue=overdue, upcoming=upcoming, done=done, warranties=warranties,
                           recall_hits=recalls.open_hits(db), recall_status=recalls.status(db),
                           recall_running=_recall_lock.locked())


# ---------- locations ----------

@app.route("/locations")
def locations_page():
    tree = location_tree(item_counts_by_location())
    return render_template("locations.html", loc_tree=tree, locations=flat_location_choices(), n=len(location_index()))


@app.route("/locations/new", methods=["POST"])
def location_new():
    name = _str(request.form.get("name"), 120)
    if not name:
        flash("Give the location a name.")
        return redirect(safe_next(url_for("locations_page")))
    parent = _int(request.form.get("parent_id"))
    db = get_db()
    if parent and db.execute("SELECT 1 FROM locations WHERE id = ?", (parent,)).fetchone() is None:
        parent = None
    now = time.time()
    cur = db.execute("INSERT INTO locations (parent_id, name, description, created, updated) VALUES (?,?,?,?,?)",
                     (parent, name, _str(request.form.get("description"), 1000), now, now))
    db.commit()
    flash("Added %s." % name)
    return redirect(safe_next(url_for("location_page", loc_id=cur.lastrowid)))


@app.route("/locations/<int:loc_id>")
def location_page(loc_id):
    db = get_db()
    index = location_index()
    if loc_id not in index:
        abort(404)
    loc = index[loc_id]
    path = location_path(loc_id, index)
    children = sorted([l for l in index.values() if l["parent_id"] == loc_id], key=lambda l: l["name"].lower())
    counts = item_counts_by_location()
    for c in children:
        c["count"] = sum(counts.get(i, 0) for i in descendant_ids(c["id"], index))
    items = decorate_items(db.execute(
        "SELECT * FROM items WHERE archived = 0 AND location_id = ? ORDER BY name COLLATE NOCASE", (loc_id,)).fetchall())
    ids = descendant_ids(loc_id, index)
    nested_count = db.execute(
        "SELECT COUNT(*) FROM items WHERE archived = 0 AND location_id IN (%s)" % ",".join("?" * len(ids)),
        ids).fetchone()[0]
    return render_template("location.html", loc=loc, path=path, children=children, items=items,
                           nested_count=nested_count, locations=flat_location_choices())


@app.route("/locations/<int:loc_id>/edit", methods=["POST"])
def location_edit(loc_id):
    db = get_db()
    index = location_index()
    if loc_id not in index:
        abort(404)
    name = _str(request.form.get("name"), 120) or index[loc_id]["name"]
    parent = _int(request.form.get("parent_id"))
    if parent and (parent == loc_id or parent in descendant_ids(loc_id, index) or parent not in index):
        flash("A location cannot live inside itself.")
        parent = index[loc_id]["parent_id"]
    db.execute("UPDATE locations SET name = ?, parent_id = ?, description = ?, updated = ? WHERE id = ?",
               (name, parent, _str(request.form.get("description"), 1000), time.time(), loc_id))
    db.commit()
    flash("Saved.")
    return redirect(url_for("location_page", loc_id=loc_id))


@app.route("/locations/<int:loc_id>/delete", methods=["POST"])
def location_delete(loc_id):
    db = get_db()
    index = location_index()
    if loc_id not in index:
        abort(404)
    parent = index[loc_id]["parent_id"]
    db.execute("UPDATE items SET location_id = ? WHERE location_id = ?", (parent, loc_id))
    db.execute("UPDATE locations SET parent_id = ? WHERE parent_id = ?", (parent, loc_id))
    db.execute("DELETE FROM locations WHERE id = ?", (loc_id,))
    db.commit()
    flash("Deleted %s. Its items and sub-locations moved up one level." % index[loc_id]["name"])
    return redirect(url_for("location_page", loc_id=parent) if parent else url_for("locations_page"))


# ---------- labels ----------

@app.route("/labels")
def labels_page():
    rows = get_db().execute(
        "SELECT l.*, (SELECT COUNT(*) FROM item_labels il JOIN items i ON i.id = il.item_id "
        "WHERE il.label_id = l.id AND i.archived = 0) AS n FROM labels l ORDER BY l.name COLLATE NOCASE").fetchall()
    return render_template("labels.html", labels=rows, colors=LABEL_COLORS)


@app.route("/labels/new", methods=["POST"])
def label_new():
    name = _str(request.form.get("name"), 60)
    color = _str(request.form.get("color"), 9)
    if not re.match(r"^#[0-9a-fA-F]{6}$", color):
        color = LABEL_COLORS[0]
    if name:
        try:
            get_db().execute("INSERT INTO labels (name, color) VALUES (?, ?)", (name, color))
            get_db().commit()
        except sqlite3.IntegrityError:
            flash("There is already a label called %s." % name)
    return redirect(safe_next(url_for("labels_page")))


@app.route("/labels/<int:label_id>/edit", methods=["POST"])
def label_edit(label_id):
    db = get_db()
    name = _str(request.form.get("name"), 60)
    color = _str(request.form.get("color"), 9)
    if not re.match(r"^#[0-9a-fA-F]{6}$", color):
        color = LABEL_COLORS[0]
    if name:
        try:
            db.execute("UPDATE labels SET name = ?, color = ? WHERE id = ?", (name, color, label_id))
            db.commit()
        except sqlite3.IntegrityError:
            flash("There is already a label called %s." % name)
    return redirect(url_for("labels_page"))


@app.route("/labels/<int:label_id>/delete", methods=["POST"])
def label_delete(label_id):
    db = get_db()
    db.execute("DELETE FROM labels WHERE id = ?", (label_id,))
    db.commit()
    return redirect(url_for("labels_page"))


# ---------- QR labels ----------

def qr_svg(text):
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1)
    return img.to_string(encoding="unicode")


@app.route("/qr/<kind>/<int:obj_id>.svg")
def qr_image(kind, obj_id):
    if kind == "item":
        target = url_for("item_page", item_id=obj_id, _external=True)
    elif kind == "location":
        target = url_for("location_page", loc_id=obj_id, _external=True)
    else:
        abort(404)
    return Response(qr_svg(target), mimetype="image/svg+xml")


@app.route("/print")
def print_page():
    db = get_db()
    kind = request.args.get("kind", "locations")
    loc = _int(request.args.get("location"))
    index = location_index()
    if kind == "items":
        if loc:
            ids = descendant_ids(loc, index)
            rows = db.execute("SELECT * FROM items WHERE archived = 0 AND location_id IN (%s) "
                              "ORDER BY name COLLATE NOCASE" % ",".join("?" * len(ids)), ids).fetchall()
        else:
            rows = db.execute("SELECT * FROM items WHERE archived = 0 ORDER BY name COLLATE NOCASE").fetchall()
        cards = [{"kind": "item", "id": r["id"], "title": r["name"],
                  "sub": location_path_text(r["location_id"], index) if r["location_id"] else ""} for r in rows]
    else:
        locs = [index[i] for i in (descendant_ids(loc, index) if loc else index.keys()) if i in index]
        locs.sort(key=lambda l: location_path_text(l["id"], index).lower())
        cards = [{"kind": "location", "id": l["id"], "title": l["name"],
                  "sub": " / ".join(p["name"] for p in location_path(l["id"], index)[:-1])} for l in locs]
    return render_template("print.html", cards=cards, kind=kind, loc=loc, locations=flat_location_choices(),
                           base=request.url_root.rstrip("/"))


# ---------- import / export ----------

CSV_COLUMNS = ["name", "quantity", "location", "labels", "description", "serial", "model", "manufacturer",
               "purchase_from", "purchase_price", "purchase_date", "warranty_until", "lifetime_warranty",
               "warranty_notes", "notes", "fields"]


@app.route("/export.csv")
def export_csv():
    return Response(items_csv(get_db()), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=stash-%s.csv" % date.today().isoformat()})


def items_csv(db):
    index = location_index()
    rows = db.execute("SELECT * FROM items ORDER BY id").fetchall()
    labels = labels_for_items([r["id"] for r in rows])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS + ["archived", "id"])
    for r in rows:
        w.writerow([r["name"], r["quantity"], location_path_text(r["location_id"], index) if r["location_id"] else "",
                    ", ".join(l["name"] for l in labels.get(r["id"], [])), r["description"], r["serial"], r["model"],
                    r["manufacturer"], r["purchase_from"], "" if r["purchase_price"] is None else r["purchase_price"],
                    r["purchase_date"] or "", r["warranty_until"] or "", r["lifetime_warranty"], r["warranty_notes"],
                    r["notes"], r["fields"] if r["fields"] not in ("", "{}") else "", r["archived"], r["id"]])
    return buf.getvalue()


@app.route("/export.zip")
def export_zip():
    """Everything needed to rebuild Stash: a consistent copy of the database, the items CSV,
    an attachments index, and every uploaded file under its original name."""
    db = get_db()
    tmp = tempfile.mkdtemp(prefix="stash-export-")
    try:
        snap = os.path.join(tmp, "stash.db")
        dst = sqlite3.connect(snap)
        db.backup(dst)
        dst.close()
        atts = db.execute("SELECT a.*, i.name AS item_name FROM attachments a JOIN items i ON i.id = a.item_id "
                          "ORDER BY a.item_id, a.id").fetchall()
        idx = io.StringIO()
        w = csv.writer(idx)
        w.writerow(["item_id", "item_name", "kind", "original_name", "path_in_zip", "is_primary"])
        zpath = os.path.join(tmp, "export.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "stash.db")
            z.writestr("items.csv", items_csv(db))
            for a in atts:
                src = os.path.join(UPLOADS, a["filename"])
                if not os.path.isfile(src):
                    continue
                arc = "files/%d/%d-%s" % (a["item_id"], a["id"], a["original_name"])
                z.write(src, arc, compress_type=zipfile.ZIP_STORED)  # photos and PDFs are already compressed
                w.writerow([a["item_id"], a["item_name"], a["kind"], a["original_name"], arc, a["is_primary"]])
            z.writestr("attachments.csv", idx.getvalue())
        resp = send_file(zpath, mimetype="application/zip", as_attachment=True,
                         download_name="stash-%s.zip" % date.today().isoformat())
        resp.call_on_close(lambda: shutil.rmtree(tmp, ignore_errors=True))
        return resp
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def ensure_location_path(text, index):
    """'Home / Garage / Shelf 2' -> id of the last segment, creating any missing segments."""
    parts = [p.strip() for p in re.split(r"\s*/\s*|\s*>\s*", text) if p.strip()]
    if not parts:
        return None
    db = get_db()
    parent = None
    for part in parts:
        match = None
        for loc in index.values():
            if loc["parent_id"] == parent and loc["name"].lower() == part.lower():
                match = loc
                break
        if match is None:
            now = time.time()
            cur = db.execute("INSERT INTO locations (parent_id, name, description, created, updated) "
                             "VALUES (?,?,?,?,?)", (parent, part, "", now, now))
            match = {"id": cur.lastrowid, "parent_id": parent, "name": part}
            index[match["id"]] = match
        parent = match["id"]
    return parent


@app.route("/import", methods=["GET", "POST"])
def import_page():
    if request.method == "GET":
        return render_template("import.html", columns=CSV_COLUMNS)
    fs = request.files.get("file")
    if fs is None or not fs.filename:
        flash("Choose a CSV file first.")
        return redirect(url_for("import_page"))
    try:
        text = fs.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("That file is not UTF-8 text.")
        return redirect(url_for("import_page"))
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "name" not in [f.strip().lower() for f in reader.fieldnames]:
        flash("The CSV needs at least a 'name' column.")
        return redirect(url_for("import_page"))
    db = get_db()
    index = location_index()
    label_index = {r["name"].lower(): r["id"] for r in db.execute("SELECT id, name FROM labels").fetchall()}
    by_id = request.form.get("update_by_id") == "1"
    headers = {(f or "").strip().lower() for f in reader.fieldnames}
    has_field_cols = "fields" in headers or any(h.startswith("field:") for h in headers)
    added = updated = 0
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if isinstance(v, str) or v is None}
        name = _str(row.get("name"), 200)
        if not name:
            continue
        fields = {}
        if row.get("fields"):
            try:
                parsed = json.loads(row["fields"])
                if isinstance(parsed, dict):
                    fields = {str(k)[:60]: str(v)[:500] for k, v in parsed.items()}
            except ValueError:
                pass
        for k, v in row.items():
            if k.startswith("field:") and v:
                fields[k[6:].strip()[:60]] = v[:500]
        yes = lambda col: row.get(col, "").lower() in ("1", "true", "yes", "y")
        v = {
            "name": name,
            "description": _str(row.get("description"), 2000),
            "quantity": _int(row.get("quantity"), 1, lo=0),
            "location_id": ensure_location_path(row["location"], index) if row.get("location") else None,
            "serial": _str(row.get("serial"), 120),
            "model": _str(row.get("model"), 120),
            "manufacturer": _str(row.get("manufacturer"), 120),
            "purchase_from": _str(row.get("purchase_from"), 120),
            "purchase_price": _money(row.get("purchase_price")),
            "purchase_date": _date(row.get("purchase_date")),
            "warranty_until": _date(row.get("warranty_until")),
            "lifetime_warranty": 1 if yes("lifetime_warranty") else 0,
            "warranty_notes": _str(row.get("warranty_notes"), 1000),
            "notes": _str(row.get("notes"), 5000),
            "fields": json.dumps(fields),
            "archived": 1 if yes("archived") else 0,
        }
        existing = None
        if by_id and _int(row.get("id")):
            existing = db.execute("SELECT id FROM items WHERE id = ?", (_int(row.get("id")),)).fetchone()
        now = time.time()
        if existing:
            # Only the columns this CSV carries are overwritten, so a partial sheet cannot blank the rest.
            cols = [c for c in v if c in headers or (c == "fields" and has_field_cols)]
            if "location" in headers:
                cols.append("location_id")
            db.execute("UPDATE items SET %s, updated = ? WHERE id = ?" % ", ".join("%s = ?" % c for c in cols),
                       [v[c] for c in cols] + [now, existing["id"]])
            item_id = existing["id"]
            updated += 1
        else:
            cur = db.execute(
                "INSERT INTO items (name, description, quantity, location_id, serial, model, manufacturer, "
                "purchase_from, purchase_price, purchase_date, warranty_until, lifetime_warranty, warranty_notes, "
                "notes, fields, archived, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (v["name"], v["description"], v["quantity"], v["location_id"], v["serial"], v["model"],
                 v["manufacturer"], v["purchase_from"], v["purchase_price"], v["purchase_date"], v["warranty_until"],
                 v["lifetime_warranty"], v["warranty_notes"], v["notes"], v["fields"], v["archived"], now, now))
            item_id = cur.lastrowid
            added += 1
        if existing and "labels" in headers:
            db.execute("DELETE FROM item_labels WHERE item_id = ?", (item_id,))
        for lname in [l.strip() for l in re.split(r"[,;|]", row.get("labels", "")) if l.strip()]:
            lid = label_index.get(lname.lower())
            if lid is None:
                c = db.execute("INSERT INTO labels (name, color) VALUES (?, ?)",
                               (lname[:60], LABEL_COLORS[len(label_index) % len(LABEL_COLORS)]))
                lid = c.lastrowid
                label_index[lname.lower()] = lid
            db.execute("INSERT OR IGNORE INTO item_labels (item_id, label_id) VALUES (?, ?)", (item_id, lid))
    db.commit()
    plural = lambda n: "" if n == 1 else "s"
    msg = "Imported %d new item%s." % (added, plural(added))
    if updated:
        msg += " Updated %d existing item%s." % (updated, plural(updated))
    flash(msg)
    return redirect(url_for("items_page", sort="created" if added else "updated"))


# ---------- Claude: snap to add, receipt reader ----------

@app.route("/api/ai/identify", methods=["POST"])
def api_identify():
    images = []
    for fs in request.files.getlist("photos")[:6]:
        data = fs.read()
        if data:
            images.append(data)
    labels = get_db().execute("SELECT id, name FROM labels ORDER BY name COLLATE NOCASE").fetchall()
    by_name = {l["name"]: l["id"] for l in labels}
    try:
        out = ai.identify(images, list(by_name))
    except ai.AIError as exc:
        return jsonify(error=str(exc)), 502
    return jsonify(
        name=_str(out.get("name"), 200), manufacturer=_str(out.get("manufacturer"), 120),
        model=_str(out.get("model"), 120), serial=_str(out.get("serial"), 120),
        description=_str(out.get("description"), 2000), confidence=out.get("confidence", ""),
        labels=[by_name[n] for n in out.get("labels", []) if n in by_name])


READABLE = ("pdf",) + tuple(IMAGE_EXT)
RECEIPT_FIELDS = [  # (form/db field, label shown on the review page)
    ("purchase_from", "Bought from"), ("purchase_date", "Purchase date"), ("purchase_price", "Price"),
    ("quantity", "Quantity"), ("manufacturer", "Manufacturer"), ("model", "Model"), ("serial", "Serial"),
    ("warranty_until", "Warranty until"), ("warranty_notes", "Warranty notes"),
]


def receipt_review(att_id):
    db = get_db()
    att = db.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
    if att is None:
        abort(404)
    item = get_item_or_404(att["item_id"])
    ext = ext_of(att["filename"])
    if ext not in READABLE:
        flash("Stash can read receipts that are photos or PDFs.")
        return redirect(url_for("item_page", item_id=item["id"]))
    with open(os.path.join(UPLOADS, att["filename"]), "rb") as fh:
        data = fh.read()
    try:
        out = ai.read_receipt(data, ext == "pdf", item["name"])
    except ai.AIError as exc:
        flash(str(exc))
        return redirect(url_for("item_page", item_id=item["id"]))
    found = {
        "purchase_from": _str(out.get("store"), 120), "purchase_date": _date(out.get("purchase_date")) or "",
        "purchase_price": _money(out.get("price")), "quantity": _int(out.get("quantity"), None, lo=1),
        "manufacturer": _str(out.get("manufacturer"), 120), "model": _str(out.get("model"), 120),
        "serial": _str(out.get("serial"), 120), "warranty_until": _date(out.get("warranty_until")) or "",
        "warranty_notes": _str(out.get("warranty_notes"), 1000),
    }
    rows = []
    for key, label in RECEIPT_FIELDS:
        new, cur = found[key], item[key]
        if new in (None, ""):
            continue
        if key == "quantity" and new == cur:
            continue
        rows.append({"key": key, "label": label, "new": new, "cur": cur,
                     "same": str(new) == str(cur if cur is not None else ""),
                     "default": cur in (None, "", 0) or (key == "quantity" and cur == 1)})
    return render_template("receipt_review.html", item=item, att=att, rows=rows, line=_str(out.get("line"), 300))


@app.route("/attachments/<int:att_id>/read", methods=["POST"])
def attachment_read(att_id):
    return receipt_review(att_id)


@app.route("/items/<int:item_id>/apply", methods=["POST"])
def item_apply(item_id):
    """Apply the receipt fields the person ticked on the review page."""
    get_item_or_404(item_id)
    clean = {"purchase_from": lambda v: _str(v, 120), "purchase_date": _date, "purchase_price": _money,
             "quantity": lambda v: _int(v, 1, lo=0), "manufacturer": lambda v: _str(v, 120),
             "model": lambda v: _str(v, 120), "serial": lambda v: _str(v, 120), "warranty_until": _date,
             "warranty_notes": lambda v: _str(v, 1000)}
    sets = {k: clean[k](request.form.get("v_" + k)) for k in request.form.getlist("apply") if k in clean}
    if sets:
        db = get_db()
        db.execute("UPDATE items SET %s, updated = ? WHERE id = ?" % ", ".join("%s = ?" % k for k in sets),
                   list(sets.values()) + [time.time(), item_id])
        db.commit()
        flash("Updated %d field%s from the receipt." % (len(sets), "" if len(sets) == 1 else "s"))
    else:
        flash("Nothing changed.")
    return redirect(url_for("item_page", item_id=item_id))


# ---------- insurance report ----------

@app.route("/report")
def report_page():
    db = get_db()
    loc = _int(request.args.get("location"))
    index = location_index()
    where, params = "archived = 0", []
    if loc:
        ids = descendant_ids(loc, index)
        where += " AND location_id IN (%s)" % ",".join("?" * len(ids))
        params = ids
    items = decorate_items(db.execute("SELECT * FROM items WHERE %s" % where, params).fetchall())
    ids = [it["id"] for it in items]
    docs = {}
    if ids:
        for r in db.execute("SELECT item_id, kind FROM attachments WHERE kind NOT IN ('photo', 'stock') AND item_id IN (%s)"
                            % ",".join("?" * len(ids)), ids).fetchall():
            docs.setdefault(r["item_id"], set()).add(r["kind"])
    groups = {}
    for it in items:
        it["value"] = (it["purchase_price"] or 0) * (it["quantity"] or 0)
        it["replace_value"] = (it["replacement_cost"] or 0) * (it["quantity"] or 0)
        it["docs"] = sorted(docs.get(it["id"], []))
        groups.setdefault(it["location_text"] or "No location", []).append(it)
    sections = []
    for name in sorted(groups, key=lambda n: (n == "No location", n.lower())):
        rows = sorted(groups[name], key=lambda i: (-i["value"], i["name"].lower()))
        sections.append({"name": name, "rows": rows, "value": sum(i["value"] for i in rows),
                         "replace": sum(i["replace_value"] for i in rows),
                         "count": sum(i["quantity"] or 0 for i in rows),
                         "unpriced": sum(1 for i in rows if i["purchase_price"] is None)})
    totals = {"value": sum(s["value"] for s in sections), "count": sum(s["count"] for s in sections),
              "replace": sum(s["replace"] for s in sections),
              "repriced": sum(1 for i in items if i["replacement_cost"] is not None),
              "records": len(items), "unpriced": sum(s["unpriced"] for s in sections),
              "with_serial": sum(1 for i in items if i["serial"]),
              "with_receipt": sum(1 for i in items if "receipt" in i["docs"]),
              "with_photo": sum(1 for i in items if i["photo_id"] and not i["photo_stock"])}
    pricing = db.execute("SELECT v FROM meta WHERE k = 'pricing'").fetchone()
    return render_template("report.html", sections=sections, totals=totals, loc=loc,
                           pricing=pricing["v"] if pricing else ("starting" if _price_lock.locked() else ""),
                           scope=location_path_text(loc, index) if loc else "Whole house",
                           locations=flat_location_choices(), generated=date.today().isoformat())


# ---------- bulk edit ----------

@app.route("/items/bulk", methods=["POST"])
def items_bulk():
    db = get_db()
    ids = [i for i in (_int(x) for x in request.form.getlist("ids")) if i]
    back = safe_next(url_for("items_page"))
    if not ids:
        flash("Select some items first.")
        return redirect(back)
    marks = ",".join("?" * len(ids))
    ids = [r["id"] for r in db.execute("SELECT id FROM items WHERE id IN (%s)" % marks, ids).fetchall()]
    marks = ",".join("?" * len(ids))
    action = request.form.get("action", "")
    n, now, plural = len(ids), time.time(), "" if len(ids) == 1 else "s"
    if action == "move":
        loc = _int(request.form.get("location_id"))
        if loc and loc not in location_index():
            loc = None
        db.execute("UPDATE items SET location_id = ?, updated = ? WHERE id IN (%s)" % marks, [loc, now] + ids)
        msg = "Moved %d item%s%s." % (n, plural, " to " + location_path_text(loc) if loc else " out of any location")
    elif action in ("add_label", "remove_label"):
        lab = db.execute("SELECT * FROM labels WHERE id = ?", (_int(request.form.get("label_id")),)).fetchone()
        if lab is None:
            flash("Pick a label.")
            return redirect(back)
        if action == "add_label":
            db.executemany("INSERT OR IGNORE INTO item_labels (item_id, label_id) VALUES (?, ?)",
                           [(i, lab["id"]) for i in ids])
            msg = "Labeled %d item%s %s." % (n, plural, lab["name"])
        else:
            db.execute("DELETE FROM item_labels WHERE label_id = ? AND item_id IN (%s)" % marks, [lab["id"]] + ids)
            msg = "Removed %s from %d item%s." % (lab["name"], n, plural)
        db.execute("UPDATE items SET updated = ? WHERE id IN (%s)" % marks, [now] + ids)
    elif action in ("archive", "unarchive"):
        db.execute("UPDATE items SET archived = ?, updated = ? WHERE id IN (%s)" % marks,
                   [1 if action == "archive" else 0, now] + ids)
        msg = "%s %d item%s." % ("Archived" if action == "archive" else "Restored", n, plural)
    elif action == "delete":
        atts = db.execute("SELECT * FROM attachments WHERE item_id IN (%s)" % marks, ids).fetchall()
        db.execute("DELETE FROM items WHERE id IN (%s)" % marks, ids)
        db.commit()
        delete_attachment_files(atts)
        flash("Deleted %d item%s." % (n, plural))
        return redirect(back)
    else:
        abort(400)
    db.commit()
    flash(msg)
    return redirect(back)


# ---------- replacement cost ----------

REPRICE_AFTER_DAYS = 180
_price_lock = threading.Lock()


def _save_replacement(conn, item_id, out):
    price = out.get("price") if out.get("found") else None
    if price is not None and not (0 < price < 10_000_000):
        price = None
    note = _str(out.get("note"), 500)
    if price is not None and out.get("match") == "equivalent" and "equivalent" not in note.lower():
        note = ("Closest current equivalent. " + note).strip()
    conn.execute("UPDATE items SET replacement_cost = ?, replacement_product = ?, replacement_url = ?, "
                 "replacement_note = ?, replacement_checked = ? WHERE id = ?",
                 (round(price, 2) if price is not None else None, _str(out.get("product"), 200),
                  _safe_url(out.get("url")), note or ("No current price found." if price is None else ""),
                  time.time(), item_id))
    conn.commit()
    return price


def _safe_url(u):
    u = _str(u, 1000)
    return u if u.startswith("https://") or u.startswith("http://") else ""


@app.route("/items/<int:item_id>/replacement", methods=["POST"])
def item_replacement(item_id):
    item = get_item_or_404(item_id)
    try:
        price = _save_replacement(get_db(), item_id, ai.replacement_cost(dict(item)))
    except ai.AIError as exc:
        flash(str(exc))
    else:
        flash("Replacement cost today: %s." % money_filter(price) if price is not None
              else "Claude could not find a current price for this one.")
    return redirect(safe_next(url_for("item_page", item_id=item_id)))


def _price_job(item_ids):
    if not _price_lock.acquire(blocking=False):
        return
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    done = 0
    try:
        for item_id in item_ids:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                continue
            try:
                _save_replacement(conn, item_id, ai.replacement_cost(dict(row)))
                done += 1
            except ai.AIError as exc:
                log.warning("replacement cost failed for item %s: %s", item_id, exc)
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('pricing', ?)", ("%d of %d" % (done, len(item_ids)),))
            conn.commit()
    finally:
        conn.execute("DELETE FROM meta WHERE k = 'pricing'")
        conn.commit()
        conn.close()
        _price_lock.release()
        log.info("replacement pricing: %d of %d items priced", done, len(item_ids))


@app.route("/report/price-all", methods=["POST"])
def report_price_all():
    if _price_lock.locked():
        flash("Pricing is already running.")
        return redirect(safe_next(url_for("report_page")))
    cutoff = time.time() - REPRICE_AFTER_DAYS * 86400
    ids = [r["id"] for r in get_db().execute(
        "SELECT id FROM items WHERE archived = 0 AND (replacement_checked IS NULL OR replacement_checked < ?) "
        "ORDER BY purchase_price IS NULL, purchase_price DESC", (cutoff,)).fetchall()]
    if not ids:
        flash("Every item was priced in the last %d days." % REPRICE_AFTER_DAYS)
    else:
        threading.Thread(target=_price_job, args=(ids,), daemon=True).start()
        flash("Pricing %d item%s in the background, a few seconds each. Reload this page to see progress."
              % (len(ids), "" if len(ids) == 1 else "s"))
    return redirect(safe_next(url_for("report_page")))


# ---------- manual finder ----------

MAX_FETCH = MAX_UPLOAD


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0) AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/18.0 Safari/605.1.15")


def fetch_public(url, max_bytes, accept="*/*", timeout=30):
    """GET from the public internet only: http(s), public IPs, at most 3 redirects, each re-checked.
    Returns (bytes, content type, final url)."""
    for _ in range(4):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("not a web link")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        for info in socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP):
            if not ipaddress.ip_address(info[4][0]).is_global:
                raise ValueError("link points at a private address")
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": accept})
        try:
            resp = urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                url = urllib.parse.urljoin(url, exc.headers["Location"])
                continue
            raise
        with resp:
            data = resp.read(max_bytes + 1)
            ctype = resp.headers.get("Content-Type", "")
        if len(data) > max_bytes:
            raise ValueError("file too large")
        return data, ctype, url
    raise ValueError("too many redirects")


def fetch_public_pdf(url):
    if not url.startswith("https://"):
        raise ValueError("not an https link")
    data, _, _ = fetch_public(url, MAX_FETCH, "application/pdf,*/*", timeout=60)
    if not data.startswith(b"%PDF-"):
        raise ValueError("the link is not a PDF")
    return data


@app.route("/items/<int:item_id>/manual", methods=["POST"])
def item_manual(item_id):
    item = get_item_or_404(item_id)
    back = url_for("item_page", item_id=item_id)
    try:
        out = ai.find_manual(dict(item))
    except ai.AIError as exc:
        flash(str(exc))
        return redirect(back)
    note = _str(out.get("note"), 300)
    pdf, page = _safe_url(out.get("pdf_url")), _safe_url(out.get("page_url"))
    if out.get("found") and pdf:
        try:
            data = fetch_public_pdf(pdf)
        except Exception as exc:
            log.info("manual download failed for item %s: %s", item_id, exc)
            page = page or pdf
        else:
            title = secure_filename(_str(out.get("title"), 80)) or "manual"
            if attach_bytes(item_id, data, title + ".pdf", "manual", "application/pdf"):
                get_db().commit()
                flash("Attached the manual%s." % (": " + note if note else ""))
                return redirect(back)
    if out.get("found") and page:
        db = get_db()
        fields = json.loads(item["fields"] or "{}")
        fields["Manual"] = page
        db.execute("UPDATE items SET fields = ?, updated = ? WHERE id = ?", (json.dumps(fields), time.time(), item_id))
        db.commit()
        flash("Found the manual online but could not download it. The link is saved under Manual in the facts.")
    else:
        flash("Claude could not find a manual for this one.%s" % (" " + note if note else ""))
    return redirect(back)


# ---------- stock photos ----------

STOCK_MAX_IMAGE = 10 * 1024 * 1024
STOCK_EDGE = 1200
COMMONS_API = "https://commons.wikimedia.org/w/api.php"


class _MetaImages(HTMLParser):
    """Collect og:image / twitter:image from a product page's <head>."""
    def __init__(self):
        super().__init__()
        self.found = []

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        key = (a.get("property") or a.get("name") or "").lower()
        if key in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src") and a.get("content"):
            self.found.append(a["content"].strip())


def _clean_stock_image(data):
    """Validate and normalize a downloaded picture to a modest JPEG; None if it is not a usable photo."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if im.width < 200 or im.height < 150:
                return None
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            im.thumbnail((STOCK_EDGE, STOCK_EDGE))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=86)
            return out.getvalue()
    except Exception:
        return None


def _image_from_page(page_url):
    html_bytes, ctype, final = fetch_public(page_url, 3 * 1024 * 1024, "text/html,*/*", timeout=20)
    if "html" not in ctype.lower():
        return None
    parser = _MetaImages()
    try:
        parser.feed(html_bytes.decode("utf-8", "ignore"))
    except Exception:
        return None
    for src in parser.found[:3]:
        img_url = urllib.parse.urljoin(final, src)
        try:
            data, _, _ = fetch_public(img_url, STOCK_MAX_IMAGE, "image/*", timeout=20)
        except Exception:
            continue
        clean = _clean_stock_image(data)
        if clean:
            return clean
    return None


def _image_from_commons(generic):
    q = urllib.parse.urlencode({"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
                                "gsrlimit": 8, "gsrsearch": generic + " filetype:bitmap", "prop": "imageinfo",
                                "iiprop": "url|extmetadata", "iiurlwidth": STOCK_EDGE})
    data, _, _ = fetch_public(COMMONS_API + "?" + q, 2 * 1024 * 1024, "application/json")
    pages = sorted(json.loads(data.decode("utf-8")).get("query", {}).get("pages", {}).values(),
                   key=lambda p: p.get("index", 0))
    pages = [p for p in pages if p.get("imageinfo")]
    if not pages:
        return None
    pick = ai.pick_commons(generic, [p["title"] for p in pages])
    if pick < 0:
        return None
    info = pages[pick]["imageinfo"][0]
    img, _, _ = fetch_public(info.get("thumburl") or info["url"], STOCK_MAX_IMAGE, "image/*")
    clean = _clean_stock_image(img)
    if not clean:
        return None
    meta = info.get("extmetadata", {})
    lic = re.sub(r"<[^>]+>", "", meta.get("LicenseShortName", {}).get("value", "")).strip()
    artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip()
    credit = "Wikimedia Commons" + (", " + artist[:60] if artist else "") + (", " + lic if lic else "")
    return clean, info.get("descriptionurl") or "", credit


def find_stock_photo(item):
    """Returns (jpeg bytes, source page, credit, match) or None. Maker and retailer pages first,
    then a Wikimedia Commons photo of the same kind of thing."""
    tried = set()
    if item["replacement_url"]:  # replacement lookup already found the product page: free first try
        tried.add(item["replacement_url"])
        try:
            img = _image_from_page(item["replacement_url"])
            if img:
                return img, item["replacement_url"], urllib.parse.urlsplit(item["replacement_url"]).hostname, "exact"
        except Exception as exc:
            log.info("stock: replacement page failed for item %s: %s", item["id"], exc)
    out = ai.stock_photo_pages(dict(item))
    for page in out.get("pages", [])[:4]:
        url = _safe_url(page.get("url"))
        if not url or url in tried:
            continue
        tried.add(url)
        try:
            img = _image_from_page(url)
        except Exception as exc:
            log.info("stock: %s failed: %s", urllib.parse.urlsplit(url).hostname, exc)
            continue
        if img:
            return img, url, (urllib.parse.urlsplit(url).hostname or "").replace("www.", ""), page.get("match", "similar")
    generic = _str(out.get("generic"), 60) or _str(item["name"], 60)
    try:
        got = _image_from_commons(generic)
    except Exception as exc:
        log.info("stock: commons failed for %r: %s", generic, exc)
        got = None
    if got:
        return got[0], got[1], got[2], "example"
    return None


def attach_stock_photo(item_id):
    """Look up and store a stock photo for an item with no photos. Returns a short result string."""
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        return "gone"
    if db.execute("SELECT 1 FROM attachments WHERE item_id = ? AND kind = 'photo'", (item_id,)).fetchone():
        return "has photos"
    found = find_stock_photo(item)
    if not found:
        return "none found"
    data, source, credit, match = found
    old = db.execute("SELECT * FROM attachments WHERE item_id = ? AND kind = 'stock'", (item_id,)).fetchall()
    att_id = attach_bytes(item_id, data, "stock.jpg", "stock", "image/jpeg")
    if not att_id:
        return "unusable image"
    if match == "example":
        label = "Example photo from " + credit
    else:
        label = ("Similar product" if match == "similar" else "Stock photo") + (" from " + credit if credit else "")
    db.execute("UPDATE attachments SET source = ? WHERE id = ?", (json.dumps({"url": source, "label": label}), att_id))
    db.execute("DELETE FROM attachments WHERE item_id = ? AND kind = 'stock' AND id != ?", (item_id, att_id))
    db.execute("UPDATE items SET stock_off = 0 WHERE id = ?", (item_id,))
    db.commit()
    delete_attachment_files(old)
    return "ok"


_stock_queue = queue.Queue()
_stock_worker = {"thread": None}


def _stock_loop():
    while True:
        item_id = _stock_queue.get()
        try:
            with app.app_context():
                result = attach_stock_photo(item_id)
            log.info("stock photo for item %s: %s", item_id, result)
        except ai.AIError as exc:
            log.info("stock photo for item %s: %s", item_id, exc)
        except Exception:
            log.exception("stock photo for item %s failed", item_id)
        finally:
            _stock_queue.task_done()


def queue_stock_photo(item_id):
    """Find a stock photo in the background, so saving an item never waits on the web."""
    if not ai.available():
        return
    if _stock_worker["thread"] is None or not _stock_worker["thread"].is_alive():
        _stock_worker["thread"] = threading.Thread(target=_stock_loop, daemon=True)
        _stock_worker["thread"].start()
    _stock_queue.put(item_id)


def needs_stock(db, item_id):
    row = db.execute("SELECT stock_off, (SELECT COUNT(*) FROM attachments a WHERE a.item_id = items.id "
                     "AND a.kind IN ('photo', 'stock')) AS n FROM items WHERE id = ?", (item_id,)).fetchone()
    return row is not None and not row["stock_off"] and row["n"] == 0


@app.route("/items/<int:item_id>/stock", methods=["POST"])
def item_stock(item_id):
    get_item_or_404(item_id)
    try:
        result = attach_stock_photo(item_id)
    except ai.AIError as exc:
        result = str(exc)
    flash({"ok": "Added a stock photo. Your own photos always show first.",
           "none found": "Could not find a stock photo for this one.",
           "has photos": "This item already has your own photos.",
           "unusable image": "The photo found could not be used. Try again later."}.get(result, result))
    return redirect(url_for("item_page", item_id=item_id))


@app.route("/items/<int:item_id>/stock/remove", methods=["POST"])
def item_stock_remove(item_id):
    db = get_db()
    get_item_or_404(item_id)
    old = db.execute("SELECT * FROM attachments WHERE item_id = ? AND kind = 'stock'", (item_id,)).fetchall()
    db.execute("DELETE FROM attachments WHERE item_id = ? AND kind = 'stock'", (item_id,))
    db.execute("UPDATE items SET stock_off = 1 WHERE id = ?", (item_id,))
    db.commit()
    delete_attachment_files(old)
    flash("Removed the stock photo. Stash will not add another unless you ask.")
    return redirect(url_for("item_page", item_id=item_id))


# ---------- room walkthrough ----------

STAGING = os.path.join(UPLOADS, "staging")
WALK_MAX = 20
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def _staging_dir(token):
    if not TOKEN_RE.match(token or ""):
        abort(404)
    path = os.path.join(STAGING, token)
    if not os.path.isdir(path):
        abort(404)
    return path


@app.route("/walkthrough", methods=["GET", "POST"])
def walkthrough_page():
    if request.method == "GET":
        return render_template("walkthrough.html", locations=flat_location_choices(),
                               preset=_int(request.args.get("location_id")), max_photos=WALK_MAX)
    request.max_content_length = WALK_MAX * MAX_UPLOAD  # 20 full-size phone photos
    loc = _int(request.form.get("location_id"))
    index = location_index()
    if loc not in index:
        loc = None
    files = [fs for fs in request.files.getlist("photos") if fs.filename][:WALK_MAX]
    if not files:
        flash("Pick some photos first.")
        return redirect(url_for("walkthrough_page", location_id=loc or ""))
    token = uuid.uuid4().hex
    folder = os.path.join(STAGING, token)
    os.makedirs(folder)
    images = []
    for n, fs in enumerate(files, 1):  # staged as clean JPEGs: no EXIF, sane size
        try:
            with Image.open(fs.stream) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((2400, 2400))
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                im.save(os.path.join(folder, "%d.jpg" % n), "JPEG", quality=88)
        except Exception:
            continue
        with open(os.path.join(folder, "%d.jpg" % n), "rb") as fh:
            images.append((n, fh.read()))
    if not images:
        shutil.rmtree(folder, ignore_errors=True)
        flash("None of those files are photos Stash can read.")
        return redirect(url_for("walkthrough_page", location_id=loc or ""))
    labels = get_db().execute("SELECT id, name FROM labels ORDER BY name COLLATE NOCASE").fetchall()
    by_name = {l["name"]: l["id"] for l in labels}
    place = location_path_text(loc, index) if loc else ""
    try:
        drafts = ai.walkthrough([b for _, b in images], place, list(by_name))
    except ai.AIError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        flash(str(exc))
        return redirect(url_for("walkthrough_page", location_id=loc or ""))
    photo_nums = [n for n, _ in images]
    clean = []
    for d in drafts[:60]:
        p = d.get("photo") or 1
        clean.append({"name": _str(d.get("name"), 200), "manufacturer": _str(d.get("manufacturer"), 120),
                      "model": _str(d.get("model"), 120), "description": _str(d.get("description"), 2000),
                      "quantity": max(1, min(_int(d.get("quantity"), 1) or 1, 999)),
                      "photo": photo_nums[p - 1] if 1 <= p <= len(photo_nums) else photo_nums[0],
                      "labels": [by_name[x] for x in d.get("labels", []) if x in by_name]})
    with open(os.path.join(folder, "drafts.json"), "w") as fh:
        json.dump({"location_id": loc, "drafts": clean}, fh)
    return render_template("walkthrough_review.html", token=token, drafts=clean, loc=loc, place=place,
                           locations=flat_location_choices(), photos=photo_nums,
                           labels={l["id"]: l["name"] for l in labels})


@app.route("/walkthrough/<token>/<int:n>.jpg")
def walkthrough_photo(token, n):
    return send_from_directory(_staging_dir(token), "%d.jpg" % n, max_age=0)


@app.route("/walkthrough/<token>/create", methods=["POST"])
def walkthrough_create(token):
    folder = _staging_dir(token)
    with open(os.path.join(folder, "drafts.json")) as fh:
        staged = json.load(fh)
    db = get_db()
    index = location_index()
    loc = _int(request.form.get("location_id"))
    if loc not in index:
        loc = None
    made, now = [], time.time()
    for i, d in enumerate(staged["drafts"]):
        if request.form.get("keep_%d" % i) != "1":
            continue
        name = _str(request.form.get("name_%d" % i), 200) or d["name"]
        if not name:
            continue
        cur = db.execute(
            "INSERT INTO items (name, description, quantity, location_id, serial, model, manufacturer, created, "
            "updated) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, _str(request.form.get("desc_%d" % i), 2000), _int(request.form.get("qty_%d" % i), 1, lo=0),
             loc, "", _str(request.form.get("model_%d" % i), 120), _str(request.form.get("maker_%d" % i), 120),
             now, now))
        item_id = cur.lastrowid
        for lid in d["labels"]:
            db.execute("INSERT OR IGNORE INTO item_labels (item_id, label_id) VALUES (?, ?)", (item_id, lid))
        src = os.path.join(folder, "%d.jpg" % d["photo"])
        if os.path.isfile(src):
            with open(src, "rb") as fh:
                attach_bytes(item_id, fh.read(), "walkthrough-%d.jpg" % d["photo"], "photo", "image/jpeg")
        made.append(item_id)
    db.commit()
    shutil.rmtree(folder, ignore_errors=True)
    flash("Added %d item%s%s." % (len(made), "" if len(made) == 1 else "s",
                                  " to " + location_path_text(loc, index) if loc else ""))
    return redirect(url_for("location_page", loc_id=loc) if loc else url_for("items_page", sort="created"))


# ---------- eBay ----------

def ebay_label_id(db):
    row = db.execute("SELECT id FROM labels WHERE name = ? COLLATE NOCASE", (EBAY_LABEL,)).fetchone()
    if row:
        return row["id"]
    cur = db.execute("INSERT INTO labels (name, color) VALUES (?, ?)", (EBAY_LABEL, "#d97706"))
    db.commit()
    return cur.lastrowid


def ebay_search_url(item):
    q = " ".join(x for x in (item["manufacturer"], item["model"] or item["name"]) if x) or item["name"]
    return "https://www.ebay.com/sch/i.html?" + urllib.parse.urlencode({"_nkw": q, "LH_Sold": "1", "LH_Complete": "1"})


def ebay_data(db, item_id):
    row = db.execute("SELECT * FROM ebay WHERE item_id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    data = json.loads(row["data"] or "{}")
    data["checked"] = row["checked"]
    return data


@app.route("/ebay")
def ebay_page():
    db = get_db()
    lid = ebay_label_id(db)
    items = decorate_items(db.execute(
        "SELECT * FROM items WHERE archived = 0 AND id IN (SELECT item_id FROM item_labels WHERE label_id = ?) "
        "ORDER BY name COLLATE NOCASE", (lid,)).fetchall())
    for it in items:
        it["ebay"] = ebay_data(db, it["id"])
    total = sum((it["ebay"] or {}).get("suggested_price", 0) * (it["quantity"] or 1) for it in items)
    return render_template("ebay.html", items=items, total=total, label_id=lid)


@app.route("/items/<int:item_id>/ebay/toggle", methods=["POST"])
def ebay_toggle(item_id):
    db = get_db()
    item = get_item_or_404(item_id)
    lid = ebay_label_id(db)
    if db.execute("SELECT 1 FROM item_labels WHERE item_id = ? AND label_id = ?", (item_id, lid)).fetchone():
        db.execute("DELETE FROM item_labels WHERE item_id = ? AND label_id = ?", (item_id, lid))
        flash("Took %s off the eBay list." % item["name"])
    else:
        db.execute("INSERT INTO item_labels (item_id, label_id) VALUES (?, ?)", (item_id, lid))
        flash("Added %s to the eBay list. Research prices from its eBay page." % item["name"])
    db.execute("UPDATE items SET updated = ? WHERE id = ?", (time.time(), item_id))
    db.commit()
    return redirect(safe_next(url_for("item_page", item_id=item_id)))


@app.route("/items/<int:item_id>/ebay")
def item_ebay(item_id):
    db = get_db()
    item = decorate_items([get_item_or_404(item_id)])[0]
    return render_template("item_ebay.html", item=item, data=ebay_data(db, item_id),
                           sold_url=ebay_search_url(item))


@app.route("/items/<int:item_id>/ebay/research", methods=["POST"])
def ebay_research(item_id):
    db = get_db()
    item = get_item_or_404(item_id)
    photo = None
    p = db.execute("SELECT filename FROM attachments WHERE item_id = ? AND kind = 'photo' "
                   "ORDER BY is_primary DESC, created LIMIT 1", (item_id,)).fetchone()
    if p and os.path.isfile(os.path.join(UPLOADS, p["filename"])):
        with open(os.path.join(UPLOADS, p["filename"]), "rb") as fh:
            photo = fh.read()
    try:
        out = ai.ebay_research(dict(item), photo)
    except ai.AIError as exc:
        flash(str(exc))
        return redirect(url_for("item_ebay", item_id=item_id))
    out["comps"] = [c for c in out.get("comps", []) if _safe_url(c.get("url"))][:8]
    out["title"] = _str(out.get("title"), 80)
    db.execute("INSERT OR REPLACE INTO ebay (item_id, data, checked) VALUES (?, ?, ?)",
               (item_id, json.dumps(out), time.time()))
    db.commit()
    return redirect(url_for("item_ebay", item_id=item_id))


# ---------- recalls ----------

_recall_lock = threading.Lock()


def _recall_job():
    if not _recall_lock.acquire(blocking=False):
        return
    try:
        added, hits = recalls.run(DB_PATH)
        log.info("recall check: %d recalls stored, %d new hits", added, hits)
    except Exception as exc:
        log.warning("recall check failed: %s", exc)
    finally:
        _recall_lock.release()


@app.route("/recalls/check", methods=["POST"])
def recalls_check():
    if _recall_lock.locked():
        flash("A recall check is already running.")
    else:
        threading.Thread(target=_recall_job, daemon=True).start()
        flash("Checking for recalls. This takes a minute or two. Reload Upkeep to see results.")
    return redirect(safe_next(url_for("maintenance_page")))


@app.route("/items/<int:item_id>/recalls/<recall_number>/dismiss", methods=["POST"])
def recall_dismiss(item_id, recall_number):
    db = get_db()
    db.execute("UPDATE recall_checks SET dismissed = 1 WHERE item_id = ? AND recall_number = ?",
               (item_id, _str(recall_number, 40)))
    db.commit()
    flash("Dismissed. Stash will not raise that recall for this item again.")
    return redirect(safe_next(url_for("item_page", item_id=item_id)))


# ---------- JSON ----------

@app.route("/api/search")
def api_search():
    q = _str(request.args.get("q"), 120)
    if not q:
        return jsonify(items=[], locations=[])
    like = "%" + q + "%"
    db = get_db()
    index = location_index()
    items = db.execute("SELECT id, name, location_id FROM items WHERE archived = 0 AND (name LIKE ? OR serial LIKE ? "
                       "OR model LIKE ? OR description LIKE ?) ORDER BY name COLLATE NOCASE LIMIT 8",
                       (like, like, like, like)).fetchall()
    locs = [l for l in index.values() if q.lower() in l["name"].lower()][:5]
    return jsonify(
        items=[{"id": r["id"], "name": r["name"], "url": url_for("item_page", item_id=r["id"]),
                "where": location_path_text(r["location_id"], index) if r["location_id"] else ""} for r in items],
        locations=[{"id": l["id"], "name": location_path_text(l["id"], index),
                    "url": url_for("location_page", loc_id=l["id"])} for l in locs])


@app.route("/api/items")
def api_items():
    rows = get_db().execute("SELECT * FROM items WHERE archived = 0 ORDER BY name COLLATE NOCASE").fetchall()
    return jsonify(items=decorate_items(rows))


@app.route("/health")
def health():
    db = get_db()
    return jsonify(ok=True, items=db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                   locations=db.execute("SELECT COUNT(*) FROM locations").fetchone()[0])


@app.route("/manifest.json")
def manifest():
    return send_from_directory(os.path.join(HERE, "static"), "manifest.json")


@app.errorhandler(413)
def too_large(_e):
    flash("That upload is over 25 MB. Resize the photo and try again.")
    ref = request.referrer or ""
    return redirect(ref if ref.startswith(request.host_url) else url_for("index"))


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html"), 404


init_db()

if __name__ == "__main__":
    if "--sweep" in sys.argv:  # nightly from backup.sh
        moved, purged = sweep_orphans()
        print("orphan sweep: %d moved to %s, %d purged" % (moved, ORPHANS, purged))
    elif "--recalls" in sys.argv:  # weekly from launchd
        added, hits = recalls.run(DB_PATH)
        print("recall check: %d recalls stored, %d new hits" % (added, hits))
    elif "--dev" in sys.argv:
        app.run(host=HOST, port=PORT, debug=True, use_reloader=False)
    else:
        from waitress import serve
        log.info("Stash serving on http://%s:%s (uploads in %s)", HOST, PORT, UPLOADS)
        log.info("Claude features: %s", "ready" if ai.available() else "off (no anthropic package or no key)")
        serve(app, host=HOST, port=PORT, threads=6)
