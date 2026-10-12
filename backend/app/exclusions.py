"""Evidence-bound reasons to remove a company from an outreach audience.

No segment match, a failed fetch, or an ambiguous identity is never a reason.
"""
import re
from urllib.parse import urlsplit

from .db import now
from .evidence import VERSION as EVIDENCE_VERSION
from .schemas import selected_directions

EXCLUDED = 'EXCLUDED'
TERMINAL = {'NO_RESULTS'}
REASONS = {
    'NO_CURRENT_SOURCES': 'После проверки выбранных направлений не найдено доступных актуальных источников',
    'CEASED_OPERATIONS': 'Официальный источник прямо сообщает о прекращении деятельности компании',
    'DATABASE_MISMATCH': 'Данные официального источника противоречат записи в исходной базе',
}
CLOSURE = re.compile(r'\b(?:ликвидирован(?:а|о)?|прекратил(?:а|о)?\s+деятельность|деятельность\s+прекращена)\b', re.I)
TAX_ID = re.compile(r'\bИНН\s*[:№]?\s*(\d{10}|\d{12})\b', re.I)


def evidence_for_exclusion(company):
    return [item for report in company.research.values() for item in report.get('evidence', [])
            if item.get('validation_version') == EVIDENCE_VERSION
            and item.get('subject_verified') and item.get('current')
            and item.get('scope') == 'company' and item.get('quote')]


def same_origin(left, right):
    left_host = (urlsplit(left).hostname or '').lower().removeprefix('www.')
    right_host = (urlsplit(right).hostname or '').lower().removeprefix('www.')
    return bool(left_host and right_host and left_host == right_host)


def no_current_sources(company, run):
    if company.identity != 'CONFIRMED' or company.stage != 'PROFILE_READY':
        return False
    directions = selected_directions(run.config)
    if not directions:
        return False
    if company.context.get('socials') and 'social' not in directions:
        return False  # An official fallback remains unexamined.
    if company.context.get('initial_snapshot') or any(report.get('pages') for report in company.research.values()):
        return False
    if run.config.get('methodology_version') == 'campaign-v1':
        base = company.research.get('base', {})
        return (directions == {'website'} and base.get('state') == 'COMPLETE'
                and base.get('termination_reason') == 'sources_exhausted'
                and bool(base.get('attempted_urls')) and not base.get('errors'))
    return all(company.research.get(direction, {}).get('state') in TERMINAL
               and company.research[direction].get('coverage') == 'checked'
               for direction in directions)


def ceased_operations(company):
    names = [name.casefold().strip() for name in [company.name, *company.context.get('names', []),
                                                   *company.context.get('legal_names', [])]
             if len(name.strip()) >= 5]
    for item in evidence_for_exclusion(company):
        quote = item['quote'].casefold()
        match = CLOSURE.search(quote)
        if not match or not any(name in quote for name in names):
            continue
        if re.search(r'\bне\s*$', quote[max(0, match.start()-12):match.start()]):
            continue
        if same_origin(item.get('url', ''), company.resource):
            return [item]
    return []


def database_mismatch(company):
    declared = {re.sub(r'\D', '', value) for value in company.context.get('legal_ids', [])}
    declared = {value for value in declared if len(value) in (10, 12)}
    if not declared:
        return []
    for report in company.research.values():
        for page in report.get('pages', []):
            if not same_origin(page.get('url', ''), company.resource):
                continue
            found = set(TAX_ID.findall(page.get('text', '')))
            if len(found) == 1 and found.isdisjoint(declared):
                return [{'id': 'page:' + (page.get('content_hash') or page.get('url', '')),
                         'url': page.get('url', ''), 'quote': 'ИНН на официальной странице не совпадает с ИНН в исходной базе'}]
    return []


def automatic_reason(company, run):
    if company.identity != 'CONFIRMED' or company.stage != 'PROFILE_READY' or company.profile.get('exclusion_override') == 'include':
        return None
    if matches := ceased_operations(company):
        return 'CEASED_OPERATIONS', matches
    if matches := database_mismatch(company):
        return 'DATABASE_MISMATCH', matches
    if no_current_sources(company, run):
        return 'NO_CURRENT_SOURCES', []
    return None


def set_excluded(company, code, sources, *, source='automatic', note=''):
    previous = {'group': company.group, 'reason': company.reason}
    detail = {'code': code, 'reason': REASONS[code], 'source': source,
              'evidence_ids': [item['id'] for item in sources],
              'urls': list(dict.fromkeys(item.get('url', '') for item in sources if item.get('url'))),
              'note': note.strip(), 'previous': previous, 'saved_at': now()}
    company.profile = {**company.profile, 'exclusion': detail}
    company.group = EXCLUDED
    company.reason = detail['reason'] + (': ' + detail['note'] if detail['note'] else '')
    return detail
