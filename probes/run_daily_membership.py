"""Operator control entry point; no secret input."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from mb_market_data.daily_membership import main
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as e:print('MEMBERSHIP COMMAND:',type(e).__name__,str(e));raise SystemExit(2)
