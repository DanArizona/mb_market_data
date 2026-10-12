"""Owned candle child; launch through supervise_live_candles.py."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mb_market_data.candle_capture import main

if __name__ == "__main__":
    raise SystemExit(main())
