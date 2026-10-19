"""Internal offline worker. Never uses credentials or a network transport."""
from pathlib import Path
import json
import sys

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from history_proof import bootstrap,deny_network,load_config,worker,MODES,ROOT
from run_review import verify_sources


def main():
    try:
        require_args=len(sys.argv) in (3,4) and sys.argv[2] in MODES
        if not require_args: raise ValueError('Internal known worker required')
        deny_network();verify_sources();bootstrap()
        folder=Path(sys.argv[1]).resolve()
        if folder.parent!=(ROOT/'runs/history').resolve(): raise ValueError('Worker path escapes review runs')
        load_config(folder)
        return worker(folder,sys.argv[2],drill=json.loads(sys.argv[3]) if len(sys.argv)==4 else None)
    except Exception:
        import traceback
        traceback.print_exc()
        return 1


if __name__=='__main__':raise SystemExit(main())
