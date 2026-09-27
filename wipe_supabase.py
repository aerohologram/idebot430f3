import config
from supabase import create_client

supabase = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)

# Удаляем все записи, где статус не пустой (т.е. все сделки и PORTFOLIO_STATE)
try:
    res1 = supabase.table("trades_pump").delete().neq("mint", "0").execute()
    print("trades_pump wiped:", len(res1.data), "rows")
except Exception as e:
    print("trades_pump error:", e)

try:
    res2 = supabase.table("trades_raydium").delete().neq("mint", "0").execute()
    print("trades_raydium wiped:", len(res2.data), "rows")
except Exception as e:
    print("trades_raydium error:", e)

