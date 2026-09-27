import pandas as pd
import aiohttp

class TATools:
    @staticmethod
    async def fetch_ohlcv(pool_address: str, limit: int = 30) -> list:
        """
        Запрашивает исторические минутные свечи (OHLCV) через GeckoTerminal API.
        Это решает задачу получения глубоких исторических данных.
        """
        url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool_address}/ohlcv/minute?limit={limit}"
        async with aiohttp.ClientSession() as session:
            try:
                # API часто требует User-Agent
                headers = {'User-Agent': 'Mozilla/5.0'}
                async with session.get(url, headers=headers, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        # Формат: [timestamp, open, high, low, close, volume]
                        # GeckoTerminal возвращает новые свечи первыми
                        return data.get('data', {}).get('attributes', {}).get('ohlcv_list', [])
                    else:
                        if response.status not in (404, 429):
                            print(f"GeckoTerminal API Error: {response.status}")
                        return []
            except Exception as e:
                print(f"Error fetching OHLCV: {e}")
                return []

    @staticmethod
    def calculate_rsi(ohlcv_list: list, periods: int = 14) -> float:
        """
        Использует Pandas для локального расчета индикатора RSI (Индекс относительной силы).
        """
        if len(ohlcv_list) < periods + 1:
            return 50.0 # Недостаточно данных, возвращаем нейтральный RSI
            
        # Переворачиваем список, чтобы старые данные были в начале (для правильного расчета Pandas)
        ohlcv_list = ohlcv_list[::-1]
        
        # Загружаем данные в датафрейм Pandas
        df = pd.DataFrame(ohlcv_list, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['close'] = df['close'].astype(float)
        
        # Логика расчета RSI
        delta = df['close'].diff()
        
        # Получаем положительные и отрицательные движения
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/periods, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/periods, adjust=False).mean()
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        latest_rsi = float(rsi.iloc[-1])
        return latest_rsi

    @staticmethod
    def calculate_macd(ohlcv_list: list, fast=12, slow=26, signal=9) -> dict:
        """
        Расчет MACD (Moving Average Convergence Divergence).
        Показывает направление и силу тренда.
        """
        if len(ohlcv_list) < slow + signal:
            return {"macd": 0.0, "signal": 0.0, "hist": 0.0}
            
        ohlcv_list = ohlcv_list[::-1]
        df = pd.DataFrame(ohlcv_list, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['close'] = df['close'].astype(float)
        
        ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
        ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        macd_hist = macd_line - signal_line
        
        return {
            "macd": float(macd_line.iloc[-1]),
            "signal": float(signal_line.iloc[-1]),
            "hist": float(macd_hist.iloc[-1])
        }

    @staticmethod
    def calculate_bollinger_bands(ohlcv_list: list, periods=20, std_dev=2.0) -> dict:
        """
        Расчет Линий Боллинджера (Bollinger Bands).
        Помогает определить перекупленность/перепроданность и волатильность.
        """
        if len(ohlcv_list) < periods:
            return {"upper": 0.0, "middle": 0.0, "lower": 0.0}
            
        ohlcv_list = ohlcv_list[::-1]
        df = pd.DataFrame(ohlcv_list, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['close'] = df['close'].astype(float)
        
        middle_band = df['close'].rolling(window=periods).mean()
        std = df['close'].rolling(window=periods).std()
        upper_band = middle_band + (std * std_dev)
        lower_band = middle_band - (std * std_dev)
        
        return {
            "upper": float(upper_band.iloc[-1]),
            "middle": float(middle_band.iloc[-1]),
            "lower": float(lower_band.iloc[-1])
        }
