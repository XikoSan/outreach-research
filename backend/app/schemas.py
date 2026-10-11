from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')
class Offer(Strict):
    id: str
    name: str
    description: str
    outcomes: str
    limits: str
class SenderProfile(Strict):
    profile_name: str = ''
    brand: str = Field(min_length=1, max_length=200)
    website: str = ''
    description: str = ''
    legal_name: str = ''
    country: str = ''
    tax_id: str = ''
    address: str = ''
    representative: str = ''
    title: str = ''
    signature: str = ''
    subject_rule: str = ''
    contacts: str = ''
    approved_claims: str = ''
    materials: str = ''
    offers: list[Offer] = Field(default_factory=list)
class RunConfig(Strict):
    methodology_version: Literal['legacy','campaign-v1'] = 'legacy'
    max_targeted_research_queries: int = Field(default=3, ge=0, le=3)
    max_targeted_research_pages: int = Field(default=5, ge=0, le=5)
    max_search_queries: int = Field(default=6, ge=1, le=20)
    max_website_pages: int = Field(default=8, ge=1, le=30)
    max_external_sources: int = Field(default=8, ge=1, le=30)
    max_social_sources: int = Field(default=3, ge=1, le=10)
    max_review_sources: int = Field(default=5, ge=1, le=20)
    social_forum_days: int = Field(default=30, ge=1, le=365)
    max_retries: int = Field(default=2, ge=0, le=3)
    max_companies: int | None = Field(default=None, ge=1)
    extraction_tokens: int = Field(default=5000, ge=1500, le=10000)
    strategy_tokens: int = Field(default=10000, ge=3000, le=24000)
    models: dict[str,str] = Field(default_factory=dict)
class CandidateSource(Strict):
    url: str
    direction: Literal['website','social','news_jobs','reviews','forums']
    rationale: str
class Discovery(Strict):
    identity_quote: str = ""
    checked_directions: list[Literal["website","social","news_jobs","reviews","forums"]] = Field(default_factory=list)
    identity_confirmed: bool
    identity_reason: str
    products: str
    sources: list[CandidateSource]
class EvidenceItem(Strict):
    date_evidence: str = ""
    subject_quote: str = ""
    provenance_quote: str = ""
    source_relation: Literal["primary","repost","unknown"] = "unknown"
    id: str
    url: str
    quote: str
    published_at: str | None
    scope: Literal['company','product','category']
    origin: str
class Signal(Strict):
    name: str
    label: str = ''
    state: Literal['confirmed','refuted','unknown']
    confidence: Literal['HIGH','MEDIUM','LOW']
    evidence_ids: list[str]
    explanation: str
class ResearchReport(Strict):
    summary: str
    evidence: list[EvidenceItem]
    signals: list[Signal]
    contradictions: list[str]
    limitations: list[str]
class Profile(Strict):
    description: str
    products: str
    audiences: str
    signals: list[Signal]
    contradictions: list[str]
    limitations: list[str]
class Segment(Strict):
    id: str
    name: str
    definition: str
    offer_id: str
    hypothesis: str
    confidence: Literal['HIGH','MEDIUM']
    required_signals: list[str] = Field(min_length=1)
    supporting_signals: list[str]
    contradicting_signals: list[str]
    outreach_angle: str
    diagnostic_question: str
    allowed_claims: list[str]
    forbidden_claims: list[str]
class Segments(Strict):
    segments: list[Segment]
    abstention_reason: str = ''
    missing_data: list[str] = Field(default_factory=list)
class Review(Strict):
    passed: bool
    issues: list[str]
class PromptResult(Strict):
    text: str
