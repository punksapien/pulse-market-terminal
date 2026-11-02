"""ADR-0199 offline regression/fault harness. No network, credentials or Redis connection."""
import io,json,subprocess,sys,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]

PYTHON=('test_reliability_races','test_recovery_structures','test_c0_review_regressions',
    'test_artifact_publisher','test_live_runtime','test_all_pair_collect','test_all_pair_hosted_recovery',
    'test_ls_repair_limit','test_metric_capsule','test_public_replay_lanes')
NODE=('test_retired_frames_cleanup.cjs','test_pulse_missing_state.js','test_pulse_wire_loader.js',
    'test_pulse_loader.js','test_pulse_ticks.js','test_pulse_live_sort.js')

def main():
    started=time.perf_counter();output=io.StringIO()
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in PYTHON)
    result=unittest.TextTestRunner(stream=output,verbosity=0).run(suite)
    nodes=[]
    for name in NODE:
        run=subprocess.run(['node',str(ROOT/'tests'/name)],cwd=ROOT,capture_output=True,text=True,timeout=60)
        nodes.append({'suite':name,'passed':run.returncode==0})
        if run.returncode:print(run.stdout+run.stderr,file=sys.stderr)
    if not result.wasSuccessful():print(output.getvalue(),file=sys.stderr)
    receipt={'status':'pass' if result.wasSuccessful() and all(r['passed'] for r in nodes) else 'failed',
        'python_tests':result.testsRun,'node':nodes,'seconds':round(time.perf_counter()-started,3),
        'retention_model_interleavings':3000,'repair_ack_interleavings':100,
        'exchange_calls':0,'redis_connections':0,'scope':'offline_fault_models_and_regressions',
        'not_checked':['Redis Lua interleaving parity','provider latency','hosted soak','production rollout']}
    print(json.dumps(receipt));return 0 if receipt['status']=='pass' else 1

if __name__=='__main__':raise SystemExit(main())
