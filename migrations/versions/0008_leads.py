"""Store interested respondents and their independent follow-up reports."""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'leads',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('source_run_id', sa.String(32), sa.ForeignKey('runs.id'), nullable=False),
        sa.Column('company_id', sa.String(32), sa.ForeignKey('companies.id'), nullable=False),
        sa.Column('source_row_id', sa.String(32), sa.ForeignKey('source_rows.id'), nullable=False),
        sa.Column('research_run_id', sa.String(32), sa.ForeignKey('runs.id'), nullable=False),
        sa.Column('respondent', sa.JSON(), nullable=False),
        sa.Column('reply', sa.Text(), nullable=False),
        sa.Column('offer', sa.Text(), nullable=False),
        sa.Column('depth', sa.String(), nullable=False),
        sa.Column('stage', sa.String(), nullable=False),
        sa.Column('pages', sa.JSON(), nullable=False),
        sa.Column('report', sa.JSON(), nullable=False),
        sa.Column('letter', sa.Text(), nullable=False),
        sa.Column('letter_status', sa.String(), nullable=False),
        sa.Column('selected_recommendation', sa.String(), nullable=False),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.Column('updated_at', sa.String(), nullable=False),
        sa.UniqueConstraint('company_id'),
        sa.UniqueConstraint('research_run_id'),
    )
    op.create_index('ix_leads_source_run_id', 'leads', ['source_run_id'])
    op.create_index('ix_leads_company_id', 'leads', ['company_id'])


def downgrade():
    op.drop_index('ix_leads_company_id', table_name='leads')
    op.drop_index('ix_leads_source_run_id', table_name='leads')
    op.drop_table('leads')
