"""Internal identity-bound worker; credentials arrive only through stdin."""
from pathlib import Path
import json
import sys

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from interleaved_load import bootstrap,deny_network,load_config,validate_run_path,worker,MODES
from interleaved_review import verify_sources


def main():
    credential=None
    offline=False
    try:
        if len(sys.argv)!=3 or sys.argv[2] not in MODES:
            raise ValueError('Internal bounded run and known role required')
        verify_sources()
        bootstrap()
        folder=validate_run_path(sys.argv[1])
        config,_=load_config(folder)
        offline=config['source']=='synthetic_only'
        if config['source']=='synthetic_only':
            deny_network()
        elif sys.argv[2]!='coordinator':
            credential=json.loads(sys.stdin.read(65536))
            if set(credential)!={'ecfg','password'}:
                raise ValueError('Private worker input differs')
        return worker(folder,sys.argv[2],credential)
    except Exception as exc:
        # SDK/config exceptions and chained traceback text may contain secrets.
        print('Experimental worker failed ('+type(exc).__name__+').',flush=True)
        if offline:
            import traceback
            traceback.print_exc()
        return 1
    finally:
        if credential: credential.clear()


if __name__=='__main__': raise SystemExit(main())
