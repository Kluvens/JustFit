import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from fitness_store import RecordError, backup_database, compare_snapshot, connect, connect_readonly, current_monthly_summary, exercise_comparison, exercise_history, ingest, manifest, monthly_narrative, period_summary, records, recover_backup_copy, reconcile_missing_projections, refresh_monthly_summary
from fitness_knowledge import invalidate_owned_note, note_path, publish_new_note, render_note


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
        self.record = {'id': str(uuid.uuid4()), 'version': 1, 'date': '2026-11-02', 'exercises': [
            {'name': '杠铃卧推', 'category': 'chest', 'sets': [{'weight': 40, 'unit': 'kg', 'weightMeaning': 'total', 'reps': 10}]},
            {'name': '引体向上', 'category': 'back', 'sets': [{'weight': None, 'unit': 'kg', 'weightMeaning': 'none', 'reps': 8}]}]}

    def tearDown(self):
        self.db.close()

    def test_order_duplicate_and_conflict(self):
        newer = copy.deepcopy(self.record)
        newer['version'] = 3
        self.assertEqual(ingest(self.db, newer), 'accepted')
        self.assertEqual(ingest(self.db, self.record), 'stale')
        self.assertEqual(ingest(self.db, newer), 'duplicate')
        newer['date'] = '2026-11-03'
        with self.assertRaises(RecordError):
            ingest(self.db, newer)
        self.assertEqual(records(self.db)[0]['date'], '2026-11-02')

    def test_delete_cannot_resurrect(self):
        ingest(self.db, self.record)
        deleted = {'id': self.record['id'], 'version': 2, 'deleted': True}
        self.assertEqual(ingest(self.db, deleted), 'accepted')
        self.assertEqual(ingest(self.db, self.record), 'stale')
        newer = dict(self.record, version=3)
        with self.assertRaises(RecordError):
            ingest(self.db, newer)
        self.assertEqual(records(self.db), [])
        self.assertTrue(manifest(self.db)[0]['deleted'])

    def test_tombstone_does_not_retain_deleted_content(self):
        ingest(self.db, self.record)
        deleted = {'id': self.record['id'].upper(), 'version': 2, 'deleted': True, 'note': 'sensitive'}
        ingest(self.db, deleted)
        payload = self.db.execute('SELECT payload FROM records').fetchone()[0]
        self.assertNotIn('sensitive', payload)
        self.assertEqual(len(manifest(self.db)), 1)

    def test_backup_copy_is_verified_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'fitness.db')
            copy_path = os.path.join(directory, 'recovered.db')
            db = connect(source)
            ingest(db, self.record)
            ingest(db, {'id': self.record['id'], 'version': 2, 'deleted': True})
            db.close()
            backup_database(source)
            recover_backup_copy(source + '.backup', copy_path)
            restored = connect_readonly(copy_path)
            try:
                self.assertEqual(records(restored), [])
                self.assertEqual(manifest(restored)[0]['version'], 2)
                self.assertTrue(manifest(restored)[0]['deleted'])
            finally:
                restored.close()
            with self.assertRaises(FileExistsError):
                recover_backup_copy(source + '.backup', source)
            cli_copy = os.path.join(directory, 'cli-recovered.db')
            cli = subprocess.run([sys.executable, 'fitness_cli.py', 'inspect-backup-copy', source + '.backup', cli_copy], capture_output=True, text=True)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertIn('未启用', cli.stdout)
            retry = subprocess.run([sys.executable, 'fitness_cli.py', 'inspect-backup-copy', source + '.backup', cli_copy], capture_output=True, text=True)
            self.assertNotEqual(retry.returncode, 0)
            corrupt = os.path.join(directory, 'corrupt.db')
            with open(corrupt, 'wb') as output:
                output.write(b'not a database')
            with self.assertRaises(sqlite3.DatabaseError):
                recover_backup_copy(corrupt, os.path.join(directory, 'bad-copy.db'))
            self.assertFalse(os.path.exists(os.path.join(directory, 'bad-copy.db')))

    def test_external_snapshot_preserves_tombstone_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as primary, tempfile.TemporaryDirectory() as destination:
            path = os.path.join(primary, 'fitness.db')
            snapshot = os.path.join(destination, 'separate-snapshot.db')
            db = connect(path)
            ingest(db, self.record)
            ingest(db, {'id': self.record['id'], 'version': 2, 'deleted': True})
            db.close()
            cmd = [sys.executable, 'fitness_cli.py', '--db', path, 'export-snapshot', snapshot]
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(os.stat(snapshot).st_mode & 0o777, 0o600)
            restored = connect_readonly(snapshot)
            try:
                self.assertEqual(records(restored), [])
                self.assertEqual(manifest(restored)[0]['version'], 2)
                self.assertTrue(manifest(restored)[0]['deleted'])
            finally:
                restored.close()
            retry = subprocess.run(cmd, capture_output=True, text=True)
            self.assertNotEqual(retry.returncode, 0)

    def test_snapshot_comparison_detects_equal_count_content_and_deletion_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            live_path = os.path.join(directory, 'live.db')
            snapshot_path = os.path.join(directory, 'snapshot.db')
            live = connect(live_path)
            ingest(live, self.record)
            recover_backup_copy(live_path, snapshot_path)
            backup = connect(snapshot_path)
            try:
                self.assertTrue(compare_snapshot(live, backup)['recordsIdentical'])
                altered = copy.deepcopy(self.record)
                altered['exercises'][0]['sets'][0]['weight'] = 45
                backup.execute('UPDATE records SET payload=? WHERE id=?', (json.dumps(altered), self.record['id']))
                backup.commit()
                result = compare_snapshot(live, backup)
                self.assertEqual(result['currentCount'], result['snapshotCount'])
                self.assertEqual(result['differences'][0]['kind'], 'same_version_content_conflict')
                ingest(live, {'id': self.record['id'], 'version': 2, 'deleted': True})
                result = compare_snapshot(live, backup)
                self.assertEqual(result['differences'][0]['kind'], 'version_or_deletion_differs')
                self.assertTrue(result['differences'][0]['current']['deleted'])
                cli = subprocess.run([sys.executable, 'fitness_cli.py', '--db', live_path, 'compare-snapshot', snapshot_path], capture_output=True, text=True)
                self.assertEqual(cli.returncode, 0, cli.stderr)
                self.assertFalse(json.loads(cli.stdout)['recordsIdentical'])
            finally:
                live.close()
                backup.close()

    def test_invalid_record_does_not_write(self):
        self.record['exercises'][0]['sets'][0]['weightMeaning'] = None
        with self.assertRaises(RecordError):
            ingest(self.db, self.record)
        self.assertEqual(records(self.db), [])

    def test_rejects_values_that_cannot_roundtrip_to_phone(self):
        invalid = []
        invalid.append(dict(self.record, version=2**63))
        invalid.append(dict(self.record, note={'not': 'text'}))
        invalid.append(dict(self.record, startedAt='2026-11-02 04:00:00+00:00'))
        enormous_reps = copy.deepcopy(self.record)
        enormous_reps['exercises'][0]['sets'][0]['reps'] = 2**63
        invalid.append(enormous_reps)
        enormous_weight = copy.deepcopy(self.record)
        enormous_weight['exercises'][0]['sets'][0]['weight'] = 10**400
        invalid.append(enormous_weight)
        bad_unit = copy.deepcopy(self.record)
        bad_unit['exercises'][1]['sets'][0]['unit'] = 123
        invalid.append(bad_unit)
        for record in invalid:
            with self.subTest(record_type=str(type(record))):
                with self.assertRaises(RecordError):
                    ingest(self.db, record)
        self.assertEqual(records(self.db), [])

    def test_times_require_offsets_and_end_not_before_start(self):
        self.record['startedAt'] = '2026-11-02T04:00:00Z'
        self.record['endedAt'] = '2026-11-02T03:59:00Z'
        with self.assertRaises(RecordError):
            ingest(self.db, self.record)
        self.record['endedAt'] = '2026-11-02T05:00:00'
        with self.assertRaises(RecordError):
            ingest(self.db, self.record)
        self.record['endedAt'] = '2026-11-02T05:00:00Z'
        self.assertEqual(ingest(self.db, self.record), 'accepted')

    def test_history_preserves_empty_weight(self):
        ingest(self.db, self.record)
        self.assertIsNone(exercise_history(self.db, '引体向上')[0]['sets'][0]['weight'])

    def test_comparison_keeps_bodyweight_and_added_weight_separate(self):
        self.record['exercises'][1]['sets'].append({'weight': 5, 'unit': 'kg', 'weightMeaning': 'additional', 'reps': 6})
        ingest(self.db, self.record)
        comparison = exercise_comparison(self.db, '引体向上')
        groups = {g['weightMeaning']: g for g in comparison['sessions'][0]['groups']}
        self.assertEqual(groups['bodyweight']['sets'][0], {'weight': None, 'reps': 8})
        self.assertEqual(groups['additional']['sets'][0], {'weight': 5, 'reps': 6})

    def test_summary_counts_each_category_once_per_workout(self):
        self.record['exercises'].append(copy.deepcopy(self.record['exercises'][0]))
        ingest(self.db, self.record)
        result = period_summary(self.db, '2026-11-01', '2026-11-07')
        self.assertEqual(result['trainingCount'], 1)
        self.assertEqual(result['trainingCountByCategory']['chest'], 1)
        self.assertEqual(result['trainingCountByCategory']['back'], 1)
        self.assertEqual(result['trainingCountByCategory']['legs'], 0)

    def test_monthly_narrative_uses_current_records_and_separates_loads(self):
        ingest(self.db, self.record)
        second = copy.deepcopy(self.record)
        second['id'] = str(uuid.uuid4())
        second['date'] = '2026-11-12'
        second['exercises'][0]['sets'][0]['weight'] = 45
        second['exercises'][0]['sets'].append({'weight': 20, 'unit': 'kg', 'weightMeaning': 'per_side', 'reps': 8})
        second['exercises'][1]['sets'][0]['reps'] = 10
        ingest(self.db, second)
        report = monthly_narrative(self.db, '2026-11')
        self.assertIn('已收录 2 次', report)
        self.assertIn('40 kg × 10 次', report)
        self.assertIn('45 kg × 10 次', report)
        self.assertIn('单只', report)
        self.assertIn('引体向上（背，未记录重量）', report)
        self.assertIn('2026-11-12 10 次', report)
        ingest(self.db, {'id': second['id'], 'version': 2, 'deleted': True})
        updated = monthly_narrative(self.db, '2026-11')
        self.assertIn('已收录 1 次', updated)
        self.assertNotIn('45 kg', updated)
        with self.assertRaises(RecordError):
            monthly_narrative(self.db, '2026-1')
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'fitness.db')
            db = connect(path)
            ingest(db, self.record)
            db.close()
            cli = subprocess.run([sys.executable, 'fitness_cli.py', '--db', path, 'monthly-report', '2026-11'], capture_output=True, text=True)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertIn('已收录 1 次', cli.stdout)
            bad = subprocess.run([sys.executable, 'fitness_cli.py', '--db', path, 'monthly-report', 'invalid'], capture_output=True, text=True)
            self.assertEqual(bad.returncode, 2)
            self.assertIn('月份必须为 YYYY-MM', bad.stderr)

    def test_move_marks_both_months_stale(self):
        ingest(self.db, self.record)
        moved = dict(self.record, version=2, date='2026-12-01')
        ingest(self.db, moved)
        self.assertEqual({r[0] for r in self.db.execute('SELECT month FROM stale_months')}, {'2026-11', '2026-12'})

    def test_projection_is_invalidated_before_source_edit_and_preserves_unowned_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory).resolve()
            db = connect(vault / '90_System/local/fitness/fitness.sqlite3')
            try:
                ingest(db, self.record)
                digest = publish_new_note(vault, '2026-11', 'synthetic generated summary')
                db.execute('INSERT INTO knowledge_notes VALUES (?,?)', ('2026-11', digest))
                db.commit()
                changed = dict(self.record, version=2)
                note_path(vault, '2026-11').write_text('user modified this note')
                with self.assertRaises(RecordError):
                    ingest(db, changed)
                self.assertEqual(records(db)[0]['version'], 1)
                self.assertEqual(note_path(vault, '2026-11').read_text(), 'user modified this note')
                note_path(vault, '2026-11').write_text('synthetic generated summary')
                self.assertEqual(ingest(db, changed), 'accepted')
                self.assertFalse(note_path(vault, '2026-11').exists())
                self.assertEqual(records(db)[0]['version'], 2)
                # A crash-created, unregistered note cannot be silently ignored.
                publish_new_note(vault, '2026-11', 'unregistered note')
                with self.assertRaises(RecordError):
                    ingest(db, dict(self.record, version=3))
                self.assertEqual(records(db)[0]['version'], 2)
            finally:
                db.close()

    def test_crash_orphan_is_recovered_only_if_it_matches_current_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory).resolve()
            db = connect(vault / '90_System/local/fitness/fitness.sqlite3')
            try:
                ingest(db, self.record)
                refresh_monthly_summary(db, '2026-11')
                summary = current_monthly_summary(db, '2026-11')
                body = render_note('2026-11', summary['body'], summary['generatedAt'])
                publish_new_note(vault, '2026-11', body)
                self.assertEqual(db.execute('SELECT count(*) FROM knowledge_notes').fetchone()[0], 0)
                self.assertEqual(ingest(db, dict(self.record, version=2)), 'accepted')
                self.assertFalse(note_path(vault, '2026-11').exists())
            finally:
                db.close()

    def test_cross_month_preflight_and_partial_failure_reconcile(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory).resolve()
            db = connect(vault / '90_System/local/fitness/fitness.sqlite3')
            try:
                ingest(db, self.record)
                for month in ('2026-11', '2026-12'):
                    refresh_monthly_summary(db, month)
                    digest = publish_new_note(vault, month, 'synthetic ' + month)
                    db.execute('INSERT INTO knowledge_notes VALUES (?,?)', (month, digest))
                    db.commit()
                december = note_path(vault, '2026-12')
                december.write_text('externally edited')
                moved = dict(self.record, version=2, date='2026-12-01')
                with self.assertRaises(RecordError):
                    ingest(db, moved)
                self.assertTrue(note_path(vault, '2026-11').exists())
                self.assertEqual(records(db)[0]['version'], 1)
                december.write_text('synthetic 2026-12')
                def fail_second(root, month, digest):
                    if month == '2026-12':
                        raise OSError('synthetic error')
                    return invalidate_owned_note(root, month, digest)
                with patch('fitness_store.invalidate_owned_note', side_effect=fail_second):
                    with self.assertRaises(RecordError):
                        ingest(db, moved)
                self.assertFalse(note_path(vault, '2026-11').exists())
                self.assertTrue(december.exists())
                reconcile_missing_projections(db)
                self.assertEqual(current_monthly_summary(db, '2026-11')['status'], 'stale')
                self.assertEqual(records(db)[0]['version'], 1)
            finally:
                db.close()

    def test_stored_summary_becomes_unreadable_on_move_and_delete(self):
        ingest(self.db, self.record)
        refresh_monthly_summary(self.db, '2026-11')
        refresh_monthly_summary(self.db, '2026-12')
        self.assertEqual(current_monthly_summary(self.db, '2026-11')['status'], 'current')
        moved = dict(self.record, version=2, date='2026-12-01')
        ingest(self.db, moved)
        for month in ('2026-11', '2026-12'):
            result = current_monthly_summary(self.db, month)
            self.assertEqual(result['status'], 'stale')
            self.assertNotIn('body', result)
            self.assertEqual(self.db.execute('SELECT body FROM monthly_summaries WHERE month=?', (month,)).fetchone()[0], '')
        refresh_monthly_summary(self.db, '2026-12')
        self.assertIn('已收录 1 次', current_monthly_summary(self.db, '2026-12')['body'])
        ingest(self.db, {'id': self.record['id'], 'version': 3, 'deleted': True})
        self.assertEqual(current_monthly_summary(self.db, '2026-12')['status'], 'stale')
        self.assertNotIn('body', current_monthly_summary(self.db, '2026-12'))
        refresh_monthly_summary(self.db, '2026-12')
        self.assertIn('已收录 0 次', current_monthly_summary(self.db, '2026-12')['body'])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'fitness.db')
            db = connect(path)
            ingest(db, self.record)
            db.close()
            cmd = [sys.executable, 'fitness_cli.py', '--db', path]
            refresh = subprocess.run(cmd + ['refresh-monthly', '2026-11'], capture_output=True, text=True)
            self.assertEqual(refresh.returncode, 0, refresh.stderr)
            self.assertTrue(os.path.isfile(path + '.backup'))
            stored = subprocess.run(cmd + ['stored-monthly', '2026-11'], capture_output=True, text=True)
            self.assertEqual(json.loads(stored.stdout)['status'], 'current')

    def test_concurrent_writers_never_overwrite_newer_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'records.db')
            db = connect(path)
            ingest(db, self.record)
            db.close()
            version2 = dict(self.record, version=2)
            version3 = dict(self.record, version=3)
            def write(value):
                connection = connect(path)
                try:
                    return ingest(connection, value)
                finally:
                    connection.close()
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(write, [version3, version2]))
            db = connect(path)
            try:
                self.assertEqual(records(db)[0]['version'], 3)
            finally:
                db.close()

    def test_cli_import_receipt_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'source.json')
            database = os.path.join(directory, 'store.db')
            receipt = os.path.join(directory, 'receipt.json')
            backup = os.path.join(directory, 'backup.json')
            with open(source, 'w', encoding='utf-8') as output:
                json.dump([self.record], output)
            cmd = [sys.executable, 'fitness_cli.py', '--db', database]
            subprocess.run(cmd + ['import', source, '--receipt', receipt], check=True, capture_output=True)
            with open(receipt, encoding='utf-8') as input_file:
                data = json.load(input_file)
            self.assertEqual(data['results'][0]['status'], 'accepted')
            self.assertEqual(data['manifest'][0]['version'], 1)
            subprocess.run(cmd + ['backup', backup], check=True, capture_output=True)
            with open(backup, encoding='utf-8') as input_file:
                saved = json.load(input_file)
            self.assertEqual(saved['records'][0]['id'], self.record['id'])
            self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)

    def test_readonly_query_does_not_create_or_modify_store(self):
        with tempfile.TemporaryDirectory(prefix='fitness space ') as directory:
            path = os.path.join(directory, 'records.db')
            with self.assertRaises(FileNotFoundError):
                connect_readonly(path)
            self.assertFalse(os.path.exists(path))
            writer = connect(path)
            ingest(writer, self.record)
            writer.close()
            reader = connect_readonly(path)
            try:
                self.assertEqual(records(reader)[0]['id'], self.record['id'])
                with self.assertRaises(sqlite3.OperationalError):
                    reader.execute('DELETE FROM records')
            finally:
                reader.close()


if __name__ == '__main__':
    unittest.main()
