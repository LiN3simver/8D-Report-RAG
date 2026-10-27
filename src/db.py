"""PLM 來源庫（MS SQL）的唯讀連線，只執行 SELECT。

不落地：語料只有數百張單、約 20 萬字，全表 10 欄 LIKE 掃描約 12ms。
落地成向量庫換不到速度，卻會讓資料過期 —— 使用者問的是「這張單現在走到哪」。

兩種逾時都要設：
    connect(timeout=)   登入逾時
    conn.timeout        查詢逾時（pyodbc 掛在 Connection 上，預設 0 = 永不逾時）

pyodbc 延遲 import，未安裝 ODBC driver 時服務仍可啟動並回報 not_ready。
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from .guard import LOGIN_TIMEOUT, STATEMENT_TIMEOUT

load_dotenv()

# 資料混有簡體字，Windows 主控台（cp950）印不出來時以問號代替，不讓整支程式中斷。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")


def mssql(*, login_timeout: int = LOGIN_TIMEOUT):
    """建立唯讀連線。`readonly=True` 只是提示，真正的保護來自伺服器端權限。"""
    import pyodbc

    parts = [
        f"DRIVER={{{os.environ.get('MSSQL_DRIVER', 'ODBC Driver 18 for SQL Server')}}}",
        f"SERVER={os.environ['MSSQL_HOST']}",
        f"DATABASE={os.environ['MSSQL_DB']}",
        f"UID={os.environ['MSSQL_USER']}",
        f"PWD={os.environ['MSSQL_PASSWORD']}",
    ]
    if os.environ.get("MSSQL_TRUST_CERT", "yes").lower() in ("yes", "true", "1"):
        parts.append("TrustServerCertificate=yes")
    conn = pyodbc.connect(";".join(parts), readonly=True, timeout=login_timeout)
    conn.timeout = STATEMENT_TIMEOUT
    return conn


def cursor(conn, *, timeout: int = STATEMENT_TIMEOUT):
    """取 cursor 並設定查詢逾時。一律用這支，不要直接 conn.cursor()。"""
    conn.timeout = timeout
    return conn.cursor()


def source() -> str:
    """資料來源的一句話描述，跟著每一筆輸出走。"""
    return f"{os.environ.get('MSSQL_HOST')} / {os.environ.get('MSSQL_DB')}"


def is_test_db() -> bool:
    """來源是否為測試庫（.env 的 MSSQL_IS_TEST_DB）。會出現在 card 與 health，
    因為上游會把這個 agent 的答案當事實使用。"""
    return os.environ.get("MSSQL_IS_TEST_DB", "0").strip().lower() in ("1", "yes", "true")


def available() -> tuple[bool, str]:
    """來源庫是否可連線。給健康檢查用，永遠不丟例外。"""
    try:
        with mssql(login_timeout=5) as c:
            cur = cursor(c, timeout=5)
            cur.execute("SELECT COUNT(*) FROM plm.complaint")
            n = cur.fetchone()[0]
        return True, f"PLM {source()} 可連線，plm.complaint {n} 列"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
