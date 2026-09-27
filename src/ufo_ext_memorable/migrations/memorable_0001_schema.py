"""Traces of one turn, and the procedures Memorable extracted from them."""

import sqlalchemy as sa
from alembic import op

revision: str = "memorable_0001"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = ("memorable",)
depends_on: str | None = "20260927025054"


def upgrade() -> None:
    op.create_table(
        "memorable_trace",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("recorded_ns", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "memorable_trace_turn",
        "memorable_trace",
        ["workspace_id", "turn_id"],
    )
    op.create_table(
        "memorable_procedure",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("steps", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "memorable_procedure_subject",
        "memorable_procedure",
        ["workspace_id", "subject"],
    )


def downgrade() -> None:
    op.drop_index("memorable_procedure_subject", table_name="memorable_procedure")
    op.drop_table("memorable_procedure")
    op.drop_index("memorable_trace_turn", table_name="memorable_trace")
    op.drop_table("memorable_trace")
