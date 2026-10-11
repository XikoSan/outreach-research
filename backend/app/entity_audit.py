"""Read-only audit of the workbook-to-company import boundary."""
from collections import Counter, defaultdict
from datetime import date, datetime, time
from hashlib import sha256
import json
from pathlib import Path

from .db import Company, Run, SourceRow
from .workbooks import clean, corporate_social_columns, corporate_socials, fingerprint, preflight

LEGAL_HEADERS = ('инн', 'tax id', 'инн компании')


def _cell_field(cells, headers, names):
    for name in names:
        for index, header in enumerate(headers):
            if header.casefold() == name:
                cell = cells[index]
                value = clean(cell.hyperlink.target if cell.hyperlink else cell.value)
                if value:
                    return value
    return ''


def _legacy_fingerprint(cells):
    values = []
    for cell in cells:
        value = cell.value
        if isinstance(value, (datetime, date, time)):
            value = value.isoformat()
        values.append([cell.data_type, type(cell.value).__name__, value])
    return sha256(json.dumps(values, ensure_ascii=False, default=str).encode()).hexdigest()


def audit_import(db, run_id):
    """Return aggregate integrity checks; never include source/contact values."""
    run = db.get(Run, run_id)
    if run is None:
        raise ValueError('Research not found')
    companies = db.query(Company).filter_by(run_id=run_id).all()
    rows = db.query(SourceRow).filter_by(run_id=run_id).all()
    company_by_id = {company.id: company for company in companies}
    source_path = Path(run.source_path)
    source_hash_matches = source_path.is_file() and sha256(source_path.read_bytes()).hexdigest() == run.source_hash
    if not source_hash_matches:
        raise ValueError('Source workbook missing or changed; audit cannot compare it')

    workbook, sheets = preflight(source_path)
    source_rows = {
        (sheet.title, cells[0].row): {
            'fingerprints': (fingerprint(cells), _legacy_fingerprint(cells)),
            'legal': _cell_field(cells, headers, LEGAL_HEADERS),
            'socials': corporate_socials(cells, corporate_social_columns(headers)),
        }
        for sheet, headers, _, _, sheet_rows in sheets
        for cells in sheet_rows
    }
    workbook.close()
    stored_coordinates = [(row.sheet, row.row_index) for row in rows]
    stored_by_company = defaultdict(list)
    for row in rows:
        stored_by_company[row.company_id].append(row)

    conflict_groups = 0
    missing_legal_context_groups = 0
    incorrect_conflict_flags = 0
    context_legal_mismatches = 0
    groups_with_legal_ids = 0
    legal_on_different_resources = defaultdict(set)
    social_source_groups = 0
    missing_social_groups = 0
    missing_social_links = 0
    planned_groups_missing_socials = 0
    researched_groups_missing_socials = 0
    for company in companies:
        legal_ids = {source_rows.get((row.sheet, row.row_index), {}).get('legal', '')
                     for row in stored_by_company[company.id]}
        legal_ids.discard('')
        groups_with_legal_ids += bool(legal_ids)
        if len(legal_ids) > 1:
            conflict_groups += 1
        context_ids = set(company.context.get('legal_ids', []))
        if legal_ids and not context_ids:
            missing_legal_context_groups += 1
        if context_ids and context_ids != legal_ids:
            context_legal_mismatches += 1
        if context_ids and bool(company.context.get('identity_conflict')) != (len(context_ids) > 1):
            incorrect_conflict_flags += 1
        for legal_id in legal_ids:
            legal_on_different_resources[legal_id].add(company.resource)
        social_links = {link for row in stored_by_company[company.id]
                        for link in source_rows.get((row.sheet, row.row_index), {}).get('socials', [])}
        social_source_groups += bool(social_links)
        missing_socials = social_links - set(company.context.get('socials', []))
        if missing_socials:
            missing_social_groups += 1
            missing_social_links += len(missing_socials)
            planned_groups_missing_socials += bool(company.context.get('acquisition_plan'))
            researched_groups_missing_socials += bool(company.research)

    group_sizes = Counter(row.company_id for row in rows)
    fingerprint_formats = Counter(
        'missing' if (source := source_rows.get((row.sheet, row.row_index))) is None
        else 'current' if row.fingerprint == source['fingerprints'][0]
        else 'legacy' if row.fingerprint == source['fingerprints'][1]
        else 'invalid'
        for row in rows
    )
    result = {
        'run_id': run_id,
        'source_hash_matches': source_hash_matches,
        'source_rows': len(source_rows),
        'stored_rows': len(rows),
        'declared_rows': run.row_count,
        'stored_companies': len(companies),
        'declared_companies': run.company_count,
        'multi_row_groups': sum(size > 1 for size in group_sizes.values()),
        'max_rows_in_group': max(group_sizes.values(), default=0),
        'rows_with_missing_company': sum(row.company_id not in company_by_id for row in rows),
        'duplicate_source_coordinates': len(stored_coordinates) - len(set(stored_coordinates)),
        'source_coordinates_missing_from_db': len(source_rows.keys() - set(stored_coordinates)),
        'db_coordinates_missing_from_source': len(set(stored_coordinates) - source_rows.keys()),
        'fingerprint_formats': dict(fingerprint_formats),
        'fingerprint_mismatches': fingerprint_formats['invalid'],
        'groups_with_legal_ids_in_source': groups_with_legal_ids,
        'groups_with_multiple_legal_ids_in_source': conflict_groups,
        'groups_missing_stored_legal_ids': missing_legal_context_groups,
        'groups_with_different_stored_legal_ids': context_legal_mismatches,
        'groups_with_incorrect_conflict_flag': incorrect_conflict_flags,
        'legal_ids_across_multiple_resources': sum(len(resources) > 1 for resources in legal_on_different_resources.values()),
        'groups_without_resource': sum(company.resource.startswith('unresolved:') for company in companies),
        'groups_with_social_links_in_source': social_source_groups,
        'groups_missing_stored_social_links': missing_social_groups,
        'missing_unique_social_links': missing_social_links,
        'planned_groups_missing_social_links': planned_groups_missing_socials,
        'researched_groups_missing_social_links': researched_groups_missing_socials,
    }
    result['row_integrity_ok'] = all(result[key] == 0 for key in (
        'rows_with_missing_company', 'duplicate_source_coordinates',
        'source_coordinates_missing_from_db', 'db_coordinates_missing_from_source',
        'fingerprint_mismatches',
    )) and result['source_rows'] == result['stored_rows'] == result['declared_rows'] \
        and result['stored_companies'] == result['declared_companies']
    return result
