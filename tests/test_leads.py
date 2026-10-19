"""The second pass starts from the respondent, not first-pass hypotheses."""
from io import BytesIO
from openpyxl import Workbook
from app.db import Session, Run, Lead, Company
from app import lead_research, leads


def workbook():
    wb = Workbook()
    ws = wb.active
    ws.append(['Название компании', 'ИНН', 'Ссылка на сайт', 'Email', 'Должность'])
    ws.append(['Мебель А', '1234567890', 'https://a.example', 'first@example.test', 'Директор'])
    ws.append(['Мебель А', '1234567890', 'https://a.example', 'second@example.test', 'Маркетолог'])
    stream = BytesIO()
    wb.save(stream)
    wb.close()
    return stream.getvalue()


def test_interested_lead_report_and_optional_letter(client, monkeypatch):
    created = client.post('/api/offer-runs',
        files={'file': ('base.xlsx', workbook(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
        data={'name': 'Тестовая база', 'offer_text': 'Помогаем мебельным компаниям развивать продажи через новый канал.'})
    assert created.status_code == 200, created.text
    rid = created.json()['id']
    candidates = client.get('/api/lead-candidates', params={'run_id': rid, 'q': 'second@example.test'}).json()
    assert len(candidates) == 1
    assert candidates[0]['contact']['title'] == 'Маркетолог'
    assert candidates[0]['company_name'] == 'Мебель А'
    row_id = candidates[0]['source_row_id']
    response = client.post('/api/leads', json={
        'source_row_id': row_id, 'reply': 'Интересно расширить продажи в регионах.',
        'offer': 'Помогаем мебельным компаниям развивать продажи через новый канал.',
        'depth': 'brief', 'respondent_name': 'Анна', 'respondent_title': 'Руководитель маркетинга',
        'respondent_email': 'second@example.test',
    })
    assert response.status_code == 200, response.text
    lead_id = response.json()['id']
    assert response.json()['letter_status'] == 'NOT_REQUESTED'
    assert client.get('/api/offer-runs/overview').json()['researched'] == 0
    duplicate = client.post('/api/leads', json={
        'source_row_id': candidates[0]['source_row_id'], 'reply': 'Другой интересный ответ',
        'offer': 'Помогаем мебельным компаниям развивать продажи через новый канал.',
        'depth': 'brief',
    })
    assert duplicate.status_code == 409

    def fake_fetch(url):
        return {'url': url, 'title': 'Главная', 'text': 'Компания Мебель А производит офисную мебель.',
                'links': [], 'retrieved_at': '2026-10-19', 'date_metadata': []}
    monkeypatch.setattr(lead_research, 'fetch', fake_fetch)
    calls = []
    def fake_ask(self, role, instructions, payload, schema, **kwargs):
        calls.append((role, payload))
        if role == 'extraction':
            return lead_research.ExtractedFacts(facts=[
                lead_research.ExtractedFact(source_index=0, quote='Компания Мебель А производит офисную мебель.',
                                            observation='Производит офисную мебель', relevance='Канал продаж')])
        if role == 'segmentation':
            assert 'profile' not in payload and 'group' not in payload
            return lead_research.LeadReport(
                company_summary='Производитель офисной мебели',
                expressed_interest='Ответивший интересуется региональными продажами',
                findings='У компании есть офисная мебель.',
                recommendations=[lead_research.Recommendation(
                    title='Обсудить региональный канал', rationale='Ответивший указал интерес',
                    proposed_action='Уточнить приоритетные регионы',
                    evidence_ids=['f1'], reply_quote='Интересно расширить продажи в регионах.')])
        if role == 'prompt':
            return lead_research.LetterDraft(subject='О региональных продажах',
                                             body='Анна, спасибо за ответ. Какие регионы сейчас приоритетны?')
        raise AssertionError(role)
    monkeypatch.setattr(lead_research.Gateway, 'ask', fake_ask)
    with Session() as db:
        lead = db.get(Lead, lead_id)
        db.get(Run, lead.research_run_id).status = 'RUNNING'
        company = db.get(Company, lead.company_id)
        company.group = 'segment_fake'
        company.profile = {'coarse': {'hypothesis': 'Do not transfer'}}
        db.commit()
    lead_research.execute(lead_id)
    detail = client.get('/api/leads/' + lead_id).json()
    assert detail['status'] == 'COMPLETED'
    assert detail['report']['facts'][0]['url'] == 'https://a.example'
    assert detail['report']['recommendations'][0]['evidence_ids'] == ['f1']
    assert not detail['letter']
    assert client.get('/api/offer-runs/overview').json()['researched'] == 1
    downloaded = client.get('/api/leads/' + lead_id + '/report.md')
    assert downloaded.status_code == 200
    assert 'Производит офисную мебель' in downloaded.text
    # A letter requires an explicit request, independent of report completion.
    monkeypatch.setattr(leads.credentials, 'for_run', lambda run: 'test-key')
    requested = client.post('/api/leads/' + lead_id + '/letter', json={'recommendation_id': 'r1'})
    assert requested.status_code == 200, requested.text
    assert requested.json()['status'] == 'QUEUED'
    with Session() as db:
        lead = db.get(Lead, lead_id)
        db.get(Run, lead.research_run_id).status = 'RUNNING'
        db.commit()
    lead_research.execute(lead_id)
    detail = client.get('/api/leads/' + lead_id).json()
    assert 'Какие регионы' in detail['letter']
    assert 'Тема: О региональных продажах' in client.get('/api/leads/' + lead_id + '/letter.txt').text
    assert client.get('/api/offer-runs/overview').json()['researched'] == 1
    assert [role for role, _ in calls] == ['extraction', 'segmentation', 'prompt']


def test_invalid_source_quote_is_not_reported():
    pages = [{'url': 'https://a.example', 'text': 'Компания производит офисную мебель.'}]
    extracted = lead_research.ExtractedFacts(facts=[
        lead_research.ExtractedFact(source_index=0, quote='Несуществующий подтвержденный бюджет',
                                    observation='Есть бюджет', relevance='Продажи')])
    assert lead_research.validated_facts(pages, extracted) == []


def test_recent_social_source_requires_publication_date(monkeypatch):
    from types import SimpleNamespace
    def fake_fetch(url):
        return {'url': url, 'title': 'Социальная сеть',
                'text': 'Компания рассказывает о своих продуктах. ' * 4,
                'links': [], 'date_metadata': ['2025-01-01']}
    monkeypatch.setattr(lead_research, 'fetch', fake_fetch)
    lead = SimpleNamespace(depth='brief', reply='Интересует сотрудничество по продажам.',
                           offer='Предлагаем развитие продаж.')
    company = SimpleNamespace(resource='https://vk.com/example_company', name='Компания',
                              context={'socials': []})
    pages, failures = lead_research.collect(lead, company, None)
    assert pages == []
    assert '30 дней' in failures[0]['reason']


def test_standard_depth_stops_before_paid_search_when_sources_are_sufficient(client, monkeypatch):
    created = client.post('/api/offer-runs',
        files={'file': ('base.xlsx', workbook(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
        data={'name': 'База', 'offer_text': 'Помогаем мебельным компаниям развивать продажи через новый канал.'})
    rid = created.json()['id']
    candidate = client.get('/api/lead-candidates', params={'run_id': rid, 'q': 'first@example.test'}).json()[0]
    lead_id = client.post('/api/leads', json={
        'source_row_id': candidate['source_row_id'], 'reply': 'Нужно расширить продажи в регионах.',
        'offer': 'Помогаем мебельным компаниям развивать продажи через новый канал.',
        'depth': 'standard'}).json()['id']
    monkeypatch.setattr(lead_research, 'fetch', lambda url: {
        'url': url, 'title': 'О компании',
        'text': 'Компания Мебель А производит офисную мебель для региональных дилеров.',
        'links': [], 'retrieved_at': '2026-10-19', 'date_metadata': []})
    roles = []
    def fake_ask(self, role, instructions, payload, schema, **kwargs):
        roles.append(role)
        if role == 'extraction':
            return lead_research.ExtractedFacts(
                facts=[lead_research.ExtractedFact(
                    source_index=0, quote='Компания Мебель А производит офисную мебель для региональных дилеров.',
                    observation='Работает с региональными дилерами', relevance='Региональные продажи')],
                needs_more_sources=False)
        if role == 'segmentation':
            return lead_research.LeadReport(
                company_summary='Производитель мебели',
                expressed_interest='Интерес к региональным продажам',
                findings='Компания работает с дилерами.',
                recommendations=[lead_research.Recommendation(
                    title='Уточнить регионы', rationale='Ответ и факт',
                    proposed_action='Обсудить регионы', evidence_ids=['f1'],
                    reply_quote='Нужно расширить продажи в регионах.')])
        raise AssertionError('Unnecessary model call: ' + role)
    monkeypatch.setattr(lead_research.Gateway, 'ask', fake_ask)
    with Session() as db:
        lead = db.get(Lead, lead_id)
        db.get(Run, lead.research_run_id).status = 'RUNNING'
        db.commit()
    lead_research.execute(lead_id)
    assert roles == ['extraction', 'segmentation']
