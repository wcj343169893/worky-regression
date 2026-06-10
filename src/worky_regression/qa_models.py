"""QA 看板資料庫的 SQLAlchemy 模型（schema 的單一真實來源）。

schema 由這裡的模型定義，遷移由 Alembic 管理（autogenerate）。
`migrate()` 會先確保資料庫存在，再跑 `alembic upgrade head`，把 schema 帶到最新。
資料存取本身（QAStore）仍走顯式 SQL，沿用本專案 raw-SQL 風格；模型只負責「schema 形狀」。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.mysql import JSON, LONGTEXT
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
ALEMBIC_DIR = PROJECT_ROOT / "alembic"

_UNSET = object()


class Base(DeclarativeBase):
    pass


class QACase(Base):
    """用例註冊表：保證每筆用例都有穩定 id（YAML 仍是定義的單一真實來源）。"""
    __tablename__ = "qa_cases"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # 人類可讀的 slug 主鍵之外，並存一個自動遞增數字序號（顯示用 #1 #2…；run_id 仍用 slug）
    seq: Mapped[int] = mapped_column(BigInteger, autoincrement=True, unique=True, nullable=False)
    file: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    system: Mapped[str] = mapped_column(String(16), nullable=False, server_default="")
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="builtin")
    description: Mapped[str | None] = mapped_column(Text)
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    yaml: Mapped[str | None] = mapped_column(LONGTEXT)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # 父用例 id（主任務/子任務下鑽用）；頂層用例為 NULL
    parent_id: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(), onupdate=func.current_timestamp())

    __table_args__ = (Index("idx_parent", "parent_id"),)


class QARun(Base):
    """每次執行。"""
    __tablename__ = "qa_runs"

    run_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(128), nullable=False)
    system: Mapped[str] = mapped_column(String(16), nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="")
    description: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    passed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_at: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="run")
    # 本次執行參與的帳號快照（{role: {phone, user_id, user_type, shop_id, display_name}}）；
    # 供詳情頁展示「參與測試的手機號 / id」。帳號由池配發、每次可能不同，故隨 run 落地。
    actors: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(server_default=func.current_timestamp())

    __table_args__ = (Index("idx_case_started", "case_id", "started_at"),)


class QARunStep(Base):
    """每步結果。"""
    __tablename__ = "qa_run_steps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(160), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="")
    name: Mapped[str] = mapped_column(String(128), nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="")
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    observations: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = (Index("idx_run", "run_id", "step_index"),)


class QASetting(Base):
    """看板可編輯設定（key-value）：目前存後台管理員帳密（base/username/password）。

    與 .env 不同：.env 是部署期固定的連線/密鑰，這裡是執行期由看板 UI 編輯並持久化的設定。
    密碼需可重放到 Yii2 後台表單登入，故以明文存放；API 對外只回 password_set 布林、不外洩明文。
    """
    __tablename__ = "qa_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(), onupdate=func.current_timestamp())


class QAAccount(Base):
    """測試帳號池（QA 自管的真相）。

    用例按「能力(caps)」要帳號，runner 從這裡配發，**執行期不再直連工作庫挖帳號**。
    硬狀態(認證/停權/profile)由特權同步任務(有 DB/後台權限時)寫入 caps；執行期只讀本表。
    """
    __tablename__ = "qa_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 帳號所屬被測庫（切分支＝換庫＝換一套帳號）；同 account_id 在不同庫是不同人，故納入唯一鍵。
    db_name: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    account_id: Mapped[int] = mapped_column(Integer, nullable=False)        # worky labor/employer id
    role: Mapped[str] = mapped_column(String(32), nullable=False)          # 'labor' | 'employer'
    user_type: Mapped[int] = mapped_column(Integer, nullable=False)        # 1 商家 / 2 打工夥伴
    phone: Mapped[str] = mapped_column(String(32), nullable=False, server_default="")
    username: Mapped[str | None] = mapped_column(String(64))
    # 姓名/性別：工作庫 display_name 走 Cipher 加密、SQL 讀不到明文，只能在「拿得到解密值」
    # 的時點寫入（API 註冊時讀 /profile）；gender 在工作庫是明文 int(0 不分/1 男/2 女)，
    # 供給/同步(sync_caps)時可直接探回。兩欄都允許 NULL（拿不到就不顯示）。
    display_name: Mapped[str | None] = mapped_column(String(64))
    gender: Mapped[int | None] = mapped_column(Integer)
    shop_id: Mapped[int | None] = mapped_column(Integer)
    # 能力標籤（JSON 陣列），例：["verified","active","profile_complete","audit_role"]
    caps: Mapped[list | None] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="available")  # available|leased|disabled
    note: Mapped[str | None] = mapped_column(String(255))
    synced_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    # 軟租約：避免同一帳號被並行 run 重複借走
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    # 最近被配發的時間（unix 秒）；acquire 以此做「最久未用優先」輪換，分散每商家每日發佈上限。
    last_used_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    # ── 本地 token 快取 ───────────────────────────────────────────────────────
    # 帳號池保存每個帳號上次登入/刷新拿到的 token，配發時「有效就用、到期才刷」(見 AccountPool)。
    # token 是 JWT，可能很長，用 LONGTEXT 避免截斷。過期時間皆為 unix 秒，0 表示無/未知。
    access_token: Mapped[str | None] = mapped_column(LONGTEXT)
    refresh_token: Mapped[str | None] = mapped_column(LONGTEXT)
    access_token_expired_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    refresh_token_expired_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    token_updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    __table_args__ = (
        Index("uq_db_account_role", "db_name", "account_id", "role", unique=True),
        Index("idx_db_role_state", "db_name", "role", "state"),
    )


class QAMarkup(Base):
    """頁面標記（看板 UI 上的「mark up」標註）。

    使用者在看板任一頁開標記模式、點選元素後填一段內容，連同元素定位資訊（CSS 選擇器、
    可見文字、頁面路由、座標）與整頁截圖一起落地。狀態機：
    `pending`（待處理）→ `processing`（已被 worker 領取）→ `done` / `failed`。
    獨立的 headless Claude worker（scripts/markup_worker.py）輪詢 pending、依內容自動處理，
    把處理摘要寫回 `result`。
    """
    __tablename__ = "qa_markups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    route: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")   # 雜湊路由，如 jobs/cases
    selector: Mapped[str | None] = mapped_column(Text)            # 被選元素的 CSS 選擇器路徑
    element_text: Mapped[str | None] = mapped_column(Text)        # 被選元素的可見文字（截斷）
    rect: Mapped[dict | None] = mapped_column(JSON)               # {x,y,w,h,vw,vh,scrollX,scrollY}
    content: Mapped[str] = mapped_column(Text, nullable=False)    # 使用者填寫的標記內容
    screenshot_path: Mapped[str | None] = mapped_column(String(255))  # 截圖相對路徑（results/markups/*.png）
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")  # pending|processing|done|failed
    # 使用者「已解決」開關（獨立於 worker 處理狀態 status）：1=已解決 → 源頁面不再畫框；
    # 取消解決（改回 0）源頁面重新顯示。純前端可視化的隱藏/顯示，不影響 worker 處理。
    resolved: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    result: Mapped[str | None] = mapped_column(LONGTEXT)          # worker 最近一次處理摘要 / Claude 輸出
    # 使用者對處理結果的追加回覆串 [{text,at}]；送出回覆會把 status 打回 pending，worker 帶著
    # 「上次 result + 回覆串」重新處理（針對同一問題再次優化）。
    replies: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(), onupdate=func.current_timestamp())

    __table_args__ = (Index("idx_status_created", "status", "created_at"),)


# ── 連線 / URL ───────────────────────────────────────────────────────────────
def db_url(settings: Settings, database=_UNSET) -> URL:
    """組 SQLAlchemy 連線 URL；database=None → 連到 server（不指定庫，供建庫用）。"""
    db = settings.qa_db_name if database is _UNSET else database
    return URL.create(
        "mysql+pymysql",
        username=settings.db_user,
        password=settings.db_pass,
        host=settings.db_host,
        port=settings.db_port,
        database=db,
        query={"charset": "utf8mb4"},
    )


_engines: dict[str, Engine] = {}


def get_engine(settings: Settings) -> Engine:
    key = f"{settings.db_host}:{settings.db_port}/{settings.qa_db_name}"
    if key not in _engines:
        _engines[key] = create_engine(db_url(settings), pool_pre_ping=True, future=True)
    return _engines[key]


def bootstrap_database(settings: Settings) -> None:
    """建庫（Alembic 只管表，不會建 database 本身）。"""
    eng = create_engine(db_url(settings, database=None), future=True)
    try:
        with eng.connect() as conn:
            conn.execute(text(
                f"CREATE DATABASE IF NOT EXISTS `{settings.qa_db_name}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
            conn.commit()
    finally:
        eng.dispose()


def migrate(settings: Settings | None = None) -> None:
    """確保資料庫存在並把 schema 帶到最新（alembic upgrade head）。"""
    from alembic import command
    from alembic.config import Config

    settings = settings or Settings.from_env()
    bootstrap_database(settings)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    command.upgrade(cfg, "head")
