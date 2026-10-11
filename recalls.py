"""Product recall check against the CPSC recall database (saferproducts.gov, no key needed).

The CPSC text search is unreliable, but date-range queries work, so Stash keeps its own copy of
every recall since 2000 and matches locally: brand or model overlap picks candidates, then Claude
judges each candidate against the item. Verdicts are cached per item and recall; an item is
re-judged only when its name, brand, or model changes.
"""
import json
import logging
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

import ai

log = logging.getLogger("stash.recalls")

API = "https://www.saferproducts.gov/RestWebServices/Recall"
FIRST_YEAR = 2000
MAX_CANDIDATES = 10
STOP = {"the", "and", "for", "with", "set", "kit", "pack", "new", "old", "inch", "small", "large", "black",
        "white", "gray", "grey", "blue", "red", "green", "silver", "model", "series", "home", "house"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS recalls (
    recall_number TEXT PRIMARY KEY,
    recall_date TEXT,
    last_publish TEXT,
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    products TEXT NOT NULL DEFAULT '',
    makers TEXT NOT NULL DEFAULT '',
    hazard TEXT NOT NULL DEFAULT '',
    remedy TEXT NOT NULL DEFAULT '',
    haystack TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS recall_checks (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    recall_number TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    dismissed INTEGER NOT NULL DEFAULT 0,
    checked REAL NOT NULL,
    PRIMARY KEY (item_id, recall_number)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def squash(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _fetch(params):
    url = API + "?" + urllib.parse.urlencode(dict(params, format="json"))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Stash home inventory)",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data and not data[0].get("RecallNumber") and "Error" in (data[0].get("Title") or ""):
        raise RuntimeError("CPSC: " + data[0]["Title"])
    return data


def _row(r):
    prods = []
    for p in r.get("Products") or []:
        bits = [p.get("Name"), p.get("Model"), p.get("Description"), p.get("Type")]
        prods.append(" | ".join(b.strip() for b in bits if b and b.strip()))
    makers = [m.get("Name", "") for m in (r.get("Manufacturers") or []) + (r.get("Importers") or [])
              + (r.get("Distributors") or []) if m.get("Name")]
    hazard = "; ".join(h.get("Name", "") for h in r.get("Hazards") or [] if h.get("Name"))
    remedy = "; ".join(x.get("Name", "") for x in r.get("Remedies") or [] if x.get("Name"))
    title = r.get("Title") or ""
    products = "\n".join(prods)
    haystack = " ".join([norm(title), norm(products), norm(" ".join(makers)), norm(r.get("Description") or "")])
    return (r["RecallNumber"], (r.get("RecallDate") or "")[:10], (r.get("LastPublishDate") or "")[:10], title,
            r.get("URL") or "", products, ", ".join(makers), hazard[:1000], remedy[:2000], haystack)


def _store(conn, records):
    n = 0
    for r in records:
        if not r.get("RecallNumber"):
            continue
        conn.execute("INSERT OR REPLACE INTO recalls VALUES (?,?,?,?,?,?,?,?,?,?)", _row(r))
        n += 1
    conn.commit()
    return n


def sync(conn):
    """Bring the local recall copy up to date. First run backfills year by year (slow: the CPSC
    API takes ~30 s a request); later runs fetch what was published since the last sync."""
    conn.executescript(SCHEMA)
    last = conn.execute("SELECT MAX(last_publish) FROM recalls").fetchone()[0]
    total = 0
    if not last:
        done = {r[0] for r in conn.execute("SELECT v FROM meta WHERE k LIKE 'backfill:%'")}
        for year in range(FIRST_YEAR, date.today().year + 1):
            if str(year) in done and year < date.today().year:
                continue
            got = _fetch({"RecallDateStart": "%d-01-01" % year, "RecallDateEnd": "%d-12-31" % year})
            total += _store(conn, got)
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", ("backfill:%d" % year, str(year)))
            conn.commit()
            log.info("recalls %d: %d", year, len(got))
    else:
        since = (date.fromisoformat(last) - timedelta(days=14)).isoformat()
        total += _store(conn, _fetch({"LastPublishDateStart": since}))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('recalls_synced', ?)", (str(time.time()),))
    conn.commit()
    return total


def fingerprint(item):
    return "|".join(squash(item[k]) for k in ("name", "manufacturer", "model"))


def candidates(conn, item):
    """Recalls sharing the item's brand (plus a product word or the model), or its model number."""
    brand = norm(item["manufacturer"])
    model = squash(item["model"])
    words = [w for w in norm(item["name"]).split() if len(w) >= 4 and w not in STOP]
    if len(brand) < 3 and len(model) < 5:
        return []
    needle = brand if len(brand) >= 3 else norm(item["model"])
    rows = conn.execute("SELECT * FROM recalls WHERE haystack LIKE ? ORDER BY recall_date DESC LIMIT 400",
                        ("%" + needle + "%",)).fetchall()
    scored = []
    for r in rows:
        hay = r["haystack"]
        score = 0
        if len(model) >= 3 and model in hay.replace(" ", ""):
            score += 10
        if brand and re.search(r"\b%s\b" % re.escape(brand), hay):
            score += 3
        score += sum(1 for w in words if re.search(r"\b%s" % re.escape(w), hay))
        if score >= 4:  # brand plus a product word, or the model number
            scored.append((score, r))
    scored.sort(key=lambda t: -t[0])  # stable: newest first within a score
    return [r for _, r in scored[:MAX_CANDIDATES]]


def check(conn, item_ids=None):
    """Judge candidates for every active item (or the given ones). Returns count of new hits."""
    conn.executescript(SCHEMA)
    q = "SELECT * FROM items WHERE archived = 0"
    params = []
    if item_ids:
        q += " AND id IN (%s)" % ",".join("?" * len(item_ids))
        params = list(item_ids)
    use_ai = ai.available()
    hits = 0
    for item in conn.execute(q, params).fetchall():
        fp = fingerprint(item)
        conn.execute("DELETE FROM recall_checks WHERE item_id = ? AND fingerprint != ? AND dismissed = 0",
                     (item["id"], fp))
        seen = {r[0] for r in conn.execute("SELECT recall_number FROM recall_checks WHERE item_id = ?",
                                           (item["id"],))}
        todo = [r for r in candidates(conn, item) if r["recall_number"] not in seen]
        if not todo:
            continue
        if use_ai:
            facts = {"Name": item["name"], "Brand": item["manufacturer"], "Model": item["model"],
                     "Description": item["description"], "Purchased": item["purchase_date"] or ""}
            try:
                verdicts = ai.judge_recalls(facts, [dict(r) for r in todo])
            except ai.AIError as exc:
                log.warning("recall judging failed for item %s: %s", item["id"], exc)
                continue
        else:  # no key: only a printed model-number match is worth flagging
            verdicts = {r["recall_number"]: ("possible", "The model number appears in this recall.")
                        for r in todo if len(squash(item["model"])) >= 3
                        and squash(item["model"]) in r["haystack"].replace(" ", "")}
        for r in todo:
            verdict, reason = verdicts.get(r["recall_number"], ("no", ""))
            conn.execute("INSERT OR REPLACE INTO recall_checks VALUES (?,?,?,?,?,0,?)",
                         (item["id"], r["recall_number"], fp, verdict, reason[:500], time.time()))
            if verdict in ("match", "possible"):
                hits += 1
        conn.commit()
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('recalls_checked', ?)", (str(time.time()),))
    conn.commit()
    return hits


def open_hits(conn, item_id=None):
    conn.executescript(SCHEMA)
    q = ("SELECT c.*, r.title, r.url, r.hazard, r.remedy, r.recall_date, i.name AS item_name "
         "FROM recall_checks c JOIN recalls r ON r.recall_number = c.recall_number "
         "JOIN items i ON i.id = c.item_id WHERE c.dismissed = 0 AND c.verdict IN ('match','possible') "
         "AND i.archived = 0")
    params = []
    if item_id:
        q += " AND c.item_id = ?"
        params.append(item_id)
    return conn.execute(q + " ORDER BY c.verdict = 'possible', r.recall_date DESC", params).fetchall()


def status(conn):
    conn.executescript(SCHEMA)
    meta = dict(conn.execute("SELECT k, v FROM meta").fetchall())
    n = conn.execute("SELECT COUNT(*) FROM recalls").fetchone()[0]
    return {"recalls": n, "synced": float(meta.get("recalls_synced", 0) or 0),
            "checked": float(meta.get("recalls_checked", 0) or 0), "error": meta.get("recalls_error", "")}


def run(db_path):
    """CLI and background entry point: sync then check. Records the last error for the UI."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    try:
        added = sync(conn)
        hits = check(conn)
        conn.execute("DELETE FROM meta WHERE k = 'recalls_error'")
        conn.commit()
        return added, hits
    except Exception as exc:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('recalls_error', ?)", (str(exc)[:300],))
        conn.commit()
        raise
    finally:
        conn.close()
