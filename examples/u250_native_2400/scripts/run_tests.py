"""Capture real test exit codes without PowerShell's stderr rendering."""
from bootstrap import ROOT
import hashlib
import json
import subprocess
import sys
import time

results = []
for name, log_name in [('test_x4_phy.py','x4_final.log'), ('test_dma_demo.py','dma_final.log')]:
    started = time.monotonic()
    with (ROOT/'evidence'/log_name).open('wb') as output:
        result = subprocess.run([sys.executable,str(ROOT/'scripts'/name)],
            cwd=ROOT,stdout=output,stderr=subprocess.STDOUT)
    results.append(dict(test=name,log='evidence/'+log_name,exit_code=result.returncode,
        elapsed_seconds=time.monotonic()-started,
        test_sha256=hashlib.sha256((ROOT/'scripts'/name).read_bytes()).hexdigest(),
        dma_sha256=hashlib.sha256((ROOT/'scripts/dma_demo.py').read_bytes()).hexdigest()))
    (ROOT/'evidence/test_exit_codes.json').write_text(json.dumps(results,indent=2))
    print(name, 'PASS' if result.returncode == 0 else 'FAIL',flush=True)
    if result.returncode:
        raise SystemExit(result.returncode)
