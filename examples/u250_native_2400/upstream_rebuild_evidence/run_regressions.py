"""Run converter/Native/init regressions against rebased sibling sources."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
modules = ['test.test_usnative_layout', 'test.test_usnative_mapping',
           'test.test_usnative_rx_trace', 'test.test_init', 'test.test_dfi',
           'test.test_ecp5ddrphy', 'test.test_dfi_full_rate', 'test.test_dfi_ddr3',
           'test.test_full_burst_dfirate']
env = dict(os.environ, PYTHONPATH=os.pathsep.join(str(ROOT/name) for name in
                                               ('litedram','litex','litex-boards','migen','liteeth')))
start = time.monotonic()
with (ROOT/'regressions.log').open('wb') as output:
    result = subprocess.run([sys.executable, '-m', 'unittest', *modules],
                            cwd=ROOT/'litedram', env=env, stdout=output, stderr=subprocess.STDOUT)
(ROOT/'regressions.json').write_text(json.dumps(dict(modules=modules,
    exit_code=result.returncode, elapsed_seconds=time.monotonic()-start), indent=2))
print('PASS' if result.returncode == 0 else 'FAIL: see regressions.log')
raise SystemExit(result.returncode)
