"""Проверка v2: всё, что чинили. Без UI, чистая логика."""
import sys, os, json, time, tempfile, shutil
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_winstub"))
sys.path.insert(0, os.path.dirname(__file__))

import adaptive_reminder as ar
from adaptive_reminder import (Reminder, RepeatType, ReminderManager,
                               _add_months, human_duration, plural, human_until)

fails = []
def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f" :: {extra}" if extra else ""))
    if not cond:
        fails.append(name)

tmp = tempfile.mkdtemp()
def fresh(active=None, missed=None):
    A = os.path.join(tmp, f"a{time.time_ns()}.json")
    M = os.path.join(tmp, f"m{time.time_ns()}.json")
    for p, d in ((A, active or []), (M, missed or [])):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f)
    return ReminderManager(A, M)

print("=" * 68)
print("1. СКЛОНЕНИЯ")
print("=" * 68)
cases = [(1, "1 минуту"), (2, "2 минуты"), (5, "5 минут"), (21, "21 минуту"),
         (60, "1 час"), (120, "2 часа"), (300, "5 часов"),
         (1320, "22 часа"), (1440, "1 день"), (2880, "2 дня"), (90, "1 час 30 минут")]
for minutes, expect in cases:
    got = human_duration(minutes)
    check(f"{minutes} мин -> «{expect}»", got == expect, got)

print()
print("=" * 68)
print("2. МЕСЯЧНЫЕ: 31 января не должно застрять на 28")
print("=" * 68)
d = datetime(2026, 1, 31, 12, 0)
anchor = d.day
seq = []
cur = d
for _ in range(5):
    cur = _add_months(cur, 1, anchor_day=anchor)
    seq.append(f"{cur:%d.%m}")
print("   ", " -> ".join(seq))
check("февраль = 28", seq[0] == "28.02", seq[0])
check("март ВЕРНУЛСЯ к 31", seq[1] == "31.03", seq[1])
check("апрель = 30", seq[2] == "30.04", seq[2])
check("май вернулся к 31", seq[3] == "31.05", seq[3])

print()
print("=" * 68)
print("3. ВСЕ СРАБОТАВШИЕ ВОЗВРАЩАЮТСЯ (было: только последнее)")
print("=" * 68)
now = time.time()
m = fresh(active=[{"message": f"Дело {i}", "time": now + 0.3,
                   "repeat_type": "ONCE", "original_time": now + 0.3}
                  for i in range(3)])
time.sleep(0.5)
trig = m.check()
check("сработало 3 из 3", len(trig) == 3, f"получено {len(trig)}")

print()
print("=" * 68)
print("4. ПОВТОРНОЕ ПОПАДАЕТ В «ТРЕБУЮТ ВНИМАНИЯ»")
print("=" * 68)
soon = time.time() + 0.3
m = fresh(active=[{"message": "Таблетки", "time": soon,
                   "repeat_type": "DAILY", "original_time": soon}])
time.sleep(0.5)
trig = m.check()
check("след срабатывания есть", len(m.missed_reminders) == 1,
      f"в списке {len(m.missed_reminders)}")
check("само напоминание осталось активным", len(m.reminders) == 1)
nxt = datetime.fromtimestamp(m.reminders[0].time)
check("сдвинулось на сутки", abs((nxt.timestamp() - soon) - 86400) < 2,
      f"{nxt:%d.%m %H:%M:%S}")

print()
print("=" * 68)
print("5. ЕЖЕДНЕВНОЕ НЕ ДРЕЙФУЕТ (считаем от original_time)")
print("=" * 68)
base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0) - timedelta(days=5)
m = fresh(active=[{"message": "Зарядка", "time": base.timestamp(),
                   "repeat_type": "DAILY", "original_time": base.timestamp()}])
r = m.reminders[0]
got = datetime.fromtimestamp(r.time)
check("время суток осталось 09:00", got.hour == 9 and got.minute == 0,
      f"{got:%d.%m %H:%M}")
# гоняем 5 срабатываний подряд
for _ in range(5):
    r.time = time.time() - 0.1
    m.check()
    r = m.reminders[0]
final = datetime.fromtimestamp(r.time)
check("после 5 срабатываний всё ещё 09:00", final.minute == 0,
      f"{final:%d.%m %H:%M}")

print()
print("=" * 68)
print("6. ОТЛОЖИТЬ НА 0 -> МИНИМУМ 1 МИНУТА")
print("=" * 68)
past = time.time() - 60
m = fresh(missed=[{"message": "Тест", "time": past, "repeat_type": "ONCE",
                   "original_time": past, "fired_at": past}])
r = m.missed_reminders[0]
snoozed = m.snooze(r, 0)
delta = snoozed.time - time.time()
check("отложено минимум на минуту", delta > 50, f"{delta:.0f} сек")

print()
print("=" * 68)
print("7. БИТЫЙ JSON НЕ ЗАТИРАЕТСЯ, А СОХРАНЯЕТСЯ В .bad")
print("=" * 68)
A = os.path.join(tmp, "broken.json")
M = os.path.join(tmp, "broken_m.json")
with open(A, "w", encoding="utf-8") as f:
    f.write('{"это": не json')
with open(M, "w", encoding="utf-8") as f:
    json.dump([], f)
m = ReminderManager(A, M)
check("программа не упала", True)
check("копия битого файла сохранена", os.path.exists(A + ".bad"))

print()
print("=" * 68)
print("8. ЗАПИСЬ БЕЗ ПОЛЯ message НЕ РОНЯЕТ")
print("=" * 68)
m = fresh(active=[{"time": time.time() + 999, "repeat_type": "ONCE"},
                  {"message": "Нормальная", "time": time.time() + 500,
                   "repeat_type": "ONCE"}])
check("обе записи прочитаны", len(m.reminders) == 2, f"{len(m.reminders)}")
check("подставлена заглушка текста",
      any("без текста" in r.message for r in m.reminders))

print()
print("=" * 68)
print("9. ПУТЬ ДАННЫХ НЕ ЗАВИСИТ ОТ РАБОЧЕЙ ПАПКИ")
print("=" * 68)
before = ar.data_dir()
os.chdir(tempfile.mkdtemp())
after = ar.data_dir()
check("папка данных та же", before == after, f"{before} vs {after}")
print(f"    -> {after}")

print()
print("=" * 68)
print("10. АТОМАРНАЯ ЗАПИСЬ: нет обрезанных файлов")
print("=" * 68)
m = fresh()
for i in range(20):
    m.add(f"Задача {i}", datetime.now() + timedelta(hours=i + 1), RepeatType.ONCE)
with open(m.data_path, encoding="utf-8") as f:
    data = json.load(f)
check("файл валиден после 20 записей", len(data) == 20, f"{len(data)}")
check("временных .tmp не осталось",
      not os.path.exists(m.data_path + ".tmp"))

print()
print("=" * 68)
print("11. РЕДАКТИРОВАНИЕ")
print("=" * 68)
m = fresh()
r = m.add("Старый текст", datetime.now() + timedelta(hours=2), RepeatType.ONCE)
new_time = datetime.now() + timedelta(hours=5)
m.update(r, "Новый текст", new_time, RepeatType.DAILY)
check("текст обновился", m.reminders[0].message == "Новый текст")
check("тип повтора обновился", m.reminders[0].repeat_type == RepeatType.DAILY)
check("время обновилось", abs(m.reminders[0].time - new_time.timestamp()) < 1)
check("дубликат не создан", len(m.reminders) == 1)

print()
print("=" * 68)
print("12. СТАРЫЙ ФОРМАТ JSON (совместимость)")
print("=" * 68)
future = time.time() + 3600
m = fresh(active=[{"message": "Из версии 1", "time": future,
                   "repeat": "🌅 Каждый день"}])
check("старая запись прочитана", len(m.reminders) == 1)
check("тип распознан как DAILY",
      m.reminders[0].repeat_type == RepeatType.DAILY,
      str(m.reminders[0].repeat_type))

print()
print("=" * 68)
print("ИТОГ")
print("=" * 68)
print("ВСЁ ПРОШЛО" if not fails else f"ПРОВАЛОВ {len(fails)}: {fails}")
sys.exit(0 if not fails else 1)
