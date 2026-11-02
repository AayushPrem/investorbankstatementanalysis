"""categorisations.source: allow 'review' (Phase 4 v3 routing review queue)

The check constraint ``ck_categorisations_source`` gains 'review'. SQLite
can't ALTER a CHECK constraint and doesn't reflect CHECKs, so on SQLite the
table is rebuilt from an explicit definition (``copy_from``); Postgres drops
and re-creates the constraint in place.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-02

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: str | Sequence[str] | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = ("rule", "ml", "llm", "default", "manual")
_NEW = ("rule", "ml", "llm", "review", "default", "manual")
_CK = "ck_categorisations_source"


def _sources_sql(values: tuple[str, ...]) -> str:
    return "source IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _table(values: tuple[str, ...]) -> sa.Table:
    md = sa.MetaData()
    sa.Table("transactions", md, sa.Column("id", sa.Uuid(), primary_key=True))
    t = sa.Table(
        "categorisations", md,
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('transaction_id', sa.Uuid(), nullable=False),
        sa.Column('category', sa.String(length=40), nullable=False),
        sa.Column('source', sa.String(length=10), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('model_version', sa.String(length=40), nullable=True),
        sa.Column('rule_id', sa.String(length=40), nullable=True),
        sa.Column('is_current', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_sources_sql(values), name=_CK),
        sa.CheckConstraint('confidence IS NULL OR (confidence >= 0 AND confidence <= 1)',
                           name='ck_categorisations_confidence'),
        sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'],
                                name='fk_categorisations_transaction_id_transactions',
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_categorisations'),
    )
    sa.Index('uq_categorisations_current', t.c.transaction_id, unique=True,
             postgresql_where=sa.text('is_current'), sqlite_where=sa.text('is_current'))
    return t


def _swap(values: tuple[str, ...]) -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("categorisations", copy_from=_table(values), recreate="always"):
            pass
    else:
        op.drop_constraint(_CK, "categorisations", type_="check")
        op.create_check_constraint(op.f(_CK), "categorisations", _sources_sql(values))


def upgrade() -> None:
    _swap(_NEW)


def downgrade() -> None:
    op.execute(sa.text("UPDATE categorisations SET source = 'default' WHERE source = 'review'"))
    _swap(_OLD)
