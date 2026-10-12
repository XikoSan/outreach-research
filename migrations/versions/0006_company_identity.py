"""Allow distinct companies to share a source website within one research run."""
from alembic import op
import sqlalchemy as sa

revision='0006'
down_revision='0005'
branch_labels=None
depends_on=None


def _resource_unique(bind):
    return next((item for item in sa.inspect(bind).get_unique_constraints('companies')
                 if set(item['column_names'])=={'run_id','resource'}),None)


def _rebuild(drop_unique):
    bind=op.get_bind()
    sqlite=bind.dialect.name=='sqlite'
    if sqlite:
        bind.exec_driver_sql('PRAGMA foreign_keys=OFF')
        if bind.exec_driver_sql('PRAGMA foreign_keys').scalar_one()!=0:
            raise RuntimeError('SQLite foreign keys could not be disabled for table rebuild')
    try:
        with op.batch_alter_table('companies',recreate='always',
                naming_convention={'uq':'uq_%(table_name)s_%(column_0_name)s'}) as batch:
            if drop_unique:batch.drop_constraint('uq_companies_run_id','unique')
            else:batch.create_unique_constraint('uq_companies_run_resource',['run_id','resource'])
        if sqlite and bind.exec_driver_sql('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Company table rebuild broke foreign key references')
    finally:
        if sqlite:bind.exec_driver_sql('PRAGMA foreign_keys=ON')


def upgrade():
    bind=op.get_bind()
    constraint=_resource_unique(bind)
    if not constraint:return
    if bind.dialect.name=='sqlite':_rebuild(True)
    else:op.drop_constraint(constraint['name'],'companies',type_='unique')


def downgrade():
    bind=op.get_bind()
    if _resource_unique(bind):return
    if bind.dialect.name=='sqlite':_rebuild(False)
    else:op.create_unique_constraint('uq_companies_run_resource','companies',['run_id','resource'])
