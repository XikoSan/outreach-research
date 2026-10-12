"""Existing API adapter. Account/subscription transport is deliberately separate."""
import threading
import time
import math
import re
from collections import deque
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

def estimate_request_tokens(instructions, payload, schema, max_output, search=False):
    """Conservative admission estimate, corrected with provider usage after completion.

    Counting every UTF-8 byte as a token needlessly penalizes Cyrillic pages. Count
    ASCII characters individually and allow up to byte-length minus one tokens for
    other codepoints, then add protocol headroom. Search can add unseen tool text,
    so it receives a separate reserve rather than inheriting the small request size.
    """
    visible = ''.join((instructions, payload, schema))
    input_estimate = sum(max(1, len(char.encode('utf-8')) - 1) for char in visible)
    return math.ceil(input_estimate * 1.2) + max_output + 512 + (24_000 if search else 0)


def _seconds(value):
    """Parse provider reset durations such as 500ms, 1s, 2m, or 1h30m."""
    if not value:
        return None
    value = str(value).strip().lower()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    parts = list(re.finditer(r'(\d+(?:\.\d+)?)(ms|s|m|h)', value))
    if not parts or ''.join(p.group(0) for p in parts) != value:
        return None
    weights = {'ms': .001, 's': 1, 'm': 60, 'h': 3600}
    return sum(float(p.group(1)) * weights[p.group(2)] for p in parts)


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
                if delay<=0:
                    ticket=[now,tokens]
                    self.events.append(ticket)
                    return ticket
            time.sleep(min(delay,.5))
    def settle(self,ticket,actual_tokens):
        # The ticket may already have expired from the rolling window.
        if ticket is not None:
            with self.lock:ticket[1]=max(0,int(actual_tokens))
    def rejected(self,headers):
        if hasattr(headers,'get'):
            retry_after=headers.get('retry-after')
            resets=[_seconds(headers.get(name)) for name in ('x-ratelimit-reset-requests','x-ratelimit-reset-tokens')]
        else:
            retry_after=headers
            resets=[]
        try:delay=float(retry_after)
        except (ValueError,TypeError):
            try:delay=(parsedate_to_datetime(retry_after)-datetime.now(timezone.utc)).total_seconds()
            except (ValueError,TypeError):delay=5
        delay=max([delay]+[seconds for seconds in resets if seconds is not None])
        with self.lock:self.cooldown=max(self.cooldown,time.monotonic()+max(1,min(delay,86400)))

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
