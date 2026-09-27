import json
import os
import time
from typing import Dict
from pydantic import BaseModel
import config

class VirtualPosition(BaseModel):
    symbol: str
    mint: str
    entry_price_usd: float
    amount_usd: float
    entry_time: float
    status: str = "open"  # "open" or "closed"
    exit_price_usd: float = 0.0
    pnl_usd: float = 0.0
    max_price_usd: float = 0.0  # Отслеживаем максимальную цену для трейлинга
    peak_time: float = 0.0      # Время, когда была достигнута максимальная цена (ракета)
    current_price_usd: float = 0.0 # Для отображения в интерфейсе
    current_pnl_usd: float = 0.0 # Для отображения в интерфейсе
    exit_reason: str = "" # Причина выхода
    ml_features: dict = {} # Фичи, по которым ИИ принял решение
    ml_confidence: float = 0.0 # Уверенность ИИ (0-100)
    is_mature: bool = False # Флаг для разделения логики (Swing vs Scalp)
    is_moonbag: bool = False # Флаг, что мы уже зафиксировали 50% прибыли
    source: str = "" # Кто открыл: SCANNER VIP RAY-XGB 98%, FOMO, COPY_xxx, ROBINHOOD rule 72%...
    chain: str = "solana" # solana | robinhood
    price_updated_at: float = 0.0 # Время свежего обновления цены из WSS
    price_checked_at: float = 0.0 # Для Crash Guard
    exit_time: float = 0.0 # Для дневного kill-switch

class PaperTracker:
    def __init__(self):
        self.filename = config.PAPER_PORTFOLIO_FILE
        self.positions: Dict[str, VirtualPosition] = {}
        self._sb = None  # ленивый Supabase-клиент
        self.load_portfolio()

    def _supabase(self):
        """Возвращает Supabase-клиент или None, если нет настроек."""
        if self._sb is not None:
            return self._sb
        url = getattr(config, 'SUPABASE_URL', None)
        key = getattr(config, 'SUPABASE_KEY', None)
        if not url or not key:
            return None
        try:
            from supabase import create_client
            self._sb = create_client(url, key)
            return self._sb
        except Exception as e:
            print(f"⚠️ Не удалось создать Supabase-клиент: {e}")
            return None

    def _parse_portfolio_data(self, data):
        for k, v in data.items():
            if isinstance(v, dict):
                if "max_price_usd" not in v:
                    v["max_price_usd"] = v.get("entry_price_usd", 0)
                if "is_mature" not in v:
                    v["is_mature"] = False
                if "is_moonbag" not in v:
                    v["is_moonbag"] = False
                self.positions[k] = VirtualPosition(**v)

    def load_portfolio(self):
        # 1. Пытаемся загрузить из Supabase (чтобы не терять данные при перезагрузке Render)
        sb = self._supabase()
        if sb is not None:
            try:
                res = sb.table("trades_pump").select("features").eq("mint", "PORTFOLIO_STATE_V3").execute()
                if res.data and res.data[0].get("features"):
                    data = json.loads(res.data[0]["features"])
                    if data:
                        print("✅ Портфель успешно загружен из Supabase!")
                        self._parse_portfolio_data(data)
                        # Синхронизируем с локальным файлом для дашборда
                        try:
                            with open(self.filename, 'w') as f:
                                json.dump(data, f, indent=4)
                        except Exception:
                            pass
                        return
                    print("ℹ️ В Supabase пустой слепок портфеля, пробуем локальный файл...")
            except Exception as e:
                print(f"⚠️ Не удалось загрузить портфель из Supabase: {e}. Пробуем локальный файл...")
            
        # 2. Fallback: загружаем из локального файла
        try:
            with open(self.filename, 'r') as f:
                data = json.load(f)
                if data:
                    print("✅ Портфель загружен из локального файла!")
                    self._parse_portfolio_data(data)
                    return
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        except Exception as e:
            print(f"⚠️ Не удалось прочитать локальный портфель: {e}")
            
        # 3. Данных нигде нет — стартуем пустыми, но НИЧЕГО НЕ ПИШЕМ,
        # чтобы случайно не затереть облачный слепок пустым словарём.
        print("🧹 Локальных и облачных данных нет — начинаем с чистого листа (в памяти, без записи).")

    def save_portfolio(self):
        data = {k: getattr(v, "model_dump", v.dict)() for k, v in self.positions.items()}
        
        # 1. Сохраняем локально (для Streamlit)
        try:
            with open(self.filename, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception:
            pass
            
        # 2. Сохраняем в Supabase (Render-proof).
        # ЗАЩИТА ОТ WIPE: пустой словарь в облако никогда не пишем —
        # пустая память + живой слепок в облаке = не трогаем облако.
        if not data:
            return
        sb = self._supabase()
        if sb is None:
            return
        try:
            sb.table("trades_pump").upsert({
                "mint": "PORTFOLIO_STATE_V3",
                "features": json.dumps(data),
                "confidence": 0,
                "status": "SYSTEM"
            }).execute()
        except Exception as e:
            print(f"⚠️ Ошибка сохранения портфеля в Supabase: {e}")

    def get_open_positions(self) -> Dict[str, VirtualPosition]:
        return {k: v for k, v in self.positions.items() if v.status == "open"}

    def get_total_capital(self) -> float:
        # Считаем изначальный капитал + сумма PnL всех закрытых позиций
        total_pnl = sum(pos.pnl_usd for pos in self.positions.values() if pos.status == "closed")
        real_capital = config.INITIAL_BALANCE_USD + total_pnl
        
        if real_capital < 5.0:
            print(f"🛑 [KILL SWITCH] Капитал критически низкий: ${real_capital:.2f}. Торговля невозможна!")
            return 0.0  # Вернуть 0 → position_size будет 0 → сделка не откроется
        
        return real_capital

    def add_position(self, symbol, mint, entry_price, amount_usd=5.0, ml_features=None, ml_confidence=0.0, is_mature=False, source="", chain="solana"):
        # БЛОКИРОВКА ПОВТОРНОГО ВХОДА С УМНЫМ КУЛДАУНОМ
        if mint in self.positions:
            pos = self.positions[mint]
            if pos.status == "open":
                print(f"⚠️ Позиция {symbol} уже открыта. Отмена повторного входа.")
                return
                
            # Если позиция закрыта, проверяем кулдаун (4 часа)
            time_since_entry = time.time() - pos.entry_time
            if time_since_entry < (4 * 3600):
                print(f"⏳ Кулдаун: {symbol} уже торговался недавно. Ждем еще {(4*3600 - time_since_entry)/3600:.1f}ч перед входом.")
                return
                
            # Если кулдаун прошел, архивируем старую сделку, чтобы не потерять ее из истории PnL
            archive_key = f"{mint}_old_{int(time.time())}"
            self.positions[archive_key] = pos
            print(f"🔄 Кулдаун прошел! Разрешен повторный вход в {symbol} (CTO/Вторая волна).")

        print(f"✅ Открыта PAPER сделка: {symbol} по цене ${entry_price} [{chain}|{source}]")
        
        ml_features_dict = ml_features if ml_features is not None else {}
        
        self.positions[mint] = VirtualPosition(
            symbol=symbol,
            mint=mint,
            entry_price_usd=entry_price,
            amount_usd=amount_usd,
            entry_time=time.time(),
            max_price_usd=entry_price,
            current_price_usd=entry_price,
            ml_features=ml_features_dict,
            ml_confidence=ml_confidence,
            is_mature=is_mature,
            source=source,
            chain=chain
        )
        self.save_portfolio()
        print(f"📝 PAPER BUY: {symbol} ({mint}) | Amount: ${amount_usd} | Price: ${entry_price} | Src: {source} | Chain: {chain}")
        
        # === СОХРАНЕНИЕ В SUPABASE (ENTRY) ===
        # Сохраняем опыт в базу
        try:
            from trade_logger import TradeLogger
            import asyncio
            logger = TradeLogger()
            asyncio.create_task(logger.log_entry(mint, ml_features_dict, ml_confidence, is_mature))
        except Exception as e:
            print(f"⚠️ Ошибка логирования входа: {e}")

    def partial_close_position(self, mint: str, exit_price: float, sell_pct: float, reason: str):
        """Частичная фиксация позиции (Moonbags)"""
        pos = self.positions.get(mint)
        if pos and pos.status == "open":
            amount_sold_usd = pos.amount_usd * sell_pct
            real_entry_price = pos.entry_price_usd * 1.01
            real_exit_price = exit_price * 0.99
            
            price_diff_pct = (real_exit_price - real_entry_price) / real_entry_price if real_entry_price > 0 else 0
            priority_fee_usd = 0.075 if pos.amount_usd < 10.0 else 0.45
            priority_fee_usd = min(priority_fee_usd, amount_sold_usd * 0.05)
            
            # PnL от проданной части
            realized_pnl_usd = (amount_sold_usd * price_diff_pct) - priority_fee_usd
            
            print(f"🚀 [Moonbag] Частичная фиксация {sell_pct*100}% {pos.symbol}: Профит +${realized_pnl_usd:.2f} ({reason})")
            
            # Сохраняем этот профит в общую копилку монеты!
            pos.pnl_usd += realized_pnl_usd
            
            # Уменьшаем позицию на проданный процент
            pos.amount_usd -= amount_sold_usd
            pos.is_moonbag = True
            self.save_portfolio()

    def close_position(self, mint: str, exit_price: float, reason: str):
        pos = self.positions.get(mint)
        if pos and pos.status == "open":
            pos.status = "closed"
            pos.exit_price_usd = exit_price
            pos.exit_reason = reason
            pos.exit_time = time.time()
            
            # РЕАЛЬНЫЙ РАСЧЕТ PnL С УЧЕТОМ КОМИССИЙ (1% вход, 1% выход + 0.003 SOL сеть)
            real_entry_price = pos.entry_price_usd * 1.01
            real_exit_price = exit_price * 0.99
            
            # Считаем изменение цены актива (процент)
            price_diff_pct = (real_exit_price - real_entry_price) / real_entry_price if real_entry_price > 0 else 0
            
            # 2. ДИНАМИЧЕСКИЕ МИКРО-КОМИССИИ JITO (Micro-Tips)
            # Аварийные выходы дороже: широкая проверка по смыслу, а не двум строкам
            # (иначе Emergency Cap / ROB/BSC/GROWTH-стопы считались по дешёвому тарифу)
            _r = reason.upper()
            if "CRASH" in _r or "STOP" in _r or "CAP" in _r or "GUARD" in _r:
                priority_fee_usd = 0.75  # 0.005 SOL
            else:
                priority_fee_usd = 0.075 if pos.amount_usd < 10.0 else 0.45
                
            # Защита математики дашборда: комиссия не может превышать 5% от микро-позиции, 
            # иначе тестовые входы на $4 будут показывать -50% убытка только из-за комиссии.
            priority_fee_usd = min(priority_fee_usd, pos.amount_usd * 0.05)
            
            # Добавляем профит от закрытия финального остатка к тому, что уже зафиксировано
            final_pnl = (pos.amount_usd * price_diff_pct) - priority_fee_usd
            pos.pnl_usd += final_pnl
            
            # Реальный итоговый процент инвестиции.
            # Moonbag продаёт 50%: остаток = 0.5 × изначальный → изначальный = остаток / 0.5.
            # (Было /0.4 от старых 60% - занижало процент на 20%.)
            original_amount = (pos.amount_usd / 0.5) if pos.is_moonbag else pos.amount_usd
            pnl_pct = pos.pnl_usd / original_amount if original_amount > 0 else 0
            
            self.save_portfolio()
            print(f"🔒 PAPER SELL: {pos.symbol} ({mint}) | Reason: {reason} | PnL: {pnl_pct*100:.2f}% (${pos.pnl_usd:.2f})")
            
            # === СОХРАНЕНИЕ ОПЫТА ДЛЯ ИИ (Continuous Learning) ===
            try:
                from trade_logger import TradeLogger
                import asyncio
                logger = TradeLogger()
                asyncio.create_task(logger.log_exit(pos.mint, pnl_pct * 100, pos.exit_reason, pos.is_mature))
            except Exception as e:
                print(f"⚠️ Ошибка сохранения опыта: {e}")

    def is_trading_allowed(self) -> bool:
        """Проверка глобального Kill-Switch"""
        if not getattr(config, "KILL_SWITCH_ENABLED", True):
            return True
        if not hasattr(config, "MAX_DAILY_LOSS_USD"):
            return True
            
        import datetime
        today = datetime.datetime.utcnow().date()
        daily_pnl = 0.0
        
        for pos in self.positions.values():
            if pos.status == "closed":
                pos_date = datetime.datetime.fromtimestamp(pos.entry_time).date()
                if pos_date == today:
                    daily_pnl += pos.pnl_usd
                    
        if daily_pnl <= -config.MAX_DAILY_LOSS_USD:
            print(f"🛑 [KILL SWITCH] Превышен дневной лимит потерь: ${daily_pnl:.2f}. Торговля остановлена!")
            return False
        return True

    def can_open_new_position(self, max_concurrent: int) -> bool:
        if not self.is_trading_allowed():
            return False
        return len(self.get_open_positions()) < max_concurrent
