"""Add compact company-name index for all-base dashboard totals."""
from alembic import op
import sqlalchemy as sa

revision='0007'
down_revision='0006'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('base_company_names',
        sa.Column('company_id',sa.String(32),sa.ForeignKey('companies.id'),primary_key=True),
        sa.Column('run_id',sa.String(32),sa.ForeignKey('runs.id'),nullable=False),
        sa.Column('name',sa.String(),nullable=False),
        sa.Column('name_key',sa.String(),nullable=False),
        sa.Column('inn',sa.String(),nullable=False),
        sa.Column('website',sa.String(),nullable=False))
    op.create_index('ix_base_company_names_run_id','base_company_names',['run_id'])
    op.create_index('ix_base_company_names_name_key','base_company_names',['name_key'])
    op.create_index('ix_base_company_names_inn','base_company_names',['inn'])


def downgrade():
    op.drop_index('ix_base_company_names_inn',table_name='base_company_names')
    op.drop_index('ix_base_company_names_name_key',table_name='base_company_names')
    op.drop_index('ix_base_company_names_run_id',table_name='base_company_names')
    op.drop_table('base_company_names')
