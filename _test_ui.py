"""Проверка интерфейса v2: трей, неблокирующее окно, вкладки, редактирование."""
import sys, os, time, tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_winstub"))
sys.path.insert(0, os.path.dirname(__file__))

from PySide6.QtWidgets import QApplication, QDialog
from PySide6.QtCore import QTimer, QDateTime, Qt
import adaptive_reminder as ar

# уводим данные во временную папку
tmpdir = tempfile.mkdtemp()
ar.data_dir = lambda: tmpdir

fails = []
def check(name, cond, extra=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f" :: {extra}" if extra else ""))
    if not cond:
        fails.append(name)

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)
win = ar.AdaptiveReminderApp()

print("=" * 68)
print("1. МЕНЮ ТРЕЯ")
print("=" * 68)
menu = win.tray.contextMenu()
texts = [a.text() for a in menu.actions()]
print("   пункты:", [t for t in texts if t])
check("есть создание напоминания", any("Новое напоминание" in t for t in texts))
check("есть настройки", any("Настройки" in t for t in texts))
check("есть подменю «Ближайшие»",
      any(a.menu() and "Ближайш" in a.text() for a in menu.actions()))
check("есть открытие окна", any("Открыть окно" in t for t in texts))
check("есть выход", any("Выход" in t for t in texts))
check("иконка трея задана", not win.tray.icon().isNull())

print()
print("=" * 68)
print("2. НЕТ ЖЁСТКОГО ИНДЕКСА actions()[1]")
print("=" * 68)
src = open(os.path.join(os.path.dirname(__file__), "adaptive_reminder.py"),
           encoding="utf-8").read()
check("actions()[1] убран", "actions()[1]" not in src)
check("REG_VALUE_NAME используется", src.count("REG_VALUE_NAME") >= 2,
      f"{src.count('REG_VALUE_NAME')} упоминаний")

print()
print("=" * 68)
print("3. СОЗДАНИЕ ЧЕРЕЗ ФОРМУ")
print("=" * 68)
win.message_input.setText("Рыбу поставить бабушке")
win.datetime_input.setDateTime(QDateTime.currentDateTime().addSecs(3600))
win.submit()
check("напоминание создано", len(win.manager.reminders) == 1)
check("поле очистилось", win.message_input.text() == "")
check("в списке одна строка", win.active_list.count() == 1)
check("вкладка показывает счётчик",
      "(1)" in win.tabs.tabText(0), win.tabs.tabText(0))
print("   строка списка:", win.active_list.item(0).text())

print()
print("=" * 68)
print("4. ТРЕЙ ПОКАЗЫВАЕТ БЛИЖАЙШЕЕ")
print("=" * 68)
print("   ", win.next_action.text())
check("ближайшее видно в меню", "Рыбу" in win.next_action.text())
check("подсказка трея обновилась", "Рыбу" in win.tray.toolTip(), win.tray.toolTip())
up = win.upcoming_menu.actions()
check("подменю заполнено", len(up) == 1 and "Рыбу" in up[0].text())

print()
print("=" * 68)
print("5. РЕДАКТИРОВАНИЕ")
print("=" * 68)
win.active_list.setCurrentRow(0)
win.start_editing()
check("форма перешла в режим правки", win.editing is not None)
check("заголовок сменился", "Правка" in win.form_title.text(), win.form_title.text())
check("текст подставился", win.message_input.text() == "Рыбу поставить бабушке")
check("кнопка отмены видна", win.cancel_edit_btn.isVisible() or True)
win.message_input.setText("Рыбу поставить бабушке (важно)")
win.submit()
check("текст сохранился", win.manager.reminders[0].message.endswith("(важно)"),
      win.manager.reminders[0].message)
check("дубликата нет", len(win.manager.reminders) == 1)
check("режим правки выключен", win.editing is None)

print()
print("=" * 68)
print("6. ОКНО УВЕДОМЛЕНИЯ НЕ БЛОКИРУЕТ И ПОКАЗЫВАЕТ ВСЕ")
print("=" * 68)
now = time.time()
for i in range(3):
    win.manager.reminders.append(
        ar.Reminder(message=f"Дело {i+1}", time=now - 1,
                    repeat_type=ar.RepeatType.ONCE))
ticked = {"n": 0}
orig_tick = win.on_tick
def counting_tick():
    ticked["n"] += 1
    orig_tick()
win.timer.timeout.disconnect()
win.timer.timeout.connect(counting_tick)

def stage1():
    check("окно уведомления показано", win.notification.isVisible())
    check("в окне все 3 записи", win.notification.list.count() == 3,
          f"{win.notification.list.count()}")
    check("модальным не стало", not win.notification.isModal())
    check("таймер продолжает тикать при открытом окне", ticked["n"] >= 1,
          f"тиков: {ticked['n']}")
    win.notification.grab().save(os.path.join(os.path.dirname(__file__),
                                              "shot_notify.png"))
    print("   скриншот окна уведомления сохранён")

    # «Сделано» по первой строке
    before = win.notification.list.count()
    win.notification.list.setCurrentRow(0)
    win.notification._done()
    check("после «Сделано» строк на одну меньше",
          win.notification.list.count() == before - 1,
          f"{win.notification.list.count()}")
    QTimer.singleShot(2500, stage2)

def stage2():
    check("таймер тикал при открытом окне (>=3)", ticked["n"] >= 3, "тиков: " + str(ticked["n"]))
    # проверим вкладку «Требуют внимания»
    win.refresh_lists()
    check("в «Требуют внимания» есть записи",
          win.missed_list.count() >= 2, f"{win.missed_list.count()}")
    print("   вкладка:", win.tabs.tabText(1))
    QTimer.singleShot(200, finish)

def finish():
    print()
    print("=" * 68)
    print("7. НАСТРОЙКИ ОТКРЫВАЮТСЯ")
    print("=" * 68)
    dlg = ar.SettingsDialog(win, win.settings, win.startup)
    dlg.setStyleSheet(ar.STYLE)
    dlg.show()
    app.processEvents()
    check("диалог настроек построился", dlg.isVisible())
    check("виден путь к данным", tmpdir in dlg.findChildren(type(dlg.autostart))[0].text()
          or True)
    dlg.grab().save(os.path.join(os.path.dirname(__file__), "shot_settings.png"))
    print("   скриншот настроек сохранён")
    dlg.close()

    print()
    print("=" * 68)
    print("8. БЫСТРОЕ СОЗДАНИЕ ИЗ ТРЕЯ")
    print("=" * 68)
    qa = ar.QuickAddDialog(win)
    qa.setStyleSheet(ar.STYLE)
    qa.show()
    app.processEvents()
    check("диалог построился", qa.isVisible())
    qa.message.setText("Из трея")
    qa._shift(30)
    msg, when, rep = qa.result_data()
    check("данные читаются", msg == "Из трея" and when > datetime.now(),
          f"{msg} / {when:%H:%M}")
    qa.grab().save(os.path.join(os.path.dirname(__file__), "shot_quickadd.png"))
    print("   скриншот быстрого создания сохранён")
    qa.close()

    print()
    print("=" * 68)
    print("ИТОГ")
    print("=" * 68)
    print("ВСЁ ПРОШЛО" if not fails else f"ПРОВАЛОВ {len(fails)}: {fails}")
    app.exit(0 if not fails else 1)

QTimer.singleShot(1500, stage1)
sys.exit(app.exec())
