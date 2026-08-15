from __future__ import annotations

import sys
import time

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


from mb_market_data.nasdaq_halts import fetch_trade_halts  # noqa: E402
from mb_market_data.nasdaq_halt_monitor import NasdaqHaltMonitor  # noqa: E402


ET_ZONE = ZoneInfo("America/New_York")

POLL_SECONDS = 60
FETCH_TIMEOUT_SECONDS = 60


def now_et() -> datetime:
    return datetime.now(ET_ZONE)


def main() -> int:
    monitor = NasdaqHaltMonitor()

    print("Nasdaq Volatility Halt Monitor")
    print("=" * 70)
    print(f"Poll interval : {POLL_SECONDS} seconds")
    print(f"Fetch timeout : {FETCH_TIMEOUT_SECONDS} seconds")
    print("Reason codes  : LUDP, M")
    print()
    print("Press Ctrl-C to stop.")
    print()

    poll_number = 0

    try:
        while True:
            poll_number += 1

            poll_time = now_et()
            session_date = poll_time.date()

            print(
                f"[{poll_time.strftime('%Y-%m-%d %H:%M:%S ET')}] "
                f"Poll {poll_number}"
            )

            try:
                feed = fetch_trade_halts(
                    timeout=FETCH_TIMEOUT_SECONDS,
                )

                new_symbols = monitor.new_symbols(
                    feed.records,
                    session_date=session_date,
                )

                print(
                    f"  Feed records : {len(feed.records)}"
                )

                print(
                    f"  Seen symbols : {len(monitor.seen_symbols)}"
                )

                if new_symbols:
                    print(
                        f"  NEW symbols  : {len(new_symbols)}"
                    )

                    print(
                        "  "
                        + " ".join(new_symbols)
                    )

                else:
                    print(
                        "  NEW symbols  : 0"
                    )

            except Exception as exc:
                print(
                    f"  ERROR        : "
                    f"{type(exc).__name__}: {exc}"
                )

            print()

            time.sleep(
                POLL_SECONDS
            )

    except KeyboardInterrupt:
        print()
        print("Monitor stopped.")

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
