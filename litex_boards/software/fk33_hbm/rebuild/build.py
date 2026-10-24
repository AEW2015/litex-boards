#!/usr/bin/env python3
"""Build the FK33 primitive HBM example and require native/RapidWright checks."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
BOARDS = HERE.parents[3]
CORE_COMMIT = '0308e0e709a9c703684e5e9f18c01f256207f9a2'


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def binding(path):
    path = Path(path).resolve()
    return dict(path=str(path), sha256=sha(path), size=path.stat().st_size)


def unchanged(rows):
    if not all(Path(row['path']).is_file() and sha(row['path']) == row['sha256'] for row in rows):
        raise ValueError('Build inputs changed; keep the failed attempt and start a new output directory')


def command(args, cwd, log, env=None):
    with Path(log).open('wb') as stream:
        result = subprocess.run(list(map(str, args)), cwd=cwd, env=env,
            stdout=stream, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError(f'Command exited {result.returncode}; inspect {log}')


def git_commit(path):
    try:
        return subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'],
            text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def contract_path(axi_domain=False, axi_clk_freq=250e6):
    if axi_clk_freq not in (250e6, 400e6) or (not axi_domain and axi_clk_freq != 250e6):
        raise ValueError('AXI400 requires the separate AXI domain; supported rates are 250/400 MHz')
    name = ('timing-contract-axi400.json' if axi_clk_freq == 400e6 else
            'timing-contract-axi-domain.json' if axi_domain else 'timing-contract.json')
    return HERE/name


def generate(attempt, core_commit=CORE_COMMIT, axi_domain=False, axi_clk_freq=250e6):
    contract_path(axi_domain, axi_clk_freq)
    # Execute the checked-out board target rather than an unrelated installed copy.
    sys.path.insert(0, str(BOARDS))
    import litex
    core = Path(litex.__file__).resolve().parents[1]
    core_head = git_commit(core)
    boards_commit = git_commit(BOARDS)
    args = ['sqrl_fk33', '--with-hbm', '--hbm-primitive', '--hbm-bist',
        '--sys-clk-freq', '200e6' if axi_clk_freq == 400e6 else '250e6', '--cpu-type', 'vexriscv', '--cpu-variant', 'minimal',
        '--build', '--no-compile-gateware', '--output-dir', str(attempt/'output'),
        '--vivado-max-threads', '8', '--vivado-place-directive', 'Explore',
        '--vivado-post-place-phys-opt-directive', 'AggressiveExplore',
        '--vivado-route-directive', 'Explore',
        '--vivado-post-route-phys-opt-directive', 'AggressiveExplore',
        '--vivado-report-level', 'signoff']
    if axi_domain:
        args.append('--hbm-axi-domain')
    if axi_clk_freq != 250e6:
        args += ['--hbm-axi-clk-freq', str(int(axi_clk_freq))]
    save(attempt/'generation-command.json', dict(python=sys.executable, args=args,
        core=str(core), core_commit=core_head, requested_core_commit=core_commit,
        boards=str(BOARDS), boards_commit=boards_commit, hbm_axi_domain=axi_domain,
        hbm_axi_clk_freq=int(axi_clk_freq), sys_clk_freq=200000000 if axi_clk_freq == 400e6 else 250000000,
        python_version=sys.version, packages={d.metadata['Name']:d.version for d in importlib.metadata.distributions()}))
    sys.argv = args
    runpy.run_module('litex_boards.targets.sqrl_fk33', run_name='__main__')
    # runpy removes the target's temporary __main__ module before this snapshot.
    paths = {Path(__file__).resolve(), BOARDS/'litex_boards/targets/sqrl_fk33.py'}
    for module in list(sys.modules.values()):
        name = getattr(module, '__file__', None)
        if name and name.endswith('.py') and Path(name).is_file():
            paths.add(Path(name).resolve())
    for directory in (core/'litex/soc/cores/ram/xilinx_usp_hbm2', HERE.parent, HERE):
        for path in directory.rglob('*'):
            if path.is_file() and path.suffix in {'.py', '.sv', '.v', '.json', '.c', '.h', '.S', '.ld', '.txt', '.tcl'}:
                paths.add(path.resolve())
    import pythondata_cpu_vexriscv
    paths.add(Path(pythondata_cpu_vexriscv.data_location)/'VexRiscv_Min.v')
    rows = []
    for index, path in enumerate(sorted(paths)):
        snapshot = attempt/'sources'/str(index)/path.name
        snapshot.parent.mkdir(parents=True)
        shutil.copyfile(path, snapshot)
        rows.append({**binding(path), 'snapshot':str(snapshot.relative_to(attempt))})
    save(attempt/'input-hashes.json', rows)
    save(attempt/'generated-hashes.json', [binding(p) for p in sorted((attempt/'output').rglob('*')) if p.is_file()])


def audit(attempt, stage, axi_domain=False, axi_clk_freq=250e6):
    from timing_audit import audit as run_audit
    options = {'stage': stage, 'contract_path': contract_path(axi_domain, axi_clk_freq)}
    result = run_audit(attempt, **options)
    save(attempt/f'timing-audit-{stage}.json', result)
    if result['status'] != 'PASS':
        raise ValueError(f'{stage} timing audit failed: inspect timing-audit-{stage}.json')
    return result


def implement(attempt, vivado):
    rows = load(attempt/'input-hashes.json')
    generated = load(attempt/'generated-hashes.json')
    unchanged(rows)
    unchanged(generated)
    gateware = attempt/'output/gateware'
    tcl = gateware/'sqrl_fk33.tcl'
    original = tcl.read_bytes()
    # Match the tested A12 resource settings. This changes no timing exceptions.
    prefix = (HERE/'implementation-prefix.tcl').read_bytes().replace(b'\r\n', b'\n')
    if hashlib.sha256(prefix).hexdigest() != 'cdd60aa1f441fbc0bdd916785b644a96f5142cc0d7fc005daf199a252da298d1':
        raise ValueError('Implementation prefix differs from the reviewed A12 recipe')
    tcl.write_bytes(prefix + original)
    save(attempt/'tool-resource-override.json', dict(
        original_generated_tcl_sha256=hashlib.sha256(original).hexdigest(),
        executed_tcl_sha256=sha(tcl), executed_prefix=prefix.decode()))
    result = dict(status='FAILED', returncode=1, sources_unchanged=False)
    try:
        command([vivado, '-mode', 'batch', '-source', tcl, '-log', attempt/'vivado.log',
                 '-journal', attempt/'vivado.jou'], gateware, attempt/'vivado-console.log')
        unchanged(rows)
        for row in generated:
            if Path(row['path']) != tcl:
                unchanged([row])
        if re.search(r'(?m)^\s*(?:ERROR:|CRITICAL WARNING:)', (attempt/'vivado-console.log').read_text(errors='replace')):
            raise ValueError('Vivado console contains an ERROR or unreviewed CRITICAL WARNING')
        result.update(status='IMPLEMENTED', returncode=0, sources_unchanged=True)
    finally:
        save(attempt/'implementation-result.json', result)


def inspect(attempt, vivado, axi_domain=False, axi_clk_freq=250e6):
    from netlist_inspection import query_tcl, parse_reset_chains, output_paths
    evidence = attempt/'inspection'
    evidence.mkdir(exist_ok=False)
    dcp = attempt/'output/gateware/sqrl_fk33_route.dcp'
    before = sha(dcp)
    query = evidence/'inspect.tcl'
    for path in (dcp, evidence):
        if any(c in path.as_posix() for c in '{}\n\r'):
            raise ValueError('Unsupported Tcl path characters')
    inspection_tcl = query_tcl(dcp, evidence, axi_domain=axi_domain)
    if axi_domain:
        import gray_cdc
        inspection_tcl = inspection_tcl.replace('puts HBM_NETLIST_INSPECTION_COMPLETE',
            gray_cdc.query_tcl(evidence, axi_clk_freq=axi_clk_freq) + '\nputs HBM_NETLIST_INSPECTION_COMPLETE')
    query.write_text(inspection_tcl, encoding='utf-8')
    command([vivado, '-mode', 'batch', '-source', query, '-log', evidence/'vivado.log',
        '-journal', evidence/'vivado.jou'], evidence, evidence/'netlist-inspection-console.log')
    text = (evidence/'netlist-inspection-console.log').read_text(errors='replace')
    if 'HBM_NETLIST_INSPECTION_COMPLETE' not in text or re.search(r'(?m)^\s*(?:ERROR:|CRITICAL WARNING:)', text):
        raise ValueError('Native netlist inspection did not complete cleanly')
    chains = parse_reset_chains(evidence/'implemented-reset-chains.txt', axi_domain=axi_domain)
    properties = (evidence/'implemented-hbm-properties.txt').read_text()
    rates = re.findall(r'^DATARATE\s*=\s*(\d+)\s*$', properties, re.M)
    all_rates = re.findall(r'^DATARATE(?:_\d+)?\s*=\s*(\d+)\s*$', properties, re.M)
    if len(rates) != 32 or set(all_rates) != {'1800'}:
        raise ValueError(f'Expected 32 native HBM AXI interfaces at DATARATE=1800; found {rates}')
    if sha(dcp) != before:
        raise ValueError('Read-only inspection changed the checkpoint')
    result = dict(status='PASS', checkpoint=binding(dcp), reset_chains=chains,
        outputs=[binding(p) for p in output_paths(evidence, axi_domain=axi_domain)], datarate_mt_s=1800)
    if axi_domain:
        result['gray_cdc'] = gray_cdc.parse(evidence, axi_clk_freq=axi_clk_freq)
        result['outputs'] += [binding(p) for p in gray_cdc.output_paths(evidence)]
    save(evidence/'result.json', result)
    return result


def diagnostics(attempt):
    g = attempt/'output/gateware'
    drc = (g/'sqrl_fk33_drc.rpt').read_text()
    if not re.search(r'Checks found:\s*0\b', drc):
        raise ValueError('DRC is not clean; inspect sqrl_fk33_drc.rpt')
    route = (g/'sqrl_fk33_route_status.rpt').read_text()
    counts = {}
    for key in ('routable nets', 'fully routed nets', 'nets with routing errors'):
        match = re.search(r'# of '+key+r'\.+\s*:\s*(\d+)', route)
        if not match:
            raise ValueError('Missing route status counter: '+key)
        counts[key] = int(match[1])
    if counts['routable nets'] <= 0 or counts['fully routed nets'] != counts['routable nets'] or counts['nets with routing errors']:
        raise ValueError('Routing is incomplete or has errors')
    cdc = (g/'sqrl_fk33_cdc.rpt').read_text()
    findings = [dict(rule=m[1], severity=m[2], count=int(m[3])) for m in
        re.finditer(r'(?m)^(CDC-\d+)\s+(Critical|Warning|Info)\s+(\d+)\s+', cdc)]
    if not findings:
        raise ValueError('Missing CDC inventory')
    return dict(status='PASS', routing=counts, cdc_findings=findings,
        cdc_clean=not any(r['severity']=='Critical' for r in findings))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path, help='Fresh directory; failed attempts are never overwritten')
    parser.add_argument('--vivado', default=os.environ.get('FK33_VIVADO', 'vivado'))
    parser.add_argument('--rapidwright-python', default=os.environ.get('FK33_RAPIDWRIGHT_PYTHON', sys.executable))
    parser.add_argument('--java-home', default=os.environ.get('JAVA_HOME'))
    parser.add_argument('--core-commit', default=CORE_COMMIT,
        help='Reference LiteX commit metadata; not enforced (default: the reviewed baseline)')
    parser.add_argument('--hbm-axi-domain', action='store_true',
        help='Build the separate AXI clock domain and use its timing/reset contracts')
    parser.add_argument('--hbm-axi-clk-freq', type=float, choices=(250e6, 400e6), default=250e6,
        help='AXI clock frequency; 400 MHz requires --hbm-axi-domain (default: 250 MHz)')
    parser.add_argument('--generate-only', action='store_true', help='Generate RTL/firmware and audit source constraints; no CAD or build PASS')
    parser.add_argument('--resume', action='store_true', help='Implement an unchanged GENERATED output; never retry a failed native build')
    args = parser.parse_args()
    if args.hbm_axi_clk_freq != 250e6 and not args.hbm_axi_domain:
        parser.error('--hbm-axi-clk-freq=400e6 requires --hbm-axi-domain')
    attempt = args.output_dir.resolve()
    result = dict(status='FAIL', attempt=str(attempt), started_utc=datetime.now(timezone.utc).isoformat(),
        hardware_tested=False, published=False,
        hbm_axi_domain=args.hbm_axi_domain, hbm_axi_clk_freq=int(args.hbm_axi_clk_freq),
        sys_clk_freq=200000000 if args.hbm_axi_clk_freq == 400e6 else 250000000,
        requested_core_commit=args.core_commit,
        scope='Portable build/timing/netlist/RapidWright checks. Local historical simulations and hardware campaign are separate evidence.')
    if args.generate_only and args.resume:
        parser.error('--generate-only and --resume are mutually exclusive')
    if args.resume:
        if load(attempt/'result.json')['status'] != 'GENERATED':
            parser.error('--resume accepts only an unchanged GENERATED attempt')
        try:
            generation = load(attempt/'generation-command.json')
        except (OSError, ValueError) as error:
            parser.error(f'--resume requires a readable generation-command.json: {error}')
        recorded_axi_domain = generation.get('hbm_axi_domain', False)
        if recorded_axi_domain != args.hbm_axi_domain:
            parser.error('--resume HBM AXI-domain profile does not match generation-command.json')
        if generation.get('hbm_axi_clk_freq', 250000000) != args.hbm_axi_clk_freq:
            parser.error('--resume AXI frequency does not match generation-command.json')
        if generation.get('sys_clk_freq', 250000000) != result['sys_clk_freq']:
            parser.error('--resume SYS profile does not match generation-command.json')
    else:
        attempt.mkdir(parents=True, exist_ok=False)
    try:
        if not args.generate_only:
            vivado = shutil.which(args.vivado) or args.vivado
            rw_python = shutil.which(args.rapidwright_python) or args.rapidwright_python
            # Preserve explicit relative paths across the tool working directories;
            # bare command names are resolved by the operating system through PATH.
            if '/' in vivado or '\\' in vivado:
                vivado = str(Path(vivado).resolve())
            if '/' in rw_python or '\\' in rw_python:
                rw_python = str(Path(rw_python).resolve())
            if args.java_home:
                args.java_home = str(Path(args.java_home).resolve())
            result.update(vivado=vivado, rapidwright_python=rw_python, java_home=args.java_home)
        if not args.resume:
            child_args = [sys.executable, '-B', __file__, '--generate-child', attempt,
                '--core-commit', args.core_commit]
            if args.hbm_axi_domain:
                child_args.append('--hbm-axi-domain')
            if args.hbm_axi_clk_freq != 250e6:
                child_args += ['--hbm-axi-clk-freq', str(int(args.hbm_axi_clk_freq))]
            command(child_args, BOARDS, attempt/'generation.log')
        audit(attempt, 'source', axi_domain=args.hbm_axi_domain, axi_clk_freq=args.hbm_axi_clk_freq)
        if args.generate_only:
            result['status'] = 'GENERATED'
        else:
            implement(attempt, vivado)
            # Preserve useful physical analysis even when final STA fails.
            rapidwright_args = [sys.executable, '-B', HERE/'rapidwright_assist.py', '--attempt', attempt,
                '--python', rw_python, '--vivado', vivado, '--output', attempt/'rapidwright']
            if args.java_home:
                rapidwright_args += ['--java-home', args.java_home]
            command(rapidwright_args, attempt, attempt/'rapidwright-console.log')
            assistance = load(attempt/'rapidwright/result.json')
            physical = assistance.get('timing_assistance', {})
            if (assistance.get('status') != 'PASS' or not assistance.get('inputs_unchanged')
                    or not physical.get('physical_pin_inventory', {}).get('available')
                    or physical.get('matched_configuration_channels') != list(range(32))
                    or len(physical.get('native_timing_paths', [])) != 400):
                raise ValueError('RapidWright receipt lacks required routed pin/channel/path evidence')
            result['rapidwright'] = binding(attempt/'rapidwright/result.json')
            result['timing'] = audit(attempt, 'final', axi_domain=args.hbm_axi_domain,
                axi_clk_freq=args.hbm_axi_clk_freq)['checks']['routed_sta']['detail']
            result['diagnostics'] = diagnostics(attempt)
            result['inspection'] = inspect(attempt, vivado, axi_domain=args.hbm_axi_domain,
                axi_clk_freq=args.hbm_axi_clk_freq)
            unchanged(load(attempt/'input-hashes.json'))
            result.update(status='BUILD_PASS', bitstream=binding(attempt/'output/gateware/sqrl_fk33.bit'))
            result['artifacts'] = [binding(attempt/path) for path in (
                'input-hashes.json', 'generated-hashes.json', 'generation-command.json',
                'output/gateware/sqrl_fk33.v', 'output/gateware/sqrl_fk33.xdc',
                'output/gateware/sqrl_fk33.tcl', 'output/gateware/sqrl_fk33_route.dcp',
                'output/software/fk33_hbm/fk33_hbm.elf', 'output/software/fk33_hbm/fk33_hbm.bin',
                'output/software/fk33_hbm/firmware-build.json',
                'timing-audit-source.json', 'timing-audit-final.json')]
    except Exception as error:
        result['error'] = repr(error)
    result['finished_utc'] = datetime.now(timezone.utc).isoformat()
    save(attempt/'result.json', result)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] in ('GENERATED', 'BUILD_PASS') else 1


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == '--generate-child':
        child = argparse.ArgumentParser(description='Internal RTL generation worker')
        child.add_argument('--generate-child', required=True, type=Path)
        child.add_argument('--core-commit', default=CORE_COMMIT)
        child.add_argument('--hbm-axi-domain', action='store_true')
        child.add_argument('--hbm-axi-clk-freq', type=float, choices=(250e6, 400e6), default=250e6)
        child_args = child.parse_args()
        generate(child_args.generate_child.resolve(), child_args.core_commit,
            child_args.hbm_axi_domain, child_args.hbm_axi_clk_freq)
    else:
        raise SystemExit(main())
