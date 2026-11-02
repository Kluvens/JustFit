"""Safe file operations for generated fitness notes; no indexing or implicit writes."""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import subprocess
import stat
import tempfile
from pathlib import Path


class NoteError(ValueError):
    pass


def note_path(vault, month):
    try:
        day = dt.date.fromisoformat(month + '-01')
        if day.strftime('%Y-%m') != month:
            raise ValueError('noncanonical')
    except (ValueError, TypeError):
        raise NoteError('月份必须为 YYYY-MM') from None
    return Path(vault).resolve() / '11_Health' / 'Fitness_Auto' / (month + '.md')


def _sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def render_note(month, narrative, generated_at):
    day = dt.datetime.fromisoformat(generated_at).date().isoformat()
    note_path('/', month)  # validate month before interpolating it
    return (f'---\nnote_contract: note-v1\ntype: important-post\nstatus: active\n'
            f'origin: ai-draft\nsource: "90_System/local/fitness/fitness.sqlite3"\n'
            f'source_authority: working-note\nrecorded_at: "{day}"\nlast_updated: "{day}"\n'
            f'areas: [health]\nsensitivity: high\nretrieval_profile: health\n---\n'
            f'# {month} 健身训练摘要\n\n## 核心观点\n\n'
            f'本摘要由已收录训练记录生成；具体事实以当前结构化记录为准。\n\n'
            f'## 提炼笔记\n\n{narrative}\n')


def index_note(vault, note):
    """Use the vault's local Python environment; never emit note contents."""
    python = Path(vault) / '90_System' / 'local' / 'venv' / 'bin' / 'python'
    helper = Path(__file__).with_name('fitness_index_note.py')
    if not python.is_file() or not helper.is_file():
        return False
    try:
        result = subprocess.run([str(python), str(helper), str(vault), str(note)],
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def publish_new_note(vault, month, content):
    """Create one private UTF-8 note, failing closed if a target already exists.

    Return its digest for the caller to persist before requesting indexing.
    The caller must serialize this operation with workout updates.
    """
    path = note_path(vault, month)
    root = Path(vault).resolve()
    for directory in (root / '11_Health', path.parent):
        if directory.is_symlink():
            raise NoteError('摘要目录包含符号链接')
        directory.mkdir(exist_ok=True, mode=0o700)
    # Reject symlinked directory components to keep the vault boundary intact.
    if path.parent.resolve() != path.parent or not path.parent.resolve().is_relative_to(root):
        raise NoteError('摘要目录包含符号链接或越过知识库边界')
    data = content.encode('utf-8')
    fd, temporary = tempfile.mkstemp(prefix='.fitness-note-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        # Hard-link is an atomic create-if-absent; it never replaces a note.
        os.link(temporary, path, follow_symlinks=False)
        _sync_directory(path.parent)
    finally:
        os.unlink(temporary)
    return hashlib.sha256(data).hexdigest()


def verify_owned_note(vault, month, expected_digest):
    """Return whether an unchanged owned file is present, without modifying it."""
    path = note_path(vault, month)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or path.parent.resolve() != path.parent:
        raise NoteError('摘要路径不是可信的普通文件')
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_digest:
        raise NoteError('摘要已被外部修改，未删除；请先检查')
    return True


def invalidate_owned_note(vault, month, expected_digest):
    """Remove only an unchanged owned note before accepting a source edit."""
    if not verify_owned_note(vault, month, expected_digest):
        return False
    path = note_path(vault, month)
    path.unlink()
    _sync_directory(path.parent)
    return True
