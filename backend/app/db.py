import uuid
from datetime import datetime, timezone
from sqlalchemy import create_engine, String, Text, JSON, Integer, Float, ForeignKey, UniqueConstraint, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .config import settings

def uid(): return uuid.uuid4().hex

def now(): return datetime.now(timezone.utc).isoformat()

class Base(DeclarativeBase): pass
class Sender(Base):
    __tablename__ = 'senders'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    version: Mapped[int] = mapped_column(Integer, default=1)
    profile: Mapped[dict] = mapped_column(JSON)
    archived: Mapped[bool] = mapped_column(default=False)
    history: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(String, default=now)
class Run(Base):
    __tablename__ = 'runs'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    filename: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String, default='')
    source_path: Mapped[str] = mapped_column(String)
    source_hash: Mapped[str] = mapped_column(String)
    sender_snapshot: Mapped[dict] = mapped_column(JSON)
    context_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    reports: Mapped[dict] = mapped_column(JSON, default=dict)
    config: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, default='CREATED')
    stage: Mapped[str] = mapped_column(String, default='IMPORTED')
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    company_count: Mapped[int] = mapped_column(Integer, default=0)
    spent: Mapped[float] = mapped_column(Float, default=0)
    reserved: Mapped[float] = mapped_column(Float, default=0)
    budget: Mapped[float] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    segments: Mapped[list] = mapped_column(JSON, default=list)
    segmentation_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String, default=now)
class Company(Base):
    __tablename__ = 'companies'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey('runs.id'), index=True)
    name: Mapped[str] = mapped_column(String)
    resource: Mapped[str] = mapped_column(String)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    identity: Mapped[str] = mapped_column(String, default='UNVERIFIED')
    stage: Mapped[str] = mapped_column(String, default='PENDING')
    group: Mapped[str] = mapped_column(String, default='NOT_PROCESSED')
    research: Mapped[dict] = mapped_column(JSON, default=dict)
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default='')
class BaseCompanyName(Base):
    """Compact per-base company list used only for the workspace overview."""
    __tablename__ = 'base_company_names'
    company_id: Mapped[str] = mapped_column(ForeignKey('companies.id'), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey('runs.id'), index=True)
    name: Mapped[str] = mapped_column(String)
    name_key: Mapped[str] = mapped_column(String, index=True)
    inn: Mapped[str] = mapped_column(String, default='', index=True)
    website: Mapped[str] = mapped_column(String, default='')
class SourceRow(Base):
    display_fields: Mapped[list] = mapped_column(JSON,default=list)
    __tablename__ = 'source_rows'
    __table_args__ = (UniqueConstraint('run_id','sheet','row_index'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey('runs.id'), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey('companies.id'), index=True)
    sheet: Mapped[str] = mapped_column(String)
    row_index: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String)
class Lead(Base):
    """One interested respondent and one independent follow-up research per company."""
    __tablename__ = 'leads'
    __table_args__ = (UniqueConstraint('company_id'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    source_run_id: Mapped[str] = mapped_column(ForeignKey('runs.id'), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey('companies.id'), index=True)
    source_row_id: Mapped[str] = mapped_column(ForeignKey('source_rows.id'))
    research_run_id: Mapped[str] = mapped_column(ForeignKey('runs.id'), unique=True)
    respondent: Mapped[dict] = mapped_column(JSON, default=dict)
    reply: Mapped[str] = mapped_column(Text)
    offer: Mapped[str] = mapped_column(Text)
    depth: Mapped[str] = mapped_column(String)
    stage: Mapped[str] = mapped_column(String, default='COLLECTING')
    pages: Mapped[list] = mapped_column(JSON, default=list)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    letter: Mapped[str] = mapped_column(Text, default='')
    letter_status: Mapped[str] = mapped_column(String, default='NOT_REQUESTED')
    selected_recommendation: Mapped[str] = mapped_column(String, default='')
    created_at: Mapped[str] = mapped_column(String, default=now)
    updated_at: Mapped[str] = mapped_column(String, default=now)

class Event(Base):
    __tablename__ = 'events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey('runs.id'), index=True)
    company_id: Mapped[str | None] = mapped_column(String, nullable=True)
    stage: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=now)
class Export(Base):
    __tablename__ = 'exports'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey('runs.id'), index=True)
    path: Mapped[str] = mapped_column(String)
    manifest: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now)

engine = create_engine(settings.database_url, connect_args={'check_same_thread': False, 'timeout':30} if settings.database_url.startswith('sqlite') else {}, pool_pre_ping=True)
if settings.database_url.startswith('sqlite'):
    @event.listens_for(engine, 'connect')
    def configure_sqlite(dbapi_connection, _):
        dbapi_connection.execute('PRAGMA foreign_keys=ON')
        dbapi_connection.execute('PRAGMA journal_mode=WAL')
Session = sessionmaker(engine, expire_on_commit=False)

def log(db, run_id, stage, message, company_id=None, metrics=None):
    db.add(Event(run_id=run_id, stage=stage, message=message, company_id=company_id, metrics=metrics or {}))

class ModelCall(Base):
    __tablename__ = 'model_calls'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey('runs.id'), index=True)
    company_id: Mapped[str | None] = mapped_column(String, nullable=True)
    package_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    role: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    reservation: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float, default=0)
    response_id: Mapped[str | None] = mapped_column(String, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    search: Mapped[bool] = mapped_column(default=False)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=now)

class ResearchContext(Base):
    __tablename__ = 'research_contexts'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String)
    summary: Mapped[str] = mapped_column(Text)
    source_text: Mapped[str] = mapped_column(Text, default='')
    source_name: Mapped[str] = mapped_column(String, default='')
    source_path: Mapped[str] = mapped_column(String, default='')
    created_at: Mapped[str] = mapped_column(String, default=now)

class PromptPackage(Base):
    __tablename__ = 'prompt_packages'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey('runs.id'), index=True)
    name: Mapped[str] = mapped_column(String)
    segment_snapshot: Mapped[dict] = mapped_column(JSON)
    sender_snapshot: Mapped[dict] = mapped_column(JSON)
    context_snapshot: Mapped[dict] = mapped_column(JSON)
    selection: Mapped[dict] = mapped_column(JSON)
    recipient_type: Mapped[str] = mapped_column(String)
    next_step: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String, default='QUEUED')
    stage: Mapped[str] = mapped_column(String, default='PROMPT')
    prompt: Mapped[str] = mapped_column(Text, default='')
    review: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    spent: Mapped[float] = mapped_column(Float, default=0)
    reserved: Mapped[float] = mapped_column(Float, default=0)
    budget: Mapped[float] = mapped_column(Float)
    path: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=now)

class ModelAttempt(Base):
    __tablename__='model_attempts'
    id: Mapped[str] = mapped_column(String(32),primary_key=True,default=uid)
    operation_id: Mapped[str] = mapped_column(ForeignKey('model_calls.id'),index=True)
    state: Mapped[str] = mapped_column(String,default='STARTED')
    response_id: Mapped[str | None] = mapped_column(String,nullable=True)
    metrics: Mapped[dict] = mapped_column(JSON,default=dict)
    cost: Mapped[float | None] = mapped_column(Float,nullable=True)
    reservation: Mapped[float] = mapped_column(Float,default=0)
    created_at: Mapped[str] = mapped_column(String,default=now)

class CompanyJob(Base):
    __tablename__='company_jobs'
    __table_args__=(UniqueConstraint('run_id','company_id','revision'),)
    id: Mapped[str] = mapped_column(String(32),primary_key=True,default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey('runs.id'),index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey('companies.id'),index=True)
    revision: Mapped[int] = mapped_column(Integer,default=1)
    state: Mapped[str] = mapped_column(String,default='PENDING')
    owner: Mapped[str | None] = mapped_column(String,nullable=True)
    lease_until: Mapped[float] = mapped_column(Float,default=0)
    stage: Mapped[str] = mapped_column(String,default='PENDING')
