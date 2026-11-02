"""Run the iOS end-to-end test with a disposable encrypted local receiver."""
from contextlib import closing
import argparse
import json
import subprocess
import sys
import tempfile
import threading
import uuid
from unittest.mock import patch
from pathlib import Path

from fitness_service import Service, make_identity, receipt, rotate_secret
from fitness_store import connect, connect_readonly, exercise_history, ingest, manifest, records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--simulator', required=True)
    parser.add_argument('--derived-data', default='/private/tmp/fitness-ui-reproducible')
    parser.add_argument('--log', default='/private/tmp/fitness-ui-reproducible.log')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='fitness-ui-fixture-') as temporary:
        root = Path(temporary)
        key, cert, _, fingerprint = make_identity(root / 'identity')
        secret = 'synthetic-test-only-012345678901234567890123456789'
        (root / 'identity' / 'pairing-secret').write_text(secret)
        database = root / 'test.sqlite3'
        def start(secret_value):
            instance = Service(('127.0.0.1', 0), database, secret_value, cert, key)
            worker = threading.Thread(target=instance.serve_forever, daemon=True)
            worker.start()
            return instance, worker
        server, thread = start(secret)
        try:
            def run_test(name, mode):
                command = ['xcodebuild', '-project', 'FitnessApp.xcodeproj', '-scheme', 'FitnessApp',
                           '-destination', f'platform=iOS Simulator,id={args.simulator}',
                           '-derivedDataPath', args.derived_data, 'CODE_SIGNING_ALLOWED=YES', 'CODE_SIGN_IDENTITY=-',
                           f'FITNESS_TEST_PORT={server.server_port}', f'FITNESS_TEST_FINGERPRINT={fingerprint}',
                           f'-only-testing:FitnessUITests/FitnessUITests/{name}', 'test']
                with open(args.log, mode, encoding='utf-8') as output:
                    return subprocess.run(command, stdout=output, stderr=subprocess.STDOUT)
            result = run_test('testSyntheticWorkoutSync', 'w')
            if result.returncode:
                print('模拟器 UI 测试失败，诊断日志：' + args.log)
                return result.returncode
            with closing(connect_readonly(database)) as db:
                history = exercise_history(db, '杠铃卧推')
            if len(history) != 1 or history[0]['sets'] != [{'weight': 40, 'unit': 'kg', 'weightMeaning': 'total', 'reps': 10}]:
                print('UI 测试完成，但 Mac 端读回的数据不符合预期。')
                return 1
            with closing(connect_readonly(database)) as db:
                previous_version = records(db)[0]['version']
            # Exercise deletion state before restoring to a replacement phone.
            deleted_id = str(uuid.uuid4())
            with closing(connect(database)) as db:
                ingest(db, {'id': deleted_id, 'version': 1, 'date': '2026-11-01', 'exercises': []})
                ingest(db, {'id': deleted_id, 'version': 2, 'deleted': True})
            server.shutdown(); server.server_close(); thread.join()
            rotate_secret(root / 'identity')
            replacement = 'synthetic-replacement-only-0123456789012345678901'
            (root / 'identity' / 'pairing-secret').write_text(replacement)
            server, thread = start(replacement)
            result = run_test('testRecoveryOnNewDevice', 'a')
            if result.returncode:
                print('模拟器恢复测试失败，诊断日志：' + args.log)
                return result.returncode
            with closing(connect_readonly(database)) as db:
                current = records(db)
                tombstones = [item for item in manifest(db) if item['deleted']]
            if len(current) != 1 or current[0]['version'] <= previous_version or not any(item['id'] == deleted_id for item in tombstones):
                print('恢复后的编辑版本或删除状态未通过验证。')
                return 1
            def omit_results(db, batch, nonce):
                result = receipt(db, batch, nonce)
                result['results'] = []
                return result
            with patch('fitness_service.receipt', side_effect=omit_results):
                result = run_test('testMissingReceiptIsNotReportedAsSuccessfulSync', 'a')
            if result.returncode:
                print('模拟器不完整回执测试失败，诊断日志：' + args.log)
                return result.returncode
            def inconsistent_manifest(db, batch, nonce):
                result = receipt(db, batch, nonce)
                result['manifest'] = [entry for entry in result['manifest'] if entry['deleted']]
                return result
            with patch('fitness_service.receipt', side_effect=inconsistent_manifest):
                result = run_test('testReceiptManifestMismatchIsNotAccepted', 'a')
            if result.returncode:
                print('模拟器回执对账不一致测试失败，诊断日志：' + args.log)
                return result.returncode
            # Artificially advance only the disposable Mac fixture to simulate a stale replay.
            newer = dict(current[0], version=current[0]['version'] + 1)
            with closing(connect(database)) as db:
                ingest(db, newer)
            result = run_test('testStaleRecordShowsReasonAndIsNotAccepted', 'a')
            if result.returncode:
                print('模拟器冲突提示测试失败，诊断日志：' + args.log)
                return result.returncode
            result = run_test('testDeleteWorkoutAndSync', 'a')
            if result.returncode:
                print('模拟器删除同步测试失败，诊断日志：' + args.log)
                return result.returncode
            with closing(connect_readonly(database)) as db:
                after = manifest(db)
            new_tombstones = [item for item in after if item['deleted'] and item['id'] != deleted_id]
            if len(new_tombstones) != 1 or new_tombstones[0]['id'] == current[0]['id']:
                print('手机删除未在 Mac 形成正确墓碑。')
                return 1
            result = run_test('testRemoveExercise', 'a')
            if result.returncode:
                print('模拟器移除动作测试失败，诊断日志：' + args.log)
                return result.returncode
            print('模拟器录入、重启草稿、加密同步、恢复、冲突提示、手机删除同步和移除动作通过；数据为虚构测试数据。')
            return 0
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    sys.exit(main())
