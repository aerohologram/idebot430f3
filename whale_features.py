import pandas as pd
import numpy as np
from typing import Dict, List, Tuple
from pydantic import BaseModel

class WalletProfile(BaseModel):
    wallet_address: str
    tier: int # 1, 2, 3 (1 - высший)
    realized_pnl: float
    win_rate: float
    fomo_accuracy: float

def score_wallet_quality(wallet_history: pd.DataFrame) -> Dict[str, WalletProfile]:
    """
    Профилирование "Alpha"-кошельков (Smart Money Engine).
    Ожидаемый формат wallet_history: [wallet, token, entry_price, exit_price, pnl_usd, is_fomo_entry, is_win]
    """
    alpha_wallets = {}
    
    # Группировка по кошелькам
    for wallet, trades in wallet_history.groupby("wallet"):
        total_trades = len(trades)
        if total_trades < 20:
            continue
            
        realized_pnl = trades['pnl_usd'].sum()
        if realized_pnl <= 50000:
            continue
            
        win_rate = trades['is_win'].mean()
        if win_rate < 0.55:
            continue
            
        # Расчет fomo_accuracy: успешность покупок на растущих свечах (+30% за час)
        fomo_trades = trades[trades['is_fomo_entry'] == True]
        fomo_accuracy = fomo_trades['is_win'].mean() if not fomo_trades.empty else 0.0
        
        # Динамический Tier-скоринг
        # Tier 1: PnL > $250k, WinRate > 65%, FOMO acc > 60%
        # Tier 2: PnL > $100k, WinRate > 60%
        # Tier 3: Все остальные, прошедшие базовый фильтр
        tier = 3
        if realized_pnl > 250000 and win_rate >= 0.65 and fomo_accuracy >= 0.60:
            tier = 1
        elif realized_pnl > 100000 and win_rate >= 0.60:
            tier = 2
            
        alpha_wallets[wallet] = WalletProfile(
            wallet_address=wallet,
            tier=tier,
            realized_pnl=realized_pnl,
            win_rate=win_rate,
            fomo_accuracy=fomo_accuracy
        )
        
    return alpha_wallets

def extract_revival_features(
    token_trades: pd.DataFrame, 
    token_candles: pd.DataFrame, 
    alpha_wallets: Dict[str, WalletProfile]
) -> pd.DataFrame:
    """
    Создает набор квантовых признаков для зрелых токенов.
    Ожидается, что DataFrame отсортированы по времени.
    """
    # Собираем данные в почасовые свечи для анализа зрелых монет
    df = token_candles.copy()
    
    # --- 1. Smart Money Inflow Density ---
    # Объем покупок Alpha-кошельков за 1h
    alpha_addresses = set(alpha_wallets.keys())
    token_trades['is_alpha'] = token_trades['wallet'].isin(alpha_addresses)
    
    # Считаем почасовой объем
    hourly_alpha_vol = token_trades[token_trades['is_alpha']].groupby(pd.Grouper(key='timestamp', freq='1H'))['volume_usd'].sum()
    hourly_total_vol = token_trades.groupby(pd.Grouper(key='timestamp', freq='1H'))['volume_usd'].sum()
    
    df['alpha_inflow_1h'] = hourly_alpha_vol
    df['total_vol_1h'] = hourly_total_vol
    df['smart_money_density_1h'] = (df['alpha_inflow_1h'] / df['total_vol_1h']).fillna(0)
    
    # Скользящие окна для 4h и 24h
    df['smart_money_density_4h'] = df['alpha_inflow_1h'].rolling(4).sum() / df['total_vol_1h'].rolling(4).sum()
    
    # --- 2. Whale Co-occurrence (Кластеризация) ---
    # Количество уникальных Alpha-кошельков, купивших токен за последние 30 минут
    token_trades['alpha_tier'] = token_trades['wallet'].apply(lambda w: alpha_wallets[w].tier if w in alpha_wallets else 99)
    tier1_buyers = token_trades[token_trades['alpha_tier'] == 1].groupby(pd.Grouper(key='timestamp', freq='1H'))['wallet'].nunique()
    df['tier1_co_occurrence'] = tier1_buyers
    df['whale_fomo_flag'] = (df['tier1_co_occurrence'] >= 2).astype(int) # 2 и более не связанных Alpha-китов
    
    # --- 3. Liquidity/FDV Health ---
    # Отношение LP к Market Cap
    df['lp_mcap_ratio'] = (df['liquidity_usd'] / df['market_cap_usd']).fillna(0)
    df['lp_mcap_trend_4h'] = df['lp_mcap_ratio'].pct_change(4)
    
    # --- 4. Dormancy Breakout ---
    # Отношение текущего объема к среднему за 7 дней (Vol_1h / SMA_Vol_7d)
    df['sma_vol_7d'] = df['total_vol_1h'].rolling(window=168, min_periods=24).mean()
    df['dormancy_breakout_score'] = (df['total_vol_1h'] / df['sma_vol_7d']).fillna(0)
    
    # --- 5. Holder Churn Rate ---
    # Скорость смены держателей
    if 'unique_buyers_1h' in df.columns and 'total_holders' in df.columns:
        df['holder_churn_rate'] = (df['unique_buyers_1h'] / df['total_holders']).fillna(0)
    else:
        df['holder_churn_rate'] = 0.0
    
    df.fillna(0, inplace=True)
    return df
