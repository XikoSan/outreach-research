"""Follow-up lead profiles and explicit report/letter operations."""
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from .crawler import normalize_url, SourceUnavailable
from sqlalchemy import or_, String, cast
from sqlalchemy.exc import IntegrityError
from .config import settings
from .db import Session, Run, Company, SourceRow, Lead, uid, now, log
from . import credentials

router = APIRouter(prefix='/api')


def _get(db, cls, identifier):
    item = db.get(cls, identifier)
    if item is None:
        raise HTTPException(404, 'Не найдено')
    return item


def _field(row, hints):
    for cell in row.display_fields:
        name = str(cell.get('name', '')).casefold()
        if any(h in name for h in hints):
            value = str(cell.get('value', '')).strip()
            if value:
                return value[:300]
    return ''


def _contact(row):
    return {
        'name': _field(row, ('имя', 'фио', 'контактное лицо', 'full name', 'contact name')),
        'title': _field(row, ('должност', 'позици', 'position', 'job title', 'role')),
        'email': _field(row, ('email', 'e-mail', 'почта', 'электронн')),
    }


def _view(db, lead, detail=False):
    run = db.get(Run, lead.research_run_id)
    company = db.get(Company, lead.company_id)
    result = {
        'id': lead.id, 'source_run_id': lead.source_run_id,
        'company_id': lead.company_id, 'company_name': company.name,
        'company_site': company.resource, 'source_row_id': lead.source_row_id,
        'respondent': lead.respondent, 'depth': lead.depth, 'status': run.status,
        'stage': run.stage, 'error': run.error, 'spent': run.spent, 'reserved': run.reserved,
        'created_at': lead.created_at, 'updated_at': lead.updated_at,
        'letter_status': lead.letter_status,
    }
    if detail:
        result.update(reply=lead.reply, offer=lead.offer, original_message=(run.context_snapshot or {}).get('original_message', ''),
                      additional_sites=(run.context_snapshot or {}).get('additional_sites', []), report=lead.report,
                      letter=lead.letter, pages=lead.pages,
                      selected_recommendation=lead.selected_recommendation,
                      report_versions=[{'version': index + 1, 'checked_at': item.get('report', {}).get('checked_at', ''),
                                        'coverage': item.get('report', {}).get('coverage', '')}
                                       for index, item in enumerate((run.reports or {}).get('lead_report_versions', []))],
                      connection=credentials.status())
    return result


@router.get('/lead-bases')
def lead_bases():
    with Session() as db:
        runs = db.query(Run).filter(Run.company_count > 0).order_by(Run.created_at.desc()).all()
        runs = [r for r in runs if r.config.get('workflow') != 'lead-research-v1']
        return [{'id': r.id, 'name': r.name, 'company_count': r.company_count,
                 'status': r.status, 'offer': (r.context_snapshot or {}).get('source_text', '')} for r in runs]


@router.get('/lead-candidates')
def lead_candidates(run_id: str, q: str = Query('', min_length=2, max_length=150)):
    q = q.strip()
    if len(q) < 2:
        return []
    with Session() as db:
        run = _get(db, Run, run_id)
        if run.company_count < 1 or run.config.get('workflow') == 'lead-research-v1':
            raise HTTPException(404, 'База не найдена')
        query = db.query(SourceRow, Company).join(Company, Company.id == SourceRow.company_id).filter(
            SourceRow.run_id == run_id,
            or_(Company.name.ilike('%' + q + '%'), cast(SourceRow.display_fields, String).ilike('%' + q + '%')))
        result = []
        for row, company in query.order_by(Company.name, SourceRow.row_index).limit(80):
            contact = _contact(row)
            if q.casefold() not in company.name.casefold() and q.casefold() not in contact['email'].casefold():
                continue
            result.append({'source_row_id': row.id, 'company_id': company.id,
                           'company_name': company.name, 'site': company.resource,
                           'contact': contact, 'existing_lead': db.query(Lead.id).filter_by(company_id=company.id).first() is not None})
            if len(result) >= 30:
                break
        return result


class LeadCreate(BaseModel):
    source_row_id: str
    reply: str = Field(min_length=8, max_length=20000)
    offer: str = Field(min_length=10, max_length=100000)
    depth: str = 'standard'
    respondent_name: str = Field(default='', max_length=200)
    respondent_title: str = Field(default='', max_length=200)
    respondent_email: str = Field(default='', max_length=320)
    original_message: str = Field(default='', max_length=20000)
    additional_sites: list[str] = Field(default_factory=list, max_length=5)


@router.post('/leads')
def create_lead(payload: LeadCreate):
    if payload.depth not in ('brief', 'standard', 'expanded'):
        raise HTTPException(422, 'Неизвестная глубина исследования')
    if not payload.reply.strip() or not payload.offer.strip():
        raise HTTPException(422, 'Добавьте ответ человека и оффер')
    try:
        additional_sites = list(dict.fromkeys(normalize_url(url.strip()) for url in payload.additional_sites if url.strip()))
    except SourceUnavailable as exc:
        raise HTTPException(422, str(exc)) from exc
    with Session() as db:
        row = _get(db, SourceRow, payload.source_row_id)
        company = _get(db, Company, row.company_id)
        source = _get(db, Run, row.run_id)
        if source.company_count < 1 or source.config.get('workflow') == 'lead-research-v1':
            raise HTTPException(422, 'Выберите базу первого этапа')
        if db.query(Lead.id).filter_by(company_id=company.id).first():
            raise HTTPException(409, 'Для этой компании уже создан профиль лида')
        original = _contact(row)
        respondent = {
            'name': payload.respondent_name.strip() or original['name'],
            'title': payload.respondent_title.strip() or original['title'],
            'email': payload.respondent_email.strip() or original['email'],
        }
        run_id = uid()
        run = Run(id=run_id, name='Лид: ' + company.name[:180], filename=source.filename,
                  source_path=source.source_path, source_hash=source.source_hash,
                  sender_snapshot={}, context_snapshot={'original_message': payload.original_message.strip(),
                                                        'additional_sites': additional_sites, 'research_revision': 1},
                  config={'workflow': 'lead-research-v1', 'pipeline_version': 4,
                          'models': {'discovery': settings.model_discovery,
                                     'extraction': settings.model_extraction,
                                     'segmentation': settings.model_segmentation,
                                     'prompt': settings.model_prompt},
                          'max_search_queries': 1},
                  status='QUEUED' if credentials.status()['configured'] else 'CREATED',
                  stage='COLLECTING', row_count=1, company_count=1, budget=0)
        lead = Lead(source_run_id=source.id, company_id=company.id,
                    source_row_id=row.id, research_run_id=run_id,
                    reply=payload.reply.strip(), offer=payload.offer.strip(),
                    respondent=respondent, depth=payload.depth)
        db.add(run)
        db.flush()
        db.add(lead)
        log(db, run_id, 'LEAD', 'Профиль заинтересованного лида создан', company.id)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, 'Для этой компании уже создан профиль лида')
        return _view(db, lead, True)


@router.get('/leads')
def list_leads(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)):
    with Session() as db:
        query = db.query(Lead)
        total = query.count()
        items = query.order_by(Lead.created_at.desc(), Lead.id).offset(offset).limit(limit).all()
        return {'total': total, 'items': [_view(db, lead) for lead in items]}


@router.get('/leads/{lead_id}')
def lead_detail(lead_id: str):
    with Session() as db:
        return _view(db, _get(db, Lead, lead_id), True)


@router.post('/leads/{lead_id}/start')
def start_lead(lead_id: str):
    with Session() as db:
        lead = _get(db, Lead, lead_id)
        run = _get(db, Run, lead.research_run_id)
        if run.status in ('QUEUED', 'RUNNING', 'PAUSING'):
            raise HTTPException(409, 'Обработка уже выполняется')
        if run.status == 'COMPLETED' and lead.report:
            raise HTTPException(409, 'Отчёт уже готов')
        if not credentials.for_run(run):
            raise HTTPException(409, 'Добавьте API-ключ в настройках')
        run.status = 'QUEUED'
        run.error = None
        db.commit()
        return _view(db, lead, True)


class LeadRerun(BaseModel):
    offer: str | None = Field(default=None, min_length=10, max_length=100000)
    original_message: str | None = Field(default=None, max_length=20000)
    additional_sites: list[str] | None = Field(default=None, max_length=5)
    depth: str | None = None


@router.post('/leads/{lead_id}/rerun')
def rerun_lead(lead_id: str, payload: LeadRerun | None = None):
    payload = payload or LeadRerun()
    if payload.depth is not None and payload.depth not in ('brief', 'standard', 'expanded'):
        raise HTTPException(422, 'Неизвестная глубина исследования')
    if payload.offer is not None and not payload.offer.strip():
        raise HTTPException(422, 'Оффер не может быть пустым')
    try:
        additional_sites = None if payload.additional_sites is None else list(dict.fromkeys(
            normalize_url(url.strip()) for url in payload.additional_sites if url.strip()))
    except SourceUnavailable as exc:
        raise HTTPException(422, str(exc)) from exc
    with Session() as db:
        lead = _get(db, Lead, lead_id)
        run = _get(db, Run, lead.research_run_id)
        if run.status != 'COMPLETED' or not lead.report:
            raise HTTPException(409, 'Повторить можно после готового исследования')
        if not credentials.for_run(run):
            raise HTTPException(409, 'Добавьте API-ключ в настройках')
        history = dict(run.reports or {})
        versions = list(history.get('lead_report_versions', []))
        versions.append({'report': lead.report, 'letter': lead.letter,
                         'offer': lead.offer, 'depth': lead.depth,
                         'context': run.context_snapshot, 'archived_at': now()})
        history['lead_report_versions'] = versions
        run.reports = history
        context = dict(run.context_snapshot or {})
        context['research_revision'] = int(context.get('research_revision', 1)) + 1
        if payload.original_message is not None:
            context['original_message'] = payload.original_message.strip()
        if additional_sites is not None:
            context['additional_sites'] = additional_sites
        run.context_snapshot = context
        if payload.offer is not None:
            lead.offer = payload.offer.strip()
        if payload.depth is not None:
            lead.depth = payload.depth
        config = dict(run.config)
        config['max_search_queries'] = int(config.get('max_search_queries', 1)) + 1
        run.config = config
        run.status = 'QUEUED'
        run.stage = 'COLLECTING'
        run.error = None
        lead.stage = 'COLLECTING'
        lead.letter = ''
        lead.letter_status = 'NOT_REQUESTED'
        lead.selected_recommendation = ''
        lead.updated_at = now()
        log(db, run.id, 'LEAD', 'Повторное исследование поставлено в очередь', lead.company_id,
            {'research_revision': context['research_revision']})
        db.commit()
        return _view(db, lead, True)


@router.post('/leads/{lead_id}/pause')
def pause_lead(lead_id: str):
    with Session() as db:
        lead = _get(db, Lead, lead_id)
        run = _get(db, Run, lead.research_run_id)
        if run.status == 'QUEUED':
            run.status = 'PAUSED'
        elif run.status == 'RUNNING':
            run.status = 'PAUSING'
        else:
            raise HTTPException(409, 'Обработка не выполняется')
        db.commit()
        return _view(db, lead, True)


class LetterRequest(BaseModel):
    recommendation_id: str = ''


@router.post('/leads/{lead_id}/letter')
def request_letter(lead_id: str, payload: LetterRequest):
    with Session() as db:
        lead = _get(db, Lead, lead_id)
        run = _get(db, Run, lead.research_run_id)
        if run.status != 'COMPLETED' or not lead.report:
            raise HTTPException(409, 'Сначала дождитесь отчёта')
        recommendations = lead.report.get('recommendations', [])
        rec_id = payload.recommendation_id or (recommendations[0]['id'] if recommendations else '')
        if rec_id and rec_id not in {item['id'] for item in recommendations}:
            raise HTTPException(422, 'Неизвестная рекомендация')
        if not credentials.for_run(run):
            raise HTTPException(409, 'Добавьте API-ключ в настройках')
        lead.selected_recommendation = rec_id
        lead.report = {**lead.report, 'selected_recommendation': rec_id}
        lead.letter_status = 'QUEUED'
        lead.letter = ''
        lead.stage = 'LETTER'
        lead.updated_at = now()
        run.stage = 'LETTER'
        run.status = 'QUEUED'
        run.error = None
        db.commit()
        return _view(db, lead, True)


def _report_markdown(lead, company, context=None, report=None):
    report = report if report is not None else (lead.report or {})
    context = context or {}
    lines = [f'# {company.name}', '', f'Проверено: {report.get("checked_at", "")}',
             f'Глубина: {lead.depth}', '', '## Контекст общения', '',
             'Ответивший: ' + ' · '.join(filter(None, [lead.respondent.get('name', ''),
                                                      lead.respondent.get('title', ''),
                                                      lead.respondent.get('email', '')])),
             '', lead.reply, '']
    if context.get('original_message'):
        lines += ['Исходное письмо:', '', context['original_message'], '']
    if report.get('quality_review'):
        lines += ['## Замечание проверки', '', report['quality_review'], '']
    lines += ['## Краткая картина компании', '', report.get('company_summary', ''), '']
    if report.get('coverage') == 'INCOMPLETE':
        lines += ['Исследование неполное: публичных сведений недостаточно для полной картины.', '']
    if report.get('sections'):
        for section in report['sections']:
            lines += ['## ' + section['topic'], '', section['text'], '',
                      'Основания: ' + ', '.join(section.get('evidence_ids', [])), '']
    else:
        lines += ['## Результаты исследования', '', report.get('findings', ''), '']
    if report.get('question_map'):
        lines += ['## Карта вопросов', '']
        for item in report['question_map']:
            lines += [f'- {item["question"]} — {item["status"]}: {item.get("answer", "")}' +
                      (f' (не выяснено: {item["remaining_gap"]})' if item.get('remaining_gap') else '')]
        lines += ['']
    if report.get('questions_for_client'):
        lines += ['## Вопросы для разговора', '']
        lines += ['- ' + value for value in report['questions_for_client']]
        lines += ['']
    lines += ['## Наблюдения и источники', '']
    for fact in report.get('facts', []):
        lines += [f'- {fact["id"]}: {fact["observation"]} — «{fact["quote"]}» ({fact["url"]})']
    if not report.get('facts'):
        lines += ['- Проверенных наблюдений нет.']
    lines += ['', '## Проверенные страницы', '']
    for source in report.get('sources', []):
        lines += [f'- {source.get("url", "")} — проверено {source.get("retrieved_at", "")}']
    if report.get('visual_portfolio_unreviewed'):
        lines += ['', 'На страницах есть изображения работ; их содержание автоматически не разбиралось.']
    lines += ['', '## Возможные направления сотрудничества', '']
    for item in report.get('recommendations', []):
        lines += [f'### {item["title"]}', '', item['rationale'], '', item['proposed_action'], '',
                  'Основания: ' + ', '.join(item.get('evidence_ids', [])),
                  'Цитата ответа: ' + item.get('reply_quote', ''),
                  'Неопределённость: ' + item.get('uncertainty', ''), '']
    if not report.get('recommendations'):
        lines += ['На доступных сведениях конкретный вариант пока не обоснован.', '']
    if report.get('limitations'):
        lines += ['## Ограничения', '']
        lines += ['- ' + value for value in report['limitations']]
    return '\n'.join(lines).strip() + '\n'


@router.get('/leads/{lead_id}/report.md')
def download_report(lead_id: str):
    with Session() as db:
        lead = _get(db, Lead, lead_id)
        if not lead.report:
            raise HTTPException(404, 'Отчёт ещё не готов')
        company = _get(db, Company, lead.company_id)
        context = (_get(db, Run, lead.research_run_id).context_snapshot or {})
        body = _report_markdown(lead, company, context)
        return PlainTextResponse(body, media_type='text/markdown; charset=utf-8',
                                 headers={'Content-Disposition': f'attachment; filename="lead-{lead.id}-report.md"'})


@router.get('/leads/{lead_id}/versions/{version}/report.md')
def download_report_version(lead_id: str, version: int):
    with Session() as db:
        lead = _get(db, Lead, lead_id)
        run = _get(db, Run, lead.research_run_id)
        versions = (run.reports or {}).get('lead_report_versions', [])
        if version < 1 or version > len(versions):
            raise HTTPException(404, 'Версия отчёта не найдена')
        item = versions[version - 1]
        company = _get(db, Company, lead.company_id)
        body = _report_markdown(lead, company, item.get('context', {}), item['report'])
        return PlainTextResponse(body, media_type='text/markdown; charset=utf-8',
                                 headers={'Content-Disposition': f'attachment; filename="lead-{lead.id}-report-v{version}.md"'})


@router.get('/leads/{lead_id}/letter.txt')
def download_letter(lead_id: str):
    with Session() as db:
        lead = _get(db, Lead, lead_id)
        if not lead.letter:
            raise HTTPException(404, 'Письмо ещё не подготовлено')
        return PlainTextResponse(lead.letter + '\n', media_type='text/plain; charset=utf-8',
                                 headers={'Content-Disposition': f'attachment; filename="lead-{lead.id}-letter.txt"'})
