"""A Home Assistant notification saying there is mail waiting in For Review."""

import json
import logging
import os
import threading
import urllib.request

log = logging.getLogger(__name__)

CORE_API = "http://supervisor/core/api/services/persistent_notification/"
NOTIFICATION_ID = "informed_unsubscribe_for_review"
TITLE = "Informed Unsubscribe"
MESSAGE = "There is mail waiting for your review."
TIMEOUT = 10

_lock = threading.Lock()
_last: int | None = None  # the queue size last acted on; None until the first check


def _call(action: str, **data) -> None:
    request = urllib.request.Request(
        CORE_API + action, data=json.dumps(data).encode(),
        headers={"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}",
                 "Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=TIMEOUT).close()


def review(waiting: int, enabled: bool, link: str = "") -> None:
    """Show the notification when mail arrives in For Review and take it down once the queue
    is empty. `waiting` is the queue's size now.

    It is only put up when the queue grows, so one the user has dismissed stays dismissed
    while they work through the queue. Outside Home Assistant this does nothing.
    """
    global _last
    if not os.environ.get("SUPERVISOR_TOKEN"):
        return
    with _lock:
        before, _last = _last, waiting
        try:
            if not enabled or waiting == 0:
                # Also on the first check, to clear one left over from before a restart.
                if before is None or before > 0:
                    _call("dismiss", notification_id=NOTIFICATION_ID)
                    _last = 0
            elif before is None or waiting > before:
                message = f"{MESSAGE} [Open Informed Unsubscribe]({link})" if link else MESSAGE
                _call("create", notification_id=NOTIFICATION_ID, title=TITLE, message=message)
        except (OSError, ValueError) as exc:
            log.warning("Could not update the Home Assistant notification: %s", exc)
