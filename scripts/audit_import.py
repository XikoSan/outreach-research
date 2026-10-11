"""Read-only aggregate audit of an imported XLSX; no API calls or contact output."""
import argparse
import json

from app.db import Session
from app.entity_audit import audit_import


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_id')
    args = parser.parse_args()
    with Session() as db:
        report = audit_import(db, args.run_id)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
