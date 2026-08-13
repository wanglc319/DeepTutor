"""
MySQL → PostgreSQL: tag_qywx 表建 schema + 批量 upsert

用法: python scripts/sql/03_tag_qywx_init.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tag_qywx.init")

MYSQL_DUMP = Path(r"C:\Users\Admin\Downloads\tag_qywx.sql")

# 表字段 (和 MySQL dump INSERT 里的列顺序一致)
COLS = [
    "tag_id", "tag_name", "tag_pid", "create_time", "sort",
    "level", "corpid", "created", "modified", "creator", "modifier", "is_deleted",
]

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS tag_qywx (
    tag_id      VARCHAR(64)  PRIMARY KEY,
    tag_name    VARCHAR(128) NOT NULL,
    tag_pid     VARCHAR(64),
    create_time VARCHAR(32),
    sort        BIGINT,
    level       SMALLINT,
    corpid      VARCHAR(32),
    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    modified    TIMESTAMPTZ NOT NULL DEFAULT now(),
    creator     VARCHAR(32),
    modifier    VARCHAR(32),
    is_deleted  BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_tag_qywx_name  ON tag_qywx (tag_name);
CREATE INDEX IF NOT EXISTS idx_tag_qywx_pid  ON tag_qywx (tag_pid);
CREATE INDEX IF NOT EXISTS idx_tag_qywx_del  ON tag_qywx (is_deleted);
"""

UPSERT_SQL = f"""
INSERT INTO tag_qywx ({", ".join(COLS)})
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
ON CONFLICT (tag_id) DO UPDATE SET
    tag_name   = EXCLUDED.tag_name,
    tag_pid    = EXCLUDED.tag_pid,
    create_time= EXCLUDED.create_time,
    sort       = EXCLUDED.sort,
    level      = EXCLUDED.level,
    corpid     = EXCLUDED.corpid,
    created    = EXCLUDED.created,
    modified   = EXCLUDED.modified,
    creator    = EXCLUDED.creator,
    modifier   = EXCLUDED.modifier,
    is_deleted = EXCLUDED.is_deleted
"""


def parse_mysql_dump(text: str) -> list[tuple]:
    """从 MySQL dump 里抽所有 VALUES 行，返回 list[list[str]]。"""
    rows: list[tuple] = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("INSERT INTO"):
            continue
        m = re.search(r"VALUES\s*\((.*)\);?$", s, re.DOTALL | re.IGNORECASE)
        if not m:
            continue
        raw = m.group(1)
        try:
            reader = csv.reader(io.StringIO(raw), quotechar="'", skipinitialspace=True)
            for row in reader:
                rows.append(_normalize(row))
        except Exception as e:
            log.warning("skip 1 bad row: %s (%s)", s[:80], e)
    return rows


def _normalize(row: list[str]) -> tuple:
    """把字符串转成 PG 友好的 Python 类型。"""
    out = []
    for i, val in enumerate(row):
        v = val.strip() if val else ""
        if v in ("NULL", "null", ""):
            out.append(None)
        elif COLS[i] in ("sort", "level", "is_deleted"):
            try:
                n = int(v)
                if COLS[i] == "is_deleted":
                    out.append(bool(n))
                else:
                    out.append(n)
            except ValueError:
                out.append(None)
        elif COLS[i] in ("created", "modified"):
            if v:
                try:
                    out.append(datetime.strptime(v.replace("T", " "), "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    out.append(None)
            else:
                out.append(None)
        else:
            out.append(v)
    return tuple(out)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", type=int, default=200)
    args = ap.parse_args()

    if not MYSQL_DUMP.exists():
        log.error("MySQL dump not found: %s", MYSQL_DUMP)
        sys.exit(1)

    text = MYSQL_DUMP.read_text(encoding="utf-8-sig")
    rows = parse_mysql_dump(text)
    log.info("MySQL dump parsed: %d rows", len(rows))
    if not rows:
        sys.exit(1)

    if args.dry_run:
        log.info("DRY RUN — first 3 rows:")
        for r in rows[:3]:
            log.info("  %s", r)
        return

    import os
    dsn = (
        f"postgresql://{os.getenv('SALES_PG_USER', 'sales')}:"
        f"{os.getenv('SALES_PG_PASSWORD', 'sales_dev_2026')}@"
        f"{os.getenv('SALES_PG_HOST', '127.0.0.1')}:"
        f"{os.getenv('SALES_PG_PORT', '5433')}/"
        f"{os.getenv('SALES_PG_DATABASE', 'sales_crm')}"
    )

    log.info("connecting to %s", dsn.rsplit("@", 1)[-1])
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)

    log.info("creating schema...")
    async with pool.acquire() as conn:
        await conn.execute(CREATE_SQL)

    log.info("upserting %d rows...", len(rows))
    done = 0
    batch = args.batch
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        async with pool.acquire() as conn:
            await conn.executemany(UPSERT_SQL, chunk)
        done += len(chunk)
        log.info("  %d / %d", done, len(rows))

    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM tag_qywx WHERE NOT is_deleted")
        log.info("DONE. active rows in tag_qywx: %s", total)
        sample = await conn.fetch("SELECT tag_id, tag_name, tag_pid FROM tag_qywx WHERE NOT is_deleted LIMIT 5")
        for s in sample:
            log.info("  sample: %s", dict(s))

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
