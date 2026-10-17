"""Rebuild missing per-base name indexes and print all-time dashboard totals."""
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.db import Base, engine, Session
from app.base_stats import backfill_indexes, workspace_overview

Base.metadata.create_all(engine)
with Session() as db:
    backfill_indexes(db)
    print(json.dumps(workspace_overview(db),ensure_ascii=False))
