"""Acquire one immutable completed-session OHLCV cache for OO replay."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import date, datetime
from pathlib import Path

from mb_market_data.observation_overlay import ET
from mb_market_data.observation_overlay_acquisition import (
    ObservationOverlayAcquisitionError,
    REQUEST_END,
    REQUEST_START,
    acquire_observation_overlay_ohlcv,
    validate_completed_session,
    write_observation_overlay_acquisition,
)
from mb_tools.schwab_secure import (
    console_auth_callback,
    make_secure_schwab_client,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Acquire and immutably preserve one completed ET session of "
            "Schwab five-minute OHLCV for Observation Overlay replay."
        )
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--ecfg")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--output-root",
        default="output/observation_overlay_ohlcv",
    )
    parser.add_argument(
        "--output-dir",
        help="Explicit immutable output directory; overrides --output-root.",
    )
    return parser.parse_args()


def resolve_ecfg(explicit_path: str | None) -> Path:
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path).expanduser())
    environment_path = os.environ.get("MB_SCHWAB_ECFG")
    if environment_path:
        candidates.append(Path(environment_path).expanduser())
    vault = os.environ.get("MB_VAULT")
    if vault:
        candidates.append(Path(vault).expanduser() / "secure_schwabdev.ecfg")
    candidates.append(Path.cwd() / "secure_schwabdev.ecfg")
    for path in candidates:
        if path.is_file():
            return path.resolve()
    searched = "\n".join(f"  {path}" for path in candidates)
    raise FileNotFoundError(
        "Could not find secure_schwabdev.ecfg.\n"
        f"Paths checked:\n{searched}"
    )


def parse_session_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"invalid --session-date {text!r}; expected YYYY-MM-DD"
        ) from error


def main() -> int:
    args = parse_args()
    try:
        symbol = args.symbol.strip().upper()
        if not symbol:
            raise ValueError("--symbol must be nonblank")
        session_date = parse_session_date(args.session_date)
        now_et = datetime.now(ET)
        validate_completed_session(session_date, now_et)
        ecfg_path = resolve_ecfg(args.ecfg)
    except (
        ObservationOverlayAcquisitionError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        print(
            f"Observation Overlay OHLCV ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    print("Observation Overlay OHLCV acquisition")
    print("=" * 79)
    print(f"Symbol             : {symbol}")
    print(f"Session date       : {session_date}")
    print(
        "Request interval   : "
        f"{REQUEST_START.strftime('%H:%M')} <= candle start < "
        f"{REQUEST_END.strftime('%H:%M')} ET"
    )
    print("Frequency          : 5 minutes")
    print("Extended hours     : YES")
    print(f"Encrypted config   : {ecfg_path}")
    print()

    password = getpass.getpass("Encrypted config password: ")
    client = None
    try:
        client = make_secure_schwab_client(
            ecfg_path,
            password,
            timeout=args.timeout,
            call_on_auth=console_auth_callback,
        )
        print("Encrypted configuration accepted.", flush=True)
        acquisition = acquire_observation_overlay_ohlcv(
            client,
            symbol=symbol,
            session_date=session_date,
        )
    except Exception as error:
        print(
            f"Observation Overlay OHLCV ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    run_stamp = acquisition.response_received_at_utc.astimezone(ET).strftime(
        "%Y-%m-%d-%H-%M-%S"
    )
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else Path(args.output_root)
        / f"{session_date}-{symbol}-{run_stamp}"
    )
    try:
        artifacts = write_observation_overlay_acquisition(
            output_dir,
            acquisition,
        )
    except (
        ObservationOverlayAcquisitionError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        print(
            f"Observation Overlay OHLCV ERROR: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    candles = acquisition.cache.candles
    print()
    print(f"HTTP status        : {acquisition.http_status}")
    print(f"Candles retained   : {len(candles):,}")
    print(f"First candle ET    : {candles[0].start_et.isoformat()}")
    print(f"Last candle ET     : {candles[-1].start_et.isoformat()}")
    print(f"Raw payload SHA-256: {acquisition.cache.source_payload_sha256}")
    print(f"Raw payload        : {artifacts.raw_payload}")
    print(f"OO cache           : {artifacts.cache}")
    print(f"Manifest           : {artifacts.manifest}")
    print("Observation Overlay OHLCV: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
