"""Private, local fitness record store. No server or network access."""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import uuid
from pathlib import Path
from fitness_knowledge import NoteError, invalidate_owned_note, note_path, publish_new_note, render_note, verify_owned_note


class RecordError(ValueError):
    pass


def validate(record):
    if not isinstance(record, dict):
        raise RecordError('训练记录必须是对象')
    try:
        uuid.UUID(record['id'])
    except (KeyError, ValueError, TypeError, AttributeError):
        raise RecordError('训练 ID 无效') from None
    version = record.get('version')
    if type(version) is not int or not 1 <= version <= 2**63 - 1:
        raise RecordError('版本号必须是设备支持的正整数')
    if record.get('deleted') is True:
        return
    if record.get('note') is not None and not isinstance(record['note'], str):
        raise RecordError('备注必须是文字或为空')
    try:
        date = dt.date.fromisoformat(record['date'])
        if date.isoformat() != record['date']:
            raise ValueError('non-canonical date')
    except (KeyError, ValueError, TypeError):
        raise RecordError('训练日期无效') from None
    timestamps = []
    for key in ('startedAt', 'endedAt'):
        value = record.get(key)
        if value is None:
            timestamps.append(None)
            continue
        try:
            if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})', value):
                raise ValueError('unsupported timestamp format')
            parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError('missing offset')
        except (ValueError, TypeError, AttributeError):
            raise RecordError('训练时间无效或缺少时区') from None
        timestamps.append(parsed)
    if all(timestamps) and timestamps[1] < timestamps[0]:
        raise RecordError('结束时间不能早于开始时间')
    if not isinstance(record.get('exercises'), list):
        raise RecordError('缺少动作列表')
    for exercise in record['exercises']:
        if not isinstance(exercise, dict) or not isinstance(exercise.get('name'), str) or not exercise['name'].strip():
            raise RecordError('动作名称不能为空')
        if exercise.get('category') not in ('chest', 'back', 'legs', 'other'):
            raise RecordError('动作类别无效')
        if not isinstance(exercise.get('sets'), list):
            raise RecordError('缺少组记录')
        for item in exercise['sets']:
            if not isinstance(item, dict) or type(item.get('reps')) is not int or not 0 <= item['reps'] <= 2**63 - 1:
                raise RecordError('次数必须为设备支持的非负整数')
            weight = item.get('weight')
            meaning = item.get('weightMeaning')
            if weight is None:
                if meaning != 'none':
                    raise RecordError('空重量必须标为不适用')
                if item.get('unit') is not None and not isinstance(item['unit'], str):
                    raise RecordError('重量单位必须是文字或为空')
            elif type(weight) not in (int, float) or (isinstance(weight, int) and abs(weight) > 10**308) or not math.isfinite(weight) or weight < 0:
                raise RecordError('重量必须是非负数或为空')
            elif item.get('unit') != 'kg' or meaning not in ('total', 'per_side', 'additional'):
                raise RecordError('重量需要 kg 单位及明确含义')


def connect(path):
    memory = str(path) == ':memory:'
    path = Path(path)
    if not memory:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(':memory:' if memory else path)
    db.execute('PRAGMA secure_delete=ON')
    db.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, version INTEGER NOT NULL, deleted INTEGER NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS stale_months (month TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE IF NOT EXISTS monthly_summaries (month TEXT PRIMARY KEY, body TEXT NOT NULL, generated_at TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS knowledge_notes (month TEXT PRIMARY KEY, digest TEXT NOT NULL)')
    if not memory:
        path.chmod(0o600)
    return db


@contextmanager
def database_lock(path):
    """Coordinate database writes and backup generation across local processes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.parent / '.fitness-database.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def backup_database(path):
    """Replace the local backup only after a consistent, verified SQLite copy."""
    path = Path(path)
    target = path.with_name(path.name + '.backup')
    fd, temporary = tempfile.mkstemp(prefix='.fitness-backup-', dir=path.parent)
    os.fchmod(fd, 0o600)
    os.close(fd)
    try:
        source = connect_readonly(path)
        try:
            destination = sqlite3.connect(temporary)
            try:
                source.backup(destination)
                result = destination.execute('PRAGMA integrity_check').fetchone()
                if result != ('ok',):
                    raise OSError('训练备份完整性检查失败')
            finally:
                destination.close()
        finally:
            source.close()
        with open(temporary, 'rb') as output:
            os.fsync(output.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def connect_readonly(path):
    """Read an existing store without creating or changing it."""
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError('训练记录库尚不存在或尚未收录')
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)


def recover_backup_copy(source_path, destination_path):
    """Inspect and copy a backup into a new file, never replacing a live store.

    A valid older backup may lack newer deletions; this function deliberately
    does not activate it or establish which copy is authoritative.
    """
    source = connect_readonly(source_path)
    created = False
    try:
        fd = os.open(destination_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        os.close(fd)
        try:
            destination = sqlite3.connect(destination_path)
            try:
                source.backup(destination)
                if destination.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                    raise RecordError('恢复副本完整性检查失败')
                for identifier, version, deleted, payload in destination.execute('SELECT id, version, deleted, payload FROM records'):
                    record = json.loads(payload)
                    validate(record)
                    if (record['id'] != identifier or record['version'] != version or
                            bool(record.get('deleted', False)) != bool(deleted)):
                        raise RecordError('备份记录与索引不一致')
                destination.execute('SELECT month FROM stale_months LIMIT 1').fetchall()
            finally:
                destination.close()
            with open(destination_path, 'rb') as output:
                os.fsync(output.fileno())
            directory_fd = os.open(Path(destination_path).resolve().parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            if created:
                os.unlink(destination_path)
            raise
    finally:
        source.close()


def projection_vault(db):
    """Enable the projection guard only for the canonical fitness store."""
    filename = db.execute('PRAGMA database_list').fetchone()[2]
    if not filename:
        return None
    path = Path(filename).resolve()
    if path.name != 'fitness.sqlite3' or tuple(p.name for p in path.parents[:3]) != ('fitness', 'local', '90_System'):
        return None
    return path.parents[3]


def invalidate_projection(db, months):
    """Withhold old indexed notes before committing a change to their source."""
    vault = projection_vault(db)
    if vault is None:
        return
    checked = []
    for month in sorted(months):
        note = note_path(vault, month)
        row = db.execute('SELECT digest FROM knowledge_notes WHERE month=?', (month,)).fetchone()
        try:
            note.lstat()
            exists = True
        except FileNotFoundError:
            exists = False
        except OSError as exc:
            raise RecordError(f'摘要文件无法检查，训练未更新：{exc}') from None
        if row is None and exists:
            # A crash between file creation and DB commit can leave an orphan.
            # Adopt it only when it exactly matches the current derived summary.
            summary = current_monthly_summary(db, month)
            if summary['status'] != 'current':
                raise RecordError('摘要文件未登记，已保留文件并停止更新，请先检查')
            expected = render_note(month, summary['body'], summary['generatedAt'])
            row = (hashlib.sha256(expected.encode('utf-8')).hexdigest(),)
        if row is not None:
            try:
                verify_owned_note(vault, month, row[0])
            except (NoteError, OSError) as exc:
                raise RecordError(f'旧摘要无法安全失效，训练未更新：{exc}') from None
            checked.append((month, row[0]))
    for month, digest in checked:
        try:
            invalidate_owned_note(vault, month, digest)
        except (NoteError, OSError) as exc:
            raise RecordError(f'旧摘要无法安全失效，训练未更新：{exc}') from None
    for month in months:
        db.execute('DELETE FROM knowledge_notes WHERE month=?', (month,))


def reconcile_missing_projections(db):
    """Retry a projection whose file disappeared while its row survived."""
    vault = projection_vault(db)
    if vault is None:
        return
    with db:
        db.execute('BEGIN IMMEDIATE')
        missing = []
        for (month,) in db.execute('SELECT month FROM knowledge_notes'):
            try:
                note_path(vault, month).lstat()
            except FileNotFoundError:
                missing.append(month)
        if missing:
            db.executemany('INSERT OR IGNORE INTO stale_months VALUES (?)', [(m,) for m in missing])
            db.executemany("UPDATE monthly_summaries SET body='' WHERE month=?", [(m,) for m in missing])
            db.executemany('DELETE FROM knowledge_notes WHERE month=?', [(m,) for m in missing])


def ingest(db, record):
    validate(record)
    record = dict(record)
    record['id'] = str(uuid.UUID(record['id']))
    if record.get('deleted') is True:
        record = {'id': record['id'], 'version': record['version'], 'deleted': True}
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    with db:
        # Serialize the read/version-check/write across independent connections.
        # A deferred transaction would allow two writers to observe the same
        # old version and then commit in the wrong order.
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT version, deleted, payload FROM records WHERE id=?', (record['id'],)).fetchone()
        if old and record['version'] < old[0]:
            return 'stale'
        if old and record['version'] == old[0]:
            if payload != old[2]:
                raise RecordError('相同版本内容不一致')
            return 'duplicate'
        if old and old[1] and not record.get('deleted'):
            raise RecordError('已删除训练不得重新写入')
        months = set()
        if old and not old[1]:
            months.add(json.loads(old[2])['date'][:7])
        if not record.get('deleted'):
            months.add(record['date'][:7])
        invalidate_projection(db, months)
        db.execute('INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version, deleted=excluded.deleted, payload=excluded.payload',
                   (record['id'], record['version'], int(record.get('deleted') is True), payload))
        db.executemany('INSERT OR IGNORE INTO stale_months VALUES (?)', [(month,) for month in months])
        db.executemany("UPDATE monthly_summaries SET body='' WHERE month=?", [(month,) for month in months])
    return 'accepted'


def records(db):
    return [json.loads(row[0]) for row in db.execute('SELECT payload FROM records WHERE deleted=0 ORDER BY json_extract(payload, "$.date") DESC, id')]


def manifest(db):
    return [{'id': row[0], 'version': row[1], 'deleted': bool(row[2])} for row in db.execute('SELECT id,version,deleted FROM records ORDER BY id')]


def compare_snapshot(current, snapshot):
    """Compare two read-only SQLite snapshots without activating either one."""
    def rows(db):
        if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise RecordError('数据库完整性检查失败')
        result = {}
        for identifier, version, deleted, payload in db.execute('SELECT id,version,deleted,payload FROM records'):
            record = json.loads(payload)
            validate(record)
            if record['id'] != identifier or record['version'] != version or bool(record.get('deleted', False)) != bool(deleted):
                raise RecordError('数据库记录与索引不一致')
            result[identifier] = (version, bool(deleted), record)
        return result
    current.execute('BEGIN')
    snapshot.execute('BEGIN')
    try:
        live, saved = rows(current), rows(snapshot)
    finally:
        current.rollback()
        snapshot.rollback()
    differences = []
    for identifier in sorted(live.keys() | saved.keys()):
        now, old = live.get(identifier), saved.get(identifier)
        if now is None:
            kind = 'snapshot_only'
        elif old is None:
            kind = 'current_only'
        elif now[0] != old[0] or now[1] != old[1]:
            kind = 'version_or_deletion_differs'
        elif now[2] != old[2]:
            kind = 'same_version_content_conflict'
        else:
            continue
        differences.append({'id': identifier, 'kind': kind,
                            'current': None if now is None else {'version': now[0], 'deleted': now[1]},
                            'snapshot': None if old is None else {'version': old[0], 'deleted': old[1]}})
    return {'currentCount': len(live), 'snapshotCount': len(saved), 'differences': differences,
            'recordsIdentical': not differences,
            'caution': '仅比较这两个副本的训练记录和墓碑，不比较摘要或配对信息；未同步的手机数据和快照之后的删除可能仍未知。'}


def exercise_history(db, name):
    results = []
    for record in records(db):
        for exercise in record['exercises']:
            if exercise['name'] == name:
                results.append({'date': record['date'], 'trainingId': record['id'], 'name': name, 'sets': exercise['sets']})
    return results


def exercise_comparison(db, name):
    """Keep meanings separate; never add weights across exercises or sides."""
    sessions = exercise_history(db, name)
    values = []
    for session in sessions:
        groups = {}
        for item in session['sets']:
            meaning = item['weightMeaning']
            key = ('bodyweight', None) if item.get('weight') is None else (meaning, item.get('unit'))
            groups.setdefault(key, []).append({'weight': item.get('weight'), 'reps': item['reps']})
        values.append({'date': session['date'], 'trainingId': session['trainingId'],
                       'groups': [{'weightMeaning': meaning, 'unit': unit, 'sets': sets}
                                  for (meaning, unit), sets in sorted(groups.items(), key=lambda x: str(x[0]))]})
    return {'exercise': name, 'sessions': values,
            'interpretation': '只比较相同动作和相同重量含义；徒手记录不等于零负荷，未推断动作质量或恢复。'}


def period_summary(db, start, end):
    try:
        first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except (ValueError, TypeError):
        raise RecordError('日期范围无效') from None
    if first > last:
        raise RecordError('开始日期不能晚于结束日期')
    selected = [r for r in records(db) if first <= dt.date.fromisoformat(r['date']) <= last]
    counts = {key: 0 for key in ('chest', 'back', 'legs', 'other')}
    for workout in selected:
        for category in {e['category'] for e in workout['exercises'] if e['sets']}:
            counts[category] += 1
    return {'start': start, 'end': end, 'trainingCount': len(selected), 'trainingCountByCategory': counts}


def monthly_narrative(db, month):
    """Compute an ephemeral report from one current database snapshot."""
    try:
        first = dt.date.fromisoformat(month + '-01')
        if first.strftime('%Y-%m') != month:
            raise ValueError('non-canonical month')
    except (ValueError, TypeError):
        raise RecordError('月份必须为 YYYY-MM') from None
    selected = [record for record in records(db) if record['date'][:7] == month]
    labels = {'chest': '胸', 'back': '背', 'legs': '腿', 'other': '其他'}
    counts = {key: 0 for key in labels}
    # Each point is a recorded best set for an exercise, category, meaning and day.
    # Different meanings and different exercises must never be combined.
    points = {}
    for record in selected:
        categories = {exercise['category'] for exercise in record['exercises'] if exercise['sets']}
        for category in categories:
            counts[category] += 1
        for exercise in record['exercises']:
            for item in exercise['sets']:
                meaning = 'bodyweight' if item.get('weight') is None else item['weightMeaning']
                key = (exercise['name'], exercise['category'], meaning, record['date'])
                value = (item['reps'],) if meaning == 'bodyweight' else (item['weight'], item['reps'])
                points[key] = max(points.get(key, value), value)
    lines = [f'{month} 已收录 {len(selected)} 次训练；' + '、'.join(f'{labels[key]} {counts[key]} 次' for key in labels) + '。']
    groups = {}
    for (name, category, meaning, date), value in points.items():
        groups.setdefault((name, category, meaning), []).append((date, value))
    meaning_labels = {'total': '总重', 'per_side': '单只', 'additional': '额外负重', 'bodyweight': '未记录重量'}
    for (name, category, meaning), series in sorted(groups.items()):
        series.sort(key=lambda item: item[0])
        def describe(point):
            date, value = point
            return f'{date} {value[0]} 次' if meaning == 'bodyweight' else f'{date} {value[0]:g} kg × {value[1]} 次'
        if len(series) > 1:
            lines.append(f'{name}（{labels[category]}，{meaning_labels[meaning]}）：每日记录中的最高组，{describe(series[0])}；最近记录日期为 {describe(series[-1])}。')
        else:
            lines.append(f'{name}（{labels[category]}，{meaning_labels[meaning]}）：记录的最高组为 {describe(series[0])}。')
    lines.append('仅依据已收录记录；未同步或未完成组不在内。重量为空不等于零负荷；次数、动作质量和身体状态不同，不能仅凭重量判断进步或恢复。')
    return '\n'.join(lines)


def refresh_monthly_summary(db, month):
    """Replace a derived summary and clear staleness in the same transaction."""
    with db:
        db.execute('BEGIN IMMEDIATE')
        body = monthly_narrative(db, month)
        generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
        db.execute('INSERT INTO monthly_summaries VALUES (?,?,?) ON CONFLICT(month) DO UPDATE SET body=excluded.body, generated_at=excluded.generated_at',
                   (month, body, generated_at))
        db.execute('DELETE FROM stale_months WHERE month=?', (month,))
    return generated_at


def current_monthly_summary(db, month):
    """Return no body for missing or stale derived summaries, atomically."""
    try:
        row = db.execute('SELECT s.body, s.generated_at, EXISTS(SELECT 1 FROM stale_months x WHERE x.month=s.month) '
                         'FROM monthly_summaries s WHERE s.month=?', (month,)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table: monthly_summaries' in str(exc):
            return {'status': 'missing'}
        raise
    if row is None:
        return {'status': 'missing'}
    if row[2]:
        return {'status': 'stale', 'generatedAt': row[1]}
    return {'status': 'current', 'generatedAt': row[1], 'body': row[0]}


def publish_summary_projection(db, month, indexer):
    """Publish a current summary under the same SQLite write lock as ingest.

    The callback must validate and index the file synchronously. If a crash
    leaves an unregistered file, later ingest fails closed until it is reviewed.
    """
    vault = projection_vault(db)
    if vault is None:
        raise RecordError('只有规范位置的健身数据库可以生成知识库摘要')
    created_digest = None
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            summary = current_monthly_summary(db, month)
            if summary['status'] != 'current':
                raise RecordError('摘要尚未生成或已过期')
            path = note_path(vault, month)
            existing = db.execute('SELECT digest FROM knowledge_notes WHERE month=?', (month,)).fetchone()
            if os.path.lexists(path):
                if existing is None or path.is_symlink() or not path.is_file():
                    raise RecordError('摘要文件未登记或路径不安全，请先检查')
                if hashlib.sha256(path.read_bytes()).hexdigest() != existing[0]:
                    raise RecordError('摘要已被外部修改，请先检查')
            else:
                content = render_note(month, summary['body'], summary['generatedAt'])
                created_digest = publish_new_note(vault, month, content)
                db.execute('INSERT OR REPLACE INTO knowledge_notes VALUES (?,?)', (month, created_digest))
            if not indexer(vault, path):
                raise RecordError('摘要索引未完成')
        return path
    except BaseException:
        if created_digest:
            try:
                invalidate_owned_note(vault, month, created_digest)
            except (NoteError, OSError):
                pass  # An orphan fails closed on the next source mutation.
        raise
