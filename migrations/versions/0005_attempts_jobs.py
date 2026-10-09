"""Add operation attempt history and durable company jobs without rewriting results."""
from alembic import op
import sqlalchemy as sa
from app.db import ModelAttempt,CompanyJob
revision='0005'
down_revision='0004'
branch_labels=None
depends_on=None

def upgrade():
    if 'display_fields' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('source_rows')}:
        op.add_column('source_rows',sa.Column('display_fields',sa.JSON(),nullable=False,server_default='[]'))
    for model in (ModelAttempt,CompanyJob):model.__table__.create(op.get_bind(),checkfirst=True)

def downgrade():
    op.drop_column('source_rows','display_fields')
    op.drop_table('company_jobs');op.drop_table('model_attempts')
