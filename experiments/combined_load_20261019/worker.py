"""Internal worker: accepts only an identity-bound experiment directory."""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from combined_load import bootstrap, deny_network, validate_run_path, worker, MODES
from run_review import verify_sources


def main():
    if len(sys.argv) != 3 or sys.argv[2] not in MODES:
        raise ValueError('Internal worker requires a bounded run and one known role')
    deny_network()
    verify_sources()
    bootstrap()
    folder = validate_run_path(sys.argv[1])
    return worker(folder, sys.argv[2])


if __name__ == '__main__':
    raise SystemExit(main())
