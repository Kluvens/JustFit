"""Synthetic-only check of the existing knowledge retrieval boundary."""
import json
from contextlib import closing
import http.client
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import unittest
import uuid
import threading
import time
from types import SimpleNamespace

PIPELINE = Path(__file__).resolve().parents[2] / '90_System' / 'pipeline'
sys.path.insert(0, str(PIPELINE))
from build_system import build_sqlite, index_document_path
from note_contracts import check_text
from fitness_knowledge import invalidate_owned_note, note_path, publish_new_note
from fitness_store import connect, ingest, publish_summary_projection, refresh_monthly_summary
from fitness_service import Service, make_identity


class KnowledgeBoundaryTests(unittest.TestCase):
    def test_health_summary_is_scoped_and_stale_source_is_withheld(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve()
            db = vault / '90_System/derived/index.db'
            build_sqlite(db, [], [], [])
            note = note_path(vault, '2026-11')
            text = ('---\ntype: note\nsensitivity: high\nretrieval_profile: health\n'
                    'origin: ai-draft\nsource_authority: working-note\n---\n'
                    '# Monthly exercise\n\nSynthetic fitnessquartznine exercise summary.\n')
            digest = publish_new_note(vault, '2026-11', text)
            self.assertTrue(index_document_path(vault, note)['indexed'])
            def search(*extra):
                proc = subprocess.run([sys.executable, str(PIPELINE / 'query.py'), 'fitnessquartznine',
                                       '--db', str(db), '--no-expand', '--no-log', '--compact', *extra],
                                      text=True, capture_output=True, check=True)
                return json.loads(proc.stdout)
            self.assertEqual(search('--safe-profile')['documents'], [])
            self.assertEqual(len(search('--include-sensitive')['documents']), 1)
            note.write_text(text.replace('fitnessquartznine', 'changedtoken'), encoding='utf-8')
            changed = search('--include-sensitive')
            self.assertEqual(changed['documents'], [])
            self.assertEqual(changed['stale_documents']['withheld'], 1)
            note.write_text(text, encoding='utf-8')
            self.assertTrue(invalidate_owned_note(vault, '2026-11', digest))
            removed = search('--include-sensitive')
            self.assertEqual(removed['documents'], [])
            self.assertEqual(removed['stale_documents']['withheld'], 1)

    def test_source_update_withholds_previously_indexed_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve()
            index = vault / '90_System/derived/index.db'
            build_sqlite(index, [], [], [])
            db = connect(vault / '90_System/local/fitness/fitness.sqlite3')
            try:
                record = {'id': str(uuid.uuid4()), 'version': 1, 'date': '2026-11-02', 'exercises': []}
                ingest(db, record)
                text = ('---\ntype: note\nsensitivity: high\nretrieval_profile: health\n'
                        'origin: ai-draft\nsource_authority: working-note\n---\n'
                        '# Synthetic\n\nfitnessopal482 old summary.\n')
                digest = publish_new_note(vault, '2026-11', text)
                db.execute('INSERT INTO knowledge_notes VALUES (?,?)', ('2026-11', digest))
                db.commit()
                self.assertTrue(index_document_path(vault, note_path(vault, '2026-11'))['indexed'])
                def search():
                    proc = subprocess.run([sys.executable, str(PIPELINE / 'query.py'), 'fitnessopal482',
                                           '--db', str(index), '--no-expand', '--no-log', '--compact',
                                           '--include-sensitive'], text=True, capture_output=True, check=True)
                    return json.loads(proc.stdout)
                self.assertEqual(len(search()['documents']), 1)
                self.assertEqual(ingest(db, dict(record, version=2)), 'accepted')
                result = search()
                self.assertEqual(result['documents'], [])
                self.assertEqual(result['stale_documents']['withheld'], 1)
            finally:
                db.close()

    def test_generate_index_invalidate_and_regenerate_from_current_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve()
            index = vault / '90_System/derived/index.db'
            build_sqlite(index, [], [], [])
            db = connect(vault / '90_System/local/fitness/fitness.sqlite3')
            try:
                record = {'id': str(uuid.uuid4()), 'version': 1, 'date': '2026-11-02', 'exercises': [
                    {'name': 'fitnessjasper720', 'category': 'chest', 'sets': [
                        {'weight': 40, 'unit': 'kg', 'weightMeaning': 'total', 'reps': 10}]}]}
                ingest(db, record)
                refresh_monthly_summary(db, '2026-11')
                def index_note(root, note):
                    self.assertEqual(check_text(note.read_text(), require_contract=True, vault=root), [])
                    return index_document_path(root, note)['indexed']
                path = publish_summary_projection(db, '2026-11', index_note)
                def search():
                    proc = subprocess.run([sys.executable, str(PIPELINE / 'query.py'), 'fitnessjasper720',
                                           '--db', str(index), '--no-expand', '--no-log', '--compact',
                                           '--include-sensitive'], text=True, capture_output=True, check=True)
                    return json.loads(proc.stdout)
                self.assertEqual(len(search()['documents']), 1)
                updated = json.loads(json.dumps(record))
                updated['version'] = 2
                updated['exercises'][0]['sets'][0]['weight'] = 45
                ingest(db, updated)
                self.assertFalse(path.exists())
                self.assertEqual(search()['documents'], [])
                refresh_monthly_summary(db, '2026-11')
                publish_summary_projection(db, '2026-11', index_note)
                self.assertEqual(len(search()['documents']), 1)
                self.assertIn('45 kg', path.read_text())
                self.assertNotIn('40 kg', path.read_text())
            finally:
                db.close()

    def test_failed_index_cleans_file_and_does_not_register_note(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve()
            db = connect(vault / '90_System/local/fitness/fitness.sqlite3')
            try:
                record = {'id': str(uuid.uuid4()), 'version': 1, 'date': '2026-11-02', 'exercises': []}
                ingest(db, record)
                refresh_monthly_summary(db, '2026-11')
                with self.assertRaisesRegex(RuntimeError, 'synthetic'):
                    publish_summary_projection(db, '2026-11', lambda *_: (_ for _ in ()).throw(RuntimeError('synthetic')))
                self.assertFalse(note_path(vault, '2026-11').exists())
                self.assertEqual(db.execute('SELECT count(*) FROM knowledge_notes').fetchone()[0], 0)
                self.assertEqual(ingest(db, dict(record, version=2)), 'accepted')
            finally:
                db.close()

    def test_background_worker_publishes_real_index_in_isolated_vault(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve()
            system = vault / '90_System'
            (system / 'local').mkdir(parents=True)
            (system / 'pipeline').symlink_to(PIPELINE, target_is_directory=True)
            (system / 'local/venv').symlink_to(PIPELINE.parent / 'local/venv', target_is_directory=True)
            index = system / 'derived/index.db'
            build_sqlite(index, [], [], [])
            database = system / 'local/fitness/fitness.sqlite3'
            db = connect(database)
            try:
                record = {'id': str(uuid.uuid4()), 'version': 1, 'date': '2026-11-02', 'exercises': [
                    {'name': 'fitnessgarnet743', 'category': 'legs', 'sets': [
                        {'weight': None, 'unit': 'kg', 'weightMeaning': 'none', 'reps': 8}]}]}
                ingest(db, record)
            finally:
                db.close()
            Service._refresh_summaries(SimpleNamespace(db_path=str(database)), ['2026-11'])
            note = note_path(vault, '2026-11')
            self.assertTrue(note.exists())
            self.assertIn('fitnessgarnet743', note.read_text())
            proc = subprocess.run([sys.executable, str(PIPELINE / 'query.py'), 'fitnessgarnet743',
                                   '--db', str(index), '--no-expand', '--no-log', '--compact',
                                   '--include-sensitive'], text=True, capture_output=True, check=True)
            self.assertEqual(len(json.loads(proc.stdout)['documents']), 1)

    def test_encrypted_sync_publishes_and_invalidates_health_note(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve()
            system = vault / '90_System'
            (system / 'local').mkdir(parents=True)
            (system / 'pipeline').symlink_to(PIPELINE, target_is_directory=True)
            (system / 'local/venv').symlink_to(PIPELINE.parent / 'local/venv', target_is_directory=True)
            index = system / 'derived/index.db'
            build_sqlite(index, [], [], [])
            database = system / 'local/fitness/fitness.sqlite3'
            key, cert, secret, _ = make_identity(vault / 'identity')
            server = Service(('127.0.0.1', 0), database, secret, cert, key)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            context = ssl.create_default_context(cafile=str(cert))
            context.check_hostname = False
            device = str(uuid.uuid4())
            record = {'id': str(uuid.uuid4()), 'version': 1, 'date': '2026-11-02', 'exercises': [
                {'name': 'fitnessamber592', 'category': 'chest', 'sets': [
                    {'weight': 40, 'unit': 'kg', 'weightMeaning': 'total', 'reps': 10}]}]}
            def sync(item):
                connection = http.client.HTTPSConnection('127.0.0.1', server.server_port, context=context, timeout=5)
                try:
                    body = json.dumps({'nonce': str(uuid.uuid4()), 'deviceId': device, 'records': [item]})
                    connection.request('POST', '/sync', body, {'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json'})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    return json.loads(response.read())
                finally:
                    connection.close()
            def wait_note():
                note = note_path(vault, '2026-11')
                deadline = time.monotonic() + 8
                while not note.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(note.exists())
                return note
            try:
                self.assertEqual(sync(record)['results'][0]['status'], 'accepted')
                self.assertEqual(sync(record)['results'][0]['status'], 'duplicate')
                note = wait_note()
                if server.summary_worker:
                    server.summary_worker.join(5)
                self.assertIn('40 kg', note.read_text())
                updated = json.loads(json.dumps(record))
                updated['version'] = 2
                updated['exercises'][0]['sets'][0]['weight'] = 45
                self.assertEqual(sync(updated)['results'][0]['status'], 'accepted')
                self.assertFalse(note.exists())
                self.assertEqual(sync(updated)['results'][0]['status'], 'duplicate')
                note = wait_note()
                if server.summary_worker:
                    server.summary_worker.join(5)
                self.assertIn('45 kg', note.read_text())
                self.assertNotIn('40 kg', note.read_text())
            finally:
                server.shutdown(); server.server_close(); worker.join()
                if server.summary_worker:
                    server.summary_worker.join(5)

    def test_source_edit_waits_until_projection_finishes_then_invalidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve()
            path = vault / '90_System/local/fitness/fitness.sqlite3'
            db = connect(path)
            record = {'id': str(uuid.uuid4()), 'version': 1, 'date': '2026-11-02', 'exercises': []}
            ingest(db, record)
            refresh_monthly_summary(db, '2026-11')
            db.close()
            entered, release = threading.Event(), threading.Event()
            failures = []
            def publish():
                local = connect(path)
                try:
                    def held_index(*_):
                        entered.set()
                        if not release.wait(3):
                            raise TimeoutError('test release')
                        return True
                    publish_summary_projection(local, '2026-11', held_index)
                except Exception as exc:
                    failures.append(exc)
                finally:
                    local.close()
            def edit():
                local = connect(path)
                try:
                    ingest(local, dict(record, version=2))
                except Exception as exc:
                    failures.append(exc)
                finally:
                    local.close()
            publisher = threading.Thread(target=publish)
            publisher.start()
            self.assertTrue(entered.wait(3))
            editor = threading.Thread(target=edit)
            editor.start()
            release.set()
            publisher.join(5); editor.join(5)
            self.assertFalse(publisher.is_alive() or editor.is_alive())
            self.assertEqual(failures, [])
            self.assertFalse(note_path(vault, '2026-11').exists())
            with closing(connect(path)) as final:
                self.assertEqual(final.execute('SELECT version FROM records').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
