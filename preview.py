"""Preview the app's page on this computer, with made-up mail and no mailbox or Home Assistant.

    uv run --no-project --python 3.12 --with-requirements informed_unsubscribe/requirements.txt \
        python preview.py

Then open http://localhost:8099. Edits to the page template show up on a browser refresh;
edits to Python files need this script restarted. Nothing is emailed: Send only marks a
request as sent. The data is thrown away when the script stops.
"""

import hashlib
import io
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "informed_unsubscribe"))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app import config, db, mail, pipeline, web  # noqa: E402

HOME = "PAT DOE\n12 OAK COURT\nSPRINGFIELD IL 62704"
BULK = "PRSRT STD\nU.S. POSTAGE\nPAID"
FIRST_CLASS = "PRESORTED\nFIRST-CLASS MAIL\nU.S. POSTAGE PAID"

# One piece per situation the page has to show.
PIECES = [
    dict(sender="Hearth & Home", addressee="PAT DOE", resident=1, category="marketing",
         reason='Bulk-mail marking "PRSRT STD".', marking=BULK, slogan="Fall sale: 20% off everything",
         site=("https://www.hearthandhome.example/", ["optout@hearthandhome.example",
               "privacy@hearthandhome.example"], True), request="draft",
         photo=True),  # taken sideways, to try Rotate on
    dict(sender="Make Essential Repairs in", addressee="VAN CHUNG", resident=0, category="marketing",
         reason='Bulk-mail marking "PRSRT STD".', marking=BULK, slogan="Make Essential Repairs in College Labs",
         site=("https://www.repairs.example/", ["press@repairs.example"], False), request="draft"),
    dict(sender="", addressee="PAT DOE", resident=1, category="undetermined",
         reason="No clear bulk-mail, bill or offer wording.", marking="", slogan="", site=None,
         request=None),
    dict(sender="City Water Department", addressee="PAT DOE", resident=1, category="real",
         reason='Marked "FIRST-CLASS".', marking=FIRST_CLASS, slogan="", site=None, request=None),
    dict(sender="Summit Bank", addressee="PAT DOE", resident=1, category="marketing",
         reason="Offer wording: apr, pre-approved.", marking=FIRST_CLASS,
         slogan="You're pre-approved! 0% intro APR",
         site=("https://www.summitbank.example/", ["privacy@summitbank.example"], True), request="sent"),
    dict(sender="Bob's Plumbing", addressee="PAT DOE", resident=1, category="marketing",
         reason="Offer wording: call today, free estimate.", marking="",
         slogan="Call today for a FREE estimate!",
         site=("https://www.bobsplumbing.example/", ["info@bobsplumbing.example"], True),
         request="skipped"),
]


def scan(piece) -> bytes:
    """A stand-in for a USPS scan of the piece."""
    image = Image.new("L", (700, 330), 205)
    draw = ImageDraw.Draw(image)
    small, medium = ImageFont.load_default(size=13), ImageFont.load_default(size=16)
    draw.multiline_text((25, 20), piece["sender"] or "", fill=40, font=small)
    draw.multiline_text((540, 20), piece["marking"], fill=40, font=small)
    draw.multiline_text((260, 150), HOME.replace("PAT DOE", piece["addressee"]), fill=30, font=medium)
    draw.text((30, 280), piece["slogan"], fill=40, font=medium)
    if piece.get("photo"):
        image = image.rotate(90, expand=True)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=70)
    return out.getvalue()


def seed(cfg) -> None:
    images = Path(cfg.data_dir) / "images"
    images.mkdir(parents=True)
    with db.connect() as conn:
        for piece in PIECES:
            data = scan(piece)
            sha = hashlib.sha256(data).hexdigest()
            (images / f"{sha}.jpg").write_bytes(data)
            sender_id = db.upsert_sender(conn, piece["sender"])
            if piece["site"]:
                website, emails, verified = piece["site"]
                note = "" if verified else pipeline.UNCONFIRMED_NOTE
                conn.execute(
                    "UPDATE senders SET website = ?, emails = ?, verified = ?, research_note = ?, "
                    "research_status = 'done', research_attempts = 1 WHERE id = ?",
                    (website, json.dumps(emails), int(verified), note, sender_id),
                )
            conn.execute(
                "INSERT INTO pieces(sha, image_path, received, photo, sender_id, category, "
                "confidence, reason, recipient, resident, auto_category, auto_sender, "
                "auto_recipient, auto_resident) VALUES (?, ?, ?, ?, ?, ?, 0.9, ?, ?, ?, ?, ?, ?, ?)",
                (sha, str(images / f"{sha}.jpg"), pipeline.now(), int(piece.get("photo", False)),
                 sender_id, piece["category"], piece["reason"], piece["addressee"],
                 piece["resident"], piece["category"], piece["sender"], piece["addressee"],
                 piece["resident"]),
            )
    pipeline.create_drafts(cfg)
    with db.connect() as conn:
        for piece in PIECES:
            if piece["request"] in ("sent", "skipped"):
                conn.execute(
                    "UPDATE drafts SET status = ?, sent_at = ? WHERE sender_id = "
                    "(SELECT id FROM senders WHERE name = ?)",
                    (piece["request"], pipeline.now(), piece["sender"]),
                )


def pretend_run(cfg) -> list[str]:
    time.sleep(4)  # long enough to see the "Processing mail" line
    pipeline.research_pending(cfg)  # with the stand-in lookup set up below
    with db.connect() as conn:  # a turned photo counts as read again, with nothing changed
        conn.execute("UPDATE pieces SET reread = 0")
    return []


cfg = config.Config(
    data_dir=tempfile.mkdtemp(prefix="informed-unsubscribe-preview-"),
    email_address="demo-mailbox@example.com", email_mailbox_password="not-used",
    physical_mail_address="12 Oak Court, Springfield, IL 62704",
    resident_last_names=("Doe",), previous_resident_last_names=("Chung",), bcc_email_address="you@example.com", california_resident=True, registered_with_dmachoice=True,
)
db.init(cfg.data_dir)
seed(cfg)


def pretend_send(*args, **kwargs) -> None:
    time.sleep(3)  # about as long as a real send, so the "Sending…" state can be seen
    print("Preview: a request would have been sent to", args[1])


mail.send = pretend_send
pipeline.research.research = lambda name, website="", **evidence: pipeline.research.Result(
    website, ["contact@" + (website.split("//")[-1].strip("/") or "sender.example")], bool(website)
)
worker = pipeline.Worker(cfg, run=pretend_run)
worker.start()
print("Preview running at http://localhost:8099 (Ctrl-C to stop)")
try:
    web.make_server(cfg, worker).serve_forever()
except KeyboardInterrupt:
    pass
