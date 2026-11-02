#!/usr/bin/env python3
"""Local import and read-only queries for the fitness app."""
import argparse
from contextlib import closing, nullcontext
import json
import os
import sqlite3
from pathlib import Path
from fitness_store import RecordError, backup_database, compare_snapshot, connect, connect_readonly, current_monthly_summary, database_lock, exercise_comparison, exercise_history, ingest, manifest, monthly_narrative, period_summary, records, recover_backup_copy, reconcile_missing_projections, refresh_monthly_summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=str(Path(__file__).resolve().parents[2] / '90_System/local/fitness/fitness.sqlite3'))
    sub = parser.add_subparsers(dest='command', required=True)
    imp = sub.add_parser('import')
    imp.add_argument('file')
    imp.add_argument('--receipt', help='Write a local JSON acknowledgement for import on the iPhone')
    sub.add_parser('manifest')
    sub.add_parser('list')
    backup = sub.add_parser('backup')
    backup.add_argument('file')
    recover = sub.add_parser('inspect-backup-copy', help='Verify a SQLite backup into a new, inactive file; never overwrite a store')
    recover.add_argument('source')
    recover.add_argument('destination')
    independent = sub.add_parser('export-snapshot', help='Create a new verified SQLite snapshot on user-selected storage')
    independent.add_argument('destination')
    compare_backup = sub.add_parser('compare-snapshot', help='Compare a snapshot with the current Mac database without restoring it')
    compare_backup.add_argument('snapshot')
    hist = sub.add_parser('history')
    hist.add_argument('exercise')
    compare = sub.add_parser('compare')
    compare.add_argument('exercise')
    summary = sub.add_parser('summary')
    summary.add_argument('start')
    summary.add_argument('end')
    monthly = sub.add_parser('monthly-report', help='Generate a current, non-indexed text summary')
    monthly.add_argument('month', help='YYYY-MM')
    refresh = sub.add_parser('refresh-monthly', help='Regenerate a local derived summary')
    refresh.add_argument('month', help='YYYY-MM')
    stored = sub.add_parser('stored-monthly', help='Read a local derived summary only if current')
    stored.add_argument('month', help='YYYY-MM')
    args = parser.parse_args()
    if args.command == 'compare-snapshot':
        try:
            with database_lock(args.db), closing(connect_readonly(args.db)) as current, closing(connect_readonly(args.snapshot)) as snapshot:
                result = compare_snapshot(current, snapshot)
        except (RecordError, OSError, ValueError, sqlite3.Error) as exc:
            parser.exit(2, f'快照对账失败：{exc}\n')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == 'export-snapshot':
        try:
            with database_lock(args.db):
                recover_backup_copy(args.db, args.destination)
        except (RecordError, OSError, ValueError, sqlite3.Error) as exc:
            parser.exit(2, f'独立快照未创建：{exc}\n')
        print('已创建校验通过的独立快照；未来恢复前必须检查之后的修改和删除。')
        return
    if args.command == 'inspect-backup-copy':
        try:
            recover_backup_copy(args.source, args.destination)
        except (RecordError, OSError, ValueError, sqlite3.Error) as exc:
            parser.exit(2, f'备份副本未创建：{exc}\n')
        print('已创建未启用的备份副本；旧备份可能缺少之后的删除或编辑，必须先对账。')
        return
    with (database_lock(args.db) if args.command in ('import', 'refresh-monthly') else nullcontext()):
        try:
            if args.command == 'refresh-monthly' and not Path(args.db).is_file():
                raise FileNotFoundError('训练记录库尚不存在或尚未收录')
            db = connect(args.db) if args.command in ('import', 'refresh-monthly') else connect_readonly(args.db)
        except FileNotFoundError as exc:
            parser.exit(2, str(exc) + '\n')
        try:
            if args.command == 'import':
                data = json.loads(Path(args.file).read_text(encoding='utf-8'))
                batch = data if isinstance(data, list) else [data]
                result = []
                for item in batch:
                    try:
                        result.append({'id': item.get('id') if isinstance(item, dict) else None, 'version': item.get('version') if isinstance(item, dict) else None, 'status': ingest(db, item)})
                    except RecordError as exc:
                        result.append({'id': item.get('id') if isinstance(item, dict) else None, 'version': item.get('version') if isinstance(item, dict) else None, 'status': 'error', 'message': str(exc)})
                reconcile_missing_projections(db)
                backup_database(args.db)
                if args.receipt:
                    receipt = Path(args.receipt)
                    fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'w', encoding='utf-8') as output:
                        output.write(json.dumps({'schema': 'fitness-receipt-v1', 'results': result, 'manifest': manifest(db)}, ensure_ascii=False, indent=2))
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if any(item['status'] in ('error', 'stale') for item in result):
                    raise SystemExit(1)
            elif args.command == 'manifest':
                print(json.dumps(manifest(db), ensure_ascii=False, indent=2))
            elif args.command == 'list':
                print(json.dumps(records(db), ensure_ascii=False, indent=2))
            elif args.command == 'backup':
                payloads = [json.loads(row[0]) for row in db.execute('SELECT payload FROM records ORDER BY id')]
                fd = os.open(args.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as output:
                    json.dump({'schema': 'fitness-recovery-v1', 'records': payloads}, output, ensure_ascii=False)
                print('已创建本地恢复文件')
            elif args.command == 'history':
                print(json.dumps(exercise_history(db, args.exercise), ensure_ascii=False, indent=2))
            elif args.command == 'compare':
                print(json.dumps(exercise_comparison(db, args.exercise), ensure_ascii=False, indent=2))
            elif args.command == 'monthly-report':
                try:
                    print(monthly_narrative(db, args.month))
                except RecordError as exc:
                    parser.exit(2, str(exc) + '\n')
            elif args.command == 'refresh-monthly':
                try:
                    generated = refresh_monthly_summary(db, args.month)
                    backup_database(args.db)
                    print(json.dumps({'status': 'current', 'generatedAt': generated}, ensure_ascii=False))
                except RecordError as exc:
                    parser.exit(2, str(exc) + '\n')
            elif args.command == 'stored-monthly':
                print(json.dumps(current_monthly_summary(db, args.month), ensure_ascii=False))
            else:
                print(json.dumps(period_summary(db, args.start, args.end), ensure_ascii=False, indent=2))
        finally:
            db.close()


if __name__ == '__main__':
    main()
