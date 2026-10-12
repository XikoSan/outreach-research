"""Restore import-only legal metadata from the unchanged source workbook."""
from collections import defaultdict

from .db import Company, Run, SourceRow
from .entity_audit import LEGAL_HEADERS, LEGAL_NAME_HEADERS, _cell_field, audit_import
from .workbooks import normalize_inn, preflight


def missing_legal_metadata(db, run_id):
    report = audit_import(db, run_id)
    if not report['row_integrity_ok'] or report['groups_with_different_stored_legal_ids']:
        raise ValueError('Import integrity failed; automatic metadata repair is unsafe')
    if report['groups_with_multiple_legal_ids_in_source']:
        raise ValueError('Legal-ID conflicts require review before metadata repair')
    run = db.get(Run, run_id)
    workbook, sheets = preflight(run.source_path)
    legal_by_coordinate = {
        (sheet.title, cells[0].row): normalize_inn(_cell_field(cells, headers, LEGAL_HEADERS))
        for sheet, headers, _, _, sheet_rows in sheets
        for cells in sheet_rows
    }
    workbook.close()
    legal_by_company = defaultdict(set)
    for row in db.query(SourceRow).filter_by(run_id=run_id):
        value = legal_by_coordinate[(row.sheet, row.row_index)]
        if value:
            legal_by_company[row.company_id].add(value)
    missing = {}
    for company in db.query(Company).filter_by(run_id=run_id):
        values = legal_by_company[company.id]
        if values and not company.context.get('legal_ids'):
            missing[company.id] = sorted(values)
    if len(missing) != report['groups_missing_stored_legal_ids']:
        raise ValueError('Import changed during metadata audit')
    return missing


def restore_missing_legal_metadata(db, run_id):
    missing = missing_legal_metadata(db, run_id)
    for company_id, legal_ids in missing.items():
        company = db.get(Company, company_id)
        company.context = {**company.context, 'legal_ids': legal_ids, 'identity_conflict': False}
    return len(missing)


def missing_social_metadata(db, run_id):
    report = audit_import(db, run_id)
    if (not report['row_integrity_ok'] or report['planned_groups_missing_social_links']
            or report['researched_groups_missing_social_links']):
        raise ValueError('Existing acquisition plans or import integrity require manual review')
    run = db.get(Run, run_id)
    workbook, sheets = preflight(run.source_path)
    from .workbooks import corporate_social_columns, corporate_socials
    social_by_coordinate = {
        (sheet.title, cells[0].row): corporate_socials(cells, corporate_social_columns(headers))
        for sheet, headers, _, _, sheet_rows in sheets
        for cells in sheet_rows
    }
    workbook.close()
    socials_by_company = defaultdict(list)
    for row in db.query(SourceRow).filter_by(run_id=run_id).order_by(SourceRow.sheet, SourceRow.row_index):
        for link in social_by_coordinate[(row.sheet, row.row_index)]:
            if link not in socials_by_company[row.company_id]:
                socials_by_company[row.company_id].append(link)
    missing = {}
    for company in db.query(Company).filter_by(run_id=run_id):
        absent = [link for link in socials_by_company[company.id]
                  if link not in company.context.get('socials', [])]
        if absent:
            missing[company.id] = absent
    if len(missing) != report['groups_missing_stored_social_links']:
        raise ValueError('Import changed during social metadata audit')
    return missing


def restore_missing_social_metadata(db, run_id):
    missing = missing_social_metadata(db, run_id)
    for company_id, links in missing.items():
        company = db.get(Company, company_id)
        company.context = {**company.context, 'socials': [*company.context.get('socials', []), *links]}
    return len(missing)


def missing_legal_names(db, run_id):
    report = audit_import(db, run_id)
    if not report['row_integrity_ok'] or report['groups_with_different_stored_legal_names']:
        raise ValueError('Import integrity or stored legal names require manual review')
    run = db.get(Run, run_id)
    workbook, sheets = preflight(run.source_path)
    names_by_coordinate = {
        (sheet.title, cells[0].row): _cell_field(cells, headers, LEGAL_NAME_HEADERS)
        for sheet, headers, _, _, sheet_rows in sheets
        for cells in sheet_rows
    }
    workbook.close()
    names_by_company = defaultdict(set)
    for row in db.query(SourceRow).filter_by(run_id=run_id):
        value = names_by_coordinate[(row.sheet, row.row_index)]
        if value:
            names_by_company[row.company_id].add(value)
    missing = {}
    for company in db.query(Company).filter_by(run_id=run_id):
        values = names_by_company[company.id]
        if values and not company.context.get('legal_names'):
            missing[company.id] = sorted(values)
    if len(missing) != report['groups_missing_stored_legal_names']:
        raise ValueError('Import changed during legal-name audit')
    return missing


def restore_missing_legal_names(db, run_id):
    missing = missing_legal_names(db, run_id)
    for company_id, names in missing.items():
        company = db.get(Company, company_id)
        company.context = {**company.context, 'legal_names': names}
    return len(missing)
