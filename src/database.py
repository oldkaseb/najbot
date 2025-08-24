
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
from .config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

async def _table_exists(conn, table_name: str) -> bool:
    q = text("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = :t
        ) AS ok
    """)
    res = await conn.execute(q, {"t": table_name})
    return bool(res.scalar())

async def _has_columns(conn, table: str, cols: list[str]) -> bool:
    q = text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=:t
    """)
    res = await conn.execute(q, {"t": table})
    have = {r[0] for r in res}
    return all(c in have for c in cols)

async def _safe_exec(conn, sql: str):
    try:
        await conn.execute(text(sql))
    except Exception:
        pass

async def init_db():
    from .models import User, Group, Whisper, Pending, Watch  # noqa
    async with engine.begin() as conn:
        # create dg_* tables if not exist
        await conn.run_sync(Base.metadata.create_all)

        legacy = ["users", "groups", "whispers", "pendings", "watches"]
        any_legacy = False
        for t in legacy:
            if await _table_exists(conn, t):
                any_legacy = True
                break

        if any_legacy:
            # users -> dg_users
            if await _table_exists(conn, "users"):
                if await _has_columns(conn, "users", ["id", "first_name", "username", "is_blocked", "joined_at"]):
                    await _safe_exec(conn, """
                        INSERT INTO dg_users(id, first_name, username, is_blocked, joined_at)
                        SELECT id, first_name, username, COALESCE(is_blocked,false), COALESCE(joined_at, NOW())
                        FROM users
                        ON CONFLICT (id) DO NOTHING
                    """)
                await _safe_exec(conn, "DROP TABLE IF EXISTS users CASCADE;")

            # groups -> dg_groups
            if await _table_exists(conn, "groups"):
                if await _has_columns(conn, "groups", ["id", "title", "active", "last_seen"]):
                    await _safe_exec(conn, """
                        INSERT INTO dg_groups(id, title, active, last_seen)
                        SELECT id, title, COALESCE(active, true), COALESCE(last_seen, NOW())
                        FROM groups
                        ON CONFLICT (id) DO NOTHING
                    """)
                await _safe_exec(conn, "DROP TABLE IF EXISTS groups CASCADE;")

            # whispers -> dg_whispers
            if await _table_exists(conn, "whispers"):
                if await _has_columns(conn, "whispers", ["id", "group_id", "sender_id", "recipient_id", "text", "created_at", "read_at", "group_message_id"]):
                    await _safe_exec(conn, """
                        INSERT INTO dg_whispers(id, group_id, sender_id, recipient_id, text, created_at, read_at, group_message_id)
                        SELECT id, group_id, sender_id, recipient_id, text, COALESCE(created_at,NOW()), read_at, group_message_id
                        FROM whispers
                        ON CONFLICT (id) DO NOTHING
                    """)
                await _safe_exec(conn, "DROP TABLE IF EXISTS whispers CASCADE;")

            # pendings -> dg_pendings
            if await _table_exists(conn, "pendings"):
                if await _has_columns(conn, "pendings", ["sender_id", "recipient_id", "group_id", "created_at"]):
                    await _safe_exec(conn, """
                        INSERT INTO dg_pendings(sender_id, recipient_id, group_id, created_at)
                        SELECT sender_id, recipient_id, group_id, COALESCE(created_at,NOW())
                        FROM pendings
                        ON CONFLICT (sender_id) DO NOTHING
                    """)
                await _safe_exec(conn, "DROP TABLE IF EXISTS pendings CASCADE;")

            # watches -> dg_watches
            if await _table_exists(conn, "watches"):
                if await _has_columns(conn, "watches", ["id", "group_id", "user_id"]):
                    await _safe_exec(conn, """
                        INSERT INTO dg_watches(id, group_id, user_id)
                        SELECT id, group_id, user_id
                        FROM watches
                        ON CONFLICT (id) DO NOTHING
                    """)
                await _safe_exec(conn, "DROP TABLE IF EXISTS watches CASCADE;")
