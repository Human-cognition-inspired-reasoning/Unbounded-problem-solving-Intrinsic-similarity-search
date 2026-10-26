"""Factory imports and deployed CLI entry points retain their operational boundaries."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from api.application import create_app
from runtime.config import BASE_DIR, configure_attachments, load_settings

SERVER = Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.database = self.root / 'separate-db' / 'study.db'
        self.env = {
            **os.environ,
            'DATA_DIR': str(self.data), 'DATABASE_PATH': str(self.database),
            'FLASK_SECRET_KEY': 'runtime-test-secret', 'SEED_DEMO_ACCOUNT': 'true',
            'SESSION_COOKIE_SECURE': 'false', 'ATTACHMENT_STORAGE': 'local',
            'DEFAULT_CREDIT_LIMIT_USD': '12.5',
            'OPENAI_API_KEY': 'test-key', 'ANTHROPIC_API_KEY': 'test-key',
            'GOOGLE_API_KEY': 'test-key',
        }

    def run_python(self, *args, cwd=None):
        result = subprocess.run(
            [sys.executable, *args], cwd=cwd or self.root, env=self.env,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_library_imports_do_not_load_settings_or_create_a_database(self):
        self.run_python('-c', '''
import sys
from unittest.mock import patch
with patch('dotenv.load_dotenv', side_effect=AssertionError('import loaded .env')), \
     patch('sqlite3.connect', side_effect=AssertionError('import opened a database')):
    import ops.accounts
    import ops.attachments
    assert 'flask' not in sys.modules
    import api.application
    assert 'app' not in sys.modules
''', cwd=SERVER)
        self.assertFalse(self.database.exists())
        self.assertFalse(self.data.exists())

    def test_explicit_settings_keep_environment_and_override_precedence(self):
        with patch.dict(os.environ, self.env, clear=True), patch('runtime.config.load_dotenv') as dotenv:
            settings = configure_attachments(load_settings({'DEFAULT_CREDIT_LIMIT_USD': 7.0}))
        dotenv.assert_called_once_with(BASE_DIR / '.env')
        self.assertEqual(BASE_DIR, SERVER)
        self.assertEqual(settings['DATA_DIR'], str(self.data))
        self.assertEqual(settings['DATABASE_PATH'], str(self.database))
        self.assertEqual(settings['DEFAULT_CREDIT_LIMIT_USD'], 7.0)
        self.assertEqual(settings['SECRET_KEY'], 'runtime-test-secret')
        self.assertTrue(settings['SEED_DEMO_ACCOUNT'])
        self.assertFalse(settings['SESSION_COOKIE_SECURE'])
        self.assertEqual(settings['ATTACHMENT_STORAGE'], 'local')
        self.assertFalse(self.database.exists())

    def test_cli_help_does_not_create_data_and_existing_commands_share_the_database(self):
        scripts = ['manage_accounts.py', 'manage_attachments.py']
        if (SERVER / 'migrate_to_sqlite.py').exists():
            scripts.append('migrate_to_sqlite.py')
        for script in scripts:
            with self.subTest(script=script):
                self.assertIn('usage:', self.run_python(str(SERVER / script), '--help'))
        self.assertFalse(self.database.exists())
        self.assertFalse(self.data.exists())

        # Even with demo mode configured, maintenance never seeds a web demo account.
        self.assertIn('Eligible: 0', self.run_python(str(SERVER / 'manage_attachments.py'), 'prune'))
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 0)
        self.run_python(
            str(SERVER / 'manage_accounts.py'), 'add', 'P-CLI',
            '--password', 'test-cli-password', '--credit-limit', '3.5',
        )
        listing = self.run_python(str(SERVER / 'manage_accounts.py'), 'list')
        self.assertIn('P-CLI', listing)
        self.assertIn('3.50', listing)
        self.assertNotIn('P-001', listing)
        self.assertFalse((self.data / 'study.db').exists())
        with patch.dict(os.environ, {**self.env, 'SEED_DEMO_ACCOUNT': 'false'}, clear=True):
            app = create_app({'TESTING': True})
        self.addCleanup(app.extensions['participant_store'].close)
        client = app.test_client()
        self.assertEqual(client.post('/api/auth/login', json={
            'studyId': 'P-CLI', 'password': 'test-cli-password',
        }).status_code, 200)
        self.assertEqual(client.get('/api/usage').get_json()['limitUsd'], 3.5)

    def test_wsgi_entry_point_and_factory_use_the_same_routes(self):
        output = self.run_python('-c', '''
import json
from app import app
client = app.test_client()
assert client.get('/api/health').get_json() == {'ok': True}
assert client.post('/api/auth/login', json={'studyId':'P-001','password':'prototype'}).status_code == 200
print(json.dumps(sorted((r.rule, sorted(r.methods)) for r in app.url_map.iter_rules())))
''', cwd=SERVER)
        with patch.dict(os.environ, self.env, clear=True):
            app = create_app({'TESTING': True})
        self.addCleanup(app.extensions['participant_store'].close)
        routes = sorted([r.rule, sorted(r.methods)] for r in app.url_map.iter_rules())
        self.assertEqual(json.loads(output), routes)
        self.assertFalse((self.data / 'study.db').exists())
