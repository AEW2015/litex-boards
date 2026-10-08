"""Select the three U250 branches and pinned Migen from sibling checkouts."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = Path(os.environ.get('U250_REPO_ROOT', ROOT.parents[2])).resolve()
REPOS = {}
for name in ('litedram', 'litex', 'litex-boards', 'migen'):
    override = os.environ.get('U250_'+name.upper().replace('-', '_')+'_REPO')
    candidates = [Path(override)] if override else [WORKSPACE/name, WORKSPACE/('u250-'+name)]
    if name == 'litex-boards' and not override:
        candidates.insert(0, ROOT.parents[1])
    path = next((item.resolve() for item in candidates if item.is_dir()), None)
    if path is None:
        raise RuntimeError(f'Missing {name} checkout; see README.txt or set U250_REPO_ROOT')
    REPOS[name] = path
for path in reversed(list(REPOS.values())):
    sys.path.insert(0, str(path))
vivado = os.environ.get('VIVADO_BIN')
if not vivado and os.name == 'nt' and Path('C:/Xilinx/2025.2/Vivado/bin').is_dir():
    vivado = 'C:/Xilinx/2025.2/Vivado/bin'
if vivado:
    os.environ['PATH'] = vivado + os.pathsep + os.environ['PATH']
(ROOT/'evidence').mkdir(parents=True, exist_ok=True)
(ROOT/'evidence/repository_paths.json').write_text(json.dumps(
    {name: str(path) for name, path in REPOS.items()}, indent=2))
