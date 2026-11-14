"""Optional chart verification in an environment with Plotly, e.g. dashing."""
from pathlib import Path
from datetime import datetime,timezone
import json,os,sys,unittest,socket,getpass,builtins
from integration_source_integrity import verify_integrated_sources
ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
os.environ['PYTHONPATH']=os.pathsep.join(sys.path[:2])
def denied(*a,**k):raise AssertionError('No external network or credentials in chart test')
def main():
    try:import plotly
    except ImportError:
        print('Plotly is not installed here. Use the existing dashing environment; do not change the collector environment.')
        return 2
    source_check=verify_integrated_sources(ROOT)
    socket.socket.connect=socket.socket.connect_ex=socket.create_connection=socket.getaddrinfo=denied
    getpass.getpass=builtins.input=denied
    output=ROOT/'output'/('overlay-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'));output.mkdir(parents=True)
    os.environ['MB_OVERLAY_PREVIEW']=str(output/'synthetic-overlay-preview.html')
    suite=unittest.defaultTestLoader.loadTestsFromNames(['test_observation_overlay','test_observation_overlay_view','test_overlay_halts','test_daily_overlay_render'])
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    passed=result.wasSuccessful() and result.testsRun==41 and not result.skipped
    report={'result':'PASS' if passed else 'FAIL','tests_run':result.testsRun,'errors':len(result.errors),
        'failures':len(result.failures),'skipped':len(result.skipped),'plotly':plotly.__version__,
        'real_provider_calls':False,'synthetic_preview':str(output/'synthetic-overlay-preview.html'),
        'desktop_visual_verified':False,'environment':sys.executable,
        'source_inventory_verified':True,'source_file_count':source_check['file_count'],
        'source_crlf_normalized':source_check['crlf_normalized']}
    (output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2));return 0 if passed else 1
if __name__=='__main__':raise SystemExit(main())
