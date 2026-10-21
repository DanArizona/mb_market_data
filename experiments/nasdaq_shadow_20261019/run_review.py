"""Offline qualification and an independently invoked authenticated shadow run."""
from pathlib import Path
import getpass
import hashlib
import json
import os
import sys
import unittest
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from shadow import (CONTRACT, SecureHorizon, acquire_catalog, bootstrap, check_live_window,
                    deny_network, load_roster, policy, read_json, request_stop, run, sha, utc_now, write_json)
from feed_capture import FeedResponse


def offline_interrupted_feed(attempt, config, normal):
    """One synthetic HTML response after the baseline; no real provider fixture."""
    return FeedResponse(b'<html><head><script src="/synthetic-check"></script></head><body></body></html>'
                        if attempt == 2 else normal,
                        {'kind': 'synthetic', 'network_request': False})


def verify_sources():
    manifest = read_json(HERE / 'source_manifest.json')
    if manifest.get('contract') != CONTRACT or not manifest.get('files'):
        raise ValueError('Shadow inventory differs')
    for relative, checksum in manifest['files'].items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT) or sha(path.read_bytes()) != checksum:
            raise ValueError('Shadow source bytes differ')
    return sha((HERE / 'source_manifest.json').read_bytes())


def check_verification(manifest_sha):
    receipt = read_json(ROOT / 'runs/nasdaq_shadow_verification.json')
    report = Path(receipt['report']).resolve()
    if (receipt.get('result') != 'PASS' or receipt['manifest_sha256'] != manifest_sha
            or report.parent.parent != (ROOT / 'runs/nasdaq_shadow').resolve()
            or sha(report.read_bytes()) != receipt['report_sha256']
            or read_json(report).get('result') not in ('PASS', 'PASS_WITH_WARNINGS')
            or read_json(report).get('milestone') != 'SYNTHETIC_TRANSITION'):
        raise ValueError('Offline shadow qualification missing or changed')


def resolve_ecfg():
    explicit = os.environ.get('MB_SCHWAB_ECFG')
    if explicit:
        path = Path(explicit).expanduser()
    else:
        vault = os.environ.get('MB_VAULT')
        path = (Path(vault).expanduser() if vault else Path.home() / 'MBV') / 'secure_schwabdev.ecfg'
    if path.suffix.lower() != '.ecfg' or not path.is_file():
        raise ValueError('Existing encrypted Schwab configuration required')
    return path.resolve()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    expected = 4 if argv and argv[0] in ('live', 'live-extended') else 1
    if not argv or argv[0] not in ('verify', 'live', 'live-extended', 'stop') or len(argv) != expected:
        print('Use 19_verify_nasdaq_shadow.cmd; 20_run_nasdaq_shadow.cmd or 22_run_extended_nasdaq_shadow.cmd OPENING_R0 FOCUS_R1 --market-open-confirmed; or 21_stop_nasdaq_shadow.cmd.')
        return 2
    folder = None
    client = credential = password = None
    try:
        if argv[0] == 'stop':
            print(request_stop(), flush=True)
            return 0
        manifest_sha = verify_sources()
        bootstrap()
        from mb_market_data.sampling_acquisition import owner_lock
        (ROOT / 'runs').mkdir(exist_ok=True)
        with owner_lock(ROOT / 'runs/nasdaq_shadow_owner.lock'):
            offline = argv[0] == 'verify'
            preset = 'extended' if argv[0] == 'live-extended' else 'short'
            if offline:
                deny_network()
                (ROOT / 'runs/nasdaq_shadow_verification.json').unlink(missing_ok=True)
                print('OFFLINE NASDAQ SHADOW: response-handling checks and an elapsed HTML-interruption/recovery proof. No API or password.', flush=True)
                suite = unittest.TestSuite(unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=pattern)
                    for pattern in ('test_nasdaq_shadow.py', 'test_nasdaq_feed_capture.py'))
                if not unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite).wasSuccessful():
                    return 1
                roster = {'opening_uni': ['BASE'], 'opening_focus': ['BASE'], 'input_sha256': {}}
                catalog = {s: {'symbol': s, 'synthetic': True} for s in ('BASE', 'NEWX', 'OTHER')}
                bodies = receipts = None
                rejection = None
            else:
                if argv[3] != '--market-open-confirmed':
                    raise ValueError('Operator must explicitly confirm a normal regular session')
                check_verification(manifest_sha)
                check_live_window(utc_now(), preset)
                roster = load_roster(argv[1], argv[2])
                ecfg = resolve_ecfg()
                settings = policy('live', preset)
                print('NASDAQ SHADOW: ' + preset + '; up to 15 minutes for discovery; first successful feed is baseline only; LUDP/M only.', flush=True)
                if preset == 'extended':
                    print('Then up to 15 minutes of Hot; 30s bounded publication drain. Acquisition window at most 30m30s plus bounded startup/cleanup. No new admission after discovery closes.', flush=True)
                print('Nasdaq RSS: at least 60s after each completed attempt; maximum ' + str(settings['max_feed_attempts']) + ' attempts. Separate from Hot quote cadence.', flush=True)
                print('At most one admitted symbol; Hot every 2s for up to ' + str(settings['hot_tail_seconds']) + 's. Quote budget: up to 3 identity lookups + ' + str(settings['max_hot_calls']) + ' Hot = ' + str(settings['max_total_quote_calls']) + '.', flush=True)
                print('Fresh isolated hierarchy/journal. Production Hot remains disabled. Only experimental Hot requests quotes.', flush=True)
                print('Request remains valid to session close despite resumption; ambiguous/catalog-unresolved identities are recorded without admission.', flush=True)
                print('No new qualifying event means READINESS_ONLY. Existing token refresh can update the shared token database.', flush=True)
                catalog, bodies, receipts = acquire_catalog()
                print('Fresh exact-symbol directory acquired; supported identities=' + str(len(catalog)) + '. One password prompt follows.', flush=True)
                password = getpass.getpass('Encrypted config password: ')
                from mb_market_data.hot_provider_trial import prepare_schwab_client
                client, rejection = prepare_schwab_client(ecfg, password, SecureHorizon(preset))
                credential = {'ecfg': str(ecfg), 'password': password}
                password = None
                check_live_window(utc_now(), preset)
                print('Encrypted configuration and refresh horizon accepted.', flush=True)
            folder = ROOT / 'runs/nasdaq_shadow' / (utc_now().strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
            folder.parent.mkdir(parents=True, exist_ok=True)
            print('Output: ' + str(folder), flush=True)
            print('Progress every 10 seconds. Ctrl+C here or 21_stop_nasdaq_shadow.cmd in another window stops this shadow run only.', flush=True)
            report = run(folder, roster, catalog, source='synthetic' if offline else 'live', preset=preset, transport=client,
                rejection=rejection, credential=credential, catalog_bodies=bodies, catalog_receipts=receipts,
                on_status=lambda value: print(value, flush=True),
                synthetic_feed=offline_interrupted_feed if offline else None)
            credential = None
            write_json(folder / 'result.json', {'result': report['result'], 'milestone': report['milestone']})
            if offline and not (report['result'] == 'PASS_WITH_WARNINGS'
                    and report['milestone'] == 'SYNTHETIC_TRANSITION'
                    and report['feed_health']['status'] == 'RECOVERED_WITH_GAPS'
                    and report['feed_health']['failed_captures'] == 1):
                raise ValueError('Offline feed-interruption/recovery proof did not qualify')
            if (offline and report['result'] == 'PASS_WITH_WARNINGS' and report['milestone'] == 'SYNTHETIC_TRANSITION'
                    and report['feed_health']['status'] == 'RECOVERED_WITH_GAPS'
                    and report['feed_health']['failed_captures'] == 1):
                write_json(ROOT / 'runs/nasdaq_shadow_verification.json', {'result': 'PASS', 'report': str(folder / 'report.json'),
                    'proof_result': report['result'], 'feed_recovery_verified': True,
                    'report_sha256': sha((folder / 'report.json').read_bytes()), 'manifest_sha256': manifest_sha})
            print((folder / 'summary.txt').read_text(encoding='utf-8'), flush=True)
            print('Finished. This command returns to its prompt.', flush=True)
            return 0 if report['result'] in ('PASS', 'PASS_WITH_WARNINGS', 'READINESS_ONLY', 'STOPPED') else 1
    except KeyboardInterrupt:
        if folder is not None and folder.is_dir():
            write_json(folder / 'result.json', {'result': 'STOPPED', 'reason': 'operator_interrupt'})
        print('Shadow interrupted; its owned workers stopped. Retained evidence may contain explicit unresolved outcomes.', flush=True)
        return 130
    except Exception as exc:
        if folder is not None and folder.is_dir():
            write_json(folder / 'result.json', {'result': 'FAILED', 'error_type': type(exc).__name__})
        # Secure configuration and SDK error text must not reach console/report output.
        print('Nasdaq shadow FAILED (' + type(exc).__name__ + '). Inspect retained evidence; no automatic rerun.', flush=True)
        return 1
    finally:
        password = None
        if credential:
            credential.clear()
        if client is not None:
            client.close()


if __name__ == '__main__':
    raise SystemExit(main())
