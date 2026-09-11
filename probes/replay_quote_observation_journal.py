"""Replay a daily quote-observation journal without live data sources."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mb_market_data.quote_journal_replay import (
    ChannelRevisionEvent,
    QuoteAcquisitionEvent,
    QuoteJournalReplayReader,
    ReplayEvent,
    paced_replay,
)


ET = ZoneInfo("America/New_York")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay membership revisions and completed quote acquisitions "
            "from one daily SQLite journal."
        )
    )
    parser.add_argument("database", type=Path, help="Daily .sqlite3 file")
    pacing = parser.add_mutually_exclusive_group()
    pacing.add_argument(
        "--speed",
        type=float,
        help=(
            "Replay at this time multiplier; for example, 60 maps one "
            "historical minute to one second"
        ),
    )
    pacing.add_argument(
        "--real-time",
        action="store_true",
        help="Preserve the original event timing (equivalent to --speed 1)",
    )
    pacing.add_argument(
        "--step",
        action="store_true",
        help="Wait for Enter before emitting each event",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Stop after this many events; useful with --step",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        metavar="N",
        help=(
            "Print progress every N acquisitions; use 0 to disable "
            "(default: 100)"
        ),
    )
    args = parser.parse_args()
    if args.speed is not None and args.speed <= 0:
        parser.error("--speed must be greater than zero")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be greater than zero")
    if args.progress_every < 0:
        parser.error("--progress-every must be nonnegative")
    return args


def event_label(event: ReplayEvent) -> str:
    available = event.available_at_utc.astimezone(ET)
    if isinstance(event, ChannelRevisionEvent):
        revision = event.revision
        return (
            f"{available:%H:%M:%S.%f} ET  revision  "
            f"{revision.channel} r{revision.revision}  "
            f"symbols={len(revision.symbols)}"
        )
    acquisition = event.acquisition
    return (
        f"{available:%H:%M:%S.%f} ET  acquisition  "
        f"{acquisition.channel}  "
        f"scheduled={acquisition.scheduled_at_utc.astimezone(ET):%H:%M:%S}  "
        f"symbols={len(event.observations)}"
    )


def step_waiter(event: ReplayEvent) -> None:
    input(f"Next: {event_label(event)}\nPress Enter to emit...")


def limited_events(
    events: Iterable[ReplayEvent],
    limit: int | None,
) -> Iterable[ReplayEvent]:
    return itertools.islice(events, limit) if limit is not None else events


def main() -> int:
    args = parse_args()
    try:
        reader = QuoteJournalReplayReader(args.database)
    except Exception as exc:
        print(f"Replay ERROR: {type(exc).__name__}: {exc}")
        return 2

    speed = 1.0 if args.real_time else args.speed
    mode = (
        "single-step"
        if args.step
        else (f"{speed:g}x" if speed is not None else "immediate")
    )
    print()
    print("Quote journal replay")
    print("=" * 79)
    print(f"Database         : {reader.database_path}")
    print(f"Session date     : {reader.session_date.isoformat()}")
    print(f"Mode             : {mode}")
    print(
        "Event limit      : "
        + (str(args.limit) if args.limit is not None else "none")
    )
    print()

    event_count = 0
    revision_count = 0
    acquisition_count = 0
    observation_count = 0
    channel_acquisitions: Counter[str] = Counter()
    channel_observations: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    digest = hashlib.sha256()
    first_available = None
    last_available = None
    started = time.perf_counter()

    source = limited_events(reader.events(), args.limit)
    replay = paced_replay(
        source,
        speed=speed,
        step_waiter=step_waiter if args.step else None,
    )
    try:
        for event in replay:
            event_count += 1
            digest.update(event.event_id.encode("utf-8"))
            digest.update(b"\n")
            if first_available is None:
                first_available = event.available_at_utc
            last_available = event.available_at_utc

            if isinstance(event, ChannelRevisionEvent):
                revision_count += 1
                print(event_label(event))
                continue

            assert isinstance(event, QuoteAcquisitionEvent)
            acquisition_count += 1
            channel_acquisitions[event.channel] += 1
            observation_count += len(event.observations)
            channel_observations[event.channel] += len(event.observations)
            status_counts.update(item.status for item in event.observations)
            if (
                args.progress_every
                and acquisition_count % args.progress_every == 0
            ):
                print(
                    f"Acquisition {acquisition_count:>5,}  "
                    f"{event_label(event)}"
                )
    except (EOFError, KeyboardInterrupt):
        print()
        print("Replay stopped by user.")

    elapsed = time.perf_counter() - started
    print()
    print("Replay summary")
    print("=" * 79)
    print(f"Events           : {event_count:,}")
    print(f"Revisions        : {revision_count:,}")
    print(f"Acquisitions     : {acquisition_count:,}")
    print(f"Observations     : {observation_count:,}")
    for channel in sorted(channel_acquisitions):
        print(
            f"  {channel:<14}: "
            f"{channel_acquisitions[channel]:,} acquisitions, "
            f"{channel_observations[channel]:,} observations"
        )
    print(
        "Statuses         : "
        + ", ".join(
            f"{name}={count:,}" for name, count in sorted(status_counts.items())
        )
    )
    if first_available is not None and last_available is not None:
        print(
            "Event window     : "
            f"{first_available.astimezone(ET).isoformat()} through "
            f"{last_available.astimezone(ET).isoformat()}"
        )
    print(f"Wall time        : {elapsed:.3f} seconds")
    if elapsed > 0:
        print(f"Observations/sec : {observation_count / elapsed:,.0f}")
    print(f"Sequence SHA-256 : {digest.hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
