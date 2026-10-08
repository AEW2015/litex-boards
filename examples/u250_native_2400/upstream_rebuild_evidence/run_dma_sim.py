"""Record true exit status for DMA simulations against rebased sources."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
EXAMPLE = ROOT/'litex-boards/examples/u250_native_2400'
started = time.monotonic()
with (ROOT/'dma_simulation.log').open('wb') as output:
    result = subprocess.run([sys.executable, str(EXAMPLE/'scripts/test_dma_demo.py')],
                            cwd=EXAMPLE, stdout=output, stderr=subprocess.STDOUT)
(ROOT/'dma_simulation.json').write_text(json.dumps(dict(exit_code=result.returncode,
    elapsed_seconds=time.monotonic()-started, tests=4,
    dma_sha256=hashlib.sha256((EXAMPLE/'scripts/dma_demo.py').read_bytes()).hexdigest()), indent=2))
print('DMA PASS' if result.returncode == 0 else 'DMA FAIL', flush=True)
raise SystemExit(result.returncode)
