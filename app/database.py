"""SQLAlchemy 引擎與 Session。MVP 用 SQLite。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from . import models  # noqa: F401  確保 model 註冊到 metadata
    Base.metadata.create_all(bind=engine)
    _migrate()
    _backfill_copies()
    _seed_vcenter()


def _migrate() -> None:
    """輕量 schema 遷移：create_all 不會替既有資料表補欄位，這裡手動 ALTER。"""
    from sqlalchemy import inspect, text

    new_columns = {
        "profiles": {
            "source_sql_instance": "VARCHAR(80) NOT NULL DEFAULT ''",
            "target_sql_instance": "VARCHAR(80) NOT NULL DEFAULT ''",
            "keep_copies": "INTEGER NOT NULL DEFAULT 1",
            "schedule_enabled": "BOOLEAN NOT NULL DEFAULT 0",
            "schedule_time": "VARCHAR(5) NOT NULL DEFAULT ''",
            "verify_enabled": "BOOLEAN NOT NULL DEFAULT 0",
            "verify_weekday": "INTEGER NOT NULL DEFAULT 6",
            "verify_time": "VARCHAR(5) NOT NULL DEFAULT ''",
            "drill_query": "TEXT NOT NULL DEFAULT ''",
            "archive_enabled": "BOOLEAN NOT NULL DEFAULT 0",
            "archive_datastore": "VARCHAR(120) NOT NULL DEFAULT ''",
            "archive_keep": "INTEGER NOT NULL DEFAULT 7",
            "chain_next_id": "INTEGER",
            "transfer_mode": "VARCHAR(10) NOT NULL DEFAULT 'shared'",
            "staging_datastore": "VARCHAR(120) NOT NULL DEFAULT ''",
            "job_type": "VARCHAR(10) NOT NULL DEFAULT 'disk'",
            "replica_name": "VARCHAR(120) NOT NULL DEFAULT ''",
            "target_network": "VARCHAR(120) NOT NULL DEFAULT ''",
            "schedule_mode": "VARCHAR(10) NOT NULL DEFAULT 'daily'",
            "schedule_date": "VARCHAR(10) NOT NULL DEFAULT ''",
        },
        "runs": {
            "kind": "VARCHAR(20) NOT NULL DEFAULT 'daily'",
        },
        "audit_logs": {
            "ip": "VARCHAR(45) NOT NULL DEFAULT ''",
        },
    }
    new_columns["profiles"]["vcenter_id"] = "INTEGER"
    new_columns["profiles"]["target_vcenter_id"] = "INTEGER"
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in new_columns.items():
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _backfill_copies() -> None:
    """功能上線前已掛載的 clone 補建 CloneCopy 紀錄（冪等）。"""
    from .models import CloneCopy, Profile

    with SessionLocal() as db:
        for p in db.query(Profile).filter(Profile.last_clone_path.isnot(None)):
            hit = (
                db.query(CloneCopy)
                .filter(CloneCopy.profile_id == p.id, CloneCopy.path == p.last_clone_path)
                .first()
            )
            if hit is None:
                db.add(CloneCopy(profile_id=p.id, path=p.last_clone_path, status="mounted"))
        db.commit()


def _seed_vcenter() -> None:
    """多 vCenter 上線前的舊設定（config.json 的單一 vCenter）種子化為第一筆
    VCenter 紀錄，並把未綁定的既有 Profile 指向它（冪等）。"""
    from .config import settings
    from .models import Profile, VCenter

    with SessionLocal() as db:
        first = db.query(VCenter).first()
        if first is None:
            if not settings.vcenter_host or settings.vcenter_host == "vcenter.example.local":
                return
            first = VCenter(
                name=settings.vcenter_host, host=settings.vcenter_host,
                user=settings.vcenter_user, password=settings.vcenter_password,
                insecure=settings.vcenter_insecure,
            )
            db.add(first)
            db.flush()
        for p in db.query(Profile).filter(Profile.vcenter_id.is_(None)):
            p.vcenter_id = first.id
        # 跨 VC 欄位上線前的既有任務：目標 vCenter 預設同來源
        for p in db.query(Profile).filter(Profile.target_vcenter_id.is_(None)):
            p.target_vcenter_id = p.vcenter_id
        db.commit()
