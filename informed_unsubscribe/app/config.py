"""Settings come from the HA app options file, with env vars (upper-cased names) taking precedence."""

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Config:
    email_address: str = ""
    email_mailbox_password: str = ""
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    physical_mail_address: str = ""
    # Last names of the people who live here; empty turns off the "not at this address" check.
    resident_last_names: tuple = ()
    # People who used to live here; helps pick their name out of a scan.
    previous_resident_last_names: tuple = ()
    # Each adds a paragraph to opt-out requests; see letters.legal_paragraphs.
    california_resident: bool = False
    registered_with_dmachoice: bool = False
    reply_to_email_address: str = ""
    # Shown beside the mailbox address in From; every sender sees it. Empty: "<street> Resident".
    from_name: str = ""
    # Gets a blind copy of every request sent; the sender never sees it.
    bcc_email_address: str = ""
    # Brave Search API key; without one the app scrapes DuckDuckGo, which rate-limits.
    brave_search_api_key: str = ""
    hours_between_runs: int = 24
    # "HH:MM" in the host's time zone: one run a day at that time, and the interval above is
    # ignored. Empty: the interval is used.
    run_at_time: str = ""
    # A Home Assistant notification while there is mail waiting in For Review.
    notify_when_mail_to_review: bool = True
    # Scans are the bulk of what the app stores; a settled piece keeps its row but not its
    # picture once it is this old.
    days_to_keep_scans: int = 90
    data_dir: str = "/data"


def load() -> Config:
    path = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
    raw = json.loads(path.read_text()) if path.exists() else {}
    values = {}
    for f in fields(Config):
        value = os.environ.get(f.name.upper(), raw.get(f.name))
        if value in (None, ""):
            continue
        if isinstance(f.default, bool):
            values[f.name] = value if isinstance(value, bool) else str(value).lower() in (
                "1", "true", "yes", "on"
            )
        elif isinstance(f.default, tuple):
            items = value.split(",") if isinstance(value, str) else value
            values[f.name] = tuple(str(item).strip() for item in items if str(item).strip())
        else:
            values[f.name] = type(f.default)(value)
    # Google shows app passwords in four groups separated by non-breaking spaces, which come
    # along when copied and can't be sent in a login. Plain spaces are left alone.
    if "email_mailbox_password" in values:
        values["email_mailbox_password"] = "".join(
            ch for ch in values["email_mailbox_password"] if ch == " " or not ch.isspace()
        ).strip()
    return Config(**values)
