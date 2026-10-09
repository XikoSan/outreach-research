"""Existing API adapter. Account/subscription transport is deliberately separate."""
import threading
import time
from collections import deque
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

class ProviderGate:
    def __init__(self,rpm=30,tpm=150000):
        self.rpm=rpm;self.tpm=tpm;self.events=deque();self.lock=threading.Lock();self.cooldown=0
    def wait(self,tokens,check):
        while True:
            check()
            with self.lock:
                now=time.monotonic()
                while self.events and self.events[0][0]<=now-60:self.events.popleft()
                used=sum(t for _,t in self.events)
                delay=max(0,self.cooldown-now)
                if self.events and (len(self.events)>=self.rpm or used+tokens>self.tpm):delay=max(delay,self.events[0][0]+60-now)
                if delay<=0:self.events.append((now,tokens));return
            time.sleep(min(delay,.5))
    def rejected(self,retry_after):
        try:delay=float(retry_after)
        except (ValueError,TypeError):
            try:delay=(parsedate_to_datetime(retry_after)-datetime.now(timezone.utc)).total_seconds()
            except (ValueError,TypeError):delay=5
        with self.lock:self.cooldown=max(self.cooldown,time.monotonic()+max(1,min(delay,300)))

API_GATE=ProviderGate()

class OpenAIAdapter:
    capabilities=frozenset({'structured_output','web_search','usage'})
    def generate(self,client,request,stream=False):
        if not stream:
            return client.responses.create(**request)
        completed=None
        with client.responses.create(**request,stream=True) as events:
            for event in events:
                if event.type == 'response.failed':
                    error=getattr(event.response,'error',None)
                    code=getattr(error,'code','unknown_error') if error else 'unknown_error'
                    raise RuntimeError(f'ChatGPT subscription response failed: {code}')
                if event.type == 'response.completed':
                    completed=event.response
        if completed is None:
            raise RuntimeError('ChatGPT subscription stream ended without response.completed')
        return completed
