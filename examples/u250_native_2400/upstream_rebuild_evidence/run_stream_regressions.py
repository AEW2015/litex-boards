"""Verify upstream changes used by Native DMA buffering and management streams."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
env = dict(os.environ, PYTHONPATH=os.pathsep.join(str(ROOT/name) for name in
    ('litedram','litex','litex-boards','migen','liteeth')))
requested = set(sys.argv[1:])
results = json.loads((ROOT/'stream_regressions.json').read_text()) if requested else []
for repo, label, modules in [
    ('litedram','dma_byte_enable',['test.test_dma_byte_enable']),
    ('litex','streams',['test.interconnect.test_stream','test.interconnect.test_stream_pipeline'])]:
    if requested and label not in requested:
        continue
    start = time.monotonic()
    with (ROOT/(label+'.log')).open('wb') as output:
        result = subprocess.run([sys.executable, str(ROOT/'simulate_write_only_compat.py'), *modules],
                                cwd=ROOT/repo, env=env, stdout=output, stderr=subprocess.STDOUT)
    results = [item for item in results if item['log'] != label+'.log']
    results.append(dict(repo=repo, modules=modules, exit_code=result.returncode,
                        elapsed_seconds=time.monotonic()-start, log=label+'.log'))
    (ROOT/'stream_regressions.json').write_text(json.dumps(results, indent=2))
    print(label, 'PASS' if result.returncode == 0 else 'FAIL', flush=True)
    if result.returncode:
        raise SystemExit(result.returncode)
