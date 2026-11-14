"""Explicit daily session entry. Default plan is non-mutating; start needs --live."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from mb_market_data.daily_runtime import main
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(f'DAILY SESSION: {type(error).__name__}: {error}',file=sys.stderr)
        raise SystemExit(2)
