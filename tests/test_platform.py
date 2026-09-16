"""跨进程真实文件锁、UTF-8 边界与可执行文件发现的测试；不调用模型。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import oil_claude_title as app
from claude_adapter import BackendError, find_claude

ID = '12345678-1234-1234-1234-123456789012'


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='title-test-')
        self.root = Path(self.tmp.name) / '中文 空格'
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def child(self, code, *args):
        return subprocess.Popen([sys.executable, '-u', '-c',
            'import sys; sys.path.insert(0, sys.argv[1]); ' + code,
            str(ROOT / 'scripts'), *map(str, args)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8')

    def test_lock_excludes_another_process_then_releases(self):
        code = ('from pathlib import Path; import oil_claude_title as t; '
                'lock=t.thread_lock(Path(sys.argv[2]), sys.argv[3]); '
                'print(int(lock.__enter__()), flush=True); lock.__exit__(None,None,None)')
        with app.thread_lock(self.root, ID):
            p = self.child(code, self.root, ID)
            output, errors = p.communicate(timeout=10)
            self.assertEqual((p.returncode, output.strip(), errors), (0, '0', ''))
        p = self.child(code, self.root, ID)
        output, errors = p.communicate(timeout=10)
        self.assertEqual((p.returncode, output.strip(), errors), (0, '1', ''))

    def test_killed_process_does_not_leave_stale_lock(self):
        p = self.child('from pathlib import Path; import oil_claude_title as t; '
            'lock=t.thread_lock(Path(sys.argv[2]),sys.argv[3]); '
            'print(int(lock.__enter__()),flush=True); sys.stdin.readline()', self.root, ID)
        try:
            self.assertEqual(p.stdout.readline().strip(), '1')
            with app.thread_lock(self.root, ID) as acquired:
                self.assertFalse(acquired)
        finally:
            p.terminate()
            p.communicate(timeout=10)
        with app.thread_lock(self.root, ID) as acquired:
            self.assertTrue(acquired)

    def test_windows_paths_cannot_leak_into_titles(self):
        for name in (r'🧩 C:\Users\example\app｜修复', r'🧩 \\server\private｜修复', '🧩 C:/Users/example/app｜修复'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                app.validate_candidate({'action':'rename','title':name,'reason':''}, '')

    def test_utf8_status_roundtrip_with_legacy_io_encoding(self):
        app.atomic_json(self.root / 'config.json', {'label':'中文 🧩｜标题'})
        env = os.environ | {'OIL_CLAUDE_TITLE_DATA':str(self.root), 'PYTHONIOENCODING':'ascii', 'PYTHONUTF8':'0'}
        p = subprocess.run([sys.executable,str(ROOT/'scripts/oil_claude_title.py'),'status'],
            capture_output=True, env=env, timeout=10)
        self.assertEqual(p.returncode,0,p.stderr.decode('utf-8'))
        self.assertEqual(json.loads(p.stdout.decode('utf-8'))['config']['label'],'中文 🧩｜标题')

    def test_manual_commands_need_a_session_id_outside_claude(self):
        env = os.environ | {'OIL_CLAUDE_TITLE_DATA': str(self.root)}
        env.pop('CLAUDE_CODE_SESSION_ID', None)
        p = subprocess.run([sys.executable, str(ROOT/'scripts/oil_claude_title.py'), 'rename'],
                           capture_output=True, encoding='utf-8', env=env, timeout=20)
        self.assertEqual(p.returncode, 1)
        self.assertIn('CLAUDE_CODE_SESSION_ID', json.loads(p.stdout)['message'])

    def test_host_execpath_is_preferred_then_path(self):
        fake = self.root / 'claude'
        fake.write_text('#!/bin/sh\n', encoding='utf-8')
        fake.chmod(0o755)
        with patch.dict(os.environ, {'CLAUDE_CODE_EXECPATH': str(fake)}):
            self.assertEqual(find_claude(), str(fake))
        with patch.dict(os.environ, {'CLAUDE_CODE_EXECPATH': str(self.root / 'missing')}), \
                patch('claude_adapter.shutil.which', return_value='/opt/bin/claude'):
            self.assertEqual(find_claude(), '/opt/bin/claude')
        with self.assertRaises(BackendError):
            find_claude(str(self.root / 'missing'))

    def test_windows_cmd_shim_is_rejected_with_guidance(self):
        with patch.dict(os.environ, {'CLAUDE_CODE_EXECPATH': ''}), patch('claude_adapter.sys.platform', 'win32'), \
                patch('claude_adapter.shutil.which', side_effect=lambda name: r'C:\npm\claude.cmd' if name == 'claude' else None):
            with self.assertRaisesRegex(BackendError, 'claude-bin'):
                find_claude()

    def test_fixture_evaluator_can_read_chinese_in_legacy_locale(self):
        env = os.environ | {'PYTHONUTF8':'0','PYTHONIOENCODING':'utf-8'}
        p = subprocess.run([sys.executable,str(ROOT/'scripts/evaluate_naming.py')],
            capture_output=True, encoding='utf-8', env=env, timeout=10)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertIn('30',p.stdout)


if __name__ == '__main__':
    unittest.main()
