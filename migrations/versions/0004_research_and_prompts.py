"""Separate reusable context and immutable prompt packages from research."""
from alembic import op
import sqlalchemy as sa
from app.db import ResearchContext, PromptPackage
revision='0004'
down_revision='0003'
branch_labels=None
depends_on=None

def upgrade():
    bind=op.get_bind()
    for table,columns in {
        'runs':[sa.Column('context_snapshot',sa.JSON(),nullable=False,server_default='{}'),sa.Column('reports',sa.JSON(),nullable=False,server_default='{}')],
        'model_calls':[sa.Column('package_id',sa.String(),nullable=True)],
    }.items():
        existing={c['name'] for c in sa.inspect(bind).get_columns(table)}
        for column in columns:
            if column.name not in existing:op.add_column(table,column)
    ResearchContext.__table__.create(bind,checkfirst=True)
    PromptPackage.__table__.create(bind,checkfirst=True)
    if 'ix_model_calls_package_id' not in {i['name'] for i in sa.inspect(bind).get_indexes('model_calls')}:
        op.create_index('ix_model_calls_package_id','model_calls',['package_id'])

def downgrade():
    op.drop_table('prompt_packages');op.drop_table('research_contexts')
    op.drop_index('ix_model_calls_package_id','model_calls');op.drop_column('model_calls','package_id')
    op.drop_column('runs','reports');op.drop_column('runs','context_snapshot')
