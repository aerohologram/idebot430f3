import config
from supabase import create_client
import os

supabase = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)

# 1. Delete PORTFOLIO_STATE
supabase.table("trades_pump").delete().eq("mint", "PORTFOLIO_STATE").execute()
print("✅ Удален слепок портфеля (PORTFOLIO_STATE).")

# 2. Delete stuck OPEN trades
res = supabase.table("trades_pump").delete().eq("status", "OPEN").execute()
print(f"✅ Удалено зависших открытых сделок (OPEN): {len(res.data)}")

# 3. Delete local portfolio
if os.path.exists(config.PAPER_PORTFOLIO_FILE):
    os.remove(config.PAPER_PORTFOLIO_FILE)
    print("✅ Локальный файл portfolio.json удален.")

print("Бот готов к чистому запуску. Исторические CLOSED сделки сохранены.")
