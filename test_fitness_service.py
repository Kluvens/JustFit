import hashlib
from contextlib import closing
import http.client
import json
import sqlite3
import ssl
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fitness_service import Service, make_identity, rotate_secret
from fitness_store import backup_database, connect, connect_readonly, current_monthly_summary, database_lock, manifest, refresh_monthly_summary


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.identity = root / 'identity'
        self.database = root / 'private' / 'records.sqlite'
        key, cert, self.secret, self.fingerprint = make_identity(self.identity)
        self.device = str(uuid.uuid4())
        self.server = Service(('127.0.0.1', 0), self.database, self.secret, cert, key)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.context = ssl.create_default_context(cafile=str(cert))
        self.context.check_hostname = False  # Application pins the certificate, not the hostname.
        self.record = {'id': str(uuid.uuid4()), 'version': 1, 'date': '2026-11-02', 'exercises': [
            {'name': '引体向上', 'category': 'back', 'sets': [{'weight': None, 'unit': 'kg', 'weightMeaning': 'none', 'reps': 8}]}]}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        if self.server.summary_worker:
            self.server.summary_worker.join(timeout=5)
        self.temp.cleanup()

    def request(self, path, data, token=None):
        connection = http.client.HTTPSConnection('127.0.0.1', self.server.server_port, context=self.context, timeout=5)
        connection.connect()
        self.assertEqual(hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest(), self.fingerprint)
        data = dict(data)
        data.setdefault('deviceId', self.device)
        connection.request('POST', path, json.dumps(data), {'Authorization': 'Bearer ' + (token or self.secret), 'Content-Type': 'application/json'})
        response = connection.getresponse()
        status, body = response.status, json.loads(response.read())
        connection.close()
        return status, body

    def test_authenticated_sync_and_fresh_recovery(self):
        nonce = 'synthetic-nonce-1234'
        status, reply = self.request('/sync', {'nonce': nonce, 'records': [self.record]})
        self.assertEqual(status, 200)
        self.assertEqual(reply['nonce'], nonce)
        self.assertEqual(reply['results'][0]['status'], 'accepted')
        self.assertEqual(reply['manifest'][0]['version'], 1)
        self.assertEqual(reply['summaryStatus'], {'lastGeneratedAt': None, 'pendingMonths': 1})
        deleted = {'id': self.record['id'], 'version': 2, 'deleted': True}
        self.request('/sync', {'nonce': 'delete-nonce-12345', 'records': [deleted]})
        status, current = self.request('/recovery', {'nonce': 'recovery-nonce-1234'})
        self.assertEqual(status, 200)
        self.assertEqual(current['nonce'], 'recovery-nonce-1234')
        self.assertEqual(current['records'], [deleted])
        status, retry = self.request('/sync', {'nonce': 'retry-nonce-123456', 'records': [self.record]})
        self.assertEqual(retry['results'][0]['status'], 'stale')
        backup = self.database.with_name(self.database.name + '.backup')
        db = connect_readonly(backup)
        try:
            self.assertTrue(manifest(db)[0]['deleted'])
            self.assertEqual(manifest(db)[0]['version'], 2)
        finally:
            db.close()

    def test_rejects_unauthorized_and_invalid_requests(self):
        status, _ = self.request('/sync', {'nonce': 'synthetic-nonce-1234', 'records': [self.record]}, token='wrong')
        self.assertEqual(status, 401)
        status, body = self.request('/sync', {'nonce': 'synthetic-nonce-5678', 'records': [dict(self.record, version=0)]})
        self.assertEqual(status, 200)
        self.assertEqual(body['results'][0]['status'], 'error')
        self.assertEqual(body['manifest'], [])
        status, _ = self.request('/recovery', {'nonce': 'short'})
        self.assertEqual(status, 400)

    def test_invalid_large_integer_does_not_break_valid_record_in_same_batch(self):
        malformed = dict(self.record, id=str(uuid.uuid4()), version=2**63)
        status, body = self.request('/sync', {'nonce': 'mixed-records-123456', 'records': [malformed, self.record]})
        self.assertEqual(status, 200)
        self.assertEqual([entry['status'] for entry in body['results']], ['error', 'accepted'])
        self.assertIn('版本号', body['results'][0]['message'])
        self.assertEqual(len(body['manifest']), 1)

    def test_newer_request_before_older_request_cannot_be_overwritten(self):
        newer = dict(self.record, version=3, note='synthetic newer')
        older = dict(self.record, version=2, note='synthetic older')
        self.assertEqual(self.request('/sync', {'nonce': 'version-three-123456', 'records': [newer]})[1]['results'][0]['status'], 'accepted')
        self.assertEqual(self.request('/sync', {'nonce': 'version-two-12345678', 'records': [older]})[1]['results'][0]['status'], 'stale')
        conflict = dict(newer, note='different same-version content')
        self.assertEqual(self.request('/sync', {'nonce': 'version-conflict-1234', 'records': [conflict]})[1]['results'][0]['status'], 'error')
        with closing(connect_readonly(self.database)) as db:
            saved = json.loads(db.execute('SELECT payload FROM records WHERE id=?', (self.record['id'],)).fetchone()[0])
            self.assertEqual(saved['version'], 3)
            self.assertEqual(saved['note'], 'synthetic newer')

    def test_lost_response_can_be_retried_without_duplicate(self):
        connection = http.client.HTTPSConnection('127.0.0.1', self.server.server_port, context=self.context, timeout=5)
        payload = {'nonce': 'disconnect-nonce-12345', 'deviceId': self.device, 'records': [self.record]}
        connection.request('POST', '/sync', json.dumps(payload), {'Authorization': 'Bearer ' + self.secret, 'Content-Type': 'application/json'})
        connection.close()  # No response is read; the client does not know if it was committed.
        deadline = time.monotonic() + 5
        accepted = False
        while time.monotonic() < deadline:
            try:
                with closing(connect_readonly(self.database)) as db:
                    accepted = len(manifest(db)) == 1
                if accepted:
                    break
            except (OSError, sqlite3.Error):
                pass
            time.sleep(.02)
        self.assertTrue(accepted)
        status, reply = self.request('/sync', {'nonce': 'retry-after-loss-1234', 'records': [self.record]})
        self.assertEqual(status, 200)
        self.assertEqual(reply['results'][0]['status'], 'duplicate')
        self.assertEqual(len(reply['manifest']), 1)

    def test_new_device_requires_rotation_and_old_service_is_revoked(self):
        request = {'nonce': 'synthetic-nonce-1234', 'records': [self.record]}
        self.assertEqual(self.request('/sync', request)[0], 200)
        request['deviceId'] = str(uuid.uuid4())
        self.assertEqual(self.request('/sync', request)[0], 403)
        rotate_secret(self.identity)
        self.assertEqual(self.request('/sync', {'nonce': 'synthetic-nonce-5678', 'records': [self.record]})[0], 401)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        key, cert, self.secret, self.fingerprint = make_identity(self.identity)
        self.server = Service(('127.0.0.1', 0), self.database, self.secret, cert, key)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.device = str(uuid.uuid4())
        status, restored = self.request('/recovery', {'nonce': 'fresh-recovery-12345'})
        self.assertEqual(status, 200)
        self.assertEqual(restored['records'][0]['version'], 1)

    def test_backup_failure_never_acknowledges_and_retry_repairs_backup(self):
        payload = {'nonce': 'backup-failure-123456', 'records': [self.record]}
        with patch('fitness_service.backup_database', side_effect=OSError('synthetic error')):
            status, _ = self.request('/sync', payload)
        self.assertEqual(status, 500)
        self.assertFalse(self.database.with_name(self.database.name + '.backup').exists())
        status, reply = self.request('/sync', payload)
        self.assertEqual(status, 200)
        self.assertEqual(reply['results'][0]['status'], 'duplicate')
        backup = self.database.with_name(self.database.name + '.backup')
        db = connect_readonly(backup)
        try:
            self.assertEqual(manifest(db)[0]['version'], 1)
        finally:
            db.close()

    def test_deleted_note_does_not_remain_in_current_database_or_backup(self):
        marker = 'SYNTHETIC_PRIVATE_NOTE_823756'
        self.record['note'] = marker
        self.assertEqual(self.request('/sync', {'nonce': 'synthetic-note-12345', 'records': [self.record]})[0], 200)
        deleted = {'id': self.record['id'], 'version': 2, 'deleted': True}
        self.assertEqual(self.request('/sync', {'nonce': 'synthetic-delete-123', 'records': [deleted]})[0], 200)
        self.assertNotIn(marker.encode(), self.database.read_bytes())
        self.assertNotIn(marker.encode(), self.database.with_name(self.database.name + '.backup').read_bytes())

    def test_deletion_invalidates_and_clears_summary_in_database_and_backup(self):
        marker = 'SYNTHETIC_EXERCISE_572814'
        self.record['exercises'][0]['name'] = marker
        self.assertEqual(self.request('/sync', {'nonce': 'summary-create-12345', 'records': [self.record]})[0], 200)
        with database_lock(self.database):
            db = connect(self.database)
            try:
                refresh_monthly_summary(db, '2026-11')
                self.assertIn(marker, current_monthly_summary(db, '2026-11')['body'])
                backup_database(self.database)
            finally:
                db.close()
        _, generated = self.request('/sync', {'nonce': 'summary-check-123456', 'records': [self.record]})
        self.assertIsNotNone(generated['summaryStatus']['lastGeneratedAt'])
        self.assertEqual(generated['summaryStatus']['pendingMonths'], 0)
        deleted = {'id': self.record['id'], 'version': 2, 'deleted': True}
        status, reply = self.request('/sync', {'nonce': 'summary-delete-12345', 'records': [deleted]})
        self.assertEqual(status, 200)
        self.assertEqual(reply['summaryStatus']['pendingMonths'], 1)
        self.assertIsNotNone(reply['summaryStatus']['lastGeneratedAt'])
        for path in (self.database, self.database.with_name(self.database.name + '.backup')):
            with closing(connect_readonly(path)) as db:
                result = current_monthly_summary(db, '2026-11')
                self.assertEqual(result['status'], 'stale')
                self.assertNotIn('body', result)
            self.assertNotIn(marker.encode(), path.read_bytes())

    def test_pending_summary_is_refreshed_after_next_sync(self):
        status, first = self.request('/sync', {'nonce': 'summary-first-123456', 'records': [self.record]})
        self.assertEqual(status, 200)
        self.assertEqual(first['summaryStatus']['pendingMonths'], 1)
        status, second = self.request('/sync', {'nonce': 'summary-second-12345', 'records': [self.record]})
        self.assertEqual(status, 200)
        self.assertEqual(second['summaryStatus']['pendingMonths'], 1)
        deadline = time.monotonic() + 5
        while self.server.summary_worker is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(self.server.summary_worker)
        self.server.summary_worker.join(timeout=5)
        with closing(connect_readonly(self.database)) as db:
            self.assertEqual(current_monthly_summary(db, '2026-11')['status'], 'current')
        with closing(connect_readonly(self.database.with_name(self.database.name + '.backup'))) as db:
            self.assertEqual(current_monthly_summary(db, '2026-11')['status'], 'current')

    def test_summary_backup_failure_leaves_month_stale_for_retry(self):
        self.assertEqual(self.request('/sync', {'nonce': 'summary-fail-1234567', 'records': [self.record]})[0], 200)
        with patch('fitness_service.backup_database', side_effect=OSError('synthetic failure')):
            self.server._refresh_summaries(['2026-11'])
        with closing(connect_readonly(self.database)) as db:
            result = current_monthly_summary(db, '2026-11')
            self.assertEqual(result['status'], 'stale')
            self.assertNotIn('body', result)


if __name__ == '__main__':
    unittest.main()
