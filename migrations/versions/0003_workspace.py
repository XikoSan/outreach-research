"""Stable sender profiles, archived records and research names."""
from alembic import op
import sqlalchemy as sa

revision='0003'
down_revision='0002'
branch_labels=None
depends_on=None

def upgrade():
    bind=op.get_bind()
    for table, columns in {
        'senders':[sa.Column('archived',sa.Boolean(),nullable=False,server_default=sa.false()),sa.Column('history',sa.JSON(),nullable=False,server_default='[]')],
        'runs':[sa.Column('name',sa.String(),nullable=False,server_default='')],
    }.items():
        existing={c['name'] for c in sa.inspect(bind).get_columns(table)}
        for column in columns:
            if column.name not in existing:op.add_column(table,column)
    op.execute("UPDATE runs SET name=filename WHERE name=''")
    profiles=sa.Table('senders',sa.MetaData(),autoload_with=bind)
    rows=bind.execute(sa.select(profiles).order_by(profiles.c.version.desc(),profiles.c.created_at.desc())).mappings().all()
    latest={}
    aliases={}
    for row in rows:
        brand=row['profile'].get('brand','')
        if brand not in latest:latest[brand]=dict(row)
        else:
            current=latest[brand]
            aliases[row['id']]=current['id']
            current['history']=current['history']+[{'version':row['version'],'profile':row['profile'],'saved_at':row['created_at']}]
            bind.execute(profiles.update().where(profiles.c.id==row['id']).values(archived=True))
    for row in latest.values():
        bind.execute(profiles.update().where(profiles.c.id==row['id']).values(history=row['history']))
    # Preserve frozen profile content/version, but route old runs to the stable profile ID.
    runs=sa.Table('runs',sa.MetaData(),autoload_with=bind)
    for row in bind.execute(sa.select(runs.c.id,runs.c.sender_snapshot)).mappings():
        snapshot=row['sender_snapshot']
        if snapshot['id'] in aliases:
            bind.execute(runs.update().where(runs.c.id==row['id']).values(sender_snapshot={**snapshot,'id':aliases[snapshot['id']]}))

def downgrade():
    op.drop_column('runs','name')
    op.drop_column('senders','history')
    op.drop_column('senders','archived')
