"""Explicit opt-in OHLCV service for a controller-owned experimental child.

This is NOT started by the read-only viewer. The daily controller alone starts
the service and supplies an already-authenticated client using the established
private password handoff and authorization lock. Browser symbol selection is a
nonsecret, validated request; the worker never writes the quote journal.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import time
from typing import Any, Callable

from .acquisition_health import atomic_json
from .daily_worker_liveness import OwnerHeartbeatGuard
from .observation_overlay import ET
from .overlay_live_ohlcv import _symbol
from .overlay_ohlcv_schedule import OwnedOHLCVSchedule

UTC = timezone.utc
SELECTION_CONTRACT = "owned-overlay-ohlcv-selection-v1"
OWNER_ROLE = "ohlcv"
TICK_SECONDS = 2


def configure_ohlcv(plan: dict) -> dict:
    """Add an explicit experimental role; preserve production defaults."""
    root = Path(plan["state_root"]).resolve()
    data = Path(plan["data_root"]).resolve()
    if root == data or not root.is_relative_to(data):
        raise ValueError("OHLCV controller state must be under the daily data root")
    session_date = date.fromisoformat(plan["session_date"])
    opens = datetime.fromisoformat(plan["opens_at"]).astimezone(ET)
    closes = datetime.fromisoformat(plan["closes_at"]).astimezone(ET)
    if opens.date() != session_date or closes.date() != session_date:
        raise ValueError("OHLCV controller dates must match its ET session")
    if not opens < closes:
        raise ValueError("OHLCV controller requires a positive session window")
    return dict(
        plan,
        overlay_ohlcv_enabled=True,
        overlay_ohlcv_selection_root=str(root / "ohlcv"),
        overlay_ohlcv_evidence_root=str(data / "output" / "overlay_ohlcv_live"),
        overlay_ohlcv_request_budget=2,
    )


def _paths(plan: dict) -> tuple[Path, Path]:
    if plan.get("overlay_ohlcv_enabled") is not True:
        raise ValueError("OHLCV worker not enabled in the original daily plan")
    root = Path(plan["state_root"]).resolve()
    selection = Path(plan["overlay_ohlcv_selection_root"]).resolve()
    evidence = Path(plan["overlay_ohlcv_evidence_root"]).resolve()
    if selection != root / "ohlcv":
        raise ValueError("OHLCV selection root must be bound to session state")
    if evidence != Path(plan["data_root"]).resolve() / "output" / "overlay_ohlcv_live":
        raise ValueError("OHLCV evidence root differs from the configured daily plan")
    return selection, evidence


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("OHLCV clock must be timezone-aware")
    return value.astimezone(ET)


def _owner_alive(root: Path, owner_pid: int, clock: Callable[[], datetime]) -> bool:
    return OwnerHeartbeatGuard(
        root, pid=owner_pid,
        reader=lambda path: json.loads(Path(path).read_text(encoding="utf-8")),
        clock=clock,
    ).available()


class OwnedOHLCVSelection:
    """Nonsecret symbol handoff, only for an explicitly authorized live owner."""

    def __init__(
        self, plan: dict, *, owner_pid: int,
        clock: Callable[[], datetime] = lambda: datetime.now(ET),
        owner_check: Callable[[Path, int, Callable[[], datetime]], bool] = _owner_alive,
    ) -> None:
        self.plan = plan
        self.owner_pid = owner_pid
        self.clock = clock
        self.owner_check = owner_check
        self.selection_root, self.evidence_root = _paths(plan)
        self.file = self.selection_root / "selected.json"
        self.session_date = date.fromisoformat(plan["session_date"])
        self.closes_at = datetime.fromisoformat(plan["closes_at"]).astimezone(ET)
        self.opens_at = datetime.fromisoformat(plan["opens_at"]).astimezone(ET)

    def _validate_owner(self) -> None:
        if (
            not isinstance(self.owner_pid, int) or self.owner_pid <= 0
            or not self.owner_check(Path(self.plan["state_root"]), self.owner_pid, self.clock)
            or (Path(self.plan["state_root"]) / "exit.request").exists()
            or (Path(self.plan["state_root"]) / "stop.request").exists()
        ):
            raise ValueError("No current authorized OHLCV controller owner")

    def publish(self, symbol: str) -> Path:
        """Browser-initiated capture intent; never a provider request."""
        selected = _symbol(symbol)
        current = _now(self.clock)
        if current.date() != self.session_date or not self.opens_at <= current < self.closes_at:
            raise ValueError("OHLCV selection is outside the authorized live session")
        self._validate_owner()
        self.selection_root.mkdir(parents=True, exist_ok=True)
        atomic_json(self.file, {
            "contract": SELECTION_CONTRACT,
            "session_date": self.session_date.isoformat(),
            "owner_pid": self.owner_pid,
            "opening_content_sha256": self.plan["opening_content_sha256"],
            "symbol": selected,
            "selected_at_utc": current.astimezone(UTC).isoformat(),
        })
        return self.file

    def read(self) -> str | None:
        """Worker-only consumption; ignore a stale/invalid selection."""
        try:
            payload = json.loads(self.file.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("contract") != SELECTION_CONTRACT
                or payload.get("session_date") != self.session_date.isoformat()
                or payload.get("owner_pid") != self.owner_pid
                or payload.get("opening_content_sha256")
                != self.plan["opening_content_sha256"]
            ):
                return None
            recorded = datetime.fromisoformat(payload["selected_at_utc"])
            if recorded.utcoffset() is None:
                return None
            at = recorded.astimezone(ET)
            if at.date() != self.session_date or not self.opens_at <= at < self.closes_at:
                return None
            if at > _now(self.clock) + timedelta(seconds=3):
                return None
            return _symbol(payload["symbol"])
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            return None


def owned_ohlcv_service(
    plan: dict, client: Any, *, owner_pid: int,
    stop: Callable[[], bool], clock: Callable[[], datetime] = lambda: datetime.now(ET),
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run an opt-in child with a supplied SDK client until close/owner stop.

    Caller must hold the exclusive OHLCV worker lease and must own/close the
    client. No additional authentication, quote-journal, or membership writes.
    """
    selected = OwnedOHLCVSelection(plan, owner_pid=owner_pid, clock=clock)
    gate = OwnedOHLCVSchedule(
        session_date=selected.session_date,
        opens_at=selected.opens_at, closes_at=selected.closes_at,
        state_root=selected.selection_root, evidence_root=selected.evidence_root,
    )
    current = _now(clock)
    while current.date() == selected.session_date and current <= selected.closes_at + timedelta(minutes=2):
        if stop() or (Path(plan["state_root"]) / "stop.request").exists():
            break
        symbol = selected.read()
        result = gate.tick(
            client=client, selected_symbol=symbol, now=current,
            owner_active=lambda: not stop() and not (
                Path(plan["state_root"]) / "stop.request"
            ).exists(),
        )
        try:
            # A status failure is visible in the service log but must not
            # misrepresent an actual successful provider acquisition.
            selected.selection_root.mkdir(parents=True, exist_ok=True)
            atomic_json(selected.selection_root / "status.json", {
                "role": OWNER_ROLE,
                "state": result.status,
                "at": current.isoformat(),
                "owner_pid": owner_pid,
                "session_date": selected.session_date.isoformat(),
                "symbol": result.symbol,
                "slot_close_et": result.slot_close_et.isoformat()
                if result.slot_close_et is not None else None,
                "candle_count": result.candle_count,
                "bundle_name": result.bundle_path.name
                if result.bundle_path is not None else None,
                "error_type": result.error_type,
            })
        except OSError as error:
            print("OHLCV STATUS WRITE UNAVAILABLE:", type(error).__name__, flush=True)
        if result.status in {"PUBLISHED", "FAILED", "NOT_READY"}:
            print(
                "OWNED OHLCV:", result.status, result.symbol,
                result.slot_close_et, result.candle_count,
                result.error_type or "", flush=True,
            )
        sleep(TICK_SECONDS)
        current = _now(clock)
    return 0
