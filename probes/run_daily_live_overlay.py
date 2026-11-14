"""Read-only live Observation Overlay; never starts an SDK client."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from mb_market_data.daily_live_overlay import main
if __name__=='__main__':
    try:raise SystemExit(main())
    except ImportError as e:
        print('Viewer dependency missing:',e.name,'Use an environment with Plotly (for example dashing); collection is unchanged.')
        raise SystemExit(2)
    except Exception as e:
        print('VIEWER:',type(e).__name__,str(e));raise SystemExit(2)
