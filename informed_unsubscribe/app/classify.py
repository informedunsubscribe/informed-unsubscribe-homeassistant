"""OCR a mailpiece scan with Tesseract, then name the sender and classify it from the text.

Classification is layered, strongest evidence first: bulk-mail postage markings, bill wording,
what the user previously said about the sender, USPS campaign listings, promotional wording and
first-class markings.
"""

import csv
import difflib
import io
import re
import subprocess
from collections import Counter
from typing import NamedTuple

from . import db, research

CATEGORIES = ("marketing", "real", "undetermined")

MIN_WORD_CONFIDENCE = 30
# Informed Delivery scans are small; Tesseract reads them far better enlarged.
MIN_OCR_WIDTH = 1800
# The return address sits in the top-left of an envelope, as fractions of (width, height).
RETURN_ZONE = (0.45, 0.45)

# Postage markings and addressing that only bulk advertising mail carries. Bills cannot be
# sent this way, so this outranks everything else.
_BULK = re.compile(
    r"prs[ra]t\.?\s*st(andar)?[do0]|presort(ed)?\s+st(andar)?d|standard\s+mail|marketing\s+mail"
    r"|non-?\s?profit\s+org|ecrwss|\beddm\b",
    re.I,
)
# Addressed to whoever lives there. Advertising nearly always, but first-class senders (the
# Postal Service, a utility) also write "Current Resident / John Smith".
_TO_ANYONE = re.compile(
    r"current\s+(resident|occupant)|postal\s+customer|our\s+neighbors?\s+at", re.I
)
_BILL = re.compile(
    r"\binvoice|statement\s+enclosed|(amount|balance|payment|total)\s+due|past\s+due|due\s+date"
    r"|account\s+(number|no\b|#)|\bbill\s+enclosed|your\s+(bill|statement|invoice)|\breceipt\b"
    r"|explanation\s+of\s+benefits|tax\s+(bill|document|form|information)|important\s+tax"
    r"|\b1099\b|\bw-?2\b",
    re.I,
)
# Offers and announcements, the mark of retail and local-service advertising.
_PROMO = re.compile(
    r"pre-?\s?(approved|qualified|selected|screened)|limited[\s-]time|\$\s?\d+\s+off"
    # A percentage only next to offer wording: a scan's hatched edges read as stray "%" signs.
    r"|\b\d{1,2}(\.\d+)?[ ]?%[ ]?(off|apr|apy|cash|back|discount|savings|intro|interest|financing)\b"
    r"|\b(save|saving|up\s+to|earn|get|extra|additional|take)[ ]+\d{1,2}(\.\d+)?[ ]?%"
    r"|\bsave\b|\bsavings\b|\bapr\b|\boffers?\b|\bspecials?\b|\bexclusive\b|\bdiscount"
    r"|\bcoupon|\bpromo|\bsale\b|\bclearance|\bdeals?\b|free\s+\w+|act\s+now"
    r"|call\s+(today|now)|shop\s+(now|online|in[\s-]store)|use\s+code|\bexpires?\b"
    r"|you'?re\s+invited|grand\s+opening|now\s+open|new\s+location|just\s+(sold|listed)"
    r"|home\s+value|thinking\s+of\s+selling|\bfinancing|new\s+customers?|\bdonat|\bcatalog"
    r"|don'?t\s+miss|\bestimates?\b|schedule\s+(your|today|now)|book\s+(now|today)|\bneighbor",
    re.I,
)
_OFFICIAL = re.compile(
    r"first[\s-]*class|official\s+business|internal\s+revenue|department\s+of|\bjury\b"
    r"|\bballot\b",
    re.I,
)
# Lines in the return-address zone that are postage, endorsements or street addresses.
_NOT_SENDER = re.compile(
    r"prs[ra]t|presort|postage|\bpaid\b|permit|first[\s-]*class|\bstd\b|non-?\s?profit"
    r"|service\s+requested|^\W*\d|p\.?\s?o\.?\s+box|\b[a-z]{2}\s+\d{5}"
    r"|current\s+(resident|occupant)|postal\s+customer",
    re.I,
)
# Lines of a delivery address block that are not a person's or business's name.
_NOT_A_NAME = re.compile(
    r"\b(resident|occupant|homeowner|neighbors?)\b|postal\s+customer"
    r"|\*|\bauto\b|\d-?digit|\bcar-?rt\b|:\s*$",  # sorting endorsements, "MAILED TO:" labels
    re.I,
)
# A name line is mostly letters ("HALE 2024 FAMILY TRUST" is, a barcode line is not).
MIN_NAME_LETTERS = 0.6
# The postal barcode printed above a name reads as a run of upright strokes: l'|"'I"lll|l|ll
# "Current Resident / John Smith", "John Smith or current resident": the name part is kept.
_OR_RESIDENT = re.compile(
    r"\s*[/,|&-]?\s*\b(or\s+|and\s+)?(current\s+)?(resident|occupant)\b\s*[/,|&-]?\s*", re.I
)
_STROKES = set("lI|!1'\"\u2018\u2019\u201c\u201d")
MAX_STROKES = 0.6
MAX_NAME_WORD = 25
_STREET_LINE = re.compile(r"^\W*\d+\s+[a-z]", re.I)
# How far apart, as fractions of the piece, lines of one address block can be.
BLOCK_INDENT = 0.08
BLOCK_LINE_GAP = 0.12
RESIDENT_SIMILARITY = 0.8
SENDER_CORNER = 0.25
_DOMAIN = re.compile(
    r"(?<![@\w.-])(?:www\.)?([a-z0-9-]{3,}\.(?:com|net|org|biz|us|co|info))\b", re.I
)


class Verdict(NamedTuple):
    category: str
    confidence: float
    reason: str  # shown to the user
    rule: str  # stable name of the rule that decided, for analysing corrections later


class ClassifyError(Exception):
    """The scan could not be read at all."""


class Line(NamedTuple):
    text: str
    left: int
    top: int
    width: int = 0
    height: int = 0


class Scan(NamedTuple):
    lines: list[Line]
    width: int
    height: int
    extra: str = ""  # text from a second OCR pass, without usable positions
    # From look_closer: each region of the piece read again on its own.
    close: tuple[str, ...] = ()  # the text of every close read
    address_names: tuple[str, ...] = ()  # candidate addressee names from the address block
    sender_lines: tuple[str, ...] = ()  # lines read from the sender's block

    @property
    def readings(self) -> list[str]:
        """Each separate read of the piece, as text."""
        return ["\n".join(line.text for line in self.lines), self.extra, *self.close]

    @property
    def text(self) -> str:
        return "\n".join(self.readings).strip()


def _prepare(image: bytes) -> bytes:
    """Enlarge and sharpen a scan into a PNG for Tesseract."""
    # Imported here so the image library is only loaded once there is a scan to read.
    from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

    try:
        picture = Image.open(io.BytesIO(image)).convert("L")
    except (UnidentifiedImageError, OSError) as exc:
        raise ClassifyError(f"unreadable image: {exc}") from exc
    if picture.width < MIN_OCR_WIDTH:
        scale = -(-MIN_OCR_WIDTH // picture.width)
        picture = picture.resize((picture.width * scale, picture.height * scale), Image.LANCZOS)
    # Of the settings tried on real scans, this was the one that read window-envelope
    # addresses and the small postage block.
    picture = ImageOps.autocontrast(picture).filter(ImageFilter.UnsharpMask(radius=2, percent=150))
    png = io.BytesIO()
    picture.save(png, "PNG")
    return png.getvalue()


def _tesseract(png: bytes, page_mode: int) -> str:
    try:
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", str(page_mode), "-l", "eng", "tsv"],
            input=png, capture_output=True, timeout=120, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClassifyError(f"tesseract failed: {exc}") from exc
    return result.stdout.decode("utf-8", "replace")


def parse_tsv(tsv: str) -> Scan:
    width = height = 0
    grouped: dict[tuple, list[dict]] = {}
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        try:
            if row["level"] == "1":
                width, height = int(row["width"]), int(row["height"])
            elif (
                row["level"] == "5"
                and float(row["conf"]) >= MIN_WORD_CONFIDENCE
                and (row["text"] or "").strip()
            ):
                key = (row["block_num"], row["par_num"], row["line_num"])
                grouped.setdefault(key, []).append(row)
        except (KeyError, ValueError, TypeError):
            continue
    lines = [
        Line(
            " ".join(w["text"].strip() for w in words),
            min(int(w["left"]) for w in words),
            min(int(w["top"]) for w in words),
            max(int(w["left"]) + int(w["width"]) for w in words) - min(int(w["left"]) for w in words),
            max(int(w["height"]) for w in words),
        )
        for words in grouped.values()
    ]
    return Scan(sorted(lines, key=lambda line: (line.top, line.left)), width, height)


def read(image: bytes) -> Scan:
    """OCR a scan twice: sparse mode keeps each text block where it sits on the piece, which
    finding the sender and addressee needs; block mode catches small print the first misses."""
    png = _prepare(image)
    scan = parse_tsv(_tesseract(png, 11))
    block = parse_tsv(_tesseract(png, 6))
    return scan._replace(extra="\n".join(line.text for line in block.lines))


def find_sender(scan: Scan, addressee: tuple[str, ...] | list[str] = ()) -> str:
    """The first name-like line of the return address, or "" if none is readable."""
    for line in scan.lines:
        if not _in_return_zone(scan, line):
            continue
        text = line.text.strip(" .,:;|-_'\"")
        if sum(c.isalpha() for c in text) < 3 or _NOT_SENDER.search(text):
            continue
        if line.text in addressee:
            continue
        return text[:120]
    # Nothing usable in the first read: take the first name-like line of the sender's block.
    for line in scan.sender_lines:
        text = line.strip(" .,:;|-_'\"")
        if sum(ch.isalpha() for ch in text) >= 3 and not _NOT_SENDER.search(text):
            return text[:120]
    return ""


def _in_return_zone(scan: Scan, line: Line) -> bool:
    return line.left <= scan.width * RETURN_ZONE[0] and line.top <= scan.height * RETURN_ZONE[1]


def last_name_match(names, last_names) -> float:
    """How closely the best word in `names` matches one of `last_names`, from 0 to 1."""
    # Compare letters only, so O'Brien-Lee matches however its punctuation was read.
    def letters(word: str) -> str:
        return re.sub(r"[^a-z]", "", word.lower())

    wanted = {letters(entry.split()[-1]) for entry in last_names if entry.split()}
    return max(
        (difflib.SequenceMatcher(None, word, name).ratio()
         for word in map(letters, " ".join(names).split()) if len(word) >= 2 for name in wanted),
        default=0.0,
    )


def has_last_name(names, last_names) -> bool:
    """Whether any word in `names` is one of `last_names`, allowing for OCR slips."""
    return last_name_match(names, last_names) >= RESIDENT_SIMILARITY


def find_recipient(scan: Scan, physical_mail_address: str = "", known_last_names=()) -> list[str]:
    """The name lines printed above the delivery street address, top first.

    There can be several candidates: the lines above the address in the first read, and the
    names from the close reads of the address block. `known_last_names` are the household's
    current and previous residents. A candidate carrying one of those names wins, the closest
    match first, and only its lines that carry one are kept; failing that, any line elsewhere on the piece that carries one. With no known
    name anywhere, the first read stands, and the close reads only fill in when it found none.
    """
    first = _lines_above_address(scan, physical_mail_address)
    candidates = ([first] if first else []) + [[name] for name in scan.address_names]
    if known_last_names:
        best = max(candidates, key=lambda names: last_name_match(names, known_last_names), default=None)
        if best and has_last_name(best, known_last_names):
            return [name for name in best if has_last_name([name], known_last_names)]
        for line in scan.lines:
            # Only the very top is ruled out as the sender's corner here: a hand-addressed
            # name often sits higher and further left than a printed address block would.
            in_sender_corner = _in_return_zone(scan, line) and line.top <= scan.height * SENDER_CORNER
            if in_sender_corner or _NOT_A_NAME.search(line.text) or _STREET_LINE.search(line.text):
                continue
            if has_last_name([line.text], known_last_names):
                return [line.text.strip(" .,;|-_'\"")[:120]]
    return candidates[0] if candidates else []


_STREET_WORDS = {"street", "avenue", "road", "drive", "boulevard", "lane", "court", "place",
                 "highway", "parkway", "circle", "terrace", "suite", "unit", "apartment"}


def _street_anchor(scan: Scan, physical_mail_address: str) -> Line | None:
    """The line of the delivery address that carries the household's street."""
    own = re.split(r"[,\n]", physical_mail_address)[0]
    house = re.search(r"\d+", own)
    if house:
        numbered = re.compile(rf"^\W*{house.group(0)}\b")
        found = [line for line in scan.lines if numbered.search(line.text)]
        if found:
            return found[-1]
    # The house number is often misread ("4382" as "2"), so go by the street's name next.
    street_names = [w for w in re.findall(r"[a-z]{4,}", own.lower()) if w not in _STREET_WORDS]
    found = [
        line for line in scan.lines
        if has_last_name([line.text], street_names)
        and not (_in_return_zone(scan, line) and line.top <= scan.height * SENDER_CORNER)
    ]
    if found:
        return found[-1]
    # Last resort: something shaped like a street address outside the sender's corner.
    found = [line for line in scan.lines if _STREET.search(line.text) and not _in_return_zone(scan, line)]
    return found[-1] if found else None


def _as_name(text: str) -> str:
    """`text` tidied up if it could be a person's or business's name, else ""."""
    text = _OR_RESIDENT.sub(" ", text).strip()
    if not text or _NOT_A_NAME.search(text):
        return ""
    words = re.findall(r"[^\W\d_]+", text)
    if len(text.split()) > 2 and not any(len(word) >= 3 for word in words):
        return ""  # a barcode read as scattered letters: "L T | UL L T"
    solid = text.replace(" ", "")
    if sum(ch in _STROKES for ch in solid) > MAX_STROKES * len(solid):
        return ""
    if any(len(word) > MAX_NAME_WORD for word in text.split()):
        return ""
    text = text.strip(" .,;|-_'\"©®")
    letters = sum(ch.isalpha() for ch in text)
    return text[:120] if letters >= 3 and letters >= MIN_NAME_LETTERS * len(text.replace(" ", "")) else ""


def _lines_above_address(scan: Scan, physical_mail_address: str) -> list[str]:
    street = below = _street_anchor(scan, physical_mail_address)
    if street is None:
        return []
    names: list[str] = []
    above = [
        line for line in scan.lines
        if line.top < street.top and abs(line.left - street.left) <= scan.width * BLOCK_INDENT
    ]
    for line in sorted(above, key=lambda line: -line.top):
        if below.top - line.top > scan.height * BLOCK_LINE_GAP or len(names) == 2:
            break
        below = line
        if _as_name(line.text):
            names.insert(0, _as_name(line.text))
    return names


# For the single name line above the street: how far above it to cut, and how far into it,
# in line heights. Each is read as one line of text.
_NAME_BANDS = ((1.8, 0.0), (2.2, 0.35), (1.5, 0.0))
_PLAIN_NAME = re.compile(r"[A-Za-z0-9 .,'&-]+")
# Used when the first read gives no landmark to cut around, as fractions of the piece.
SENDER_REGION = (0.0, 0.0, 0.45, 0.30)
POSTAGE_REGION = (0.65, 0.0, 1.0, 0.45)
MAX_WEB_LINES = 2


def _is_own_street(text: str, physical_mail_address: str) -> bool:
    own = re.split(r"[,\n]", physical_mail_address)[0]
    house = re.search(r"\d+", own)
    if house and re.search(rf"^\W*{house.group(0)}\b", text):
        return True
    street_names = [w for w in re.findall(r"[a-z]{4,}", own.lower()) if w not in _STREET_WORDS]
    return has_last_name([text], street_names)


def _read_crop(picture, box, enlarge: int, page_mode: int) -> list[str]:
    """OCR one region of the original scan, enlarged, and return its lines."""
    from PIL import Image, ImageOps

    left, top, right, bottom = (round(v) for v in box)
    left, top = max(0, left), max(0, top)
    right, bottom = min(picture.width, right), min(picture.height, bottom)
    if right - left < 10 or bottom - top < 5:
        return []
    crop = picture.crop((left, top, right, bottom))
    crop = ImageOps.autocontrast(crop.resize((crop.width * enlarge, crop.height * enlarge), Image.LANCZOS))
    data = io.BytesIO()
    crop.save(data, "PNG")
    result = subprocess.run(
        ["tesseract", "stdin", "stdout", "--psm", str(page_mode), "-l", "eng"],
        input=data.getvalue(), capture_output=True, timeout=60, check=True,
    )
    return [line.strip() for line in result.stdout.decode("utf-8", "replace").splitlines() if line.strip()]


def look_closer(image: bytes, scan: Scan, physical_mail_address: str = "") -> Scan:
    """Read the sender's block, the address block and the postage corner again, each alone.

    Reading a whole piece at once drops or garbles text that reads fine when it is the only
    thing in view: small return addresses, names in a handwriting-style face. The first read
    says where to cut (around the household's street line, around the text in the upper
    left), with fixed corners as the fallback. Returns the scan with what was found added;
    on any failure, the scan as it was.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        picture = Image.open(io.BytesIO(image)).convert("L")
    except (UnidentifiedImageError, OSError):
        return scan
    if not scan.width:
        return scan
    shrink = picture.width / scan.width  # the scan's positions are in the enlarged image
    width, height = picture.width, picture.height
    street = _street_anchor(scan, physical_mail_address)
    close: list[str] = []
    names: list[str] = []
    sender_lines: list[str] = []
    try:
        if street is not None and street.height:
            left, top = street.left * shrink, street.top * shrink
            line, span = street.height * shrink, street.width * shrink
            right = left + max(span * 2, width / 3)
            # The one line above the street, when the first read found no name there.
            if not _lines_above_address(scan, physical_mail_address):
                for up, down in _NAME_BANDS:
                    read = _read_crop(picture, (left - line, top - up * line, right, top + down * line), 3, 7)
                    names += [_as_name(" ".join(read))]
            # The whole block: a couple of name lines, the street, the city.
            for enlarge in (3, 4):
                read = _read_crop(picture, (left - line, top - 2.6 * line, right, top + 2.8 * line), enlarge, 6)
                close.append("\n".join(read))
                for index, text in enumerate(read):
                    if _is_own_street(text, physical_mail_address):
                        names += [_as_name(above) for above in read[max(0, index - 2):index]]
                        break
        # The sender's block: around whatever the first read saw in the upper left.
        corner = [
            line for line in scan.lines
            if _in_return_zone(scan, line) and line.top <= scan.height * 0.35
            and (street is None or line.top + line.height < street.top - street.height)
        ]
        if corner:
            margin = max(line.height for line in corner) * shrink
            box = (
                min(line.left for line in corner) * shrink - margin,
                min(line.top for line in corner) * shrink - margin,
                max(line.left + line.width for line in corner) * shrink + width * 0.15,
                max(line.top + line.height for line in corner) * shrink + margin * 2,
            )
        else:
            box = tuple(f * size for f, size in zip(SENDER_REGION, (width, height, width, height)))
        for enlarge in (4, 3):
            read = _read_crop(picture, box, enlarge, 6)
            close.append("\n".join(read))
            sender_lines += [text for text in read if text not in sender_lines]
        box = tuple(f * size for f, size in zip(POSTAGE_REGION, (width, height, width, height)))
        close.append("\n".join(_read_crop(picture, box, 4, 6)))
        # A web address the first read caught, often only its tail: the line it is on, with
        # room to the left for the part that was missed.
        for line in [line for line in scan.lines if _DOMAIN.search(line.text)][:MAX_WEB_LINES]:
            left, top = line.left * shrink, line.top * shrink
            span, tall = line.width * shrink, line.height * shrink
            box = (left - span, top - tall * 0.5, left + span * 1.3, top + tall * 1.5)
            for enlarge in (3, 4):
                close.append("\n".join(_read_crop(picture, box, enlarge, 7)))
    except (OSError, subprocess.SubprocessError):
        return scan
    plain = [name for name in names if name and _PLAIN_NAME.fullmatch(name)]
    return scan._replace(
        close=tuple(text for text in close if text),
        address_names=tuple(dict.fromkeys(plain)),
        sender_lines=tuple(sender_lines),
    )


def check_resident(names: list[str], cfg) -> int | None:
    """1 if the addressee lives here, 0 if not, None if unknown.

    A current resident's last name wins, and matching is forgiving of OCR slips, so a
    doubtful name counts as a resident. A previous resident's last name is a non-resident.
    Anyone else is a non-resident once the household has listed who lives there; with only
    previous residents listed, they are unknown. "Current Resident" and the like name nobody,
    so they say nothing either way.
    """
    names = [name for name in names if _OR_RESIDENT.sub(" ", _TO_ANYONE.sub(" ", name)).strip()]
    if not names:
        return None
    if has_last_name(names, cfg.resident_last_names):
        return 1
    if has_last_name(names, cfg.previous_resident_last_names):
        return 0
    return 0 if cfg.resident_last_names else None


def _agreed(scan: Scan, find) -> list[str]:
    """What `find` pulls out of at least two separate reads of the piece, in order seen."""
    found = [list(dict.fromkeys(find(reading))) for reading in scan.readings]
    counts = Counter(item for reading in found for item in reading)
    return [item for item in dict.fromkeys(i for reading in found for i in reading) if counts[item] >= 2]


def find_domain(scan: Scan) -> str:
    """A web address printed on the piece, which identifies the sender better than a search.

    Only an address two separate reads agree on is trusted: a misread one would send the
    request to a stranger.
    """
    domains = _agreed(scan, lambda text: [m.group(1).lower() for m in _DOMAIN.finditer(text)])
    return next((domain for domain in domains if domain != "usps.com"), "")


_CITY_ZIP = re.compile(r"^(.*[A-Za-z]{2})[\s,.]+(\d{5})(?:-\d{4})?$")


def find_phones(scan: Scan) -> list[str]:
    """Phone numbers printed on the piece, as ten digits.

    Like web addresses, only numbers two separate reads agree on are kept, since one wrong
    digit is somebody else's phone.
    """
    return _agreed(scan, research.phones_in)[:3]


def find_return_location(scan: Scan, physical_mail_address: str = "") -> str:
    """The "City, State" of the return address, to narrow a search for the sender."""
    own_zips = set(re.findall(r"\d{5}", physical_mail_address))
    in_corner = [line.text for line in scan.lines if _in_return_zone(scan, line)]
    for text in (*in_corner, *scan.sender_lines):
        match = _CITY_ZIP.match(text.strip())
        if match and match.group(2) not in own_zips:
            # When the whole address is on one line ("1269 Harbor Blvd., Cedar Grove, CA"),
            # keep the city and state. OCR often reads the comma after the street as a period.
            place = match.group(1)
            breaks = r"[.,]+\s+" if re.match(r"\W*\d", place) else r","
            parts = [part.strip(" .,") for part in re.split(breaks, place) if part.strip(" .,")]
            return " ".join(", ".join(parts[-2:]).split())[:60]
    return ""


def home_city(physical_mail_address: str) -> str:
    """ "City, ST" from the household's own address."""
    match = re.search(r"([A-Za-z .'-]+,\s*[A-Za-z]{2})\s+\d{5}", physical_mail_address)
    return " ".join(match.group(1).split()) if match else ""


_STREET = re.compile(
    r"^\W*\d+\s+.*\b(st|street|ave|avenue|rd|road|dr|drive|blvd|boulevard|ln|lane|ct|court|way"
    r"|pl|place|hwy|highway|pkwy|parkway|cir|circle|ter|terrace|suite|ste|apt|unit|room|floor)\b",
    re.I,
)
_PO_BOX = re.compile(r"\bp\.?\s?o\.?\s*box\b", re.I)
_ENDS_WITH_ZIP = re.compile(r"\b\d{5}(-\d{4})?\s*$")
SAME_TEXT = 0.75


def _resembles(line: str, wanted: str) -> bool:
    """Whether an OCR'd line is, or contains, a known piece of text, allowing for misreads."""
    line, wanted = db.normalize(line), db.normalize(wanted)
    if not line or not wanted:
        return False
    return wanted in line or difflib.SequenceMatcher(None, line, wanted).ratio() >= SAME_TEXT


def strip_personal(text: str, names, cfg) -> str:
    """Blank out the lines of a piece's text that name a person or give an address.

    Best effort: it works line by line on OCR output, so a badly misread name or address can
    get through. What it leaves is the sender, the postage markings and the wording.
    """
    own_street = re.split(r"[,\n]", cfg.physical_mail_address)[0]
    kept = []
    for line in text.splitlines():
        if _STREET.search(line) or _PO_BOX.search(line) or _ENDS_WITH_ZIP.search(line) \
                or _resembles(line, own_street):
            kept.append("[address removed]")
        elif any(_resembles(line, name) for name in names) or has_last_name(
            [line], (*cfg.resident_last_names, *cfg.previous_resident_last_names)
        ):
            kept.append("[name removed]")
        else:
            kept.append(line)
    return "\n".join(kept)


def categorize(
    text: str, remembered: str | None = None, campaign: bool = False
) -> Verdict:
    """Decide a piece's category from its text.

    `remembered` is the category the user last gave this sender;
    `campaign` means USPS showed the piece with an advertiser's name and artwork.
    """
    if not text.strip() and not campaign and not remembered:
        return Verdict("undetermined", 0.0, "No readable text on the scan.", "no_text")
    bulk = _BULK.search(text) or (not _OFFICIAL.search(text) and _TO_ANYONE.search(text))
    if bulk:
        return Verdict("marketing", 0.9, f'Bulk-mail marking "{bulk.group(0)}".', "bulk_marking")
    # Checked before sender memory: a plumber who advertises may also send a real bill.
    bill = _BILL.search(text)
    if bill:
        return Verdict("real", 0.85, f'Looks like a bill or statement ("{bill.group(0)}").', "bill_wording")
    if remembered in ("marketing", "real"):
        label = "junk mail" if remembered == "marketing" else "not junk"
        return Verdict(remembered, 0.85, f"You marked this sender as {label} before.", "sender_memory")
    if campaign:
        return Verdict("marketing", 0.85, "USPS shows this as an advertiser's campaign.", "campaign")
    promos = sorted({m.group(0).lower() for m in _PROMO.finditer(text)})
    official = _OFFICIAL.search(text)
    # First-class postage is what bills travel under, so ask for more evidence there.
    if len(promos) >= (3 if official else 2):
        return Verdict("marketing", 0.7, f"Offer wording: {', '.join(promos[:4])}.", "offer_wording")
    if official and not promos:
        return Verdict("real", 0.8, f'Marked "{official.group(0)}".', "official_marking")
    return Verdict("undetermined", 0.5, "No clear bulk-mail, bill or offer wording.", "no_rule")
