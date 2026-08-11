#!/usr/bin/env python3
"""
Простой тест для проверки BusyTracker.
"""

import time
import sys
from pathlib import Path

# Добавляем app в путь
sys.path.insert(0, str(Path(__file__).parent))

from app.busy_tracker import BusyTracker

def test_tracker():
    print("=== Тест BusyTracker ===\n")
    
    # Создаём тестовый трекер
    tracker = BusyTracker(Path("data/test_busy_channels.db"))
    
    print("1. Создаём тестовый звонок на расширение 101...")
    tracker.on_channel_new({
        "channel": "PJSIP/101@addpac-00000001",
        "exten": "101",
        "slot": "0/0",
        "state": "Ring",
        "caller": "101",
        "connected": "lobby",
    })
    
    print("2. Получаем активные звонки...")
    active = tracker.get_active_calls()
    print(f"   Активных звонков: {len(active)}")
    if active:
        print(f"   Первый звонок: {active[0]['exten']} - {active[0]['channel']}")
    
    print("\n3. Ждём 2 секунды...")
    time.sleep(2)
    
    print("4. Обновляем состояние канала...")
    tracker.on_channel_state_change({
        "channel": "PJSIP/101@addpac-00000001",
        "exten": "101",
        "state": "Up",
    })
    
    print("\n5. Завершаем звонок (отбой)...")
    tracker.on_channel_hangup({
        "channel": "PJSIP/101@addpac-00000001",
        "exten": "101",
    }, cause="Normal Clearing")
    
    print("\n6. Получаем историю звонков...")
    history = tracker.get_calls_history(limit=10)
    print(f"   Записей в истории: {len(history)}")
    if history:
        call = history[0]
        print(f"   Последний звонок:")
        print(f"     - Расширение: {call['exten']}")
        print(f"     - Канал: {call['channel']}")
        print(f"     - Длительность: {call['duration']} сек")
        print(f"     - Причина отбоя: {call['hangup_cause']}")
    
    print("\n7. Получаем статистику...")
    stats = tracker.get_statistics()
    print(f"   Всего звонков: {stats['total_calls']}")
    print(f"   Завершено: {stats['completed_calls']}")
    print(f"   Средняя длительность: {stats['avg_duration']} сек")
    print(f"   Общее время: {stats['total_duration']} сек")
    
    print("\n8. Создаём снимок состояния портов...")
    ports = [
        {"exten": "101", "slot": "0/0", "gateway_state": "Idle", "channel": "", "channel_state": "", "duration": 0},
        {"exten": "102", "slot": "0/1", "gateway_state": "On-Hook", "channel": "", "channel_state": "", "duration": 0},
    ]
    tracker.save_snapshot(ports)
    print("   Снимок сохранён")
    
    print("\n✅ Тест завершён успешно!")

if __name__ == "__main__":
    try:
        test_tracker()
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
