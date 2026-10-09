from urllib.parse import urlsplit
from test_pipeline import FakeGateway
from app.schemas import Discovery,ResearchReport


def v3_fetch(url):
    name=url.rsplit('/',1)[-1] if 'industry' in url else urlsplit(url).hostname.split('.')[0].title()
    text=f'{name} company offers multiple product lines.' if 'industry' not in url else f'2026-10-01. Independent original reporting by Industry. {name} company is entering a new B2B market.'
    return {'url':url,'title':name,'text':text,'truncated':False,'links':[],'retrieved_at':'2026-10-09T00:00:00Z','content_hash':url}

class V3Gateway(FakeGateway):
    def ask(self,role,instructions,payload,schema,**kwargs):
        if schema is Discovery and payload['direction']=='all':
            self.roles.append(role)
            return Discovery(checked_directions=['website','social','news_jobs','reviews','forums'],identity_confirmed=True,identity_quote=payload['initial_page']['text'],identity_reason='Provided resource matches company',products='Products',sources=[{'url':'https://industry.example/news/'+payload['company'],'direction':'news_jobs','rationale':'Independent source'}])
        if schema is ResearchReport:
            report=super().ask(role,instructions,payload,schema,**kwargs)
            e=report.evidence[0]
            if payload['direction']=='news_jobs':
                e.date_evidence='2026-10-01';e.subject_quote=payload['pages'][0]['text'];e.source_relation='primary';e.provenance_quote='Independent original reporting by Industry.'
            else:e.published_at=None
            return report
        return super().ask(role,instructions,payload,schema,**kwargs)
