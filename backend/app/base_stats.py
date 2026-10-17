"""Compact company indexes per imported base and all-time workspace totals."""
from collections import defaultdict
from sqlalchemy import func
from .db import BaseCompanyName, Company, Run


def index_companies(db, run, companies):
    """Save one name/identity row per grouped company, never contact rows."""
    from .workbooks import company_name_key, normalize_inn, canonical_resource
    indexed=[]
    for company in companies:
        context=company.context or {}
        name_key=company_name_key(company.name)
        if not name_key or company.name=='Компания не указана':
            name_key=f'unknown:{company.id}'
        inn=next((value for raw in context.get('legal_ids',[])
                  if (value:=normalize_inn(raw)).isdigit() and len(value) in (10,12)), '')
        website=next((site for raw in context.get('websites',[])
                      if (site:=canonical_resource(raw))), '')
        indexed.append(BaseCompanyName(company_id=company.id,run_id=run.id,
                                       name=company.name,name_key=name_key,
                                       inn=inn,website=website))
    db.add_all(indexed)


def backfill_indexes(db):
    """Bring bases imported before the compact index existed into the same totals."""
    counts=dict(db.query(BaseCompanyName.run_id,func.count(BaseCompanyName.company_id))
                .group_by(BaseCompanyName.run_id).all())
    for run in db.query(Run).all():
        if counts.get(run.id,0)>=run.company_count:continue
        existing={cid for (cid,) in db.query(BaseCompanyName.company_id).filter_by(run_id=run.id)}
        companies=db.query(Company).filter_by(run_id=run.id).yield_per(1000)
        index_companies(db,run,(company for company in companies if company.id not in existing))
    db.commit()


def unique_company_count(rows):
    """Merge by INN, then by compatible name/site; conflicting identities stay separate."""
    rows=list(rows)
    parents=list(range(len(rows)))
    def root(i):
        while parents[i]!=i:
            parents[i]=parents[parents[i]];i=parents[i]
        return i
    def merge(indices):
        if len(indices)<2:return
        first=root(indices[0])
        for i in indices[1:]:parents[root(i)]=first
    by_inn=defaultdict(list);by_name=defaultdict(list)
    for i,row in enumerate(rows):
        if row.inn:by_inn[row.inn].append(i)
        if not row.name_key.startswith('unknown:'):by_name[row.name_key].append(i)
    for indices in by_inn.values():merge(indices)
    for indices in by_name.values():
        inns={rows[i].inn for i in indices if rows[i].inn}
        sites=defaultdict(list)
        for i in indices:sites[rows[i].website].append(i)
        if len(inns)>1:
            # An unlabelled row may join only one known legal entity via its site.
            for site,group in sites.items():
                matches={rows[i].inn for i in group if rows[i].inn}
                if site and len(matches)==1:merge(group)
                else:merge([i for i in group if not rows[i].inn])
        elif len([site for site in sites if site])<=1:
            merge(indices)
        else:
            for group in sites.values():merge(group)
    return len({root(i) for i in range(len(rows))})


def workspace_overview(db):
    rows=db.query(BaseCompanyName).all()
    runs=db.query(Run.segments).all()
    researched=db.query(Company).filter(Company.stage=='PROFILE_READY',
        Company.identity=='CONFIRMED').count()
    return {'unique_companies':unique_company_count(rows),
            'segments_defined':sum(len(segments or []) for (segments,) in runs),
            'researched':researched}
