"""add qa_accounts db_name (帳號池按被測庫隔離)

切分支＝換被測庫＝換一套帳號：同 account_id 在不同庫是不同人。
故 qa_accounts 加 db_name 欄，唯一鍵改為 (db_name, account_id, role)。
既有列回填為 worky_next_staging_v30x（它們本就是 v30x 探測出來的）；
新庫（v31x…）下池為空，需重跑 provision/sync 建立。

Revision ID: b7f3a2c1d4e5
Revises: 06c5835c0c2e
Create Date: 2026-06-09 17:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7f3a2c1d4e5'
down_revision: Union[str, None] = '06c5835c0c2e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 既有列回填的舊庫名（先前 .env 漂移期間建出的池，皆為 v30x 探測）
_LEGACY_DB = 'worky_next_staging_v30x'


def upgrade() -> None:
    op.add_column('qa_accounts',
                  sa.Column('db_name', sa.String(length=64), nullable=False, server_default=''))
    # 回填既有列為舊庫；新庫下從零 provision/sync 重建一套
    op.execute(sa.text(
        "UPDATE qa_accounts SET db_name = :db WHERE db_name = ''"
    ).bindparams(db=_LEGACY_DB))
    # 唯一鍵 / 查詢索引改為含 db_name
    op.drop_index('uq_account_role', table_name='qa_accounts')
    op.drop_index('idx_role_state', table_name='qa_accounts')
    op.create_index('uq_db_account_role', 'qa_accounts',
                    ['db_name', 'account_id', 'role'], unique=True)
    op.create_index('idx_db_role_state', 'qa_accounts',
                    ['db_name', 'role', 'state'])


def downgrade() -> None:
    op.drop_index('idx_db_role_state', table_name='qa_accounts')
    op.drop_index('uq_db_account_role', table_name='qa_accounts')
    op.create_index('idx_role_state', 'qa_accounts', ['role', 'state'])
    op.create_index('uq_account_role', 'qa_accounts', ['account_id', 'role'], unique=True)
    op.drop_column('qa_accounts', 'db_name')
