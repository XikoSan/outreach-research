import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Plus,Settings,ChevronRight} from 'lucide-react';
import './style.css';
import {Obj,api,date,fmt,useResource,useAction,ErrorBox,Loading,Badge,Pager} from './shared';
import {NewCoarse,CoarseDetail} from './coarse';
import {LeadsList,NewLead,LeadProfile} from './leads';

function Connection({back}:any){
 const resource=useResource('/settings/connection'),action=useAction(),[key,setKey]=useState('');
 const save=()=>action.run(async()=>{await api('/settings/connection',{method:'PUT',body:JSON.stringify({api_key:key})});setKey('');resource.refresh();});
 return <><button className="back" onClick={back}>← К базам</button><div className="page-heading"><h1>Подключение API</h1></div><Loading resource={resource}/><ErrorBox message={action.error}/><section className="panel"><label>OpenAI API-ключ<input type="password" autoComplete="new-password" value={key} onChange={e=>setKey(e.target.value)}/></label><div className="toolbar"><button className="primary" disabled={action.busy||key.length<20} onClick={save}>Сохранить ключ</button><button disabled={action.busy||!resource.data?.configured} onClick={()=>action.run(async()=>{await api('/settings/connection/check',{method:'POST'});resource.refresh();})}>Проверить подключение</button></div>{resource.data?.configured&&<small>{resource.data.state==='verified'?'Подключение проверено':'Ключ сохранён'}</small>}</section></>;
}
function Bases({open}:any){
 const [creating,setCreating]=useState(false),[offset,setOffset]=useState(0);
 const resource=useResource(`/offer-runs?offset=${offset}`,5000),overview=useResource('/offer-runs/overview',30000);
 if(creating)return <NewCoarse close={()=>setCreating(false)} created={open}/>;
 return <><div className="page-heading"><h1>Базы</h1><button className="primary" onClick={()=>setCreating(true)}><Plus size={17}/>Новая база</button></div><div className="base-overview" aria-label="Сводка по базам"><div className="base-stat"><span>Уникальных компаний</span><strong>{overview.data?fmt(overview.data.unique_companies):'—'}</strong></div><div className="base-stat"><span>Сегментов определено</span><strong>{overview.data?fmt(overview.data.segments_defined):'—'}</strong></div><div className="base-stat"><span>Исследовано</span><strong>{overview.data?fmt(overview.data.researched):'—'}</strong></div></div><ErrorBox message={overview.error}/><Loading resource={resource}/>{resource.data&&<><div className="run-list">{resource.data.items.map((r:Obj)=><button className="run-row" key={r.id} onClick={()=>open(r.id)}><div className="run-name"><strong>{r.name}</strong><small>{date(r.created_at)}</small></div><div className="row-progress"><span>Компаний: {fmt(r.company_count)} · контактов: {fmt(r.row_count)}</span></div><Badge value={r.status}/><ChevronRight size={17}/></button>)}{!resource.data.items.length&&!resource.loading&&<div className="empty">Баз в этом режиме пока нет</div>}</div>{resource.data.total>20&&<Pager total={resource.data.total} offset={offset} setOffset={setOffset}/>}</>}<LeadsList newLead={()=>open('new-lead')} open={(id:string)=>open('lead:'+id)}/></>;
}
function App(){
 const [page,setPage]=useState<'bases'|'settings'>('bases'),[rid,setRid]=useState<string|null>(null);
 const back=()=>{setRid(null);setPage('bases')};
 return <div className="workspace"><main><div className="content"><div className="workspace-tools"><a className="workspace-logo" href="#" aria-label="ReachLab — базы" onClick={e=>{e.preventDefault();back();}}><img src="/reachlab-logo.png" alt="ReachLab"/></a><button onClick={()=>{setRid(null);setPage(page==='settings'?'bases':'settings')}}><Settings size={16}/>{page==='settings'?'К базам':'API'}</button></div>{page==='settings'?<Connection back={back}/>:rid==='new-lead'?<NewLead back={back} created={(id:string)=>setRid('lead:'+id)}/>:rid?.startsWith('lead:')?<LeadProfile key={rid} id={rid.slice(5)} back={back}/>:rid?<CoarseDetail key={rid} id={rid} back={back}/>:<Bases open={setRid}/>}</div></main></div>;
}
createRoot(document.getElementById('root')!).render(<App/>);
