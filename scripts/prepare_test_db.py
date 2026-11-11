"""Create mai_test on the isolated CI service only, waiting for SQL Server startup."""
import time
import os
import pymssql

for attempt in range(60):
    try:
        with pymssql.connect(server=os.environ.get("MAI_CI_SQL_HOST", "localhost"), user="sa", password="CI-only-CorV2-2026!",
                             database="master", login_timeout=2, autocommit=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute("IF DB_ID('mai_test') IS NULL CREATE DATABASE mai_test")
        break
    except pymssql.Error:
        if attempt == 59:
            raise
        time.sleep(2)
