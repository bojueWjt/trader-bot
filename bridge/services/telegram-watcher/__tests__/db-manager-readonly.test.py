"""Run with system python3; no third-party dependencies or real DB/config."""
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/crypto-trader/scripts/db_manager.py'
spec = importlib.util.spec_from_file_location('watcher_legacy_db', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReadOnlyCommands(unittest.TestCase):
    def test_configuration_writes_fail_without_opening_or_creating_database(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'must-not-exist.db'
            for command in [
                ['init-db'],
                ['add-account', 'fixture', 'fake-key', 'fake-secret'],
                ['set-channel', '-100', 'fixture'],
                ['set-risk', 'BTCUSDT', '0.02'],
            ]:
                result = subprocess.run([sys.executable, str(SCRIPT), '--db', str(missing), *command], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('watcher site or app', result.stderr)
                self.assertNotIn('fake-key', result.stderr)
                self.assertNotIn('fake-secret', result.stderr)
                self.assertFalse(missing.exists())
            manager = module.DatabaseManager(str(missing))
            for method in [manager.init_db, manager.add_account, manager.set_channel, manager.set_risk, manager._ensure_schema]:
                with self.assertRaisesRegex(RuntimeError, 'watcher site or app'):
                    method()
            self.assertFalse(missing.exists())

    def test_read_commands_preserve_schema_data_and_file(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'fixture.db'
            with sqlite3.connect(file) as db:
                db.executescript(module.SCHEMA_SQL)
                db.execute("INSERT INTO account_configs(account_id,api_key,api_secret,risk_capital_multiplier,is_enabled) VALUES ('fixture','fake','fake',1,1)")
                db.execute("INSERT INTO channel_routing(channel_id,target_account_id) VALUES ('-100','fixture')")
                db.execute("INSERT INTO symbol_risk_configs(symbol,risk_ratio) VALUES ('BTCUSDT',0.02)")
            before = hashlib.sha256(file.read_bytes()).hexdigest()
            for command in [['list-accounts'], ['list-channels'], ['list-risks'], ['get-risk','BTCUSDT'], ['list-orders'], ['list-briefings'], ['list-signal-ops']]:
                result = subprocess.run([sys.executable,str(SCRIPT),'--db',str(file),*command],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                json.loads(result.stdout)
                self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(),before)
            self.assertFalse(Path(str(file) + '-wal').exists())

    def test_signal_and_order_writes_still_work_without_config_table_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'fixture.db'
            with sqlite3.connect(file) as db:
                db.executescript(module.SCHEMA_SQL)
                db.execute("INSERT INTO account_configs(account_id,api_key,api_secret,risk_capital_multiplier,is_enabled) VALUES ('fixture','fake','fake',1,1)")
                db.execute("INSERT INTO channel_routing(channel_id,target_account_id) VALUES ('-100','fixture')")
                db.execute("INSERT INTO symbol_risk_configs(symbol,risk_ratio) VALUES ('BTCUSDT',0.02)")
            def run(*args):
                result = subprocess.run([sys.executable, str(SCRIPT), '--db', str(file), *args], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            first = run('record-signal', 'signal-1', 'entry', '--symbol', 'BTCUSDT')
            self.assertEqual(first['status'], 'ok')
            self.assertEqual(run('record-signal', 'signal-1', 'entry')['status'], 'duplicate')
            self.assertTrue(run('check-signal', 'signal-1', 'entry')['exists'])
            order = run('create-order', '-100', 'fixture', 'BTCUSDT', 'BUY', '65000')
            self.assertEqual(order['status'], 'ok')
            order_id = str(order['order_id'])
            self.assertEqual(run('update-order', order_id, 'OPEN')['status'], 'ok')
            self.assertEqual(run('update-sl', order_id, '64000')['status'], 'ok')
            self.assertEqual(run('add-briefing', '-100', 'fixture briefing')['status'], 'ok')
            with sqlite3.connect(file) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM signal_operations').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT count(*) FROM active_orders').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT count(*) FROM briefings').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT count(*) FROM account_configs').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT count(*) FROM channel_routing').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT count(*) FROM symbol_risk_configs').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
