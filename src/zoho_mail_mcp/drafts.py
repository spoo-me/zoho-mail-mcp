"""In-memory draft store backing the two-step send confirmation flow.

A draft only lives inside the running MCP server process, expires after
DRAFT_TTL_SECONDS, and is consumed (removed) by a successful send. Sending
requires the draft_id *and* the confirm_token that were returned with the
preview, which forces the caller to have actually created and read the draft.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

DRAFT_TTL_SECONDS = 15 * 60


class DraftError(RuntimeError):
    pass


@dataclass
class Draft:
    draft_id: str
    confirm_token: str
    account_name: str
    recipients_display: str
    subject: str
    payload: dict
    reply_to_message_id: str | None
    created_at: float


_drafts: dict[str, Draft] = {}


def _purge() -> None:
    now = time.monotonic()
    for key in [k for k, d in _drafts.items() if now - d.created_at > DRAFT_TTL_SECONDS]:
        del _drafts[key]


def store(
    account_name: str,
    recipients_display: str,
    subject: str,
    payload: dict,
    reply_to_message_id: str | None = None,
) -> Draft:
    _purge()
    draft = Draft(
        draft_id=f"draft-{secrets.token_hex(4)}",
        confirm_token=secrets.token_hex(3).upper(),
        account_name=account_name,
        recipients_display=recipients_display,
        subject=subject,
        payload=payload,
        reply_to_message_id=reply_to_message_id,
        created_at=time.monotonic(),
    )
    _drafts[draft.draft_id] = draft
    return draft


def take(draft_id: str, confirm_token: str) -> Draft:
    """Validate and consume a draft; it cannot be sent twice."""
    _purge()
    draft = _drafts.get(draft_id)
    if draft is None:
        raise DraftError(
            f"Draft {draft_id!r} not found or expired (drafts live {DRAFT_TTL_SECONDS // 60} "
            "minutes, in memory only). Create a new draft with zoho_draft_email and get the "
            "user's approval again."
        )
    if confirm_token != draft.confirm_token:
        raise DraftError(
            "confirm_token does not match this draft. Use the exact token from the draft "
            "preview; never guess or fabricate it."
        )
    del _drafts[draft_id]
    return draft


def restore(draft: Draft) -> None:
    """Put a draft back after a failed send so it can be retried."""
    _drafts[draft.draft_id] = draft


def discard(draft_id: str) -> bool:
    _purge()
    return _drafts.pop(draft_id, None) is not None


def pending() -> list[Draft]:
    _purge()
    return sorted(_drafts.values(), key=lambda d: d.created_at)


def remaining_seconds(draft: Draft) -> int:
    return max(0, int(DRAFT_TTL_SECONDS - (time.monotonic() - draft.created_at)))
