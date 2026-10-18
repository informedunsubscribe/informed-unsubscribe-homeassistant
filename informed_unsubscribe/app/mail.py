import email
import email.policy
import imaplib
import re
import smtplib
from datetime import date, timedelta
from email.headerregistry import Address
from email.message import EmailMessage
from typing import Callable, NamedTuple

from bs4 import BeautifulSoup

# How far back to look in the mailbox on each run.
LOOKBACK_DAYS = 14
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# Used only when the digest's layout can't be read: logos and icons are well under this.
MIN_IMAGE_BYTES = 5_000


LOGIN_HELP = (
    "the mailbox rejected the login. Check the Email address and Email mailbox password settings "
    "(for Gmail this must be an app password, not the account's normal password)"
)


MISSING_LOGIN = (
    "the Email address or Email mailbox password setting is empty, so the mailbox can't be "
    "checked and requests can't be sent"
)


class LoginRejected(Exception):
    pass


class Piece(NamedTuple):
    image: bytes
    subtype: str
    sender: str = ""  # the advertiser name USPS prints above campaign pieces
    campaign: bool = False


def is_digest(msg) -> bool:
    sender = str(msg.get("From", "")).lower()
    subject = str(msg.get("Subject", "")).lower()
    return (
        "informeddelivery" in sender
        or "informed delivery" in sender + subject
        or "daily digest" in subject
    )


def _is_ride_along(name: str, alt: str = "") -> bool:
    """An advertiser's extra picture shown under a mailpiece, not a mailpiece itself."""
    return name.lower().startswith("content-") or alt.lower().startswith("ridealong")


def _is_section(heading: str) -> bool:
    return heading.lower().startswith("expected")


def extract_pieces(raw: bytes) -> list[Piece]:
    """Return the mailpieces a digest lists as expected today, in order."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    if not is_digest(msg):
        return []
    images = {}
    for part in msg.walk():
        if part.get_content_maintype() == "image":
            name = (part.get("Content-ID") or part.get_filename() or "").strip("<> ")
            images[name or f"image-{len(images)}"] = (
                part.get_payload(decode=True) or b"", part.get_content_subtype()
            )

    pieces: list[Piece] = []
    understood = False
    body = msg.get_body(("html",))
    if body is not None:
        # Walk the page in order. A "FROM: advertiser" label applies to the next mailpiece, and
        # a section heading ("Expected Today", "Expected This Week") to everything below it.
        advertiser, section, used = "", "", set()

        def wanted(tag) -> bool:
            return (
                tag.name == "img"
                or "campaign-from-span" in tag.get("id", "")  # clients prefix ids on forward
                or (tag.name in ("h1", "h2", "h3") and _is_section(tag.get_text(" ", strip=True)))
            )

        for tag in BeautifulSoup(body.get_content(), "html.parser").find_all(wanted):
            if tag.name in ("h1", "h2", "h3"):
                section = tag.get_text(" ", strip=True).lower()
                continue
            if tag.name != "img":
                advertiser = tag.get_text(" ", strip=True)[:120]
                continue
            alt, source = tag.get("alt", ""), tag.get("src", "")
            name = source[4:] if source.lower().startswith("cid:") else ""
            if name not in images or name in used or _is_ride_along(name, alt):
                continue
            used.add(name)
            understood = True
            # Only today's mail: pieces "expected this week" have no delivery scan yet and are
            # mostly circulars sent to every address.
            if not section or "today" in section:
                campaign = bool(advertiser) or alt.lower() == "campaign"
                pieces.append(Piece(*images[name], advertiser, campaign))
            advertiser = ""
    if not understood:
        pieces = [
            Piece(data, subtype) for name, (data, subtype) in images.items()
            if len(data) >= MIN_IMAGE_BYTES and not _is_ride_along(name)
        ]
    return pieces


def _fetched(parts) -> bytes:
    return next((part[1] for part in parts if isinstance(part, tuple)), b"")


def poll(cfg, is_done: Callable[[str], bool], handle: Callable[[str, bytes], None]) -> int:
    """Pass each recent message not yet handled to `handle(message_id, raw)`.

    The mailbox is opened read-only and nothing in it is changed. Whether a message has been
    handled is the caller's record, so mail the user has already opened, or mail that arrived
    before a reinstall, is still picked up.
    """
    today = date.today() - timedelta(days=LOOKBACK_DAYS)
    since = f"{today.day:02d}-{_MONTHS[today.month - 1]}-{today.year}"
    count = 0
    with imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port) as imap:
        try:
            imap.login(cfg.email_address, cfg.email_mailbox_password)
        except imaplib.IMAP4.error as exc:
            raise LoginRejected(LOGIN_HELP) from exc
        imap.select("INBOX", readonly=True)
        _, data = imap.search(None, "SINCE", since)
        for num in data[0].split():
            _, parts = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            header = email.message_from_bytes(_fetched(parts))
            # A message without an id is told apart by its number in this mailbox.
            message_id = str(header.get("Message-ID") or "").strip() or f"no-id-{num.decode()}"
            if is_done(message_id):
                continue
            _, parts = imap.fetch(num, "(BODY.PEEK[])")
            raw = _fetched(parts)
            if raw:
                handle(message_id, raw)
                count += 1
    return count


def send(cfg, to_addr: str, subject: str, body: str, bcc: str | None = None,
         scan: tuple[bytes, str] | None = None) -> None:
    """Send a request. `bcc` is the copy address for this message; None means the configured
    one. `scan` is the mailpiece image to attach, as (bytes, file extension)."""
    bcc = cfg.bcc_email_address if bcc is None else bcc
    msg = EmailMessage()
    # The name shown is the user's choice or, failing that, the street the sender already
    # mails to: requests must not tell the sender anything they don't have.
    street = re.split(r"[,\n]", cfg.physical_mail_address)[0].strip()
    name = " ".join((cfg.from_name or (f"{street} Resident" if street else "")).split())
    msg["From"] = Address(name, addr_spec=cfg.email_address) if name else cfg.email_address
    msg["To"] = to_addr
    msg["Subject"] = subject
    if cfg.reply_to_email_address:
        msg["Reply-To"] = cfg.reply_to_email_address
    if bcc:
        # send_message delivers to Bcc addresses but strips the header from what goes out.
        msg["Bcc"] = bcc
    msg.set_content(body)
    if scan:
        data, extension = scan
        msg.add_attachment(
            data, maintype="image", subtype="jpeg" if extension == "jpg" else extension,
            filename=f"mailpiece.{extension}",
        )
    with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
        try:
            smtp.login(cfg.email_address, cfg.email_mailbox_password)
        except smtplib.SMTPAuthenticationError as exc:
            raise LoginRejected(LOGIN_HELP) from exc
        smtp.send_message(msg)
