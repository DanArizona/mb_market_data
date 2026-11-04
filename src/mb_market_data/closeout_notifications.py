"""Disabled-by-default extension point for future Pushover closeout alerts.

NO network transport, token, account, credential-file access or push delivery.
A future implementation must be explicitly approved and tested before opt-in.
This module deliberately never claims a message has been sent.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class CloseoutEvent(StrEnum):
    PREPARATION_DUE = "preparation_due"
    DEADLINE_MISSED = "deadline_missed"
    PREPARATION_READY = "preparation_ready"


@dataclass(frozen=True, slots=True)
class CloseoutNotice:
    event: CloseoutEvent
    source_session: str
    title: str
    message: str
    next_session: str | None = None
    urgency: str = "warning"

    def __post_init__(self) -> None:
        if not self.source_session or not self.title or not self.message:
            raise ValueError("Closeout notice requires source, title and message")
        if self.urgency not in {"notice", "warning", "critical"}:
            raise ValueError("Unsupported urgency")


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    provider: str
    outcome: str
    detail: str

    @property
    def delivered(self) -> bool:
        return self.outcome == "DELIVERED"


class CloseoutNotificationChannel(Protocol):
    def dispatch(self, notice: CloseoutNotice) -> DeliveryResult:
        ...


class PushoverStub:
    """Future integration contract; intentionally incapable of delivery.

    When a Pushover account is reactivated, the replacement will require an
    encrypted credential source rather than command-line secrets, and it
    must explicitly opt in to real HTTPS delivery.
    """

    def __init__(self, *, enabled: bool = False):
        self.enabled = enabled

    def dispatch(self, notice: CloseoutNotice) -> DeliveryResult:
        if self.enabled:
            return DeliveryResult(
                provider="pushover",
                outcome="NOT_IMPLEMENTED",
                detail="No enabled transport or credential loader is installed; no push sent",
            )
        return DeliveryResult(
            provider="pushover",
            outcome="DISABLED",
            detail="No configured Pushover account or approved delivery capability; no push sent",
        )


def due_notice(source_session: str, explanation: str) -> CloseoutNotice:
    """Human-readable, credential-free payload for desktop and future phone."""
    return CloseoutNotice(
        event=CloseoutEvent.PREPARATION_DUE,
        source_session=source_session,
        title="MasterBot: post-close preparation due",
        message=(f"Source {source_session}: next-session Universe preparation is "
                 f"not READY. {explanation} Authenticate once in the attended "
                 "closeout command before the Eastern calendar date ends."),
    )
