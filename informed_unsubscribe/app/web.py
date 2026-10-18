import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, urlparse

from jinja2 import Environment, FileSystemLoader

from . import __version__, classify, db, letters, mail, notify, pipeline

PORT = 8099
# Inside Home Assistant the UI is only served through ingress, which authenticates the user.
INGRESS_PROXY = "172.30.32.2"
MAX_FORM_BYTES = 1_000_000
MAX_PHOTO_BYTES = 8_000_000  # the page shrinks a photo to about a tenth of this
HISTORY_PAGE = 50
MAX_UNSURE = 30  # undecided pieces shown in For Review at once
FEEDBACK_EMAIL = "informedunsubscribe@gmail.com"
# The author's referral link, shown in the page footer.
REFERRAL_URL = "https://refer.virtualpostmail.com/l/1KHIYEPRR/vpmmail"
ATTACHED_NOTE = "\n\nA {} of the mailpiece is attached.\n"  # "scan" or "photo"
ADDRESS_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
IMAGE_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".gif": "image/gif"}

templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=True
)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def short_date(timestamp: str) -> str:
    """ "2026-10-20T09:15:00+00:00" as "2026 Oct 20"."""
    year, month, day = timestamp[:10].split("-")
    return f"{year} {_MONTHS[int(month) - 1]} {int(day)}"


templates.filters["short_date"] = short_date


class Download(NamedTuple):
    body: bytes
    content_type: str
    filename: str


def _app_id() -> str:
    """This app's id in Home Assistant, or "" when not running under it.

    Home Assistant names the container after the app's id with dashes for underscores
    ("local-informed-unsubscribe"), which is the only place the app can see its own id.
    """
    if not os.environ.get("SUPERVISOR_TOKEN"):
        return ""
    return os.environ.get("HOSTNAME", "").replace("-", "_")


def settings_url() -> str:
    """Home Assistant's Configuration page for this app, or "" when not running under it."""
    return f"/config/app/{_app_id()}/config" if _app_id() else ""


def app_url() -> str:
    """Where Home Assistant shows this app's page, or "" when not running under it."""
    return f"/app/{_app_id()}" if _app_id() else ""


# The request written for a piece, of the kind the piece calls for.
_REQUEST_FOR_PIECE = (
    "(SELECT d.id FROM drafts d WHERE d.piece_id = p.id AND ("
    "   (p.resident = 0 AND d.kind = 'moved') OR "
    "   (p.resident IS NOT 0 AND p.category = 'marketing' AND d.kind = 'optout')) "
    " ORDER BY d.id DESC LIMIT 1)"
)


# What the page shows about a piece, wherever it is listed (p is the piece, s its sender).
_FACTS = (
    "p.id AS piece_id, s.name AS sender_name, p.recipient AS addressee, p.resident, p.category, "
    "p.reason, COALESCE(p.image_path, '') != '' AS has_image, p.photo, p.reread, s.website, "
    "s.verified, "
    "s.research_status, s.research_note"
)

# Pieces with no request yet because the app couldn't settle them: no verdict, or a verdict that
# calls for a request but no readable sender to send it to. Anything the user has already
# answered drops out.
_UNSURE = (
    "((p.category = 'undetermined' AND p.manual = 0 "
    "  AND (p.resident IS NOT 0 OR p.sender_id IS NULL)) "
    " OR (p.sender_id IS NULL AND p.corrected_at IS NULL "
    "     AND (p.category = 'marketing' OR COALESCE(p.resident, 1) = 0)))"
)

# A request for new mail stays out of For Review until its sender's website has been looked up
# once, so it arrives complete. That never applies to a piece the user has corrected (`piece`
# is its alias in the query): its card stays put and shows that it is being reprocessed.
def _being_prepared(piece: str) -> str:
    return (f"(s.research_status = 'pending' AND s.research_attempts = 0 "
            f"AND {piece}.corrected_at IS NULL)")


def review_count(server) -> int:
    """How many cards For Review shows right now."""
    newest = server.worker.hide_after if server.worker.hide_after is not None else sys.maxsize
    with db.connect() as conn:
        requests = conn.execute(
            "SELECT COUNT(*) FROM drafts d JOIN senders s ON s.id = d.sender_id "
            "LEFT JOIN pieces p ON p.id = d.piece_id "
            f"WHERE d.status = 'draft' AND NOT {_being_prepared('p')} "
            "AND COALESCE(d.piece_id, 0) <= ?",
            (newest,),
        ).fetchone()[0]
        unsure = conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM pieces p "
            f"LEFT JOIN senders s ON s.id = p.sender_id WHERE {_UNSURE} AND p.id <= ? "
            f"LIMIT {MAX_UNSURE})",
            (newest,),
        ).fetchone()[0]
    return requests + unsure


def announce(server) -> None:
    """Bring Home Assistant's notification in line with what is waiting in For Review."""
    notify.review(review_count(server), server.cfg.notify_when_mail_to_review, app_url())


def index(server, query) -> str:
    page = max(1, int(query.get("page") or 1))
    sender = query.get("sender", "").strip()
    # Pieces taken in by a run that is still going stay off the page until it finishes.
    newest = server.worker.hide_after if server.worker.hide_after is not None else sys.maxsize
    with db.connect() as conn:
        drafts = [
            dict(row, emails=json.loads(row["emails"]), candidates=json.loads(row["candidates"]))
            for row in conn.execute(
                f"SELECT d.*, s.name, s.emails, s.candidates, {_FACTS} "
                "FROM drafts d JOIN senders s ON s.id = d.sender_id "
                "LEFT JOIN pieces p ON p.id = d.piece_id "
                f"WHERE d.status = 'draft' AND NOT {_being_prepared('p')} "
                "AND COALESCE(d.piece_id, 0) <= ?",
                (newest,),
            )
        ]
        unsure = conn.execute(
            f"SELECT p.*, {_FACTS} FROM pieces p "
            f"LEFT JOIN senders s ON s.id = p.sender_id WHERE {_UNSURE} AND p.id <= ? "
            f"ORDER BY p.id DESC LIMIT {MAX_UNSURE}",
            (newest,),
        ).fetchall()
        # One list, newest piece first, so a card keeps its place when a correction turns it
        # from "no request yet" into a request or rewrites its request.
        queue = sorted(
            drafts + [dict(row, id=None) for row in unsure],
            key=lambda item: (item["piece_id"] or 0, item["id"] or 0), reverse=True,
        )
        # History never shows a piece before For Review does: nothing still to be read, and
        # nothing whose request hasn't been written or is still being prepared.
        rows = conn.execute(
            "SELECT q.*, d.status AS request_status, d.sent_at AS request_sent_at FROM ("
            f"  SELECT p.*, {_FACTS}, {_REQUEST_FOR_PIECE} AS request_id, "
            f"  {_UNSURE} AS unsure "
            "  FROM pieces p LEFT JOIN senders s ON s.id = p.sender_id) q "
            "LEFT JOIN drafts d ON d.id = q.request_id LEFT JOIN senders s ON s.id = d.sender_id "
            "WHERE q.category IS NOT NULL AND q.id <= ? "
            f"AND NOT (COALESCE(d.status, '') = 'draft' AND {_being_prepared('q')}) "
            # resident is NULL when unknown; COALESCE keeps that from turning the test NULL
            "AND NOT (q.request_id IS NULL AND q.sender_id IS NOT NULL AND q.manual = 0 "
            "         AND (q.category = 'marketing' OR COALESCE(q.resident, 1) = 0)) "
            # one box searches both names on a row
            + ("AND (q.sender_name LIKE ? OR q.recipient LIKE ?) " if sender else "")
            + "ORDER BY q.id DESC LIMIT ? OFFSET ?",
            [newest] + ([f"%{sender}%"] * 2 if sender else [])
            + [HISTORY_PAGE + 1, (page - 1) * HISTORY_PAGE],
        ).fetchall()
    return templates.get_template("index.html").render(
        queue=queue,
        history=rows[:HISTORY_PAGE],
        page=page,
        more=len(rows) > HISTORY_PAGE,
        sender=sender,
        settings_url=settings_url(),
        version=__version__,
        feedback_email=FEEDBACK_EMAIL,
        referral_url=REFERRAL_URL,
        worker=server.worker,
        cfg=server.cfg,
        categories=classify.CATEGORIES,
    )


def _sample(cfg, kind: str) -> tuple[dict, str]:
    """Values to preview a template with: the user's most recent such mail, or a made-up piece."""
    wanted = "p.resident = 0 AND p.recipient != ''" if kind == "moved" else "p.category = 'marketing'"
    with db.connect() as conn:
        row = conn.execute(
            "SELECT s.name, p.recipient, p.received, s.website FROM pieces p "
            f"JOIN senders s ON s.id = p.sender_id WHERE {wanted} ORDER BY p.id DESC LIMIT 1"
        ).fetchone()
    if row:
        return letters.values(cfg, *row), f"your latest mail from {row['name']}"
    made_up = ("Summit Bank", "John Smith") if kind == "moved" else ("Acme Catalog", "Pat Doe")
    return letters.values(cfg, *made_up, pipeline.now(), "https://www.example.com/"), "a made-up example"


def edit_templates(server, _query) -> str:
    current = letters.load(server.cfg)
    samples, cards = {}, []
    for kind, title in letters.KINDS.items():
        samples[kind], sample_from = _sample(server.cfg, kind)
        cards.append({
            "kind": kind, "title": title, **current[kind], "sample_from": sample_from,
            "is_default": current[kind] == letters.DEFAULTS[kind],
            "unknown": letters.unknown_variables(current[kind]["subject"] + " " + current[kind]["body"]),
        })
    return templates.get_template("letters.html").render(
        templates=cards, samples=samples, variables=letters.VARIABLES,
    )


def save_templates(server, form) -> str:
    action = form.get("action", "save")
    current = letters.load(server.cfg)
    chosen = {}
    for kind in letters.KINDS:
        if action == f"reset_{kind}":
            chosen[kind] = dict(letters.DEFAULTS[kind])
        else:
            chosen[kind] = {
                "subject": form.get(f"{kind}_subject", current[kind]["subject"]),
                "body": form.get(f"{kind}_body", current[kind]["body"]).replace("\r\n", "\n"),
            }
    letters.save(server.cfg, chosen)
    return "templates"  # back to this page, not the mail


def image(_server, _query, piece_id: str) -> Path | None:
    with db.connect() as conn:
        row = conn.execute("SELECT image_path FROM pieces WHERE id = ?", (piece_id,)).fetchone()
    path = Path(row["image_path"]) if row and row["image_path"] else None
    return path if path and path.is_file() else None


def corrections(server, _form, full: str | None = None) -> Download:
    """Pieces where the user's answer differs from the app's, for improving the rules.

    The redacted version is meant to be shareable: addressees are left out and the text has
    names and addresses blanked (see classify.strip_personal for the limits of that). The
    full version, for the user's own eyes, has both as read.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT p.*, s.name AS sender_name FROM pieces p "
            "LEFT JOIN senders s ON s.id = p.sender_id "
            "WHERE p.manual = 1 OR p.corrected_at IS NOT NULL ORDER BY p.id"
        ).fetchall()
    records = [
        {
            "piece": row["id"],
            "received": row["received"],
            "corrected_at": row["corrected_at"],
            "app_version": row["app_version"],
            "category_changed": row["auto_category"] != row["category"],
            "app": {
                "category": row["auto_category"], "rule": row["auto_rule"],
                "reason": row["auto_reason"], "confidence": row["auto_confidence"],
                "sender": row["auto_sender"], "addressee_read": bool(row["auto_recipient"]),
                "resident": row["auto_resident"],
            },
            "you": {
                "category": row["category"], "sender": row["sender_name"] or "",
                "addressee_changed": row["recipient"] != row["auto_recipient"],
                "resident": row["resident"],
            },
            "usps_listed_sender": row["listed_sender"],
            "usps_campaign": bool(row["campaign"]),
            "text": row["ocr_text"] if full else classify.strip_personal(
                row["ocr_text"], {row["recipient"], row["auto_recipient"]} - {""}, server.cfg
            ),
            **({"addressee": {"app": row["auto_recipient"], "you": row["recipient"]}} if full else {}),
        }
        for row in rows
    ]
    # Pieces the user merely confirmed are left out: only the app's misses are useful.
    records = [r for r in records if r["category_changed"] or r["you"]["addressee_changed"] or any(
        r["app"][field] != r["you"][field] for field in ("sender", "resident")
    )]
    body = json.dumps({"redacted": not full, "corrections": records}, indent=2).encode()
    kind = "full" if full else "redacted"
    return Download(body, "application/json", f"informed-unsubscribe-corrections-{kind}.json")


def status(server, _form) -> Download:
    """What the background run is doing, plus a fingerprint that changes when results do."""
    with db.connect() as conn:
        counts = conn.execute(
            "SELECT (SELECT COUNT(*) FROM pieces), (SELECT COUNT(category) FROM pieces), "
            "(SELECT COUNT(*) FROM drafts), (SELECT COUNT(*) FROM drafts WHERE to_addr != ''), "
            "(SELECT COUNT(*) FROM senders WHERE research_status != 'pending')"
        ).fetchone()
    body = json.dumps({
        "activity": server.worker.activity,
        "errors": len(server.worker.errors),
        "results": "-".join(map(str, counts)),
    })
    return Download(body.encode(), "application/json", "")


def check(server, _form) -> None:
    server.worker.request_run()


def add_photo(server, form) -> None:
    """Take in a picture the user took of a mailpiece and start a run to read it."""
    image = form.get("photo")
    # The page sends a JPEG it has just redrawn (see index.html), never the camera's own file.
    if not isinstance(image, bytes) or not image.startswith(b"\xff\xd8\xff"):
        raise ValueError("not a JPEG")
    pipeline.add_piece(server.cfg, image, photo=True)
    server.worker.request_run()


def _follow_up(server) -> None:
    """After the user corrects something: write any request that is now due, and start a run
    only if one of them needs its sender's website looked up."""
    pipeline.create_drafts(server.cfg)
    if pipeline.lookups_waiting():
        server.worker.request_run()


def update_piece(server, form, piece_id: str) -> None:
    """Apply the user's corrections to what the app read off a piece: who sent it, who it is
    addressed to and whether they live here, what kind of mail it is, and the sender's website.
    A request still in review that was written from the old facts is removed, and written
    again from the new ones if they call for one."""
    category = form.get("category", "")
    if category and category not in ("marketing", "real"):
        raise ValueError("unknown category")
    recipient = " ".join(form.get("recipient", "").split())
    website = form.get("website", "").strip()
    cfg = server.cfg
    with db.connect() as conn:
        piece = conn.execute("SELECT * FROM pieces WHERE id = ?", (piece_id,)).fetchone()
        if not piece:
            raise LookupError("no such piece")
        # A typed name is taken exactly as typed. Merging it into a similar-looking sender, as
        # is done for names read off scans, would either discard the correction or change
        # other pieces the user didn't touch.
        sender_id = db.upsert_sender(conn, form.get("sender", ""), merge_similar=False)
        resident = 1 if form.get("lives_here") else classify.check_resident(
            [recipient] if recipient else [], cfg
        )
        conn.execute(
            "UPDATE pieces SET sender_id = ?, recipient = ?, resident = ?, corrected_at = ? "
            "WHERE id = ?",
            (sender_id, recipient, resident, pipeline.now(), piece_id),
        )
        if category and category != piece["category"]:
            conn.execute(
                "UPDATE pieces SET category = ?, confidence = 1, manual = 1, "
                "reason = 'Set by you.' WHERE id = ?",
                (category, piece_id),
            )
        # The website box starts out holding the old sender's site, so it only counts when the
        # user actually changed it. A site the user gives is looked up afresh and trusted.
        if sender_id is not None and website != form.get("website_was", website).strip():
            # The user vouches for this site, so it stops being a low-confidence guess at once.
            # Addresses from the old site no longer apply; the lookup fills in new ones.
            conn.execute(
                "UPDATE senders SET website = ?, verified = ?, emails = '[]', candidates = '[]', "
                "research_status = 'pending', research_note = '' WHERE id = ?",
                (website, int(bool(website)), sender_id),
            )
            conn.execute(
                "UPDATE drafts SET to_addr = '' WHERE sender_id = ? AND status = 'draft'",
                (sender_id,),
            )
        marketing = (category or piece["category"]) == "marketing"
        wanted = None
        if sender_id is not None:
            wanted = "moved" if resident == 0 and recipient else "optout" if marketing else None
        for draft in conn.execute(
            "SELECT id, kind, sender_id FROM drafts WHERE piece_id = ? AND status = 'draft'",
            (piece_id,),
        ).fetchall():
            if (draft["kind"], draft["sender_id"]) != (wanted, sender_id) \
                    or recipient != piece["recipient"]:
                conn.execute("DELETE FROM drafts WHERE id = ?", (draft["id"],))
    _follow_up(server)


def turn_photo(server, form, piece_id: str) -> None:
    """Replace the user's photo with the turned copy the page sends, and have it read again."""
    image = form.get("photo")
    # As with a new photo, the page sends a JPEG it has just redrawn (see index.html).
    if not isinstance(image, bytes) or not image.startswith(b"\xff\xd8\xff"):
        raise ValueError("not a JPEG")
    with db.connect() as conn:
        piece = conn.execute(
            "SELECT image_path FROM pieces WHERE id = ? AND photo = 1 AND image_path != ''",
            (piece_id,),
        ).fetchone()
        if not piece:
            raise LookupError("no such photo")
        Path(piece["image_path"]).write_bytes(image)
        conn.execute("UPDATE pieces SET reread = 1, attempts = 0 WHERE id = ?", (piece_id,))
    server.worker.request_run()


def delete_piece(_server, _form, piece_id: str) -> None:
    """Remove a piece from History for good: its row, its scan, and every request written for
    it, sent or not. The sender stays, as other pieces may share it."""
    with db.connect() as conn:
        piece = conn.execute("SELECT image_path FROM pieces WHERE id = ?", (piece_id,)).fetchone()
        if not piece:
            raise LookupError("no such piece")
        conn.execute("DELETE FROM drafts WHERE piece_id = ?", (piece_id,))
        conn.execute("DELETE FROM pieces WHERE id = ?", (piece_id,))
    if piece["image_path"]:
        Path(piece["image_path"]).unlink(missing_ok=True)


def update_draft(server, form, draft_id: str) -> None:
    action = form.get("action", "")
    to_addr = form.get("to_addr", "").strip()
    bcc = form.get("bcc", "").strip()
    subject = " ".join(form.get("subject", "").split())
    body = form.get("body", "").replace("\r\n", "\n")
    with db.connect() as conn:
        if action == "reopen":
            conn.execute(
                "UPDATE drafts SET status = 'draft' WHERE id = ? AND status IN ('skipped', 'sent')",
                (draft_id,),
            )
            return
        draft = conn.execute(
            "SELECT d.id, d.piece_id, COALESCE(p.image_path, '') AS image_path, "
            "COALESCE(p.photo, 0) AS photo FROM drafts d "
            "LEFT JOIN pieces p ON p.id = d.piece_id WHERE d.id = ? AND d.status = 'draft'",
            (draft_id,),
        ).fetchone()
        if not draft:
            raise LookupError("no such draft")
        if action == "skip":
            conn.execute("UPDATE drafts SET status = 'skipped' WHERE id = ?", (draft_id,))
            return
        conn.execute(
            "UPDATE drafts SET to_addr = ?, subject = ?, body = ?, error = '' WHERE id = ?",
            (to_addr, subject, body, draft_id),
        )
    if action != "send":
        return
    error = ""
    if not ADDRESS_RE.match(to_addr):
        error = "Enter a valid recipient address before sending."
    elif bcc and not ADDRESS_RE.match(bcc):
        error = "The Bcc address isn't valid. Correct it or leave it empty."
    else:
        # The scan goes along when asked for and still on file. The line saying so is added
        # here, not kept in the draft, so the email never mentions an attachment it lacks.
        scan = None
        image = Path(draft["image_path"]) if draft["image_path"] else None
        if form.get("attach_scan") and image and image.is_file():
            scan = (image.read_bytes(), image.suffix.lstrip(".") or "jpg")
        note = ATTACHED_NOTE.format("photo" if draft["photo"] else "scan")
        try:
            mail.send(
                server.cfg, to_addr, subject,
                body.rstrip() + note if scan else body, bcc=bcc, scan=scan,
            )
        except Exception as exc:  # shown on the draft so it can be retried
            error = f"Sending failed: {exc}"
    with db.connect() as conn:
        if error:
            conn.execute("UPDATE drafts SET error = ? WHERE id = ?", (error, draft_id))
        else:
            conn.execute(
                "UPDATE drafts SET status = 'sent', sent_at = ? WHERE id = ?",
                (pipeline.now(), draft_id),
            )


ROUTES = [
    ("GET", re.compile(r"/"), index),
    ("GET", re.compile(r"/images/(\d+)"), image),
    ("GET", re.compile(r"/corrections(-full)?\.json"), corrections),
    ("GET", re.compile(r"/status"), status),
    ("GET", re.compile(r"/templates"), edit_templates),
    ("POST", re.compile(r"/templates"), save_templates),
    ("POST", re.compile(r"/check"), check),
    ("POST", re.compile(r"/photos"), add_photo),
    ("POST", re.compile(r"/pieces/(\d+)"), update_piece),
    ("POST", re.compile(r"/pieces/(\d+)/delete"), delete_piece),
    ("POST", re.compile(r"/pieces/(\d+)/turn"), turn_photo),
    ("POST", re.compile(r"/drafts/(\d+)"), update_draft),
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        if os.environ.get("SUPERVISOR_TOKEN") and self.client_address[0] != INGRESS_PROXY:
            return self.send_error(403)
        path = urlparse(self.path).path
        for route_method, pattern, view in ROUTES:
            match = pattern.fullmatch(path)
            if route_method == method and match:
                break
        else:
            return self.send_error(404)
        # Views get the submitted form on POST and the query string on GET.
        form = {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            # A photo arrives as the bare image; everything else is a form.
            photo = self.headers.get("Content-Type", "").startswith("image/")
            if length > (MAX_PHOTO_BYTES if photo else MAX_FORM_BYTES):
                return self.send_error(413)
            body = self.rfile.read(length)
            if photo:
                form = {"photo": body}
            else:
                fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
                form = {key: values[-1] for key, values in fields.items()}
        try:
            result = view(self.server, form, *match.groups())
        except ValueError:
            return self.send_error(400)
        except LookupError:
            return self.send_error(404)
        if method == "POST":
            announce(self.server)  # the action may have emptied or added to For Review
            # Ingress serves the app under a per-session prefix, passed in this header.
            # A view may name the page to return to; otherwise it is the main page.
            self.send_response(303)
            back = result if isinstance(result, str) else ""
            self.send_header("Location", self.headers.get("X-Ingress-Path", "") + "/" + back)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif isinstance(result, str):
            self._send(result.encode(), "text/html; charset=utf-8")
        elif isinstance(result, Download):
            self._send(result.body, result.content_type, result.filename)
        elif result is None:
            self.send_error(404)
        else:
            self._send(result.read_bytes(), IMAGE_TYPES.get(result.suffix, "application/octet-stream"))

    def _send(self, body: bytes, content_type: str, filename: str = "") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        # A turned photo keeps its address, so nothing may be shown from an old copy.
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def make_server(cfg, worker, port: int = PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.cfg = cfg
    server.worker = worker
    return server
