"""Offline contract tests and owned-process failure/restart drills. No API."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from mb_market_data.candle_session import CandleSessionReader
from mb_market_data.candle_supervision import health_view, instant, publish_json


def wait_for(function, timeout=10):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        try:
            value=function()
            if value:
                return value
        except (OSError,ValueError,KeyError):
            pass
        time.sleep(.1)
    raise AssertionError("Owned synthetic process did not reach the required state")


def digest_evidence(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and (p.name.endswith("receipt.json") or p.name.endswith("response.txt"))}


def drill(output):
    namespace=runpy.run_path(str(ROOT/"probes"/"supervise_live_candles.py"))
    reports=[]
    for scenario in ("api-error","hang","exit"):
        group=output/scenario
        group.mkdir()
        log=(group/"console.log").open("w",encoding="utf-8")
        child=subprocess.Popen([sys.executable,"-u",str(ROOT/"probes"/"supervise_live_candles.py"),
            "start","--synthetic-scenario",scenario,"--duration","3","--interval","0.2", "--output-root",str(group)],
            stdout=log,stderr=subprocess.STDOUT)
        session=None
        try:
            session=wait_for(lambda:next(group.glob("*/session_plan.json"),None)).parent
            warning=wait_for(lambda:(s if (s:=health_view(session)).get("state")=="WARNING" and s.get("open_incident") else None),
                             timeout=25 if scenario=="hang" else 10)
            assert warning["audible_alarm"] is False
            before=digest_evidence(session)
            heartbeat=warning["heartbeat_at_utc"]
            wait_for(lambda:(s if (s:=health_view(session)).get("heartbeat_at_utc")!=heartbeat and s.get("open_incident") else None))
            if scenario=="api-error":
                assert warning.get("producer_returncode") is not None or wait_for(lambda:health_view(session).get("producer_returncode") is not None)
                namespace["request"](session,"restart","healthy")
                healthy=wait_for(lambda:(s if (s:=health_view(session)).get("state")=="HEALTHY" else None))
                assert healthy["backfill_reviews_pending"]==1
                assert all(digest_evidence(session).get(name)==value for name,value in before.items())
                reader=CandleSessionReader(session)
                while not reader.caught_up:
                    reader.refresh()
                acquisition=instant(healthy["last_successful_acquisition_at_utc"])
                first=min(reader.availability_times)
                # Failed receipts have no candle. Recovery remains invisible at
                # the last failed acquisition, even for older market intervals.
                prior=reader.project(first)
                assert not prior.closed and not prior.pending
                restored=reader.project(acquisition)
                assert restored.closed or restored.pending
                fresh=CandleSessionReader(session)
                while not fresh.caught_up:
                    fresh.refresh()
                assert fresh.project(acquisition)==restored
                count=len(list(session.glob("run_*")))
                namespace["request"](session,"restart","healthy")
                wait_for(lambda:any(json.loads(p.read_text())["result"]=="REFUSED" for p in session.glob("command_receipt_*.json")))
                assert len(list(session.glob("run_*")))==count
            elif scenario=="exit":
                assert warning.get("producer_returncode") is not None
                # Kill only this drill's owned supervisor after its child exit.
                child.kill()
                child.wait(timeout=3)
                stale=wait_for(lambda:(s if "supervisor heartbeat is stale" in (s:=health_view(session)).get("message","") else None),timeout=13)
                assert stale["state"]=="WARNING"
                watch=wait_for(lambda:(v if (v:=json.loads((session/"watchdog_status.json").read_text()))["state"]=="WARNING" else None),timeout=3)
                assert watch["audible_alarm"] is False
            reports.append({"scenario":scenario,"result":"PASS","session":str(session),
                            "raw_evidence_preserved":True,"audible_alarm":False})
            print(f"Owned synthetic {scenario} drill: PASS",flush=True)
        finally:
            if session is not None:
                namespace["request"](session,"close")
            if child.poll() is None:
                try:
                    child.wait(timeout=16)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    child.wait(timeout=3)
            log.close()
            if session is not None:
                if scenario=="hang":
                    stop=json.loads((session/"run_0001"/"stop_receipt.json").read_text())
                    assert stop["forced_owned_child_cleanup"] is True
                # A fresh process can hold both released OS locks after close.
                wait_for(lambda:namespace["load"](session/"watchdog_status.json") if (session/"close-request.json").exists() else None)
                time.sleep(.6)
    return reports


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root",type=Path,default=ROOT/"output"/"candle_supervision_verification")
    parser.add_argument("--skip-process-drill",action="store_true")
    args=parser.parse_args()
    args.output_root.mkdir(parents=True,exist_ok=True)
    output=Path(tempfile.mkdtemp(prefix="offline_",dir=args.output_root))
    print("OFFLINE ONLY: synthetic candles and owned processes. No API, credentials, or production journal.",flush=True)
    suite=unittest.TestSuite()
    for name in ("test_candle_supervision","test_candle_receipts","test_live_overlay_dashboard"):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(name))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1
    reports=[] if args.skip_process_drill else drill(output)
    publish_json(output/"verification.json",{"result":"PASS","tests":result.testsRun,
        "skipped":len(result.skipped),"process_drills":reports,
        "production_journal_access":"NONE","API_requests":0,"audible_alarm":False})
    print(f"Candle supervision offline verification: PASS; evidence: {output}",flush=True)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
