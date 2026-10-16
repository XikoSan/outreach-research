from app.semantic_segmentation import discover,BatchResult
from test_candidate_discovery import profile,fact,evidence


def proposal(id,members):
    return dict(id=id,name='Общая работа',definition='Обоснование сотрудничества',offer_id='service',hypothesis='Проверить общую задачу',confidence='MEDIUM',required_signals=[],supporting_signals=[],contradicting_signals=[],outreach_angle='Обсудить работу',diagnostic_question='Интересно ли?',allowed_claims=[],forbidden_claims=[],basis_type='applicability',cooperation_proposal='Исследовать критерии выбора',next_step='Обсудить',shared_message='Предложение общей работы',event_signals=[],members=members)


def test_all_profiles_considered_more_than_three_groups_and_fabrications_rejected():
    profiles=[]
    for i in range(10):
        cid=str(i)
        profiles.append(profile(cid,[fact('products','Товар '+cid,'p'),fact('buyer_types','Монтажники '+cid,'b')],[evidence('p',quote='Продукт'),evidence('b',quote='Покупатели')]))
    class Fake:
        def ask(self,role,instructions,payload,schema,**kwargs):
            ids={p['company_id'] for p in payload['profiles']}
            assert len(ids)<=8
            groups=[]
            for i in range(4):
                if str(i*2) not in ids:continue
                members=[dict(company_id=str(j),context_signal=profiles[j]['signals'][1]['name'],product_signal=profiles[j]['signals'][0]['name']) for j in (i*2,i*2+1)]
                groups.append(proposal(str(i),members))
            if '8' in ids:groups.append(proposal('invalid',[dict(company_id='8',context_signal='invented',product_signal='invented2')]))
            return schema.model_validate({'segments':groups,'unassigned':[{'company_id':'9','reason':'Нет общей ситуации'}]})
    candidates,audit=discover(Fake(),profiles,[{'id':'service'}])
    assert len(candidates)==4
    assert audit['reviewed_companies']==10
    assert {x['company_id'] for x in [item for batch in audit['batches'] for item in batch['unassigned']]}=={'8','9'}
    assert sum(len(g['member_bindings']) for g in candidates)==8


def test_cross_batch_members_reuse_same_proposal():
    profiles=[]
    for i in range(2):
        p=profile(str(i),[fact('products','Товар','p'),fact('buyer_types','Монтажники','b')],[evidence('p',quote='quote'),evidence('b',quote='quote')])
        # Keep all facts; force two batches via labels rather than dropped profiles.
        p['signals'][0]['label']='x'*40000
        profiles.append(p)
    class Fake:
        calls=0
        def ask(self,role,instructions,payload,schema,**kwargs):
            self.calls+=1
            assert len(payload['profiles'])==1
            if self.calls==2:assert payload['existing_groups'][0]['id']=='shared'
            p=payload['profiles'][0]
            member=dict(company_id=p['company_id'],context_signal=p['signals'][1]['name'],product_signal=p['signals'][0]['name'])
            item=proposal('shared',[member])
            if self.calls==2:item['cooperation_proposal']='Краткая формулировка'
            return schema.model_validate({'segments':[item],'unassigned':[]})
    fake=Fake();candidates,audit=discover(fake,profiles,[{'id':'service'}])
    assert fake.calls==2 and len(candidates)==1
    assert set(candidates[0]['member_bindings'])=={'0','1'}
    assert candidates[0]['cooperation_proposal']=='Исследовать критерии выбора'


def test_checkpoint_resumes_without_repeating_model_calls(monkeypatch):
    from types import SimpleNamespace
    import app.semantic_segmentation as semantic
    profiles=[]
    for i in range(7):
        profiles.append(profile(str(i),[fact('products','Товар','p'),fact('buyer_types','Монтажники','b')],
                                [evidence('p',quote='Продукт'),evidence('b',quote='Покупатель')]))
    run=SimpleNamespace(reports={})
    class Db:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,model,id):return run
        def commit(self):pass
    monkeypatch.setattr(semantic,'Session',Db)
    class Fake:
        id='pilot'
        calls=0
        def ask(self,role,instructions,payload,schema,**kwargs):
            self.calls+=1
            if self.calls==2:raise RuntimeError('interrupted')
            members=[{'company_id':p['company_id'],
                      'context_signal':p['signals'][1]['name'],
                      'product_signal':p['signals'][0]['name']}
                     for p in payload['profiles']]
            return schema.model_validate({'segments':[proposal('shared',members)],'unassigned':[]})
    first=Fake()
    import pytest
    with pytest.raises(RuntimeError,match='interrupted'):
        discover(first,profiles,[{'id':'service'}])
    assert run.reports['semantic_checkpoint']['next_batch']==1
    second=Fake()
    groups,audit=discover(second,profiles,[{'id':'service'}])
    assert second.calls==1
    assert run.reports['semantic_checkpoint']['next_batch']==2
    assert audit['reviewed_companies']==7
    assert len(groups)==1 and len(groups[0]['member_bindings'])==7
