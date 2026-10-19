"""Only the explicitly identified shadow run may be opened by this worker."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shadow import bootstrap, worker

if __name__ == '__main__':
    credential = None
    try:
        bootstrap()
        if len(sys.argv) != 3 or sys.argv[2] not in ('coordinator', 'hot'):
            raise ValueError('Explicit shadow run and worker mode required')
        if not sys.stdin.isatty():
            value = sys.stdin.buffer.read(65537)
            if value:
                if len(value) > 65536:
                    raise ValueError('Worker credential input too large')
                credential = json.loads(value.decode())
        raise SystemExit(worker(Path(sys.argv[1]), sys.argv[2], credential))
    except Exception as exc:
        print('Nasdaq shadow worker failed (' + type(exc).__name__ + '). Inspect retained run evidence.', flush=True)
        raise SystemExit(1)
    finally:
        if credential:
            credential.clear()
