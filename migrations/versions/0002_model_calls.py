"""Checkpoint paid operations independently from workflow stages."""
from alembic import op
from app.db import ModelCall
revision='0002'
down_revision='0001'
branch_labels=None
depends_on=None

def upgrade():ModelCall.__table__.create(bind=op.get_bind(),checkfirst=True)
def downgrade():ModelCall.__table__.drop(bind=op.get_bind(),checkfirst=True)
