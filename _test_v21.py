"""Тесты v2.1: дни недели, предупреждение заранее, группировка."""
import sys, os, json, time, tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_winstub"))
sys.path.insert(0, os.path.dirname(__file__))

from adaptive_reminder import (Reminder, RepeatType, ReminderManager,
                               weekdays_label, human_duration)

fails = []
def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f" :: {extra}" if extra else ""))
    if not cond:
        fails.append(name)

tmp = tempfile.mkdtemp()
def fresh():
    A = os.path.join(tmp, f"a{time.time_ns()}.json")
    M = os.path.join(tmp, f"m{time.time_ns()}.json")
    for p in (A, M):
        with open(p, "w", encoding="utf-8") as f:
            json.dump([], f)
    return ReminderManager(A, M)

print("=" * 68)
print("1. ПОДПИСИ ДНЕЙ НЕДЕЛИ")
print("=" * 68)
cases = [([0, 2, 4], "Пн, Ср, Пт"), ([0, 1, 2, 3, 4], "по будням"),
         ([5, 6], "по выходным"), ([0, 1, 2, 3, 4, 5, 6], "каждый день"),
         ([3], "Чт"), ([], "дни не выбраны")]
for days, expect in cases:
    got = weekdays_label(days)
    check(f"{days} -> «{expect}»", got == expect, got)

print()
print("=" * 68)
print("2. ПН/СР/ПТ: СРАБАТЫВАЕТ ТОЛЬКО В СВОИ ДНИ")
print("=" * 68)
m = fresh()
# ставим на воскресенье — должно уехать на понедельник
sunday = datetime.now()
while sunday.weekday() != 6:
    sunday += timedelta(days=1)
sunday = sunday.replace(hour=9, minute=0, second=0, microsecond=0)
r = m.add("Репетиция", sunday, RepeatType.WEEKDAYS, weekdays=[0, 2, 4])
first = r.dt
check("первое срабатывание в разрешённый день", first.weekday() in (0, 2, 4),
      f"{first:%A %d.%m %H:%M}")
check("время суток сохранилось", first.hour == 9 and first.minute == 0,
      f"{first:%H:%M}")

# прокручиваем 6 срабатываний, все должны быть пн/ср/пт
seq = []
for _ in range(6):
    r.time = time.time() - 1
    m.check()
    r = [x for x in m.reminders if not x.is_lead][0]
    seq.append(r.dt)
days_hit = {d.weekday() for d in seq}
print("   ", " -> ".join(f"{d:%a %d.%m %H:%M}" for d in seq))
check("все срабатывания в пн/ср/пт", days_hit <= {0, 2, 4}, str(sorted(days_hit)))
check("время не уползло", all(d.hour == 9 and d.minute == 0 for d in seq))

print()
print("=" * 68)
print("3. ВЫХОДНЫЕ")
print("=" * 68)
m = fresh()
r = m.add("Отоспаться", datetime.now() + timedelta(minutes=5),
          RepeatType.WEEKDAYS, weekdays=[5, 6])
seq = []
for _ in range(4):
    r.time = time.time() - 1
    m.check()
    r = [x for x in m.reminders if not x.is_lead][0]
    seq.append(r.dt)
check("только сб/вс", {d.weekday() for d in seq} <= {5, 6},
      " ".join(f"{d:%a}" for d in seq))

print()
print("=" * 68)
print("4. ПРЕДУПРЕЖДЕНИЕ ЗА N МИНУТ")
print("=" * 68)
m = fresh()
target = datetime.now() + timedelta(hours=2)
main = m.add("Поезд", target, RepeatType.ONCE, lead_minutes=30)
leads = [x for x in m.reminders if x.is_lead]
check("предупреждение создано", len(leads) == 1, f"{len(leads)}")
if leads:
    gap = (main.time - leads[0].time) / 60
    check("ровно за 30 минут до", abs(gap - 30) < 0.1, f"{gap:.1f} мин")
    check("в тексте видно, о чём речь", "Поезд" in leads[0].message,
          leads[0].message)
    check("предупреждение помечено is_lead", leads[0].is_lead)

print()
print("=" * 68)
print("5. ПОЗДНО ПРЕДУПРЕЖДАТЬ — НЕ СОЗДАЁМ")
print("=" * 68)
m = fresh()
m.add("Скоро", datetime.now() + timedelta(minutes=5),
      RepeatType.ONCE, lead_minutes=60)
leads = [x for x in m.reminders if x.is_lead]
check("лишнего предупреждения нет", len(leads) == 0, f"{len(leads)}")

print()
print("=" * 68)
print("6. ПРЕДУПРЕЖДЕНИЕ ВОССТАНАВЛИВАЕТСЯ ДЛЯ ПОВТОРНЫХ")
print("=" * 68)
m = fresh()
soon = datetime.now() + timedelta(seconds=1)
r = m.add("Таблетки", soon, RepeatType.DAILY, lead_minutes=15)
before = len([x for x in m.reminders if x.is_lead])
time.sleep(1.3)
m.check()
after = [x for x in m.reminders if x.is_lead]
check("после срабатывания предупреждение снова есть", len(after) == 1,
      f"было {before}, стало {len(after)}")
if after:
    main_now = [x for x in m.reminders if not x.is_lead][0]
    gap = (main_now.time - after[0].time) / 60
    check("снова за 15 минут", abs(gap - 15) < 0.1, f"{gap:.1f} мин")

print()
print("=" * 68)
print("7. ПРАВКА: СТАРОЕ ПРЕДУПРЕЖДЕНИЕ НЕ ДУБЛИРУЕТСЯ")
print("=" * 68)
m = fresh()
r = m.add("Встреча", datetime.now() + timedelta(hours=3),
          RepeatType.ONCE, lead_minutes=20)
m.update(r, "Встреча перенесена", datetime.now() + timedelta(hours=5),
         RepeatType.ONCE, [], 20)
leads = [x for x in m.reminders if x.is_lead]
check("предупреждение ровно одно", len(leads) == 1, f"{len(leads)}")
check("текст обновился", leads and "перенесена" in leads[0].message,
      leads[0].message if leads else "")

print()
print("=" * 68)
print("8. ВЫКЛЮЧИЛИ ПРЕДУПРЕЖДЕНИЕ — ОНО ПРОПАЛО")
print("=" * 68)
m = fresh()
r = m.add("Дело", datetime.now() + timedelta(hours=3), RepeatType.ONCE, lead_minutes=20)
m.update(r, "Дело", datetime.now() + timedelta(hours=3), RepeatType.ONCE, [], 0)
check("предупреждений нет", len([x for x in m.reminders if x.is_lead]) == 0)

print()
print("=" * 68)
print("9. СОХРАНЕНИЕ И ЧТЕНИЕ НОВЫХ ПОЛЕЙ")
print("=" * 68)
m = fresh()
m.add("Спортзал", datetime.now() + timedelta(days=1),
      RepeatType.WEEKDAYS, weekdays=[1, 3], lead_minutes=45)
m2 = ReminderManager(m.data_path, m.missed_path)
main = [x for x in m2.reminders if not x.is_lead]
check("напоминание прочитано", len(main) == 1, f"{len(main)}")
if main:
    check("дни недели сохранились", main[0].weekdays == [1, 3], str(main[0].weekdays))
    check("тип WEEKDAYS сохранился", main[0].repeat_type == RepeatType.WEEKDAYS)
    check("lead_minutes сохранился", main[0].lead_minutes == 45,
          str(main[0].lead_minutes))

print()
print("=" * 68)
print("10. СТАРЫЙ ФАЙЛ БЕЗ НОВЫХ ПОЛЕЙ ЧИТАЕТСЯ")
print("=" * 68)
A = os.path.join(tmp, "old.json")
M = os.path.join(tmp, "old_m.json")
with open(A, "w", encoding="utf-8") as f:
    json.dump([{"message": "Из версии 2.0", "time": time.time() + 7200,
                "repeat_type": "DAILY", "original_time": time.time() + 7200}], f)
with open(M, "w", encoding="utf-8") as f:
    json.dump([], f)
m = ReminderManager(A, M)
check("прочиталось", len(m.reminders) == 1)
check("weekdays по умолчанию пуст", m.reminders[0].weekdays == [])
check("lead_minutes по умолчанию 0", m.reminders[0].lead_minutes == 0)

print()
print("=" * 68)
print("11. ПУСТЫЕ ДНИ НЕДЕЛИ НЕ ЛОМАЮТ")
print("=" * 68)
m = fresh()
r = Reminder(message="Кривое", time=time.time() - 5,
             repeat_type=RepeatType.WEEKDAYS, weekdays=[])
m.reminders.append(r)
try:
    m.check()
    check("не упало на пустом списке дней", True)
except Exception as exc:
    check("не упало на пустом списке дней", False, f"{type(exc).__name__}: {exc}")

print()
print("=" * 68)
print("ИТОГ")
print("=" * 68)
print("ВСЁ ПРОШЛО" if not fails else f"ПРОВАЛОВ {len(fails)}: {fails}")
sys.exit(0 if not fails else 1)
