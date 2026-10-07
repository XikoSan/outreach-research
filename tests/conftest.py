import os,tempfile
from pathlib import Path
os.environ['DATA_DIR']=tempfile.mkdtemp(prefix='outreach-test-')
os.environ['DATABASE_URL']='sqlite:///'+str(Path(os.environ['DATA_DIR'])/'test.db')
import pytest
from app.db import Base,engine,Session
from app.api import app
from fastapi.testclient import TestClient
@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
    yield
@pytest.fixture
def client():
    with TestClient(app) as c:yield c
