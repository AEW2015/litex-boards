from bootstrap import ROOT
import json
import re
from litex_boards.platforms.xilinx_alveo_u250 import Platform
from litedram.phy.usnative.pins import extract_ddr_pins
from litedram.phy.usnative.query import query_device
from litedram.phy.usnative.layout import native_layout

platform = Platform()
pads = platform.request('ddram', 0)
pin_map = extract_ddr_pins(platform, pads)
xdc = (ROOT/'references/u250_xdc/alveo-u250-xdc.xdc').read_text()
official = {}
for line in xdc.splitlines():
    if line.lstrip().startswith('#'):
        continue
    match = re.search(r'PACKAGE_PIN\s+(\w+)\s+IOSTANDARD\s+(\w+)\s*}\s*\[get_ports\s+([^\s]+)', line)
    if match:
        official[match[3].rstrip(';')] = (match[1], match[2])
rows = []
for pin in pin_map.pins:
    name = {'a':'adr', 'ras_n':'adr', 'cas_n':'adr', 'we_n':'adr',
        'clk_p':'ck_t', 'clk_n':'ck_c', 'dqs_p':'dqs_t', 'dqs_n':'dqs_c'}.get(pin.signal, pin.signal)
    index = {'ras_n':16, 'cas_n':15, 'we_n':14}.get(pin.signal, pin.index)
    scalar = pin.signal in ('act_n', 'reset_n')
    port = 'c0_ddr4_' + name + ('' if scalar else f'[{index}]')
    expected = official.get(port)
    if expected is None:
        # Closing bracket of scalar get_ports is not part of its name.
        expected = official.get(port + ']')
    if expected is None or expected[0] != pin.package_pin:
        raise ValueError(f'Official U250 XDC mismatch {port}: {expected}, {pin.package_pin}')
    rows.append(dict(signal=pin.signal,index=pin.index,pin=pin.package_pin,
        platform_standard=pin.iostandard,official_standard=expected[1]))
(ROOT/'evidence/pin_audit.json').write_text(json.dumps(rows,indent=2))
physical, auxiliary, directory = query_device(pin_map, ROOT/'build/native',
    cache_dir=ROOT/'query_cache')
layout = native_layout(physical, auxiliary, family=pin_map.family)
summary = dict(part=pin_map.part, banks=layout.banks, data_width=layout.databits,
    strobe_groups=len(layout.lanes), group_widths=[len(lane.dq) for lane in layout.lanes],
    controls=len(layout.controls), query_directory=str(directory))
(ROOT/'evidence/topology.json').write_text(json.dumps(summary,indent=2))
print(json.dumps(summary,indent=2))
