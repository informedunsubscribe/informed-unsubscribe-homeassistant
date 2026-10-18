"""The wording of the requests the app writes, which the user can replace with their own.

A template is a subject and a body containing variables in braces, such as {sender}. Custom
templates are kept in a file in the data folder; anything not customised uses the defaults.
"""

import json
import re
from pathlib import Path

KINDS = {"optout": "Opt-out request", "moved": "Does not live here"}

# Written to be opened, acted on, or passed along: the subject says what is wanted and that
# it is about paper mail; the record to find comes first; and the reader is asked outright
# to forward it if it isn't theirs to handle.
DEFAULTS = {
    "optout": {
        "subject": "Please remove this address from your postal mailing list",
        "body": """Hello,

Please remove this address from your postal (paper) mailing list:

{address_block}

I received mail from {sender}{addressed_to_phrase} and would like it to stop. Specifically, \
please:

1. Stop sending marketing or solicitation mail to this address.
2. Delete the personal information you hold for this {name_and_address_phrase}, keeping only \
what you need to honor this request.
3. Stop selling, renting or sharing it, and pass this request on to anyone you have shared it \
with or who sends mail on your behalf.
{legal_paragraphs}
If you are not the right person for this, please forward it to whoever manages your mailing \
list or privacy requests.

A short reply to confirm would be appreciated. Thank you.
""",
    },
    "moved": {
        "subject": "Wrong address on file for {addressed_to}",
        "body": """Hello,

Mail from {sender} for {addressed_to} is being delivered to this address:

{physical_mail_address}

{addressed_to} does not live here. Earlier mail for {addressed_to} was marked "Return to \
Sender" and sent back, but it continues to arrive. Please remove this address from your \
records for {addressed_to}, so that their mail, which may contain private information, stops \
arriving here.

If you are not the right person for this, please forward it to whoever maintains your \
customer or mailing records.

A short reply to confirm would be appreciated. Thank you.
""",
    },
}

# Every variable a template may use, with what it stands for. Deliberately not here: the
# mailbox password, the search key, and the household's lists of names.
VARIABLES = {
    "sender": "Who sent the mail",
    "addressed_to": "Who the mail is addressed to (this could be empty!)",
    "addressed_to_phrase": (
        'If {addressed_to} is not empty, this becomes " addressed to {addressed_to}" '
        "(with a space in front). If {addressed_to} is empty, this is empty."
    ),
    "name_and_address_phrase": (
        'If {addressed_to} is not empty, this becomes the words "name and address". '
        'If {addressed_to} is empty, this becomes the word "address".'
    ),
    "address_block": (
        "If {addressed_to} is not empty, this becomes {addressed_to} on one line with "
        "{physical_mail_address} on the next. If {addressed_to} is empty, this is just "
        "{physical_mail_address}."
    ),
    "physical_mail_address": "Your mailing address, from Settings",
    "legal_paragraphs": "The California and DMAchoice paragraphs switched on in Settings",
    "date_received": "The date the mail arrived",
    "website": "The sender's website, if known",
}

# Added to a request only when the matching setting is on. Each is worded "to the extent it
# applies" because many senders fall outside these laws.
CALIFORNIA_PARAGRAPH = (
    "I am a California resident. To the extent the California Consumer Privacy Act (Cal. Civ. "
    "Code § 1798.100 et seq.) applies to you, this is a request to delete my personal "
    "information under § 1798.105 and to opt out of its sale or sharing under § 1798.120. "
    "The Act and its regulations require you to act on an opt-out within 15 business days and "
    "to respond to a deletion request within 45 days."
)
DMACHOICE_PARAGRAPH = (
    "This address is registered with DMAchoice, the mail preference service run by the "
    "Association of National Advertisers, as not wanting marketing mail. Please check your "
    "mailing lists against it before future mailings."
)
_VARIABLE = re.compile(r"\{([a-z_]+)\}")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def legal_paragraphs(cfg) -> str:
    chosen = [
        text for wanted, text in (
            (cfg.california_resident, CALIFORNIA_PARAGRAPH),
            (cfg.registered_with_dmachoice, DMACHOICE_PARAGRAPH),
        ) if wanted
    ]
    return "".join(f"\n{text}\n" for text in chosen)


def values(cfg, sender: str, addressee: str = "", received: str = "", website: str = "") -> dict:
    """What each variable stands for, for one piece of mail."""
    date = ""
    if received[:10].count("-") == 2:
        year, month, day = received[:10].split("-")
        date = f"{year} {_MONTHS[int(month) - 1]} {int(day)}"
    return {
        "sender": sender,
        "addressed_to": addressee,
        "addressed_to_phrase": f" addressed to {addressee}" if addressee else "",
        "name_and_address_phrase": "name and address" if addressee else "address",
        "address_block": f"{addressee}\n{cfg.physical_mail_address}".strip(),
        "physical_mail_address": cfg.physical_mail_address,
        "legal_paragraphs": legal_paragraphs(cfg),
        "date_received": date,
        "website": website,
    }


def fill(template: str, known: dict) -> str:
    """Put values in place of {variables}. Anything unrecognised is left exactly as written,
    so a typo shows up in the draft instead of silently vanishing."""
    return _VARIABLE.sub(lambda m: str(known[m.group(1)]) if m.group(1) in known else m.group(0), template)


def unknown_variables(template: str) -> list[str]:
    return sorted({name for name in _VARIABLE.findall(template) if name not in VARIABLES})


def _path(cfg) -> Path:
    return Path(cfg.data_dir) / "templates.json"


def load(cfg) -> dict:
    """The templates in use: the user's where they have set one, otherwise the defaults."""
    try:
        saved = json.loads(_path(cfg).read_text())
    except (OSError, ValueError):
        saved = {}
    templates = {}
    for kind, default in DEFAULTS.items():
        custom = saved.get(kind) if isinstance(saved, dict) else None
        custom = custom if isinstance(custom, dict) else {}
        templates[kind] = {
            part: custom[part] if isinstance(custom.get(part), str) and custom[part].strip() else default[part]
            for part in ("subject", "body")
        }
    return templates


def save(cfg, templates: dict) -> None:
    """Keep only what differs from the defaults, so untouched wording follows future updates."""
    custom = {
        kind: {part: text for part, text in parts.items() if text != DEFAULTS[kind][part]}
        for kind, parts in templates.items() if kind in DEFAULTS
    }
    path = _path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({kind: parts for kind, parts in custom.items() if parts}, indent=2))


def write(cfg, kind: str, known: dict) -> tuple[str, str]:
    """The subject and body of a request of this kind."""
    template = load(cfg)[kind]
    subject = " ".join(fill(template["subject"], known).split())
    return subject, fill(template["body"], known)
