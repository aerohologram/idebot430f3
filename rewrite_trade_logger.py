import re

with open("trade_logger.py", "r") as f:
    content = f.read()

# Add aiohttp and timedelta imports
if "import aiohttp" not in content:
    content = content.replace("from datetime import datetime", "from datetime import datetime, timedelta\nimport aiohttp")

# Replace _init_db
old_db = """                    CREATE TABLE IF NOT EXISTS {table} (
                        mint TEXT PRIMARY KEY,
                        entry_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        features TEXT,
                        confidence REAL,
                        pnl REAL DEFAULT NULL,
                        exit_reason TEXT DEFAULT NULL,
                        status TEXT DEFAULT 'OPEN'
                    )"""

new_db = """                    CREATE TABLE IF NOT EXISTS {table} (
                        mint TEXT PRIMARY KEY,
                        entry_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        features TEXT,
                        confidence REAL,
                        pnl REAL DEFAULT NULL,
                        exit_reason TEXT DEFAULT NULL,
                        status TEXT DEFAULT 'OPEN',
                        exit_time TIMESTAMP DEFAULT NULL,
                        post_exit_ath REAL DEFAULT 0.0,
                        missed_pnl REAL DEFAULT 0.0,
                        check_1h_done INTEGER DEFAULT 0,
                        check_4h_done INTEGER DEFAULT 0,
                        check_24h_done INTEGER DEFAULT 0
                    )"""
content = content.replace(old_db, new_db)

# Replace log_exit data update
old_update = """                data = {
                    "pnl": pnl_pct,
                    "exit_reason": exit_reason,
                    "status": "CLOSED"
                }"""

new_update = """                data = {
                    "pnl": pnl_pct,
                    "exit_reason": exit_reason,
                    "status": "CLOSED",
                    "exit_time": datetime.utcnow().isoformat()
                }"""
content = content.replace(old_update, new_update)

old_sql_update = """"UPDATE {table_name} SET pnl = ?, exit_reason = ?, status = 'CLOSED' WHERE mint = ?","""
new_sql_update = """"UPDATE {table_name} SET pnl = ?, exit_reason = ?, status = 'CLOSED', exit_time = ? WHERE mint = ?","""
content = content.replace(old_sql_update, new_sql_update)

old_sql_args = """(pnl_pct, exit_reason, mint)"""
new_sql_args = """(pnl_pct, exit_reason, datetime.utcnow().isoformat(), mint)"""
content = content.replace(old_sql_args, new_sql_args)


with open("trade_logger.py", "w") as f:
    f.write(content)

