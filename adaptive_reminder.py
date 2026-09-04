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
                               QCheckBox, QSpinBox, QFileDialog, QAbstractItemView,
                               QScrollArea, QTimeEdit)
from PySide6.QtGui import (QIcon, QAction, QPixmap, QPainter, QColor, QBrush,
                           QFont, QKeySequence, QPolygon, QPen, QShortcut)
from PySide6.QtCore import (QTimer, QDateTime, QTime, Qt, QObject, QSize, Signal, QPoint,
                            QAbstractNativeEventFilter,
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
APP_VERSION = "2.6"

DATA_FILENAME = "reminders.json"
MISSED_FILENAME = "missed_reminders.json"
ARCHIVE_FILENAME = "archive.json"
SETTINGS_FILENAME = "settings.json"

ICON_FILE = "icon.ico"
SOUND_FILE = "notification.wav"

REG_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_VALUE_NAME = "AdaptiveReminder"      # именно это имя пишется в реестр

TIMER_INTERVAL = 1000                    # мс

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

# Токены оформления. Уровни поверхностей (surface elevation): чем ближе
# элемент к пользователю, тем заметнее он отделён от фона. На тёмной теме
# поверхность светлеет, на светлой — наоборот, темнеет рамкой и тенью.
DARK_PALETTE = {
    "bg":        "#16151a",
    "surface":   "#1e1d24",
    "surface2":  "#26252e",
    "surface3":  "#2f2e39",
    "line":      "#34333f",
    "line_soft": "#26252e",
    "text":      "#eceaf2",
    "text_dim":  "#8f8c9e",
    "accent":    "#f0912b",
    "ok":        "#3fa45b",
    "warn":      "#d9a520",
    "danger":    "#c9484f",
    "info":      "#4a90d9",
    "lead":      "#8fbde8",
    "on_accent": "#241f14",
    "is_light":  False,
}

LIGHT_PALETTE = {
    "bg":        "#f4f4f7",
    "surface":   "#ffffff",
    "surface2":  "#f0eff4",
    "surface3":  "#e6e5ec",
    "line":      "#d8d7e0",
    "line_soft": "#e8e7ee",
    "text":      "#1a1a24",
    # #6b6a78 давал 4.6 — на грани. Берём темнее: подписи и подсказки
    # должны читаться так же уверенно, как основной текст.
    "text_dim":  "#55545f",
    # На светлом фоне те же цвета выглядят кислотно — берём глубже,
    # чтобы белый текст на них читался (контраст не ниже AA).
    # Цвета текста на светлом фоне: все проверены на контраст >= 4.5
    "accent":    "#a1550a",
    "ok":        "#1f7a3c",
    "warn":      "#7d5a05",
    "danger":    "#a82f36",
    "info":      "#1f5f9e",
    "lead":      "#1f5f9e",
    "on_accent": "#ffffff",
    "is_light":  True,
}

THEMES = {"dark": DARK_PALETTE, "light": LIGHT_PALETTE}
THEME_LABELS = {"dark": "Тёмная", "light": "Светлая", "auto": "Как в системе"}

# Активная палитра. Меняется через apply_theme().
PALETTE = dict(DARK_PALETTE)


def system_prefers_light() -> bool:
    """Смотрим тему Windows. На других системах считаем, что тёмная."""
    if not winreg:
        return False
    try:
        path = r"Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return bool(value)
    except Exception:
        return False


def resolve_theme(name: str) -> str:
    if name == "auto":
        return "light" if system_prefers_light() else "dark"
    return name if name in THEMES else "dark"


def apply_theme(name: str) -> str:
    """Переключает активную палитру. Возвращает применённое имя."""
    global PALETTE
    resolved = resolve_theme(name)
    PALETTE.clear()
    PALETTE.update(THEMES[resolved])
    return resolved


# 8pt-сетка: все отступы кратны четырём. Так вертикальный ритм не плывёт.
SP = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}
RADIUS = "10px"
RADIUS_SM = "8px"
FIELD_H = 36
FIELD_H_SM = 30



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


def parse_hhmm(text: str, fallback=(9, 0)) -> tuple:
    """«21:30» -> (21, 30). Кривой ввод не роняет программу."""
    try:
        hh, mm = str(text).split(":")
        hh, mm = int(hh), int(mm)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except Exception:
        pass
    return fallback


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


def clean_qdt(dt: QDateTime) -> QDateTime:
    """Обнуляет секунды и миллисекунды.

    Поле ввода показывает только «дд.ММ.гггг ЧЧ:мм», а внутри держит ещё
    и секунды от QDateTime.currentDateTime(). Из-за этого напоминание,
    выставленное «на 14:40», срабатывало в 14:40:54 — то есть когда часы
    в трее показывали уже 14:41. Пользователь видел минутное опоздание.
    """
    result = QDateTime(dt)
    t = result.time()
    result.setTime(QTime(t.hour(), t.minute(), 0, 0))
    return result


def now_qdt(add_seconds: int = 0) -> QDateTime:
    """Текущее время без секунд, при желании со сдвигом."""
    return clean_qdt(QDateTime.currentDateTime().addSecs(add_seconds))


def darken_color(hex_color: str, amount: int = 30) -> str:
    hex_color = hex_color.lstrip("#")
    rgb = tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    darkened = tuple(max(0, c - amount) for c in rgb)
    return f"#{darkened[0]:02x}{darkened[1]:02x}{darkened[2]:02x}"


def refresh_theme_assets():
    """Перерисовывает иконки и собирает стиль под текущую палитру."""
    global ARROW_ICON, ARROW_UP_ICON, CHECK_ICON, STYLE
    ARROW_ICON = _arrow_icon_path("down")
    ARROW_UP_ICON = _arrow_icon_path("up")
    CHECK_ICON = _check_icon_path()
    STYLE = build_style()
    return STYLE


def make_fallback_icon() -> QIcon:
    """Рисуем иконку сами, если icon.ico не нашёлся — трей без иконки невидим."""
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QBrush(QColor(PALETTE["accent"])))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(4, 4, 56, 56)
    painter.setPen(QColor(PALETTE["bg"]))
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




def _arrow_icon_path(direction: str = "down") -> str:
    """Рисуем треугольник-стрелку в файл.

    В Qt-стилях картинка надёжнее border-хака: тот на части систем
    показывается квадратом вместо треугольника.
    """
    tone = "l" if PALETTE["text"].lower() < "#888888" else "d"
    path = os.path.join(data_dir(), f"_arrow_{direction}_{tone}.png")
    try:
        if os.path.exists(path):
            return path.replace("\\", "/")
        pix = QPixmap(18, 12)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QBrush(QColor(PALETTE["text"])))
        painter.setPen(Qt.NoPen)
        if direction == "up":
            poly = QPolygon([QPoint(3, 8), QPoint(15, 8), QPoint(9, 2)])
        else:
            poly = QPolygon([QPoint(3, 4), QPoint(15, 4), QPoint(9, 10)])
        painter.drawPolygon(poly)
        painter.end()
        pix.save(path, "PNG")
        return path.replace("\\", "/")
    except Exception:
        return ""


def _check_icon_path() -> str:
    """Галочка внутри отмеченного чекбокса."""
    tone = PALETTE["on_accent"].lstrip("#")
    path = os.path.join(data_dir(), f"_check_{tone}.png")
    try:
        if os.path.exists(path):
            return path.replace("\\", "/")
        pix = QPixmap(18, 18)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(PALETTE["on_accent"]))
        pen.setWidth(3)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline(QPolygon([QPoint(4, 9), QPoint(8, 13), QPoint(14, 5)]))
        painter.end()
        pix.save(path, "PNG")
        return path.replace("\\", "/")
    except Exception:
        return ""


ARROW_ICON = ""
ARROW_UP_ICON = ""
CHECK_ICON = ""

# --- Модель ----------------------------------------------------------------

class RepeatType(Enum):
    ONCE = auto()
    DAILY = auto()
    WEEKLY = auto()
    MONTHLY = auto()
    WEEKDAYS = auto()        # по выбранным дням недели (пн/ср/пт и т.п.)
    INTERVAL = auto()        # каждые N часов/минут внутри окна: «с 9:00 до 21:00»


REPEAT_LABELS = {
    RepeatType.ONCE: "Один раз",
    RepeatType.DAILY: "Каждый день",
    RepeatType.WEEKLY: "Каждую неделю",
    RepeatType.MONTHLY: "Каждый месяц",
    RepeatType.WEEKDAYS: "По дням недели",
    RepeatType.INTERVAL: "Каждые N часов в окне",
}

WEEKDAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def weekdays_label(days: List[int]) -> str:
    """[0,2,4] -> «Пн, Ср, Пт». Частые наборы называем по-человечески."""
    if not days:
        return "дни не выбраны"
    ordered = sorted(set(days))
    if ordered == [0, 1, 2, 3, 4]:
        return "по будням"
    if ordered == [5, 6]:
        return "по выходным"
    if ordered == [0, 1, 2, 3, 4, 5, 6]:
        return "каждый день"
    return ", ".join(WEEKDAY_SHORT[d] for d in ordered)


@dataclass
class Reminder:
    message: str
    time: float                                  # когда сработает
    repeat_type: RepeatType
    original_time: float = field(default=None)   # когда завели (якорь повторов)
    created: float = field(default_factory=time.time)
    fired_at: Optional[float] = None             # когда реально сработало
    weekdays: List[int] = field(default_factory=list)   # 0=Пн … 6=Вс, для WEEKDAYS
    lead_minutes: int = 0                        # предупредить за N минут до
    # График «каждые N минут с ЧЧ:ММ до ЧЧ:ММ» (тип INTERVAL)
    every_minutes: int = 180                     # шаг повтора
    window_start: str = "09:00"                  # начало окна
    window_end: str = "21:00"                    # конец окна
    window_days: List[int] = field(default_factory=list)  # пусто = каждый день
    is_lead: bool = False                        # это само предупреждение
    tag: str = ""                                # метка: Работа, Здоровье, ...
    done_at: Optional[float] = None              # когда отметили выполненным

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
            "weekdays": list(self.weekdays),
            "lead_minutes": self.lead_minutes,
            "is_lead": self.is_lead,
            "tag": self.tag,
            "done_at": self.done_at,
            "every_minutes": self.every_minutes,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "window_days": list(self.window_days),
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
        raw_days = data.get("weekdays") or []
        weekdays = sorted({int(d) for d in raw_days if isinstance(d, (int, float))
                           and 0 <= int(d) <= 6})
        return Reminder(
            message=str(data.get("message", "(без текста)")),
            time=stamp,
            repeat_type=repeat_type,
            original_time=float(data.get("original_time", stamp)),
            created=float(data.get("created", stamp)),
            fired_at=data.get("fired_at"),
            weekdays=weekdays,
            lead_minutes=int(data.get("lead_minutes", 0) or 0),
            is_lead=bool(data.get("is_lead", False)),
            tag=str(data.get("tag", "") or "").strip(),
            done_at=data.get("done_at"),
            every_minutes=int(data.get("every_minutes", 180) or 180),
            window_start=str(data.get("window_start", "09:00")),
            window_end=str(data.get("window_end", "21:00")),
            window_days=sorted({int(d) for d in (data.get("window_days") or [])
                                if isinstance(d, (int, float)) and 0 <= int(d) <= 6}),
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
    "theme": "dark",             # dark / light / auto
    "hotkey_enabled": True,      # глобальная горячая клавиша
    "hotkey": "Ctrl+Alt+N",      # какая именно
    "tags": ["Работа", "Здоровье", "Дом", "Музыка"],
    "presets": [],               # заготовки напоминаний
}


# Цвета меток. Два набора: на тёмном фоне читаются светлые тона,
# на светлом — насыщенные тёмные. Один набор на обе темы не работает:
# на белом фоне светло-зелёный давал контраст 1.8 при норме 4.5.
TAG_COLORS_DARK = ["#5ec26a", "#4aa3f0", "#f0a33c", "#f0688f",
                   "#b57fe0", "#3fc4d6", "#9ed45c", "#f5825a"]
TAG_COLORS_LIGHT = ["#1f7a35", "#125fa8", "#8a5200", "#a81f4d",
                    "#6b2f9e", "#0d6b78", "#4a6b12", "#a83a14"]


def tag_color(tag: str) -> str:
    """Цвет метки под текущую тему. Один и тот же тег — всегда один цвет."""
    if not tag:
        return PALETTE["text_dim"]
    palette = TAG_COLORS_LIGHT if PALETTE.get("is_light") else TAG_COLORS_DARK
    return palette[sum(ord(c) for c in tag) % len(palette)]


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

    ARCHIVE_LIMIT = 200        # больше не храним, файл не должен пухнуть

    def __init__(self, data_path: str, missed_path: str,
                 archive_path: Optional[str] = None):
        super().__init__()
        self.data_path = data_path
        self.missed_path = missed_path
        self.archive_path = archive_path or (
            os.path.join(os.path.dirname(data_path), ARCHIVE_FILENAME))
        self.reminders: List[Reminder] = []
        self.missed_reminders: List[Reminder] = []
        self.archive: List[Reminder] = []
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
        self.archive = self._read_list(self.archive_path)

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
                                       original_time=r.time, fired_at=r.time,
                                       tag=r.tag)
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
        self._write_list(self.archive, self.archive_path)

    # ---- операции ----
    def add(self, message: str, when: datetime, repeat: RepeatType,
            weekdays: Optional[List[int]] = None, lead_minutes: int = 0,
            tag: str = "", every_minutes: int = 180,
            window_start: str = "09:00", window_end: str = "21:00") -> Reminder:
        # Секунды обнуляем и здесь: если время пришло не из формы (трей,
        # тесты, старый файл), напоминание всё равно сработает ровно в минуту.
        when = when.replace(second=0, microsecond=0)
        stamp = when.timestamp()
        r = Reminder(message=message, time=stamp, repeat_type=repeat,
                     original_time=stamp, weekdays=sorted(set(weekdays or [])),
                     lead_minutes=max(0, int(lead_minutes)), tag=(tag or "").strip(),
                     every_minutes=max(1, int(every_minutes)),
                     window_start=window_start, window_end=window_end,
                     window_days=sorted(set(weekdays or [])))
        # Для графика первое срабатывание считаем по сетке окна,
        # а не по выбранной дате: иначе первый сигнал придёт мимо расписания.
        if repeat == RepeatType.INTERVAL:
            r.time = self._next_interval_time(r, time.time())
            r.original_time = r.time
        # Если день недели у WEEKDAYS не совпал с выбранной датой — подвинем
        # на ближайший подходящий, иначе первое срабатывание будет «не в свой день».
        if r.repeat_type == RepeatType.WEEKDAYS and r.weekdays:
            probe = when
            for _ in range(8):
                if probe.weekday() in r.weekdays and probe.timestamp() > time.time():
                    break
                probe += timedelta(days=1)
            r.time = probe.timestamp()
            r.original_time = r.time
        self.reminders.append(r)
        self._sync_lead(r)
        self.reminders.sort(key=lambda x: x.time)
        self.save_all()
        self.changed.emit()
        logging.info("Добавлено: %s на %s", message, datetime.fromtimestamp(r.time))
        return r

    def _sync_lead(self, reminder: Reminder):
        """Создаёт/обновляет отдельное предупреждение «за N минут до»."""
        self.reminders = [x for x in self.reminders
                          if not (x.is_lead and x.message == reminder.message
                                  and abs(x.time + x.lead_minutes * 60 - reminder.time) < 1)]
        if reminder.is_lead or reminder.lead_minutes <= 0:
            return
        lead_time = reminder.time - reminder.lead_minutes * 60
        if lead_time <= time.time():
            return          # предупреждать уже поздно
        warn = Reminder(
            message=f"Через {human_duration(reminder.lead_minutes)}: {reminder.message}",
            time=lead_time, repeat_type=RepeatType.ONCE,
            original_time=lead_time, lead_minutes=reminder.lead_minutes,
            is_lead=True, tag=reminder.tag)
        self.reminders.append(warn)

    def update(self, reminder: Reminder, message: str, when: datetime,
               repeat: RepeatType, weekdays: Optional[List[int]] = None,
               lead_minutes: int = 0, tag: str = "", every_minutes: int = 180,
               window_start: str = "09:00", window_end: str = "21:00"):
        # старое предупреждение убираем до правки, иначе не найдём по времени
        self.reminders = [x for x in self.reminders if not x.is_lead]
        when = when.replace(second=0, microsecond=0)
        reminder.message = message
        reminder.time = when.timestamp()
        reminder.original_time = reminder.time
        reminder.repeat_type = repeat
        reminder.weekdays = sorted(set(weekdays or []))
        reminder.lead_minutes = max(0, int(lead_minutes))
        reminder.tag = (tag or "").strip()
        reminder.every_minutes = max(1, int(every_minutes))
        reminder.window_start = window_start
        reminder.window_end = window_end
        reminder.window_days = sorted(set(weekdays or []))
        if repeat == RepeatType.INTERVAL:
            reminder.time = self._next_interval_time(reminder, time.time())
            reminder.original_time = reminder.time
        # у всех остальных напоминаний предупреждения восстановим
        for other in list(self.reminders):
            if other is not reminder and other.lead_minutes > 0 and not other.is_lead:
                self._sync_lead(other)
        self._sync_lead(reminder)
        self.reminders.sort(key=lambda x: x.time)
        self.save_all()
        self.changed.emit()

    def delete(self, reminder: Reminder):
        if reminder in self.reminders:
            self.reminders.remove(reminder)
            self.save_all()
            self.changed.emit()

    def mark_done(self, reminder: Reminder, to_archive: bool = True):
        """Убирает из «Требуют внимания». По умолчанию — в архив.

        Раньше запись просто стиралась: случайно нажал «Сделано» —
        и напоминание пропало навсегда. Теперь его можно вернуть.
        """
        if reminder not in self.missed_reminders:
            return
        self.missed_reminders.remove(reminder)
        if to_archive:
            reminder.done_at = time.time()
            self.archive.append(reminder)
            self.archive = self.archive[-self.ARCHIVE_LIMIT:]
        self.save_all()
        self.changed.emit()

    def restore(self, reminder: Reminder):
        """Вернуть из архива обратно в «Требуют внимания»."""
        if reminder not in self.archive:
            return
        self.archive.remove(reminder)
        reminder.done_at = None
        self.missed_reminders.append(reminder)
        self.missed_reminders.sort(key=lambda x: x.fired_at or x.time)
        self.save_all()
        self.changed.emit()
        logging.info("Возвращено из архива: %s", reminder.message)

    def clear_archive(self):
        self.archive.clear()
        self.save_all()
        self.changed.emit()

    def clear_missed(self):
        self.missed_reminders.clear()
        self.save_all()
        self.changed.emit()

    def snooze(self, reminder: Reminder, minutes: int) -> Reminder:
        minutes = max(1, int(minutes))          # 0 минут = мгновенный повтор, не даём
        self.mark_done(reminder, to_archive=False)
        new_time = time.time() + minutes * 60
        snoozed = Reminder(message=reminder.message, time=new_time,
                           repeat_type=RepeatType.ONCE, original_time=new_time,
                           tag=reminder.tag)
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

        if reminder.repeat_type == RepeatType.INTERVAL:
            return self._next_interval_time(reminder, now)

        if reminder.repeat_type == RepeatType.WEEKDAYS:
            days = sorted(set(reminder.weekdays))
            if not days:
                return reminder.time      # дни не выбраны — считать нечего
            # Идём вперёд по суткам, сохраняя время из original_time.
            probe = datetime.fromtimestamp(reminder.time).replace(
                hour=base.hour, minute=base.minute,
                second=base.second, microsecond=0)
            for _ in range(1, 400):
                probe += timedelta(days=1)
                if probe.weekday() in days and probe.timestamp() > now:
                    return probe.timestamp()
            return reminder.time

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

    def _next_interval_time(self, reminder: Reminder, now: float) -> float:
        """Следующее срабатывание графика «каждые N минут с ЧЧ:ММ до ЧЧ:ММ».

        Правила:
          * шаги отсчитываются от начала окна, чтобы время не уползало;
          * последний шаг не позже конца окна;
          * окно кончилось — переносим на начало окна следующего разрешённого дня;
          * окно через полночь (с 22:00 до 06:00) считается одним куском.

        Важно: проверяем и вчерашнее окно тоже. Ночное окно 22:00–06:00,
        начавшееся вчера, всё ещё идёт сегодня в 02:00 — без этого
        ночные шаги после полуночи терялись.
        """
        step = max(1, int(reminder.every_minutes or 1))
        sh, sm = parse_hhmm(reminder.window_start, (9, 0))
        eh, em = parse_hhmm(reminder.window_end, (21, 0))
        days = sorted(set(reminder.window_days or []))
        current = datetime.fromtimestamp(now)

        best = None
        # -1 — окно, начавшееся вчера (важно для окон через полночь)
        for offset in range(-1, 400):
            day = (current + timedelta(days=offset)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            start = day.replace(hour=sh, minute=sm)
            end = day.replace(hour=eh, minute=em)
            if end <= start:
                end += timedelta(days=1)      # окно через полночь

            # День недели проверяем по НАЧАЛУ окна: ночная смена
            # с пятницы на субботу считается пятничной.
            if days and start.weekday() not in days:
                continue

            if now < start.timestamp():
                candidate = start
            else:
                passed = (now - start.timestamp()) / 60.0
                index = int(passed // step) + 1
                candidate = start + timedelta(minutes=index * step)

            if candidate.timestamp() > now and candidate <= end:
                best = candidate.timestamp()
                break

        return best if best is not None else reminder.time

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
                                  original_time=fired_time, fired_at=fired_time,
                                  tag=reminder.tag)
                self.missed_reminders.append(record)
                triggered.append(record)
                reminder.time = self.next_time(reminder)
                # для следующего срабатывания заново ставим предупреждение
                if reminder.lead_minutes > 0:
                    self._sync_lead(reminder)

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


def build_style() -> str:
    return f"""
QWidget {{
    background:{PALETTE["bg"]}; color:{PALETTE["text"]};
    font-family:'Segoe UI Variable Display','Segoe UI','Inter',sans-serif;
    font-size:10pt;
}}

/* ---- Карточки: уровень 1 ---- */
QFrame#card {{
    background:{PALETTE["surface"]};
    border:1px solid {PALETTE["line"]};
    border-radius:{RADIUS};
}}
QFrame#inner {{
    background:{PALETTE["surface2"]};
    border:1px solid {PALETTE["line"]};
    border-radius:{RADIUS_SM};
}}

QLabel {{ background:transparent; border:none; padding:0; }}
QLabel#hint  {{ color:{PALETTE["text_dim"]}; font-size:9pt; }}
QLabel#title {{
    color:{PALETTE["text"]}; font-size:12pt; font-weight:600;
}}
QLabel#appname {{
    color:{PALETTE["text"]}; font-size:13pt; font-weight:700;
}}
QLabel#section {{
    color:{PALETTE["text_dim"]}; font-size:8pt; font-weight:700;
    letter-spacing:.09em;
}}

/* ---- Поля ввода: уровень 2 ---- */
QLineEdit, QComboBox, QDateTimeEdit, QSpinBox {{
    background:{PALETTE["surface"]};
    border:1px solid {PALETTE["line"]};
    border-radius:{RADIUS_SM};
    padding:0 {SP["md"]}px;
    min-height:{FIELD_H}px; max-height:{FIELD_H}px;
    selection-background-color:{PALETTE["info"]};
}}
QLineEdit:hover, QComboBox:hover, QDateTimeEdit:hover, QSpinBox:hover {{
    background:{PALETTE["surface3"]};
}}
QFrame#inner QLineEdit, QFrame#inner QComboBox,
QFrame#inner QDateTimeEdit, QFrame#inner QSpinBox {{
    background:{PALETTE["surface"]};
}}
QLineEdit:focus, QComboBox:focus, QDateTimeEdit:focus, QSpinBox:focus {{
    border-color:{PALETTE["accent"]}; background:{PALETTE["surface3"]};
}}
QLineEdit#big {{ font-size:11pt; min-height:44px; max-height:44px; }}

QComboBox::drop-down, QDateTimeEdit::drop-down {{
    subcontrol-origin:padding; subcontrol-position:center right;
    width:28px; border:none; background:transparent;
}}
QComboBox::down-arrow, QDateTimeEdit::down-arrow {{
    image:url("{ARROW_ICON}"); width:10px; height:7px;
}}
QComboBox QAbstractItemView {{
    background:{PALETTE["surface3"]};
    border:1px solid {PALETTE["line"]};
    border-radius:{RADIUS_SM};
    selection-background-color:{PALETTE["accent"]};
    selection-color:{PALETTE["on_accent"]};
    outline:none; padding:{SP["xs"]}px;
}}
QComboBox QAbstractItemView::item {{
    min-height:32px; padding:0 {SP["sm"]}px; border-radius:6px;
}}

QSpinBox::up-button, QSpinBox::down-button {{
    width:20px; border:none; background:transparent;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background:{PALETTE["line"]}; border-radius:4px;
}}
QSpinBox::up-arrow {{ image:url("{ARROW_UP_ICON}"); width:9px; height:6px; }}
QSpinBox::down-arrow {{ image:url("{ARROW_ICON}"); width:9px; height:6px; }}

/* ---- Кнопки ---- */
QPushButton {{
    background:{PALETTE["surface2"]};
    color:{PALETTE["text"]};
    /* на светлой теме нужна чуть заметная граница, иначе кнопка сливается */
    border:1px solid {PALETTE["line"]};
    border-radius:{RADIUS_SM};
    padding:0 {SP["lg"]}px;
    min-height:{FIELD_H}px;
    font-weight:600;
}}
QPushButton:hover  {{ background:{PALETTE["surface3"]}; border-color:#43414f; }}
QPushButton:pressed{{ background:{PALETTE["line_soft"]}; }}
QPushButton:disabled {{
    background:{PALETTE["surface"]}; color:#5c5a68; border-color:{PALETTE["line_soft"]};
}}
QPushButton#chip {{
    min-height:{FIELD_H_SM}px; padding:0 {SP["md"]}px;
    font-size:9pt; font-weight:600; border-radius:{FIELD_H_SM // 2}px;
}}
QPushButton#ghost {{
    background:transparent; border:none; color:{PALETTE["text_dim"]};
    padding:0 {SP["sm"]}px; min-height:{FIELD_H_SM}px; text-align:left;
}}
QPushButton#ghost:hover {{ color:{PALETTE["accent"]}; background:transparent; }}
QPushButton#icon {{
    background:transparent; border:none; padding:0;
    min-width:{FIELD_H}px; max-width:{FIELD_H}px;
    min-height:{FIELD_H}px; max-height:{FIELD_H}px;
    border-radius:{RADIUS_SM}; font-size:14pt; color:{PALETTE["text_dim"]};
}}
QPushButton#icon:hover {{ background:{PALETTE["surface2"]}; color:{PALETTE["text"]}; }}

/* ---- Списки ---- */
QListWidget {{
    background:transparent; border:none; outline:none;
    padding:{SP["xs"]}px 0;
}}
QListWidget::item {{
    padding:{SP["sm"]}px {SP["md"]}px; border-radius:{RADIUS_SM};
    margin:1px {SP["xs"]}px;
}}
QListWidget::item:hover {{ background:{PALETTE["surface2"]}; }}
QListWidget::item:selected {{
    background:{PALETTE["surface3"]}; color:{PALETTE["text"]};
    border:1px solid {PALETTE["accent"]};
}}

/* ---- Вкладки: подчёркивание вместо «папок» ---- */
QTabWidget::pane {{ border:none; background:transparent; top:0; }}
QTabBar {{ qproperty-drawBase:0; }}
QTabBar::tab {{
    background:transparent; color:{PALETTE["text_dim"]};
    padding:{SP["sm"]}px {SP["md"]}px; margin-right:{SP["xs"]}px;
    border:none; border-bottom:2px solid transparent; font-weight:600;
}}
QTabBar::tab:hover {{ color:{PALETTE["text"]}; }}
QTabBar::tab:selected {{
    color:{PALETTE["text"]}; border-bottom-color:{PALETTE["accent"]};
}}

/* ---- Переключатели ---- */
QCheckBox, QRadioButton {{ background:transparent; spacing:{SP["sm"]}px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width:18px; height:18px; }}
QCheckBox::indicator {{
    border:1px solid #56545f; border-radius:5px; background:{PALETTE["surface2"]};
}}
QCheckBox::indicator:hover {{ border-color:{PALETTE["accent"]}; }}
QCheckBox::indicator:checked {{
    border-color:{PALETTE["accent"]}; background:{PALETTE["accent"]};
    image:url("{CHECK_ICON}");
}}
QCheckBox::indicator:disabled {{ border-color:#3a3844; background:{PALETTE["surface"]}; }}
QRadioButton::indicator {{
    border:1px solid #56545f; border-radius:9px; background:{PALETTE["surface2"]};
}}
QRadioButton::indicator:hover {{ border-color:{PALETTE["accent"]}; }}
QRadioButton::indicator:checked {{
    border:5px solid {PALETTE["accent"]}; background:{PALETTE["bg"]};
}}

/* ---- Прокрутка ---- */
QScrollBar:vertical {{ background:transparent; width:10px; margin:0; }}
QScrollBar::handle:vertical {{
    background:{PALETTE["line"]}; border-radius:5px; min-height:32px;
}}
QScrollBar::handle:vertical:hover {{ background:#45434f; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background:transparent; }}

/* ---- Меню ---- */
QMenu {{
    background:{PALETTE["surface3"]}; border:1px solid {PALETTE["line"]};
    border-radius:{RADIUS_SM}; padding:{SP["xs"]}px;
}}
QMenu::item {{
    padding:{SP["sm"]}px {SP["xl"]}px {SP["sm"]}px {SP["md"]}px;
    border-radius:6px; min-width:170px;
}}
QMenu::item:selected {{ background:{PALETTE["accent"]}; color:{PALETTE["on_accent"]}; }}
QMenu::separator {{
    height:1px; background:{PALETTE["line"]}; margin:{SP["xs"]}px {SP["sm"]}px;
}}

QToolTip {{
    background:{PALETTE["surface3"]}; color:{PALETTE["text"]};
    border:1px solid {PALETTE["line"]}; border-radius:6px;
    padding:{SP["sm"]}px {SP["md"]}px;
}}

QDialog {{ background:{PALETTE["bg"]}; }}
"""


STYLE = ""



def accent_button(text: str, role: str = "accent", big: bool = False) -> QPushButton:
    """Кнопка смыслового цвета: accent, ok, warn, danger, info."""
    color = PALETTE.get(role, PALETTE["accent"])
    fg = PALETTE["on_accent"] if role in ("warn", "accent") else "#ffffff"
    height = 46 if big else FIELD_H
    size = "11pt" if big else "10pt"
    btn = QPushButton(text)
    btn.setProperty("accentRole", role)
    btn.setProperty("accentBig", big)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setStyleSheet(f"""
        QPushButton {{
            background:{color}; color:{fg}; border:none;
            border-radius:{RADIUS_SM};
            padding:0 {SP["lg"]}px;
            min-height:{height}px; max-height:{height}px;
            font-size:{size}; font-weight:700;
        }}
        QPushButton:hover  {{ background:{darken_color(color, 14)}; }}
        QPushButton:pressed{{ background:{darken_color(color, 28)}; }}
        QPushButton:disabled {{ background:#2c2b33; color:#5c5a68; }}
    """)
    return btn


def wrap_list(widget: QListWidget) -> QFrame:
    """Кладёт список на «утопленную» поверхность.

    Своя рамка у QListWidget конфликтовала с рамкой карточки — получалась
    двойная линия. Теперь фон рисует обёртка, а список прозрачный.
    """
    frame = QFrame()
    frame.setObjectName("inner")
    box = QVBoxLayout(frame)
    box.setContentsMargins(SP["xs"], SP["xs"], SP["xs"], SP["xs"])
    box.addWidget(widget)
    return frame


def tame_dialog_buttons(dialog: QDialog, default_button=None):
    """Отключает autoDefault у всех кнопок диалога.

    В QDialog кнопки по умолчанию «автоглавные»: Enter в любом поле нажимает
    ПЕРВУЮ из них. В настройках первой была «Выбрать…» — поэтому при попытке
    подтвердить ввод раз за разом открывался проводник выбора звука.
    """
    for btn in dialog.findChildren(QPushButton):
        btn.setAutoDefault(False)
        btn.setDefault(False)
    if default_button is not None:
        default_button.setAutoDefault(True)
        default_button.setDefault(True)


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
        line.setStyleSheet(f'color:{PALETTE["line"]};')
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
        self.ok_btn = accent_button("Отложить", "ok")
        self.ok_btn.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addStretch(1)
        buttons.addWidget(self.ok_btn)
        root.addLayout(buttons)
        tame_dialog_buttons(self, self.ok_btn)

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
            self.preview.setStyleSheet(f'color:{PALETTE["danger"]};')
            self.ok_btn.setEnabled(False)
            return
        when = datetime.now() + timedelta(minutes=minutes)
        self.preview.setText(f"Напомню в {when:%H:%M} — это {human_duration(minutes)}")
        self.preview.setStyleSheet(f'color:{PALETTE["text_dim"]};')
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
        self.header.setStyleSheet(f'color:{PALETTE["accent"]}; font-size:13pt; font-weight:700;')
        root.addWidget(self.header)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setMinimumHeight(120)
        root.addWidget(self.list)

        row = QHBoxLayout()
        self.done_btn = accent_button("Сделано", "ok")
        self.done_btn.clicked.connect(self._done)
        self.snooze_btn = accent_button(f"Отложить {self.default_snooze} мин", "warn")
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
        self.when = QDateTimeEdit(now_qdt(900))
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
        self.ok_btn = accent_button("Создать", "accent")
        self.ok_btn.clicked.connect(self._accept)
        buttons.addWidget(cancel)
        buttons.addStretch(1)
        buttons.addWidget(self.ok_btn)
        root.addLayout(buttons)

        tame_dialog_buttons(self, self.ok_btn)
        self.message.returnPressed.connect(self._accept)
        self.when.dateTimeChanged.connect(self._refresh)
        self._refresh()
        self.message.setFocus()

    def _shift(self, minutes: int):
        self.when.setDateTime(now_qdt(minutes * 60))

    def _refresh(self):
        target = self.when.dateTime().toPython()
        if target <= datetime.now():
            self.preview.setText("Время уже прошло")
            self.preview.setStyleSheet(f'color:{PALETTE["danger"]};')
        else:
            self.preview.setText("Сработает " + human_until(target))
            self.preview.setStyleSheet(f'color:{PALETTE["text_dim"]};')

    def _accept(self):
        if not self.message.text().strip():
            self.preview.setText("Введи текст напоминания")
            self.preview.setStyleSheet(f'color:{PALETTE["danger"]};')
            return
        if self.when.dateTime().toPython() <= datetime.now():
            self.preview.setText("Укажи время в будущем")
            self.preview.setStyleSheet(f'color:{PALETTE["danger"]};')
            return
        self.accept()

    def result_data(self):
        return (self.message.text().strip(),
                self.when.dateTime().toPython(),
                self.repeat.currentData())


# --- Глобальная горячая клавиша --------------------------------------------

# Модификаторы WinAPI для RegisterHotKey
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
WM_HOTKEY = 0x0312
HOTKEY_ID = 0xA17E          # любое число, лишь бы не пересекалось в процессе

# Клавиши, которые предлагаем выбрать. Значение — виртуальный код Windows.
HOTKEY_KEYS = {
    "N": 0x4E, "R": 0x52, "T": 0x54, "A": 0x41, "Q": 0x51, "Z": 0x5A,
    "Пробел": 0x20,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}


def parse_hotkey(text: str):
    """«Ctrl+Alt+N» -> (модификаторы, код клавиши). None, если не разобрали."""
    if not text:
        return None
    mods, key_code = 0, None
    for part in [p.strip() for p in text.split("+") if p.strip()]:
        low = part.lower()
        if low in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif low == "alt":
            mods |= MOD_ALT
        elif low == "shift":
            mods |= MOD_SHIFT
        elif low in ("win", "windows"):
            mods |= MOD_WIN
        else:
            for name, code in HOTKEY_KEYS.items():
                if name.lower() == low:
                    key_code = code
                    break
    if key_code is None or mods == 0:
        return None            # без модификатора система перехватит всю клавишу
    return mods, key_code


class GlobalHotkey(QObject, QAbstractNativeEventFilter):
    """Ctrl+Alt+N работает, даже когда окно свёрнуто.

    Внутри — WinAPI RegisterHotKey: Qt своего кроссплатформенного способа
    не даёт. На не-Windows просто ничего не делает.
    """
    activated = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._registered = False
        self._user32 = None
        if IS_WINDOWS:
            try:
                import ctypes
                self._user32 = ctypes.windll.user32
            except Exception as exc:
                logging.error("WinAPI недоступен: %s", exc)

    @property
    def available(self) -> bool:
        return bool(self._user32)

    def register(self, combo: str) -> bool:
        self.unregister()
        if not self._user32:
            return False
        parsed = parse_hotkey(combo)
        if not parsed:
            logging.warning("Не разобрал сочетание: %s", combo)
            return False
        mods, key = parsed
        try:                       # без argtypes на x64 бывает мусор в аргументах
            import ctypes
            from ctypes import wintypes
            self._user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                                    wintypes.UINT, wintypes.UINT]
            self._user32.RegisterHotKey.restype = wintypes.BOOL
            self._user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        except Exception:
            pass
        ok = bool(self._user32.RegisterHotKey(None, HOTKEY_ID,
                                              mods | MOD_NOREPEAT, key))
        if not ok:
            try:
                import ctypes
                logging.warning("RegisterHotKey код ошибки: %s",
                                ctypes.GetLastError())
            except Exception:
                pass
        if ok:
            self._registered = True
            QApplication.instance().installNativeEventFilter(self)
            logging.info("Горячая клавиша %s зарегистрирована", combo)
        else:
            logging.warning("Горячая клавиша %s занята другой программой", combo)
        return ok

    def unregister(self):
        if self._registered and self._user32:
            try:
                self._user32.UnregisterHotKey(None, HOTKEY_ID)
                QApplication.instance().removeNativeEventFilter(self)
            except Exception:
                pass
        self._registered = False

    @staticmethod
    def _msg_address(message) -> int:
        """Адрес структуры MSG из того, что отдал PySide6.

        Тут была причина, по которой Ctrl+Alt+N не работал: PySide6 передаёт
        не число, а PyCapsule. int(message) на нём падает, исключение
        глоталось — и клавиша молчала. Пробуем все известные формы.
        """
        try:                                  # sip.voidptr / int
            return int(message)
        except Exception:
            pass
        try:                                  # PyCapsule
            import ctypes
            ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
            ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object,
                                                              ctypes.c_char_p]
            return int(ctypes.pythonapi.PyCapsule_GetPointer(message, None))
        except Exception:
            pass
        try:
            return int(message.__int__())
        except Exception:
            return 0

    def nativeEventFilter(self, event_type, message):
        if event_type not in (b"windows_dispatcher_MSG", b"windows_generic_MSG"):
            return False, 0
        try:
            import ctypes
            from ctypes import wintypes

            class MSG(ctypes.Structure):
                _fields_ = [("hwnd", wintypes.HWND),
                            ("message", wintypes.UINT),
                            ("wParam", ctypes.c_void_p),
                            ("lParam", ctypes.c_void_p),
                            ("time", wintypes.DWORD),
                            ("pt_x", wintypes.LONG),
                            ("pt_y", wintypes.LONG)]

            address = self._msg_address(message)
            if not address:
                return False, 0
            msg = MSG.from_address(address)
            if msg.message == WM_HOTKEY and int(msg.wParam or 0) == HOTKEY_ID:
                self.activated.emit()
                return True, 0
        except Exception as exc:
            logging.debug("Разбор WM_HOTKEY не удался: %s", exc)
        return False, 0


# --- Заготовки --------------------------------------------------------------

class PresetSaveDialog(QDialog):
    """Как назвать заготовку и как запоминать время."""

    def __init__(self, parent=None, default_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Сохранить заготовку")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setMinimumWidth(430)
        self._build(default_name)

    def _build(self, default_name: str):
        root = QVBoxLayout(self)
        root.setSpacing(SP["md"])
        root.setContentsMargins(SP["xl"], SP["xl"], SP["xl"], SP["xl"])

        title = QLabel("Новая заготовка")
        title.setObjectName("title")
        root.addWidget(title)

        name_label = QLabel("НАЗВАНИЕ")
        name_label.setObjectName("section")
        root.addWidget(name_label)

        self.name = QLineEdit(default_name)
        self.name.setPlaceholderText("Например: Репетиция")
        root.addWidget(self.name)

        time_label = QLabel("КАК ЗАПОМНИТЬ ВРЕМЯ")
        time_label.setObjectName("section")
        root.addWidget(time_label)

        # Варианты — карточками с пояснением, а не голыми радиокнопками:
        # так сразу видно, чем они отличаются.
        self.group = QButtonGroup(self)
        self.offset_radio = self._option(
            root, "Через столько же от «сейчас»",
            "Удобно для «через 15 минут», «через час»")
        self.time_radio = self._option(
            root, "В это же время суток",
            "Удобно для «каждый день в 19:00»")
        self.offset_radio.setChecked(True)
        self._sync_options()
        self.group.buttonToggled.connect(lambda *_: self._sync_options())

        root.addSpacing(SP["xs"])
        buttons = QHBoxLayout()
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        ok = accent_button("Сохранить", "ok")
        ok.clicked.connect(self._accept)
        buttons.addWidget(cancel)
        buttons.addStretch(1)
        buttons.addWidget(ok)
        root.addLayout(buttons)

        tame_dialog_buttons(self, ok)
        self.name.setFocus()
        self.name.returnPressed.connect(self._accept)

    def _option(self, root: QVBoxLayout, title: str, hint: str) -> QRadioButton:
        """Один вариант выбора: карточка с заголовком и пояснением."""
        frame = QFrame()
        frame.setObjectName("inner")
        box = QVBoxLayout(frame)
        box.setContentsMargins(SP["md"], SP["sm"], SP["md"], SP["sm"])
        box.setSpacing(2)

        radio = QRadioButton(title)
        radio.setCursor(Qt.PointingHandCursor)
        self.group.addButton(radio)
        box.addWidget(radio)

        note = QLabel(hint)
        note.setObjectName("hint")
        note.setContentsMargins(26, 0, 0, 0)   # под текстом радиокнопки
        box.addWidget(note)

        # Клик по всей карточке выбирает вариант — попадать проще
        frame.mousePressEvent = lambda _e, r=radio: r.setChecked(True)
        frame.setCursor(Qt.PointingHandCursor)
        root.addWidget(frame)
        radio._frame = frame
        return radio

    def _sync_options(self):
        """Подсвечивает выбранную карточку рамкой."""
        for button in self.group.buttons():
            frame = getattr(button, "_frame", None)
            if not frame:
                continue
            if button.isChecked():
                frame.setStyleSheet(
                    f"QFrame#inner{{border:1px solid {PALETTE['accent']};"
                    f"background:{PALETTE['surface2']};}}")
            else:
                frame.setStyleSheet("")

    def _accept(self):
        if not self.name.text().strip():
            self.name.setFocus()
            return
        self.accept()

    def result_data(self):
        mode = "offset" if self.offset_radio.isChecked() else "at_time"
        return self.name.text().strip(), mode


class PresetManageDialog(QDialog):
    """Список заготовок: посмотреть и удалить лишние."""

    def __init__(self, parent, settings: "Settings"):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Заготовки")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setMinimumWidth(460)
        self._build()
        self._reload()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Сохранённые заготовки")
        title.setObjectName("title")
        root.addWidget(title)

        self.list = QListWidget()
        self.list.setMinimumHeight(200)
        root.addWidget(self.list)

        row = QHBoxLayout()
        remove = QPushButton("Удалить выбранную")
        remove.clicked.connect(self._remove)
        clear = QPushButton("Удалить все")
        clear.clicked.connect(self._clear)
        close = accent_button("Закрыть", "ok")
        close.clicked.connect(self.accept)
        row.addWidget(remove)
        row.addWidget(clear)
        row.addStretch(1)
        row.addWidget(close)
        root.addLayout(row)
        tame_dialog_buttons(self, close)

    def _reload(self):
        self.list.clear()
        presets = self.settings["presets"]
        if not presets:
            item = QListWidgetItem("Заготовок пока нет")
            item.setFlags(Qt.NoItemFlags)
            self.list.addItem(item)
            return
        for index, preset in enumerate(presets):
            bits = [preset.get("message", "")]
            if preset.get("tag"):
                bits.append(f"[{preset['tag']}]")
            if preset.get("offset_minutes"):
                bits.append(f"через {human_duration(preset['offset_minutes'])}")
            elif preset.get("at_time"):
                bits.append(f"в {preset['at_time']}")
            repeat = preset.get("repeat", "ONCE")
            if repeat != "ONCE":
                bits.append(REPEAT_LABELS[RepeatType[repeat]].lower())
            item = QListWidgetItem(f"{preset.get('name','')} — " + ", ".join(b for b in bits if b))
            item.setData(Qt.UserRole, index)
            self.list.addItem(item)

    def _remove(self):
        item = self.list.currentItem()
        if not item or item.data(Qt.UserRole) is None:
            return
        presets = list(self.settings["presets"])
        try:
            presets.pop(int(item.data(Qt.UserRole)))
        except (IndexError, ValueError, TypeError):
            return
        self.settings["presets"] = presets
        self.settings.save()
        self._reload()

    def _clear(self):
        if not self.settings["presets"]:
            return
        if QMessageBox.question(self, "Удалить все",
                                "Удалить все заготовки?") == QMessageBox.Yes:
            self.settings["presets"] = []
            self.settings.save()
            self._reload()


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

        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("Оформление:"))
        self.theme_combo = QComboBox()
        for key in ("dark", "light", "auto"):
            self.theme_combo.addItem(THEME_LABELS[key], key)
        pos = self.theme_combo.findData(self.settings["theme"])
        self.theme_combo.setCurrentIndex(pos if pos >= 0 else 0)
        theme_row.addWidget(self.theme_combo, 1)
        theme_row.addStretch(1)
        root.addLayout(theme_row)

        line0 = QFrame()
        line0.setFrameShape(QFrame.HLine)
        line0.setStyleSheet(f'color:{PALETTE["line"]};')
        root.addWidget(line0)

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
        line.setStyleSheet(f'color:{PALETTE["line"]};')
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
        # Пустое поле выглядело как «звука нет». Показываем, что по умолчанию
        # играет файл из комплекта, и даём его послушать.
        self.sound_path = QLineEdit(self.settings["sound_path"])
        self.sound_path.setPlaceholderText(f"По умолчанию: {SOUND_FILE}")
        self.sound_path.setReadOnly(True)
        browse = QPushButton("Выбрать…")
        browse.clicked.connect(self._pick_sound)
        play = QPushButton("▶")
        play.setToolTip("Прослушать")
        play.setMaximumWidth(44)
        play.clicked.connect(self._play_sound)
        clear = QPushButton("Сброс")
        clear.setToolTip(f"Вернуть звук из комплекта ({SOUND_FILE})")
        clear.clicked.connect(lambda: self.sound_path.clear())
        sound_row.addWidget(self.sound_path, 1)
        sound_row.addWidget(play)
        sound_row.addWidget(browse)
        sound_row.addWidget(clear)
        root.addLayout(sound_row)

        line3 = QFrame()
        line3.setFrameShape(QFrame.HLine)
        line3.setStyleSheet(f'color:{PALETTE["line"]};')
        root.addWidget(line3)

        self.hotkey_check = QCheckBox("Горячая клавиша для быстрого создания")
        self.hotkey_check.setChecked(self.settings["hotkey_enabled"])
        root.addWidget(self.hotkey_check)

        hk_row = QHBoxLayout()
        hk_row.addWidget(QLabel("Сочетание:"))
        self.hk_ctrl = QCheckBox("Ctrl")
        self.hk_alt = QCheckBox("Alt")
        self.hk_shift = QCheckBox("Shift")
        self.hk_key = QComboBox()
        for name in HOTKEY_KEYS:
            self.hk_key.addItem(name)
        parsed = [x.strip().lower() for x in str(self.settings["hotkey"]).split("+")]
        self.hk_ctrl.setChecked("ctrl" in parsed)
        self.hk_alt.setChecked("alt" in parsed)
        self.hk_shift.setChecked("shift" in parsed)
        for name in HOTKEY_KEYS:
            if name.lower() in parsed:
                self.hk_key.setCurrentText(name)
                break
        hk_row.addWidget(self.hk_ctrl)
        hk_row.addWidget(self.hk_alt)
        hk_row.addWidget(self.hk_shift)
        hk_row.addWidget(self.hk_key)
        hk_row.addStretch(1)
        root.addLayout(hk_row)

        hk_hint = QLabel("Работает даже когда окно свёрнуто. "
                         "Нужен хотя бы один модификатор.")
        hk_hint.setObjectName("hint")
        hk_hint.setWordWrap(True)
        root.addWidget(hk_hint)

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
        line2.setStyleSheet(f'color:{PALETTE["line"]};')
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
        save = accent_button("Сохранить", "ok")
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addStretch(1)
        buttons.addWidget(save)
        root.addLayout(buttons)
        tame_dialog_buttons(self, save)

    def _pick_sound(self):
        start = os.path.dirname(self.sound_path.text()) or data_dir()
        path, _ = QFileDialog.getOpenFileName(self, "Выбери звук", start,
                                              "Звуки WAV (*.wav)")
        if path:
            self.sound_path.setText(path)

    def _play_sound(self):
        """Проверить, как звучит выбранный файл."""
        path = self.sound_path.text().strip() or resource_path(SOUND_FILE)
        if not os.path.exists(path):
            return
        if winsound:
            try:
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception as exc:
                logging.error("Не удалось проиграть звук: %s", exc)

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
        self.settings["theme"] = self.theme_combo.currentData()

        mods = []
        if self.hk_ctrl.isChecked():
            mods.append("Ctrl")
        if self.hk_alt.isChecked():
            mods.append("Alt")
        if self.hk_shift.isChecked():
            mods.append("Shift")
        if mods:
            self.settings["hotkey"] = "+".join(mods + [self.hk_key.currentText()])
            self.settings["hotkey_enabled"] = self.hotkey_check.isChecked()
        else:
            # без модификатора система отдала бы нам всю клавишу целиком
            self.settings["hotkey_enabled"] = False
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
                                       data_file(MISSED_FILENAME),
                                       data_file(ARCHIVE_FILENAME))
        self.startup = WindowsStartupManager(REG_VALUE_NAME, REG_KEY_PATH)
        self.sound_path = resource_path(SOUND_FILE)
        self.editing: Optional[Reminder] = None
        self._force_quit = False

        self.setWindowTitle(f"{APP_TITLE} {APP_VERSION}")
        self.setWindowIcon(app_icon())
        self.setStyleSheet(STYLE)
        self.resize(580, 720)
        self.setMinimumSize(480, 560)

        self.notification = NotificationWindow(None, int(self.settings["default_snooze"]))
        self.notification.setStyleSheet(QApplication.instance().styleSheet() or STYLE)
        self.notification.done_requested.connect(self._notification_done)
        self.notification.snooze_requested.connect(self._notification_snooze)

        self._build_ui()
        self._build_tray()
        self._build_hotkeys()
        self.manager.changed.connect(self.refresh_lists)
        self.refresh_lists()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.on_tick)
        self.timer.start(TIMER_INTERVAL)
        QTimer.singleShot(0, self._sync_form_height)

    # ---------- интерфейс ----------
    def _build_ui(self):
        # Всё содержимое кладём в прокрутку: если окно не помещается
        # на экран, появится полоса, а не наложение виджетов.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setCentralWidget(self._scroll)

        central = QWidget()
        self._page = central
        self._scroll.setWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(SP["md"])
        root.setContentsMargins(SP["lg"], SP["md"], SP["lg"], SP["md"])

        root.addWidget(self._header())
        root.addWidget(self._form_card())
        root.addWidget(self._tabs(), 1)

        # Всплывающая плашка вместо постоянной строки состояния
        self.status = QLabel("", self)
        self.status.setObjectName("toast")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignCenter)
        self.status.hide()
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._hide_status)

    def _header(self) -> QWidget:
        """Шапка: название и шестерёнка настроек.

        Раньше настройки жили только в трее — до них нельзя было добраться
        из окна, что противоречит ожиданиям: люди ищут их в самом окне.
        """
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(SP["xs"], 0, 0, 0)
        row.setSpacing(SP["sm"])

        title = QLabel(APP_TITLE)
        title.setObjectName("appname")
        row.addWidget(title)
        row.addStretch(1)

        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setObjectName("icon")
        self.settings_btn.setToolTip("Настройки  (Ctrl+,)")
        self.settings_btn.setCursor(Qt.PointingHandCursor)
        self.settings_btn.clicked.connect(self.open_settings)
        row.addWidget(self.settings_btn)

        self.hide_btn = QPushButton("—")
        self.hide_btn.setObjectName("icon")
        self.hide_btn.setToolTip("Свернуть в трей")
        self.hide_btn.setCursor(Qt.PointingHandCursor)
        self.hide_btn.clicked.connect(self.hide)
        row.addWidget(self.hide_btn)
        return bar

    def _form_card(self) -> QFrame:
        """Форма создания.

        Сверху только «что» и «когда». Повтор, метка, предупреждение
        и заготовки скрыты за «Подробнее»: ими пользуются редко,
        а места на экране они занимают много.
        """
        frame = card()
        # Карточка не должна сжиматься ниже нужной высоты — иначе
        # содержимое налезает друг на друга, как было в архиве.
        frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout = QVBoxLayout(frame)
        layout.setSpacing(SP["sm"])
        layout.setContentsMargins(SP["lg"], SP["lg"], SP["lg"], SP["lg"])

        head = QHBoxLayout()
        head.setSpacing(SP["sm"])
        self.form_title = QLabel("Новое напоминание")
        self.form_title.setObjectName("title")
        head.addWidget(self.form_title)
        head.addStretch(1)
        self.cancel_edit_btn = QPushButton("Отмена")
        self.cancel_edit_btn.setObjectName("chip")
        self.cancel_edit_btn.clicked.connect(self.stop_editing)
        self.cancel_edit_btn.hide()
        head.addWidget(self.cancel_edit_btn)
        layout.addLayout(head)

        self.message_input = QLineEdit()
        self.message_input.setObjectName("big")
        self.message_input.setPlaceholderText("О чём напомнить?")
        self.message_input.returnPressed.connect(self.submit)
        layout.addWidget(self.message_input)

        # Быстрый выбор времени — «чипсы»
        chips = QHBoxLayout()
        chips.setSpacing(SP["sm"])
        for text, handler in (("+15 мин", lambda: self._shift(15)),
                              ("+1 час", lambda: self._shift(60)),
                              ("Вечером", self._this_evening),
                              ("Завтра", self._tomorrow_morning)):
            btn = QPushButton(text)
            btn.setObjectName("chip")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(handler)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            chips.addWidget(btn)
        layout.addLayout(chips)

        self.datetime_input = QDateTimeEdit(now_qdt(900))
        self.datetime_input.setCalendarPopup(True)
        self.datetime_input.setDisplayFormat("dd.MM.yyyy    HH:mm")
        self.datetime_input.dateTimeChanged.connect(self._update_preview)
        layout.addWidget(self.datetime_input)

        self.more_btn = QPushButton("  Подробнее")
        self.more_btn.setObjectName("ghost")
        self.more_btn.setCheckable(True)
        self.more_btn.setCursor(Qt.PointingHandCursor)
        # Иконкой, а не символом ▾: текстовый треугольник рисуется
        # шрифтом и на части систем «плывёт» по нижней кромке.
        self.more_btn.setIcon(QIcon(ARROW_ICON))
        self.more_btn.setIconSize(QSize(10, 7))
        self.more_btn.toggled.connect(self._toggle_more)
        layout.addWidget(self.more_btn, 0, Qt.AlignLeft)

        # ---- Скрытая часть: сетка, а не мешанина из QHBoxLayout ----
        # Отдельная «утопленная» панель: раньше поля висели прямо на карточке
        # и выглядели впихнутыми. Своя поверхность + воздух вокруг.
        self.more_box = QFrame()
        self.more_box.setObjectName("inner")
        grid = QGridLayout(self.more_box)
        grid.setContentsMargins(SP["md"], SP["md"], SP["md"], SP["md"])
        grid.setHorizontalSpacing(SP["md"])
        grid.setVerticalSpacing(SP["md"])
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setColumnMinimumWidth(0, 74)
        grid.setColumnMinimumWidth(2, 60)

        def label(text: str) -> QLabel:
            lab = QLabel(text)
            lab.setObjectName("hint")
            lab.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            return lab

        grid.addWidget(label("Повтор"), 0, 0)
        self.repeat_combo = QComboBox()
        for rt in RepeatType:
            self.repeat_combo.addItem(REPEAT_LABELS[rt], rt)
        self.repeat_combo.currentIndexChanged.connect(self._repeat_changed)
        grid.addWidget(self.repeat_combo, 0, 1)

        grid.addWidget(label("Метка"), 0, 2)
        self.tag_combo = QComboBox()
        self.tag_combo.setEditable(True)
        self.tag_combo.lineEdit().setPlaceholderText("нет")
        self._reload_tags()
        grid.addWidget(self.tag_combo, 0, 3)

        # Дни недели — отдельной строкой на всю ширину
        self.weekday_row = QWidget()
        wd = QHBoxLayout(self.weekday_row)
        wd.setContentsMargins(0, 0, 0, 0)
        wd.setSpacing(SP["xs"])
        self.weekday_boxes: List[QCheckBox] = []
        for index, name in enumerate(WEEKDAY_SHORT):
            box = QCheckBox(name)
            box.setToolTip(("Понедельник", "Вторник", "Среда", "Четверг",
                            "Пятница", "Суббота", "Воскресенье")[index])
            box.stateChanged.connect(self._update_preview)
            self.weekday_boxes.append(box)
            wd.addWidget(box)
        wd.addStretch(1)
        for text, days, tip in (("Будни", [0, 1, 2, 3, 4], "Пн–Пт"),
                                ("Вых", [5, 6], "Сб и Вс")):
            btn = QPushButton(text)
            btn.setObjectName("chip")
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _=False, d=days: self._set_weekdays(d))
            wd.addWidget(btn)
        self.weekday_row.hide()
        grid.addWidget(self.weekday_row, 1, 0, 1, 4)

        # Строка графика: «каждые N часов с ЧЧ:ММ до ЧЧ:ММ».
        # Показывается только для режима «Каждые N часов в окне».
        self.interval_row = QWidget()
        iv = QGridLayout(self.interval_row)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.setHorizontalSpacing(SP["sm"])
        iv.setVerticalSpacing(SP["sm"])
        iv.setColumnStretch(1, 1)
        iv.setColumnStretch(3, 1)

        every_label = QLabel("Каждые")
        every_label.setObjectName("hint")
        iv.addWidget(every_label, 0, 0)

        # Интервал: поле + быстрые кнопки рядом. Набирать «180» руками неудобно,
        # но и пять кнопок в одну строку с полями времени не помещаются.
        every_box = QWidget()
        eb = QHBoxLayout(every_box)
        eb.setContentsMargins(0, 0, 0, 0)
        eb.setSpacing(SP["xs"])
        self.every_spin = QSpinBox()
        self.every_spin.setRange(1, 24 * 60)
        self.every_spin.setValue(180)
        self.every_spin.setSuffix(" мин")
        self.every_spin.setMinimumWidth(96)
        self.every_spin.valueChanged.connect(self._update_preview)
        eb.addWidget(self.every_spin)
        for text, minutes in (("1ч", 60), ("2ч", 120), ("3ч", 180), ("4ч", 240)):
            btn = QPushButton(text)
            btn.setObjectName("chip")
            btn.setFixedWidth(40)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(f"Каждые {human_duration(minutes)}")
            btn.clicked.connect(lambda _=False, v=minutes: self.every_spin.setValue(v))
            eb.addWidget(btn)
        eb.addStretch(1)
        iv.addWidget(every_box, 0, 1, 1, 3)

        from_label = QLabel("С")
        from_label.setObjectName("hint")
        iv.addWidget(from_label, 1, 0)
        self.window_start = QTimeEdit(QTime(9, 0))
        self.window_start.setDisplayFormat("HH:mm")
        self.window_start.timeChanged.connect(self._update_preview)
        iv.addWidget(self.window_start, 1, 1)

        to_label = QLabel("до")
        to_label.setObjectName("hint")
        to_label.setAlignment(Qt.AlignCenter)
        iv.addWidget(to_label, 1, 2)
        self.window_end = QTimeEdit(QTime(21, 0))
        self.window_end.setDisplayFormat("HH:mm")
        self.window_end.timeChanged.connect(self._update_preview)
        iv.addWidget(self.window_end, 1, 3)

        self.interval_row.hide()
        grid.addWidget(self.interval_row, 2, 0, 1, 4)

        self.lead_check = QCheckBox("Напомнить заранее")
        self.lead_check.stateChanged.connect(self._lead_toggled)
        grid.addWidget(self.lead_check, 3, 0, 1, 3)
        self.lead_spin = QSpinBox()
        self.lead_spin.setRange(1, 1440)
        self.lead_spin.setValue(10)
        self.lead_spin.setSuffix(" мин")
        self.lead_spin.setEnabled(False)
        self.lead_spin.valueChanged.connect(self._update_preview)
        grid.addWidget(self.lead_spin, 3, 3)

        grid.addWidget(label("Заготовки"), 4, 0)
        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip("Подставить сохранённые настройки")
        self.preset_combo.activated.connect(self._apply_preset)
        grid.addWidget(self.preset_combo, 4, 1, 1, 2)
        self.save_preset_btn = QPushButton("Запомнить")
        self.save_preset_btn.setToolTip("Сохранить настройки формы как заготовку")
        self.save_preset_btn.clicked.connect(self._save_preset)
        grid.addWidget(self.save_preset_btn, 4, 3)

        self.more_box.hide()
        layout.addWidget(self.more_box)
        self._reload_presets()

        self.preview = QLabel()
        self.preview.setObjectName("hint")
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)

        self.submit_btn = accent_button("Создать напоминание", "accent", big=True)
        self.submit_btn.clicked.connect(self.submit)
        layout.addWidget(self.submit_btn)

        self._form_frame = frame
        self._update_preview()
        return frame

    def _sync_form_height(self):
        """Подгоняет высоту формы и окна, чтобы ничего не наложилось.

        Qt при нехватке места не добавляет прокрутку, а сжимает виджеты
        и они наезжают друг на друга. Поэтому: фиксируем высоту карточки
        по её содержимому, пересчитываем раскладку и поднимаем минимум окна.
        Если экран не позволяет вырасти — включаем прокрутку всего окна.
        """
        frame = getattr(self, "_form_frame", None)
        if not frame or not hasattr(self, "_scroll"):
            return

        frame.setFixedHeight(frame.sizeHint().height())
        # Пересчитываем раскладку СРАЗУ, иначе minimumSizeHint отдаст
        # прошлые значения и окно окажется меньше нужного.
        self._page.adjustSize()
        needed = self._page.sizeHint().height()

        screen = QApplication.primaryScreen()
        limit = int(screen.availableGeometry().height() * 0.92) if screen else 900
        target = min(needed + 4, limit)

        if self.height() < target:
            self.resize(self.width(), target)

    def _toggle_more(self, opened: bool):
        self.more_box.setVisible(opened)
        self.more_btn.setText("  Свернуть" if opened else "  Подробнее")
        self.more_btn.setIcon(QIcon(ARROW_UP_ICON if opened else ARROW_ICON))
        # Пересчитываем высоту карточки, иначе места не хватит и поля наедут
        QTimer.singleShot(0, self._sync_form_height)

    def _show_more(self):
        """Раскрыть блок «Подробнее» — нужно при правке напоминания."""
        if not self.more_btn.isChecked():
            self.more_btn.setChecked(True)

    def _tabs(self) -> QTabWidget:
        self.tabs = QTabWidget()

        # --- Активные ---
        active = QWidget()
        al = QVBoxLayout(active)
        al.setContentsMargins(0, SP["md"], 0, 0)
        al.setSpacing(SP["sm"])

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumHeight(30)
        self.search_input.textChanged.connect(self.refresh_lists)
        filter_row.addWidget(self.search_input, 2)
        self.tag_filter = QComboBox()
        self.tag_filter.setMinimumWidth(130)
        self.tag_filter.setMinimumHeight(30)
        self.tag_filter.currentIndexChanged.connect(self.refresh_lists)
        filter_row.addWidget(self.tag_filter, 1)
        al.addLayout(filter_row)

        self.active_list = QListWidget()
        self.active_list.setMinimumHeight(90)
        self.active_list.itemDoubleClicked.connect(lambda _: self.start_editing())
        self.active_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.active_list.customContextMenuRequested.connect(self._active_menu)
        al.addWidget(wrap_list(self.active_list), 1)

        ab = QHBoxLayout()
        edit_btn = QPushButton("Изменить")
        edit_btn.clicked.connect(self.start_editing)
        delete_btn = QPushButton("Удалить")
        delete_btn.clicked.connect(self.delete_selected)
        ab.addWidget(edit_btn)
        ab.addWidget(delete_btn)
        ab.addStretch(1)
        hint = QLabel("Двойной клик — изменить")
        hint.setObjectName("hint")
        ab.addWidget(hint)
        al.addLayout(ab)
        self.tabs.addTab(active, "Активные")

        # --- Требуют внимания ---
        missed = QWidget()
        ml = QVBoxLayout(missed)
        ml.setContentsMargins(0, SP["md"], 0, 0)
        ml.setSpacing(SP["sm"])
        self.missed_list = QListWidget()
        self.missed_list.setMinimumHeight(90)
        self.missed_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.missed_list.customContextMenuRequested.connect(self._missed_menu)
        ml.addWidget(wrap_list(self.missed_list), 1)
        mb = QHBoxLayout()
        done_btn = accent_button("Сделано", "ok")
        done_btn.clicked.connect(self.mark_done_selected)
        snooze_btn = QPushButton("Отложить…")
        snooze_btn.clicked.connect(self.snooze_selected)
        mb.addWidget(done_btn)
        mb.addWidget(snooze_btn)
        mb.addStretch(1)
        clear_btn = QPushButton("Убрать все")
        clear_btn.setToolTip("Все записи уедут в архив")
        clear_btn.clicked.connect(self.clear_missed)
        mb.addWidget(clear_btn)
        ml.addLayout(mb)
        self.tabs.addTab(missed, "Требуют внимания")

        # --- Архив ---
        archive = QWidget()
        gl = QVBoxLayout(archive)
        gl.setContentsMargins(0, SP["md"], 0, 0)
        gl.setSpacing(SP["sm"])
        # Пояснение — в подсказку вкладки, чтобы не занимать две строки
        # и не ломать расчёт высоты окна.
        self.archive_list = QListWidget()
        self.archive_list.setMinimumHeight(90)
        self.archive_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.archive_list.customContextMenuRequested.connect(self._archive_menu)
        gl.addWidget(wrap_list(self.archive_list), 1)
        gb = QHBoxLayout()
        restore_btn = accent_button("Вернуть", "info")
        restore_btn.clicked.connect(self.restore_selected)
        again_btn = QPushButton("Повторить")
        again_btn.setToolTip("Создать такое же напоминание заново")
        again_btn.clicked.connect(self.repeat_from_archive)
        gb.addWidget(restore_btn)
        gb.addWidget(again_btn)
        gb.addStretch(1)
        wipe_btn = QPushButton("Очистить архив")
        wipe_btn.clicked.connect(self.clear_archive)
        gb.addWidget(wipe_btn)
        gl.addLayout(gb)
        self.tabs.addTab(archive, "Архив")
        self.tabs.setTabToolTip(2, "Выполненные напоминания.\n"
                                   "Нажал «Сделано» случайно — верни обратно.")

        # Одинаковая минимальная высота: иначе при переключении вкладок
        # окно то расширяется, то поджимается и виджеты «прыгают».
        for index in range(self.tabs.count()):
            self.tabs.widget(index).setMinimumHeight(190)
        return self.tabs

    # ---------- контекстные меню списков ----------
    def _menu_for(self, widget: QListWidget, actions):
        """Правый клик по строке: те же действия, что и кнопками."""
        item = widget.itemAt(widget.mapFromGlobal(widget.cursor().pos()))
        menu = QMenu(self)
        menu.setStyleSheet(QApplication.instance().styleSheet() or STYLE)
        for label, handler in actions:
            act = QAction(label, self)
            act.triggered.connect(handler)
            menu.addAction(act)
        menu.exec(widget.cursor().pos())

    def _active_menu(self, _pos):
        self._menu_for(self.active_list, [
            ("Изменить", self.start_editing),
            ("Удалить", self.delete_selected),
        ])

    def _missed_menu(self, _pos):
        self._menu_for(self.missed_list, [
            ("Сделано", self.mark_done_selected),
            ("Отложить…", self.snooze_selected),
        ])

    def _archive_menu(self, _pos):
        self._menu_for(self.archive_list, [
            ("Вернуть обратно", self.restore_selected),
            ("Повторить", self.repeat_from_archive),
        ])

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(app_icon())
        self.tray.setToolTip(APP_TITLE)

        menu = QMenu()
        menu.setStyleSheet(QApplication.instance().styleSheet() or STYLE)

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
        self.datetime_input.setDateTime(now_qdt(minutes * 60))

    def _tomorrow_morning(self):
        target = QDateTime.currentDateTime().addDays(1)
        target.setTime(QTime(9, 0, 0, 0))
        self.datetime_input.setDateTime(target)

    def _this_evening(self):
        now = QDateTime.currentDateTime()
        target = QDateTime(now.date(), QTime(19, 0, 0, 0))
        if target <= now:
            target = target.addDays(1)
        self.datetime_input.setDateTime(target)

    # ---------- метки и заготовки ----------
    def _reload_tags(self):
        """Перезаполняет выпадашку меток, сохраняя введённый текст."""
        current = self.tag_combo.currentText() if hasattr(self, "tag_combo") else ""
        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        self.tag_combo.addItem("")                    # пустая = без метки
        for name in self.settings["tags"]:
            self.tag_combo.addItem(name)
        self.tag_combo.setCurrentText(current)
        self.tag_combo.blockSignals(False)

    def _remember_tag(self, tag: str):
        """Новую метку запоминаем, чтобы в следующий раз выбрать из списка."""
        tag = (tag or "").strip()
        if not tag:
            return
        tags = list(self.settings["tags"])
        if tag not in tags:
            tags.append(tag)
            self.settings["tags"] = tags
            self.settings.save()
            self._reload_tags()

    def _reload_presets(self):
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("Заготовки…", None)
        for index, preset in enumerate(self.settings["presets"]):
            self.preset_combo.addItem(preset.get("name", "без имени"), index)
        self.preset_combo.addItem("— управление заготовками —", "manage")
        self.preset_combo.setCurrentIndex(0)
        self.preset_combo.blockSignals(False)

    def _apply_preset(self, index: int):
        data = self.preset_combo.itemData(index)
        self.preset_combo.setCurrentIndex(0)
        if data == "manage":
            self._manage_presets()
            return
        if data is None:
            return
        try:
            preset = self.settings["presets"][int(data)]
        except (IndexError, ValueError, TypeError):
            return

        self.message_input.setText(preset.get("message", ""))
        repeat = RepeatType[preset.get("repeat", "ONCE")]
        pos = self.repeat_combo.findData(repeat)
        if pos >= 0:
            self.repeat_combo.setCurrentIndex(pos)
        self._set_weekdays(preset.get("weekdays", []))
        lead = int(preset.get("lead_minutes", 0) or 0)
        self.lead_check.setChecked(lead > 0)
        if lead:
            self.lead_spin.setValue(lead)
        self.tag_combo.setCurrentText(preset.get("tag", ""))
        # время: либо «через N минут», либо фиксированный час
        offset = preset.get("offset_minutes")
        at_time = preset.get("at_time")
        if offset:
            self.datetime_input.setDateTime(now_qdt(int(offset) * 60))
        elif at_time:
            try:
                hh, mm = (int(x) for x in str(at_time).split(":"))
                target = QDateTime.currentDateTime()
                target.setTime(QTime(hh, mm, 0, 0))
                if target <= QDateTime.currentDateTime():
                    target = target.addDays(1)
                self.datetime_input.setDateTime(target)
            except Exception:
                pass
        self.set_status(f"Заготовка «{preset.get('name','')}» подставлена", "info")
        self.message_input.setFocus()

    def _save_preset(self):
        message = self.message_input.text().strip()
        if not message:
            self.set_status("Сначала заполни форму, потом сохраняй заготовку", PALETTE["warn"])
            self.message_input.setFocus()
            return
        dialog = PresetSaveDialog(self, default_name=message[:30])
        dialog.setStyleSheet(self.styleSheet() or STYLE)
        if dialog.exec() != QDialog.Accepted:
            return
        name, mode = dialog.result_data()

        preset = {
            "name": name,
            "message": message,
            "repeat": self.repeat_combo.currentData().name,
            "weekdays": self._selected_weekdays(),
            "lead_minutes": self._lead_minutes(),
            "tag": self.tag_combo.currentText().strip(),
        }
        chosen = self.datetime_input.dateTime().toPython()
        if mode == "offset":
            delta = max(1, int((chosen - datetime.now()).total_seconds() // 60))
            preset["offset_minutes"] = delta
        else:
            preset["at_time"] = f"{chosen.hour:02d}:{chosen.minute:02d}"

        presets = list(self.settings["presets"])
        presets = [p for p in presets if p.get("name") != name]   # перезапись по имени
        presets.append(preset)
        self.settings["presets"] = presets
        self.settings.save()
        self._reload_presets()
        self._remember_tag(preset["tag"])
        self.set_status(f"Заготовка «{name}» сохранена", PALETTE["ok"])

    def _manage_presets(self):
        dialog = PresetManageDialog(self, self.settings)
        dialog.setStyleSheet(self.styleSheet() or STYLE)
        dialog.exec()
        self._reload_presets()

    def _repeat_changed(self):
        mode = self.repeat_combo.currentData()
        is_weekdays = mode == RepeatType.WEEKDAYS
        is_interval = mode == RepeatType.INTERVAL
        self.weekday_row.setVisible(is_weekdays or is_interval)
        self.interval_row.setVisible(is_interval)
        if is_interval and not self._selected_weekdays():
            # Для графика пустой список дней = каждый день, подсказываем это
            pass
        QTimer.singleShot(0, self._sync_form_height)
        if is_weekdays and not self._selected_weekdays():
            # по умолчанию отмечаем день выбранной даты
            today = self.datetime_input.dateTime().toPython().weekday()
            self.weekday_boxes[today].setChecked(True)
        self._update_preview()

    def _set_weekdays(self, days: List[int]):
        for index, box in enumerate(self.weekday_boxes):
            box.setChecked(index in days)

    def _selected_weekdays(self) -> List[int]:
        return [i for i, box in enumerate(self.weekday_boxes) if box.isChecked()]

    def _lead_toggled(self):
        self.lead_spin.setEnabled(self.lead_check.isChecked())
        self._update_preview()

    def _lead_minutes(self) -> int:
        return self.lead_spin.value() if self.lead_check.isChecked() else 0

    def _update_preview(self):
        target = self.datetime_input.dateTime().toPython()
        repeat = self.repeat_combo.currentData()

        if repeat == RepeatType.INTERVAL:
            every = self.every_spin.value()
            w1 = self.window_start.time().toString("HH:mm")
            w2 = self.window_end.time().toString("HH:mm")
            days = self._selected_weekdays()
            # Сколько всего сигналов за окно — полезно понимать заранее
            sh, sm = parse_hhmm(w1); eh, em = parse_hhmm(w2)
            span = (eh * 60 + em) - (sh * 60 + sm)
            if span <= 0:
                span += 24 * 60
            count = span // max(1, every) + 1
            when_text = weekdays_label(days) if days else "каждый день"
            text = (f"Каждые {human_duration(every)} с {w1} до {w2}, "
                    f"{when_text} — примерно {count} "
                    f"{plural(count, 'сигнал', 'сигнала', 'сигналов')} за день")
            if self._lead_minutes():
                text += f" · предупрежу за {human_duration(self._lead_minutes())}"
            self.preview.setText(text)
            self.preview.setStyleSheet(f'color:{PALETTE["text_dim"]};')
            return

        if repeat == RepeatType.WEEKDAYS:
            days = self._selected_weekdays()
            if not days:
                self.preview.setText("Отметь хотя бы один день недели")
                self.preview.setStyleSheet(f'color:{PALETTE["danger"]};')
                return
            probe = target
            for _ in range(8):
                if probe.weekday() in days and probe > datetime.now():
                    break
                probe += timedelta(days=1)
            text = (f"{weekdays_label(days).capitalize()} в {probe:%H:%M} · "
                    f"ближайшее {human_until(probe)}")
            if self._lead_minutes():
                text += f" · предупрежу за {human_duration(self._lead_minutes())}"
            self.preview.setText(text)
            self.preview.setStyleSheet(f'color:{PALETTE["text_dim"]};')
            return

        if target <= datetime.now():
            self.preview.setText("Время уже прошло — выбери будущее")
            self.preview.setStyleSheet(f'color:{PALETTE["danger"]};')
            return

        text = "Сработает " + human_until(target)
        lead = self._lead_minutes()
        if lead:
            warn_at = target - timedelta(minutes=lead)
            if warn_at > datetime.now():
                text += f"; предупрежу в {warn_at:%H:%M}"
            else:
                text += " · предупредить заранее не успеем"
        self.preview.setText(text)
        self.preview.setStyleSheet(f'color:{PALETTE["text_dim"]};')

    def set_status(self, text: str, level: str = "neutral"):
        """Короткое сообщение всплывающей плашкой поверх окна.

        Раньше это была постоянная строка внизу: место занимала всегда,
        а нужна была секунду после действия.
        """
        colors = {
            "success": PALETTE["ok"], "ok": PALETTE["ok"],
            "error": PALETTE["danger"], "danger": PALETTE["danger"],
            "info": PALETTE["info"], "warning": PALETTE["warn"],
            "warn": PALETTE["warn"], "alert": PALETTE["accent"],
            "neutral": PALETTE["text_dim"],
        }
        color = colors.get(level, level if str(level).startswith("#")
                           else PALETTE["text_dim"])
        self.status.setText(text)
        self.status.setStyleSheet(
            f"QLabel#toast{{background:{PALETTE['surface']};color:{color};"
            f"border:1px solid {color};border-radius:8px;padding:8px 14px;"
            "font-weight:600;}")
        self.status.adjustSize()
        self._place_status()
        self.status.show()
        self.status.raise_()
        self._status_timer.start(3200)

    def _place_status(self):
        """Плашка висит по центру снизу, над краем окна."""
        parent = self
        if not parent:
            return
        width = min(parent.width() - 40, max(240, self.status.sizeHint().width()))
        self.status.setFixedWidth(width)
        self.status.adjustSize()
        x = (parent.width() - self.status.width()) // 2
        # Держим плашку над кнопками списка, иначе она их перекрывает
        y = parent.height() - self.status.height() - 62
        self.status.move(max(10, x), max(10, y))

    def _hide_status(self):
        self.status.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.status.isVisible():
            self._place_status()

    # ---------- списки ----------
    def _sync_tag_filter(self):
        """Держит выпадашку фильтра в согласии с реально используемыми метками."""
        used = sorted({r.tag for r in self.manager.reminders if r.tag}
                      | {r.tag for r in self.manager.missed_reminders if r.tag})
        current = self.tag_filter.currentData()
        self.tag_filter.blockSignals(True)
        self.tag_filter.clear()
        self.tag_filter.addItem("Все метки", None)
        for name in used:
            self.tag_filter.addItem(name, name)
        pos = self.tag_filter.findData(current)
        self.tag_filter.setCurrentIndex(pos if pos >= 0 else 0)
        self.tag_filter.blockSignals(False)

    def _passes_filter(self, reminder: Reminder) -> bool:
        query = self.search_input.text().strip().lower()
        if query and query not in reminder.message.lower() and query not in reminder.tag.lower():
            return False
        wanted = self.tag_filter.currentData()
        if wanted and reminder.tag != wanted:
            return False
        return True

    def refresh_lists(self):
        self._sync_tag_filter()
        self.active_list.clear()
        today = datetime.now().date()
        current_group = None
        shown = 0
        for r in self.manager.reminders:
            if not self._passes_filter(r):
                continue
            shown += 1
            # Заголовок группы: Сегодня / Завтра / дата
            day = r.dt.date()
            delta_days = (day - today).days
            if delta_days <= 0:
                group = "Сегодня"
            elif delta_days == 1:
                group = "Завтра"
            elif delta_days < 7:
                # %A даёт английское название — берём русское сами
                group = ("Понедельник", "Вторник", "Среда", "Четверг",
                         "Пятница", "Суббота", "Воскресенье")[r.dt.weekday()]
            else:
                group = f"{r.dt:%d.%m.%Y}"
            if group != current_group:
                current_group = group
                header = QListWidgetItem(group.upper())
                header.setFlags(Qt.NoItemFlags)          # не выбирается
                header.setForeground(QColor(PALETTE["accent"]))
                font = header.font()
                font.setBold(True)
                font.setPointSize(max(8, font.pointSize() - 1))
                header.setFont(font)
                self.active_list.addItem(header)

            # Без эмодзи: в списке Qt они рисуются квадратиком на части систем
            prefix = "↑ " if r.is_lead else ""
            label = f"    {r.dt:%H:%M}   {prefix}{r.message}"
            marks = []
            if r.repeat_type == RepeatType.INTERVAL:
                mark = (f"каждые {human_duration(r.every_minutes)} "
                        f"{r.window_start}–{r.window_end}")
                if r.window_days:
                    mark += f", {weekdays_label(r.window_days)}"
                marks.append(mark)
            elif r.repeat_type == RepeatType.WEEKDAYS:
                marks.append(weekdays_label(r.weekdays))
            elif r.repeat_type != RepeatType.ONCE:
                marks.append(REPEAT_LABELS[r.repeat_type].lower())
            if r.lead_minutes and not r.is_lead:
                marks.append(f"за {human_duration(r.lead_minutes)}")
            if marks:
                label += "   · " + ", ".join(marks)
            if r.tag:
                label += f"   [{r.tag}]"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, r)
            item.setToolTip(f"{r.message}\n{r.dt:%d.%m.%Y %H:%M} — {human_until(r.dt)}")
            if r.is_lead:
                item.setForeground(QColor(PALETTE["lead"]))
            elif r.tag:
                item.setForeground(QColor(tag_color(r.tag)))
            self.active_list.addItem(item)

        if shown == 0 and (self.search_input.text().strip()
                           or self.tag_filter.currentData()):
            empty = QListWidgetItem("Ничего не найдено")
            empty.setFlags(Qt.NoItemFlags)
            empty.setForeground(QColor(PALETTE["text_dim"]))
            self.active_list.addItem(empty)

        self.missed_list.clear()
        for r in self.manager.missed_reminders:
            if not self._passes_filter(r):
                continue
            when = datetime.fromtimestamp(r.fired_at or r.time)
            text = f"{when:%d.%m %H:%M}   {r.message}"
            if r.tag:
                text += f"   · {r.tag}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, r)
            if r.tag:
                item.setForeground(QColor(tag_color(r.tag)))
            self.missed_list.addItem(item)

        # --- Архив ---
        self.archive_list.clear()
        for r in reversed(self.manager.archive):      # свежие сверху
            if not self._passes_filter(r):
                continue
            done = datetime.fromtimestamp(r.done_at or r.fired_at or r.time)
            text = f"{done:%d.%m %H:%M}   {r.message}"
            if r.tag:
                text += f"   [{r.tag}]"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, r)
            item.setForeground(QColor(tag_color(r.tag) if r.tag else PALETTE["text_dim"]))
            self.archive_list.addItem(item)
        if not self.manager.archive:
            empty = QListWidgetItem("Пока пусто. Отмеченные выполненными окажутся здесь.")
            empty.setFlags(Qt.NoItemFlags)
            empty.setForeground(QColor(PALETTE["text_dim"]))
            self.archive_list.addItem(empty)

        # Считаем реальные напоминания, а не строки списка (там ещё заголовки дней)
        real_count = sum(1 for r in self.manager.reminders if not r.is_lead)
        self.tabs.setTabText(0, f"Активные ({real_count})")
        missed_count = len(self.manager.missed_reminders)
        self.tabs.setTabText(1, f"Требуют внимания ({missed_count})"
                             if missed_count else "Требуют внимания")
        self.tabs.setTabText(2, f"Архив ({len(self.manager.archive)})"
                             if self.manager.archive else "Архив")
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
        reminder = item.data(Qt.UserRole) if item else None
        if reminder is not None:
            return reminder
        # Выделен заголовок дня (или ничего) — берём первое напоминание ниже.
        start = widget.currentRow() + 1 if item else 0
        for row in range(max(0, start), widget.count()):
            candidate = widget.item(row).data(Qt.UserRole)
            if candidate is not None:
                widget.setCurrentRow(row)
                return candidate
        return None

    # ---------- действия ----------
    def submit(self):
        message = self.message_input.text().strip()
        when = self.datetime_input.dateTime().toPython()
        repeat = self.repeat_combo.currentData()

        days = (self._selected_weekdays()
                if repeat in (RepeatType.WEEKDAYS, RepeatType.INTERVAL) else [])
        every = self.every_spin.value()
        w_start = self.window_start.time().toString("HH:mm")
        w_end = self.window_end.time().toString("HH:mm")
        lead = self._lead_minutes()
        tag = self.tag_combo.currentText().strip()

        if not message:
            self.set_status("Введи текст напоминания", PALETTE["danger"])
            self.message_input.setFocus()
            return
        if repeat == RepeatType.WEEKDAYS and not days:
            self.set_status("Отметь хотя бы один день недели", PALETTE["danger"])
            return
        # Для «по дням недели» прошедшее время нормально: подвинем на нужный день.
        if repeat != RepeatType.WEEKDAYS and when <= datetime.now():
            self.set_status("Укажи время в будущем", PALETTE["danger"])
            return

        if self.editing is not None:
            self.manager.update(self.editing, message, when, repeat, days, lead, tag,
                                every, w_start, w_end)
            self.set_status(f"Изменено: {message}", PALETTE["ok"])
            self.stop_editing()
        else:
            created = self.manager.add(message, when, repeat, days, lead, tag,
                                       every, w_start, w_end)
            self._remember_tag(tag)
            note = (f"Создано: {message} — {created.dt:%d.%m %H:%M} "
                    f"({human_until(created.dt)})")
            if days:
                note += f", {weekdays_label(days)}"
            if lead:
                note += f", предупрежу за {human_duration(lead)}"
            self.set_status(note, PALETTE["ok"])
            self.message_input.clear()
        self.datetime_input.setDateTime(now_qdt(900))

    def start_editing(self):
        reminder = self._selected(self.active_list)
        if not reminder:
            self.set_status("Сначала выбери напоминание в списке", PALETTE["warn"])
            return
        self.editing = reminder
        # Раскрываем «Подробнее», только когда там есть что смотреть,
        # иначе форма зря разрастается на простом напоминании.
        if (reminder.tag or reminder.lead_minutes
                or reminder.repeat_type != RepeatType.ONCE):
            self._show_more()
        self.message_input.setText(reminder.message)
        self.datetime_input.setDateTime(clean_qdt(QDateTime.fromSecsSinceEpoch(int(reminder.time))))
        index = self.repeat_combo.findData(reminder.repeat_type)
        if index >= 0:
            self.repeat_combo.setCurrentIndex(index)
        self._set_weekdays(reminder.weekdays)
        self.lead_check.setChecked(reminder.lead_minutes > 0)
        if reminder.lead_minutes > 0:
            self.lead_spin.setValue(reminder.lead_minutes)
        self.tag_combo.setCurrentText(reminder.tag)
        self.every_spin.setValue(max(1, reminder.every_minutes))
        sh, sm = parse_hhmm(reminder.window_start, (9, 0))
        eh, em = parse_hhmm(reminder.window_end, (21, 0))
        self.window_start.setTime(QTime(sh, sm))
        self.window_end.setTime(QTime(eh, em))
        if reminder.repeat_type == RepeatType.INTERVAL and reminder.window_days:
            self._set_weekdays(reminder.window_days)
        self.form_title.setText("Правка напоминания")
        self.submit_btn.setText("Сохранить")
        self.cancel_edit_btn.show()
        self.message_input.setFocus()

    def stop_editing(self):
        self.editing = None
        self.form_title.setText("Новое напоминание")
        self.submit_btn.setText("Создать напоминание")
        self.cancel_edit_btn.hide()
        self.message_input.clear()
        self.lead_check.setChecked(False)
        self._set_weekdays([])
        self.tag_combo.setCurrentText("")

    def delete_selected(self):
        reminder = self._selected(self.active_list)
        if not reminder:
            self.set_status("Сначала выбери, что удалить", PALETTE["warn"])
            return
        if self.editing is reminder:
            self.stop_editing()
        self.manager.delete(reminder)
        self.set_status("Напоминание удалено")

    def restore_selected(self):
        reminder = self._selected(self.archive_list)
        if not reminder:
            self.set_status("Сначала выбери запись в архиве", "warning")
            return
        self.manager.restore(reminder)
        self.tabs.setCurrentIndex(1)
        self.set_status(f"«{reminder.message}» вернулось в «Требуют внимания»", "info")

    def repeat_from_archive(self):
        """Создать такое же напоминание заново — частый сценарий."""
        reminder = self._selected(self.archive_list)
        if not reminder:
            self.set_status("Сначала выбери запись в архиве", "warning")
            return
        if reminder.tag:
            self._show_more()          # чтобы метка была видна
        self.message_input.setText(reminder.message)
        self.tag_combo.setCurrentText(reminder.tag)
        self.every_spin.setValue(max(1, reminder.every_minutes))
        sh, sm = parse_hhmm(reminder.window_start, (9, 0))
        eh, em = parse_hhmm(reminder.window_end, (21, 0))
        self.window_start.setTime(QTime(sh, sm))
        self.window_end.setTime(QTime(eh, em))
        if reminder.repeat_type == RepeatType.INTERVAL and reminder.window_days:
            self._set_weekdays(reminder.window_days)
        self.datetime_input.setDateTime(now_qdt(3600))
        self.tabs.setCurrentIndex(0)
        self.message_input.setFocus()
        self.set_status("Осталось выбрать время и нажать «Создать»", "info")

    def clear_archive(self):
        if not self.manager.archive:
            return
        if QMessageBox.question(self, "Очистить архив",
                                "Удалить все записи из архива? "
                                "Вернуть их будет нельзя.") == QMessageBox.Yes:
            self.manager.clear_archive()
            self.set_status("Архив очищен")

    def mark_done_selected(self):
        reminder = self._selected(self.missed_list)
        if not reminder:
            self.set_status("Сначала выбери строку", PALETTE["warn"])
            return
        self.manager.mark_done(reminder)
        self.notification._drop(reminder)
        self.set_status("Отмечено выполненным", PALETTE["ok"])

    def snooze_selected(self):
        reminder = self._selected(self.missed_list)
        if not reminder:
            self.set_status("Сначала выбери, что отложить", PALETTE["warn"])
            return
        dialog = SnoozeDialog(self, int(self.settings["default_snooze"]))
        dialog.setStyleSheet(self.styleSheet() or STYLE)
        if dialog.exec() == QDialog.Accepted:
            minutes = dialog.get_minutes()
            self.manager.snooze(reminder, minutes)
            self.notification._drop(reminder)
            self.set_status(f"Отложено на {human_duration(minutes)}", "info")

    def clear_missed(self):
        if not self.manager.missed_reminders:
            return
        answer = QMessageBox.question(
            self, "Убрать все",
            "Отметить все записи выполненными?\nОни уедут в архив, оттуда можно вернуть.")
        if answer == QMessageBox.Yes:
            for reminder in list(self.manager.missed_reminders):
                self.manager.mark_done(reminder)
            self.notification.items.clear()
            self.notification.refresh()
            self.set_status("Всё уехало в архив", "ok")

    def quick_add(self):
        dialog = QuickAddDialog(self)
        dialog.setStyleSheet(self.styleSheet() or STYLE)
        if dialog.exec() == QDialog.Accepted:
            message, when, repeat = dialog.result_data()
            self.manager.add(message, when, repeat)
            self.set_status(f"Создано: {message} — {when:%d.%m %H:%M}", PALETTE["ok"])
            if self.settings["tray_balloon"]:
                self.tray.showMessage(APP_TITLE,
                                      f"Напомню {when:%d.%m в %H:%M}: {message}",
                                      QSystemTrayIcon.Information, 4000)

    def open_settings(self):
        dialog = SettingsDialog(self, self.settings, self.startup)
        dialog.setStyleSheet(self.styleSheet() or STYLE)
        before = self.settings["theme"]
        if dialog.exec() == QDialog.Accepted:
            minutes = int(self.settings["default_snooze"])
            self.notification.default_snooze = minutes
            self.notification.snooze_btn.setText(f"Отложить {minutes} мин")
            self.apply_hotkey()          # клавиша могла смениться
            self._reload_tags()
            if self.settings["theme"] != before:
                self.apply_theme_now()
            self.set_status("Настройки сохранены", "ok")

    def apply_theme_now(self):
        """Переключает оформление без перезапуска программы."""
        apply_theme(self.settings["theme"])
        style = refresh_theme_assets()
        app = QApplication.instance()
        if app:
            app.setStyleSheet(style)
        self.setStyleSheet(style)
        self.notification.setStyleSheet(style)
        self._restyle_accent_buttons()
        self.more_btn.setIcon(QIcon(ARROW_UP_ICON if self.more_btn.isChecked()
                                    else ARROW_ICON))
        self.refresh_lists()
        self._sync_form_height()

    def _restyle_accent_buttons(self):
        """Перекрашивает цветные кнопки после смены темы.

        Они красятся инлайн-стилем, поэтому общий setStyleSheet их не трогает.
        Находим по метке accentRole и пересобираем стиль.
        """
        for widget in self.findChildren(QPushButton):
            role = widget.property("accentRole")
            if not role:
                continue
            fresh = accent_button(widget.text(), role,
                                  bool(widget.property("accentBig")))
            widget.setStyleSheet(fresh.styleSheet())
            fresh.deleteLater()
        for dlg in (self.notification,):
            for widget in dlg.findChildren(QPushButton):
                role = widget.property("accentRole")
                if not role:
                    continue
                fresh = accent_button(widget.text(), role,
                                      bool(widget.property("accentBig")))
                widget.setStyleSheet(fresh.styleSheet())
                fresh.deleteLater()

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
                        "alert")

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

    # ---------- горячие клавиши ----------
    def _build_hotkeys(self):
        """Локальные (в окне) и глобальная (на всю систему).

        Используем QShortcut, а не QAction: QAction срабатывает, только если
        добавлен в виджет И не перехвачен полем ввода. QShortcut с контекстом
        WindowShortcut работает из любого места окна — так надёжнее.
        """
        self._shortcuts = []
        pairs = (
            ("Ctrl+N", self._focus_message),
            ("Ctrl+Return", self.submit),
            ("Ctrl+Enter", self.submit),          # цифровой Enter
            ("Ctrl+E", self.start_editing),
            ("Ctrl+F", self._focus_search),
            ("Ctrl+,", self.open_settings),
            ("Ctrl+D", self._delete_if_list_focused),
            ("F5", self.refresh_lists),
            ("Escape", self._escape_pressed),
            ("Ctrl+1", lambda: self.tabs.setCurrentIndex(0)),
            ("Ctrl+2", lambda: self.tabs.setCurrentIndex(1)),
            ("Ctrl+3", lambda: self.tabs.setCurrentIndex(2)),
        )
        for combo, handler in pairs:
            sc = QShortcut(QKeySequence(combo), self)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(handler)
            self._shortcuts.append(sc)

        # Delete — только когда фокус в списке, иначе мешал бы вводу текста
        for widget in (self.active_list, self.missed_list, self.archive_list):
            sc = QShortcut(QKeySequence("Delete"), widget)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(self._delete_if_list_focused)
            self._shortcuts.append(sc)

        # Глобальная — работает даже когда окно свёрнуто
        self.hotkey = GlobalHotkey(self)
        self.hotkey.activated.connect(self._global_hotkey_fired)
        self.apply_hotkey()

    def _focus_message(self):
        self.show_window()
        self.message_input.setFocus()
        self.message_input.selectAll()

    def _focus_search(self):
        self.show_window()
        self.tabs.setCurrentIndex(0)
        self.search_input.setFocus()
        self.search_input.selectAll()

    def _global_hotkey_fired(self):
        """Глобальная клавиша: быстрое создание поверх всех окон."""
        self.quick_add()

    def apply_hotkey(self):
        """Перерегистрирует глобальную клавишу по текущим настройкам."""
        if not hasattr(self, "hotkey"):
            return
        self.hotkey.unregister()
        if not self.settings["hotkey_enabled"]:
            return
        if not self.hotkey.available:
            return
        combo = self.settings["hotkey"]
        if not self.hotkey.register(combo):
            self.set_status(f"Сочетание {combo} занято другой программой", PALETTE["warn"])

    def _delete_if_list_focused(self):
        """Delete удаляет только когда фокус в списке, иначе мешал бы вводу."""
        if self.active_list.hasFocus():
            self.delete_selected()
        elif self.missed_list.hasFocus():
            self.mark_done_selected()

    def _escape_pressed(self):
        if self.editing is not None:
            self.stop_editing()
        elif self.search_input.text():
            self.search_input.clear()
        else:
            self.hide()

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
        if hasattr(self, "hotkey"):
            self.hotkey.unregister()
        self.manager.save_all()
        self.settings.save()
        self.tray.hide()
        QApplication.quit()


# --- Точка входа -----------------------------------------------------------

def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setQuitOnLastWindowClosed(False)

    saved = Settings(data_file(SETTINGS_FILENAME))
    apply_theme(saved["theme"])
    app.setStyleSheet(refresh_theme_assets())

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
