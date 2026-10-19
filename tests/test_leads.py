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
            return lead_research.CompanyDossier(
                company_summary='Производитель офисной мебели',
                expressed_interest='Ответивший интересуется региональными продажами',
                findings='У компании есть офисная мебель.',
                sections=[lead_research.DossierSection(topic='Деятельность', text='Производит офисную мебель.', evidence_ids=['f1'])],
                question_map=[lead_research.QuestionAssessment(id='activity', status='partial', answer='Мебель', evidence_ids=['f1'])])
        if role == 'prompt' and schema is lead_research.RecommendationSet:
            return lead_research.RecommendationSet(recommendations=[lead_research.Recommendation(
                title='Обсудить региональный канал', rationale='Ответивший указал интерес',
                proposed_action='Уточнить приоритетные регионы',
                evidence_ids=['f1'], reply_quote='Интересно расширить продажи в регионах.'),
                lead_research.Recommendation(title='Уточнить запрос', rationale='Интерес пока общий',
                proposed_action='Спросить о приоритетной задаче', evidence_ids=[],
                reply_quote='Интересно расширить продажи в регионах.')])
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
    assert detail['report']['sections'][0]['topic'] == 'Деятельность'
    assert detail['report']['question_map'][0]['status'] == 'partial'
    assert detail['report']['coverage'] == 'INCOMPLETE'
    assert detail['report']['recommendations'][0]['evidence_ids'] == ['f1']
    assert len(detail['report']['recommendations']) == 2
    assert detail['report']['recommendations'][1]['evidence_ids'] == []
    assert detail['report']['recommendations'][1]['reply_quote'] == 'Интересно расширить продажи в регионах.'
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
    assert [role for role, _ in calls] == ['extraction', 'segmentation', 'prompt', 'prompt']
    rerun = client.post('/api/leads/' + lead_id + '/rerun', json={
        'offer': 'Новый оффер для регионального развития продаж мебели.',
        'original_message': 'Мы писали о продажах в регионах.',
        'additional_sites': ['https://other.example/company'],
        'depth': 'expanded',
    })
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()['status'] == 'QUEUED'
    assert rerun.json()['report_versions'][0]['version'] == 1
    assert rerun.json()['report']['company_summary']
    assert rerun.json()['offer'] == 'Новый оффер для регионального развития продаж мебели.'
    assert rerun.json()['original_message'] == 'Мы писали о продажах в регионах.'
    assert rerun.json()['additional_sites'] == ['https://other.example/company']
    assert rerun.json()['depth'] == 'expanded'
    archived = client.get('/api/leads/' + lead_id + '/versions/1/report.md')
    assert archived.status_code == 200
    assert 'Производит офисную мебель' in archived.text
    with Session() as db:
        run = db.get(Run, db.get(Lead, lead_id).research_run_id)
        assert run.context_snapshot['research_revision'] == 2
        assert run.reports['lead_report_versions'][0]['offer'] == 'Помогаем мебельным компаниям развивать продажи через новый канал.'
        assert run.reports['lead_report_versions'][0]['depth'] == 'brief'
        assert run.config['max_search_queries'] == 2


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
        'text': 'Компания Мебель А производит офисную мебель. Работает с региональными дилерами. Заказы принимаются через сайт.',
        'links': [], 'retrieved_at': '2026-10-19', 'date_metadata': []})
    roles = []
    def fake_ask(self, role, instructions, payload, schema, **kwargs):
        roles.append(role)
        if role == 'extraction':
            return lead_research.ExtractedFacts(
                facts=[
                    lead_research.ExtractedFact(source_index=0, quote='Компания Мебель А производит офисную мебель.',
                        observation='Производит офисную мебель', relevance='Деятельность', question_ids=['activity', 'products']),
                    lead_research.ExtractedFact(source_index=0, quote='Работает с региональными дилерами.',
                        observation='Покупатели — региональные дилеры', relevance='Покупатели', question_ids=['buyers']),
                    lead_research.ExtractedFact(source_index=0, quote='Заказы принимаются через сайт.',
                        observation='Принимает заказы через сайт', relevance='Путь обращения', question_ids=['ordering'])],
                needs_more_sources=False)
        if role == 'segmentation':
            return lead_research.CompanyDossier(
                company_summary='Производитель мебели',
                expressed_interest='Интерес к региональным продажам',
                findings='Компания работает с дилерами.',
                sections=[lead_research.DossierSection(topic='Покупатели', text='Работает с дилерами.', evidence_ids=['f1'])])
        if role == 'prompt' and schema is lead_research.RecommendationSet:
            return lead_research.RecommendationSet(recommendations=[lead_research.Recommendation(
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
    assert roles == ['extraction', 'segmentation', 'prompt']


def test_company_page_selection_keeps_product_and_portfolio():
    page = {'links': [
        {'label': 'Политика конфиденциальности', 'url': 'https://factory.example/privacy'},
        {'label': 'Услуги', 'url': 'https://factory.example/services'},
        {'label': 'Прицепы и полуприцепы', 'url': 'https://factory.example/izgotovlenie-kuzovov'},
        {'label': 'Выполненные работы', 'url': 'https://factory.example/portfolio'},
    ]}
    selected = lead_research._relevant_links(page, 'https://factory.example/', {'интересно'}, 3)
    assert len(selected) == 3
    assert 'https://factory.example/izgotovlenie-kuzovov' in selected
    assert not any('privacy' in url for url in selected)


def test_no_verified_sources_produces_incomplete_dossier_without_synthesis(client, monkeypatch):
    from app.crawler import SourceUnavailable
    created = client.post('/api/offer-runs',
        files={'file': ('base.xlsx', workbook(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
        data={'name': 'База', 'offer_text': 'Развитие продаж и продуктовых направлений.'})
    rid = created.json()['id']
    row = client.get('/api/lead-candidates', params={'run_id': rid, 'q': 'first@example.test'}).json()[0]
    response = client.post('/api/leads', json={
        'source_row_id': row['source_row_id'], 'reply': 'Тема интересна. Какие предложения?',
        'offer': 'Развитие продаж и продуктовых направлений.', 'depth': 'brief',
        'original_message': 'Предлагаем обсудить развитие продукции.',
        'additional_sites': ['https://other.example/company'],
    })
    assert response.status_code == 200, response.text
    lid = response.json()['id']
    assert response.json()['original_message'] == 'Предлагаем обсудить развитие продукции.'
    assert response.json()['additional_sites'] == ['https://other.example/company']
    monkeypatch.setattr(lead_research, 'fetch', lambda url: (_ for _ in ()).throw(SourceUnavailable('Недоступно')))
    monkeypatch.setattr(lead_research.Gateway, 'ask', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('No model call needed')))
    with Session() as db:
        lead = db.get(Lead, lid)
        db.get(Run, lead.research_run_id).status = 'RUNNING'
        db.commit()
    lead_research.execute(lid)
    detail = client.get('/api/leads/' + lid).json()
    assert detail['report']['coverage'] == 'INCOMPLETE'
    assert detail['report']['recommendations'] == []
    assert detail['report']['question_map'][0]['status'] == 'unavailable'
    assert 'Не удалось использовать' in client.get('/api/leads/' + lid + '/report.md').text


def test_secondary_site_is_explored_when_main_site_unavailable(monkeypatch):
    from types import SimpleNamespace
    from app.crawler import SourceUnavailable
    def fake_fetch(url):
        if url == 'https://missing.example/':
            raise SourceUnavailable('HTTP 404')
        if url == 'https://backup.example/':
            return {'url': url, 'title': 'Компания', 'text': 'Компания выпускает изделия для заказчиков.',
                    'links': [{'label': 'Услуги', 'url': 'https://backup.example/services'}],
                    'retrieved_at': '2026-10-19', 'date_metadata': []}
        return {'url': url, 'title': 'Услуги', 'text': 'Компания оказывает услуги по изготовлению деталей.',
                'links': [], 'retrieved_at': '2026-10-19', 'date_metadata': []}
    monkeypatch.setattr(lead_research, 'fetch', fake_fetch)
    lead = SimpleNamespace(depth='brief', reply='Тема интересна. Какие предложения?', offer='Услуги.')
    company = SimpleNamespace(resource='https://missing.example/', name='Компания', context={'socials': []})
    pages, failures = lead_research.collect(lead, company, None, additional_sites=['https://backup.example/'])
    assert [page['url'] for page in pages] == ['https://backup.example/', 'https://backup.example/services']
    assert all(page['origin'] == 'provided' for page in pages)
    assert failures[0]['url'] == 'https://missing.example/'


def test_gap_search_extracts_only_new_page(client, monkeypatch):
    created = client.post('/api/offer-runs',
        files={'file': ('base.xlsx', workbook(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
        data={'name': 'База', 'offer_text': 'Развитие продуктов и продаж.'})
    rid = created.json()['id']
    row = client.get('/api/lead-candidates', params={'run_id': rid, 'q': 'first@example.test'}).json()[0]
    lid = client.post('/api/leads', json={
        'source_row_id': row['source_row_id'], 'reply': 'Интересно обсудить продажи.',
        'offer': 'Развитие продуктов и продаж.', 'depth': 'standard',
    }).json()['id']
    def fake_fetch(url):
        if url == 'https://a.example':
            return {'url': url, 'title': 'Сайт', 'text': 'Мебель А производит офисную мебель.',
                    'links': [], 'retrieved_at': '2026-10-19', 'date_metadata': []}
        return {'url': url, 'title': 'Профиль',
                'text': 'Мебель А работает с региональными дилерами. Заказы принимаются через сайт.',
                'links': [], 'retrieved_at': '2026-10-19', 'date_metadata': []}
    monkeypatch.setattr(lead_research, 'fetch', fake_fetch)
    extraction_payloads = []
    def fake_ask(self, role, instructions, payload, schema, **kwargs):
        if role == 'extraction':
            extraction_payloads.append(payload)
            if len(extraction_payloads) == 1:
                return lead_research.ExtractedFacts(facts=[lead_research.ExtractedFact(
                    source_index=0, quote='Мебель А производит офисную мебель.',
                    observation='Производит мебель', relevance='Деятельность',
                    question_ids=['activity', 'products'])], needs_more_sources=True)
            return lead_research.ExtractedFacts(facts=[lead_research.ExtractedFact(
                source_index=0, quote='Мебель А работает с региональными дилерами.',
                observation='Работает с дилерами', relevance='Покупатели', question_ids=['buyers'])],
                needs_more_sources=False)
        if role == 'discovery':
            assert any('заказ' in q.lower() or 'обращ' in q.lower() for q in payload['unanswered_questions'])
            return lead_research.DiscoveredSources(sources=[lead_research.DiscoveredSource(
                url='https://catalog.example/m', category='directory', reason='Профиль компании')])
        if role == 'segmentation':
            assert {f['id'] for f in payload['facts']} == {'f1', 'f2'}
            return lead_research.CompanyDossier(
                company_summary='Производит мебель для дилеров.',
                expressed_interest='Интерес к продажам', findings='Два проверенных источника.',
                summary_evidence_ids=['f1', 'f2'],
                sections=[lead_research.DossierSection(
                    topic='Деятельность', text='Производит мебель.', evidence_ids=['f1'])])
        if role == 'prompt':
            return lead_research.RecommendationSet(recommendations=[lead_research.Recommendation(
                title='Общее предложение', rationale='Ответил на письмо', proposed_action='Обсудить сотрудничество',
                reply_quote='Интересно обсудить продажи.')])
        raise AssertionError(role)
    monkeypatch.setattr(lead_research.Gateway, 'ask', fake_ask)
    with Session() as db:
        lead = db.get(Lead, lid)
        db.get(Run, lead.research_run_id).status = 'RUNNING'
        db.commit()
    lead_research.execute(lid)
    assert [len(p['pages']) for p in extraction_payloads] == [1, 1]
    assert [p['pages'][0]['index'] for p in extraction_payloads] == [0, 0]
    result = client.get('/api/leads/' + lid).json()['report']
    assert [fact['id'] for fact in result['facts']] == ['f1', 'f2']
    assert result['coverage'] == 'INCOMPLETE'
    assert len(result['recommendations']) == 1
    assert result['recommendations'][0]['evidence_ids'] == []
    assert result['recommendations'][0]['reply_quote'] == 'Интересно обсудить продажи.'
