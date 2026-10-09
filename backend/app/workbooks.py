"""Immutable workbook registry and verified row-preserving partition export."""
import hashlib, json, re, shutil, zipfile
from copy import copy
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from collections import defaultdict
from datetime import date, datetime, time
from openpyxl import load_workbook, Workbook
from .db import Company, SourceRow, Export, uid

EMPTY = {'', '-', '—', 'нет', 'null', 'none', 'n/a', 'nan'}
SOCIAL = {'vk.com','vkontakte.ru','linkedin.com','facebook.com','instagram.com','t.me','telegram.me','ok.ru','youtube.com','youtu.be','x.com','twitter.com'}
SHARED = {'yandex.ru','ya.ru','2gis.ru','google.com','maps.google.com','tilda.ws','wixsite.com'}

def clean(v):
    s = str(v or '').strip()
    return '' if s.lower() in EMPTY else s

def canonical_resource(value, social=False):
    value=clean(value)
    if not value: return None
    value=re.split(r'[\s;,]+',value)[0]
    try:
        p=urlsplit(value if '://' in value else 'https://'+value)
        host=(p.hostname or '').lower().removeprefix('www.').encode('idna').decode()
        if p.scheme not in ('http','https') or '.' not in host or p.username or p.password or p.port not in (None,80,443): return None
        if host in SHARED: return None
        is_social=host in SOCIAL
        if is_social and not p.path.strip('/'):return None
        # Retain full tenant paths on corporate social resources.
        path=p.path.rstrip('/') if is_social else ''
        if social and not is_social:return None
        return urlunsplit(('https',host,path,'',''))
    except (ValueError,UnicodeError):return None

def fingerprint(cells):
    vals=[]
    for c in cells:
        v=c.value
        if isinstance(v,(datetime,date,time)):v=v.isoformat()
        vals.append([c.data_type,type(c.value).__name__,v])
    return hashlib.sha256(json.dumps(vals,ensure_ascii=False,default=str).encode()).hexdigest()

def preflight(path):
    try:
        with zipfile.ZipFile(path) as z:
            if sum(x.file_size for x in z.infolist())>200_000_000: raise ValueError('Распакованный XLSX превышает 200 МБ')
            if any('externalLink' in x.filename for x in z.infolist()):raise ValueError('Внешние связи Excel не поддерживаются')
        wb=load_workbook(path)
    except ValueError:raise
    except Exception as e:raise ValueError('Не удалось открыть XLSX') from e
    data=[]
    for ws in wb:
        if ws.max_row>100_001 or ws.max_column>200: raise ValueError('Лимит: 100 000 строк и 200 колонок на лист')
        if ws.merged_cells.ranges: raise ValueError(f'{ws.title}: объединённые ячейки не поддерживаются')
        if ws.max_row<2:continue
        headers=[clean(c.value) for c in ws[1]]
        names={h.casefold():i for i,h in enumerate(headers) if h}
        company=next((names[h] for h in ['название компании','company','company name'] if h in names),None)
        if company is None:raise ValueError(f'{ws.title}: первая строка должна содержать «Название компании»')
        rows=[]
        for cells in ws.iter_rows(min_row=2):
            if any(c.data_type=='f' for c in cells):raise ValueError(f'{ws.title}: формулы не поддерживаются, сохраните вычисленные значения')
            if any(c.value is not None for c in cells):rows.append(cells)
        if rows:data.append((ws,headers,names,company,rows))
    if not data:raise ValueError('В файле нет строк контактов')
    return wb,data

def import_rows(db,run):
    wb,sheets=preflight(run.source_path)
    companies={};rows_out=[]
    for ws,headers,names,name_col,rows in sheets:
        def val(cells,*keys):
            for k in keys:
                if k.casefold() in names:
                    c=cells[names[k.casefold()]]
                    v=clean(c.hyperlink.target if c.hyperlink else c.value)
                    if v:return v
            return ''
        for cells in rows:
            name=clean(cells[name_col].value) or 'Компания не указана'
            website=canonical_resource(val(cells,'Ссылка на сайт','Website','Сайт'))
            social=canonical_resource(val(cells,'VK компании'),True) or canonical_resource(val(cells,'LinkedIn компании'),True)
            resource=website or social or 'unresolved:'+uid()
            if resource not in companies:
                c=Company(id=uid(),run_id=run.id,name=name,resource=resource,context={'names':[name],'description':val(cells,'Описание'),'socials':[s for s in [canonical_resource(val(cells,'VK компании'),True),canonical_resource(val(cells,'LinkedIn компании'),True)] if s]},identity='UNVERIFIED' if website or social else 'NEEDS_REVIEW',group='NOT_PROCESSED' if website or social else 'NEEDS_REVIEW',reason='' if website or social else 'Нет подтверждаемого сайта или корпоративной соцстраницы')
                companies[resource]=c
            c=companies[resource]
            legal=val(cells,'ИНН','Tax ID','ИНН компании')
            if legal:
                ids=list(dict.fromkeys(c.context.get('legal_ids',[])+[legal]));c.context={**c.context,'legal_ids':ids,'identity_conflict':len(ids)>1}
            if name not in c.context['names']:c.context={**c.context,'names':[*c.context['names'],name]}
            rows_out.append(SourceRow(id=uid(),run_id=run.id,company_id=c.id,sheet=ws.title,row_index=cells[0].row,fingerprint=fingerprint(cells),display_fields=[{'column':cell.column,'name':str(headers[cell.column-1] or f'Колонка {cell.column}'),'value':str(cell.value) if cell.value is not None else ''} for cell in cells]))
    db.add_all(companies.values());db.flush();db.add_all(rows_out)
    run.row_count=len(rows_out);run.company_count=len(companies)
    run.config={**run.config,'sheets':[{'name':w.title,'headers':h,'rows':len(rs)} for w,h,_,_,rs in sheets]}
    wb.close()

STATUS_GROUPS={'FAILED','NEEDS_REVIEW','INSUFFICIENT_DATA','AMBIGUOUS','UNCLASSIFIED','NOT_PROCESSED'}

def export_run(db,run,root,partial=None):
    if hashlib.sha256(Path(run.source_path).read_bytes()).hexdigest()!=run.source_hash:raise ValueError('Исходный файл изменён')
    companies={c.id:c for c in db.query(Company).filter_by(run_id=run.id)}
    rows=db.query(SourceRow).filter_by(run_id=run.id).order_by(SourceRow.sheet,SourceRow.row_index).all()
    segments={s['id']:s for s in run.segments}
    grouped=defaultdict(list)
    for r in rows:
        group=companies[r.company_id].group
        if group not in STATUS_GROUPS and not re.fullmatch(r'segment_\d{3,}',group):raise ValueError('Некорректная группа')
        grouped[group].append(r)
    export_id=uid();stage=Path(root)/('tmp-'+export_id);dest=Path(root)/export_id
    stage.mkdir(parents=True)
    source=load_workbook(run.source_path)
    manifest={'rows':len(rows),'files':[],'groups':{},'segmentation_version':run.segmentation_version,'partial':(run.status not in ('COMPLETED','COMPLETED_WITH_ISSUES')) if partial is None else partial}
    seen=set()
    try:
        for group,members in sorted(grouped.items()):
            folder='statuses' if group in STATUS_GROUPS else 'databases'
            out=Workbook();out.remove(out.active)
            bysheet=defaultdict(list)
            for row in members:bysheet[row.sheet].append(row)
            for spec in run.config['sheets']:
                title=spec['name'];src=source[title];ws=out.create_sheet(title)
                ws.freeze_panes='A2'
                for key,dim in src.column_dimensions.items():ws.column_dimensions[key]=copy(dim)
                indices=[1]+[r.row_index for r in bysheet[title]]
                for dest_i,original_i in enumerate(indices,1):
                    for c in src[original_i]:
                        target=ws.cell(dest_i,c.column,c.value)
                        target.data_type=c.data_type
                        if c.has_style:target._style=copy(c._style)
                        if c.hyperlink:target._hyperlink=copy(c.hyperlink)
                        if c.comment:target.comment=copy(c.comment)
                    ws.row_dimensions[dest_i].height=src.row_dimensions[original_i].height
                ws.auto_filter.ref=ws.dimensions
            target=stage/folder/(group+'.xlsx');target.parent.mkdir(exist_ok=True);out.save(target);out.close()
            verified=load_workbook(target)
            for spec in run.config['sheets']:
                title=spec['name'];check=verified[title]
                if fingerprint(check[1])!=fingerprint(source[title][1]):raise ValueError('Изменены заголовки')
                for idx,row in enumerate(bysheet[title],2):
                    if fingerprint(check[idx])!=row.fingerprint:raise ValueError(f'Изменена строка {row.id}')
                    if row.id in seen:raise ValueError('Повтор строки в экспорте')
                    seen.add(row.id)
            verified.close();manifest['files'].append(str(target.relative_to(stage)));manifest['groups'][group]=len(members)
            if group not in STATUS_GROUPS and segments.get(group,{}).get('prompt'):
                p=stage/'prompts'/(group+'.md');p.parent.mkdir(exist_ok=True);p.write_text(segments[group]['prompt'],encoding='utf-8');manifest['files'].append(str(p.relative_to(stage)))
        if len(seen)!=run.row_count:raise ValueError('Нарушено количество строк')
        manifest['checksums']={f:hashlib.sha256((stage/f).read_bytes()).hexdigest() for f in manifest['files']}
        with zipfile.ZipFile(stage/'results.zip','w',zipfile.ZIP_DEFLATED) as z:
            for f in manifest['files']:z.write(stage/f,f)
        stage.rename(dest)
        record=Export(id=export_id,run_id=run.id,path=str(dest),manifest=manifest);db.add(record);db.commit();return record
    except Exception:
        shutil.rmtree(stage,ignore_errors=True);raise
    finally:source.close()
