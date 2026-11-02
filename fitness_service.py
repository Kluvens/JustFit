"""Explicitly started, private HTTPS transport for one paired fitness app.

The service has no discovery, cloud dependencies, or background persistence.
Keys are generated locally; the caller must convey both secret and certificate
fingerprint to the phone over a trusted pairing channel.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import ssl
import subprocess
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fitness_store import RecordError, backup_database, connect, database_lock, ingest, invalidate_projection, manifest, projection_vault, publish_summary_projection, reconcile_missing_projections, refresh_monthly_summary
from fitness_knowledge import index_note

MAX_BODY = 1024 * 1024


@contextmanager
def identity_lock(directory: Path, exclusive: bool):
    fd = os.open(directory / 'identity.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def make_identity(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    with identity_lock(directory, exclusive=True):
        return _make_identity_locked(directory)


def _make_identity_locked(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    key = directory / 'server-key.pem'
    cert = directory / 'server-cert.pem'
    secret_file = directory / 'pairing-secret'
    if not key.exists() and not cert.exists() and not secret_file.exists():
        old_umask = os.umask(0o077)
        try:
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-keyout', str(key), '-out', str(cert), '-days', '365',
                            '-subj', '/CN=Fitness Local Sync'], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as file:
                file.write(secrets.token_urlsafe(32))
        finally:
            os.umask(old_umask)
    if not all(p.is_file() for p in (key, cert, secret_file)):
        raise RuntimeError('配对身份不完整；请保留现有文件并检查')
    for path in (key, cert, secret_file):
        os.chmod(path, 0o600)
    secret = secret_file.read_text().strip()
    if len(secret) < 32:
        raise RuntimeError('配对密钥无效')
    cert_der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    return key, cert, secret, hashlib.sha256(cert_der).hexdigest()


def rotate_secret(directory: Path):
    """Revoke all previous clients before pairing a replacement phone."""
    with identity_lock(directory, exclusive=True):
        target = directory / 'pairing-secret'
        if not target.is_file():
            raise RuntimeError('需要先创建现有配对身份')
        fd, temporary = tempfile.mkstemp(prefix='.pairing-', dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as output:
                output.write(secrets.token_urlsafe(32))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def receipt(db, batch, nonce):
    results = []
    for record in batch:
        identity = record.get('id') if isinstance(record, dict) else None
        version = record.get('version') if isinstance(record, dict) else None
        try:
            status = ingest(db, record)
            results.append({'id': identity, 'version': version, 'status': status})
        except RecordError as exc:
            results.append({'id': identity, 'version': version, 'status': 'error', 'message': str(exc)})
    reconcile_missing_projections(db)
    latest = db.execute('SELECT MAX(generated_at) FROM monthly_summaries').fetchone()[0]
    pending = db.execute('SELECT COUNT(*) FROM stale_months').fetchone()[0]
    return {'schema': 'fitness-receipt-v2', 'nonce': nonce, 'results': results, 'manifest': manifest(db),
            'summaryStatus': {'lastGeneratedAt': latest, 'pendingMonths': pending}}


def recovery_snapshot(db, nonce):
    # Read the records and tombstones in a single SQLite snapshot.
    with db:
        db.execute('BEGIN')
        values = [json.loads(row[0]) for row in db.execute('SELECT payload FROM records ORDER BY id')]
    return {'schema': 'fitness-recovery-v2', 'nonce': nonce, 'records': values}


def authorize_device(db, secret, device_id):
    """Bind this pairing generation to the first phone that uses it."""
    try:
        canonical = str(uuid.UUID(device_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('device id') from None
    epoch = hashlib.sha256(secret.encode('utf-8')).hexdigest()
    with db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS fitness_authority (slot INTEGER PRIMARY KEY CHECK(slot=1), epoch TEXT NOT NULL, device TEXT NOT NULL)')
        current = db.execute('SELECT epoch, device FROM fitness_authority WHERE slot=1').fetchone()
        if current is None or current[0] != epoch:
            db.execute('INSERT OR REPLACE INTO fitness_authority VALUES (1,?,?)', (epoch, canonical))
            return True
        return hmac.compare_digest(current[1], canonical)


class Service(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # A dropped connection is expected; do not print peer addresses or
        # exception traces that could contain private request details.
        return

    def __init__(self, address, db_path, secret, cert, key):
        self.db_path = str(db_path)
        self.secret = secret
        self.secret_path = Path(cert).parent / 'pairing-secret'
        self.summary_lock = threading.Lock()
        self.summary_worker = None
        super().__init__(address, Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(cert), str(key))
        self.socket = context.wrap_socket(self.socket, server_side=True)

    def schedule_summary_refresh(self, months):
        if not months:
            return
        with self.summary_lock:
            if self.summary_worker and self.summary_worker.is_alive():
                return  # Remaining stale months will be retried on a later sync.
            self.summary_worker = threading.Thread(target=self._refresh_summaries, args=(months,), daemon=True)
            self.summary_worker.start()

    def _refresh_summaries(self, months):
        for month in months:
            try:
                with database_lock(self.db_path), closing(connect(self.db_path)) as db:
                    if not db.execute('SELECT 1 FROM stale_months WHERE month=?', (month,)).fetchone():
                        continue
                    try:
                        refresh_monthly_summary(db, month)
                        backup_database(self.db_path)
                        if projection_vault(db) is not None:
                            publish_summary_projection(db, month, index_note)
                            backup_database(self.db_path)
                    except Exception:
                        # Invalidate any published projection before marking retry.
                        with db:
                            db.execute('BEGIN IMMEDIATE')
                            invalidate_projection(db, {month})
                            db.execute('INSERT OR IGNORE INTO stale_months VALUES (?)', (month,))
                            db.execute("UPDATE monthly_summaries SET body='' WHERE month=?", (month,))
                        raise
            except Exception:
                # No private values in logs; the stale month remains retryable.
                continue


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # Do not log tokens, health data, paths or network identifiers.
        return

    def reply(self, code, value):
        data = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_POST(self):
        with identity_lock(self.server.secret_path.parent, exclusive=False):
            self.handle_post()

    def handle_post(self):
        pending_before = []
        supplied = self.headers.get('Authorization', '')
        expected = 'Bearer ' + self.server.secret
        try:
            current = self.server.secret_path.read_text().strip()
        except OSError:
            current = ''
        if not hmac.compare_digest(current.encode(), self.server.secret.encode()) or not hmac.compare_digest(supplied.encode('utf-8'), expected.encode('utf-8')):
            self.reply(401, {'error': '设备未认证'})
            return
        try:
            size = int(self.headers.get('Content-Length', ''))
            if not 0 < size <= MAX_BODY:
                raise ValueError('size')
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict) or not isinstance(payload.get('nonce'), str) or not 16 <= len(payload['nonce']) <= 128:
                raise ValueError('nonce')
            if self.path == '/sync':
                batch = payload.get('records')
                if not isinstance(batch, list) or len(batch) > 1000:
                    raise ValueError('records')
                with database_lock(self.server.db_path), closing(connect(self.server.db_path)) as db:
                    if not authorize_device(db, self.server.secret, payload.get('deviceId')):
                        self.reply(403, {'error': '另一台设备已配对；请在 Mac 上重新配对'})
                        return
                    pending_before = [row[0] for row in db.execute('SELECT month FROM stale_months ORDER BY month')]
                    result = receipt(db, batch, payload['nonce'])
                    backup_database(self.server.db_path)
            elif self.path == '/recovery':
                with database_lock(self.server.db_path), closing(connect(self.server.db_path)) as db:
                    if not authorize_device(db, self.server.secret, payload.get('deviceId')):
                        self.reply(403, {'error': '另一台设备已配对；请在 Mac 上重新配对'})
                        return
                    result = recovery_snapshot(db, payload['nonce'])
            else:
                self.reply(404, {'error': '接口不存在'})
                return
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self.reply(400, {'error': '请求格式无效'})
            return
        except Exception:
            self.reply(500, {'error': '本地接收失败，请重试'})
            return
        self.reply(200, result)
        if self.path == '/sync':
            self.server.schedule_summary_refresh(pending_before)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--rotate-pairing', action='store_true')
    args = parser.parse_args()
    key, cert, secret, fingerprint = make_identity(args.directory)
    if args.rotate_pairing:
        rotate_secret(args.directory)
        key, cert, secret, fingerprint = make_identity(args.directory)
    server = Service((args.host, args.port), args.db, secret, cert, key)
    # No pairing secret is printed or placed in logs; a future Mac companion
    # must show it only in an explicit local pairing interface.
    print(json.dumps({'port': server.server_port, 'certificate_sha256': fingerprint}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
