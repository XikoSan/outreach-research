from app.evidence import verify_report
from app.schemas import ResearchReport

def checked(url, quote='A precise observation about products.'):
    report=ResearchReport(summary='',evidence=[dict(id='e',url=url,quote=quote,published_at=None,scope='company',origin='example.test')],signals=[],contradictions=[],limitations=[])
    return verify_report(report,[dict(url='https://example.test',text='A precise observation about products.',subject_id='c',publisher_id='company:c')],'website',30,as_of='2026-10-10',company_id='c')

def test_root_slash_preserves_exact_quote_and_subject():
    result=checked('https://example.test/')
    assert len(result['evidence'])==1
    assert result['evidence'][0]['subject_verified']

def test_alias_does_not_accept_paraphrase():
    assert not checked('https://example.test/','A different observation about products.')['evidence']

def test_distinct_paths_queries_schemes_and_hosts_stay_distinct():
    for url in ['https://example.test/product','https://example.test/?other=1','http://example.test/','https://another.test/']:
        result=checked(url)
        assert not result['evidence']
        assert 'страница не найдена' in result['limitations'][0]


def test_default_port_is_same_page_but_nondefault_port_is_not():
    assert len(checked('https://example.test:443')['evidence'])==1
    assert not checked('https://example.test:8443')['evidence']
