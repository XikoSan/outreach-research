from io import BytesIO
from openpyxl import Workbook
from app.db import Session, BaseCompanyName, Company, Run


def workbook(rows):
    book=Workbook();sheet=book.active
    sheet.append(['Название компании','ИНН','Ссылка на сайт','Email'])
    for row in rows:sheet.append(row)
    stream=BytesIO();book.save(stream);book.close();return stream.getvalue()


def upload(client,name,rows):
    response=client.post('/api/offer-runs',files={'file':(f'{name}.xlsx',workbook(rows),
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
        data={'name':name,'offer_text':'Помогаем производителям находить новые каналы продаж.'})
    assert response.status_code==200,response.text
    return response.json()['id']


def test_overview_deduplicates_across_bases_without_merging_conflicting_inns(client):
    first=upload(client,'Первая',[
        ('ООО Альфа','1234567890','https://alpha.example','one@example.test'),
        ('Тезис','1234567891','https://t1.example','two@example.test'),
        ('Общее','','https://common.example','three@example.test')])
    upload(client,'Вторая',[
        ('Альфа','1234567890','https://alpha.example','four@example.test'),
        ('Тезис','1234567892','https://t2.example','five@example.test'),
        ('Общее','','https://common.example','six@example.test')])
    with Session() as db:
        assert db.query(BaseCompanyName).count()==6
        alpha=db.query(Company).filter_by(run_id=first,name='ООО Альфа').one()
        alpha.stage='PROFILE_READY';alpha.identity='CONFIRMED';alpha.profile={'summary':'Исследование завершено'}
        run=db.get(Run,first);run.segments=[{'id':'segment_001'},{'id':'segment_002'}]
        db.commit()
    overview=client.get('/api/offer-runs/overview')
    assert overview.status_code==200
    assert overview.json()=={'unique_companies':4,'segments_defined':2,'researched':1}


def test_old_base_is_indexed_once_during_backfill(client):
    from app.base_stats import backfill_indexes
    with Session() as db:
        run=Run(filename='old.xlsx',name='Прежняя база',source_path='old.xlsx',source_hash='hash',
                sender_snapshot={},context_snapshot={},config={},budget=0,company_count=1,row_count=1)
        db.add(run);db.flush()
        db.add(Company(run_id=run.id,name='Старая компания',resource='unresolved:old',
                       context={'legal_ids':['1234567890'],'websites':['https://old.example']}))
        db.commit()
        backfill_indexes(db);backfill_indexes(db)
        assert db.query(BaseCompanyName).filter_by(run_id=run.id).count()==1
    assert client.get('/api/offer-runs/overview').json()['unique_companies']==1
