"""Experimental bounded-late scheduling; native collection and gates stay intact.

Only a running worker retains its next planned slot. Restart starts in the
future. Expired slots go through the native missed-slot path without an API
request; they remain missing in the independent journal coverage audit.
"""
import sqlite3
from datetime import datetime, timedelta

from mb_market_data.sampling_acquisition import AcquisitionBusy, aware, next_slot

POLICY = 'continuous-next-slot-existing-dispatch-budget-v1'


def slow_proof_qualified(report):
    """Keep deliberate gap warnings while qualifying complete synthetic coverage."""
    fixture = report.get('slow_fixture', {})
    hot = report.get('channels', {}).get('hot', {})
    start = report.get('logical_start')
    if not start:
        return False
    target = datetime.fromisoformat(start) + timedelta(seconds=22)
    late_slots = [datetime.fromisoformat(s.split(':',1)[1])
                  for s in hot.get('late_acquisitions', [])]
    assessment = report.get('assessment', {})
    return (report.get('source')=='synthetic_only'
        and report.get('duration_seconds')==60
        and report.get('result') in ('PASS','PASS_WITH_WARNINGS')
        and report.get('slot_selection_policy')==POLICY
        and fixture=={'enabled':True,'at_seconds':18,'call_seconds':3.818881,
                       'post_response_seconds':.35,'focus_call_seconds':1.461032}
        and target in late_slots
        and report.get('audit_passed') is True
        and report.get('replays_identical') is True
        and report.get('request_replay_identical') is True
        and report.get('publication_replay_identical') is True
        and assessment.get('all_observations_are_quotes') is True
        and report.get('combined_symbol_cadence',{}).get('outcome_coverage_complete') is True
        and not report.get('failures')
        and set(report.get('channels',{}))=={'uni','focus','hot'}
        and all(report['channels'][c].get('committed_slots')==n
                for c,n in (('uni',2),('focus',4),('hot',24)))
        and all(not c.get('missing_slots') and not c.get('missed_gate_events')
                for c in report.get('channels',{}).values()))


def run_bounded_late(poller, stop, *, on_status=None):
    def emit(value):
        if on_status is not None:
            on_status(value)

    # Do not replay history when starting/restarting this loop.
    slot = next_slot(poller.settings, after=aware(poller.clock()))
    notified = set()
    while slot is not None and not stop.is_set():
        try:
            recovered = poller.recover()
            if recovered:
                emit(recovered)
                if recovered.get('slot_id') == slot.slot_id:
                    slot = next_slot(poller.settings, after=slot.scheduled_at)
                    continue
            now = aware(poller.clock())
            already_due = now >= slot.scheduled_at
            if already_due and slot.slot_id not in notified:
                emit({'status': 'scheduler_overrun', 'slot_id': slot.slot_id,
                      'noticed_at_utc': now.isoformat(),
                      'lateness_seconds': (now-slot.scheduled_at).total_seconds(),
                      'dispatch_allowance_seconds': poller.settings.max_dispatch_lateness_seconds,
                      'slot_selection_policy': POLICY})
                notified.add(slot.slot_id)
            if stop.wait(max(0., (slot.scheduled_at-poller.clock()).total_seconds())):
                break
            if stop.is_set():
                break
            # The native gates run again before dispatch, after spooling and
            # at the quote client. No deadline is extended by this selector.
            result = poller.sample(slot)
            result = dict(result, selected_after_overrun=already_due,
                          slot_selection_policy=POLICY)
            emit(result)
            # Retain the next planned instant, rather than skipping elapsed
            # instants by searching after the work-completion clock.
            slot = next_slot(poller.settings, after=slot.scheduled_at)
        except (sqlite3.OperationalError, AcquisitionBusy) as exc:
            emit({'status': 'retrying', 'error_type': type(exc).__name__,
                  'error': str(exc), 'slot_id': slot.slot_id,
                  'slot_selection_policy': POLICY})
            stop.wait(poller.settings.retry_seconds)
    return {'status': 'stopped' if stop.is_set() else 'window_ended',
            'slot_selection_policy': POLICY}
