"""One morning unlock: frozen current-volume shortlist, accepted OV/r1, comparison.

Does not publish either proposal, activate pollers, or replace Focus with Vol_test.
"""
from __future__ import annotations

import argparse
from datetime import datetime, time
import getpass
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mb_market_data.opening_hierarchy import load_opening_proposal
from mb_market_data.volume_proxy_experiment import run_comparison

ET = ZoneInfo('America/New_York')


def no_interactive_reauthorization(*args, **kwargs):
    raise RuntimeError('Schwab authorization renewal required; this experiment will not start another login')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--opening-proposal', type=Path, required=True)
    parser.add_argument('--ecfg', type=Path)
    parser.add_argument('--watchlists-checkout', type=Path,
                        help='Existing schwab_watchlists checkout; no files are modified')
    parser.add_argument('--snapshot-time', type=time.fromisoformat, default=time(9, 0))
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--plan', action='store_true', help='Show inputs; no password, network or writes')
    args = parser.parse_args(argv)
    client = None
    try:
        opening_path = args.opening_proposal.expanduser().resolve()
        opening = load_opening_proposal(opening_path)
        now = datetime.now(ET)
        if args.snapshot_time.tzinfo is not None or not time(9) <= args.snapshot_time < time(9, 30):
            raise ValueError('Snapshot time must be local ET from 09:00 to before 09:30')
        scheduled = datetime.combine(opening.session_date, args.snapshot_time, ET)
        output = (args.output_dir or ROOT / 'output' / 'voltest' /
                  now.strftime('%Y%m%dT%H%M%S%f')).expanduser().resolve()
        if args.watchlists_checkout:
            watch_src = args.watchlists_checkout.expanduser().resolve() / 'src'
            if not (watch_src / 'schwab_watchlists' / 'api_ov_focus_production.py').is_file():
                raise FileNotFoundError('Accepted Focus selector not found in supplied checkout')
            sys.path.insert(0, str(watch_src))
        from schwab_watchlists.api_ov_focus_production import (
            load_api_ov_evidence, select_api_ov_symbols, write_api_ov_focus_artifacts,
        )
        ecfg = args.ecfg or (Path(os.environ['MB_SCHWAB_ECFG']) if os.environ.get('MB_SCHWAB_ECFG')
                            else Path(os.environ.get('MB_VAULT', str(Path.home() / 'MBV'))) /
                            'secure_schwabdev.ecfg')
        print(f'Opening date: {opening.session_date}; Uni: {len(opening.uni_symbols)}')
        print(f'Snapshot at: {scheduled.isoformat()}; quote batches: {(len(opening.uni_symbols)+399)//400}')
        print(f'Output: {output}')
        print('Vol_test stays experimental. Reference r1 is constructed, not published.')
        if args.plan:
            print('PLAN ONLY: no authentication, provider requests or writes.')
            return 0
        if opening.revision != 0 or opening.focus_symbols or opening.hot_symbols or not opening.uni_symbols:
            raise ValueError('Expected nonempty opening Uni and empty Focus/Hot r0')
        if now.date() != opening.session_date or now >= opening.effective_at:
            raise ValueError('Run on the opening date before 09:30 ET')
        if output.exists():
            raise FileExistsError('Output exists; choose a new evidence directory')
        if not ecfg.is_file():
            raise FileNotFoundError('Encrypted configuration file not found')
        from mb_tools.schwab_secure import make_secure_schwab_client
        password = getpass.getpass('Encrypted config password: ')
        try:
            client = make_secure_schwab_client(
                ecfg, password, timeout=30, call_on_auth=no_interactive_reauthorization)
        finally:
            del password
        print('Encrypted configuration accepted. One client will serve both acquisitions.', flush=True)
        report = run_comparison(client, opening_path=opening_path, output_dir=output,
                                snapshot_at=scheduled)
        return 0 if report['result'] == 'COMPARISON_COMPLETE' else 1
    except KeyboardInterrupt:
        print('\nComparison cancelled; any saved evidence is retained.')
        return 130
    except Exception as error:
        # Exception text from authentication/provider libraries may include secrets.
        print(f'Comparison stopped: {type(error).__name__}. No replacement r1 was published.',
              file=sys.stderr)
        return 2
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
