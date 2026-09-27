import re

with open('main.py', 'r') as f:
    content = f.read()

old_trailing = '''                    # Если еще не дошли до Тейк-Профита, защищаем прибыль!
                    # Активируем трейлинг, как только монета дала +15% профита.
                    if max_pnl_pct >= 0.15:
                        if drop_from_max >= 0.10: # Шаг трейлинга 10%
                            tracker.close_position(mint, current_price, f"Smart Trailing (+{max_pnl_pct*100:.0f}% peak)")
                            continue
                            
                # 3. ЖЕСТКИЙ Stop Loss -15%
                if pnl_pct <= -0.15:
                    tracker.close_position(mint, current_price, "Hard Stop Loss (-15%)")
                    continue
                    
                # 4. Time Exit: если за 10 минут профит меньше 5% — это мертвый груз
                if minutes_held >= 10 and pnl_pct < 0.05:
                    tracker.close_position(mint, current_price, "Time-based Exit (Dead Coin)")
                    continue'''

new_trailing = '''                    # Активируем трейлинг из config.py
                    if max_pnl_pct >= config.TRAILING_ACTIVATION_PCT:
                        if drop_from_max >= config.TRAILING_DISTANCE_PCT:
                            tracker.close_position(mint, current_price, f"Smart Trailing (+{max_pnl_pct*100:.0f}% peak)")
                            continue
                            
                # 3. ЖЕСТКИЙ Stop Loss из config.py
                if pnl_pct <= config.STOP_LOSS_PCT:
                    tracker.close_position(mint, current_price, f"Hard Stop Loss ({config.STOP_LOSS_PCT*100:.0f}%)")
                    continue
                    
                # 4. Time Exit из config.py: если монета застыла
                if minutes_held >= config.TIME_EXIT_MINUTES and pnl_pct < config.TIME_EXIT_PROFIT_REQ:
                    tracker.close_position(mint, current_price, "Time-based Exit (Dead Coin)")
                    continue'''

content = content.replace(old_trailing, new_trailing)

with open('main.py', 'w') as f:
    f.write(content)
