import io
import pytest
from openpyxl import Workbook

from app.db import Company, Run, Session
from app.entity_audit import audit_import
from app.entity_repair import restore_missing_legal_metadata, restore_missing_legal_names, restore_missing_social_metadata


def _workbook():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(['Название компании', 'Ссылка на сайт', 'ИНН', 'Имя', 'VK компании', 'Юридическое название компании'])
    sheet.append(['Альфа', 'https://alpha.example', '1001', 'Контакт 1', 'https://vk.com/alpha', 'ООО АЛЬФА'])
    sheet.append(['Альфа', 'http://www.alpha.example/about', '1001', 'Контакт 2', 'https://vk.com/alpha_team', 'ООО АЛЬФА'])
    sheet.append(['Бета', 'https://beta.example', '2002', 'Контакт 3', None, 'ООО БЕТА'])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_audit_and_restore_legacy_legal_metadata_without_changing_assignments(client):
    sender_id = client.get('/api/senders').json()[0]['id']
    response = client.post('/api/runs', data={'sender_id': sender_id},
                           files={'file': ('companies.xlsx', _workbook())})
    assert response.status_code == 200, response.text
    run_id = response.json()['id']
    with Session() as db:
        original = {
            company.id: (company.resource, company.group, company.stage, company.identity)
            for company in db.query(Company).filter_by(run_id=run_id)
        }
        before = audit_import(db, run_id)
        assert before['row_integrity_ok']
        assert before['multi_row_groups'] == 1
        assert before['groups_with_multiple_legal_ids_in_source'] == 0
        assert before['groups_missing_stored_legal_ids'] == 0
        assert before['groups_missing_stored_legal_names'] == 0
        for company in db.query(Company).filter_by(run_id=run_id):
            context = {key: value for key, value in company.context.items()
                       if key not in ('legal_ids', 'legal_names', 'identity_conflict')}
            if company.resource == 'https://alpha.example':
                context['socials'] = ['https://vk.com/alpha']
            company.context = context
        db.commit()
        missing_report = audit_import(db, run_id)
        assert missing_report['groups_missing_stored_legal_ids'] == 2
        assert missing_report['groups_missing_stored_legal_names'] == 2
        assert missing_report['groups_missing_stored_social_links'] == 1
        assert restore_missing_legal_metadata(db, run_id) == 2
        assert restore_missing_legal_names(db, run_id) == 2
        assert restore_missing_social_metadata(db, run_id) == 1
        db.commit()
        after = audit_import(db, run_id)
        assert after['row_integrity_ok']
        assert after['groups_missing_stored_legal_ids'] == 0
        assert after['groups_missing_stored_legal_names'] == 0
        assert after['groups_missing_stored_social_links'] == 0
        assert restore_missing_legal_metadata(db, run_id) == 0
        assert restore_missing_legal_names(db, run_id) == 0
        assert restore_missing_social_metadata(db, run_id) == 0
        assert {
            company.id: (company.resource, company.group, company.stage, company.identity)
            for company in db.query(Company).filter_by(run_id=run_id)
        } == original
        assert db.get(Run, run_id).status == 'CREATED'


def test_different_inns_on_same_website_are_separate_companies(client):
    sender_id = client.get('/api/senders').json()[0]['id']
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(['Название компании', 'Ссылка на сайт', 'ИНН'])
    sheet.append(['Альфа', 'https://alpha.example', '1001'])
    sheet.append(['Альфа', 'https://alpha.example/about', '2002'])
    output = io.BytesIO()
    workbook.save(output)
    response = client.post('/api/runs', data={'sender_id': sender_id},
                           files={'file': ('conflict.xlsx', output.getvalue())})
    assert response.status_code == 200, response.text
    with Session() as db:
        companies=db.query(Company).filter_by(run_id=response.json()['id']).all()
        assert len(companies)==2
        assert {tuple(c.context['legal_ids']) for c in companies}=={('1001',),('2002',)}
        assert all(c.resource=='https://alpha.example' for c in companies)
        assert all(c.group=='NEEDS_REVIEW' for c in companies)
        report = audit_import(db, response.json()['id'])
        assert report['groups_with_multiple_legal_ids_in_source'] == 0
        assert report['row_integrity_ok']
    retried=client.post(f"/api/runs/{response.json()['id']}/retry",json={'groups':['NEEDS_REVIEW']})
    assert retried.status_code==200
    with Session() as db:
        assert all(c.identity=='NEEDS_REVIEW' and c.group=='NEEDS_REVIEW'
                   for c in db.query(Company).filter_by(run_id=response.json()['id']))
