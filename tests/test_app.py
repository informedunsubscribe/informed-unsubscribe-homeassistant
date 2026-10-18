import dataclasses
import io
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "informed_unsubscribe"))

from app import classify, config, db, letters, mail, pipeline, research, web  # noqa: E402

SCAN = b"\xff\xd8" + os.urandom(9_000)


def digest(subject="Your Daily Digest for Sat, 10/10 is ready to view", images=(SCAN,)):
    msg = EmailMessage()
    msg["From"] = "USPS Informed Delivery <USPSInformeddelivery@email.informeddelivery.usps.com>"
    msg["Subject"] = subject
    msg.set_content("digest")
    for i, data in enumerate(images):
        msg.add_attachment(data, maintype="image", subtype="jpeg", filename=f"{i}.jpg")
    return msg.as_bytes()


def scan_of(*lines):
    """A Scan whose lines run down the left edge, the first one in the return-address zone."""
    return classify.Scan(
        [classify.Line(text, 50, 50 + 150 * i) for i, text in enumerate(lines)], 2000, 1000
    )


@pytest.fixture
def cfg(tmp_path):
    db.init(str(tmp_path))
    return config.Config(data_dir=str(tmp_path), physical_mail_address="1 Main St")


def test_extract_pieces_without_layout_skips_logos_and_other_mail():
    assert mail.extract_pieces(digest(images=(SCAN, b"tiny-logo"))) == [mail.Piece(SCAN, "jpeg")]
    other = EmailMessage()
    other["From"] = "someone@example.org"
    other["Subject"] = "hello"
    other.add_attachment(SCAN, maintype="image", subtype="jpeg", filename="a.jpg")
    assert mail.extract_pieces(other.as_bytes()) == []


def test_extract_pieces_follows_the_digest_layout():
    """Structure taken from a real digest: campaign labels, ride-along adverts, plain scans."""
    html = """
    <img src="https://www.usps.com/email/id/uspslogo.png" alt="USPS Logo">
    <h2 class="section-heading">Expected Today</h2>
    <p id="campaign-from-label-id">FROM: <b><span id="campaign-from-span-id">LeafFilter</span></b></p>
    <a><img src="cid:111-035.jpg" alt="campaign"></a>
    <a><img src="cid:content-900.jpg" alt="RideAlong image"></a>
    <a><img src="cid:content-900.jpg" alt="RideAlong"></a>
    <p id="m_-42campaign-from-label-id">FROM: <b><span id="m_-42campaign-from-span-id">EJ Plumbing </span></b></p>
    <a><img src="cid:mailer-901.jpg" alt="campaign"></a>
    <a><img src="cid:content-901.jpg" alt="RideAlong image"></a>
    <img id="mailpiece-image-src-id" src="cid:222-035.jpg" alt="Mailpiece Image">
    <img src="cid:missing.jpg" alt="Mailpiece Image">
    <h2 class="section-heading">Expected This Week</h2>
    <p id="campaign-from-label-id">FROM: <b><span id="campaign-from-span-id">Save-Sweepstakes</span></b></p>
    <a><img src="cid:mailer-902.jpg" alt="campaign"></a>
    <a><img src="cid:content-902.jpg" alt="RideAlong"></a>
    """
    msg = EmailMessage()
    msg["From"] = "USPS Informed Delivery <USPSInformeddelivery@email.informeddelivery.usps.com>"
    msg["Subject"] = "Your Daily Digest for Sat, 10/10 is ready to view"
    msg.set_content("digest")
    msg.add_alternative(html, subtype="html")
    names = ["111-035.jpg", "content-900.jpg", "mailer-901.jpg", "content-901.jpg", "222-035.jpg",
             "mailer-902.jpg", "content-902.jpg"]
    for name in names:
        msg.get_payload()[1].add_related(
            name.encode() * 800, maintype="image", subtype="jpeg", cid=f"<{name}>", filename=name
        )
    pieces = mail.extract_pieces(msg.as_bytes())
    assert [(p.image[:12], p.sender, p.campaign) for p in pieces] == [
        (b"111-035.jpg1", "LeafFilter", True),
        (b"mailer-901.j", "EJ Plumbing", True),
        (b"222-035.jpg2", "", False),  # the label above must not carry over to a plain scan
    ]
    # a digest with nothing expected today yields nothing, rather than falling back to every image
    later = msg.as_bytes().replace(b"Expected Today", b"Expected This Week")
    assert mail.extract_pieces(later) == []


@pytest.mark.parametrize("text, expected", [
    ("PRSRT STD\nU.S. POSTAGE PAID\nAcme", "marketing"),
    ("PRSRT STANDARD\nUS POSTAGE PAID", "marketing"),
    ("County Finance\nTAX BILL - OPEN IMMEDIATELY", "real"),
    ("Jane Doe\nOr Current Resident", "marketing"),
    ("You're pre-approved! 0% intro APR", "marketing"),
    ("Bob's Plumbing\nCall today for a free estimate", "marketing"),
    ("Hearth & Home\nFall sale - 20% off everything", "marketing"),
    ("Just listed in your neighborhood!\nWhat's your home value?", "marketing"),
    # a bulk marking wins even when the piece is dressed up as a bill
    ("PRSRT STD\nFinal invoice enclosed", "marketing"),
    ("Bob's Plumbing\nInvoice enclosed", "real"),
    ("Bob's Plumbing\nFIRST-CLASS MAIL\nAmount due: see inside", "real"),
    ("PRESORTED FIRST-CLASS MAIL\nCity Water Dept", "real"),
    # first-class postage needs more than two offer words to be called marketing
    ("First-Class\nBig sale inside, save now", "undetermined"),
    ("Aunt Carol\n12 Oak Court", "undetermined"),
    ("  ", "undetermined"),
])
def test_categorize_rules(text, expected):
    assert classify.categorize(text)[0] == expected


def test_sender_memory_never_hides_a_bill():
    assert classify.categorize("Bob's Plumbing", remembered="marketing")[0] == "marketing"
    assert classify.categorize("Bob's Plumbing", remembered="real")[0] == "real"
    assert classify.categorize("Bob's Plumbing\nYour invoice", remembered="marketing")[0] == "real"
    assert classify.categorize("Bob's Plumbing\nPRSRT STD", remembered="real")[0] == "marketing"


def test_parse_tsv_and_find_sender():
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    rows = [
        (1, 1, 0, 0, 0, 0, 0, 0, 2000, 1000, -1, ""),
        (5, 1, 1, 1, 1, 1, 60, 80, 50, 30, 91, "Acme"),
        (5, 1, 1, 1, 1, 2, 120, 82, 90, 30, 88, "Outfitters"),
        (5, 1, 1, 1, 2, 1, 60, 120, 40, 30, 90, "PO"),
        (5, 1, 1, 1, 2, 2, 110, 120, 40, 30, 90, "Box"),
        (5, 1, 1, 1, 2, 3, 160, 120, 40, 30, 90, "77"),
        (5, 1, 2, 1, 1, 1, 1500, 60, 90, 30, 85, "PRSRT"),
        (5, 1, 2, 1, 1, 2, 1600, 60, 60, 30, 85, "STD"),
        (5, 1, 3, 1, 1, 1, 60, 30, 90, 30, 12, "smudge"),
        (5, 1, 4, 1, 1, 1, 800, 500, 60, 30, 93, "Pat"),
        (5, 1, 4, 1, 1, 2, 870, 500, 60, 30, 93, "Doe"),
    ]
    scan = classify.parse_tsv("\n".join([header] + ["\t".join(map(str, r)) for r in rows]))
    assert (scan.width, scan.height) == (2000, 1000)
    assert [line.text for line in scan.lines] == [
        "PRSRT STD", "Acme Outfitters", "PO Box 77", "Pat Doe",
    ]
    assert classify.find_sender(scan, ["Acme Outfitters"]) == ""
    assert classify.find_sender(scan._replace(lines=scan.lines[2:])) == ""


def test_campaign_pieces_are_marketing_unless_they_look_like_a_bill():
    assert classify.categorize("", campaign=True)[0] == "marketing"
    assert classify.categorize("Trusted home service experts", campaign=True)[0] == "marketing"
    assert classify.categorize("Your statement enclosed", campaign=True)[0] == "real"
    assert classify.categorize("anything", remembered="real", campaign=True)[0] == "real"


def test_find_domain_needs_both_ocr_passes_to_agree():
    def scan(sparse, block):
        return classify.Scan([classify.Line(sparse, 0, 0)], 100, 100, block)

    both = "Call us! www.BobsPlumbing.com info@other.com"
    assert classify.find_domain(scan(both, both)) == "bobsplumbing.com"
    # a real misread: one pass split EJHOMESERVICES.COM and produced a different site
    assert classify.find_domain(scan("ERVICES.COM", "EJHOMESERVICES.COM")) == ""
    assert classify.find_domain(scan("ERVICES.COM EJHOMESERVICES.COM", "EJHOMESERVICES.COM")) == "ejhomeservices.com"
    assert classify.find_domain(scan("usps.com", "usps.com")) == ""


def test_senders_merge_across_spelling_and_ocr_slips(cfg):
    assert db.normalize("The Geico Co., Inc.") == db.normalize("GEICO")
    with db.connect() as conn:
        first = db.upsert_sender(conn, "Acme Outfitters")
        assert db.upsert_sender(conn, "Acme Outfiters") == first
        assert db.upsert_sender(conn, "Summit Bank") != first
        assert db.upsert_sender(conn, " ") is None


def test_pick_result_skips_aggregators():
    html = """
    <a class="result__a" href="//duckduckgo.com/y.js?ad=1">Ad</a>
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.angi.com%2Fgeico">dir</a>
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FGeico">W</a>
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.geico.com%2Fabout%2F&rut=x">G</a>
    """
    assert research.pick_results(html) == ["https://www.geico.com/"]


def test_extract_and_rank_emails():
    html = """
    <a href="mailto:Careers@acme.com">jobs</a> privacy@acme.com noreply@acme.com
    <img src="logo@2x.png"> optout@listvendor.net info%40acme.com
    <span data-cfemail="{}"></span>
    """.format(bytes([0x42] + [c ^ 0x42 for c in b"unsubscribe@acme.com"]).hex())
    emails = research.extract_emails(html)
    assert "logo@2x.png" not in emails
    ranked = research.rank(emails, "acme.com")
    assert ranked[:3] == ["unsubscribe@acme.com", "privacy@acme.com", "optout@listvendor.net"]
    assert ranked[-1] == "careers@acme.com"
    assert "noreply@acme.com" not in ranked


def test_candidate_links_prioritise_opt_out_pages():
    html = """
    <a href="/about">About</a><a href="/privacy">Privacy</a>
    <a href="https://other.com/contact">Contact</a><a href="/mail-opt-out">Stop mail</a>
    """
    links = research.candidate_links(html, "https://www.acme.com/")
    assert links[:3] == [
        "https://www.acme.com/mail-opt-out",
        "https://www.acme.com/privacy",
        "https://www.acme.com/about",
    ]
    assert not any("other.com" in link for link in links)


def test_private_and_non_web_addresses_are_refused():
    assert research._fetch("http://127.0.0.1:1/") == ("", "")
    assert research._fetch("file:///etc/passwd") == ("", "")


def test_pipeline_end_to_end(cfg, monkeypatch):
    assert pipeline.ingest(cfg, digest()) == 1
    assert pipeline.ingest(cfg, digest()) == 0  # same scan is not stored twice

    monkeypatch.setattr(classify, "read", lambda image: scan_of(
        "Acme Catalog", "PRSRT STD", "Pat Doe", "shop acmecatalog.com"
    )._replace(extra="shop acmecatalog.com"))
    seen = []
    monkeypatch.setattr(research, "research", lambda name, website="", **evidence: (
        seen.append(website) or research.Result(website, ["optout@acmecatalog.com"], True)
    ))
    pipeline.classify_pending(cfg)
    pipeline.research_pending(cfg)
    pipeline.create_drafts(cfg)
    pipeline.create_drafts(cfg)

    assert seen == ["https://acmecatalog.com/"]  # the printed web address skips the search
    with db.connect() as conn:
        drafts = conn.execute("SELECT * FROM drafts").fetchall()
        piece = conn.execute("SELECT * FROM pieces").fetchone()
    assert piece["category"] == "marketing" and "PRSRT STD" in piece["ocr_text"]
    assert len(drafts) == 1
    assert drafts[0]["to_addr"] == "optout@acmecatalog.com"
    # no addressee was read, so only the address is quoted
    assert "I received mail from Acme Catalog and would like it to stop." in " ".join(drafts[0]["body"].split())
    assert "you hold for this address," in drafts[0]["body"] and "1 Main St" in drafts[0]["body"]


def test_your_label_sticks_to_the_sender_wherever_its_name_appears(cfg, monkeypatch):
    pipeline.ingest(cfg, digest(images=(SCAN,)))
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Bob's Plumbing"))
    pipeline.classify_pending(cfg)
    with db.connect() as conn:
        assert conn.execute("SELECT category FROM pieces").fetchone()[0] == "undetermined"
        conn.execute("UPDATE pieces SET category = 'marketing', manual = 1")

    # next time it is a postcard: no return address, name buried in the text
    pipeline.ingest(cfg, digest(images=(SCAN + b"2",)))
    monkeypatch.setattr(classify, "read", lambda image: scan_of(
        "Is your water heater ready?", "Trust Bob's Plumbing"
    ))
    pipeline.classify_pending(cfg)
    # and then a bill from the same plumber
    pipeline.ingest(cfg, digest(images=(SCAN + b"3",)))
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Bob's Plumbing", "Invoice"))
    pipeline.classify_pending(cfg)

    with db.connect() as conn:
        rows = conn.execute("SELECT category, sender_id FROM pieces ORDER BY id").fetchall()
    assert [r["category"] for r in rows] == ["marketing", "marketing", "real"]
    assert len({r["sender_id"] for r in rows}) == 1


def test_unreadable_scan_gives_up_as_undetermined(cfg, monkeypatch):
    pipeline.ingest(cfg, digest())

    def broken(image):
        raise classify.ClassifyError("nope")

    monkeypatch.setattr(classify, "read", broken)
    for _ in range(pipeline.MAX_CLASSIFY_ATTEMPTS):
        pipeline.classify_pending(cfg)
    with db.connect() as conn:
        assert conn.execute("SELECT category FROM pieces").fetchone()[0] == "undetermined"


def test_web_review_and_send(cfg, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    worker = pipeline.Worker(cfg)  # never started; the test drives the steps itself
    server = web.make_server(cfg, worker, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def get(path):
        with urllib.request.urlopen(base + path) as response:
            return response.read()

    def post(path, **fields):
        request = urllib.request.Request(
            base + path, data=urllib.parse.urlencode(fields).encode()
        )
        with urllib.request.urlopen(request) as response:  # follows the redirect back home
            return response.read().decode()

    try:
        pipeline.ingest(cfg, digest())
        assert "No mail yet." in get("/").decode()  # an unread piece isn't listed anywhere yet
        post("/pieces/1", category="marketing", sender="Acme Catalog")
        assert worker.wake.is_set()
        monkeypatch.setattr(research, "research", lambda name, website="", **evidence: research.Result("https://acme.com/", [], True))
        pipeline.research_pending(cfg)
        pipeline.create_drafts(cfg)
        page = get("/").decode()
        assert "Acme Catalog" in page and "No email addresses found" in page
        assert get("/images/1") == SCAN
        with pytest.raises(urllib.error.HTTPError):
            get("/images/999")

        sent = []
        monkeypatch.setattr(mail, "send", lambda cfg, to, subject, body, **kw: sent.append((to, body)))
        assert "Enter a valid recipient" in post(
            "/drafts/1", action="send", to_addr="", subject="s", body="b"
        )
        assert not sent
        page = post("/drafts/1", action="send", to_addr="optout@acme.com", subject="s",
                    body="line one\r\nline two")
        assert sent == [("optout@acme.com", "line one\nline two")]
        assert 'email sent<time class="when"> 20' in _section(page, "<h2>History</h2>")  # with its date
        # a sent request can be reopened, edited and sent again
        assert "Reopen the sent request" in page
        assert "For Review (1)" in post("/drafts/1", action="reopen")
        post("/drafts/1", action="send", to_addr="privacy@acme.com", subject="s", body="again")
        assert sent[-1] == ("privacy@acme.com", "again")
    finally:
        server.shutdown()
        server.server_close()


def envelope(recipient_lines, sender="Summit Bank", extra=()):
    """Return address top-left, delivery block mid-piece ending in the household's street."""
    lines = [classify.Line(sender, 50, 40), classify.Line("500 Market St", 50, 80)]
    lines += [classify.Line(text, 800, 520 + 40 * i) for i, text in enumerate(recipient_lines)]
    top = 520 + 40 * len(recipient_lines)
    lines += [classify.Line("12 OAK COURT", 800, top), classify.Line("SPRINGFIELD IL 62704", 800, top + 40)]
    lines += [classify.Line(text, 50, 900) for text in extra]
    return classify.Scan(sorted(lines, key=lambda line: (line.top, line.left)), 2000, 1000)


def test_find_recipient_reads_the_block_above_the_street_address():
    address = "12 Oak Court, Springfield IL 62704"
    assert classify.find_recipient(envelope(["JOHN SMITH"]), address) == ["JOHN SMITH"]
    assert classify.find_recipient(
        envelope(["***AUTO**5-DIGIT 62704", "JOHN SMITH", "OR CURRENT RESIDENT"]), address
    ) == ["JOHN SMITH"]
    assert classify.find_recipient(envelope(["CURRENT RESIDENT"]), address) == []
    # names may contain digits; labels and sorting marks are not names
    assert classify.find_recipient(
        envelope(["MAILED TO:", "ROBERT AND MARY HALE 2024 FAMILY TRUST"]), address
    ) == ["ROBERT AND MARY HALE 2024 FAMILY TRUST"]
    # a name printed beside "Current Resident" is kept; a barcode read as loose letters is not
    assert classify.find_recipient(
        envelope(["L T | UL L T", "Current Resident / John Smith"]), address
    ) == ["John Smith"]
    assert classify.find_recipient(envelope(["JOHN SMITH OR CURRENT RESIDENT"]), address) == ["JOHN SMITH"]
    assert classify.find_recipient(envelope(["AL XU"]), address) == ["AL XU"]
    # the postal barcode above the name reads as a run of strokes, not a name
    assert classify.find_recipient(
        envelope(["l'|\"'I\"lll|l|ll'tll|lll\"|ll'l0'\"lllll\"IIOlOl\"'ll\"l'lllllll", "JILL HILL"]), address
    ) == ["JILL HILL"]
    # with a known last name on one line, the other lines above the street are dropped
    both = envelope(["ACME HOLDINGS", "JOHN SMITH"])
    assert classify.find_recipient(both, address) == ["ACME HOLDINGS", "JOHN SMITH"]
    assert classify.find_recipient(both, address, ("Smith",)) == ["JOHN SMITH"]
    assert classify.find_recipient(both, address, ("Nguyen",)) == ["ACME HOLDINGS", "JOHN SMITH"]
    family = envelope(["KIM NGUYEN", "JOHN SMITH"])
    assert classify.find_recipient(family, address, ("Smith", "Nguyen")) == ["KIM NGUYEN", "JOHN SMITH"]
    # without a house number to anchor on, fall back to the address outside the return corner
    assert classify.find_recipient(envelope(["JOHN SMITH"]), "") == ["JOHN SMITH"]


def test_check_resident_is_forgiving_and_off_without_a_list():
    home = config.Config(resident_last_names=("Nguyen", "Jane O'Brien-Lee"))
    assert classify.check_resident(["JOHN SMITH"], home) == 0
    assert classify.check_resident(["JANE LEE"], home) == 0  # only the last word is the surname
    assert classify.check_resident(["SAM O'BRIEN-LEE"], home) == 1
    assert classify.check_resident(["THE NGUYEM FAMILY"], home) == 1  # OCR slip
    assert classify.check_resident(["JOHN SMITH", "KIM NGUYEN"], home) == 1
    assert classify.check_resident([], home) is None
    assert classify.check_resident(["JOHN SMITH"], config.Config()) is None


def test_config_reads_residents_from_env_and_options(tmp_path, monkeypatch):
    options = tmp_path / "options.json"
    options.write_text('{"resident_last_names": ["Doe", " Nguyen "], "hours_between_runs": 12}')
    monkeypatch.setenv("OPTIONS_PATH", str(options))
    loaded = config.load()
    assert loaded.resident_last_names == ("Doe", "Nguyen") and loaded.hours_between_runs == 12
    monkeypatch.setenv("RESIDENT_LAST_NAMES", "Lee, Park")
    assert config.load().resident_last_names == ("Lee", "Park")


def test_mail_for_non_residents_gets_a_moved_request_whatever_the_category(tmp_path, monkeypatch):
    db.init(str(tmp_path))
    home = config.Config(data_dir=str(tmp_path), resident_last_names=("Doe",),
                         physical_mail_address="12 Oak Court, Springfield IL 62704")
    monkeypatch.setattr(research, "research", lambda name, website="", **evidence: research.Result("https://x.com/", ["a@x.com"], True))
    scans = [
        envelope(["JOHN SMITH"], extra=["FIRST-CLASS MAIL"]),            # real mail, old tenant
        envelope(["JOHN SMlTH"], extra=["FIRST-CLASS MAIL"]),            # again: its own request
        envelope(["JOHN SMITH"], sender="Acme Catalog", extra=["PRSRT STD"]),  # junk, old tenant
        envelope(["PAT DOE"], sender="Hearth Home", extra=["PRSRT STD"]),      # junk, resident
        envelope(["PAT DOE"], sender="City Water", extra=["FIRST-CLASS MAIL"]),  # real, resident
    ]
    for i, scan in enumerate(scans):
        pipeline.ingest(home, digest(images=(SCAN + bytes([i]),)))
        monkeypatch.setattr(classify, "read", lambda image, scan=scan: scan)
        pipeline.classify_pending(home)
    for _ in range(2):
        pipeline.research_pending(home)
        pipeline.create_drafts(home)

    with db.connect() as conn:
        drafts = conn.execute(
            "SELECT s.name, d.kind, d.recipient, d.subject, d.body, d.piece_id FROM drafts d "
            "JOIN senders s ON s.id = d.sender_id ORDER BY s.name, d.piece_id"
        ).fetchall()
    assert [(d["name"], d["kind"], d["piece_id"]) for d in drafts] == [
        ("Acme Catalog", "moved", 3), ("Hearth Home", "optout", 4),
        ("Summit Bank", "moved", 1), ("Summit Bank", "moved", 2),
    ]
    # the opt-out quotes the addressee as printed, and nothing is signed with a name
    hearth = drafts[1]
    assert "mail from Hearth Home addressed to PAT DOE and would like" in " ".join(hearth["body"].split())
    assert "you hold for this name and address," in hearth["body"]
    assert "PAT DOE\n12 Oak Court" in hearth["body"]
    assert all(d["body"].rstrip().endswith("Thank you.") for d in drafts)
    bank = drafts[2]
    assert bank["subject"] == "Wrong address on file for JOHN SMITH"
    assert "for JOHN SMITH is being delivered" in bank["body"] and "12 Oak Court" in bank["body"]
    assert bank["piece_id"] == 1


def test_web_shows_moved_drafts_and_lets_you_fix_the_addressee(tmp_path, monkeypatch):
    db.init(str(tmp_path))
    home = config.Config(data_dir=str(tmp_path), resident_last_names=("Doe",),
                         physical_mail_address="12 Oak Court")
    pipeline.ingest(home, digest())
    monkeypatch.setattr(classify, "read", lambda image: envelope(["JOHN SMITH"]))
    monkeypatch.setattr(research, "research", lambda name, website="", **evidence: research.Result("", [], False))
    pipeline.classify_pending(home)
    pipeline.research_pending(home)
    pipeline.create_drafts(home)
    server = web.make_server(home, pipeline.Worker(home), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert '<span class="value">JOHN SMITH</span>' in page and "not a resident" in page
        assert "Wrong address on file for JOHN SMITH" in page
        data = urllib.parse.urlencode(
            {"category": "real", "sender": "Summit Bank", "recipient": "Pat Doe"}
        ).encode()
        urllib.request.urlopen(urllib.request.Request(base + "/pieces/1", data=data)).read()
        with db.connect() as conn:
            assert conn.execute("SELECT resident FROM pieces").fetchone()[0] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_sent_mail_has_no_display_name_and_hides_the_blind_copy(monkeypatch):
    sessions = []

    class FakeSmtp(mail.smtplib.SMTP):
        """Real send_message logic, with the network calls recorded instead of made."""

        def __init__(self, *args, **kwargs):
            sessions.append(self)

        def __exit__(self, *args):
            return False

        def login(self, *args):
            pass

        def ehlo_or_helo_if_needed(self):
            pass

        def sendmail(self, from_addr, to_addrs, msg, *args, **kwargs):
            self.sent = (from_addr, sorted(to_addrs), msg.decode())
            return {}

    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSmtp)
    box = config.Config(email_address="box@example.org", email_mailbox_password="x")
    mail.send(box, "optout@acme.com", "subject", "body")
    from_addr, recipients, wire = sessions[-1].sent
    assert recipients == ["optout@acme.com"]
    assert "From: box@example.org" in wire and "Reply-To" not in wire

    mail.send(config.Config(email_address="box@example.org", email_mailbox_password="x",
                            bcc_email_address="me@example.net"), "optout@acme.com", "subject", "body")
    from_addr, recipients, wire = sessions[-1].sent
    assert recipients == ["me@example.net", "optout@acme.com"]
    assert "me@example.net" not in wire
    # an address given for one message replaces the configured one; "" sends no copy
    copied = config.Config(email_address="box@example.org", email_mailbox_password="x",
                           bcc_email_address="me@example.net")
    mail.send(copied, "optout@acme.com", "subject", "body", bcc="other@example.net")
    assert sessions[-1].sent[1] == ["optout@acme.com", "other@example.net"]
    mail.send(copied, "optout@acme.com", "subject", "body", bcc="")
    assert sessions[-1].sent[1] == ["optout@acme.com"]
    # the From name defaults to the street the sender already mails to
    housed = config.Config(email_address="box@example.org", email_mailbox_password="x",
                           physical_mail_address="12 Oak Court, Springfield, IL 62704")
    mail.send(housed, "optout@acme.com", "subject", "body")
    assert "From: 12 Oak Court Resident <box@example.org>" in sessions[-1].sent[2]
    # and is whatever the user sets instead
    named = config.Config(email_address="box@example.org", email_mailbox_password="x",
                          from_name=" Resident, 12 Oak Court\n")
    mail.send(named, "optout@acme.com", "subject", "body")
    from_addr, recipients, wire = sessions[-1].sent
    assert from_addr == "box@example.org"
    assert 'From: "Resident, 12 Oak Court" <box@example.org>' in wire
    # a scan goes as an ordinary attachment with a plain name
    mail.send(box, "optout@acme.com", "subject", "body", scan=(SCAN, "jpg"))
    wire = sessions[-1].sent[2]
    assert 'filename="mailpiece.jpg"' in wire and "Content-Type: image/jpeg" in wire


def test_campaign_sender_from_the_digest_is_used_for_the_piece(cfg, monkeypatch):
    html = '<span id="campaign-from-span-id">EJ Plumbing</span><img src="cid:mailer-1.jpg" alt="campaign">'
    msg = EmailMessage()
    msg["From"] = "USPSInformeddelivery@email.informeddelivery.usps.com"
    msg["Subject"] = "Your Daily Digest"
    msg.set_content("digest")
    msg.add_alternative(html, subtype="html")
    msg.get_payload()[1].add_related(SCAN, maintype="image", subtype="jpeg", cid="<mailer-1.jpg>")
    assert pipeline.ingest(cfg, msg.as_bytes()) == 1
    # artwork with no address: OCR finds a slogan and a web address, but no sender line
    monkeypatch.setattr(classify, "read", lambda image: classify.Scan(
        [classify.Line("Trusted Home Service Experts", 1000, 100),
         classify.Line("EJHOMESERVICES.COM", 1300, 900)], 2000, 1000, "EJHOMESERVICES.COM"
    ))
    pipeline.classify_pending(cfg)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT s.name, s.website, p.category FROM pieces p JOIN senders s ON s.id = p.sender_id"
        ).fetchone()
    assert tuple(row) == ("EJ Plumbing", "https://ejhomeservices.com/", "marketing")


def test_phone_and_return_address_are_read_off_the_piece():
    lines = [
        classify.Line("County of Sacramento", 200, 80),
        classify.Line("700 H Street, Room 1710", 200, 220),
        classify.Line("Sacramentn, California 95814", 200, 255),
        classify.Line("Call (650 395-9146 or 650.487.0598", 900, 300),
        classify.Line("CEDAR GROVE, CA 95061", 230, 300),  # the household's own city line
        classify.Line("Springfield IL 62704", 1500, 800),   # outside the return corner
    ]
    scan = classify.Scan(lines, 2000, 1000, "650.487.0598 and (650) 395-9145")
    assert classify.find_phones(scan) == ["6504870598"]  # the other number was read two ways
    assert classify.find_return_location(scan, "4382 Wellington Ave, Cedar Grove, CA 95061") \
        == "Sacramentn, California"
    assert classify.home_city("4382 Wellington Ave\nCedar Grove, CA 95061") == "Cedar Grove, CA"
    assert classify.home_city("4382 Wellington Ave, Cedar Grove, CA 95061") == "Cedar Grove, CA"
    assert classify.home_city("somewhere") == ""


def test_search_queries_use_the_strongest_evidence_first():
    assert research.queries("EJ Plumbing", ["6504870598"], "", "Cedar Grove, CA") == [
        '"650-487-0598" EJ Plumbing', "EJ Plumbing official website", "EJ Plumbing Cedar Grove, CA",
    ]
    assert research.queries("County of Sacramento", [], "Sacramento, California", "Cedar Grove, CA") \
        == ["County of Sacramento Sacramento, California"]


def test_confirm_ties_a_site_to_the_sender():
    def site(url, title="", phones=()):
        return research.Site(url, title, [], set(phones))

    assert "phone number" in research.confirm(
        "EJ Plumbing", ["6504870598"], site("https://ejhomeservices.com/", "EJ Home", ["6504870598"])
    )
    assert research.confirm("LeafFilter", [], site("https://www.leaffilter.com/"))
    assert research.confirm("County of Sacramento", [], site("https://saccounty.gov/", "Sacramento County"))
    assert research.confirm("EJ Plumbing", [], site("https://www.ejplumbing.com/"))
    # nothing distinctive in common: a different plumber, a directory, a same-word stranger
    assert research.confirm("EJ Plumbing", [], site("https://bestplumbing.com/", "Best Plumbing")) == ""
    assert research.confirm("Summit Bank", [], site("https://local.example/", "Banks near you")) == ""
    assert research.confirm("Hearth Trading Post", [], site("https://hearth.com/", "Hearth")) == ""


def test_research_picks_the_confirmed_site_not_the_first(monkeypatch):
    monkeypatch.setattr(research, "SEARCH_PAUSE", 0)
    asked = []
    results = {'"650-487-0598" EJ Plumbing': ["https://wrongplumber.com/", "https://ejhomeservices.com/"]}
    monkeypatch.setattr(research, "search", lambda query, api_key="": asked.append(query) or results.get(query, []))
    sites = {
        "https://wrongplumber.com/": research.Site("https://wrongplumber.com/", "Wrong Plumber", ["info@wrongplumber.com"], {"4155550100"}),
        "https://ejhomeservices.com/": research.Site("https://ejhomeservices.com/", "EJ Home Services", ["info@ejhomeservices.com"], {"6504870598"}),
    }
    monkeypatch.setattr(research, "crawl", sites.get)

    found = research.research("EJ Plumbing", phones=["6504870598"], home="Cedar Grove, CA")
    assert len(asked) == 1  # the phone search settled it, so no further searches were made
    assert "phone number" in found.reason
    assert (found.website, found.emails, found.verified) == (
        "https://ejhomeservices.com/", ["info@ejhomeservices.com"], True
    )
    # without the phone number nothing ties either site to the sender
    guess = research.research("EJ Plumbing")
    assert guess.verified is False and guess.website == ""
    results["EJ Plumbing official website"] = ["https://wrongplumber.com/"]
    guess = research.research("EJ Plumbing")
    assert (guess.website, guess.verified) == ("https://wrongplumber.com/", False)
    assert guess.candidates == ("https://wrongplumber.com/",)
    # a website printed on the piece or typed by the user is taken as given
    given = research.research("EJ Plumbing", "wrongplumber.com/")
    assert given.verified and given.emails == ["info@wrongplumber.com"]


def test_address_from_an_unconfirmed_site_is_filled_in_and_labelled(cfg, monkeypatch):
    pipeline.ingest(cfg, digest())
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Acme Catalog", "PRSRT STD"))
    monkeypatch.setattr(research, "research", lambda name, website="", **evidence: research.Result(
        "https://guess.example/", ["info@guess.example", "press@guess.example"], False, "",
        ("https://guess.example/",)
    ))
    pipeline.classify_pending(cfg)
    pipeline.create_drafts(cfg)
    pipeline.research_pending(cfg)
    with db.connect() as conn:
        draft = conn.execute("SELECT to_addr FROM drafts").fetchone()
        sender = conn.execute("SELECT * FROM senders").fetchone()
    assert draft["to_addr"] == "info@guess.example"
    assert sender["verified"] == 0 and sender["research_note"] == pipeline.UNCONFIRMED_NOTE
    server = web.make_server(cfg, pipeline.Worker(cfg), port=0)
    try:
        page = web.index(server, {})
    finally:
        server.server_close()
    form = page[page.index('name="to_addr"'):page.index(">Bcc</label>")]
    assert 'value="info@guess.example"' in form
    assert 'Found on low confidence site <a href="https://guess.example/"' in form
    assert "https://guess.example/</a> · low confidence" in page and "confirmed" not in page
    assert "Also found: press@guess.example" in form


def test_blocked_search_stops_the_pass_and_is_retried_later(cfg, monkeypatch):
    for i, name in enumerate(["Acme Catalog", "Hearth Trading"]):
        pipeline.ingest(cfg, digest(images=(SCAN + bytes([i]),)))
        monkeypatch.setattr(classify, "read", lambda image, name=name: scan_of(name, "PRSRT STD"))
        pipeline.classify_pending(cfg)
    calls = []

    def blocked(name, website="", **evidence):
        calls.append(name)
        raise research.LookupFailed("the search engine is limiting requests")

    monkeypatch.setattr(research, "research", blocked)
    pipeline.research_pending(cfg)
    assert len(calls) == 1  # the second sender was not attempted while blocked
    pipeline.create_drafts(cfg)

    monkeypatch.setattr(research, "research", lambda name, website="", **evidence: research.Result(
        "https://site.example/", ["optout@site.example"], True, "its name matches the sender"
    ))
    pipeline.research_pending(cfg)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT s.research_status, d.to_addr FROM senders s LEFT JOIN drafts d ON d.sender_id = s.id"
        ).fetchall()
    # both drafts existed before the lookup and both were filled in once it succeeded
    assert [tuple(r) for r in rows] == [("done", "optout@site.example")] * 2

    def always_blocked(name, website="", **evidence):
        raise research.LookupFailed("still blocked")

    with db.connect() as conn:
        conn.execute("UPDATE senders SET research_status = 'pending'")
    monkeypatch.setattr(research, "research", always_blocked)
    for _ in range(20):
        pipeline.research_pending(cfg)
    with db.connect() as conn:
        attempts = [r[0] for r in conn.execute("SELECT research_attempts FROM senders ORDER BY id")]
    assert max(attempts) <= pipeline.MAX_LOOKUP_ATTEMPTS + 1  # it gives up rather than knock forever


def test_search_uses_the_api_when_a_key_is_set(monkeypatch):
    import io
    import json as json_

    requests = []

    class Response(io.BytesIO):
        status = 200

    def fake_open(url, data=None, headers=None):
        requests.append((url, headers))
        return Response(json_.dumps({"web": {"results": [
            {"url": "https://www.yelp.com/biz/ej-plumbing"},
            {"url": "https://www.ejplumbing.com/contact"},
            {"url": "https://www.ejplumbing.com/about"},
            {"url": "ftp://odd.example/"},
            "not a result",
        ]}}).encode())

    monkeypatch.setattr(research, "_open", fake_open)
    assert research.search("EJ Plumbing Cedar Grove", "secret-key") == ["https://www.ejplumbing.com/"]
    url, headers = requests[0]
    assert url.startswith("https://api.search.brave.com/res/v1/web/search?q=EJ+Plumbing")
    assert headers["X-Subscription-Token"] == "secret-key"

    def refused(url, data=None, headers=None):
        raise research.urllib.error.HTTPError(url, 401, "Unauthorized", None, None)

    monkeypatch.setattr(research, "_open", refused)
    with pytest.raises(research.LookupFailed, match="HTTP 401"):
        research.search("anything", "bad-key")


def test_version_is_the_same_everywhere():
    import app

    manifest = (Path(__file__).parent.parent / "informed_unsubscribe" / "config.yaml").read_text()
    assert f'version: "{app.__version__}"' in manifest


def test_every_verdict_names_its_rule():
    cases = {
        "": "no_text", "PRSRT STD": "bulk_marking", "Invoice enclosed": "bill_wording",
        "Big sale, 20% off": "offer_wording", "FIRST-CLASS MAIL": "official_marking",
        "Aunt Carol": "no_rule",
    }
    assert {text: classify.categorize(text).rule for text in cases} == cases
    assert classify.categorize("x", remembered="real").rule == "sender_memory"
    assert classify.categorize("x", campaign=True).rule == "campaign"


def test_corrections_keep_the_apps_own_answer_and_can_be_exported(cfg, monkeypatch):
    import json as json_

    pipeline.ingest(cfg, digest())
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Bobs Plumbing", "Big sale, 20% off"))
    pipeline.classify_pending(cfg)
    server = web.make_server(cfg, pipeline.Worker(cfg), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert json_.loads(urllib.request.urlopen(base + "/corrections.json").read()) == {"redacted": True, "corrections": []}
        data = urllib.parse.urlencode({"category": "real", "sender": "Bob\'s Plumbing Co"}).encode()
        page = urllib.request.urlopen(urllib.request.Request(base + "/pieces/1", data=data)).read().decode()
        assert "The app had said junk mail." in page
        response = urllib.request.urlopen(base + "/corrections.json")
        assert "attachment" in response.headers["Content-Disposition"]
        (record,) = json_.loads(response.read())["corrections"]
    finally:
        server.shutdown()
        server.server_close()
    import app

    assert record["category_changed"] is True and record["app_version"] == app.__version__
    assert record["app"] == {
        "category": "marketing", "rule": "offer_wording", "confidence": 0.7,
        "reason": "Offer wording: 20% off, sale.", "sender": "Bobs Plumbing", "addressee_read": False,
        "resident": None,
    }
    assert record["you"]["category"] == "real" and record["corrected_at"]
    assert "Big sale" in record["text"]



def test_app_password_copied_from_google_is_cleaned(tmp_path, monkeypatch):
    options = tmp_path / "options.json"
    options.write_text('{"email_mailbox_password": "abcd\\u00a0efgh\\u00a0ijkl\\u00a0mnop "}')
    monkeypatch.setenv("OPTIONS_PATH", str(options))
    assert config.load().email_mailbox_password == "abcdefghijklmnop"
    options.write_text('{"email_mailbox_password": "pass phrase with spaces"}')
    assert config.load().email_mailbox_password == "pass phrase with spaces"


def test_drafts_appear_before_the_slow_website_lookup_and_get_filled_in_after(cfg, monkeypatch):
    pipeline.ingest(cfg, digest())
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Acme Catalog", "PRSRT STD"))
    seen_during_lookup = []

    def slow_lookup(name, website="", **evidence):
        with db.connect() as conn:
            seen_during_lookup.append(conn.execute("SELECT to_addr FROM drafts").fetchall())
        return research.Result("https://acme.com/", ["optout@acme.com"], True, "its name matches the sender")

    monkeypatch.setattr(research, "research", slow_lookup)
    worker = pipeline.Worker(cfg)
    activities = []
    monkeypatch.setattr(pipeline, "create_drafts", lambda c, real=pipeline.create_drafts: (
        activities.append(worker.activity), real(c)
    ))
    worker.cycle()
    assert [tuple(r) for r in seen_during_lookup[0]] == [("",)]  # draft already there, no address yet
    with db.connect() as conn:
        assert conn.execute("SELECT to_addr FROM drafts").fetchone()[0] == "optout@acme.com"
    assert activities == ["Processing mail"] and worker.activity == "" and worker.last_run


def test_worker_waits_for_the_interval_or_until_the_daily_time():
    from datetime import datetime

    morning = datetime(2026, 1, 15, 5, 30)
    assert pipeline.seconds_until_next_run(config.Config(hours_between_runs=6), morning) == 6 * 3600
    daily = config.Config(run_at_time="06:00")
    assert pipeline.seconds_until_next_run(daily, morning) == 30 * 60
    # A run that ends at or after the time waits for the next day's, whatever the interval says.
    assert pipeline.seconds_until_next_run(daily, datetime(2026, 1, 15, 6, 0)) == 24 * 3600
    assert pipeline.seconds_until_next_run(daily, datetime(2026, 1, 15, 18, 0)) == 12 * 3600


def test_page_says_when_the_app_runs(cfg):
    def page(**settings):
        server = web.make_server(config.Config(data_dir=cfg.data_dir, **settings), pipeline.Worker(cfg), port=0)
        try:
            return web.index(server, {})
        finally:
            server.server_close()

    assert "runs every 12 h" in page(hours_between_runs=12)
    assert "runs daily at 06:00" in page(run_at_time="06:00") and "runs every" not in page(run_at_time="06:00")


def test_status_reports_activity_and_changes_when_results_do(cfg):
    import json as json_

    worker = pipeline.Worker(cfg)
    server = web.make_server(cfg, worker, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        before = json_.loads(urllib.request.urlopen(base + "/status").read())
        assert before["activity"] == ""
        urllib.request.urlopen(urllib.request.Request(base + "/check", data=b"")).read()
        pipeline.ingest(cfg, digest())
        after = json_.loads(urllib.request.urlopen(base + "/status").read())
        assert after["activity"] == "Processing mail" and after["results"] != before["results"]
        assert "Processing mail…" in urllib.request.urlopen(base + "/").read().decode()
    finally:
        server.shutdown()
        server.server_close()


def test_mailbox_is_read_without_changing_it_and_each_email_handled_once(cfg, monkeypatch):
    inbox = {b"1": digest(), b"2": digest(images=(SCAN + b"x",)), b"3": b"Subject: hi\r\n\r\nnot a digest"}
    inbox[b"1"] = b"Message-ID: <one@usps>\r\n" + inbox[b"1"]
    inbox[b"2"] = b"Message-ID: <two@usps>\r\n" + inbox[b"2"]
    log = []

    class FakeImap:
        def __init__(self, host, port): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def login(self, user, password): pass
        def select(self, box, readonly=False): log.append(("select", box, readonly))
        def search(self, charset, *criteria):
            log.append(("search",) + criteria)
            return "OK", [b" ".join(sorted(inbox))]
        def store(self, *args): raise AssertionError("the mailbox must not be modified")
        def fetch(self, num, what):
            log.append(("fetch", num, "header" if "HEADER" in what else "full"))
            raw = inbox[num]
            body = raw.split(b"\r\n", 1)[0] + b"\r\n\r\n" if "HEADER" in what and raw.startswith(b"Message-ID") else (b"\r\n" if "HEADER" in what else raw)
            return "OK", [(b"x", body), b")"]

    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeImap)
    box = config.Config(
        data_dir=cfg.data_dir, email_address="box@example.org", email_mailbox_password="x"
    )
    def since(days):
        start = date.today() - timedelta(days=days)
        return f"{start.day:02d}-{mail._MONTHS[start.month - 1]}-{start.year}"

    assert pipeline.check_mailbox(box) == 3
    assert log[0] == ("select", "INBOX", True) and log[1] == ("search", "SINCE", since(5))  # a first run
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pieces").fetchone()[0] == 2
    log.clear()
    assert pipeline.check_mailbox(box) == 0  # already handled, read or unread makes no difference
    assert [entry[2] for entry in log if entry[0] == "fetch"] == ["header"] * 3
    assert log[1] == ("search", "SINCE", since(14))  # later runs look further back


def test_wrong_email_mailbox_password_is_reported_plainly(cfg, monkeypatch):
    class Rejecting:
        def __init__(self, host, port): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def login(self, user, password):
            raise mail.imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", Rejecting)
    worker = pipeline.Worker(config.Config(
        data_dir=cfg.data_dir, email_address="box@example.org", email_mailbox_password="wrong"
    ))
    worker.cycle()
    assert worker.errors == ["Mailbox: " + mail.LOGIN_HELP]


def _served(cfg):
    worker = pipeline.Worker(cfg)
    server = web.make_server(cfg, worker, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path, **fields):
        request = urllib.request.Request(base + path, data=urllib.parse.urlencode(fields).encode())
        return urllib.request.urlopen(request).read().decode()

    return server, post


def _section(page, heading):
    """The part of the page under one heading, up to the next section."""
    start = page.index(heading)
    ends = [page.find(marker, start + 1) for marker in ("<h2>", '<details class="section"')]
    return page[start:min([e for e in ends if e > 0], default=len(page))]


def test_editing_a_cards_facts_removes_or_rewrites_its_request(tmp_path, monkeypatch):
    db.init(str(tmp_path))
    home = config.Config(data_dir=str(tmp_path), resident_last_names=("Doe",),
                         physical_mail_address="12 Oak Court, Springfield IL 62704")
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result(website, [], bool(website)))
    scans = [
        envelope(["PAT DOE"], sender="Acme Catalog", extra=["PRSRT STD"]),     # 1: junk
        envelope(["KIM NGUYEN"], sender="Summit Bank", extra=["FIRST-CLASS"]),  # 2: flagged non-resident
        envelope(["PAT DOE"], sender="Hearth Hame", extra=["PRSRT STD"]),       # 3: junk, sender misread
        envelope(["KIM NGUYEN"], sender="Birch Dental", extra=["PRSRT STD"]),   # 4: junk for a "non-resident"
    ]
    for i, scan in enumerate(scans):
        pipeline.ingest(home, digest(images=(SCAN + bytes([i]),)))
        monkeypatch.setattr(classify, "read", lambda image, scan=scan: scan)
        pipeline.classify_pending(home)
    pipeline.create_drafts(home)
    pipeline.research_pending(home)
    server, post = _served(home)
    try:
        page = post("/check")
        cards = _section(page, "For Review (4)")
        # one way to correct a card, and none of the old one-off buttons
        assert cards.count('class="pencil"') == 12 and ">Update</button>" not in cards
        assert cards.count("Reason: Bulk-mail marking") == 3
        assert not any(old in page for old in ("They live here", "Wrong sender", "Use this website", "Make Corrections"))
        assert '<span class="value">KIM NGUYEN</span>' in cards and "Bulk-mail marking" in cards
        assert cards.count("Is current resident") == 4 and cards.count('name="lives_here" value="1" checked') == 2

        post("/pieces/1", sender="Acme Catalog", recipient="PAT DOE", category="real")
        post("/pieces/2", sender="Summit Bank", recipient="KIM NGUYEN", lives_here="1", category="real")
        post("/pieces/3", sender="Hearth & Home", recipient="PAT DOE", category="marketing",
             website="hearthandhome.example", website_was="")
        post("/pieces/4", sender="Birch Dental", recipient="KIM NGUYEN", lives_here="1", category="marketing")
        pipeline.research_pending(home)
        page = post("/check")
    finally:
        server.shutdown()
        server.server_close()

    with db.connect() as conn:
        pieces = {r["id"]: r for r in conn.execute(
            "SELECT p.*, s.name, s.website FROM pieces p JOIN senders s ON s.id = p.sender_id")}
        drafts = [tuple(r) for r in conn.execute(
            "SELECT s.name, d.kind, d.status FROM drafts d JOIN senders s ON s.id = d.sender_id ORDER BY d.id")]
    assert (pieces[1]["category"], pieces[1]["manual"], pieces[1]["auto_category"]) == ("real", 1, "marketing")
    assert (pieces[2]["resident"], pieces[2]["auto_resident"]) == (1, 0)
    assert pieces[3]["name"] == "Hearth & Home" and pieces[3]["auto_sender"] == "Hearth Hame"
    assert pieces[3]["website"] == "hearthandhome.example"
    assert all(p["corrected_at"] for p in pieces.values())
    assert drafts == [
        # gone: Acme Catalog (said to be real mail), Summit Bank (she lives here, and it isn't
        # junk), Birch Dental's "moved" (she lives here...)
        ("Hearth & Home", "optout", "draft"),       # rewritten for the right sender
        ("Birch Dental", "optout", "draft"),        # ...but it is junk, so an opt-out instead
    ]
    assert "For Review (2)" in page and "from Hearth &amp; Home" in page


def test_not_sure_pieces_are_the_same_card_waiting_for_an_answer(cfg, monkeypatch):
    for i, lines in enumerate([("Aunt Carol",), ("Bobs Plumbing",), ()]):
        pipeline.ingest(cfg, digest(images=(SCAN + bytes([i]),)))
        scan = scan_of(*lines) if lines else classify.Scan(
            [classify.Line("PRSRT STD", 1500, 60)], 2000, 1000)  # junk, but no sender readable
        monkeypatch.setattr(classify, "read", lambda image, scan=scan: scan)
        pipeline.classify_pending(cfg)
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result("", [], False))
    server, post = _served(cfg)
    try:
        queue = _section(post("/check"), "For Review (3)")
        assert queue.count("No request written yet") == 3
        # the sender nobody could read is an empty field straight away, and Type says "Unclassified"
        assert queue.count('class="field editing" data-empty') >= 1
        assert queue.count('<option value="" selected>Unclassified</option>') == 2

        post("/pieces/1", sender="Aunt Carol", recipient="", category="real")
        post("/pieces/2", sender="Bobs Plumbing", recipient="", category="marketing")
        page = post("/pieces/3", sender="Acme Catalog", recipient="")  # only the missing sender
    finally:
        server.shutdown()
        server.server_close()
    # the two junk cards stay where they were, now with a request and a note that the
    # sender's website is being looked up; the real-mail one has left the queue
    assert "For Review (2)" in page and page.count("</span>Processing…</span>") == 4  # per card: Website and To
    assert page.count('value="send" data-waiting disabled>Send</button>') == 2  # nothing to send to yet
    assert page.index("Acme Catalog") < page.index("Bobs Plumbing")  # newest piece still first
    pipeline.research_pending(cfg)
    server = web.make_server(cfg, pipeline.Worker(cfg), port=0)
    try:
        page = web.index(server, {})
    finally:
        server.server_close()
    assert "For Review (2)" in page and "</span>Processing…</span>" not in page
    assert "data-waiting" not in page.split("<script>")[0]  # Send is usable again
    assert "Please remove this address from your postal mailing list" in page
    with db.connect() as conn:
        rows = [tuple(r) for r in conn.execute("SELECT category, manual FROM pieces ORDER BY id")]
    assert rows == [("real", 1), ("marketing", 1), ("marketing", 0)]  # the third kept the app's verdict


def test_skip_sets_a_request_aside_until_the_sender_writes_again(tmp_path, monkeypatch):
    db.init(str(tmp_path))
    home = config.Config(data_dir=str(tmp_path), resident_last_names=("Doe",),
                         physical_mail_address="12 Oak Court, Springfield IL 62704")
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result("", [], False))
    count = [0]

    def arrives(scan):
        count[0] += 1
        pipeline.ingest(home, digest(images=(SCAN + bytes([count[0]]),)))
        monkeypatch.setattr(classify, "read", lambda image: scan)
        pipeline.classify_pending(home)
        pipeline.create_drafts(home)

    def requests():
        with db.connect() as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT s.name, d.kind, d.status FROM drafts d JOIN senders s ON s.id = d.sender_id ORDER BY d.id")]

    junk = envelope(["PAT DOE"], sender="Acme Catalog", extra=["PRSRT STD"])
    stranger = envelope(["JOHN SMITH"], sender="Summit Bank", extra=["FIRST-CLASS"])
    arrives(junk)
    arrives(stranger)
    server, post = _served(home)
    try:
        post("/drafts/1", action="skip")
        post("/drafts/2", action="skip")
        pipeline.create_drafts(home)
        assert requests() == [("Acme Catalog", "optout", "skipped"), ("Summit Bank", "moved", "skipped")]
        page = post("/check")
        assert "For Review (0)" in page
        assert page.count("Reopen the skipped request") == 2

        # more mail from both senders: each gets a fresh request
        arrives(junk)
        arrives(stranger)
        assert requests()[2:] == [("Acme Catalog", "optout", "draft"), ("Summit Bank", "moved", "draft")]
        # History is per piece: the earlier mail still shows its own skipped request
        with db.connect() as conn:
            conn.execute("UPDATE senders SET research_status = 'done'")
        history = _section(web.index(server, {}), "<h2>History</h2>")
        import re

        states = re.findall(r"<span>(email in review|skipped)</span>", history)
        assert states == ["email in review", "email in review", "skipped", "skipped"]  # newest first
        # each piece's request stands alone: an old skipped one can be reopened beside a new one
        post("/drafts/3", action="skip")
        post("/drafts/1", action="reopen")
        pipeline.create_drafts(home)
        assert [r[2] for r in requests()] == ["draft", "skipped", "skipped", "draft"]
        states = re.findall(r"<span>(email in review|skipped)</span>",
                            _section(web.index(server, {}), "<h2>History</h2>"))
        assert states == ["email in review", "skipped", "skipped", "email in review"]
    finally:
        server.shutdown()
        server.server_close()


def test_settings_link_points_at_home_assistants_configuration_page(cfg, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert web.settings_url() == ""
    assert "Settings</a>" not in web.index(web.make_server(cfg, pipeline.Worker(cfg), port=0), {})
    monkeypatch.setenv("SUPERVISOR_TOKEN", "x")
    monkeypatch.setenv("HOSTNAME", "local-informed-unsubscribe")
    assert web.settings_url() == "/config/app/local_informed_unsubscribe/config"
    server = web.make_server(cfg, pipeline.Worker(cfg), port=0)
    try:
        page = web.index(server, {})
        assert '<a href="/config/app/local_informed_unsubscribe/config" target="_top">Settings</a>' in page
        assert page.index("runs every") < page.index("Settings</a>") < page.index('id="run-now"')
    finally:
        server.server_close()


def test_history_is_paged_and_filtered_by_sender(cfg, monkeypatch):
    monkeypatch.setattr(web, "HISTORY_PAGE", 2)
    with db.connect() as conn:
        for i, name in enumerate(["Acme Catalog", "Summit Bank", "Acme Catalog", "City Water", "Acme Catalog"]):
            conn.execute("INSERT INTO pieces(sha, image_path, received, sender_id, category) VALUES (?, '', ?, ?, 'real')",
                         (str(i), f"2026-10-0{i + 1}T00:00:00+00:00", db.upsert_sender(conn, name)))
    server = web.make_server(cfg, pipeline.Worker(cfg), port=0)
    try:
        first = _section(web.index(server, {}), "<h2>History</h2>")
        assert first.count("<details>") == 2 and "2026 Oct 5" in first and "Older" in first and "Newer" not in first
        last = _section(web.index(server, {"page": "3"}), "<h2>History</h2>")
        assert last.count("<details>") == 1 and "2026 Oct 1" in last and "Older" not in last and "Newer" in last
        acme = _section(web.index(server, {"sender": "acme"}), "<h2>History</h2>")
        assert acme.count("<details>") == 2 and "Summit" not in acme and "sender=acme" in acme
        assert "No mail from or addressed to a name like that" in web.index(server, {"sender": "nobody"})
        # the same box finds mail by who it is addressed to
        with db.connect() as conn:
            conn.execute("UPDATE pieces SET recipient = 'VAN CHUNG' WHERE id IN (2, 4)")
            conn.execute("UPDATE pieces SET recipient = 'PAT DOE' WHERE id NOT IN (2, 4)")
        chung = _section(web.index(server, {"sender": "chung"}), "<h2>History</h2>")
        assert chung.count("<details>") == 2 and "Summit Bank" in chung and "City Water" in chung
        assert "Acme" not in chung
    finally:
        server.server_close()


def test_old_settled_scans_are_deleted_but_their_rows_kept(cfg, monkeypatch):
    scans = [
        envelope(["PAT DOE"], sender="City Water", extra=["FIRST-CLASS"]),   # 1: old, settled
        envelope(["PAT DOE"], sender="Acme Catalog", extra=["PRSRT STD"]),   # 2: old, request still open
        scan_of("Aunt Carol"),                                                # 3: old, never decided
        envelope(["PAT DOE"], sender="Gas Company", extra=["FIRST-CLASS"]),  # 4: recent
    ]
    for i, scan in enumerate(scans):
        pipeline.ingest(cfg, digest(images=(SCAN + bytes([i]),)))
        monkeypatch.setattr(classify, "read", lambda image, scan=scan: scan)
        pipeline.classify_pending(cfg)
    pipeline.create_drafts(cfg)
    with db.connect() as conn:
        conn.execute("UPDATE pieces SET received = '2026-01-01T00:00:00+00:00' WHERE id < 4")
        paths = {r["id"]: Path(r["image_path"]) for r in conn.execute("SELECT id, image_path FROM pieces")}
    # kept while younger than the setting allows
    assert pipeline.prune_images(dataclasses.replace(cfg, days_to_keep_scans=3650)) == 0
    assert pipeline.prune_images(cfg) == 1 and pipeline.prune_images(cfg) == 0
    assert [paths[i].exists() for i in (1, 2, 3, 4)] == [False, True, True, True]
    server = web.make_server(cfg, pipeline.Worker(cfg), port=0)
    try:
        page = web.index(server, {})
        assert web.image(server, {}, "1") is None and web.image(server, {}, "2") is not None
    finally:
        server.server_close()
    assert "City Water" in page and "The scan is no longer kept." in page


@pytest.mark.parametrize("login", [
    {}, {"email_address": "box@example.org"}, {"email_mailbox_password": "x"},
])
def test_missing_mailbox_login_is_an_error_not_a_silent_skip(cfg, login):
    worker = pipeline.Worker(config.Config(data_dir=cfg.data_dir, **login))
    worker.cycle()
    assert worker.errors == ["Mailbox: " + mail.MISSING_LOGIN]
    server = web.make_server(cfg, worker, port=0)
    try:
        assert 'class="error">Mailbox: the Email address or Email mailbox password' in web.index(server, {})
    finally:
        server.server_close()


def test_history_shows_the_state_of_each_unsubscribe_request(cfg):
    assert web.short_date("2026-10-20T09:15:00+00:00") == "2026 Oct 20"
    assert web.short_date("2027-01-05T00:00:00+00:00") == "2027 Jan 5"
    NAMES = ["Acme Outfitters", "Birch Dental", "Cedar Roofing", "Ember Pizza"]
    with db.connect() as conn:
        for i, status in enumerate(["draft", "sent", "skipped", None]):
            sender = db.upsert_sender(conn, NAMES[i])
            conn.execute("INSERT INTO pieces(sha, image_path, received, sender_id, category) "
                         "VALUES (?, '', '2026-10-20T00:00:00+00:00', ?, ?)",
                         (str(i), sender, "marketing" if status else "real"))
            if status:
                conn.execute("INSERT INTO drafts(sender_id, piece_id, subject, body, status, sent_at) "
                             "VALUES (?, ?, 's', 'b', ?, '2026-10-20T09:15:00+00:00')", (sender, i + 1, status))
        conn.execute("UPDATE senders SET research_status = 'done'")
    server = web.make_server(cfg, pipeline.Worker(cfg), port=0)
    try:
        history = _section(web.index(server, {}), "<h2>History</h2>")
    finally:
        server.server_close()
    assert "<span>Mail Type</span><span>Unsubscribe Request</span>" in history
    import re

    states = [re.sub(r"<[^>]+>", "", history[history.index(name):].split("<span>")[3].split("</span>")[0])
              for name in NAMES]
    assert '<time class="when"> 2026 Oct 20</time>' in history  # the part hidden on a phone
    assert states == ["email in review", "email sent 2026 Oct 20", "skipped", "NA"]


def test_footer_and_export_of_misclassified_mail(cfg, monkeypatch):
    import json as json_

    import app

    for i, name in enumerate(["Acme Catalog", "City Water"]):
        pipeline.ingest(cfg, digest(images=(SCAN + bytes([i]),)))
        monkeypatch.setattr(classify, "read", lambda image, name=name: scan_of(name, "PRSRT STD"))
        pipeline.classify_pending(cfg)
    server, post = _served(cfg)
    try:
        post("/pieces/1", category="marketing", sender="Acme Catalog", recipient="")  # agrees with the app
        page = post("/pieces/2", category="real", sender="City Water", recipient="")   # overrules it
        exported = json_.loads(web.corrections(server, {}).body)["corrections"]
    finally:
        server.shutdown()
        server.server_close()
    import re

    footer = page[page.index("<footer"):page.index("</footer>")]
    assert f"v{app.__version__}" in footer
    assert f'href="mailto:{web.FEEDBACK_EMAIL}"' in footer
    # the download links are commented out for now; the export itself still works
    shown = re.sub(r"<!--.*?-->", "", footer, flags=re.S)
    assert "corrections" not in shown and "corrections.json" not in page[:page.index("<footer")]
    assert [record["piece"] for record in exported] == [2]


def test_exported_text_has_names_and_addresses_blanked():
    home = config.Config(resident_last_names=("Whitfield",),
                         physical_mail_address="4382 Wellington Ave, Cedar Grove, CA 95061")
    text = "\n".join([
        "County of Sacramento", "Department of Finance", "700 H Street, Room 1710",
        "Sacramentn, California 95814", "TAX BILL - OPEN IMMEDIATELY", "PRESORTED FIRST CLASS",
        "MAILED TO:", "ROBERT AND MARY HALE 2024 FAMILY TRUST", "43B2 WELLINGTON", "CEDAR GROVE, CA 95061",
        "The Whitfieid Residence", "PO Box 77", "Save up to 35% OFF", "0% Interest for 12 Months",
        "Call (650) 395-9146", "CA License 1035795",
    ])
    stripped = classify.strip_personal(text, {"ROBERT AND MARY HALE 2024 FAMILY TRUST"}, home).splitlines()
    assert stripped == [
        "County of Sacramento", "Department of Finance", "[address removed]",
        "[address removed]", "TAX BILL - OPEN IMMEDIATELY", "PRESORTED FIRST CLASS",
        "MAILED TO:", "[name removed]", "[address removed]", "[address removed]",
        "[name removed]", "[address removed]", "Save up to 35% OFF", "0% Interest for 12 Months",
        "Call (650) 395-9146", "CA License 1035795",
    ]


def test_export_carries_no_addressee(tmp_path, monkeypatch):
    import json as json_

    db.init(str(tmp_path))
    home = config.Config(data_dir=str(tmp_path), resident_last_names=("Doe",),
                         physical_mail_address="12 Oak Court, Springfield IL 62704")
    pipeline.ingest(home, digest())
    monkeypatch.setattr(classify, "read", lambda image: envelope(["JOHN SMITH"], extra=["FIRST-CLASS"]))
    pipeline.classify_pending(home)
    pipeline.create_drafts(home)
    server, post = _served(home)
    try:
        page = post("/pieces/1", sender="Summit Bank", recipient="JOHN SMITH", lives_here="1")
        exported = web.corrections(server, {}).body.decode()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        full = urllib.request.urlopen(base + "/corrections-full.json")
        assert "corrections-full.json" in full.headers["Content-Disposition"]
        full = json_.loads(full.read())
    finally:
        server.shutdown()
        server.server_close()
    (record,) = json_.loads(exported)["corrections"]
    assert (record["app"]["resident"], record["you"]["resident"]) == (0, 1)
    assert record["app"]["addressee_read"] is True and "Summit Bank" in record["text"]
    assert not any(word in exported for word in ("SMITH", "OAK COURT", "62704", "500 Market"))
    # the non-redacted file is the same record with everything as read
    assert full["redacted"] is False and "12 OAK COURT" in full["corrections"][0]["text"]
    assert full["corrections"][0]["addressee"] == {"app": "JOHN SMITH", "you": "JOHN SMITH"}


def test_asking_for_a_new_lookup_keeps_the_request_on_the_page(cfg, monkeypatch):
    pipeline.ingest(cfg, digest())
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Acme Catalog", "PRSRT STD"))
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result("", [], False))
    pipeline.classify_pending(cfg)
    pipeline.create_drafts(cfg)
    pipeline.research_pending(cfg)
    server, post = _served(cfg)
    try:
        page = post("/pieces/1", sender="Acme Catalog", recipient="", category="marketing",
                    website="acme.example", website_was="")
    finally:
        server.shutdown()
        server.server_close()
    assert "For Review (1)" in page


def test_bcc_is_a_field_on_the_request_filled_from_the_setting(tmp_path, monkeypatch):
    db.init(str(tmp_path))
    home = config.Config(data_dir=str(tmp_path), bcc_email_address="me@example.net")
    pipeline.ingest(home, digest())
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Acme Catalog", "PRSRT STD"))
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result("", [], False))
    pipeline.classify_pending(home)
    pipeline.create_drafts(home)
    pipeline.research_pending(home)
    sent = []
    monkeypatch.setattr(mail, "send", lambda cfg, to, subject, body, bcc=None, **kw: sent.append((to, bcc)))
    server, post = _served(home)
    try:
        page = post("/check")
        form = page[page.index('name="to_addr"'):page.index('name="body"')]
        assert page.count("me@example.net") == 1
        assert form.index(">Bcc</label>") < form.index('name="bcc" type="email" value="me@example.net"') < form.index(">Subject</label>")
        fields = {"action": "send", "to_addr": "optout@acme.com", "subject": "s", "body": "b"}
        assert "The Bcc address isn" in post("/drafts/1", bcc="not an address", **fields)
        assert not sent
        post("/drafts/1", bcc="", **fields)  # cleared for this one request
    finally:
        server.shutdown()
        server.server_close()
    assert sent == [("optout@acme.com", "")]


def test_a_run_in_the_helper_process_reports_back(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPTIONS_PATH", str(tmp_path / "none.json"))
    for name in ("EMAIL_ADDRESS", "EMAIL_MAILBOX_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    # a real second process: no mailbox is set, so that is the one thing it reports
    assert pipeline.run_in_helper() == ["Mailbox: " + mail.MISSING_LOGIN]
    assert (tmp_path / "informed_unsubscribe.db").exists()

    (tmp_path / "broken.json").write_text("{not json")
    monkeypatch.setenv("OPTIONS_PATH", str(tmp_path / "broken.json"))
    assert pipeline.run_in_helper() == ["The run stopped unexpectedly. The app's log has the details."]


def test_page_says_whether_the_last_run_succeeded(cfg):
    worker = pipeline.Worker(cfg, run=lambda cfg: [])
    worker.cycle()
    server = web.make_server(cfg, worker, port=0)
    try:
        assert "succeeded" in web.index(server, {}) and "had errors" not in web.index(server, {})
        worker.do_run = lambda cfg: ["Mailbox: nope"]
        worker.cycle()
        page = web.index(server, {})
    finally:
        server.server_close()
    assert "had errors" in page and 'class="error">Mailbox: nope' in page


def test_nothing_reaches_history_before_it_reaches_for_review(cfg, monkeypatch):
    def page():
        server = web.make_server(cfg, pipeline.Worker(cfg), port=0)
        try:
            html = web.index(server, {})
        finally:
            server.server_close()
        return html[html.index("For Review ("):html.index("<h2>History</h2>")], _section(html, "<h2>History</h2>")

    pipeline.ingest(cfg, digest(images=(SCAN, SCAN + b"2", SCAN + b"3")))
    review, history = page()
    assert "For Review (0)" in review and "No mail yet." in history  # nothing read yet

    scans = iter([scan_of("Acme Catalog", "PRSRT STD"), scan_of("City Water", "FIRST-CLASS MAIL"),
                  scan_of("Aunt Carol")])
    monkeypatch.setattr(classify, "read", lambda image: next(scans))
    pipeline.classify_pending(cfg)
    review, history = page()
    # read, but Acme's request isn't written yet: it is in neither list
    assert "Acme" not in review and "Acme" not in history
    assert "City Water" in history and "Aunt Carol" in review and "Aunt Carol" in history

    pipeline.create_drafts(cfg)
    review, history = page()
    assert "For Review (1)" in review and "Acme" not in review and "Acme" not in history

    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result("", [], False))
    pipeline.research_pending(cfg)
    review, history = page()
    assert "For Review (2)" in review and "Acme Catalog" in review
    assert "Acme Catalog" in history and "email in review" in history and "in review</span>" in history


def test_a_runs_results_all_appear_together_when_it_finishes(cfg, monkeypatch):
    def arrives(name, marking, n):
        pipeline.ingest(cfg, digest(images=(SCAN + bytes([n]),)))
        monkeypatch.setattr(classify, "read", lambda image: scan_of(name, marking))
        pipeline.classify_pending(cfg)

    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result("", [], False))
    arrives("Old Catalog", "PRSRT STD", 0)   # from an earlier run, already on the page
    pipeline.create_drafts(cfg)
    pipeline.research_pending(cfg)
    during = []

    def a_run(cfg):
        arrives("City Water", "FIRST-CLASS MAIL", 1)  # real mail: would go straight to History
        arrives("Aunt Carol", "", 2)                   # not sure: would go straight to For Review
        arrives("New Catalog", "PRSRT STD", 3)
        pipeline.create_drafts(cfg)
        during.append(web.index(server, {}))           # the page as seen mid-run
        pipeline.research_pending(cfg)
        return []

    worker = pipeline.Worker(cfg, run=a_run)
    server = web.make_server(cfg, worker, port=0)
    try:
        worker.cycle()
        after = web.index(server, {})
    finally:
        server.server_close()
    mid = during[0]
    assert "Old Catalog" in mid and "For Review (1)" in mid
    assert not any(name in mid for name in ("City Water", "Aunt Carol", "New Catalog"))
    assert all(name in after for name in ("City Water", "Aunt Carol", "New Catalog"))
    assert "For Review (3)" in after and worker.hide_after is None


def test_only_actions_that_need_a_lookup_start_a_run(tmp_path, monkeypatch):
    db.init(str(tmp_path))
    home = config.Config(data_dir=str(tmp_path), resident_last_names=("Doe",),
                         physical_mail_address="12 Oak Court, Springfield IL 62704")
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result("", [], False))
    scans = [
        envelope(["PAT DOE"], sender="Acme Catalog", extra=["PRSRT STD"]),
        envelope(["PAT DOE"], sender="Birch Dental", extra=["PRSRT STD"]),
        envelope(["PAT DOE"], sender="Cedar Roofing", extra=["PRSRT STD"]),
        envelope(["JOHN SMITH"], sender="Summit Bank", extra=["FIRST-CLASS"]),
    ]
    for i, scan in enumerate(scans):
        pipeline.ingest(home, digest(images=(SCAN + bytes([i]),)))
        monkeypatch.setattr(classify, "read", lambda image, scan=scan: scan)
        pipeline.classify_pending(home)
    pipeline.create_drafts(home)
    pipeline.research_pending(home)
    worker = pipeline.Worker(home)
    server = web.make_server(home, worker, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def starts_a_run(path, **fields):
        worker.wake.clear()
        worker.activity = ""
        urllib.request.urlopen(urllib.request.Request(base + path, data=urllib.parse.urlencode(fields).encode())).read()
        return worker.wake.is_set()

    try:
        assert not starts_a_run("/drafts/1", action="skip")
        assert not starts_a_run("/pieces/2", sender="Birch Dental", recipient="PAT DOE", category="real")
        assert not starts_a_run("/pieces/4", sender="Summit Bank", recipient="JOHN SMITH", lives_here="1")
        assert not starts_a_run("/drafts/3", action="save", to_addr="a@b.co", subject="s", body="b")
        # a sender the app hasn't looked up yet does need one
        assert starts_a_run("/pieces/3", sender="Delta Motors", recipient="PAT DOE", category="marketing")
        # and so does giving a different website for a sender already looked up
        assert starts_a_run("/pieces/3", sender="Delta Motors", recipient="PAT DOE", category="marketing",
                            website="deltamotors.example", website_was="")
    finally:
        server.shutdown()
        server.server_close()


def test_a_correction_can_be_undone_by_putting_the_fact_back(tmp_path, monkeypatch):
    db.init(str(tmp_path))
    home = config.Config(data_dir=str(tmp_path), resident_last_names=("Doe",),
                         physical_mail_address="12 Oak Court, Springfield IL 62704")
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result("", [], False))
    for i, scan in enumerate([envelope(["JOHN SMITH"], sender="Summit Bank", extra=["FIRST-CLASS"]),
                              envelope(["PAT DOE"], sender="Acme Catalog", extra=["PRSRT STD"])]):
        pipeline.ingest(home, digest(images=(SCAN + bytes([i]),)))
        monkeypatch.setattr(classify, "read", lambda image, scan=scan: scan)
        pipeline.classify_pending(home)
    pipeline.create_drafts(home)
    pipeline.research_pending(home)

    def requests():
        with db.connect() as conn:
            return [tuple(r) for r in conn.execute("SELECT kind, status FROM drafts ORDER BY id")]

    server, post = _served(home)
    try:
        smith = dict(sender="Summit Bank", recipient="JOHN SMITH", category="real")
        acme = dict(sender="Acme Catalog", recipient="PAT DOE")
        post("/pieces/1", lives_here="1", **smith)       # ticked by mistake...
        post("/pieces/2", category="real", **acme)        # ...and the wrong type chosen
        assert requests() == []
        post("/pieces/1", **smith)                         # unticked again
        page = post("/pieces/2", category="marketing", **acme)
    finally:
        server.shutdown()
        server.server_close()
    assert requests() == [("moved", "draft"), ("optout", "draft")] and "For Review (2)" in page


def test_a_request_removed_by_a_correction_is_written_again_when_due(tmp_path, monkeypatch):
    db.init(str(tmp_path))
    home = config.Config(data_dir=str(tmp_path), resident_last_names=("Doe",),
                         physical_mail_address="12 Oak Court, Springfield IL 62704")
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result("", [], False))
    pipeline.ingest(home, digest())
    monkeypatch.setattr(classify, "read", lambda image: envelope(["CURRENT RESIDENT"], sender="Parc", extra=["PRSRT STD"]))
    pipeline.classify_pending(home)
    pipeline.create_drafts(home)
    pipeline.research_pending(home)
    server, post = _served(home)
    try:
        post("/pieces/1", sender="", recipient="", category="marketing")  # name cleared...
        post("/pieces/1", sender="Parca", recipient="", category="marketing")  # ...then retyped
        # a piece for whoever lives here is not mail for someone who moved away
        page = post("/pieces/1", sender="Parca", recipient="Current Resident", category="marketing")
    finally:
        server.shutdown()
        server.server_close()
    with db.connect() as conn:
        drafts = [tuple(r) for r in conn.execute(
            "SELECT s.name, d.kind, d.status FROM drafts d JOIN senders s ON s.id = d.sender_id ORDER BY d.id")]
        assert conn.execute("SELECT resident FROM pieces").fetchone()[0] is None
    assert drafts == [("Parca", "optout", "draft")]
    assert "For Review (1)" in page and "email in review" in _section(page, "<h2>History</h2>")


def test_a_website_the_user_gives_is_trusted_straight_away(cfg, monkeypatch):
    pipeline.ingest(cfg, digest())
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Acme Catalog", "PRSRT STD"))
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result(
        "https://guess.example/", ["info@guess.example"], False))
    pipeline.classify_pending(cfg)
    pipeline.create_drafts(cfg)
    pipeline.research_pending(cfg)
    server, post = _served(cfg)
    try:
        assert "low confidence" in post("/check")
        page = post("/pieces/1", sender="Acme Catalog", recipient="", category="marketing",
                    website="https://acme.example/", website_was="https://guess.example/")
    finally:
        server.shutdown()
        server.server_close()
    # before any lookup has run: the site shows without the tag, and the old site's address
    # and warning are gone from the request
    assert "For Review (1)" in page and "https://acme.example/</a>" in page
    assert "low confidence" not in page and "guess.example" not in page
    with db.connect() as conn:
        assert conn.execute("SELECT to_addr FROM drafts").fetchone()[0] == ""


def test_typing_the_right_name_over_a_misread_one_changes_only_that_piece(cfg, monkeypatch):
    garbled = "I-IOOT' JUDKINS"
    for i in range(2):  # two pieces from the same misread sender
        pipeline.ingest(cfg, digest(images=(SCAN + bytes([i]),)))
        monkeypatch.setattr(classify, "read", lambda image: scan_of(garbled, "PRSRT STD"))
        pipeline.classify_pending(cfg)
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result(
        "https://guess.example/", ["info@guess.example"], False))
    pipeline.create_drafts(cfg)
    pipeline.research_pending(cfg)
    server, post = _served(cfg)
    try:
        page = post("/pieces/2", sender="Hoot Judkins", recipient="", category="marketing",
                    website="https://guess.example/", website_was="https://guess.example/")
    finally:
        server.shutdown()
        server.server_close()
    with db.connect() as conn:
        pieces = [tuple(r) for r in conn.execute(
            "SELECT p.id, s.name, p.auto_sender FROM pieces p JOIN senders s ON s.id = p.sender_id ORDER BY p.id")]
        drafts = {r["name"]: r["body"] for r in conn.execute(
            "SELECT s.name, d.body FROM drafts d JOIN senders s ON s.id = d.sender_id WHERE d.status = 'draft'")}
    # only the edited piece moved to the typed name; the other is exactly as it was
    assert pieces == [(1, garbled, garbled), (2, "Hoot Judkins", garbled)]
    assert set(drafts) == {garbled, "Hoot Judkins"} and "mail from Hoot Judkins" in drafts["Hoot Judkins"]
    assert "For Review (2)" in page and 'name="sender" value="Hoot Judkins"' in page
    assert page.count("</span>Processing…</span>") == 2  # its Website and To, while it is looked up


def test_known_last_names_pick_the_addressee_out_when_the_usual_method_misses(tmp_path):
    address = "4382 Wellington Ave, Cedar Grove, CA 95061"
    # modelled on a real piece: the name is off to one side and a printed line sits above the
    # street address, so the usual method takes "SPECIAL INVITATION" for the addressee
    scan = classify.Scan([
        classify.Line("HOOT JUDKINS", 60, 40),
        classify.Line("1269 Harbor Blvd, Cedar Grove, CA 95063", 60, 80),
        classify.Line("Brooke Freelano", 500, 420),          # OCR slip in a previous resident's name
        classify.Line("SPECIAL INVITATION", 1100, 520),
        classify.Line("4382 Wellington Ave", 1100, 560),
        classify.Line("Cedar Grove, CA 95061", 1100, 600),
    ], 2000, 1000)
    assert classify.find_recipient(scan, address) == ["SPECIAL INVITATION"]
    assert classify.find_recipient(scan, address, ("Whitfield", "Freeland")) == ["Brooke Freelano"]
    # with no known name anywhere on the piece, the usual answer stands
    assert classify.find_recipient(scan, address, ("Whitfield", "Nguyen")) == ["SPECIAL INVITATION"]
    # a known name in the sender's corner is the sender, not the addressee
    assert classify.find_recipient(scan, address, ("Judkins",)) == ["SPECIAL INVITATION"]

    home = config.Config(resident_last_names=("Whitfield",), previous_resident_last_names=("Freeland",))
    assert classify.check_resident(["Brooke Freelano"], home) == 0
    assert classify.check_resident(["The Whitfield Residence"], home) == 1
    assert classify.check_resident(["Freeland and Whitfield"], home) == 1  # a current resident wins
    only_previous = config.Config(previous_resident_last_names=("Freeland",))
    assert classify.check_resident(["Brooke Freeland"], only_previous) == 0
    assert classify.check_resident(["Someone Else"], only_previous) is None
    # their names are blanked in the shareable export too
    assert classify.strip_personal("Brooke Freeland\nSPECIAL INVITATION", set(), home).splitlines() == [
        "[name removed]", "SPECIAL INVITATION"]


def test_previous_residents_are_used_when_mail_is_read(tmp_path, monkeypatch):
    db.init(str(tmp_path))
    home = config.Config(data_dir=str(tmp_path), resident_last_names=("Doe",),
                         previous_resident_last_names=("Smith",),
                         physical_mail_address="12 Oak Court, Springfield IL 62704")
    pipeline.ingest(home, digest())
    scan = envelope(["ANNIVERSARY SALE"], sender="Acme Catalog", extra=["PRSRT STD"])
    scan = scan._replace(lines=scan.lines + [classify.Line("John Smith", 400, 700)])
    monkeypatch.setattr(classify, "read", lambda image: scan)
    pipeline.classify_pending(home)
    pipeline.create_drafts(home)
    with db.connect() as conn:
        piece = conn.execute("SELECT recipient, resident FROM pieces").fetchone()
        draft = conn.execute("SELECT kind, recipient FROM drafts").fetchone()
    assert tuple(piece) == ("John Smith", 0) and tuple(draft) == ("moved", "John Smith")


def test_address_is_found_by_street_name_when_the_house_number_is_misread():
    """From a real envelope: "4382" was read as "2", and a sale line starts with a number."""
    address = "4382 Wellington Ave, Cedar Grove, CA 95061"
    scan = classify.Scan([
        classify.Line("I-IOOT' JUDKINS", 114, 68, 400, 50),
        classify.Line("2 Wellington Ave", 674, 420, 550, 55),
        classify.Line("Q Q50U", 678, 504, 200, 50),
        classify.Line("SPECIAL INVITATION", 1416, 577, 500, 40),
        classify.Line("ANNIVERSARY SECRET SALE", 1332, 621, 650, 40),
        classify.Line("NOW THRU OCTOBER 19, 2026", 1311, 667, 700, 40),
        classify.Line("14 DAYS ONLY", 1485, 711, 350, 40),
    ], 2136, 936)
    assert classify._street_anchor(scan, address).text == "2 Wellington Ave"
    # nothing was read above the street line, so there is no addressee rather than a wrong one
    assert classify.find_recipient(scan, address, ("Whitfield", "Freeland")) == []
    # with no address to go by, a sale line is not mistaken for a street
    assert classify._street_anchor(scan._replace(lines=scan.lines[2:]), address) is None


def test_close_reads_of_each_region_are_merged_into_the_scan(monkeypatch):
    """Follows a real envelope whose first read lost the addressee and the return address."""
    from PIL import Image

    blank = io.BytesIO()
    Image.new("L", (712, 312), 200).save(blank, "JPEG")
    address = "4382 Wellington Ave, Cedar Grove, CA 95061"
    scan = classify.Scan([
        classify.Line("I-IOOT' JUDKINS", 114, 68, 400, 50),
        classify.Line("2 Wellington Ave", 674, 420, 550, 55),
        classify.Line("14 DAYS ONLY", 1485, 711, 350, 40),
    ], 2136, 936)
    answers = iter([
        "byooke Fregland", "brooke Freeland", "broogeFr\\eel{nz",          # the name line, three ways
        "byooke Freeland\n382 Wellington Ave\nC edar. Grvve, LA G50l",    # the address block
        "nothing useful",
        "HooT. Jupians\n1269 Harbor Blvd., Cedar Grove, CA 95063\nCall 650-555-0100",  # sender's block
        "HOOT JUDKINS\nThe Wise Choice for Real Wood Furniture\nCall (650) 555-0100",
        "USA PRESORTED STANDARD",                                          # postage corner
    ])
    calls = []

    class Done:
        def __init__(self, text): self.stdout = text.encode()

    def fake_tesseract(command, **kwargs):
        calls.append(command[command.index("--psm") + 1])
        return Done(next(answers))

    monkeypatch.setattr(classify.subprocess, "run", fake_tesseract)
    closer = classify.look_closer(blank.getvalue(), scan, address)
    assert calls == ["7", "7", "7", "6", "6", "6", "6", "6"]
    # garbled readings are dropped; the rest are candidates for the addressee
    assert closer.address_names == ("byooke Fregland", "brooke Freeland", "byooke Freeland")
    assert classify.find_recipient(closer, address, ("Whitfield", "Freeland")) == ["brooke Freeland"]
    assert classify.find_recipient(closer, address) == ["byooke Fregland"]  # no list: the first reading
    # the sender's block supplies what the first read couldn't
    assert classify.find_return_location(closer, address) == "Cedar Grove, CA"
    misread = closer._replace(sender_lines=("1269 Harbor Bivd.. Cedar Grve, CA 95063",))
    assert classify.find_return_location(misread, address) == "Cedar Grve, CA"
    assert classify.find_phones(closer) == ["6505550100"]  # two close reads agree on it
    assert classify.categorize(closer.text).rule == "bulk_marking"  # from the postage corner
    assert classify.find_sender(closer) == "I-IOOT' JUDKINS"  # the first read's sender still stands

    # with no sender in the first read, the sender's block provides one
    monkeypatch.setattr(classify.subprocess, "run", lambda command, **k: Done("Acme Outfitters\nPO Box 77"))
    bare = classify.look_closer(blank.getvalue(), classify.Scan([], 2136, 936), address)
    assert classify.find_sender(bare) == "Acme Outfitters" and bare.address_names == ()

    # anything that can't be read leaves the scan as it was
    assert classify.look_closer(b"not an image", scan, address) is scan

    def missing(*args, **kwargs):
        raise OSError("tesseract is not installed")

    monkeypatch.setattr(classify.subprocess, "run", missing)
    assert classify.look_closer(blank.getvalue(), scan, address) is scan


def test_template_variables_are_filled_and_unknown_ones_left_visible(cfg):
    known = letters.values(cfg, "Acme Catalog", "Pat Doe", "2026-10-20T09:00:00+00:00", "https://acme.example/")
    assert letters.fill("From {sender} to {addressed_to} on {date_received}, see {website}", known) \
        == "From Acme Catalog to Pat Doe on 2026 Oct 20, see https://acme.example/"
    assert letters.fill("mail from {sender}{addressed_to_phrase}; this {name_and_address_phrase}", known) \
        == "mail from Acme Catalog addressed to Pat Doe; this name and address"
    no_name = letters.values(cfg, "Acme Catalog")
    assert letters.fill("mail from {sender}{addressed_to_phrase}; this {name_and_address_phrase}", no_name) \
        == "mail from Acme Catalog; this address"
    assert no_name["address_block"] == "1 Main St"
    # a typo stays in the text where the user will see it, and is reported
    assert letters.fill("Dear {sendr}, {sender}", known) == "Dear {sendr}, Acme Catalog"
    assert letters.unknown_variables("Dear {sendr} {sender} {email_mailbox_password}") \
        == ["email_mailbox_password", "sendr"]
    # secrets and the household's name lists are not available to a template
    assert not {"email_mailbox_password", "brave_search_api_key", "resident_last_names"} & set(letters.VARIABLES)
    assert set(known) == set(letters.VARIABLES)


def test_custom_templates_are_saved_and_used_for_new_requests(tmp_path, monkeypatch):
    db.init(str(tmp_path))
    home = config.Config(data_dir=str(tmp_path), resident_last_names=("Doe",),
                         physical_mail_address="12 Oak Court, Springfield IL 62704")
    assert letters.load(home) == letters.DEFAULTS
    server, post = _served(home)
    try:
        page = post("/templates", action="save",
                    optout_subject="Stop mailing {addressed_to}",
                    optout_body="Hi {sender},\r\nPlease stop. {address_block}\r\n{oops}",
                    moved_subject=letters.DEFAULTS["moved"]["subject"],
                    moved_body=letters.DEFAULTS["moved"]["body"])
        # saving returns to the templates page, which flags the unknown variable
        assert "<h1>Email Templates</h1>" in page and "{oops}" in page.split("<script>")[0]
        saved = json.loads((tmp_path / "templates.json").read_text())
        assert list(saved) == ["optout"]  # only what differs from the default is stored
        assert letters.load(home)["moved"] == letters.DEFAULTS["moved"]

        for i, scan in enumerate([envelope(["PAT DOE"], sender="Acme Catalog", extra=["PRSRT STD"]),
                                  envelope(["JOHN SMITH"], sender="Summit Bank", extra=["FIRST-CLASS"])]):
            pipeline.ingest(home, digest(images=(SCAN + bytes([i]),)))
            monkeypatch.setattr(classify, "read", lambda image, scan=scan: scan)
            pipeline.classify_pending(home)
        pipeline.create_drafts(home)
        with db.connect() as conn:
            drafts = {r["kind"]: r for r in conn.execute("SELECT kind, subject, body FROM drafts")}
        assert drafts["optout"]["subject"] == "Stop mailing PAT DOE"
        assert drafts["optout"]["body"] == "Hi Acme Catalog,\nPlease stop. PAT DOE\n12 Oak Court, Springfield IL 62704\n{oops}"
        assert drafts["moved"]["subject"] == "Wrong address on file for JOHN SMITH"

        page = post("/templates", action="reset_optout", optout_subject="x", optout_body="y",
                    moved_subject="Gone: {addressed_to}", moved_body="{addressed_to} moved.")
        assert "Using the default wording." in page
        current = letters.load(home)
        assert current["optout"] == letters.DEFAULTS["optout"] and current["moved"]["subject"] == "Gone: {addressed_to}"
        # the main page links here, and the preview is built from the user's own mail
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/") as response:
            assert '<a href="templates">Email Templates</a>' in response.read().decode()
        assert "Filled in from your latest mail from Acme Catalog." in page
    finally:
        server.shutdown()
        server.server_close()
    (tmp_path / "templates.json").write_text("not json")
    assert letters.load(home) == letters.DEFAULTS  # a damaged file falls back to the defaults


def test_default_requests_are_built_to_be_opened_acted_on_or_forwarded(cfg, monkeypatch):
    pipeline.ingest(cfg, digest())
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Acme Catalog", "PRSRT STD"))
    pipeline.classify_pending(cfg)
    pipeline.create_drafts(cfg)
    with db.connect() as conn:
        plain = conn.execute("SELECT subject, body FROM drafts").fetchone()
        conn.execute("DELETE FROM drafts")
    body = plain["body"]
    assert plain["subject"] == "Please remove this address from your postal mailing list"
    # the record to find comes first, then the asks, then who to pass it to
    assert body.index("postal (paper) mailing list") < body.index("1 Main St") < body.index("1. Stop sending") \
        < body.index("2. Delete") < body.index("3. Stop selling") < body.index("please forward it")
    assert "Return to Sender" not in body  # that line belongs to the other kind of request only
    assert not any(word in body for word in ("California", "DMAchoice"))
    assert "\n\n\n" not in body and body.rstrip().endswith("Thank you.")

    everything = config.Config(
        data_dir=cfg.data_dir, physical_mail_address="1 Main St", california_resident=True,
        registered_with_dmachoice=True,
    )
    pipeline.create_drafts(everything)
    with db.connect() as conn:
        body = conn.execute("SELECT body FROM drafts").fetchone()[0]
    assert "§ 1798.105" in body and "§ 1798.120" in body and "DMAchoice" in body
    assert "as not wanting marketing mail. Please check your mailing lists against it" in body
    assert body.index("3. Stop selling") < body.index("California") < body.index("DMAchoice") \
        < body.index("If you are not the right person")
    assert "\n\n\n" not in body

    moved = letters.write(cfg, "moved", letters.values(cfg, "Summit Bank", "John Smith"))
    assert moved[0] == "Wrong address on file for John Smith"
    assert "John Smith does not live here." in moved[1] and "please forward it" in moved[1]
    assert 'for John Smith was marked "Return to Sender"' in " ".join(moved[1].split())


def test_boolean_settings_load_from_options_and_env(tmp_path, monkeypatch):
    options = tmp_path / "options.json"
    options.write_text('{"california_resident": true, "registered_with_dmachoice": false}')
    monkeypatch.setenv("OPTIONS_PATH", str(options))
    loaded = config.load()
    assert (loaded.california_resident, loaded.registered_with_dmachoice) == (True, False)
    monkeypatch.setenv("REGISTERED_WITH_DMACHOICE", "true")
    monkeypatch.setenv("CALIFORNIA_RESIDENT", "false")
    assert (config.load().california_resident, config.load().registered_with_dmachoice) == (False, True)


def test_scan_is_attached_when_ticked_and_the_email_says_so(cfg, monkeypatch):
    for i, name in enumerate(["Acme Catalog", "Birch Dental"]):
        pipeline.ingest(cfg, digest(images=(SCAN + bytes([i]),)))
        monkeypatch.setattr(classify, "read", lambda image, name=name: scan_of(name, "PRSRT STD"))
        pipeline.classify_pending(cfg)
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result("", [], False))
    pipeline.create_drafts(cfg)
    pipeline.research_pending(cfg)
    sent = []
    monkeypatch.setattr(mail, "send", lambda cfg, to, subject, body, bcc=None, scan=None: sent.append((body, scan)))
    server, post = _served(cfg)
    try:
        assert post("/check").count('name="attach_scan" value="1" checked') == 2  # ticked to begin with
        fields = {"action": "send", "to_addr": "optout@acme.com", "subject": "s", "body": "Please stop.\n"}
        post("/drafts/1", attach_scan="1", **fields)
        post("/drafts/2", **fields)  # unticked: a checkbox that is off sends nothing
    finally:
        server.shutdown()
        server.server_close()
    (with_body, with_scan), (without_body, without_scan) = sent
    assert with_scan == (SCAN + bytes([0]), "jpg")
    assert with_body == "Please stop.\n\nA scan of the mailpiece is attached.\n"
    assert without_scan is None and without_body == "Please stop.\n"
    with db.connect() as conn:  # the saved text is what the user wrote, without the added line
        assert conn.execute("SELECT body FROM drafts WHERE id = 1").fetchone()[0] == "Please stop.\n"


def test_a_photo_is_taken_in_like_a_scan_and_attached_only_when_ticked(cfg, monkeypatch):
    server, post = _served(cfg)
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def upload(body, content_type="image/jpeg"):
        request = urllib.request.Request(base + "/photos", data=body, headers={"Content-Type": content_type})
        return urllib.request.urlopen(request).read().decode()

    sent = []
    monkeypatch.setattr(mail, "send", lambda cfg, to, subject, body, bcc=None, scan=None: sent.append((body, scan)))
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result("", [], False))
    photo = b"\xff\xd8\xff" + os.urandom(2_000_000)  # larger than a form may be
    try:
        assert 'id="add-photo"' in upload(photo)
        assert server.worker.wake.is_set() and server.worker.activity == "Processing mail"
        for not_a_photo in (b"GIF89a", b""):
            with pytest.raises(urllib.error.HTTPError) as refused:
                upload(not_a_photo)
            assert refused.value.code == 400
        # Sent as a form it is over the limit; the server may hang up before the 413 is read.
        with pytest.raises(urllib.error.URLError):
            upload(photo, "application/x-www-form-urlencoded")
        with db.connect() as conn:
            (piece,) = conn.execute("SELECT image_path, photo, category FROM pieces").fetchall()
        assert Path(piece["image_path"]).read_bytes() == photo and piece["photo"] == 1

        monkeypatch.setattr(classify, "read", lambda image: scan_of("Acme Catalog", "PRSRT STD"))
        pipeline.classify_pending(cfg)
        pipeline.create_drafts(cfg)
        pipeline.research_pending(cfg)
        page = post("/check")
        assert "Acme Catalog" in page and 'alt="Photo of the mailpiece"' in page
        assert '<input type="checkbox" name="attach_scan" value="1" > Attach your photo' in page  # off to begin with
        fields = {"to_addr": "optout@acme.com", "subject": "s", "body": "Please stop.\n"}
        post("/drafts/1", action="send", **fields)
        with db.connect() as conn:  # back to a draft, to send it again with the box ticked
            conn.execute("UPDATE drafts SET status = 'draft', sent_at = NULL")
        post("/drafts/1", action="send", attach_scan="1", **fields)
    finally:
        server.shutdown()
        server.server_close()
    assert sent == [
        ("Please stop.\n", None),
        ("Please stop.\n\nA photo of the mailpiece is attached.\n", (photo, "jpg")),
    ]


def test_a_site_that_refuses_the_browser_style_request_is_asked_again_plainly(monkeypatch):
    import email.message
    import urllib.error

    asked = []

    class Page:
        headers = email.message.Message()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def geturl(self):
            return "https://shop.example/"

        def read(self, limit):
            return b"<a href='mailto:help@shop.example'>write to us</a>"

    Page.headers["Content-Type"] = "text/html; charset=utf-8"

    def picky(url, data=None, headers=None):
        asked.append(headers)
        if not headers:
            raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)
        return Page()

    monkeypatch.setattr(research, "_open", picky)
    assert research.extract_emails(research._fetch("https://shop.example/")[1]) == {"help@shop.example"}
    assert asked == [None, {"User-Agent": research.PLAIN_USER_AGENT}]

    # a site that refuses both ways is given up on after the two tries
    asked.clear()
    monkeypatch.setattr(research, "_open", lambda url, data=None, headers=None: picky(url) )
    assert research._fetch("https://shop.example/") == ("", "")


def test_stray_percent_signs_from_a_scan_are_not_offer_wording():
    hatching = "PRESORTED\nFIRST CLASS MAIL\n7%\n4 %\nPO Box 100158\n%\n132%\nUnum Life Insurance"
    assert classify.categorize(hatching).rule == "official_marking"
    offer = "PRESORTED\nFIRST CLASS MAIL\n0% intro APR\nSave 20% today\nTake an extra 15% off"
    assert classify.categorize(offer).rule == "offer_wording"


def test_more_junk_after_a_sent_request_gets_a_request_of_its_own(cfg, monkeypatch):
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result(
        "https://acme.example/", ["optout@acme.example"], True))
    monkeypatch.setattr(mail, "send", lambda *args, **kwargs: None)
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Acme Catalog", "PRSRT STD"))

    def arrives(n):
        pipeline.ingest(cfg, digest(images=(SCAN + bytes([n]),)))
        pipeline.classify_pending(cfg)
        pipeline.research_pending(cfg)
        pipeline.create_drafts(cfg)

    arrives(1)
    server, post = _served(cfg)
    try:
        post("/drafts/1", action="send", to_addr="optout@acme.example", subject="s", body="b")
        arrives(2)
        page = web.index(server, {})
    finally:
        server.shutdown()
        server.server_close()
    with db.connect() as conn:
        assert [tuple(r) for r in conn.execute("SELECT piece_id, status FROM drafts ORDER BY id")] == [
            (1, "sent"), (2, "draft")]
    assert "For Review (1)" in page
    import re

    states = re.findall(r"<span>(email in review|email sent)", _section(page, "<h2>History</h2>"))
    assert states == ["email in review", "email sent"]


def test_current_resident_is_only_a_junk_marking_without_first_class_postage():
    official = "First-Class Mail\nPostage & Fees Paid\nUSPS\nCurrent Resident / John Smith"
    assert classify.categorize(official).rule == "official_marking"
    assert classify.categorize("Our neighbors at\nCurrent Resident\n12 Oak Court").rule == "bulk_marking"
    assert classify.categorize("PRSRT STD\nFirst-Class look\nCurrent Resident").rule == "bulk_marking"


def test_a_web_address_caught_in_part_is_read_again_on_its_own(monkeypatch):
    import io

    from PIL import Image

    blank = io.BytesIO()
    Image.new("L", (1000, 500), 255).save(blank, "PNG")
    scan = classify.Scan([classify.Line("howard2026.org", 800, 900, 300, 30)], 2000, 1000)
    crops = []

    def read_crop(picture, box, enlarge, page_mode):
        crops.append(box)
        return ["- dianehoward2026.org"] if page_mode == 7 else []

    monkeypatch.setattr(classify, "_read_crop", read_crop)
    assert classify.find_domain(scan) == ""  # one read, and only the tail of the address
    closer = classify.look_closer(blank.getvalue(), scan, "12 Oak Court")
    assert classify.find_domain(closer) == "dianehoward2026.org"
    assert crops[-1][0] < 400  # the crop reaches left of where the first read began (800 / 2)


def test_home_assistant_is_told_when_mail_is_waiting_and_when_it_no_longer_is(monkeypatch):
    from app import notify

    calls = []
    monkeypatch.setattr(notify, "_call", lambda action, **data: calls.append((action, data)))
    monkeypatch.setattr(notify, "_last", None)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    notify.review(3, True)
    assert calls == []  # not under Home Assistant: nothing to tell

    monkeypatch.setenv("SUPERVISOR_TOKEN", "x")
    notify.review(2, True, "/app/local_informed_unsubscribe")
    assert [c[0] for c in calls] == ["create"]
    assert calls[0][1]["notification_id"] == notify.NOTIFICATION_ID
    assert "(/app/local_informed_unsubscribe)" in calls[0][1]["message"]
    notify.review(1, True)  # working through the queue: one the user dismissed stays dismissed
    assert len(calls) == 1
    notify.review(2, True)  # more mail arrived
    assert [c[0] for c in calls] == ["create", "create"]
    notify.review(0, True)
    notify.review(0, True)
    assert [c[0] for c in calls] == ["create", "create", "dismiss"]

    # turned off: one left from before is cleared, and nothing is put up
    calls.clear()
    monkeypatch.setattr(notify, "_last", None)
    notify.review(4, False)
    notify.review(5, False)
    assert [c[0] for c in calls] == ["dismiss"]

    # Home Assistant being unreachable never breaks the app
    def unreachable(action, **data):
        raise OSError("no route")

    monkeypatch.setattr(notify, "_call", unreachable)
    monkeypatch.setattr(notify, "_last", None)
    notify.review(1, True)


def test_the_notification_follows_the_for_review_count(cfg, monkeypatch):
    from app import notify

    seen = []
    monkeypatch.setattr(notify, "review", lambda waiting, enabled, link="": seen.append((waiting, enabled)))
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result(
        "https://acme.example/", ["optout@acme.example"], True))
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Acme Catalog", "PRSRT STD"))
    for n in (1, 2):
        pipeline.ingest(cfg, digest(images=(SCAN + bytes([n]),)))
    pipeline.classify_pending(cfg)
    pipeline.research_pending(cfg)
    pipeline.create_drafts(cfg)
    assert cfg.notify_when_mail_to_review is True
    server, post = _served(cfg)
    try:
        assert "For Review (2)" in web.index(server, {}) and web.review_count(server) == 2
        post("/drafts/1", action="skip")
        assert seen[-1] == (1, True) and "For Review (1)" in web.index(server, {})
        post("/drafts/2", action="skip")
        assert seen[-1] == (0, True)
    finally:
        server.shutdown()
        server.server_close()

    # the end of a run reports too
    worker = pipeline.Worker(cfg, run=lambda cfg: [])
    ran = []
    worker.after_run = lambda: ran.append(worker.activity)
    worker.cycle()
    assert ran == [""]


def test_send_says_it_is_sending_while_the_email_goes_out(cfg):
    server = web.make_server(cfg, pipeline.Worker(cfg), port=0)
    try:
        page = web.index(server, {})
    finally:
        server.server_close()
    assert "Sending…" in page and 'action.name = "action"' in page



def test_deleting_a_piece_removes_its_scan_and_requests(cfg, monkeypatch):
    for i in range(2):
        pipeline.ingest(cfg, digest(images=(SCAN + bytes([i]),)))
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Acme Catalog", "PRSRT STD"))
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result("", [], False))
    pipeline.classify_pending(cfg)
    pipeline.create_drafts(cfg)
    pipeline.research_pending(cfg)  # History waits for the sender's website lookup
    with db.connect() as conn:
        scan = Path(conn.execute("SELECT image_path FROM pieces WHERE id = 1").fetchone()[0])
    server, post = _served(cfg)
    try:
        page = post("/check")
        assert 'data-delete="pieces/1/delete"' in _section(page, "<h2>History</h2>")
        page = post("/pieces/1/delete")
        with pytest.raises(urllib.error.HTTPError):
            post("/pieces/1/delete")  # already gone
    finally:
        server.shutdown()
        server.server_close()
    assert 'data-delete="pieces/1/delete"' not in page and 'data-delete="pieces/2/delete"' in page
    assert not scan.exists()
    with db.connect() as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM pieces")] == [2]
        assert [r[0] for r in conn.execute("SELECT piece_id FROM drafts")] == [2]


def test_a_turned_photo_is_read_again_keeping_the_users_answers(cfg, monkeypatch):
    pipeline.add_piece(cfg, SCAN, photo=True)
    pipeline.ingest(cfg, digest(images=(SCAN + b"x",)))  # a digest scan has no rotate button
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Acme Catalog", "PRSRT STD"))
    monkeypatch.setattr(research, "research", lambda name, website="", **e: research.Result("", [], False))
    pipeline.classify_pending(cfg)
    pipeline.create_drafts(cfg)
    pipeline.research_pending(cfg)
    turned = b"\xff\xd8\xff" + os.urandom(500)
    server, post = _served(cfg)
    try:
        page = post("/check")
        assert page.count('data-turn="pieces/1/turn"') == 2  # in For Review and in History
        assert 'data-turn="pieces/2/turn"' not in page
        post("/pieces/1", sender="Acme Catalog", recipient="", category="real")  # the user's answers
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/pieces/1/turn", data=turned,
            headers={"Content-Type": "image/jpeg"},
        )
        page = urllib.request.urlopen(request).read().decode()
        assert "Reading the photo again" in page and 'data-turn="pieces/1/turn"' not in page
        assert server.worker.wake.is_set()
        with pytest.raises(urllib.error.HTTPError):  # only photos can be turned
            urllib.request.urlopen(urllib.request.Request(
                request.full_url.replace("/1/", "/2/"), data=turned,
                headers={"Content-Type": "image/jpeg"}))
    finally:
        server.shutdown()
        server.server_close()
    with db.connect() as conn:
        assert Path(conn.execute("SELECT image_path FROM pieces WHERE id = 1").fetchone()[0]).read_bytes() == turned
    # The right way up, it turns out to be from someone else.
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Birch Dental", "FIRST-CLASS"))
    pipeline.classify_pending(cfg)
    with db.connect() as conn:
        piece = conn.execute(
            "SELECT p.*, s.name FROM pieces p JOIN senders s ON s.id = p.sender_id WHERE p.id = 1"
        ).fetchone()
    assert (piece["reread"], piece["category"], piece["manual"], piece["name"]) == (0, "real", 1, "Acme Catalog")
    assert piece["auto_sender"] == "Birch Dental"  # the app's own reading is the new one


def test_reading_a_turned_photo_again_rewrites_a_request_it_changes(cfg, monkeypatch):
    pipeline.add_piece(cfg, SCAN, photo=True)
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Acme Catalog", "PRSRT STD"))
    pipeline.classify_pending(cfg)
    pipeline.create_drafts(cfg)
    with db.connect() as conn:
        conn.execute("UPDATE pieces SET reread = 1")
    monkeypatch.setattr(classify, "read", lambda image: scan_of("Birch Dental", "PRSRT STD"))
    pipeline.classify_pending(cfg)
    pipeline.create_drafts(cfg)
    with db.connect() as conn:
        drafts = [tuple(r) for r in conn.execute(
            "SELECT s.name, d.status FROM drafts d JOIN senders s ON s.id = d.sender_id")]
    assert drafts == [("Birch Dental", "draft")]
