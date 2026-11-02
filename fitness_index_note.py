"""Validate and index one generated health note using the vault runtime."""
import json
from pathlib import Path
import sys


def main():
    if len(sys.argv) != 3:
        return 2
    vault = Path(sys.argv[1]).resolve()
    note = Path(sys.argv[2]).resolve()
    try:
        note.relative_to(vault / '11_Health' / 'Fitness_Auto')
    except ValueError:
        return 2
    sys.path.insert(0, str(vault / '90_System' / 'pipeline'))
    from note_contracts import check_text
    from build_system import index_document_path
    if check_text(note.read_text(encoding='utf-8'), require_contract=True, vault=vault):
        return 1
    result = index_document_path(vault, note)
    print(json.dumps({'indexed': bool(result.get('indexed'))}))
    return 0 if result.get('indexed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
