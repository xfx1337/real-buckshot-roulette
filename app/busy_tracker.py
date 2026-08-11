"""
Отслеживание и хранение истории занятости каналов VoIP.

Сохраняет в SQLite:
- Активные звонки (начало, конец, длительность)
- Снимки состояния портов каждые N секунд
- Статистику по расширениям
"""

import sqlite3
import time
import threading
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

DB_PATH = Path("data/busy_channels.db")


class BusyTracker:
    """Трекер занятости каналов с сохранением в SQLite."""
    
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._active_calls = {}  # channel_name -> call_info
        self._lock = threading.Lock()
    
    def _init_db(self):
        """Создать таблицы, если их нет."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exten TEXT NOT NULL,
                    slot TEXT,
                    channel TEXT NOT NULL,
                    channel_state TEXT,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    duration INTEGER,
                    caller TEXT,
                    connected TEXT,
                    hangup_cause TEXT
                );
                
                CREATE INDEX IF NOT EXISTS idx_calls_exten ON calls(exten);
                CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at);
                CREATE INDEX IF NOT EXISTS idx_calls_ended ON calls(ended_at);
                
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    exten TEXT NOT NULL,
                    slot TEXT,
                    gateway_state TEXT,
                    channel TEXT,
                    channel_state TEXT,
                    duration INTEGER
                );
                
                CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON snapshots(timestamp);
                CREATE INDEX IF NOT EXISTS idx_snapshots_exten ON snapshots(exten);
            """)
            conn.commit()
        finally:
            conn.close()
    
    def on_channel_new(self, channel: dict):
        """Обработать событие нового канала."""
        with self._lock:
            channel_name = channel.get("channel", "")
            if not channel_name or channel_name in self._active_calls:
                return
            
            exten = channel.get("exten") or channel.get("caller") or channel.get("connected", "")
            
            call_info = {
                "channel": channel_name,
                "exten": exten,
                "slot": channel.get("slot", ""),
                "channel_state": channel.get("state", ""),
                "started_at": time.time(),
                "caller": channel.get("caller", ""),
                "connected": channel.get("connected", ""),
            }
            
            self._active_calls[channel_name] = call_info
            
            # Сохранить начало звонка в БД
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("""
                    INSERT INTO calls (exten, slot, channel, channel_state, started_at, caller, connected)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    call_info["exten"],
                    call_info["slot"],
                    call_info["channel"],
                    call_info["channel_state"],
                    call_info["started_at"],
                    call_info["caller"],
                    call_info["connected"],
                ))
                conn.commit()
                call_info["db_id"] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            except Exception as e:
                logger.error(f"Failed to save call start: {e}")
            finally:
                conn.close()
    
    def on_channel_hangup(self, channel: dict, cause: str = ""):
        """Обработать событие отбоя."""
        with self._lock:
            channel_name = channel.get("channel", "")
            call_info = self._active_calls.pop(channel_name, None)
            
            if not call_info:
                return
            
            ended_at = time.time()
            duration = int(ended_at - call_info["started_at"])
            
            # Обновить запись в БД
            conn = sqlite3.connect(self.db_path)
            try:
                db_id = call_info.get("db_id")
                if db_id:
                    conn.execute("""
                        UPDATE calls 
                        SET ended_at = ?, duration = ?, hangup_cause = ?
                        WHERE id = ?
                    """, (ended_at, duration, cause, db_id))
                    conn.commit()
            except Exception as e:
                logger.error(f"Failed to update call end: {e}")
            finally:
                conn.close()
    
    def on_channel_state_change(self, channel: dict):
        """Обновить состояние канала."""
        with self._lock:
            channel_name = channel.get("channel", "")
            call_info = self._active_calls.get(channel_name)
            
            if call_info:
                call_info["channel_state"] = channel.get("state", "")
    
    def save_snapshot(self, ports: list[dict]):
        """Сохранить снимок состояния всех портов."""
        timestamp = time.time()
        conn = sqlite3.connect(self.db_path)
        try:
            for port in ports:
                conn.execute("""
                    INSERT INTO snapshots (timestamp, exten, slot, gateway_state, channel, channel_state, duration)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    timestamp,
                    port.get("exten", ""),
                    port.get("slot", ""),
                    port.get("gateway_state", ""),
                    port.get("channel", ""),
                    port.get("channel_state", ""),
                    port.get("duration", 0),
                ))
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to save snapshot: {e}")
        finally:
            conn.close()
    
    def get_calls_history(
        self, 
        exten: Optional[str] = None, 
        since: Optional[float] = None,
        until: Optional[float] = None,
        limit: int = 100
    ) -> list[dict]:
        """Получить историю звонков."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        
        query = "SELECT * FROM calls WHERE 1=1"
        params = []
        
        if exten:
            query += " AND exten = ?"
            params.append(exten)
        
        if since:
            query += " AND started_at >= ?"
            params.append(since)
        
        if until:
            query += " AND started_at <= ?"
            params.append(until)
        
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        
        try:
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    
    def get_statistics(self, since: Optional[float] = None) -> dict:
        """Получить статистику по звонкам."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        
        where_clause = ""
        params = []
        if since:
            where_clause = "WHERE started_at >= ?"
            params.append(since)
        
        try:
            # Общая статистика
            stats = conn.execute(f"""
                SELECT 
                    COUNT(*) as total_calls,
                    COUNT(ended_at) as completed_calls,
                    AVG(CASE WHEN duration > 0 THEN duration END) as avg_duration,
                    SUM(duration) as total_duration
                FROM calls {where_clause}
            """, params).fetchone()
            
            # По расширениям
            by_exten = conn.execute(f"""
                SELECT 
                    exten,
                    COUNT(*) as calls_count,
                    AVG(CASE WHEN duration > 0 THEN duration END) as avg_duration,
                    SUM(duration) as total_duration,
                    MAX(started_at) as last_call_at
                FROM calls {where_clause}
                GROUP BY exten
                ORDER BY calls_count DESC
            """, params).fetchall()
            
            return {
                "total_calls": stats["total_calls"],
                "completed_calls": stats["completed_calls"],
                "avg_duration": round(stats["avg_duration"] or 0, 1),
                "total_duration": stats["total_duration"] or 0,
                "by_extension": [dict(row) for row in by_exten],
            }
        finally:
            conn.close()
    
    def get_active_calls(self) -> list[dict]:
        """Получить текущие активные звонки."""
        with self._lock:
            return [
                {
                    **info,
                    "current_duration": int(time.time() - info["started_at"])
                }
                for info in self._active_calls.values()
            ]
    
    def cleanup_old_data(self, days: int = 30):
        """Удалить данные старше N дней."""
        cutoff = time.time() - (days * 86400)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM calls WHERE started_at < ?", (cutoff,))
            conn.execute("DELETE FROM snapshots WHERE timestamp < ?", (cutoff,))
            conn.commit()
            logger.info(f"Cleaned up data older than {days} days")
        finally:
            conn.close()


# Глобальный экземпляр трекера
_tracker: Optional[BusyTracker] = None


def get_tracker() -> BusyTracker:
    """Получить глобальный трекер."""
    global _tracker
    if _tracker is None:
        _tracker = BusyTracker()
    return _tracker
