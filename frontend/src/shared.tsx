import {ArrowUpRight} from 'lucide-react';
import React,{useEffect,useState,useCallback} from 'react';
export type Obj=Record<string,any>;
export const labels:Obj={READY_FOR_GENERATION:'Готово для генерации',PROMPT_RUNNING:'Подготовка промпта',CREATED:'Готово к запуску',QUEUED:'В очереди',RUNNING:'Исследуется',PAUSING:'Останавливается',PAUSED:'Приостановлено',COMPLETED:'Завершено',COMPLETED_WITH_ISSUES:'С ограничениями',FAILED:'Ошибка',EXCLUDED:'Исключена',NOT_PROCESSED:'Ожидает обработки',NEEDS_REVIEW:'Нужна проверка',INSUFFICIENT_DATA:'Недостаточно данных',AMBIGUOUS:'Неоднозначно',UNCLASSIFIED:'Без сегмента',IMPORTED:'База импортирована',RESEARCH:'Исследование источников',COLLECTING:'Сбор материалов',DISCOVERY:'Поиск источников',EXTRACTION:'Извлечение фактов',SYNTHESIS:'Сопоставление фактов',SEGMENTATION:'Формирование сегментов',CLASSIFICATION:'Распределение компаний',PROMPT:'Подготовка промптов',EXPORT:'Подготовка файлов',EXPORTED:'Исследование завершено',COARSE_CLASSIFICATION:'Группировка базы',COARSE_READY:'База сгруппирована',GENERAL:'Общее',DO_NOT_SEND:'Не отправлять',PROFILE_READY:'Профиль собран',PENDING:'Ожидает обработки',IDENTITY:'Проверка компании',RESEARCHING:'Сбор фактов',CONFIRMED:'Подтверждена',UNVERIFIED:'Не проверена'};
export const directions:Obj={website:'Сайт',social:'Социальные сети',jobs:'Вакансии',reviews:'Отзывы',forums:'Форумы'};
export const attention=['FAILED','NEEDS_REVIEW','INSUFFICIENT_DATA','AMBIGUOUS','UNCLASSIFIED'];
export const active=(status:string)=>['RUNNING','QUEUED','PAUSING'].includes(status);
export const title=(value:string)=>labels[value]||value;
export const fmt=(n:number)=>new Intl.NumberFormat('ru-RU').format(n||0);
export const money=(n:number)=>'$'+Number(n||0).toFixed(2);
export const date=(s:string)=>s?new Date(s).toLocaleDateString('ru-RU'):'Не указана';
export const profileName=(s:Obj)=>s.profile.profile_name||s.profile.brand;
export const json=(body:Obj,method='POST')=>({method,body:JSON.stringify(body)});
export async function api(path:string,options:RequestInit={}):Promise<any>{
 const response=await fetch('/api'+path,{...options,headers:options.body instanceof FormData?options.headers:{'Content-Type':'application/json',...options.headers}});
 if(!response.ok){let detail='Не удалось выполнить запрос';try{const e=await response.json();detail=typeof e.detail==='string'?e.detail:'Проверьте заполнение полей';}catch{}throw new Error(detail);}
 return response.json();
}
export function useResource(path:string|null,interval=0){
 const [data,setData]=useState<any>(null),[error,setError]=useState(''),[loading,setLoading]=useState(true),[revision,setRevision]=useState(0),[loadedPath,setLoadedPath]=useState<string|null>(null);
 const refresh=useCallback(()=>setRevision(n=>n+1),[]);
 useEffect(()=>{let alive=true,pending=false;setLoading(true);setError('');
   if(!path){setData(null);setLoading(false);return;}
   async function load(){if(pending)return;pending=true;try{const result=await api(path!);if(alive){setData(result);setLoadedPath(path);setError('');}}catch(e){if(alive)setError((e as Error).message);}finally{pending=false;if(alive)setLoading(false);}}
   load();const timer=interval?setInterval(load,interval):undefined;return()=>{alive=false;if(timer)clearInterval(timer);};
 },[path,interval,revision]);
 return {data:loadedPath===path?data:null,error,loading,refresh};
}
export function useAction(){const [busy,setBusy]=useState(false),[error,setError]=useState('');async function run(fn:()=>Promise<any>){setBusy(true);setError('');try{await fn();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}return {busy,error,run};}
export function ErrorBox({message,retry}:{message:string,retry?:()=>void}){return message?<div className="alert" role="alert"><span>{message}</span>{retry&&<button onClick={retry}>Повторить</button>}</div>:null;}
export function Loading({resource}:any){return <><ErrorBox message={resource.error} retry={resource.refresh}/>{resource.loading&&!resource.data&&<p className="empty">Загрузка…</p>}</>;}
export function Badge({value}:{value:string}){return <span className={'badge '+(attention.includes(value)||value==='COMPLETED_WITH_ISSUES'?'warning':value==='RUNNING'?'running':'')}>{title(value)}</span>;}
export function Pager({total,offset,setOffset,size=20}:any){return <div className="pagination"><span>{total?`${fmt(offset+1)}–${fmt(Math.min(offset+size,total))} из ${fmt(total)}`:'0 записей'}</span><button disabled={!offset} onClick={()=>setOffset(Math.max(0,offset-size))}>Назад</button><button disabled={offset+size>=total} onClick={()=>setOffset(offset+size)}>Далее</button></div>;}
export function Link({url,children}:any){let safe=false;try{safe=['http:','https:'].includes(new URL(url).protocol);}catch{}return safe?<a href={url} target="_blank" rel="noopener noreferrer" onClick={e=>e.stopPropagation()}>{children||url}<ArrowUpRight size={13}/></a>:<span>{children||'Не указан'}</span>;}

export const blankProfile=()=>({profile_name:'',brand:'',website:'',description:'',legal_name:'',country:'',tax_id:'',address:'',representative:'',title:'',signature:'',subject_rule:'',contacts:'',approved_claims:'',materials:'',offers:[]});

export const issueLabels:Obj={NEEDS_REVIEW:'Не подтверждена компания',INSUFFICIENT_DATA:'Недостаточно данных',FAILED:'Ошибка обработки',AMBIGUOUS:'Подходят несколько гипотез',UNCLASSIFIED:'Не подошла ни одна гипотеза'};
