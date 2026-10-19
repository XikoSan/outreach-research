"""Interested-lead research, independent from first-pass segmentation."""
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit
from pydantic import Field
from .db import Session, Lead, Run, Company, SourceRow, now, log
from .schemas import Strict
from .crawler import fetch, SourceUnavailable
from .llm import Gateway, RunPaused


DEPTHS = {
    'brief': {'pages': 4, 'site_links': 3, 'search': False, 'categories': ()},
    'standard': {'pages': 9, 'site_links': 3, 'search': True, 'categories': ('jobs', 'social', 'reviews')},
    'expanded': {'pages': 16, 'site_links': 6, 'search': True, 'categories': ('jobs', 'social', 'reviews', 'forums')},
}
CATEGORY_HINTS = {
    'jobs': ('ваканс', 'карьер', 'работа', 'job', 'career', 'hh.ru'),
    'social': ('vk.com', 't.me', 'linkedin.com', 'соцсет', 'telegram'),
    'reviews': ('отзыв', 'review', '2gis', 'maps', 'yandex.ru/maps'),
    'forums': ('форум', 'forum', 'обсуждени'),
}
PAGE_HINTS = ('о нас', 'компани', 'услуг', 'продукт', 'решени', 'about', 'service', 'product', 'case', 'кейс', 'направлен', 'команда')


class DiscoveredSource(Strict):
    url: str
    category: str
    reason: str


class DiscoveredSources(Strict):
    sources: list[DiscoveredSource] = Field(default_factory=list)


class ExtractedFact(Strict):
    source_index: int
    quote: str
    observation: str
    relevance: str


class ExtractedFacts(Strict):
    facts: list[ExtractedFact] = Field(default_factory=list)
    needs_more_sources: bool = True


class Recommendation(Strict):
    title: str
    rationale: str
    proposed_action: str
    evidence_ids: list[str] = Field(default_factory=list)
    reply_quote: str = ''
    uncertainty: str = ''


class LeadReport(Strict):
    company_summary: str
    expressed_interest: str
    findings: str
    limitations: list[str] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)


class LetterDraft(Strict):
    subject: str
    body: str


def _recent_page(page):
    for raw in page.get('date_metadata', []):
        try:
            published = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - timedelta(days=30) <= published <= datetime.now(timezone.utc) + timedelta(days=1):
                return True
        except ValueError:
            continue
    return False


def _words(value):
    return {x for x in re.findall(r'[a-zа-яё0-9]{5,}', value.casefold()) if x not in {'компания', 'компании', 'который', 'которые', 'предложение', 'сотрудничество'}}


def _same_host(url, root):
    try:
        host = (urlsplit(url).hostname or '').removeprefix('www.')
        base = (urlsplit(root).hostname or '').removeprefix('www.')
        return bool(host and base and host == base)
    except ValueError:
        return False


def _category(url, label=''):
    value = (url + ' ' + label).casefold()
    for category, hints in CATEGORY_HINTS.items():
        if any(h in value for h in hints):
            return category
    return 'website'


def _relevant_links(page, root, task_words, max_links):
    ranked = []
    for item in page.get('links', []):
        url = item.get('url', '')
        label = (item.get('label', '') + ' ' + urlsplit(url).path).casefold()
        score = sum(word in label for word in task_words) * 4 + sum(h in label for h in PAGE_HINTS)
        if score and (_same_host(url, root) or score >= 4 or _category(url, label) != 'website'):
            ranked.append((score, url))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [url for _, url in ranked[:max_links]]


def _set_stage(lead_id, stage):
    with Session() as db:
        lead = db.get(Lead, lead_id)
        run = db.get(Run, lead.research_run_id)
        if run.status != 'RUNNING':
            raise RunPaused()
        lead.stage = stage
        lead.updated_at = now()
        run.stage = stage
        db.commit()


def collect(lead, company, gateway, search=False):
    """Collect a bounded set of pages; a source found in search is not evidence until fetched."""
    scope = DEPTHS[lead.depth]
    root = company.resource if company.resource.startswith(('https://', 'http://')) else ''
    task_words = _words(lead.reply + ' ' + lead.offer)
    pages = []
    failures = []
    seen = set()

    def add(url, category='website', discovered=False):
        if not url or url in seen or len(pages) >= scope['pages']:
            return
        seen.add(url)
        try:
            page = fetch(url)
            if category in ('social', 'forums') and not _recent_page(page):
                failures.append({'url': url, 'reason': 'Нет подтверждённой публикации за последние 30 дней'})
                return
            if discovered and not _same_host(page['url'], root):
                name_words = _words(company.name)
                if name_words and not any(w in page['text'].casefold() for w in name_words):
                    failures.append({'url': url, 'reason': 'Не установлена связь найденной страницы с компанией'})
                    return
            pages.append({key: page[key] for key in ('url', 'title', 'text', 'retrieved_at', 'date_metadata', 'content_hash', 'links') if key in page} | {'category': category})
        except (SourceUnavailable, OSError, ValueError) as exc:
            failures.append({'url': url, 'reason': str(exc)[:180]})

    if root:
        add(root, 'social' if _category(root) == 'social' else 'website')
    if pages and root:
        for url in _relevant_links(pages[0], root, task_words, scope['site_links']):
            add(url, _category(url))
    if search and lead.depth != 'brief':
        for url in company.context.get('socials', []):
            add(url, 'social')
        if scope['search'] and len(pages) < scope['pages']:
            directions = list(scope['categories'])
            discovered = gateway.ask(
                'discovery',
                'Найди только публичные источники, относящиеся к указанной компании и запросу ответившего человека. '
                'Категории строго из списка. При отсутствии источника верни пустой список. '
                'Не делай выводов о потребности из отсутствия вакансий. Ссылки лишь кандидаты на последующую проверку.',
                {'company': company.name, 'website': root, 'reply': lead.reply[:3000],
                 'offer': lead.offer[:5000], 'categories': directions,
                 'max_sources': min(8, scope['pages'] - len(pages))},
                DiscoveredSources, max_output=1800, search=True, company_id=company.id)
            for source in discovered.sources[:8]:
                if source.category in directions:
                    add(source.url, source.category, discovered=True)
    # No first-pass profile, group, evidence, or hypothesis is read here.
    return pages, failures


def validated_facts(pages, extracted):
    result = []
    for item in extracted.facts:
        if not 0 <= item.source_index < len(pages):
            continue
        quote = ' '.join(item.quote.split())
        text = ' '.join(pages[item.source_index]['text'].split())
        if not 12 <= len(quote) <= 600 or quote.casefold() not in text.casefold():
            continue
        result.append({'id': f'f{len(result) + 1}', 'source_index': item.source_index,
                       'url': pages[item.source_index]['url'], 'quote': quote,
                       'observation': item.observation[:700], 'relevance': item.relevance[:500]})
    return result[:35]


def execute(lead_id):
    with Session() as db:
        lead = db.get(Lead, lead_id)
        company = db.get(Company, lead.company_id)
        run_id = lead.research_run_id
        letter_requested = lead.stage == 'LETTER'
        snapshot = {
            'reply': lead.reply, 'offer': lead.offer, 'depth': lead.depth,
            'respondent': lead.respondent, 'company_name': company.name,
            'company_resource': company.resource,
        }
        existing_report = lead.report
    gateway = Gateway(run_id)

    if letter_requested:
        if not existing_report:
            raise ValueError('Сначала подготовьте отчёт')
        chosen = next((r for r in existing_report['recommendations']
                       if r['id'] == existing_report.get('selected_recommendation')), None)
        if existing_report.get('selected_recommendation') and chosen is None:
            raise ValueError('Не найдена выбранная рекомендация')
        draft = gateway.ask(
            'prompt',
            'Напиши ответ на уже полученное заинтересованное сообщение. Если выбрана рекомендация, используй её. '
            'проверенные сведения отчёта и текст оффера. Не придумывай обещания, цены, сроки, должности, '
            'имена или подпись. Не выдавай гипотезы за факты. Письмо должно отвечать на запрос человека '
            'и предлагать конкретный следующий шаг. Если данных для адресного предложения нет, '
            'сформулируй уточняющий вопрос без выдуманной конкретики.',
            {**snapshot, 'report': existing_report, 'chosen_recommendation': chosen},
            LetterDraft, max_output=1800, company_id=lead.company_id)
        with Session() as db:
            lead = db.get(Lead, lead_id)
            lead.letter = ('Тема: ' + draft.subject.strip() + '\n\n' + draft.body.strip()).strip()
            lead.letter_status = 'COMPLETED'
            lead.stage = 'COMPLETED'
            lead.updated_at = now()
            run = db.get(Run, run_id)
            run.status = 'COMPLETED'
            run.stage = 'LETTER_READY'
            log(db, run_id, 'LETTER', 'Черновик ответа подготовлен', lead.company_id)
            db.commit()
        return

    _set_stage(lead_id, 'COLLECTING')
    pages, failures = collect(lead, company, gateway, search=False)
    _set_stage(lead_id, 'EXTRACTION')

    def extract(current_pages):
        if not current_pages:
            return ExtractedFacts()
        return gateway.ask(
            'extraction',
            'Извлеки только сведения, имеющие отношение к предложению и сообщению ответившего. '
            'Для каждого факта дай дословную цитату из одной страницы и индекс этой страницы. '
            'Не превращай маркетинговое заявление или вакансию в доказанный бюджет либо намерение купить. '
            'needs_more_sources=false только если ответ и подтверждённые факты уже позволяют обосновать '
            'конкретное предложение; одного общего описания отрасли недостаточно.',
            {'task': snapshot, 'pages': [{'index': i, 'url': p['url'], 'category': p['category'],
                                          'title': p['title'], 'text': p['text'][:6000],
                                          'date_metadata': p.get('date_metadata', [])}
                                         for i, p in enumerate(current_pages)]},
            ExtractedFacts, max_output=3500, company_id=lead.company_id)

    extracted = extract(pages)
    facts = validated_facts(pages, extracted)
    if lead.depth != 'brief' and (extracted.needs_more_sources or not facts):
        _set_stage(lead_id, 'DISCOVERY')
        expanded_pages, expanded_failures = collect(lead, company, gateway, search=True)
        if {p['url'] for p in expanded_pages} != {p['url'] for p in pages}:
            pages, failures = expanded_pages, expanded_failures
            _set_stage(lead_id, 'EXTRACTION')
            facts = validated_facts(pages, extract(pages))
        else:
            failures = expanded_failures
    with Session() as db:
        saved_lead = db.get(Lead, lead_id)
        saved_lead.pages = [{k: v for k, v in page.items() if k not in ('text', 'links')} for page in pages]
        saved_lead.updated_at = now()
        db.commit()
    _set_stage(lead_id, 'SYNTHESIS')
    report = gateway.ask(
        'segmentation',
        'Подготовь отчёт для ответа уже заинтересованному человеку. Компания определена по исходной базе; '
        'её сайт из базы и ссылки с него считаются принадлежащими компании. '
        'Описывай потребность из сообщения как выраженный интерес, не как подтверждённый бюджет или проект. '
        'Опирайся только на предоставленные проверенные факты и сообщение; не используй знания о компании вне них. '
        'Раздели сведения, выводы и неопределённости. Дай до трёх конкретных вариантов сотрудничества по офферу, '
        'каждый с фактическими evidence_ids и/или дословным reply_quote. '
        'Не выводи отсутствие потребности из отсутствия вакансии. Не ссылайся на неоткрытые источники.',
        {'task': snapshot, 'facts': facts,
         'sources': [{k: v for k, v in page.items() if k not in ('text', 'links')}
                     for page in pages],
         'failed_sources': failures},
        LeadReport, max_output=4300, company_id=lead.company_id)
    valid_ids = {fact['id'] for fact in facts}
    recommendations = []
    for item in report.recommendations[:3]:
        ids = [value for value in item.evidence_ids if value in valid_ids]
        reply_quote = ' '.join(item.reply_quote.split())
        if reply_quote and reply_quote.casefold() not in ' '.join(lead.reply.split()).casefold():
            reply_quote = ''
        if not ids and not reply_quote:
            continue
        recommendations.append({'id': f'r{len(recommendations) + 1}', 'title': item.title[:180],
                                'rationale': item.rationale[:1000], 'proposed_action': item.proposed_action[:1000],
                                'evidence_ids': ids, 'reply_quote': reply_quote,
                                'uncertainty': item.uncertainty[:500]})
    saved = {'company_summary': report.company_summary, 'expressed_interest': report.expressed_interest,
             'findings': report.findings, 'facts': facts, 'recommendations': recommendations,
             'limitations': report.limitations + [f"Не удалось использовать {item['url']}: {item['reason']}" for item in failures[:6]],
             'sources': [{k: v for k, v in page.items() if k not in ('text', 'links')} for page in pages],
             'checked_at': datetime.now(timezone.utc).isoformat(), 'depth': lead.depth,
             'selected_recommendation': recommendations[0]['id'] if recommendations else ''}
    with Session() as db:
        lead = db.get(Lead, lead_id)
        lead.report = saved
        lead.stage = 'COMPLETED'
        lead.updated_at = now()
        run = db.get(Run, run_id)
        run.status = 'COMPLETED'
        run.stage = 'REPORT_READY'
        log(db, run_id, 'REPORT', 'Отчёт и рекомендации подготовлены', lead.company_id,
            {'sources': len(pages), 'facts': len(facts), 'recommendations': len(recommendations)})
        db.commit()
