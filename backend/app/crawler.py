"""Bounded public HTTP collection with DNS pinning and redirect validation."""
import socket, ssl, ipaddress, http.client, gzip
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser
from bs4 import BeautifulSoup

class SourceUnavailable(Exception):pass

def public_address(url):
    p=urlsplit(url)
    if p.scheme not in ('https','http') or not p.hostname or p.username or p.password or p.port not in (None,80,443):raise SourceUnavailable('Недопустимый URL')
    addresses=socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme=='https' else 80),type=socket.SOCK_STREAM)
    ips={x[4][0] for x in addresses}
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):raise SourceUnavailable('Источник не является публичным адресом')
    return p,addresses[0][4][0]

def request(url,limit=2_000_000):
    for _ in range(4):
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

def fetch(url):
    p,_=public_address(url)
    robots_url=f'{p.scheme}://{p.netloc}/robots.txt'
    try:
        _,body=request(robots_url,100_000)
        robots=RobotFileParser();robots.parse(body.splitlines())
        if not robots.can_fetch('OutreachResearchBot',url):raise SourceUnavailable('Запрещено robots.txt')
    except SourceUnavailable as e:
        if str(e)!='HTTP 404':raise
    final,html=request(url)
    soup=BeautifulSoup(html,'html.parser')
    title=soup.title.get_text(' ',strip=True) if soup.title else ''
    for tag in soup(['script','style','nav','footer','noscript','form']):tag.decompose()
    text=' '.join(soup.get_text(' ',strip=True).split())
    if len(text)<80:raise SourceUnavailable('Недостаточно доступного текста; возможно нужна JS-загрузка')
    return {'url':final,'title':title,'text':text[:24000],'truncated':len(text)>24000}
