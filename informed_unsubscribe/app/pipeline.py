import hashlib
import json
import logging
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, classify, db, letters, mail, research

log = logging.getLogger(__name__)

MAX_CLASSIFY_ATTEMPTS = 3
UNCONFIRMED_NOTE = (
    "This might not be the sender's website, the email draft might be addressed to the wrong "
    "email. You can change the website and we will repeat the email search."
)
# Longest a run may take before it is given up on.
HELPER_TIMEOUT = 3600
# A failed website lookup is retried on later cycles, this many times in all.
MAX_LOOKUP_ATTEMPTS = 6

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_piece(cfg, image: bytes, subtype: str = "jpeg", sender: str = "", campaign: bool = False,
              photo: bool = False) -> int:
    """Store one picture of a mailpiece for a later run to read; 0 if it is already on file."""
    images_dir = Path(cfg.data_dir) / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(image).hexdigest()
    path = images_dir / f"{sha}.{'jpg' if subtype == 'jpeg' else subtype}"
    path.write_bytes(image)
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO pieces(sha, image_path, received, listed_sender, campaign, photo) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sha, str(path), now(), sender, int(campaign), int(photo)),
        )
    return cursor.rowcount


def ingest(cfg, raw: bytes) -> int:
    """Store the mailpiece scans from one email; classification happens later."""
    return sum(
        add_piece(cfg, piece.image, piece.subtype, piece.sender, piece.campaign)
        for piece in mail.extract_pieces(raw)
    )


def check_mailbox(cfg) -> int:
    """Take in any recent email not handled before."""
    if not cfg.email_address or not cfg.email_mailbox_password:
        raise mail.LoginRejected(mail.MISSING_LOGIN)

    def is_done(message_id: str) -> bool:
        with db.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone() is not None

    def handle(message_id: str, raw: bytes) -> None:
        ingest(cfg, raw)
        with db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO messages VALUES (?, ?)", (message_id, now()))

    return mail.poll(cfg, is_done, handle)


def known_sender(text: str, senders) -> int | None:
    """A sender already on file whose name appears anywhere on the piece (longest name wins)."""
    haystack = f" {db.normalize(text)} "
    for sender in sorted(senders, key=lambda s: -len(s["norm"])):
        if len(sender["norm"]) >= 5 and f" {sender['norm']} " in haystack:
            return sender["id"]
    return None


def classify_pending(cfg) -> None:
    """Read every piece not read yet, and every photo the user has turned since it was read.

    Reading a turned photo again replaces what the app had decided, but not the user's own
    answers: a category they chose, or the sender and addressee they corrected.
    """
    with db.connect() as conn:
        pending = conn.execute(
            "SELECT * FROM pieces WHERE category IS NULL OR reread = 1 ORDER BY id"
        ).fetchall()
        if not pending:
            return
    for piece in pending:
        try:
            image = Path(piece["image_path"]).read_bytes()
            scan = classify.look_closer(image, classify.read(image), cfg.physical_mail_address)
        except classify.ClassifyError as exc:
            log.warning("piece %s: %s", piece["id"], exc)
            with db.connect() as conn:
                if piece["reread"]:
                    # It was read before, so it keeps what that read found.
                    conn.execute(
                        "UPDATE pieces SET attempts = attempts + 1, "
                        "reread = attempts + 1 < ? WHERE id = ?",
                        (MAX_CLASSIFY_ATTEMPTS, piece["id"]),
                    )
                    continue
                conn.execute(
                    "UPDATE pieces SET attempts = attempts + 1, "
                    "category = CASE WHEN attempts + 1 >= ? THEN 'undetermined' END, "
                    "reason = ? WHERE id = ?",
                    (MAX_CLASSIFY_ATTEMPTS, "The scan could not be read.", piece["id"]),
                )
            continue
        text = scan.text
        domain = classify.find_domain(scan)
        with db.connect() as conn:
            names = classify.find_recipient(
                scan, cfg.physical_mail_address,
                (*cfg.resident_last_names, *cfg.previous_resident_last_names),
            )
            # The name USPS gives for a campaign beats anything read off the scan.
            sender_id = db.upsert_sender(conn, piece["listed_sender"]) or known_sender(
                text, conn.execute("SELECT id, norm FROM senders").fetchall()
            )
            if sender_id is None:
                name = classify.find_sender(scan, names) or domain
                sender_id = db.upsert_sender(conn, name)
            remembered = None
            if sender_id is not None:
                last = conn.execute(
                    "SELECT category FROM pieces WHERE sender_id = ? AND manual = 1 "
                    "ORDER BY id DESC LIMIT 1",
                    (sender_id,),
                ).fetchone()
                remembered = last["category"] if last else None
                if domain:
                    conn.execute(
                        "UPDATE senders SET website = ? WHERE id = ? AND website = ''",
                        (f"https://{domain}/", sender_id),
                    )
                phones = classify.find_phones(scan)
                if phones:
                    conn.execute(
                        "UPDATE senders SET phones = ? WHERE id = ? AND phones = '[]'",
                        (json.dumps(phones), sender_id),
                    )
                location = classify.find_return_location(scan, cfg.physical_mail_address)
                if location:
                    conn.execute(
                        "UPDATE senders SET location = ? WHERE id = ? AND location = ''",
                        (location, sender_id),
                    )
            verdict = classify.categorize(text, remembered, bool(piece["campaign"]))
            recipient = names[0] if names else ""
            resident = classify.check_resident(names, cfg)
            sender = conn.execute("SELECT name FROM senders WHERE id = ?", (sender_id,)).fetchone()
            conn.execute(
                "UPDATE pieces SET ocr_text = ?, auto_category = ?, auto_confidence = ?, "
                "auto_reason = ?, auto_rule = ?, auto_sender = ?, auto_recipient = ?, "
                "auto_resident = ?, app_version = ?, reread = 0 WHERE id = ?",
                (text, verdict.category, verdict.confidence, verdict.reason, verdict.rule,
                 sender["name"] if sender else "", recipient, resident, __version__, piece["id"]),
            )
            # Whatever the user has answered stays; the rest is the app's new reading.
            if not piece["manual"]:
                conn.execute(
                    "UPDATE pieces SET category = ?, confidence = ?, reason = ? WHERE id = ?",
                    (verdict.category, verdict.confidence, verdict.reason, piece["id"]),
                )
            if not piece["corrected_at"]:
                conn.execute(
                    "UPDATE pieces SET sender_id = ?, recipient = ?, resident = ? WHERE id = ?",
                    (sender_id, recipient, resident, piece["id"]),
                )
            # A request still in review that the new reading changed the facts of is written
            # again by the drafting step.
            if piece["reread"]:
                now_facts = conn.execute(
                    "SELECT sender_id, recipient, resident, category FROM pieces WHERE id = ?",
                    (piece["id"],),
                ).fetchone()
                if tuple(now_facts) != (piece["sender_id"], piece["recipient"],
                                        piece["resident"], piece["category"]):
                    conn.execute(
                        "DELETE FROM drafts WHERE piece_id = ? AND status = 'draft'",
                        (piece["id"],),
                    )


def lookups_waiting() -> bool:
    """Whether any sender that needs a request has never had its website looked up."""
    with db.connect() as conn:
        return conn.execute(
            "SELECT 1 FROM senders s JOIN pieces p ON p.sender_id = s.id "
            "WHERE (p.category = 'marketing' OR p.resident = 0) AND s.research_status = 'pending' "
            "LIMIT 1"
        ).fetchone() is not None


def research_pending(cfg) -> None:
    with db.connect() as conn:
        senders = conn.execute(
            "SELECT DISTINCT s.id, s.name, s.website, s.phones, s.location FROM senders s "
            "JOIN pieces p ON p.sender_id = s.id "
            "WHERE (p.category = 'marketing' OR p.resident = 0) AND (s.research_status = 'pending' "
            "OR (s.research_status = 'failed' AND s.research_attempts < ?))",
            (MAX_LOOKUP_ATTEMPTS,),
        ).fetchall()
    for sender in senders:
        status, found = "done", research.Result(sender["website"], [], False)
        try:
            found = research.research(
                sender["name"], sender["website"], phones=json.loads(sender["phones"]),
                location=sender["location"], home=classify.home_city(cfg.physical_mail_address),
                api_key=cfg.brave_search_api_key,
            )
            if not found.website:
                note = "No website found."
            elif not found.verified:
                note = UNCONFIRMED_NOTE
            elif not found.emails:
                note = "No email addresses found on the website."
            else:
                note = f"Confirmed: {found.reason}." if found.reason else ""
        except research.LookupFailed as exc:
            status, note = "failed", f"Lookup failed ({exc}). It will be tried again later."
            log.warning("research for %s failed: %s", sender["name"], exc)
        with db.connect() as conn:
            conn.execute(
                "UPDATE senders SET website = ?, emails = ?, verified = ?, candidates = ?, "
                "research_status = ?, research_note = ?, "
                "research_attempts = research_attempts + 1 WHERE id = ?",
                (found.website, json.dumps(found.emails), int(found.verified),
                 json.dumps(found.candidates), status, note, sender["id"]),
            )
            if found.emails:
                conn.execute(
                    "UPDATE drafts SET to_addr = ? "
                    "WHERE sender_id = ? AND status = 'draft' AND to_addr = ''",
                    (found.emails[0], sender["id"]),
                )
        if status == "failed":
            break  # the search is blocked or offline; leave the other senders for next time


def create_drafts(cfg) -> None:
    """Write a request for every piece that calls for one and has none yet.

    Each piece gets its own, so a sender who keeps sending is asked again. A request the user
    skipped still counts as that piece's request.
    """
    with db.connect() as conn:
        # Mail addressed to someone who doesn't live here gets a "moved" request, junk or not.
        rows = conn.execute(
            "SELECT * FROM ("
            "  SELECT s.id, s.name, s.emails, s.verified, s.website, p.id AS piece_id, "
            "  p.recipient, p.received, "
            "  CASE WHEN p.resident = 0 THEN 'moved' ELSE 'optout' END AS kind "
            "  FROM pieces p JOIN senders s ON s.id = p.sender_id "
            "  WHERE (p.resident = 0 AND p.recipient != '') "
            "     OR (p.resident IS NOT 0 AND p.category = 'marketing')) q "
            "WHERE NOT EXISTS (SELECT 1 FROM drafts d "
            "                  WHERE d.piece_id = q.piece_id AND d.kind = q.kind) "
            "ORDER BY q.piece_id"
        ).fetchall()
        for row in rows:
            moved = row["kind"] == "moved"
            _insert_draft(conn, row, row["kind"], row["recipient"] if moved else "", *letters.write(
                cfg, row["kind"], letters.values(
                    cfg, row["name"], row["recipient"], row["received"], row["website"],
                )))


def _insert_draft(conn, row, kind: str, recipient: str, subject: str, body: str) -> None:
    # The best address found is filled in even from a site that couldn't be tied to the
    # sender; the page says so beside the address, and nothing is sent without the user.
    emails = json.loads(row["emails"])
    conn.execute(
        "INSERT INTO drafts(sender_id, kind, recipient, piece_id, to_addr, subject, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (row["id"], kind, recipient, row["piece_id"], emails[0] if emails else "", subject, body),
    )


def prune_images(cfg) -> int:
    """Delete the scans of old pieces that no longer need looking at."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cfg.days_to_keep_scans)).isoformat(
        timespec="seconds"
    )
    with db.connect() as conn:
        old = conn.execute(
            "SELECT id, image_path FROM pieces WHERE image_path != '' AND received < ? "
            "AND category IS NOT NULL AND NOT (category = 'undetermined' AND manual = 0) "
            "AND id NOT IN (SELECT piece_id FROM drafts "
            "               WHERE status = 'draft' AND piece_id IS NOT NULL)",
            (cutoff,),
        ).fetchall()
        for piece in old:
            Path(piece["image_path"]).unlink(missing_ok=True)
            conn.execute("UPDATE pieces SET image_path = '' WHERE id = ?", (piece["id"],))
    return len(old)


def run_once(cfg) -> list[str]:
    """Do one full run and return what went wrong, one line per failed stage."""
    # Drafts are written before the website lookup so a piece's request exists as soon as it
    # has been read; the lookup then fills in the address.
    steps = [
        ("Mailbox", lambda: check_mailbox(cfg)),
        ("Classification", lambda: classify_pending(cfg)),
        ("Drafting", lambda: create_drafts(cfg)),
        ("Sender lookup", lambda: research_pending(cfg)),
        ("Cleanup", lambda: prune_images(cfg)),
    ]
    errors = []
    for label, step in steps:
        try:
            step()
        except Exception as exc:  # one failing stage must not stop the others
            log.exception("%s step failed", label)
            errors.append(f"{label}: {exc}")
    return errors


def run_in_helper(_cfg=None) -> list[str]:
    """Do a run in a separate process (see run.py) and return its errors.

    Reading scans loads an image library and builds up working memory that a long-lived
    process never gives back. A helper that exits after each run does, which keeps the app
    small between runs.
    """
    package_parent = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.run"], cwd=package_parent, stdout=subprocess.PIPE,
            timeout=HELPER_TIMEOUT,
        )
        return [str(error) for error in json.loads(result.stdout)]
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.error("the run did not finish: %s", exc)
        return ["The run stopped unexpectedly. The app's log has the details."]


def seconds_until_next_run(cfg, at: datetime | None = None) -> float:
    """How long the worker waits after a run: the interval, or until the daily time comes round."""
    if not cfg.run_at_time:
        return cfg.hours_between_runs * 3600
    at = at or datetime.now()
    hour, minute = map(int, cfg.run_at_time.split(":"))
    target = at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= at:
        target += timedelta(days=1)
    # Both are wall-clock times, so a clock change in between moves the wait, not the time of day.
    return target.timestamp() - at.timestamp()


class Worker(threading.Thread):
    """Runs the pipeline on a timer and whenever the page asks for it."""

    ACTIVITY = "Processing mail"

    def __init__(self, cfg, run=run_once):
        super().__init__(daemon=True, name="pipeline")
        self.cfg = cfg
        self.do_run = run
        self.wake = threading.Event()
        self.last_run = ""
        self.errors: list[str] = []
        self.activity = ""  # shown on the page while a run is going; "" when idle
        # While a run is going, the id of the newest piece from before it. The page shows
        # nothing newer until the run is over, so a run's results all arrive together.
        self.hide_after: int | None = None
        self.after_run = lambda: None  # called once a run's results are on the page

    def run(self) -> None:
        while True:
            self.wake.clear()
            self.cycle()
            self.wake.wait(seconds_until_next_run(self.cfg))

    def request_run(self) -> None:
        self.activity = self.ACTIVITY
        self.wake.set()

    def cycle(self) -> None:
        self.activity = self.ACTIVITY
        with db.connect() as conn:
            self.hide_after = conn.execute("SELECT COALESCE(MAX(id), 0) FROM pieces").fetchone()[0]
        try:
            self.errors = self.do_run(self.cfg)
        finally:
            self.hide_after = None
            self.last_run = now()
            self.activity = ""
        self.after_run()
