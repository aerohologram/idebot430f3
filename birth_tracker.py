import time
import threading

class BirthTracker:
    def __init__(self):
        self.tokens = {}  # mint -> timestamp
        self.lock = threading.Lock()
    
    def add_token(self, mint: str):
        with self.lock:
            self.tokens[mint] = time.time()
    
    def get_mature_tokens(self, min_age_minutes: int, max_age_minutes: int):
        now = time.time()
        min_age_seconds = min_age_minutes * 60
        max_age_seconds = max_age_minutes * 60
        
        with self.lock:
            mature = []
            to_remove = []
            for mint, timestamp in self.tokens.items():
                age = now - timestamp
                if age >= min_age_seconds and age <= max_age_seconds:
                    mature.append(mint)
                elif age > max_age_seconds:
                    to_remove.append(mint)
            
            for mint in to_remove:
                del self.tokens[mint]
            
            return mature

birth_tracker = BirthTracker()