"""
Minimal Schwab CHART_EQUITY capability probe.

This probe is intentionally small.  Its immediate purpose is to learn:

1. What CHART_EQUITY emits for SPY.
2. What field 1 (Sequence) looks like.
3. What candle interval and session start the sequence implies.
4. Whether the stream exposes useful volume outside the REST
   price_history session.

All displayed timestamps use Eastern Time.
"""

from __future__ import annotations

import getpass
import json
import os
import queue
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import schwabdev

from mb_tools.schwab_secure import (
    console_auth_callback,
    make_secure_schwab_client,
)


ET = ZoneInfo("America/New_York")


def resolve_ecfg() -> Path:
    env_ecfg = os.environ.get("MB_SCHWAB_ECFG")

    if env_ecfg:
        path = Path(env_ecfg).expanduser()

        if path.is_file():
            return path.resolve()

    mb_vault = os.environ.get("MB_VAULT")

    if mb_vault:
        path = (
            Path(mb_vault).expanduser()
            / "secure_schwabdev.ecfg"
        )

        if path.is_file():
            return path.resolve()

    path = Path("secure_schwabdev.ecfg")

    if path.is_file():
        return path.resolve()

    raise FileNotFoundError(
        "Could not find secure_schwabdev.ecfg"
    )


def epoch_ms_to_et(value):
    if not isinstance(value, (int, float)):
        return None

    if value < 1_000_000_000_000:
        return None

    return datetime.fromtimestamp(
        value / 1000.0,
        tz=ET,
    )


def main():
    symbol = "SPY"
    duration = 30

    ecfg_path = resolve_ecfg()

    print()
    print("Schwab CHART_EQUITY probe")
    print("=" * 72)
    print(f"Symbol           : {symbol}")
    print(f"Duration         : {duration} seconds")
    print(f"Encrypted config : {ecfg_path}")
    print()

    password = getpass.getpass(
        "Encrypted config password: "
    )

    client = make_secure_schwab_client(
        ecfg_path,
        password,
        timeout=10,
        call_on_auth=console_auth_callback,
    )

    messages = queue.Queue()

    def receiver(message):
        messages.put(
            (
                datetime.now(ET),
                message,
            )
        )

    streamer = schwabdev.Stream(client)

    try:
        streamer.start(
            receiver=receiver,
            daemon=True,
        )

        request = streamer.chart_equity(
            symbol,
            list("012345678"),
            command="SUBS",
        )

        streamer.send(request)

        print()
        print("CHART_EQUITY subscription submitted.")
        print()

        deadline = time.monotonic() + duration

        while time.monotonic() < deadline:
            try:
                received_at, raw = messages.get(
                    timeout=1.0
                )
            except queue.Empty:
                continue

            print("-" * 72)
            print(
                "Received:",
                received_at.isoformat(),
            )

            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                print(raw)
                continue

            for data_item in message.get(
                "data",
                [],
            ):
                if (
                    data_item.get("service")
                    != "CHART_EQUITY"
                ):
                    continue

                print(
                    "Service timestamp:",
                    data_item.get("timestamp"),
                )

                for item in data_item.get(
                    "content",
                    [],
                ):
                    chart_time = epoch_ms_to_et(
                        item.get("7")
                    )

                    print(
                        "key        :",
                        item.get("key"),
                    )
                    print(
                        "field 1 seq:",
                        item.get("1"),
                    )
                    print(
                        "msg seq    :",
                        item.get("seq"),
                    )
                    print(
                        "open       :",
                        item.get("2"),
                    )
                    print(
                        "high       :",
                        item.get("3"),
                    )
                    print(
                        "low        :",
                        item.get("4"),
                    )
                    print(
                        "close      :",
                        item.get("5"),
                    )
                    print(
                        "volume     :",
                        item.get("6"),
                    )
                    print(
                        "chart time :",
                        (
                            chart_time.isoformat()
                            if chart_time
                            else item.get("7")
                        ),
                    )
                    print(
                        "chart day  :",
                        item.get("8"),
                    )
                    print()
                    print(
                        "RAW CONTENT:"
                    )
                    print(
                        json.dumps(
                            item,
                            indent=2,
                            sort_keys=True,
                        )
                    )

    finally:
        try:
            streamer.stop()
        except Exception:
            pass

        try:
            client.close()
        except Exception:
            pass

    print()
    print("Probe finished.")


if __name__ == "__main__":
    main()
