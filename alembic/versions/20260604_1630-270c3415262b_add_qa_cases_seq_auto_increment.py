"""add qa_cases.seq auto-increment

Revision ID: 270c3415262b
Revises: d158a575cc0e
Create Date: 2026-06-04 16:30:16.975450
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '270c3415262b'
down_revision: Union[str, None] = 'd158a575cc0e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 單一 ALTER 同時：加欄、設 AUTO_INCREMENT、UNIQUE 鍵，並自動回填既有列（1,2,3…）
    op.execute(
        "ALTER TABLE qa_cases "
        "ADD COLUMN seq BIGINT NOT NULL AUTO_INCREMENT UNIQUE FIRST"
    )


def downgrade() -> None:
    op.drop_column("qa_cases", "seq")
