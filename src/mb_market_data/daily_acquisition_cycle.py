"""Daily acquisition call path: one full validation per stage, no duplicate no-op reads.

Provider guards consume the exact immutable state verified immediately before
this synchronous dispatch. The original standalone experimental path is unchanged.
"""
import time
from .sampling_acquisition import owner_lock,aware,text,stamp,encode_result,fetch_quotes_batched


def sample_daily(self,slot,*,fault_hook=None):
    cycle_started = time.perf_counter()
    verification_before = self.verification_seconds
    with owner_lock(self.lock_path):
        s = self._read()
        if s['pending'] is not None:
            self._recover(s); s = self._read()
        if sum(e['kind']=='dispatch' and bool(e['symbols']) for e in s['events']) >= self.max_quote_calls:
            return {'status':'budget_exhausted'}
        if slot.watchlist_kind != self.settings.channel or slot.seconds != self.settings.seconds or not self.settings.window.contains(slot.scheduled_at):
            raise ValueError("Sample slot differs from this acquisition configuration")
        if slot.slot_id in s["finished"]:
            return {"status": "already_finished", "slot_id": slot.slot_id}
        now = aware(self.clock())
        if now < slot.scheduled_at:
            raise ValueError("Cannot acquire a future slot")
        gate1 = (now - slot.scheduled_at).total_seconds()
        state_prepare_seconds = time.perf_counter() - cycle_started
        if gate1 > self.settings.max_dispatch_lateness_seconds or now >= self.settings.window.end_at:
            reason = "window_ended" if now >= self.settings.window.end_at else "before_dispatch_deadline"
            self._append(s, "missed", slot_id=slot.slot_id, scheduled_at_utc=text(slot.scheduled_at), reason="late_dispatch_no_catchup", gate=reason, lateness_seconds=gate1)
            return {"status": "missed", "slot_id": slot.slot_id, "gate": reason, "gate_lateness_seconds": gate1, "state_prepare_seconds": state_prepare_seconds}
        revision = self.journal.latest_membership_revision_effective_at(slot.scheduled_at)
        if revision is None:
            raise ValueError("No hierarchy is effective at the scheduled slot")
        symbols = list(revision.symbols_for(self.settings.channel))
        p = {"slot_id": slot.slot_id, "scheduled_at_utc": text(slot.scheduled_at),
             "dispatched_at_utc": text(now), "revision": revision.revision,
             "membership_effective_at_utc": text(revision.effective_at),
             "hierarchy_sha256": revision.content_sha256, "symbols": symbols, "run_id": self.run.run_id}
        spool_started = time.perf_counter()
        self._append(s, "dispatch", **p)
        if fault_hook:
            fault_hook("after_dispatch")
        s = self._read()
        now = aware(self.clock())
        gate2 = (now - slot.scheduled_at).total_seconds()
        spool_and_check_seconds = time.perf_counter() - spool_started
        if gate2 > self.settings.max_dispatch_lateness_seconds or now >= self.settings.window.end_at:
            reason = "window_ended" if now >= self.settings.window.end_at else "before_api_deadline"
            self._append(s, "abandoned", slot_id=slot.slot_id, reason="dispatch_budget_passed_before_api", gate=reason, lateness_seconds=gate2)
            return {"status": "missed", "slot_id": slot.slot_id, "gate": reason, "gate_lateness_seconds": gate2, "state_prepare_seconds": state_prepare_seconds, "spool_and_check_seconds": spool_and_check_seconds}
        if symbols:
            started = time.perf_counter()
            self._provider_dispatch_state=s
            try:
                result = fetch_quotes_batched(self.client, symbols, fields=self.settings.fields, batch_size=self.settings.batch_size)
            finally:
                self._provider_dispatch_state=None
            if fault_hook:
                fault_hook("after_response")
            completed = aware(self.clock())
            encoded = encode_result(result)
            self._append(s, "assembled", slot_id=slot.slot_id, completed_at_utc=text(completed),
                         collector_seconds=time.perf_counter() - started, result=encoded)
            if fault_hook:
                fault_hook("after_assembled")
            s = self._read()
        outcome = self._commit(s, fault_hook=fault_hook)
        outcome["cycle_seconds"] = time.perf_counter() - cycle_started
        outcome["verification_seconds"] = self.verification_seconds - verification_before
        outcome.update(state_prepare_seconds=state_prepare_seconds, spool_and_check_seconds=spool_and_check_seconds,
            before_dispatch_gate_seconds=gate1, before_api_gate_seconds=gate2,
            recovery_before_slot_seconds=getattr(self, 'last_recovery_seconds', 0.0))
        outcome["dispatch_lateness_seconds"] = (stamp(p["dispatched_at_utc"]) - slot.scheduled_at).total_seconds()
        return outcome

