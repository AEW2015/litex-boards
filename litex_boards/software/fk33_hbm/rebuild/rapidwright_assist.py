"""Repeatable, read-only physical assistance after a completed FK33 build.

Vivado remains the setup/hold/DRC authority. This stage exports readable EDIF,
then uses Python RapidWright to relate routed fanout/locality to native paths.
It never writes a DCP, changes constraints, routes a design or accesses hardware.
"""
import os
import re
import sys
import json
import time
import shutil
import hashlib
import argparse
import traceback
import subprocess
import importlib.metadata

from pathlib import Path

# Configuration ------------------------------------------------------------------------------------

JAVA_HOME = os.environ.get('JAVA_HOME', '')
RW_PYTHON = Path(sys.executable)
VIVADO    = Path(os.environ.get('FK33_VIVADO', 'vivado'))
SCHEMA    = 'fk33-rapidwright-assist-v1'


# Helpers ------------------------------------------------------------------------------------------

def command_path(value):
    # Resolve found PATH entries too: PATH itself can contain relative folders.
    if '/' in value or '\\' in value:
        return str(Path(value).expanduser().resolve())
    found = shutil.which(value)
    return str(Path(found).resolve()) if found else value


def package_version(name):
    try:
        return importlib.metadata.version(name)
    except Exception:
        return None


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def bind(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': sha(path), 'bytes': path.stat().st_size}


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def unchanged(rows):
    return all(sha(row['path']) == row['sha256'] for row in rows)


def tcl_path(path):
    text = Path(path).resolve().as_posix()
    if any(character in text for character in '{}\n\r'):
        raise ValueError('Unsupported Tcl path characters')
    return '{' + text + '}'


def export_tcl(dcp, edf, timing, summary):
    return '\n'.join([
        'set_param general.maxThreads 2',
        'open_checkpoint ' + tcl_path(dcp),
        'write_edif -force ' + tcl_path(edf),
        'report_timing -delay_type max -max_paths 400 -nworst 1 -input_pins -file ' + tcl_path(timing),
        'report_timing_summary -file ' + tcl_path(summary),
        'puts FK33_RAPIDWRIGHT_EXPORT_COMPLETE', 'quit', ''])


def read_paths(path):
    if not path or not Path(path).is_file():
        return []
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    matches = re.finditer(r'Slack\s*\(([^)]+)\)\s*:\s*(-?[\d.]+)ns(.*?)(?=\nSlack\s*\(|\Z)', text, re.S)
    rows = []
    for match in matches:
        body = match[3]
        source = re.search(r'^\s*Source:\s*(\S+)', body, re.M)
        destination = re.search(r'^\s*Destination:\s*(\S+)', body, re.M)
        delay = re.search(r'Data Path Delay:\s*([\d.]+)ns\s*\(logic\s*([\d.]+)ns.*?route\s*([\d.]+)ns', body)
        if source and destination:
            rows.append({'slack_ns': float(match[2]), 'native_verdict': match[1],
                         'source_pin': source[1], 'destination_pin': destination[1],
                         'source_cell': source[1].rsplit('/', 1)[0],
                         'destination_cell': destination[1].rsplit('/', 1)[0],
                         'data_delay_ns': float(delay[1]) if delay else None,
                         'route_delay_ns': float(delay[3]) if delay else None})
    return rows


def family(name):
    lower = name.lower()
    if 'owner' in lower and 'hbm' in lower:
        return 'ownership'
    if ('fk33_hbm_bist' in lower or 'hbm_bist_pc' in lower) and any(
            word in lower for word in ('count[', 'beats[', 'stall[', 'seed[', 'strobe[', 'masked')):
        return 'per_port_configuration'
    return None


def channel(name):
    match = re.search(r'(?:hbm_bist_pc|fk33_hbm_bist_)(\d+)', name)
    if match:
        return int(match[1])
    return 0 if 'fk33_hbm_bist/' in name else None


def summarize(nets, paths, cells, physical_pin_inventory=None):
    matched_paths = []
    for path in paths:
        row = dict(path)
        row['source_placement'] = cells.get(path['source_cell'])
        row['destination_placement'] = cells.get(path['destination_cell'])
        row['matched_endpoint_cells'] = sum(row[key] is not None for key in ('source_placement', 'destination_placement'))
        source, destination = row['source_placement'], row['destination_placement']
        if source and destination and source.get('site_xy') and destination.get('site_xy'):
            row['site_coordinate_delta'] = [abs(a-b) for a, b in zip(source['site_xy'], destination['site_xy'])]
            row['coordinate_caution'] = 'Site X/Y are resource coordinates, not physical distance or delay.'
        matched_paths.append(row)
    selected = [row for row in nets if family(row['name'])]
    selected.sort(key=lambda row: row['site_pin_fanout'], reverse=True)
    counts = {name: sum(family(row['name']) == name for row in selected)
              for name in ('ownership', 'per_port_configuration')}
    failures = [row for row in matched_paths if row['slack_ns'] < 0]
    recommendations = []
    owner = [row for row in selected if family(row['name']) == 'ownership' and row['site_pin_fanout'] > 0]
    if owner and (physical_pin_inventory is None or physical_pin_inventory['available']):
        recommendations.append({'kind': 'ownership_fanout', 'evidence_nets': owner[:16],
            'action': 'Compare global ownership fanout with per-port owner copies and matched native failing paths before choosing another local register or placement change.',
            'automatic_constraint_change': False})
    if failures:
        recommendations.append({'kind': 'native_failed_path_locality', 'paths': failures[:40],
            'action': 'Use matched placement and native route/logic delay to review these exact failed endpoints. Rebuild and verify native setup, hold and DRC after any proposed change.',
            'automatic_constraint_change': False})
    return {'selected_owner_and_config_nets': selected, 'matched_net_counts': counts,
            'matched_configuration_channels': sorted({channel(row['name']) for row in selected if channel(row['name']) is not None}),
            'native_timing_paths': matched_paths, 'matched_timing_endpoint_cells': len(cells),
            'native_failed_paths_reported': len(failures), 'recommendations': recommendations,
            **({'physical_pin_inventory': physical_pin_inventory} if physical_pin_inventory is not None else {}),
            'constraint_candidates': [],
            'constraint_candidate_reason': 'No physical constraint is applied or emitted without a separate review of exact failed-path locality; fanout alone does not justify a pblock or LOC.'}


def placement(cell):
    if cell is None:
        return None
    site_inst = cell.getSiteInst()
    site = str(site_inst.getSiteName()) if site_inst else None
    match = re.search(r'_X(\d+)Y(\d+)$', site or '')
    return {'name': str(cell.getName()), 'type': str(cell.getType()), 'site': site,
            'site_xy': [int(match[1]), int(match[2])] if match else None,
            'bel': str(cell.getBELName()) if site_inst else None}


# RapidWright analysis -----------------------------------------------------------------------------

def worker(args):
    # This process must be started with the dedicated RapidWright Python/JVM.
    out = Path(args.output)
    dcp, edf = Path(args.dcp), Path(args.readable_edif)
    inputs = [bind(dcp), bind(edf), bind(__file__)]
    if args.timing_report and Path(args.timing_report).is_file():
        inputs.append(bind(args.timing_report))
    result = {'schema': SCHEMA, 'status': 'INCOMPLETE', 'input_bindings': inputs,
              'checkpoint_stage': args.checkpoint_stage,
              'environment': {'python': sys.version, 'python_executable': bind(sys.executable),
                              'JAVA_HOME': os.environ.get('JAVA_HOME'),
                              'JAVA_TOOL_OPTIONS': os.environ.get('JAVA_TOOL_OPTIONS')},
              'scope': 'Read-only Python RapidWright physical analysis. No DCP writes, placement/routing changes, hardware or timing signoff.'}
    try:
        import rapidwright
        import jpype
        import _jpype
        from com.xilinx.rapidwright.design import Design
        from java.lang import System
        jars = sorted(Path(rapidwright.__file__).parent.glob('*.jar'))
        result['libraries'] = {'rapidwright_version': package_version('rapidwright'),
            'jpype_version': package_version('jpype1'),
            'rapidwright_jars': [bind(jar) for jar in jars],
            'rapidwright_initializer': bind(rapidwright.__file__), 'jpype_initializer': bind(jpype.__file__),
            'jpype_native_extension': bind(_jpype.__file__)}
        result['environment']['java_version'] = str(System.getProperty('java.version'))
        java_home = System.getProperty('java.home')
        if java_home:
            result['environment']['java_home'] = str(java_home)
        inputs.extend(value for value in result['libraries'].values() if isinstance(value, dict))
        inputs.extend(result['libraries']['rapidwright_jars'])
        Design.setAutoGenerateReadableEdif(False)
        design = Design.readCheckpoint(str(dcp), str(edf))
        selected, high = [], []
        inventory = {'available': False, 'signal_nets': 0, 'nets_with_site_pins': 0,
                     'site_pin_instances': 0, 'selected_nets': 0,
                     'selected_nets_with_site_pins': 0, 'selected_site_pin_instances': 0}
        for net in design.getNets():
            if net.isClockNet() or str(net.getType()) != 'WIRE':
                continue
            name = str(net.getName())
            fanout = int(net.getFanOut())
            pin_count = int(net.getPins().size())
            inventory['signal_nets'] += 1
            inventory['nets_with_site_pins'] += int(pin_count > 0)
            inventory['site_pin_instances'] += pin_count
            if family(name):
                inventory['selected_nets'] += 1
                inventory['selected_nets_with_site_pins'] += int(pin_count > 0)
                inventory['selected_site_pin_instances'] += pin_count
            if not family(name) and fanout < 100:
                continue
            source = net.getSource()
            sites = sorted({str(pin.getSiteInst().getSiteName()) for pin in net.getPins() if pin.getSiteInst()})
            xy = [tuple(map(int, match.groups())) for site in sites
                  if (match := re.search(r'_X(\d+)Y(\d+)$', site))]
            row = {'name': name, 'site_pin_fanout': fanout, 'routing_pips': int(net.getPIPs().size()),
                   'site_pin_metrics_available': pin_count > 0,
                   'source_site': str(source.getSiteInst().getSiteName()) if source else None,
                   'distinct_pin_sites': len(sites),
                   'site_coordinate_bounds': [min(x for x,y in xy), min(y for x,y in xy), max(x for x,y in xy), max(y for x,y in xy)] if xy else None,
                   'site_coordinate_caution': 'Resource coordinates across site types; not physical distance or delay.'}
            if family(name):selected.append(row)
            if fanout >= 100:high.append(row)
        paths = read_paths(args.timing_report)
        if not paths:
            raise ValueError('No native timing paths parsed; physical assistance cannot be bound to path evidence')
        endpoint_names = {row[key] for row in paths for key in ('source_cell', 'destination_cell')}
        cells = {}
        for name in sorted(endpoint_names):
            info = placement(design.getCell(name))
            if info:cells[name] = info
        inventory['available'] = inventory['site_pin_instances'] > 0 and inventory['selected_site_pin_instances'] > 0
        inventory['caution'] = ('Physical SitePinInst inventory is present; site-pin fanout is distinct from logical cell-pin fanout.'
            if inventory['available'] else
            'Physical SitePinInst inventory is unavailable in this checkpoint. Observed zero fanout/PIP values do not mean zero logical fanout or establish routing locality. Only cell placement/native path correlation is usable.')
        result['physical_analysis'] = {'part': str(design.getPartName()),
            'physical_cells': int(design.getCells().size()), 'physical_nets': int(design.getNets().size()),
            'high_fanout_signal_nets': sorted(high,key=lambda row:row['site_pin_fanout'],reverse=True)[:40],
            **summarize(selected, paths, cells, inventory)}
        if not str(design.getPartName()).startswith('xcvu33p') or not selected or not cells:
            raise ValueError('FK33 part, selected physical nets or matched timing endpoint cells missing')
        if args.checkpoint_stage == 'routed' and not inventory['available']:
            raise ValueError('Routed assistance requires physical SitePinInst metrics; placement-only analysis must explicitly select --checkpoint-stage placed')
        result['status'] = 'PASS'
    except Exception:
        result.update(status='FAIL', error=traceback.format_exc())
    result['inputs_unchanged'] = unchanged(inputs)
    if not result['inputs_unchanged']:
        result['status'] = 'FAIL'
    write(out/'physical-analysis.json', result)
    print('FK33_RAPIDWRIGHT_ANALYSIS_' + result['status'], flush=True)
    return 0 if result['status'] == 'PASS' else 1


def run_process(command, cwd, log, env=None):
    with Path(log).open('wb') as handle:
        process = subprocess.run(
            command, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    return {
        'returncode': process.returncode,
        'command': list(map(str, command)),
        'stdout_stderr_log': bind(log),
    }


# Native export and analysis -----------------------------------------------------------------------

def assist(args):
    attempt = Path(args.attempt).resolve()
    out = Path(args.output).resolve() if args.output else attempt/'rapidwright-assist/runs'/str(time.time_ns())
    out.mkdir(parents=True, exist_ok=True)
    destination = out/'result.json'
    if destination.exists():
        raise ValueError('Existing assistance result preserved; choose a fresh --output directory')
    result = {'schema': SCHEMA, 'status': 'INCOMPLETE', 'attempt': str(attempt),
              'started': time.time(), 'launcher': bind(__file__), 'scope':
              'Repeatable post-implementation read-only assistance. Native Vivado setup/hold/pulse/DRC remains mandatory; RapidWright analysis is not timing qualification.'}
    bindings = []
    try:
        implementation = attempt/'implementation-result.json'
        dcp = attempt/'output/gateware/sqrl_fk33_route.dcp'
        if not implementation.is_file() or not dcp.is_file():
            raise FileNotFoundError('Completed implementation-result and routed checkpoint are required')
        completed = json.loads(implementation.read_text(encoding='utf-8-sig'))
        if completed.get('returncode') != 0 or completed.get('status') != 'IMPLEMENTED' or not completed.get('sources_unchanged'):
            raise ValueError('Implementation must be completed with unchanged captured sources')
        bindings = [bind(dcp), bind(implementation), bind(attempt/'input-hashes.json'), bind(__file__)]
        edf = Path(args.readable_edif).resolve() if args.readable_edif else out/'readable.edf'
        timing = Path(args.timing_report).resolve() if args.timing_report else out/'native-paths.rpt'
        if args.readable_edif:
            if not args.edif_receipt:
                raise ValueError('Reused EDIF requires its export receipt')
            receipt_path = Path(args.edif_receipt).resolve()
            receipt = json.loads(receipt_path.read_text(encoding='utf-8-sig'))
            if (receipt['returncode'] != 0 or not receipt['inputs_unchanged']
                    or receipt['output']['sha256'] != sha(edf)
                    or not any(row['sha256'] == sha(dcp) for row in receipt['inputs'])):
                raise ValueError('EDIF export receipt does not bind this checkpoint/output')
            bindings.extend([bind(edf), bind(receipt_path)])
            result['export'] = {'reused': True, 'receipt': bind(receipt_path)}
        else:
            query = out/'export-readable-edif.tcl'
            query.write_text(export_tcl(dcp, edf, timing, out/'native-summary.rpt'), encoding='utf-8')
            bindings.append(bind(query))
            export = run_process(
                [str(VIVADO), '-mode', 'batch', '-source', str(query),
                 '-log', 'export.log', '-journal', 'export.jou'],
                out, out/'export-console.log',
            )
            text = Path(export['stdout_stderr_log']['path']).read_text(encoding='utf-8', errors='replace')
            if (export['returncode'] or 'FK33_RAPIDWRIGHT_EXPORT_COMPLETE' not in text
                    or re.search(r'(?im)^\s*ERROR:', text)):
                raise ValueError('Native read-only EDIF/timing export failed')
            export.update(
                reused=False, query=bind(query), edif=bind(edf),
                timing=bind(timing), summary=bind(out/'native-summary.rpt'),
            )
            export_receipt = {'returncode': export['returncode'],
                'inputs': [bind(dcp), bind(query), bind(__file__)],
                'inputs_unchanged': unchanged(bindings), 'output': bind(edf),
                'scope': 'Native read-only checkpoint open, EDIF export and timing reports; no checkpoint writes.'}
            write(out/'edif-export-receipt.json', export_receipt)
            export['receipt'] = bind(out/'edif-export-receipt.json')
            result['export'] = export
            bindings.append(bind(edf))
        if timing.is_file():
            bindings.append(bind(timing))
        else:
            raise FileNotFoundError('Native timing report required for physical/path assistance')
        env = os.environ.copy()
        env.update(JAVA_TOOL_OPTIONS='-Xmx4g', PYTHONDONTWRITEBYTECODE='1')
        if JAVA_HOME:
            env['JAVA_HOME'] = JAVA_HOME
        command = [
            str(RW_PYTHON), '-B', str(Path(__file__).resolve()), '--worker',
            '--dcp', str(dcp), '--readable-edif', str(edf),
            '--timing-report', str(timing), '--output', str(out),
        ]
        process = run_process(command, out, out/'rapidwright-console.log', env)
        analysis = json.loads((out/'physical-analysis.json').read_text(encoding='utf-8-sig'))
        if process['returncode'] or analysis['status'] != 'PASS' or not analysis['inputs_unchanged']:
            raise ValueError('RapidWright analysis process failed')
        result.update(
            status='PASS', input_bindings=bindings, process=process,
            physical_analysis=bind(out/'physical-analysis.json'),
            timing_assistance=analysis['physical_analysis'],
            environment=analysis['environment'], libraries=analysis['libraries'],
        )
        result['native_timing_qualification'] = 'Required separately; this stage does not compute or certify setup/hold/pulse closure.'
    except FileNotFoundError:
        result.update(status='INCOMPLETE', error=traceback.format_exc())
    except Exception:
        result.update(status='FAIL', error=traceback.format_exc())
    result.update(finished=time.time(), inputs_unchanged=unchanged(bindings), input_bindings=bindings)
    if not result['inputs_unchanged']:
        result['status'] = 'FAIL'
    write(destination, result)
    print(json.dumps({
        'status': result['status'], 'attempt': str(attempt),
        'result': str(destination), 'error': result.get('error'),
    }), flush=True)
    return {'PASS': 0, 'FAIL': 1, 'INCOMPLETE': 2}[result['status']]


# Run ----------------------------------------------------------------------------------------------

def main():
    global JAVA_HOME, RW_PYTHON, VIVADO
    parser = argparse.ArgumentParser(description=__doc__)

    # Tools.
    parser.add_argument('--java-home', default=os.environ.get('JAVA_HOME'))
    parser.add_argument('--python',    default=sys.executable, help='Python with RapidWright installed')
    parser.add_argument('--vivado',    default=os.environ.get('FK33_VIVADO', 'vivado'))

    # Inputs and outputs.
    parser.add_argument('--attempt')
    parser.add_argument('--output')
    parser.add_argument('--readable-edif')
    parser.add_argument('--edif-receipt')
    parser.add_argument('--timing-report')

    # Analysis worker.
    parser.add_argument('--worker',           action='store_true')
    parser.add_argument('--dcp')
    parser.add_argument('--checkpoint-stage', choices=('routed', 'placed'), default='routed',
        help='Worker only: placed permits missing physical pin inventory and never qualifies routed assistance')
    args = parser.parse_args()
    JAVA_HOME = str(Path(args.java_home).expanduser().resolve()) if args.java_home else ''
    if JAVA_HOME:
        os.environ['JAVA_HOME'] = JAVA_HOME
    RW_PYTHON = command_path(args.python)
    VIVADO = command_path(args.vivado)
    if args.worker:
        if not all((args.output, args.dcp, args.readable_edif)):
            parser.error('worker requires output,dcp,readable-edif')
        return worker(args)
    if not args.attempt:
        parser.error('--attempt is required')
    return assist(args)


if __name__ == '__main__':
    raise SystemExit(main())
