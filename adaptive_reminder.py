"""
Умные Памятки — напоминалка для Windows.  v2.0

Что изменилось против v1:
  * данные лежат в %APPDATA%\\AdaptiveReminder, а не в рабочей папке
    (иначе при запуске из автозагрузки напоминания «терялись»);
  * из трея можно создать напоминание, посмотреть ближайшие и открыть настройки;
  * окно уведомления НЕ блокирует программу и показывает ВСЕ сработавшие сразу;
  * компактная вёрстка: форма сверху, списки во вкладках;
  * убрана рамка вокруг каждой подписи (QLabel наследует QFrame — стиль цеплялся ко всему);
  * месячные напоминания больше не уползают с 31 числа на 28 навсегда;
  * человеческие склонения: «1 минута», «22 часа», «2 дня»;
  * второй запуск не плодит копию, а показывает уже открытое окно;
  * файл данных пишется атомарно, битый JSON не затирается, а откладывается в .bad.
"""
from __future__ import annotations

import sys
import os
import time
import json
import shutil
import logging
import calendar
from datetime import datetime, timedelta
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

from PySide6.QtWidgets import (QApplication, QSystemTrayIcon, QMenu, QWidget,
                               QVBoxLayout, QLabel, QLineEdit, QPushButton, QDateTimeEdit,
                               QListWidget, QMessageBox, QHBoxLayout, QComboBox, QFrame,
                               QSizePolicy, QMainWindow, QDialog, QGridLayout,
                               QButtonGroup, QRadioButton, QListWidgetItem, QTabWidget,
                               QCheckBox, QSpinBox, QFileDialog, QAbstractItemView)
from PySide6.QtGui import QIcon, QAction, QPixmap, QPainter, QColor, QBrush, QFont
from PySide6.QtCore import (QTimer, QDateTime, Qt, QObject, QSize, Signal,
                            QSharedMemory, QStandardPaths)

# --- Платформа -------------------------------------------------------------
IS_WINDOWS = sys.platform == "win32"

try:
    import winsound
except ImportError:      # Linux/Mac — чтобы код хотя бы запускался и тестировался
    winsound = None

try:
    import winreg
except ImportError:
    winreg = None

# --- Константы -------------------------------------------------------------
APP_NAME = "Adaptive Reminder"
APP_TITLE = "Умные Памятки"
APP_VERSION = "2.0"

DATA_FILENAME = "reminders.json"
MISSED_FILENAME = "missed_reminders.json"
SETTINGS_FILENAME = "settings.json"

ICON_FILE = "icon.ico"
SOUND_FILE = "notification.wav"

REG_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_VALUE_NAME = "AdaptiveReminder"      # именно это имя пишется в реестр

TIMER_INTERVAL = 1000                    # мс

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")


# --- Пути ------------------------------------------------------------------

def resource_path(relative_path: str) -> str:
    """Путь к ресурсу внутри EXE (PyInstaller) или рядом со скриптом."""
    try:
        base_path = sys._MEIPASS          # noqa: SLF001 — так задумано в PyInstaller
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def data_dir() -> str:
    """Постоянная папка данных. Не зависит от того, откуда запущена программа."""
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = (os.environ.get("XDG_CONFIG_HOME")
                or os.path.join(os.path.expanduser("~"), ".config"))
    path = os.path.join(base, "AdaptiveReminder")
    os.makedirs(path, exist_ok=True)
    return path


def data_file(name: str) -> str:
    return os.path.join(data_dir(), name)


def migrate_old_data():
    """Переносит данные из старого места (рядом с exe) в %APPDATA%."""
    old_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    for name in (DATA_FILENAME, MISSED_FILENAME):
        old = os.path.join(old_dir, name)
        new = data_file(name)
        if os.path.exists(old) and not os.path.exists(new):
            try:
                shutil.copy2(old, new)
                logging.info("Перенесён старый файл данных: %s -> %s", old, new)
            except Exception as exc:
                logging.error("Не удалось перенести %s: %s", old, exc)


# --- Мелкие утилиты --------------------------------------------------------

def plural(number: int, one: str, few: str, many: str) -> str:
    """Русские склонения: 1 минута, 2 минуты, 5 минут."""
    number = abs(int(number))
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many


def human_duration(minutes: int) -> str:
    """«90» -> «1 час 30 минут». Используется в статусах и подсказках."""
    minutes = max(0, int(minutes))
    if minutes == 0:
        return "0 минут"
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} {plural(days, 'день', 'дня', 'дней')}")
    if hours:
        parts.append(f"{hours} {plural(hours, 'час', 'часа', 'часов')}")
    if mins:
        parts.append(f"{mins} {plural(mins, 'минуту', 'минуты', 'минут')}")
    return " ".join(parts)


def human_until(target: datetime, now: Optional[datetime] = None) -> str:
    """«через 5 минут», «через 2 часа», «просрочено на 10 минут»."""
    now = now or datetime.now()
    delta = target - now
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "просрочено на " + human_duration(max(1, abs(seconds) // 60))
    if seconds < 60:
        return "меньше минуты"
    return "через " + human_duration(seconds // 60)


def _add_months(sourcedate: datetime, months: int, anchor_day: Optional[int] = None) -> datetime:
    """Прибавляет месяцы, помня ИСХОДНОЕ число.

    Без anchor_day 31 января превращалось в 28 февраля, а дальше навсегда
    оставалось 28-м. С anchor_day оно вернётся к 31-му в марте.
    """
    month = sourcedate.month - 1 + months
    year = sourcedate.year + month // 12
    month = month % 12 + 1
    wanted = anchor_day or sourcedate.day
    day = min(wanted, calendar.monthrange(year, month)[1])
    return sourcedate.replace(year=year, month=month, day=day)


def darken_color(hex_color: str, amount: int = 30) -> str:
    hex_color = hex_color.lstrip("#")
    rgb = tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    darkened = tuple(max(0, c - amount) for c in rgb)
    return f"#{darkened[0]:02x}{darkened[1]:02x}{darkened[2]:02x}"


def make_fallback_icon() -> QIcon:
    """Рисуем иконку сами, если icon.ico не нашёлся — трей без иконки невидим."""
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QBrush(QColor("#FF9800")))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(4, 4, 56, 56)
    painter.setPen(QColor("#2b2b2b"))
    font = QFont()
    font.setPointSize(30)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pix.rect(), Qt.AlignCenter, "!")
    painter.end()
    return QIcon(pix)


def app_icon() -> QIcon:
    path = resource_path(ICON_FILE)
    if os.path.exists(path):
        icon = QIcon(path)
        if not icon.isNull():
            return icon
    return make_fallback_icon()


# --- Модель ----------------------------------------------------------------

class RepeatType(Enum):
    ONCE = auto()
    DAILY = auto()
    WEEKLY = auto()
    MONTHLY = auto()


REPEAT_LABELS = {
    RepeatType.ONCE: "Один раз",
    RepeatType.DAILY: "Каждый день",
    RepeatType.WEEKLY: "Каждую неделю",
    RepeatType.MONTHLY: "Каждый месяц",
}


@dataclass
class Reminder:
    message: str
    time: float                                  # когда сработает
    repeat_type: RepeatType
    original_time: float = field(default=None)   # когда завели (якорь повторов)
    created: float = field(default_factory=time.time)
    fired_at: Optional[float] = None             # когда реально сработало

    def __post_init__(self):
        if self.original_time is None:
            self.original_time = self.time

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message": self.message,
            "time": self.time,
            "repeat_type": self.repeat_type.name,
            "original_time": self.original_time,
            "created": self.created,
            "fired_at": self.fired_at,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Reminder":
        repeat_str = str(data.get("repeat_type", data.get("repeat", "ONCE")))
        try:
            repeat_type = RepeatType[repeat_str]
        except KeyError:
            # старый формат — человекочитаемые строки
            low = repeat_str.lower()
            if "день" in low:
                repeat_type = RepeatType.DAILY
            elif "недел" in low:
                repeat_type = RepeatType.WEEKLY
            elif "месяц" in low:
                repeat_type = RepeatType.MONTHLY
            else:
                repeat_type = RepeatType.ONCE

        stamp = float(data.get("time", time.time()))
        return Reminder(
            message=str(data.get("message", "(без текста)")),
            time=stamp,
            repeat_type=repeat_type,
            original_time=float(data.get("original_time", stamp)),
            created=float(data.get("created", stamp)),
            fired_at=data.get("fired_at"),
        )


# --- Настройки -------------------------------------------------------------

DEFAULT_SETTINGS = {
    "sound_enabled": True,
    "sound_path": "",            # пусто = звук из комплекта
    "popup_enabled": True,       # показывать окно поверх всего
    "tray_balloon": True,        # всплывашка в трее
    "default_snooze": 10,        # минуты для кнопки «Отложить»
    "minimize_to_tray": True,    # крестик прячет в трей, а не закрывает
    "start_minimized": False,
}


class Settings:
    def __init__(self, path: str):
        self.path = path
        self.values = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                self.values.update({k: v for k, v in stored.items()
                                    if k in DEFAULT_SETTINGS})
        except FileNotFoundError:
            pass
        except Exception as exc:
            logging.error("Не прочитались настройки: %s", exc)

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.values, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:
            logging.error("Не сохранились настройки: %s", exc)

    def __getitem__(self, key):
        return self.values.get(key, DEFAULT_SETTINGS.get(key))

    def __setitem__(self, key, value):
        self.values[key] = value


# --- Автозагрузка ----------------------------------------------------------

class WindowsStartupManager:
    def __init__(self, value_name: str, key_path: str):
        self.value_name = value_name
        self.key_path = key_path
        self.root = winreg.HKEY_CURRENT_USER if winreg else None

    def available(self) -> bool:
        return bool(winreg)

    def _command(self) -> str:
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}"'
        return f'"{sys.executable}" "{os.path.abspath(__file__)}"'

    def add(self) -> bool:
        if not winreg:
            return False
        try:
            with winreg.OpenKey(self.root, self.key_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, self.value_name, 0, winreg.REG_SZ, self._command())
            logging.info("Добавлено в автозагрузку.")
            return True
        except Exception as exc:
            logging.error("Автозагрузка (добавление): %s", exc)
            return False

    def remove(self) -> bool:
        if not winreg:
            return False
        try:
            with winreg.OpenKey(self.root, self.key_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, self.value_name)
            logging.info("Удалено из автозагрузки.")
            return True
        except FileNotFoundError:
            return True
        except Exception as exc:
            logging.error("Автозагрузка (удаление): %s", exc)
            return False

    def enabled(self) -> bool:
        if not winreg:
            return False
        try:
            with winreg.OpenKey(self.root, self.key_path, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, self.value_name)
            return True
        except FileNotFoundError:
            return False
        except Exception as exc:
            logging.error("Автозагрузка (проверка): %s", exc)
            return False


# --- Логика ----------------------------------------------------------------

class ReminderManager(QObject):
    changed = Signal()

    def __init__(self, data_path: str, missed_path: str):
        super().__init__()
        self.data_path = data_path
        self.missed_path = missed_path
        self.reminders: List[Reminder] = []
        self.missed_reminders: List[Reminder] = []
        self._load()

    # ---- файлы ----
    def _read_list(self, path: str) -> List[Reminder]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, list):
                raise ValueError("ожидался список")
            out = []
            for item in raw:
                try:
                    out.append(Reminder.from_dict(item))
                except Exception as exc:
                    logging.error("Пропущена битая запись: %s (%s)", item, exc)
            return out
        except FileNotFoundError:
            return []
        except Exception as exc:
            # НЕ затираем повреждённый файл — откладываем, вдруг data recovery
            logging.error("Файл %s повреждён: %s", path, exc)
            try:
                shutil.move(path, path + ".bad")
                logging.warning("Повреждённый файл сохранён как %s.bad", path)
            except Exception:
                pass
            return []

    def _write_list(self, items: List[Reminder], path: str):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump([r.to_dict() for r in items], f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)          # атомарно: не будет обрезанного файла
        except Exception as exc:
            logging.error("Не сохранился %s: %s", path, exc)

    def _load(self):
        now = time.time()
        loaded = self._read_list(self.data_path)
        self.missed_reminders = self._read_list(self.missed_path)

        for r in loaded:
            if r.time > now:
                self.reminders.append(r)
            elif r.repeat_type == RepeatType.ONCE:
                r.fired_at = r.fired_at or r.time
                self.missed_reminders.append(r)
            else:
                # повторное проспали, пока программа была выключена
                missed_copy = Reminder(message=r.message, time=r.time,
                                       repeat_type=RepeatType.ONCE,
                                       original_time=r.time, fired_at=r.time)
                self.missed_reminders.append(missed_copy)
                r.time = self.next_time(r)
                self.reminders.append(r)

        self.reminders.sort(key=lambda x: x.time)
        self.missed_reminders.sort(key=lambda x: x.fired_at or x.time)
        logging.info("Загружено: активных %d, требуют внимания %d",
                     len(self.reminders), len(self.missed_reminders))
        self.save_all()

    def save_all(self):
        self._write_list(self.reminders, self.data_path)
        self._write_list(self.missed_reminders, self.missed_path)

    # ---- операции ----
    def add(self, message: str, when: datetime, repeat: RepeatType) -> Reminder:
        stamp = when.timestamp()
        r = Reminder(message=message, time=stamp, repeat_type=repeat, original_time=stamp)
        self.reminders.append(r)
        self.reminders.sort(key=lambda x: x.time)
        self.save_all()
        self.changed.emit()
        logging.info("Добавлено: %s на %s", message, when)
        return r

    def update(self, reminder: Reminder, message: str, when: datetime, repeat: RepeatType):
        reminder.message = message
        reminder.time = when.timestamp()
        reminder.original_time = reminder.time
        reminder.repeat_type = repeat
        self.reminders.sort(key=lambda x: x.time)
        self.save_all()
        self.changed.emit()

    def delete(self, reminder: Reminder):
        if reminder in self.reminders:
            self.reminders.remove(reminder)
            self.save_all()
            self.changed.emit()

    def mark_done(self, reminder: Reminder):
        if reminder in self.missed_reminders:
            self.missed_reminders.remove(reminder)
            self.save_all()
            self.changed.emit()

    def clear_missed(self):
        self.missed_reminders.clear()
        self.save_all()
        self.changed.emit()

    def snooze(self, reminder: Reminder, minutes: int) -> Reminder:
        minutes = max(1, int(minutes))          # 0 минут = мгновенный повтор, не даём
        self.mark_done(reminder)
        new_time = time.time() + minutes * 60
        snoozed = Reminder(message=reminder.message, time=new_time,
                           repeat_type=RepeatType.ONCE, original_time=new_time)
        self.reminders.append(snoozed)
        self.reminders.sort(key=lambda x: x.time)
        self.save_all()
        self.changed.emit()
        logging.info("Отложено на %s: %s", human_duration(minutes), reminder.message)
        return snoozed

    # ---- расписание ----
    def next_time(self, reminder: Reminder) -> float:
        """Следующее срабатывание. Считаем ОТ ИСХОДНОГО времени, а не от
        последнего — иначе время суток и число месяца постепенно уползают."""
        base = datetime.fromtimestamp(reminder.original_time)
        now = time.time()

        if reminder.repeat_type == RepeatType.DAILY:
            step = timedelta(days=1)
        elif reminder.repeat_type == RepeatType.WEEKLY:
            step = timedelta(weeks=1)
        elif reminder.repeat_type == RepeatType.MONTHLY:
            anchor = base.day
            nxt = base
            guard = 0
            while nxt.timestamp() <= now and guard < 1200:
                nxt = _add_months(nxt, 1, anchor_day=anchor)
                guard += 1
            return nxt.timestamp()
        else:
            return reminder.time

        nxt = base
        guard = 0
        while nxt.timestamp() <= now and guard < 100000:
            nxt += step
            guard += 1
        return nxt.timestamp()

    def check(self) -> List[Reminder]:
        """Возвращает всё, что сработало к этому моменту."""
        now = time.time()
        triggered: List[Reminder] = []

        for reminder in self.reminders[:]:
            if now < reminder.time:
                continue
            fired_time = reminder.time

            if reminder.repeat_type == RepeatType.ONCE:
                self.reminders.remove(reminder)
                reminder.fired_at = fired_time
                self.missed_reminders.append(reminder)
                triggered.append(reminder)
            else:
                # запись о срабатывании, чтобы повторное не пропадало бесследно
                record = Reminder(message=reminder.message, time=fired_time,
                                  repeat_type=RepeatType.ONCE,
                                  original_time=fired_time, fired_at=fired_time)
                self.missed_reminders.append(record)
                triggered.append(record)
                reminder.time = self.next_time(reminder)

        if triggered:
            self.reminders.sort(key=lambda x: x.time)
            self.save_all()
            self.changed.emit()
        return triggered

    def next_reminder(self) -> Optional[Reminder]:
        return self.reminders[0] if self.reminders else None


# --- Общие стили -----------------------------------------------------------
# ВАЖНО: QLabel наследуется от QFrame. В версии v1 стиль QFrame{...} цеплялся
# к каждой подписи, поэтому вокруг слов «Сообщение:» рисовалась рамка.
# Теперь карточки помечены objectName="card" и стиль адресный.

STYLE = """
QWidget { background:#2b2b2b; color:#e9e9e9; font-family:'Segoe UI'; font-size:10pt; }
QFrame#card {
    background:#333333; border:1px solid #444444; border-radius:8px;
}
QLabel { background:transparent; border:none; padding:0; }
QLabel#hint  { color:#9b9b9b; font-size:9pt; }
QLabel#title { color:#FF9800; font-size:11pt; font-weight:bold; }
QLineEdit, QComboBox, QDateTimeEdit, QSpinBox {
    background:#3b3b3b; border:1px solid #555555; border-radius:6px;
    padding:6px 8px; min-height:18px; selection-background-color:#0078d4;
}
QLineEdit:focus, QComboBox:focus, QDateTimeEdit:focus, QSpinBox:focus {
    border:1px solid #0078d4; background:#404040;
}
QComboBox::drop-down, QDateTimeEdit::drop-down { border:none; width:18px; }
QPushButton {
    background:#4a4a4a; border:none; border-radius:6px;
    padding:7px 12px; font-weight:600;
}
QPushButton:hover  { background:#565656; }
QPushButton:pressed{ background:#3f3f3f; }
QPushButton:disabled { background:#3a3a3a; color:#777777; }
QListWidget {
    background:#303030; border:1px solid #4a4a4a; border-radius:6px; outline:none;
}
QListWidget::item { padding:7px 8px; border-bottom:1px solid #3d3d3d; }
QListWidget::item:selected { background:#0078d4; color:#ffffff; }
QTabWidget::pane { border:1px solid #444444; border-radius:8px; top:-1px; }
QTabBar::tab {
    background:#333333; color:#bdbdbd; padding:7px 14px;
    border:1px solid #444444; border-bottom:none;
    border-top-left-radius:7px; border-top-right-radius:7px; margin-right:2px;
}
QTabBar::tab:selected { background:#3f3f3f; color:#ffffff; }
QCheckBox, QRadioButton { background:transparent; spacing:8px; padding:3px; }
QCheckBox::indicator, QRadioButton::indicator { width:16px; height:16px; }
QCheckBox::indicator {
    border:1px solid #6a6a6a; border-radius:4px; background:#3b3b3b;
}
QCheckBox::indicator:checked {
    border:1px solid #FF9800; background:#FF9800;
}
QCheckBox::indicator:disabled { border-color:#4a4a4a; background:#333333; }
QRadioButton::indicator {
    border:1px solid #6a6a6a; border-radius:8px; background:#3b3b3b;
}
QRadioButton::indicator:checked {
    border:4px solid #FF9800; background:#2b2b2b; border-radius:8px;
}
QScrollBar:vertical { background:#2b2b2b; width:10px; margin:0; }
QScrollBar::handle:vertical { background:#555555; border-radius:5px; min-height:24px; }
QScrollBar::handle:vertical:hover { background:#666666; }
QScrollBar::add-line, QScrollBar::sub-line { height:0; }
"""


def accent_button(text: str, color: str, big: bool = False) -> QPushButton:
    btn = QPushButton(text)
    pad = "10px 14px" if big else "7px 12px"
    size = "10.5pt" if big else "9.5pt"
    btn.setStyleSheet(f"""
        QPushButton {{ background:{color}; color:#ffffff; padding:{pad};
                       font-size:{size}; font-weight:700; border-radius:6px; }}
        QPushButton:hover  {{ background:{darken_color(color, 22)}; }}
        QPushButton:pressed{{ background:{darken_color(color, 40)}; }}
    """)
    return btn


def card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    return frame


# --- Диалог «Отложить» -----------------------------------------------------

class SnoozeDialog(QDialog):
    PRESETS = [("5 минут", 5), ("10 минут", 10), ("15 минут", 15), ("30 минут", 30),
               ("1 час", 60), ("2 часа", 120), ("4 часа", 240), ("До завтра", 1440)]

    def __init__(self, parent=None, default_minutes: int = 10):
        super().__init__(parent)
        self.setWindowTitle("Отложить напоминание")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.selected_minutes = default_minutes
        self._build(default_minutes)

    def _build(self, default_minutes: int):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("На сколько отложить?")
        title.setObjectName("title")
        root.addWidget(title)

        grid = QGridLayout()
        grid.setSpacing(6)
        self.group = QButtonGroup(self)
        for index, (text, minutes) in enumerate(self.PRESETS):
            radio = QRadioButton(text)
            radio.minutes = minutes
            self.group.addButton(radio)
            grid.addWidget(radio, index // 2, index % 2)
            if minutes == default_minutes:
                radio.setChecked(True)
        root.addLayout(grid)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#4a4a4a;")
        root.addWidget(line)

        custom = QHBoxLayout()
        custom.addWidget(QLabel("Своё время:"))
        self.hours = QSpinBox()
        self.hours.setRange(0, 72)
        self.hours.setSuffix(" ч")
        self.minutes = QSpinBox()
        self.minutes.setRange(0, 59)
        self.minutes.setSuffix(" мин")
        self.hours.setValue(default_minutes // 60)
        self.minutes.setValue(default_minutes % 60)
        custom.addWidget(self.hours)
        custom.addWidget(self.minutes)
        custom.addStretch(1)
        root.addLayout(custom)

        self.preview = QLabel()
        self.preview.setObjectName("hint")
        root.addWidget(self.preview)

        buttons = QHBoxLayout()
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        self.ok_btn = accent_button("Отложить", "#4CAF50")
        self.ok_btn.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addStretch(1)
        buttons.addWidget(self.ok_btn)
        root.addLayout(buttons)

        self.group.buttonClicked.connect(self._preset_picked)
        self.hours.valueChanged.connect(self._custom_changed)
        self.minutes.valueChanged.connect(self._custom_changed)
        self._refresh()

    def _preset_picked(self, button):
        self.selected_minutes = button.minutes
        for box, value in ((self.hours, button.minutes // 60),
                           (self.minutes, button.minutes % 60)):
            box.blockSignals(True)
            box.setValue(value)
            box.blockSignals(False)
        self._refresh()

    def _custom_changed(self):
        self.selected_minutes = self.hours.value() * 60 + self.minutes.value()
        match = None
        for button in self.group.buttons():
            if button.minutes == self.selected_minutes:
                match = button
                break
        self.group.setExclusive(False)
        for button in self.group.buttons():
            button.setChecked(button is match)
        self.group.setExclusive(True)
        self._refresh()

    def _refresh(self):
        minutes = self.selected_minutes
        if minutes <= 0:
            self.preview.setText("Укажи хотя бы 1 минуту")
            self.preview.setStyleSheet("color:#f44336;")
            self.ok_btn.setEnabled(False)
            return
        when = datetime.now() + timedelta(minutes=minutes)
        self.preview.setText(f"Напомню в {when:%H:%M} — это {human_duration(minutes)}")
        self.preview.setStyleSheet("color:#9b9b9b;")
        self.ok_btn.setEnabled(True)

    def get_minutes(self) -> int:
        return max(1, self.selected_minutes)


# --- Окно уведомления (НЕ блокирующее) -------------------------------------

class NotificationWindow(QDialog):
    """Показывает все сработавшие напоминания списком.

    В v1 тут был QMessageBox.exec() — он замораживал программу и показывал
    только последнее из сработавших.
    """
    done_requested = Signal(object)
    snooze_requested = Signal(object, int)

    def __init__(self, parent=None, default_snooze: int = 10):
        super().__init__(parent)
        self.default_snooze = default_snooze
        self.items: List[Reminder] = []
        self.setWindowTitle("Напоминание")
        self.setWindowIcon(app_icon())
        self.setModal(False)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setMinimumWidth(420)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        self.header = QLabel("Напоминание")
        self.header.setObjectName("title")
        self.header.setStyleSheet("color:#FF9800; font-size:13pt; font-weight:bold;")
        root.addWidget(self.header)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setMinimumHeight(120)
        root.addWidget(self.list)

        row = QHBoxLayout()
        self.done_btn = accent_button("Сделано", "#4CAF50")
        self.done_btn.clicked.connect(self._done)
        self.snooze_btn = accent_button(f"Отложить {self.default_snooze} мин", "#FFC107")
        self.snooze_btn.setStyleSheet(self.snooze_btn.styleSheet().replace("#ffffff", "#2b2b2b"))
        self.snooze_btn.clicked.connect(self._quick_snooze)
        self.snooze_more = QPushButton("Отложить на…")
        self.snooze_more.clicked.connect(self._snooze_dialog)
        row.addWidget(self.done_btn)
        row.addWidget(self.snooze_btn)
        row.addWidget(self.snooze_more)
        root.addLayout(row)

        bottom = QHBoxLayout()
        self.hint = QLabel()
        self.hint.setObjectName("hint")
        bottom.addWidget(self.hint)
        bottom.addStretch(1)
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.hide)
        bottom.addWidget(close_btn)
        root.addLayout(bottom)

    def present(self, reminders: List[Reminder]):
        """Добавляет новые сработавшие к уже показанным."""
        for r in reminders:
            if r not in self.items:
                self.items.append(r)
        self.refresh()
        if self.items:
            self.show()
            self.raise_()
            self.activateWindow()

    def refresh(self):
        self.list.clear()
        for r in self.items:
            when = datetime.fromtimestamp(r.fired_at or r.time)
            item = QListWidgetItem(f"{when:%H:%M}  ·  {r.message}")
            item.setData(Qt.UserRole, r)
            self.list.addItem(item)
        count = len(self.items)
        self.header.setText("Напоминание" if count == 1
                            else f"Напоминания — {count} шт.")
        self.hint.setText("Выбери строку и нажми действие"
                          if count > 1 else "")
        if self.list.count():
            self.list.setCurrentRow(0)
        has = bool(self.items)
        for widget in (self.done_btn, self.snooze_btn, self.snooze_more):
            widget.setEnabled(has)
        if not has:
            self.hide()

    def _current(self) -> Optional[Reminder]:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _drop(self, reminder):
        if reminder in self.items:
            self.items.remove(reminder)
        self.refresh()

    def _done(self):
        reminder = self._current()
        if reminder:
            self.done_requested.emit(reminder)
            self._drop(reminder)

    def _quick_snooze(self):
        reminder = self._current()
        if reminder:
            self.snooze_requested.emit(reminder, self.default_snooze)
            self._drop(reminder)

    def _snooze_dialog(self):
        reminder = self._current()
        if not reminder:
            return
        dialog = SnoozeDialog(self, self.default_snooze)
        if dialog.exec() == QDialog.Accepted:
            self.snooze_requested.emit(reminder, dialog.get_minutes())
            self._drop(reminder)


# --- Быстрое создание из трея ---------------------------------------------

class QuickAddDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Новое напоминание")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setMinimumWidth(380)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("О чём напомнить?")
        title.setObjectName("title")
        root.addWidget(title)

        self.message = QLineEdit()
        self.message.setPlaceholderText("Например: позвонить бабушке")
        root.addWidget(self.message)

        quick = QHBoxLayout()
        for text, minutes in (("+15 мин", 15), ("+30 мин", 30),
                              ("+1 час", 60), ("+3 часа", 180)):
            btn = QPushButton(text)
            btn.clicked.connect(lambda _=False, m=minutes: self._shift(m))
            quick.addWidget(btn)
        root.addLayout(quick)

        row = QHBoxLayout()
        self.when = QDateTimeEdit(QDateTime.currentDateTime().addSecs(900))
        self.when.setCalendarPopup(True)
        self.when.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.repeat = QComboBox()
        for rt in RepeatType:
            self.repeat.addItem(REPEAT_LABELS[rt], rt)
        row.addWidget(self.when, 2)
        row.addWidget(self.repeat, 1)
        root.addLayout(row)

        self.preview = QLabel()
        self.preview.setObjectName("hint")
        root.addWidget(self.preview)

        buttons = QHBoxLayout()
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        self.ok_btn = accent_button("Создать", "#FF9800")
        self.ok_btn.clicked.connect(self._accept)
        buttons.addWidget(cancel)
        buttons.addStretch(1)
        buttons.addWidget(self.ok_btn)
        root.addLayout(buttons)

        self.message.returnPressed.connect(self._accept)
        self.when.dateTimeChanged.connect(self._refresh)
        self._refresh()
        self.message.setFocus()

    def _shift(self, minutes: int):
        self.when.setDateTime(QDateTime.currentDateTime().addSecs(minutes * 60))

    def _refresh(self):
        target = self.when.dateTime().toPython()
        if target <= datetime.now():
            self.preview.setText("Время уже прошло")
            self.preview.setStyleSheet("color:#f44336;")
        else:
            self.preview.setText("Сработает " + human_until(target))
            self.preview.setStyleSheet("color:#9b9b9b;")

    def _accept(self):
        if not self.message.text().strip():
            self.preview.setText("Введи текст напоминания")
            self.preview.setStyleSheet("color:#f44336;")
            return
        if self.when.dateTime().toPython() <= datetime.now():
            self.preview.setText("Укажи время в будущем")
            self.preview.setStyleSheet("color:#f44336;")
            return
        self.accept()

    def result_data(self):
        return (self.message.text().strip(),
                self.when.dateTime().toPython(),
                self.repeat.currentData())


# --- Настройки -------------------------------------------------------------

class SettingsDialog(QDialog):
    def __init__(self, parent, settings: Settings, startup: WindowsStartupManager):
        super().__init__(parent)
        self.settings = settings
        self.startup = startup
        self.setWindowTitle("Настройки")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setMinimumWidth(420)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Настройки")
        title.setObjectName("title")
        root.addWidget(title)

        self.autostart = QCheckBox("Запускать вместе с Windows")
        self.autostart.setChecked(self.startup.enabled())
        self.autostart.setEnabled(self.startup.available())
        if not self.startup.available():
            self.autostart.setText("Запуск с Windows (недоступно на этой системе)")
        root.addWidget(self.autostart)

        self.start_min = QCheckBox("Запускаться свёрнутым в трей")
        self.start_min.setChecked(self.settings["start_minimized"])
        root.addWidget(self.start_min)

        self.to_tray = QCheckBox("Крестик сворачивает в трей, а не закрывает")
        self.to_tray.setChecked(self.settings["minimize_to_tray"])
        root.addWidget(self.to_tray)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#4a4a4a;")
        root.addWidget(line)

        self.popup = QCheckBox("Показывать окно поверх всех окон")
        self.popup.setChecked(self.settings["popup_enabled"])
        root.addWidget(self.popup)

        self.balloon = QCheckBox("Показывать всплывающее сообщение в трее")
        self.balloon.setChecked(self.settings["tray_balloon"])
        root.addWidget(self.balloon)

        self.sound = QCheckBox("Звук при срабатывании")
        self.sound.setChecked(self.settings["sound_enabled"])
        root.addWidget(self.sound)

        sound_row = QHBoxLayout()
        self.sound_path = QLineEdit(self.settings["sound_path"])
        self.sound_path.setPlaceholderText("Звук из комплекта (notification.wav)")
        browse = QPushButton("Выбрать…")
        browse.clicked.connect(self._pick_sound)
        clear = QPushButton("Сброс")
        clear.clicked.connect(lambda: self.sound_path.clear())
        sound_row.addWidget(self.sound_path, 1)
        sound_row.addWidget(browse)
        sound_row.addWidget(clear)
        root.addLayout(sound_row)

        snooze_row = QHBoxLayout()
        snooze_row.addWidget(QLabel("Кнопка «Отложить» откладывает на:"))
        self.snooze = QSpinBox()
        self.snooze.setRange(1, 240)
        self.snooze.setSuffix(" мин")
        self.snooze.setValue(int(self.settings["default_snooze"]))
        snooze_row.addWidget(self.snooze)
        snooze_row.addStretch(1)
        root.addLayout(snooze_row)

        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setStyleSheet("color:#4a4a4a;")
        root.addWidget(line2)

        where = QLabel(f"Данные хранятся тут:\n{data_dir()}")
        where.setObjectName("hint")
        where.setWordWrap(True)
        root.addWidget(where)

        open_folder = QPushButton("Открыть папку с данными")
        open_folder.clicked.connect(self._open_folder)
        root.addWidget(open_folder)

        buttons = QHBoxLayout()
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        save = accent_button("Сохранить", "#4CAF50")
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addStretch(1)
        buttons.addWidget(save)
        root.addLayout(buttons)

    def _pick_sound(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выбери звук", "",
                                              "Звуки WAV (*.wav)")
        if path:
            self.sound_path.setText(path)

    def _open_folder(self):
        folder = data_dir()
        try:
            if IS_WINDOWS:
                os.startfile(folder)          # noqa: S606
            else:
                os.system(f'xdg-open "{folder}"')
        except Exception as exc:
            logging.error("Не открылась папка: %s", exc)

    def _save(self):
        self.settings["start_minimized"] = self.start_min.isChecked()
        self.settings["minimize_to_tray"] = self.to_tray.isChecked()
        self.settings["popup_enabled"] = self.popup.isChecked()
        self.settings["tray_balloon"] = self.balloon.isChecked()
        self.settings["sound_enabled"] = self.sound.isChecked()
        self.settings["sound_path"] = self.sound_path.text().strip()
        self.settings["default_snooze"] = self.snooze.value()
        self.settings.save()

        if self.startup.available():
            if self.autostart.isChecked() and not self.startup.enabled():
                self.startup.add()
            elif not self.autostart.isChecked() and self.startup.enabled():
                self.startup.remove()
        self.accept()


# --- Главное окно ----------------------------------------------------------

class AdaptiveReminderApp(QMainWindow):
    def __init__(self):
        super().__init__()
        migrate_old_data()

        self.settings = Settings(data_file(SETTINGS_FILENAME))
        self.manager = ReminderManager(data_file(DATA_FILENAME),
                                       data_file(MISSED_FILENAME))
        self.startup = WindowsStartupManager(REG_VALUE_NAME, REG_KEY_PATH)
        self.sound_path = resource_path(SOUND_FILE)
        self.editing: Optional[Reminder] = None
        self._force_quit = False

        self.setWindowTitle(f"{APP_TITLE} {APP_VERSION}")
        self.setWindowIcon(app_icon())
        self.setStyleSheet(STYLE)
        self.resize(560, 640)
        self.setMinimumSize(460, 540)

        self.notification = NotificationWindow(None, int(self.settings["default_snooze"]))
        self.notification.setStyleSheet(STYLE)
        self.notification.done_requested.connect(self._notification_done)
        self.notification.snooze_requested.connect(self._notification_snooze)

        self._build_ui()
        self._build_tray()
        self.manager.changed.connect(self.refresh_lists)
        self.refresh_lists()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.on_tick)
        self.timer.start(TIMER_INTERVAL)

    # ---------- интерфейс ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        root.addWidget(self._form_card())
        root.addWidget(self._tabs(), 1)

        self.status = QLabel("Готово к работе")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

    def _form_card(self) -> QFrame:
        frame = card()
        layout = QVBoxLayout(frame)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 10, 12, 12)

        head = QHBoxLayout()
        self.form_title = QLabel("Новое напоминание")
        self.form_title.setObjectName("title")
        head.addWidget(self.form_title)
        head.addStretch(1)
        self.cancel_edit_btn = QPushButton("Отменить правку")
        self.cancel_edit_btn.clicked.connect(self.stop_editing)
        self.cancel_edit_btn.hide()
        head.addWidget(self.cancel_edit_btn)
        layout.addLayout(head)

        self.message_input = QLineEdit()
        self.message_input.setPlaceholderText("О чём напомнить? Например: репетиция")
        self.message_input.returnPressed.connect(self.submit)
        layout.addWidget(self.message_input)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.datetime_input = QDateTimeEdit(QDateTime.currentDateTime().addSecs(900))
        self.datetime_input.setCalendarPopup(True)
        self.datetime_input.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.datetime_input.dateTimeChanged.connect(self._update_preview)
        self.repeat_combo = QComboBox()
        for rt in RepeatType:
            self.repeat_combo.addItem(REPEAT_LABELS[rt], rt)
        row.addWidget(self.datetime_input, 3)
        row.addWidget(self.repeat_combo, 2)
        layout.addLayout(row)

        quick = QHBoxLayout()
        quick.setSpacing(6)
        presets = [("+15 мин", lambda: self._shift(15)),
                   ("+1 час", lambda: self._shift(60)),
                   ("+3 часа", lambda: self._shift(180)),
                   ("Завтра 9:00", self._tomorrow_morning),
                   ("Вечером 19:00", self._this_evening)]
        for text, handler in presets:
            btn = QPushButton(text)
            btn.clicked.connect(handler)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            quick.addWidget(btn)
        layout.addLayout(quick)

        self.preview = QLabel()
        self.preview.setObjectName("hint")
        layout.addWidget(self.preview)

        self.submit_btn = accent_button("Создать напоминание", "#FF9800", big=True)
        self.submit_btn.clicked.connect(self.submit)
        layout.addWidget(self.submit_btn)

        self._update_preview()
        return frame

    def _tabs(self) -> QTabWidget:
        self.tabs = QTabWidget()

        active = QWidget()
        active_layout = QVBoxLayout(active)
        active_layout.setContentsMargins(8, 8, 8, 8)
        active_layout.setSpacing(8)
        self.active_list = QListWidget()
        self.active_list.itemDoubleClicked.connect(lambda _: self.start_editing())
        active_layout.addWidget(self.active_list)
        active_buttons = QHBoxLayout()
        edit_btn = QPushButton("Изменить")
        edit_btn.clicked.connect(self.start_editing)
        delete_btn = QPushButton("Удалить")
        delete_btn.clicked.connect(self.delete_selected)
        active_buttons.addWidget(edit_btn)
        active_buttons.addWidget(delete_btn)
        active_buttons.addStretch(1)
        active_layout.addLayout(active_buttons)
        self.tabs.addTab(active, "Активные")

        missed = QWidget()
        missed_layout = QVBoxLayout(missed)
        missed_layout.setContentsMargins(8, 8, 8, 8)
        missed_layout.setSpacing(8)
        self.missed_list = QListWidget()
        missed_layout.addWidget(self.missed_list)
        missed_buttons = QHBoxLayout()
        done_btn = accent_button("Сделано", "#4CAF50")
        done_btn.clicked.connect(self.mark_done_selected)
        snooze_btn = QPushButton("Отложить на…")
        snooze_btn.clicked.connect(self.snooze_selected)
        clear_btn = QPushButton("Очистить всё")
        clear_btn.clicked.connect(self.clear_missed)
        missed_buttons.addWidget(done_btn)
        missed_buttons.addWidget(snooze_btn)
        missed_buttons.addStretch(1)
        missed_buttons.addWidget(clear_btn)
        missed_layout.addLayout(missed_buttons)
        self.tabs.addTab(missed, "Требуют внимания")

        return self.tabs

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(app_icon())
        self.tray.setToolTip(APP_TITLE)

        menu = QMenu()
        menu.setStyleSheet(STYLE)

        self.next_action = QAction("Ближайших нет", self)
        self.next_action.setEnabled(False)
        menu.addAction(self.next_action)
        menu.addSeparator()

        add_action = QAction("Новое напоминание…", self)
        add_action.triggered.connect(self.quick_add)
        menu.addAction(add_action)

        self.upcoming_menu = QMenu("Ближайшие", menu)
        menu.addMenu(self.upcoming_menu)
        menu.addSeparator()

        show_action = QAction("Открыть окно", self)
        show_action.triggered.connect(self.show_window)
        menu.addAction(show_action)

        settings_action = QAction("Настройки…", self)
        settings_action.triggered.connect(self.open_settings)
        menu.addAction(settings_action)
        menu.addSeparator()

        quit_action = QAction("Выход", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_clicked)
        self.tray.show()

    # ---------- вспомогательное ----------
    def _shift(self, minutes: int):
        self.datetime_input.setDateTime(QDateTime.currentDateTime().addSecs(minutes * 60))

    def _tomorrow_morning(self):
        target = QDateTime.currentDateTime().addDays(1)
        target.setTime(target.time().fromString("09:00", "HH:mm"))
        self.datetime_input.setDateTime(target)

    def _this_evening(self):
        now = QDateTime.currentDateTime()
        target = QDateTime(now.date(), now.time().fromString("19:00", "HH:mm"))
        if target <= now:
            target = target.addDays(1)
        self.datetime_input.setDateTime(target)

    def _update_preview(self):
        target = self.datetime_input.dateTime().toPython()
        if target <= datetime.now():
            self.preview.setText("Время уже прошло — выбери будущее")
            self.preview.setStyleSheet("color:#f44336;")
        else:
            self.preview.setText("Сработает " + human_until(target))
            self.preview.setStyleSheet("color:#9b9b9b;")

    def set_status(self, text: str, color: str = "#9b9b9b"):
        self.status.setText(text)
        self.status.setStyleSheet(f"color:{color};")

    # ---------- списки ----------
    def refresh_lists(self):
        self.active_list.clear()
        for r in self.manager.reminders:
            label = f"{r.dt:%d.%m %H:%M}   {r.message}"
            if r.repeat_type != RepeatType.ONCE:
                label += f"   · {REPEAT_LABELS[r.repeat_type].lower()}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, r)
            item.setToolTip(f"{r.message}\n{r.dt:%d.%m.%Y %H:%M} — {human_until(r.dt)}")
            self.active_list.addItem(item)

        self.missed_list.clear()
        for r in self.manager.missed_reminders:
            when = datetime.fromtimestamp(r.fired_at or r.time)
            item = QListWidgetItem(f"{when:%d.%m %H:%M}   {r.message}")
            item.setData(Qt.UserRole, r)
            self.missed_list.addItem(item)

        self.tabs.setTabText(0, f"Активные ({len(self.manager.reminders)})")
        self.tabs.setTabText(1, f"Требуют внимания ({len(self.manager.missed_reminders)})")
        self._refresh_tray()

    def _refresh_tray(self):
        nxt = self.manager.next_reminder()
        if nxt:
            self.next_action.setText(f"Ближайшее: {nxt.dt:%d.%m %H:%M} — {nxt.message[:32]}")
            self.tray.setToolTip(f"{APP_TITLE}\nБлижайшее: {nxt.dt:%H:%M} {nxt.message[:40]}")
        else:
            self.next_action.setText("Ближайших напоминаний нет")
            self.tray.setToolTip(APP_TITLE)

        self.upcoming_menu.clear()
        upcoming = self.manager.reminders[:7]
        if not upcoming:
            empty = QAction("Пусто", self)
            empty.setEnabled(False)
            self.upcoming_menu.addAction(empty)
            return
        for r in upcoming:
            action = QAction(f"{r.dt:%d.%m %H:%M}  {r.message[:40]}", self)
            action.triggered.connect(lambda _=False, rem=r: self._focus_reminder(rem))
            self.upcoming_menu.addAction(action)

    def _focus_reminder(self, reminder: Reminder):
        self.show_window()
        self.tabs.setCurrentIndex(0)
        for row in range(self.active_list.count()):
            if self.active_list.item(row).data(Qt.UserRole) is reminder:
                self.active_list.setCurrentRow(row)
                break

    def _selected(self, widget: QListWidget) -> Optional[Reminder]:
        item = widget.currentItem()
        return item.data(Qt.UserRole) if item else None

    # ---------- действия ----------
    def submit(self):
        message = self.message_input.text().strip()
        when = self.datetime_input.dateTime().toPython()
        repeat = self.repeat_combo.currentData()

        if not message:
            self.set_status("Введи текст напоминания", "#f44336")
            self.message_input.setFocus()
            return
        if when <= datetime.now():
            self.set_status("Укажи время в будущем", "#f44336")
            return

        if self.editing is not None:
            self.manager.update(self.editing, message, when, repeat)
            self.set_status(f"Изменено: {message} — {when:%d.%m %H:%M}", "#4CAF50")
            self.stop_editing()
        else:
            self.manager.add(message, when, repeat)
            self.set_status(f"Создано: {message} — {when:%d.%m %H:%M} "
                            f"({human_until(when)})", "#4CAF50")
            self.message_input.clear()
        self.datetime_input.setDateTime(QDateTime.currentDateTime().addSecs(900))

    def start_editing(self):
        reminder = self._selected(self.active_list)
        if not reminder:
            self.set_status("Сначала выбери напоминание в списке", "#FFC107")
            return
        self.editing = reminder
        self.message_input.setText(reminder.message)
        self.datetime_input.setDateTime(QDateTime.fromSecsSinceEpoch(int(reminder.time)))
        index = self.repeat_combo.findData(reminder.repeat_type)
        if index >= 0:
            self.repeat_combo.setCurrentIndex(index)
        self.form_title.setText("Правка напоминания")
        self.submit_btn.setText("Сохранить изменения")
        self.cancel_edit_btn.show()
        self.message_input.setFocus()

    def stop_editing(self):
        self.editing = None
        self.form_title.setText("Новое напоминание")
        self.submit_btn.setText("Создать напоминание")
        self.cancel_edit_btn.hide()
        self.message_input.clear()

    def delete_selected(self):
        reminder = self._selected(self.active_list)
        if not reminder:
            self.set_status("Сначала выбери, что удалить", "#FFC107")
            return
        if self.editing is reminder:
            self.stop_editing()
        self.manager.delete(reminder)
        self.set_status("Напоминание удалено")

    def mark_done_selected(self):
        reminder = self._selected(self.missed_list)
        if not reminder:
            self.set_status("Сначала выбери строку", "#FFC107")
            return
        self.manager.mark_done(reminder)
        self.notification._drop(reminder)
        self.set_status("Отмечено выполненным", "#4CAF50")

    def snooze_selected(self):
        reminder = self._selected(self.missed_list)
        if not reminder:
            self.set_status("Сначала выбери, что отложить", "#FFC107")
            return
        dialog = SnoozeDialog(self, int(self.settings["default_snooze"]))
        dialog.setStyleSheet(STYLE)
        if dialog.exec() == QDialog.Accepted:
            minutes = dialog.get_minutes()
            self.manager.snooze(reminder, minutes)
            self.notification._drop(reminder)
            self.set_status(f"Отложено на {human_duration(minutes)}", "#2196F3")

    def clear_missed(self):
        if not self.manager.missed_reminders:
            return
        answer = QMessageBox.question(self, "Очистить список",
                                      "Убрать все записи из «Требуют внимания»?")
        if answer == QMessageBox.Yes:
            self.manager.clear_missed()
            self.notification.items.clear()
            self.notification.refresh()
            self.set_status("Список очищен")

    def quick_add(self):
        dialog = QuickAddDialog(self)
        dialog.setStyleSheet(STYLE)
        if dialog.exec() == QDialog.Accepted:
            message, when, repeat = dialog.result_data()
            self.manager.add(message, when, repeat)
            self.set_status(f"Создано: {message} — {when:%d.%m %H:%M}", "#4CAF50")
            if self.settings["tray_balloon"]:
                self.tray.showMessage(APP_TITLE,
                                      f"Напомню {when:%d.%m в %H:%M}: {message}",
                                      QSystemTrayIcon.Information, 4000)

    def open_settings(self):
        dialog = SettingsDialog(self, self.settings, self.startup)
        dialog.setStyleSheet(STYLE)
        if dialog.exec() == QDialog.Accepted:
            minutes = int(self.settings["default_snooze"])
            self.notification.default_snooze = minutes
            self.notification.snooze_btn.setText(f"Отложить {minutes} мин")
            self.set_status("Настройки сохранены", "#4CAF50")

    # ---------- тик таймера ----------
    def on_tick(self):
        triggered = self.manager.check()
        if not triggered:
            return
        logging.info("Сработало напоминаний: %d", len(triggered))

        self._play_sound()

        if self.settings["tray_balloon"]:
            if len(triggered) == 1:
                self.tray.showMessage("Напоминание", triggered[0].message,
                                      QSystemTrayIcon.Information, 8000)
            else:
                names = ", ".join(r.message for r in triggered[:3])
                self.tray.showMessage(f"Напоминаний: {len(triggered)}", names,
                                      QSystemTrayIcon.Information, 8000)

        if self.settings["popup_enabled"]:
            self.notification.present(triggered)

        last = triggered[-1].message
        self.set_status(f"Сработало: {last}"
                        + (f" (и ещё {len(triggered)-1})" if len(triggered) > 1 else ""),
                        "#FF9800")

    def _play_sound(self):
        if not self.settings["sound_enabled"]:
            return
        path = self.settings["sound_path"] or self.sound_path
        if not path or not os.path.exists(path):
            if winsound:
                try:
                    winsound.MessageBeep()
                except Exception:
                    pass
            return
        if winsound:
            try:
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception as exc:
                logging.error("Звук не воспроизвёлся: %s", exc)

    def _notification_done(self, reminder):
        self.manager.mark_done(reminder)

    def _notification_snooze(self, reminder, minutes):
        self.manager.snooze(reminder, minutes)

    # ---------- окно/трей ----------
    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def toggle_window(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.show_window()

    def _tray_clicked(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.toggle_window()

    def closeEvent(self, event):
        if self._force_quit or not self.settings["minimize_to_tray"]:
            event.accept()
            return
        event.ignore()
        self.hide()
        self.tray.showMessage(APP_TITLE, "Программа свернулась в трей и продолжает следить.",
                              QSystemTrayIcon.Information, 3000)

    def quit_app(self):
        self._force_quit = True
        self.manager.save_all()
        self.settings.save()
        self.tray.hide()
        QApplication.quit()


# --- Точка входа -----------------------------------------------------------

def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(STYLE)

    # Не даём запустить вторую копию: иначе два процесса пишут в один файл.
    lock = QSharedMemory("AdaptiveReminderSingleInstance")
    if not lock.create(1):
        QMessageBox.information(None, APP_TITLE,
                                "Программа уже запущена — ищи её значок в трее.")
        return 0
    app._instance_lock = lock          # держим ссылку, иначе GC освободит

    if not QSystemTrayIcon.isSystemTrayAvailable():
        logging.warning("Системный трей недоступен — работаем только окном.")

    window = AdaptiveReminderApp()
    if not window.settings["start_minimized"]:
        window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
