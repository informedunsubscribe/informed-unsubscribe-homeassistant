import difflib
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS senders(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    norm TEXT NOT NULL UNIQUE,
    website TEXT NOT NULL DEFAULT '',
    emails TEXT NOT NULL DEFAULT '[]',
    -- evidence read off the mailpieces, used to find and confirm the website
    phones TEXT NOT NULL DEFAULT '[]',
    location TEXT NOT NULL DEFAULT '',
    -- 1 when the website is tied to the sender (printed, typed by the user, or confirmed)
    verified INTEGER NOT NULL DEFAULT 0,
    candidates TEXT NOT NULL DEFAULT '[]',
    research_status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
    research_note TEXT NOT NULL DEFAULT '',
    research_attempts INTEGER NOT NULL DEFAULT 0
);
-- Emails already taken from the mailbox, so each is handled once without touching the mailbox.
CREATE TABLE IF NOT EXISTS messages(
    message_id TEXT PRIMARY KEY,
    handled_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pieces(
    id INTEGER PRIMARY KEY,
    sha TEXT NOT NULL UNIQUE,
    image_path TEXT NOT NULL,
    received TEXT NOT NULL,
    listed_sender TEXT NOT NULL DEFAULT '',  -- advertiser name given in the digest
    campaign INTEGER NOT NULL DEFAULT 0,
    -- 1 when the user took the picture. It may show more than the mailpiece, so attaching it
    -- to a request is off unless the user turns it on.
    photo INTEGER NOT NULL DEFAULT 0,
    -- 1 once the user has turned their photo, until a run has read it again
    reread INTEGER NOT NULL DEFAULT 0,
    sender_id INTEGER REFERENCES senders(id),
    category TEXT,  -- NULL until classified; marketing | real | undetermined
    confidence REAL,
    reason TEXT NOT NULL DEFAULT '',
    ocr_text TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL DEFAULT '',
    resident INTEGER,  -- 1 lives here, 0 does not, NULL unknown or check turned off
    manual INTEGER NOT NULL DEFAULT 0,  -- 1 when the user set the category
    -- What the app decided on its own, kept unchanged when the user corrects a piece so the
    -- two can be compared later.
    auto_category TEXT,
    auto_confidence REAL,
    auto_reason TEXT NOT NULL DEFAULT '',
    auto_rule TEXT NOT NULL DEFAULT '',
    auto_sender TEXT NOT NULL DEFAULT '',
    auto_recipient TEXT NOT NULL DEFAULT '',
    auto_resident INTEGER,
    app_version TEXT NOT NULL DEFAULT '',
    corrected_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS drafts(
    id INTEGER PRIMARY KEY,
    sender_id INTEGER NOT NULL REFERENCES senders(id),
    kind TEXT NOT NULL DEFAULT 'optout',  -- optout | moved (addressee does not live here)
    recipient TEXT NOT NULL DEFAULT '',  -- the addressee, for moved drafts
    piece_id INTEGER REFERENCES pieces(id),
    to_addr TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    -- draft | sent | skipped (set aside by the user; newer mail from the sender reopens the
    -- question). The user can reopen a sent or skipped request from History.
    status TEXT NOT NULL DEFAULT 'draft',
    sent_at TEXT,
    error TEXT NOT NULL DEFAULT ''
);
"""

_path: Path | None = None


def init(data_dir: str) -> None:
    global _path
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    _path = Path(data_dir) / "informed_unsubscribe.db"
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect():
    conn = sqlite3.connect(_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


SAME_SENDER_SIMILARITY = 0.88
_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "the"}


def normalize(name: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", "", name.lower().replace("&", " and ")).split()
    return " ".join(w for w in words if w not in _SUFFIXES)


def upsert_sender(conn, name: str, merge_similar: bool = True) -> int | None:
    name = " ".join(name.split())
    norm = normalize(name)
    if not norm:
        return None
    known = {row["norm"]: row["id"] for row in conn.execute("SELECT id, norm FROM senders")}
    if norm in known:
        return known[norm]
    # OCR misreads a letter or two, so treat near-identical names as the same sender.
    close = merge_similar and difflib.get_close_matches(
        norm, list(known), n=1, cutoff=SAME_SENDER_SIMILARITY
    )
    if close:
        return known[close[0]]
    return conn.execute("INSERT INTO senders(name, norm) VALUES (?, ?)", (name, norm)).lastrowid
