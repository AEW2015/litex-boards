"""Portable host contracts, without LiteX, Vivado, GCC, Java or RapidWright.

Run with ``python -m unittest discover -s test -p test_fk33_portability.py -v``.
TemporaryDirectory follows the host's TMPDIR/TEMP selection. Run in separate
processes with different temp bases to exercise actual mounted filesystems.
Native subprocess tests use the current OS; they do not emulate another OS.
The fake compiler explicitly tests orchestration, not firmware correctness.
"""
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import struct
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOFTWARE = ROOT / 'litex_boards/software/fk33_hbm'


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = load_script('fk33_portable_build', SOFTWARE / 'rebuild/build.py')
FIRMWARE = load_script('fk33_portable_firmware', SOFTWARE / 'build.py')
RW = load_script('fk33_portable_rw', SOFTWARE / 'rebuild/rapidwright_assist.py')


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fk33 portability ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'space café Ω'
        self.base.mkdir()
        python_environment = mock.patch.dict(os.environ, {'FK33_TEST_PYTHON': sys.executable})
        python_environment.start()
        self.addCleanup(python_environment.stop)

    def wrapper(self, name, body):
        """Real native executable wrapper with arbitrary basename, not a tool."""
        script = self.base / (name + '.py')
        script.write_text(body, encoding='utf-8')
        if os.name == 'nt':
            executable = self.base / (name + '.cmd')
            # %~dp0 keeps non-ASCII directories out of CMD source decoding.
            executable.write_text('@echo off\r\n"%FK33_TEST_PYTHON%" "%~dp0' + script.name + '" %*\r\n', encoding='ascii')
        else:
            import shlex
            executable = self.base / name
            executable.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(script)) + ' "$@"\n', encoding='utf-8')
            executable.chmod(0o755)
        return executable

    def test_native_command_argv_cwd_unicode_and_optional_java(self):
        script = self.base / 'argument recorder.py'
        script.write_text("import json,os,sys;print(json.dumps({'args':sys.argv[1:],'cwd':os.getcwd(),'java':os.environ.get('JAVA_HOME')}))", encoding='utf-8')
        arguments = ['two words', 'café Ω', 'semi;dollar$bracket[]', 'quote"word', 'tail\\']
        for java in (None, str(self.base / 'optional java')):
            env = dict(os.environ)
            env.pop('JAVA_HOME', None)
            if java is not None:
                env['JAVA_HOME'] = java
            for runner in (BUILD.command, RW.run_process):
                with self.subTest(java=java, runner=runner.__module__):
                    log = self.base / 'child.log'
                    runner([sys.executable, script, *arguments], self.base, log, env=env)
                    result = json.loads(log.read_text(encoding='utf-8-sig'))
                    self.assertEqual(result['args'], arguments)
                    self.assertEqual(Path(result['cwd']).resolve(), self.base.resolve())
                    self.assertEqual(result['java'], java)

    def test_native_nonzero_exit_is_failure(self):
        with self.assertRaises(RuntimeError):
            BUILD.command([sys.executable, '-c', 'raise SystemExit(7)'], self.base, self.base / 'failure.log')
        result = RW.run_process([sys.executable, '-c', 'raise SystemExit(7)'], self.base, self.base / 'failure.log')
        self.assertEqual(result['returncode'], 7)

    def test_path_command_and_explicit_wrapper_from_other_cwd(self):
        wrapper = self.wrapper('arbitrary launch', 'print("wrapper executed")\n')
        other = self.base / 'child working directory'
        other.mkdir()
        env = dict(os.environ, PATH=str(self.base) + os.pathsep + os.environ.get('PATH', ''))
        self.assertEqual(RW.command_path(wrapper.name), wrapper.name)
        selected = shutil.which(wrapper.name, path=env['PATH'])
        self.assertIsNotNone(selected)
        relative = os.path.relpath(wrapper, Path.cwd())
        explicit = RW.command_path(relative)
        self.assertTrue(Path(explicit).is_absolute())
        for command in (selected, explicit):
            log = self.base / 'wrapper.log'
            BUILD.command([command], other, log, env=env)
            self.assertIn('wrapper executed', log.read_text(encoding='utf-8'))

    def test_best_effort_git_missing_executable_mock(self):
        # Explicit isolated failure injection, not evidence of another OS.
        with mock.patch.object(BUILD.subprocess, 'check_output', side_effect=FileNotFoundError('git')):
            self.assertIsNone(BUILD.git_commit(self.base))
        with mock.patch.object(BUILD.subprocess, 'check_output', side_effect=subprocess.CalledProcessError(128, ['git'])):
            self.assertIsNone(BUILD.git_commit(self.base))

    def test_missing_git_real_path_is_only_missing_provenance(self):
        with mock.patch.dict(os.environ, {'PATH': str(self.base)}):
            self.assertIsNone(BUILD.git_commit(self.base))

    def test_relative_path_entry_resolves_before_child_cwd(self):
        wrapper = self.wrapper('relative path launcher', 'print("relative PATH executed")\n')
        previous = Path.cwd()
        try:
            os.chdir(self.base.parent)
            relative_directory = self.base.name
            with mock.patch.dict(os.environ, {'PATH': relative_directory + os.pathsep + os.environ.get('PATH', '')}):
                selected = RW.command_path(wrapper.name)
                self.assertEqual(Path(selected).resolve(), wrapper.resolve())
                self.assertTrue(Path(selected).is_absolute())
                log = self.base / 'relative path.log'
                BUILD.command([selected], self.base, log)
                self.assertIn('relative PATH executed', log.read_text(encoding='utf-8'))
        finally:
            os.chdir(previous)

    def test_json_utf8_bom_newlines_and_content_hash(self):
        expected = {'path': 'café Ω/space directory', 'value': 9}
        path = self.base / 'input.json'
        for bom in (False, True):
            for newline in ('\n', '\r\n'):
                with self.subTest(bom=bom, newline=repr(newline)):
                    data = json.dumps(expected, ensure_ascii=False, indent=2).replace('\n', newline).encode('utf-8')
                    path.write_bytes((b'\xef\xbb\xbf' if bom else b'') + data)
                    self.assertEqual(BUILD.load(path), expected)
                    self.assertEqual(BUILD.sha(path), RW.sha(path))
                    BUILD.unchanged([BUILD.binding(path)])
        for writer in (BUILD.save, RW.write):
            writer(path, expected)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), expected)

    def test_binding_detects_changed_input(self):
        path = self.base / 'source.txt'
        path.write_bytes(b'original\n')
        row = BUILD.binding(path)
        path.write_bytes(b'changed\r\n')
        with self.assertRaises(ValueError):
            BUILD.unchanged([row])
        self.assertFalse(RW.unchanged([row]))

    def test_tcl_literal_roundtrip_native_interpreter(self):
        try:
            import tkinter
            interpreter = tkinter.Tcl()
        except (ImportError, RuntimeError) as exc:
            self.skipTest('Host tkinter/Tcl unavailable: ' + str(exc))
        path = self.base / 'literal $env(HOME) [exit] ; payload.dcp'
        literal = RW.tcl_path(path)
        interpreter.eval('set roundtrip ' + literal)
        self.assertEqual(interpreter.getvar('roundtrip'), path.resolve().as_posix())
        for bad in ('bad{path', 'bad}path', 'bad\npath', 'bad\rpath'):
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError):
                RW.tcl_path(self.base / bad)

    def test_symlink_source_binding_real_filesystem(self):
        source = self.base / 'source.txt'
        source.write_bytes(b'symlink source')
        link = self.base / 'linked source.txt'
        try:
            link.symlink_to(source)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('Filesystem/user cannot create symlinks: ' + str(exc))
        self.assertEqual(BUILD.binding(link)['path'], str(source.resolve()))
        self.assertEqual(BUILD.sha(link), BUILD.sha(source))

    def test_source_names_have_no_casefold_collisions(self):
        # Real tracked package layout, independent of host case sensitivity.
        try:
            tracked = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z', '--', 'litex_boards'], stderr=subprocess.DEVNULL)
            relatives = [name.decode('utf-8', errors='surrogateescape') for name in tracked.split(b'\0') if name]
            self.assertTrue(relatives)
            inventory = 'git tracked names'
        except (OSError, subprocess.CalledProcessError):
            relatives = [path.relative_to(ROOT).as_posix() for path in (ROOT / 'litex_boards').rglob('*') if '__pycache__' not in path.parts]
            inventory = 'physical scan fallback; Git unavailable, tracked aliases not verified'
        print(json.dumps({'case_collision_inventory': inventory}, ensure_ascii=True))
        names = {}
        for relative in relatives:
            folded = relative.casefold()
            self.assertNotIn(folded, names, (relative, names.get(folded)))
            names[folded] = relative

    def test_actual_filesystem_io_and_case_capability(self):
        source = self.base / 'binary caf\u00e9 \u03a9.bin'
        renamed = self.base / 'renamed caf\u00e9 \u03a9.bin'
        replacement = self.base / 'replacement caf\u00e9 \u03a9.bin'
        original = bytes(range(256)) + b'\x00\r\n\xff'
        source.write_bytes(original)
        source.rename(renamed)
        self.assertFalse(source.exists())
        self.assertEqual(renamed.read_bytes(), original)
        replacement.write_bytes(b'replacement\x00\xff')
        os.replace(replacement, renamed)
        self.assertFalse(replacement.exists())
        self.assertEqual(renamed.read_bytes(), b'replacement\x00\xff')
        case_source = self.base / 'CaseProbe'
        case_source.write_bytes(b'case lookup')
        alternate = self.base / 'caseprobe'
        case_sensitive = not alternate.exists()
        if not case_sensitive:
            self.assertTrue(case_source.samefile(alternate))
            self.assertEqual(alternate.read_bytes(), b'case lookup')
        print(json.dumps({
            'portability_host': {
                'python': platform.python_version(), 'os': platform.system(),
                'release': platform.release(), 'architecture': platform.machine(),
                'pointer_bits': struct.calcsize('P') * 8,
                'filesystem_encoding': sys.getfilesystemencoding(),
                'selected_temp_base': tempfile.gettempdir(),
                'actual_test_directory': str(self.base.resolve()),
                'temp_environment': {key: os.environ.get(key) for key in ('TMPDIR', 'TEMP', 'TMP')},
                'capabilities': {'case_sensitive_lookup': case_sensitive,
                    'unicode_binary_io': True, 'rename': True, 'replace_existing': True},
            }}, ensure_ascii=True, sort_keys=True))

    def test_firmware_arbitrary_wrapper_orchestration_not_real_compiler(self):
        """Actual subprocesses, fake tool outputs: exercises host plumbing only."""
        companion_body = '''import pathlib,sys
if '-O' in sys.argv: pathlib.Path(sys.argv[-1]).write_bytes(b'\\x13\\0\\0\\0')
else: print('fake disassembly')
'''
        companion = self.wrapper('unrelated companion', companion_body)
        compiler_body = '''import pathlib,sys
companion = COMPANION
if sys.argv[1].startswith('-print-prog-name='): print(companion)
elif sys.argv[1]=='--version': print('fake compiler for host orchestration')
else: pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_bytes(b'fake ELF')
'''.replace('COMPANION', repr(companion.name))
        compiler = self.wrapper('no gcc prefix at all', compiler_body)
        profiles = {'800': {'pll_words': [0xa402, 0x6300, 0x1212], 'refresh_period_cycles': 3120}, '900': {'pll_words': [0xa482, 0x9600, 0x1414], 'refresh_period_cycles': 3510}}
        profile = dict(reference_mhz=200, hbm_mhz=900, stacks=2, regions_per_stack=8, region_stride=0x20000, destructive_clock_profiles=profiles, selectors=[0]*4)
        for name, count in (('ms_pre',11),('pll',16),('ms_post',3),('phy',9),('mc',42)):
            profile[name] = [{'address': 0, 'value': 0} for _ in range(count)]
        for i, value in enumerate(profiles['900']['pll_words']):
            profile['pll'][i]['value'] = value
        for name in ('calibration_status_mask','calibration_status_expected','calibration_result_mask','calibration_result_expected'):
            profile[name] = 0
        path = self.base / 'profile.json'
        path.write_text('\ufeff' + json.dumps(profile), encoding='utf-8')
        with mock.patch.dict(os.environ, {'FK33_RISCV_GCC': str(compiler)}):
            result = FIRMWARE.build(self.base / 'firmware output', profile_path=path)
        self.assertEqual(result['rom_words'], [0x13])
        self.assertEqual(result['command'][0], str(compiler))


if __name__ == '__main__':
    unittest.main()
