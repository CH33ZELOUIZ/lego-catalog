import csv
import io
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, flash, redirect, render_template, request, url_for

ASSET_DIR = Path(os.environ.get('LEGO_ASSET_DIR', '/app/static/cache'))
ASSET_DIR.mkdir(parents=True, exist_ok=True)
APP_PORT = int(os.environ.get("PORT", "3012"))
DB_PATH = os.environ.get("LEGO_DB_PATH", "/data/lego_catalog.db")
OVERRIDE_PATH = Path(os.environ.get("LEGO_OVERRIDE_PATH", "/data/lego_overrides.json"))
USER_AGENT = os.environ.get("LEGO_USER_AGENT", "LEGO Catalog self-hosted app (+https://github.com/your-user/lego-catalog)")
PHOTO_REVIEW_DIR = Path(os.environ.get("LEGO_PHOTO_REVIEW_DIR", "")) if os.environ.get("LEGO_PHOTO_REVIEW_DIR") else None

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_number TEXT NOT NULL,
                item_type TEXT NOT NULL CHECK(item_type IN ('set','minifig')),
                list_kind TEXT NOT NULL CHECK(list_kind IN ('owned','wanted')),
                quantity INTEGER NOT NULL DEFAULT 1,
                name TEXT,
                msrp TEXT,
                market_new TEXT,
                market_used TEXT,
                year_released TEXT,
                pieces TEXT,
                minifig_count TEXT,
                appears_in TEXT,
                image_url TEXT,
                source_set_number TEXT NOT NULL DEFAULT '',
                notes TEXT,
                inventory_version TEXT,
                includes_spares INTEGER DEFAULT 1,
                last_synced_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(item_number, item_type, list_kind, source_set_number)
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
        for column in ["year_released", "pieces", "minifig_count", "appears_in"]:
            if column not in columns:
                conn.execute(f"ALTER TABLE items ADD COLUMN {column} TEXT")
        if "verified" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN verified INTEGER DEFAULT 0")


def normalize_item_type(item_number: str) -> str:
    return "minifig" if item_number.lower().startswith("fig-") or item_number.lower().startswith("sw") else "set"


def clean_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def fetch_html(url: str) -> str:
    response = session.get(url, timeout=25)
    response.raise_for_status()
    return response.text


def usd_price_text(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    dollar = re.search(r"\$\s*([0-9][0-9,]*(?:\.\d{2})?)", value)
    if dollar:
        return f"${dollar.group(1)}"
    number = re.search(r"([0-9][0-9,]*(?:\.\d{2})?)", value)
    if not number:
        return ""
    return f"${number.group(1)}"


def parse_dt_value(soup: BeautifulSoup, label: str) -> str:
    dt = soup.find("dt", string=lambda s: s and label.lower() in s.lower())
    if not dt:
        return ""
    dd = dt.find_next("dd")
    return clean_text(dd.get_text(" ", strip=True)) if dd else ""


def load_overrides() -> dict:
    if not OVERRIDE_PATH.exists():
        return {}
    try:
        import json
        with OVERRIDE_PATH.open('r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def apply_override(item_number: str, item_type: str, metadata: dict) -> dict:
    overrides = load_overrides()
    key = f"{item_type}:{item_number}"
    override = overrides.get(key) or overrides.get(item_number) or {}
    if not isinstance(override, dict):
        return metadata
    merged = dict(metadata)
    for field in ["name", "msrp", "market_new", "market_used", "year_released", "pieces", "minifig_count", "appears_in", "image_url"]:
        value = clean_text(override.get(field, ""))
        if value:
            merged[field] = value
    return merged


def download_image(url: str, prefix: str) -> str:
    if not url:
        return ""
    try:
        ext = os.path.splitext(url.split("?", 1)[0])[1].lower() or ".jpg"
        if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            ext = ".jpg"
        local_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", prefix)[:80] + ext
        local_path = ASSET_DIR / local_name
        if not local_path.exists():
            resp = session.get(url, timeout=25)
            resp.raise_for_status()
            local_path.write_bytes(resp.content)
        return f"/static/cache/{local_name}"
    except Exception:
        return url


def duckduckgo_first_result(query: str) -> str:
    try:
        html = fetch_html("https://html.duckduckgo.com/html/?q=" + quote(query))
    except Exception:
        return ""
    m = re.search(r'result__a[^>]*href="[^"]+">(.*?)</a>', html, re.I | re.S)
    if not m:
        return ""
    text = re.sub(r"<.*?>", " ", m.group(1))
    return clean_text(text)


def bing_first_result_title(query: str, needle: str = "") -> str:
    try:
        html = fetch_html("https://www.bing.com/search?q=" + quote(query))
    except Exception:
        return ""
    blocks = re.findall(r'<li class="b_algo".*?</li>', html, re.I | re.S)
    for block in blocks:
        title_match = re.search(r'<h2 class=""><a[^>]*>(.*?)</a></h2>', block, re.I | re.S)
        if not title_match:
            continue
        title = clean_text(re.sub(r"<.*?>", " ", title_match.group(1)))
        if needle and needle.lower() not in title.lower() and needle.lower() not in block.lower():
            continue
        if title:
            return title
    return ""


def best_public_title(query: str, needle: str = "") -> str:
    for finder in (bing_first_result_title, duckduckgo_first_result):
        title = finder(query, needle) if finder is bing_first_result_title else finder(query)
        if title:
            return title
    return ""


def scrape_set(set_number: str):
    url = f"https://brickset.com/sets/{quote(set_number)}"
    try:
        html = fetch_html(url)
    except Exception:
        return apply_override(set_number, "set", {
            "item_number": set_number,
            "item_type": "set",
            "name": set_number,
            "msrp": "",
            "market_new": "",
            "market_used": "",
            "image_url": "",
            "source_url": url,
        })

    def grab(pattern, default=""):
        m = re.search(pattern, html, re.I | re.S)
        return clean_text(m.group(1)) if m else default

    title = grab(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']')
    if not title:
        title = grab(r'<title>(.*?)</title>', set_number)
    title = re.sub(r"\s*\|\s*Brickset.*$", "", title)
    title = re.sub(r"^LEGO\s+", "", title, flags=re.I)
    title = re.sub(r"^" + re.escape(set_number) + r"\s*", "", title).strip(" -")
    if not title or re.search(r"access denied|cloudflare", title, re.I):
        title = best_public_title(f'"{set_number}" LEGO', set_number) or set_number

    image = grab(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']') or grab(r'<meta\s+name=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']')
    if not image:
        image = f"https://images.brickset.com/sets/images/{set_number}.jpg"
    year = grab(r'<dt>\s*Year released\s*</dt>\s*<dd>.*?>(\d{4})<', "")
    pieces = grab(r'<dt>\s*Pieces\s*</dt>\s*<dd>([^<]+)</dd>', "")
    minifigs = grab(r'<dt>\s*Minifigs\s*</dt>\s*<dd>([^<]+)</dd>', "")
    rrp = grab(r'<dt>\s*(?:RRP|Retail price)\s*</dt>\s*<dd>([^<]+)</dd>', "")
    current = grab(r'<dt>\s*Current value\s*</dt>\s*<dd>([^<]+)</dd>', "")
    market_new = ""
    market_used = ""
    if current:
        new_match = re.search(r"New:\s*([^U]+?)(?:Used:|$)", current)
        used_match = re.search(r"Used:\s*(.+)$", current)
        market_new = usd_price_text(clean_text(new_match.group(1)) if new_match else current)
        market_used = usd_price_text(clean_text(used_match.group(1)) if used_match else "")

    image_url = download_image(image, f"set_{set_number}")
    metadata = {
        "item_number": set_number,
        "item_type": "set",
        "name": title,
        "msrp": usd_price_text(rrp),
        "market_new": market_new,
        "market_used": market_used,
        "year_released": year,
        "pieces": pieces,
        "minifig_count": minifigs,
        "image_url": image_url,
        "source_url": url,
    }
    return apply_override(set_number, "set", metadata)


def scrape_set_minifigs(set_number: str):
    url = f"https://brickset.com/minifigs/in-{quote(set_number)}"
    try:
        soup = BeautifulSoup(fetch_html(url), "html.parser")
    except Exception:
        return []

    results = []
    seen = set()
    for anchor in soup.select("a[href^='/minifigs/']"):
        href = anchor.get("href", "")
        match = re.match(r"/minifigs/([A-Za-z0-9-]+)/", href)
        if not match:
            continue
        code = match.group(1)
        if code.startswith("in-") or code in seen:
            continue
        seen.add(code)
        name = clean_text(anchor.get_text(" ", strip=True))
        img = anchor.find("img")
        title_attr = img.get("title", "") if img else ""
        if not name:
            alt = img.get("alt", "") if img else ""
            payload = clean_text(alt or title_attr)
            name = re.sub(r"^.*?</h1>", "", payload)
            name = re.sub(r"<.*?>", " ", name)
            name = clean_text(name)
            if ":" in title_attr:
                name = clean_text(title_attr.split(":", 1)[1])
        if not name or name == code:
            name = best_public_title(f'"{code}" LEGO minifig', code) or name or code
        image = ""
        if img:
            image = img.get("src", "")
        if not image:
            image = f"https://images.brickset.com/minifigs/images/{code}.jpg"
        results.append({
            "item_number": code,
            "item_type": "minifig",
            "name": name or code,
            "image_url": image,
        })
    return results


def scrape_minifig(minifig_number: str):
    url = f"https://brickset.com/minifigs/{quote(minifig_number)}"
    try:
        soup = BeautifulSoup(fetch_html(url), "html.parser")
    except Exception:
        return apply_override(minifig_number, "minifig", {
            "item_number": minifig_number,
            "item_type": "minifig",
            "name": minifig_number,
            "msrp": "",
            "market_new": "",
            "market_used": "",
            "image_url": "",
            "source_url": url,
        })

    title = clean_text(soup.title.get_text()) if soup.title else minifig_number
    title = re.sub(r"\s*\|\s*Brickset.*$", "", title)
    title = re.sub(r"^LEGO minifigures\s+" + re.escape(minifig_number) + r":\s*", "", title, flags=re.I).strip()
    if not title or re.search(r"access denied|cloudflare", title, re.I):
        title = best_public_title(f'"{minifig_number}" LEGO minifig', minifig_number) or minifig_number
    current = parse_dt_value(soup, "Current value")
    new_match = re.search(r"New:\s*(.+?)(?:Used:|$)", current)
    used_match = re.search(r"Used:\s*(.+)$", current)
    image = soup.select_one("meta[property='og:image']")
    year = parse_dt_value(soup, "Year released")
    appears = parse_dt_value(soup, "Appears in")
    image_url = download_image(image.get("content", "") if image else "", f"minifig_{minifig_number}")
    metadata = {
        "item_number": minifig_number,
        "item_type": "minifig",
        "name": title or minifig_number,
        "msrp": "",
        "market_new": usd_price_text(clean_text(new_match.group(1)) if new_match else ""),
        "market_used": usd_price_text(clean_text(used_match.group(1)) if used_match else ""),
        "year_released": year,
        "appears_in": appears,
        "image_url": image_url,
        "source_url": url,
    }
    return apply_override(minifig_number, "minifig", metadata)


def scrape_item(item_number: str, item_type: str):
    return scrape_minifig(item_number) if item_type == "minifig" else scrape_set(item_number)


def upsert_item(item_number, item_type, list_kind, quantity=1, metadata=None, source_set_number=None, inventory_version="", includes_spares=True, notes=""):
    metadata = metadata or {}
    item_number = clean_text(item_number)
    now = now_iso()
    with db() as conn:
        current = conn.execute(
            """
            SELECT * FROM items
            WHERE item_number = ? AND item_type = ? AND list_kind = ? AND source_set_number = ?
            """,
            (item_number, item_type, list_kind, source_set_number or ""),
        ).fetchone()
        if current:
            conn.execute(
                """
                UPDATE items
                SET quantity = quantity + ?,
                    name = COALESCE(NULLIF(?, ''), name),
                    msrp = COALESCE(NULLIF(?, ''), msrp),
                    market_new = COALESCE(NULLIF(?, ''), market_new),
                    market_used = COALESCE(NULLIF(?, ''), market_used),
                    image_url = COALESCE(NULLIF(?, ''), image_url),
                    inventory_version = COALESCE(NULLIF(?, ''), inventory_version),
                    includes_spares = ?,
                    notes = CASE WHEN ? != '' THEN ? ELSE notes END,
                    last_synced_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    int(quantity),
                    metadata.get("name", ""),
                    metadata.get("msrp", ""),
                    metadata.get("market_new", ""),
                    metadata.get("market_used", ""),
                    metadata.get("image_url", ""),
                    inventory_version,
                    1 if includes_spares else 0,
                    notes,
                    notes,
                    now,
                    now,
                    current["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO items (
                    item_number, item_type, list_kind, quantity, name, msrp, market_new, market_used,
                    image_url, source_set_number, notes, inventory_version, includes_spares,
                    last_synced_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_number,
                    item_type,
                    list_kind,
                    int(quantity),
                    metadata.get("name", ""),
                    metadata.get("msrp", ""),
                    metadata.get("market_new", ""),
                    metadata.get("market_used", ""),
                    metadata.get("image_url", ""),
                    source_set_number or "",
                    notes,
                    inventory_version,
                    1 if includes_spares else 0,
                    now,
                    now,
                    now,
                ),
            )


def replace_quantity(item_id: int, quantity: int):
    with db() as conn:
        if quantity <= 0:
            conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        else:
            conn.execute("UPDATE items SET quantity = ?, updated_at = ? WHERE id = ?", (quantity, now_iso(), item_id))


def delete_item(item_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        if row["item_type"] == "set":
            conn.execute(
                "DELETE FROM items WHERE source_set_number = ? AND list_kind = ?",
                (row["item_number"], row["list_kind"]),
            )


def get_items(query="", list_kind="all", item_type="all"):
    sql = "SELECT * FROM items WHERE 1=1"
    params = []
    if query:
        sql += " AND (item_number LIKE ? OR name LIKE ? OR COALESCE(source_set_number, '') LIKE ?)"
        like = f"%{query}%"
        params += [like, like, like]
    if list_kind != "all":
        sql += " AND list_kind = ?"
        params.append(list_kind)
    if item_type != "all":
        sql += " AND item_type = ?"
        params.append(item_type)
    sql += " ORDER BY list_kind, item_type, COALESCE(NULLIF(name,''), item_number) COLLATE NOCASE, item_number COLLATE NOCASE"
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["msrp"] = usd_price_text(item.get("msrp", ""))
        item["market_new"] = usd_price_text(item.get("market_new", ""))
        item["market_used"] = usd_price_text(item.get("market_used", ""))
        out.append(item)
    return out


def backfill_missing_metadata(items):
    refreshed = []
    with db() as conn:
        for item in items:
            if item.get("name") and item.get("image_url") and (item.get("year_released") or item.get("item_type") == "minifig"):
                continue
            try:
                metadata = scrape_item(item["item_number"], item["item_type"])
            except Exception:
                continue
            updates = {
                "name": metadata.get("name") or item.get("name") or item["item_number"],
                "msrp": metadata.get("msrp") or item.get("msrp") or "",
                "market_new": metadata.get("market_new") or item.get("market_new") or "",
                "market_used": metadata.get("market_used") or item.get("market_used") or "",
                "year_released": metadata.get("year_released") or item.get("year_released") or "",
                "pieces": metadata.get("pieces") or item.get("pieces") or "",
                "minifig_count": metadata.get("minifig_count") or item.get("minifig_count") or "",
                "appears_in": metadata.get("appears_in") or item.get("appears_in") or "",
                "image_url": metadata.get("image_url") or item.get("image_url") or "",
                "updated_at": now_iso(),
            }
            conn.execute(
                """
                UPDATE items
                SET name = ?, msrp = ?, market_new = ?, market_used = ?, year_released = ?, pieces = ?,
                    minifig_count = ?, appears_in = ?, image_url = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updates["name"], updates["msrp"], updates["market_new"], updates["market_used"],
                    updates["year_released"], updates["pieces"], updates["minifig_count"], updates["appears_in"],
                    updates["image_url"], updates["updated_at"], item["id"],
                ),
            )
            item.update(updates)
            refreshed.append(item["item_number"])
    return refreshed


def get_summary():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT list_kind, item_type, COUNT(*) AS rows_count, COALESCE(SUM(quantity), 0) AS qty_total
            FROM items
            GROUP BY list_kind, item_type
            """
        ).fetchall()
    summary = {"owned": {"set": 0, "minifig": 0}, "wanted": {"set": 0, "minifig": 0}}
    for row in rows:
        summary[row["list_kind"]][row["item_type"]] = row["qty_total"]
    return summary


def get_review_items(limit=200):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM items
            WHERE list_kind = 'owned' AND item_type = 'minifig'
              AND (
                COALESCE(notes, '') LIKE '%Inferred best-effort match%'
                OR COALESCE(notes, '') LIKE '%low-light reference photos%'
                OR COALESCE(name, '') = ''
                OR COALESCE(image_url, '') = ''
                OR COALESCE(verified, 0) = 0
              )
            ORDER BY COALESCE(verified, 0) ASC,
                     CASE WHEN COALESCE(notes, '') LIKE '%Inferred best-effort match%' THEN 0 ELSE 1 END,
                     COALESCE(year_released, '') DESC,
                     COALESCE(name, item_number)
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return rows


def extraction_candidates_from_text():
    candidates = []
    base = PHOTO_REVIEW_DIR
    if not base or not base.exists():
        return candidates
    for path in sorted(base.glob('*.HEIC')):
        candidates.append({
            'file': path.name,
            'url': f'/review/extract/{path.name}',
        })
    return candidates


@app.route("/")
def index():
    q = clean_text(request.args.get("q", ""))
    list_kind = request.args.get("list_kind", "all")
    item_type = request.args.get("item_type", "all")
    items = get_items(q, list_kind, item_type)
    backfill_missing_metadata(items[:15])
    items = get_items(q, list_kind, item_type)
    return render_template("index.html", items=items, summary=get_summary(), q=q, list_kind=list_kind, item_type=item_type)


@app.get("/review")
def review_page():
    items = get_review_items()
    return render_template("review.html", items=items, photo_candidates=extraction_candidates_from_text())


@app.post("/review/refresh")
def review_refresh():
    items = get_review_items(limit=500)
    refreshed = []
    for item in items:
        metadata = scrape_item(item["item_number"], item["item_type"])
        with db() as conn:
            conn.execute(
                """
                UPDATE items
                SET name = COALESCE(NULLIF(?, ''), name),
                    msrp = COALESCE(NULLIF(?, ''), msrp),
                    market_new = COALESCE(NULLIF(?, ''), market_new),
                    market_used = COALESCE(NULLIF(?, ''), market_used),
                    year_released = COALESCE(NULLIF(?, ''), year_released),
                    pieces = COALESCE(NULLIF(?, ''), pieces),
                    minifig_count = COALESCE(NULLIF(?, ''), minifig_count),
                    appears_in = COALESCE(NULLIF(?, ''), appears_in),
                    image_url = COALESCE(NULLIF(?, ''), image_url),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    metadata.get("name", ""),
                    metadata.get("msrp", ""),
                    metadata.get("market_new", ""),
                    metadata.get("market_used", ""),
                    metadata.get("year_released", ""),
                    metadata.get("pieces", ""),
                    metadata.get("minifig_count", ""),
                    metadata.get("appears_in", ""),
                    metadata.get("image_url", ""),
                    now_iso(),
                    item["id"],
                ),
            )
        refreshed.append(item["item_number"])
    flash(f"Refreshed {len(refreshed)} review items.", "success")
    return redirect(url_for("review_page"))


@app.get("/review/extract/<path:filename>")
def review_extract(filename):
    # Best-effort decode of HEIC sources for manual review.
    # These files often contain many embedded streams; the visible photo is usually the largest stream.
    
    if not PHOTO_REVIEW_DIR:
        return "Photo review directory not configured", 404
    src = PHOTO_REVIEW_DIR / filename
    if not src.exists():
        return "Missing file", 404
    tmp = ASSET_DIR / f'review_{src.stem}.jpg'
    import json
    import subprocess
    probe = subprocess.check_output([
        'ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', str(src)
    ])
    info = json.loads(probe)
    video_streams = [s for s in info.get('streams', []) if s.get('codec_type') == 'video']
    if not video_streams:
        return "No video streams found", 500
    best = max(video_streams, key=lambda s: int(s.get('width', 0)) * int(s.get('height', 0)))
    stream_spec = f'0:v:{best["index"]}'
    cmd = [
        'ffmpeg', '-y', '-i', str(src), '-map', stream_spec,
        '-vf', 'format=yuv420p,eq=brightness=0.0:contrast=1.0:saturation=1.0',
        '-frames:v', '1', '-update', '1', str(tmp)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return redirect(url_for('static', filename=f'cache/{tmp.name}'))


@app.post("/add")
def add_item():
    item_number = clean_text(request.form.get("item_number", ""))
    item_type = request.form.get("item_type", "auto")
    list_kind = request.form.get("list_kind", "owned")
    quantity = int(request.form.get("quantity", "1") or 1)
    notes = clean_text(request.form.get("notes", ""))
    if not item_number:
        flash("Item number is required.", "error")
        return redirect(url_for("index"))
    if item_type == "auto":
        item_type = normalize_item_type(item_number)
    metadata = scrape_item(item_number, item_type)
    upsert_item(item_number, item_type, list_kind, quantity=quantity, metadata=metadata, notes=notes)
    if item_type == "set":
        for fig in scrape_set_minifigs(item_number):
            fig_meta = scrape_minifig(fig["item_number"])
            fig_meta["name"] = fig_meta.get("name") or fig.get("name")
            fig_meta["image_url"] = fig_meta.get("image_url") or fig.get("image_url")
            upsert_item(fig["item_number"], "minifig", list_kind, quantity=quantity, metadata=fig_meta, source_set_number=item_number)
    flash(f"Added {item_number} to {list_kind}.", "success")
    return redirect(url_for("index"))


@app.post("/import")
def import_csv():
    upload = request.files.get("csv_file")
    if not upload or not upload.filename:
        flash("Choose a CSV file first.", "error")
        return redirect(url_for("index"))
    text = upload.read().decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(text))
    count = 0
    for row in reader:
        item_number = clean_text(row.get("Set Number", ""))
        if not item_number:
            continue
        quantity = int(clean_text(row.get("Quantity", "1") or "1") or 1)
        inventory_version = clean_text(row.get("Inventory Ver", ""))
        includes_spares = clean_text(row.get("Includes Spares", "True")).lower() in {"1", "true", "yes"}
        item_type = normalize_item_type(item_number)
        metadata = scrape_item(item_number, item_type)
        upsert_item(
            item_number,
            item_type,
            "owned",
            quantity=quantity,
            metadata=metadata,
            inventory_version=inventory_version,
            includes_spares=includes_spares,
        )
        if item_type == "set":
            for fig in scrape_set_minifigs(item_number):
                fig_meta = scrape_minifig(fig["item_number"])
                fig_meta["name"] = fig_meta.get("name") or fig.get("name")
                fig_meta["image_url"] = fig_meta.get("image_url") or fig.get("image_url")
                upsert_item(
                    fig["item_number"],
                    "minifig",
                    "owned",
                    quantity=quantity,
                    metadata=fig_meta,
                    source_set_number=item_number,
                    inventory_version=inventory_version,
                    includes_spares=includes_spares,
                )
        count += 1
    flash(f"Imported {count} top-level rows from CSV.", "success")
    return redirect(url_for("index"))


@app.get("/export/owned.csv")
def export_owned():
    with db() as conn:
        rows = conn.execute(
            "SELECT item_number, quantity, includes_spares, inventory_version FROM items WHERE list_kind = 'owned' AND source_set_number = '' ORDER BY item_number"
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Set Number", "Quantity", "Includes Spares", "Inventory Ver"])
    for row in rows:
        writer.writerow([row["item_number"], row["quantity"], "True" if row["includes_spares"] else "False", row["inventory_version"] or "1"])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=lego-owned-export.csv"})


@app.post("/item/<int:item_id>/quantity")
def update_quantity(item_id):
    quantity = int(request.form.get("quantity", "1") or 1)
    replace_quantity(item_id, quantity)
    flash("Quantity updated.", "success")
    return redirect(url_for("index"))


@app.post("/item/<int:item_id>/notes")
def update_notes(item_id):
    notes = clean_text(request.form.get("notes", ""))
    with db() as conn:
        conn.execute("UPDATE items SET notes = ?, updated_at = ? WHERE id = ?", (notes, now_iso(), item_id))
    flash("Notes updated.", "success")
    return redirect(url_for("index"))


@app.post("/item/<int:item_id>/delete")
def remove_item(item_id):
    delete_item(item_id)
    flash("Item removed.", "success")
    return redirect(url_for("index"))


@app.post("/item/<int:item_id>/refresh")
def refresh_item(item_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            flash("Item not found.", "error")
            return redirect(url_for("index"))
        metadata = scrape_item(row["item_number"], row["item_type"])
        conn.execute(
            """
            UPDATE items
            SET name = ?, msrp = ?, market_new = ?, market_used = ?, image_url = ?, last_synced_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                metadata.get("name", row["name"]),
                metadata.get("msrp", row["msrp"]),
                metadata.get("market_new", row["market_new"]),
                metadata.get("market_used", row["market_used"]),
                metadata.get("image_url", row["image_url"]),
                now_iso(),
                now_iso(),
                item_id,
            ),
        )
    flash("Pricing/details refreshed.", "success")
    return redirect(url_for("index"))


@app.post("/item/<int:item_id>/verify")
def verify_item(item_id):
    with db() as conn:
        conn.execute("UPDATE items SET verified = 1, updated_at = ? WHERE id = ?", (now_iso(), item_id))
    flash("Minifig verified.", "success")
    return redirect(request.referrer or url_for("review_page"))


@app.post("/item/<int:item_id>/unverify")
def unverify_item(item_id):
    with db() as conn:
        conn.execute("UPDATE items SET verified = 0, updated_at = ? WHERE id = ?", (now_iso(), item_id))
    flash("Minifig unverified.", "success")
    return redirect(request.referrer or url_for("review_page"))


@app.get("/item/<int:item_id>")
def item_detail(item_id):
    with db() as conn:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not item:
            flash("Item not found.", "error")
            return redirect(url_for("index"))
        if not item["name"] or not item["image_url"] or (item["item_type"] == "set" and not item["year_released"]):
            metadata = scrape_item(item["item_number"], item["item_type"])
            conn.execute(
                """
                UPDATE items
                SET name = COALESCE(NULLIF(?, ''), name),
                    msrp = COALESCE(NULLIF(?, ''), msrp),
                    market_new = COALESCE(NULLIF(?, ''), market_new),
                    market_used = COALESCE(NULLIF(?, ''), market_used),
                    year_released = COALESCE(NULLIF(?, ''), year_released),
                    pieces = COALESCE(NULLIF(?, ''), pieces),
                    minifig_count = COALESCE(NULLIF(?, ''), minifig_count),
                    appears_in = COALESCE(NULLIF(?, ''), appears_in),
                    image_url = COALESCE(NULLIF(?, ''), image_url),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    metadata.get("name", ""),
                    metadata.get("msrp", ""),
                    metadata.get("market_new", ""),
                    metadata.get("market_used", ""),
                    metadata.get("year_released", ""),
                    metadata.get("pieces", ""),
                    metadata.get("minifig_count", ""),
                    metadata.get("appears_in", ""),
                    metadata.get("image_url", ""),
                    now_iso(),
                    item_id,
                ),
            )
            item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if item["item_type"] == "set":
            related = conn.execute("SELECT * FROM items WHERE source_set_number = ? ORDER BY item_type, COALESCE(NULLIF(name,''), item_number)", (item["item_number"],)).fetchall()
        else:
            related = []
    return render_template("detail.html", item=item, related=related)


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=APP_PORT, debug=False)
