"""Restore missing INN and corporate social metadata without restarting research."""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.db import Session, engine
from app.entity_audit import audit_import
from app.entity_repair import (missing_legal_metadata, missing_social_metadata,
                               restore_missing_legal_metadata, restore_missing_social_metadata)


def backup_sqlite():
    if engine.url.get_backend_name() != 'sqlite':
        raise ValueError('Automatic backup supports SQLite only')
    path = Path(engine.url.database).resolve()
    folder = path.parent / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    backup = folder / f'{path.stem}-before-import-metadata-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.db'
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_id')
    parser.add_argument('--apply', action='store_true', help='Back up SQLite and restore missing import metadata')
    args = parser.parse_args()
    with Session() as db:
        missing_legal = missing_legal_metadata(db, args.run_id)
        missing_social = missing_social_metadata(db, args.run_id)
        result = {'run_id': args.run_id, 'legal_groups_to_restore': len(missing_legal),
                  'social_groups_to_restore': len(missing_social), 'applied': False}
        if args.apply and (missing_legal or missing_social):
            backup = backup_sqlite()
            legal_count = restore_missing_legal_metadata(db, args.run_id) if missing_legal else 0
            social_count = restore_missing_social_metadata(db, args.run_id) if missing_social else 0
            db.commit()
            after = audit_import(db, args.run_id)
            if after['groups_missing_stored_legal_ids'] or after['groups_missing_stored_social_links']:
                raise RuntimeError('Metadata repair did not pass post-check')
            result.update({'applied': True, 'legal_groups_restored': legal_count,
                           'social_groups_restored': social_count, 'backup': str(backup)})
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
