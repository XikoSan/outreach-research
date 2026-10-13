"""Bounded public HTTP collection with DNS pinning and redirect validation."""
import socket, ssl, ipaddress, http.client, gzip, hashlib, json, time, threading
from datetime import datetime, timezone
from .config import settings
from urllib.parse import urlsplit, urljoin, urlunsplit, quote
from urllib.robotparser import RobotFileParser
from bs4 import BeautifulSoup

class SourceUnavailable(Exception):pass

def normalize_url(url):
    """Encode an IRI for HTTP without double-encoding existing percent escapes."""
    if any(ord(char)<32 or ord(char)==127 for char in url):
        raise SourceUnavailable('Управляющий символ в URL')
    try:
        p=urlsplit(url)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.port not in (None,80,443):
            raise SourceUnavailable('Недопустимый URL')
        host=p.hostname.encode('idna').decode('ascii')
        if ':' in host:host='['+host+']'
        netloc=host+(':'+str(p.port) if p.port else '')
        return urlunsplit((p.scheme,netloc,quote(p.path,safe="/%:@!$&'()*+,;=-._~"),
                          quote(p.query,safe="%/:?@!$&'()*+,;=-._~"),''))
    except (ValueError,UnicodeError) as exc:
        raise SourceUnavailable('Недопустимый URL') from exc

def public_address(url):
    p=urlsplit(url)
    if p.scheme not in ('https','http') or not p.hostname or p.username or p.password or p.port not in (None,80,443):raise SourceUnavailable('Недопустимый URL')
    addresses=socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme=='https' else 80),type=socket.SOCK_STREAM)
    ips={x[4][0] for x in addresses}
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):raise SourceUnavailable('Источник не является публичным адресом')
    return p,addresses[0][4][0]

def request(url,limit=2_000_000):
    for _ in range(4):
        url=normalize_url(url)
        p,ip=public_address(url)
        conn=http.client.HTTPConnection(p.hostname,p.port or (443 if p.scheme=='https' else 80),timeout=15)
        sock=socket.create_connection((ip,conn.port),timeout=15)
        if p.scheme=='https':sock=ssl.create_default_context().wrap_socket(sock,server_hostname=p.hostname)
        conn.sock=sock
        try:
            conn.request('GET',(p.path or '/')+('?' + p.query if p.query else ''),headers={'User-Agent':'OutreachResearchBot/0.1','Accept':'text/html,text/plain','Accept-Encoding':'identity'})
            response=conn.getresponse()
            if response.status in (301,302,303,307,308):
                url=urljoin(url,response.getheader('Location',''));continue
            if response.status>=400:raise SourceUnavailable(f'HTTP {response.status}')
            content_type=response.getheader('Content-Type','')
            if not any(t in content_type for t in ('text/html','text/plain','application/xhtml')):raise SourceUnavailable('Неподдерживаемый тип страницы')
            data=response.read(limit+1)
            if len(data)>limit:raise SourceUnavailable('Страница превышает лимит размера')
            return url,data.decode('utf-8',errors='replace')
        finally:conn.close()
    raise SourceUnavailable('Слишком много перенаправлений')

_locks_guard=threading.Lock()
_locks={}

def domain_lock(url):
    host=urlsplit(url).netloc
    with _locks_guard:return _locks.setdefault(host,threading.Lock())

def robots_allowed(url):
    p,_=public_address(url)
    robots_url=f'{p.scheme}://{p.netloc}/robots.txt'
    key=hashlib.sha256(robots_url.encode()).hexdigest()
    folder=settings.data_dir/'http-cache';folder.mkdir(parents=True,exist_ok=True)
    path=folder/(key+'.robots.json')
    try:
        value=json.loads(path.read_text())
        if time.time()-value['saved_at']>3600:raise ValueError()
        body=value['body']
    except (OSError,ValueError,KeyError):
        try:_,body=request(robots_url,100_000)
        except SourceUnavailable as e:
            if str(e)!='HTTP 404':raise
            body=''
        path.write_text(json.dumps({'saved_at':time.time(),'body':body}))
    robots=RobotFileParser();robots.parse(body.splitlines())
    if not robots.can_fetch('OutreachResearchBot',url):raise SourceUnavailable('Запрещено robots.txt')

def parse_page(final,html):
    soup=BeautifulSoup(html,'html.parser')
    title=soup.title.get_text(' ',strip=True) if soup.title else ''
    links=[]
    for a in soup.find_all('a',href=True):
        target=urljoin(final,a['href']).split('#')[0]
        if urlsplit(target).scheme in ('http','https'):
            links.append({'url':target,'label':a.get_text(' ',strip=True)[:160]})
    dates=[m.get('content','') for m in soup.find_all('meta') if m.get('property') in ('article:published_time',) or m.get('name') in ('date','pubdate')]
    for tag in soup(['script','style','nav','footer','noscript','form']):tag.decompose()
    text=' '.join(soup.get_text(' ',strip=True).split())
    if len(text)<80:raise SourceUnavailable('Недостаточно доступного текста; возможно нужна JS-загрузка')
    return {'url':final,'title':title,'text':text[:24000],'truncated':len(text)>24000,
            'links':list({x['url']:x for x in links}.values())[:200],'date_metadata':dates,
            'retrieved_at':datetime.now(timezone.utc).isoformat(),'content_hash':hashlib.sha256(text.encode()).hexdigest()}

def fetch(url):
    url=normalize_url(url)
    # Cache is public source content only; no account or contact information.
    folder=settings.data_dir/'http-cache';folder.mkdir(parents=True,exist_ok=True)
    path=folder/(hashlib.sha256(url.encode()).hexdigest()+'.page.json')
    with domain_lock(url):
        try:
            value=json.loads(path.read_text())
            if time.time()-value['saved_at']<3600:return value['page']
        except (OSError,ValueError,KeyError):pass
        robots_allowed(url)
        final,html=request(url)
        if final!=url:robots_allowed(final)
        page=parse_page(final,html)
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps({'saved_at':time.time(),'page':page},ensure_ascii=False));temp.replace(path)
        return page
