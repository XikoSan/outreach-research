"""Workspace behavior, snapshot integrity and profile credential isolation."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app import credentials
from app.db import Session, Run, Company, ModelCall
from app.llm import Gateway, RunPaused
from app.schemas import Review
from app.pipeline import Pipeline, sender_directions
from test_workflow import workbook, upload
from test_pipeline import new_run, FakeGateway, fake_fetch

def test_profile_edit_is_stable_and_snapshots_are_immutable(client):
    s=client.get('/api/senders').json()[0]
    r=upload(client,workbook([['A','example.com','x','y']])).json()
    profile={**s['profile'],'profile_name':'Новое название','brand':'Другой бренд'}
    response=client.put('/api/senders/'+s['id'],json=profile)
    assert response.status_code==200
    assert response.json()['id']==s['id'] and response.json()['version']==2
    assert len(client.get('/api/senders').json())==1
    assert client.get('/api/senders/'+s['id']+'/history').json()[0]['profile']==s['profile']
    assert client.get('/api/runs/'+r['id']).json()['sender_snapshot']['profile']==s['profile']

def test_last_profile_can_be_deleted_without_reseed(client):
    from app.seed import seed
    s=client.get('/api/senders').json()[0]
    assert client.delete('/api/senders/'+s['id']).status_code==200
    with Session() as db:seed(db)
    assert client.get('/api/senders').json()==[]
    created=client.post('/api/senders',json={'brand':'Новый'}).json()
    assert created['profile']['offers']==[]
    assert all(not value for key,value in created['profile'].items() if key!='brand')

def test_credentials_are_isolated_private_and_never_echoed(client):
    a=client.get('/api/senders').json()[0]['id']
    b=client.post('/api/senders',json={'brand':'B'}).json()['id']
    key='sk-offline-private-test-not-a-real-key'
    response=client.put('/api/senders/'+a+'/connection',json={'api_key':key})
    assert response.status_code==200 and key not in response.text
    assert credentials.location(a).stat().st_mode & 0o777==0o600
    assert credentials.status(a)['configured'] and not credentials.status(b)['configured']
    r=upload(client,workbook([['A','example.com','x','y']])).json()
    # upload chooses newest profile B, which must not borrow A's key.
    assert r['sender_snapshot']['id']==b
    assert client.post('/api/runs/'+r['id']+'/start').status_code==409
    assert key not in client.get('/api/senders').text+client.get('/api/runs').text
    bad='sk-secret bad input must never echo'
    response=client.put('/api/senders/'+a+'/connection',json={'api_key':bad})
    assert response.status_code==422 and bad not in response.text
    client.delete('/api/senders/'+a+'/connection')
    assert not credentials.status(a)['configured']

def test_gateway_uses_rotated_profile_key_and_pauses_before_reserving(client,monkeypatch):
    rid=new_run(client)
    with Session() as db:sid=db.get(Run,rid).sender_snapshot['id']
    response=SimpleNamespace(id='offline',usage=SimpleNamespace(input_tokens=10,output_tokens=5),output=[],output_text='{"passed":true,"issues":[]}')
    provider=Mock();provider.responses.create.return_value=response
    factory=Mock(return_value=provider);monkeypatch.setattr('app.llm.OpenAI',factory)
    credentials.write(sid,{'key':'first'})
    gateway=Gateway(rid);gateway.ask('synthesis','first',{},Review)
    credentials.write(sid,{'key':'second'})
    gateway.ask('synthesis','second',{},Review)
    assert [call.kwargs['api_key'] for call in factory.call_args_list]==['first','second']
    credentials.write(sid,{})
    with pytest.raises(RunPaused):gateway.ask('synthesis','third',{},Review)
    with Session() as db:
        assert db.get(Run,rid).status=='PAUSED'
        assert db.get(Run,rid).reserved==pytest.approx(0)
        assert db.query(ModelCall).count()==2

def test_connection_check_is_explicit_and_sanitized(client,monkeypatch):
    sid=client.get('/api/senders').json()[0]['id']
    credentials.write(sid,{'key':'offline-key'})
    provider=Mock();factory=Mock();factory.return_value.__enter__=Mock(return_value=provider);factory.return_value.__exit__=Mock(return_value=False)
    monkeypatch.setattr('openai.OpenAI',factory)
    assert client.post('/api/senders/'+sid+'/connection/check').json()['state']=='verified'
    provider.models.list.side_effect=RuntimeError('offline-key secret')
    result=client.post('/api/senders/'+sid+'/connection/check')
    assert result.status_code==502 and 'offline-key' not in result.text

def test_progress_attention_filter_and_named_pagination(client):
    r=upload(client,workbook([['Known','example.com','x','y'],['Unknown',None,'z','w']])).json()
    rid=r['id']
    result=client.get('/api/runs/'+rid).json()
    assert result['researched']==0 and result['finished']==1 and result['attention']==1
    assert client.get(f'/api/runs/{rid}/companies?attention_only=true').json()['total']==1
    assert client.patch('/api/runs/'+rid,json={'name':'Исследование рынка'}).status_code==200
    assert client.get('/api/research?q=рынка&limit=1').json()['total']==1
    assert client.get('/api/research?offset=1&limit=1').json()['items']==[]
    assert client.get('/api/research?q=неизвестно').json()['total']==0

def test_readable_company_evidence_without_raw_pages(client,monkeypatch):
    rid=new_run(client);monkeypatch.setattr('app.pipeline.fetch',fake_fetch);Pipeline(rid,FakeGateway()).execute()
    row=client.get(f'/api/runs/{rid}/companies').json()['items'][0]
    assert row['segment']['name']=='Новая аудитория'
    assert row['signals'] and row['evidence_state']=='Достаточно для сегмента'
    result=client.get('/api/companies/'+row['id']).json()
    assert result['evidence'][0]['quote'] and result['evidence'][0]['retrieved_at']
    assert all('pages' not in r for r in result['reports'].values())
    assert all(eid in {e['id'] for e in result['evidence']} for s in result['signals'] for eid in s['evidence_ids'])

def test_pipeline_accepts_sender_description_without_offer_catalogue(client,monkeypatch):
    from app.schemas import Segments
    rid=new_run(client)
    with Session() as db:
        run=db.get(Run,rid);run.sender_snapshot={**run.sender_snapshot,'profile':{**run.sender_snapshot['profile'],'offers':[]}}
        db.commit()
    class WithoutOffers(FakeGateway):
        def ask(self,role,instructions,payload,schema,**kwargs):
            result=super().ask(role,instructions,payload,schema,**kwargs)
            if schema is Segments:
                assert payload['offers'][0]['id']=='sender_activity'
                result.segments[0].offer_id='sender_activity'
            return result
    monkeypatch.setattr('app.pipeline.fetch',fake_fetch)
    Pipeline(rid,WithoutOffers()).execute()
    assert client.get('/api/runs/'+rid).json()['assigned']==2
    assert sender_directions({'brand':'Empty','offers':[],'description':''})==[]

def test_migration_merges_versions_without_changing_snapshot_content(tmp_path):
    import importlib.util
    from pathlib import Path
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec=importlib.util.spec_from_file_location('workspace_migration',Path(__file__).resolve().parents[1]/'migrations/versions/0003_workspace.py')
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    engine=sa.create_engine('sqlite:///'+str(tmp_path/'old.db'))
    meta=sa.MetaData()
    senders=sa.Table('senders',meta,sa.Column('id',sa.String,primary_key=True),sa.Column('version',sa.Integer),sa.Column('profile',sa.JSON),sa.Column('created_at',sa.String))
    runs=sa.Table('runs',meta,sa.Column('id',sa.String,primary_key=True),sa.Column('filename',sa.String),sa.Column('sender_snapshot',sa.JSON))
    meta.create_all(engine)
    snapshot={'id':'old','version':1,'profile':{'brand':'Brand','representative':'Old person'}}
    with engine.begin() as connection:
        connection.execute(senders.insert(),[{'id':'old','version':1,'profile':snapshot['profile'],'created_at':'2026-01-01'},{'id':'new','version':2,'profile':{'brand':'Brand','representative':'New person'},'created_at':'2026-02-01'}])
        connection.execute(runs.insert(),{'id':'run','filename':'source.xlsx','sender_snapshot':snapshot})
        with Operations.context(MigrationContext.configure(connection)):migration.upgrade()
        updated=sa.Table('senders',sa.MetaData(),autoload_with=connection)
        rows={r.id:r for r in connection.execute(sa.select(updated))}
        assert rows['old'].archived and not rows['new'].archived
        assert rows['new'].history[0]['profile']==snapshot['profile']
        result=connection.execute(sa.select(runs.c.sender_snapshot)).scalar_one()
        assert result=={**snapshot,'id':'new'}
