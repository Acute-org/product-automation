import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal

JobStatus = Literal["queued", "running", "succeeded", "failed"]


@dataclass(frozen=True)
class JobRow:
    job_id: str
    status: JobStatus
    created_at: float
    updated_at: float
    request_json: dict[str, Any]
    error: str | None


@dataclass(frozen=True)
class JobResultRow:
    job_id: str
    count: int
    products_json: list[dict[str, Any]]


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
          job_id TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          request_json TEXT NOT NULL,
          error TEXT
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
          sno INTEGER PRIMARY KEY,
          name TEXT,
          market_name TEXT,
          url TEXT,
          first_seen_job_id TEXT,
          first_seen_at REAL,
          last_seen_job_id TEXT,
          last_seen_at REAL,
          data_json TEXT NOT NULL,
          FOREIGN KEY(first_seen_job_id) REFERENCES jobs(job_id),
          FOREIGN KEY(last_seen_job_id) REFERENCES jobs(job_id)
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_products (
          job_id TEXT NOT NULL,
          sno INTEGER NOT NULL,
          PRIMARY KEY(job_id, sno),
          FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE,
          FOREIGN KEY(sno) REFERENCES products(sno) ON DELETE CASCADE
        );
        """
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_last_seen_at ON products(last_seen_at);"
    )
    conn.commit()


def now_ts() -> float:
    return time.time()


def create_job(
    conn: sqlite3.Connection, *, job_id: str, request: dict[str, Any]
) -> None:
    ts = now_ts()
    conn.execute(
        """
        INSERT INTO jobs(job_id, status, created_at, updated_at, request_json, error)
        VALUES(?, 'queued', ?, ?, ?, NULL);
        """,
        (job_id, ts, ts, json.dumps(request, ensure_ascii=False)),
    )
    conn.commit()


def update_job_status(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    status: JobStatus,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE jobs SET status=?, updated_at=?, error=? WHERE job_id=?;",
        (status, now_ts(), error, job_id),
    )
    conn.commit()


def get_job(conn: sqlite3.Connection, *, job_id: str) -> JobRow | None:
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?;", (job_id,)).fetchone()
    if not row:
        return None
    return JobRow(
        job_id=str(row["job_id"]),
        status=str(row["status"]),  # type: ignore[return-value]
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        request_json=json.loads(row["request_json"]),
        error=row["error"],
    )


def list_jobs(
    conn: sqlite3.Connection, *, limit: int = 50, offset: int = 0
) -> list[JobRow]:
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?;",
        (limit, offset),
    ).fetchall()
    out: list[JobRow] = []
    for r in rows:
        out.append(
            JobRow(
                job_id=str(r["job_id"]),
                status=str(r["status"]),  # type: ignore[return-value]
                created_at=float(r["created_at"]),
                updated_at=float(r["updated_at"]),
                request_json=json.loads(r["request_json"]),
                error=r["error"],
            )
        )
    return out


def get_known_snos(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT sno FROM products;").fetchall()
    return {int(r["sno"]) for r in rows}


def upsert_products_for_job(
    conn: sqlite3.Connection, *, job_id: str, products: list[dict[str, Any]]
) -> None:
    ts = now_ts()
    for p in products:
        sno = p.get("sno")
        if not isinstance(sno, int):
            continue
        name = p.get("name")
        market_name = p.get("market_name")
        url = p.get("url")
        data_json = json.dumps(p, ensure_ascii=False)

        existing = conn.execute(
            "SELECT sno FROM products WHERE sno=?;", (sno,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE products
                SET name=COALESCE(?, name),
                    market_name=COALESCE(?, market_name),
                    url=COALESCE(?, url),
                    last_seen_job_id=?,
                    last_seen_at=?,
                    data_json=?
                WHERE sno=?;
                """,
                (name, market_name, url, job_id, ts, data_json, sno),
            )
        else:
            conn.execute(
                """
                INSERT INTO products(
                  sno, name, market_name, url,
                  first_seen_job_id, first_seen_at,
                  last_seen_job_id, last_seen_at,
                  data_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (sno, name, market_name, url, job_id, ts, job_id, ts, data_json),
            )

        conn.execute(
            "INSERT OR IGNORE INTO job_products(job_id, sno) VALUES(?, ?);",
            (job_id, sno),
        )
    conn.commit()


def get_job_products(conn: sqlite3.Connection, *, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT p.data_json
        FROM job_products jp
        JOIN products p ON p.sno = jp.sno
        WHERE jp.job_id = ?
        ORDER BY p.sno ASC;
        """,
        (job_id,),
    ).fetchall()
    return [json.loads(r["data_json"]) for r in rows]


def list_products(
    conn: sqlite3.Connection, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT data_json
        FROM products
        ORDER BY last_seen_at DESC
        LIMIT ? OFFSET ?;
        """,
        (limit, offset),
    ).fetchall()
    return [json.loads(r["data_json"]) for r in rows]


def get_products_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS cnt FROM products;").fetchone()
    return int(row["cnt"] if row else 0)
