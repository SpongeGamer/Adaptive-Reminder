"""
Сборка «Умные Памятки» в один EXE.

Почему сборка вынесена в Python, а не написана в .bat:
cmd.exe читает батник в текущей кодовой странице консоли. У разных людей
она разная (866, 1251, 65001), и русский текст внутри .bat превращается
в мусор, а обрывки строк выполняются как команды. Обойти это надёжно нельзя.
Python 3.6+ на Windows выводит текст через Unicode-API консоли, поэтому
русские сообщения печатаются правильно при любой кодовой странице.

Запускается двойным кликом по «Запусти меня.bat».
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

APP_NAME = "Умные Памятки"
MAIN_SCRIPT = "adaptive_reminder.py"
ICON = "icon.ico"
SOUND = "notification.wav"

HERE = os.path.dirname(os.path.abspath(__file__))


def line(char: str = "=") -> None:
    print(char * 52)


def step(number: int, total: int, text: str) -> None:
    print(f"\n[{number}/{total}] {text}")


def fail(text: str) -> None:
    print()
    line()
    print("  ОШИБКА: " + text)
    line()


def run(args: list[str], quiet: bool = False) -> bool:
    """Запускает команду. Возвращает True, если всё хорошо."""
    try:
        if quiet:
            done = subprocess.run(args, cwd=HERE,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        else:
            done = subprocess.run(args, cwd=HERE)
        return done.returncode == 0
    except FileNotFoundError:
        return False


def check_python() -> bool:
    if sys.version_info < (3, 9):
        fail(f"нужен Python 3.9 или новее, а стоит {sys.version.split()[0]}")
        return False
    print(f"  Python {sys.version.split()[0]} — подходит")
    return True


def install_deps() -> bool:
    run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], quiet=True)
    print("  ставим PySide6 и PyInstaller, это может занять минуту...")
    ok = run([sys.executable, "-m", "pip", "install", "PySide6", "pyinstaller"],
             quiet=True)
    if not ok:
        fail("не удалось поставить зависимости.\n"
             "  Проверь интернет и попробуй ещё раз.")
        return False
    print("  зависимости на месте")
    return True


def clean() -> None:
    for folder in ("build", "dist"):
        path = os.path.join(HERE, folder)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            print(f"  удалено: {folder}")
    spec = os.path.join(HERE, APP_NAME + ".spec")
    if os.path.exists(spec):
        os.remove(spec)
        print(f"  удалено: {APP_NAME}.spec")
    print("  чисто")


def build() -> bool:
    args = [sys.executable, "-m", "PyInstaller",
            "--onefile", "--windowed", "--noconsole", "--clean",
            "--name", APP_NAME]

    icon_path = os.path.join(HERE, ICON)
    if os.path.exists(icon_path):
        args += ["--icon", ICON, "--add-data", f"{ICON}{os.pathsep}."]
    else:
        print(f"  [i] {ICON} не найден — иконку нарисует сама программа")

    sound_path = os.path.join(HERE, SOUND)
    if os.path.exists(sound_path):
        args += ["--add-data", f"{SOUND}{os.pathsep}."]
    else:
        print(f"  [i] {SOUND} не найден — будет системный звук")

    args.append(MAIN_SCRIPT)
    print("  собираем, это займёт пару минут...\n")
    run(args)

    exe = os.path.join(HERE, "dist", APP_NAME + ".exe")
    if not os.path.exists(exe):
        fail("EXE не собрался.\n"
             "  Посмотри последние строки вывода выше — там причина.")
        return False
    size = os.path.getsize(exe) / 1024 / 1024
    print(f"\n  готово: dist\\{APP_NAME}.exe ({size:.1f} МБ)")
    return True


def make_shortcut() -> bool:
    """Ярлык на рабочем столе.

    Пишем .ps1 в UTF-8 **с BOM**: PowerShell 5.1 без BOM читает файл как ANSI,
    и русские буквы в имени ярлыка превращаются в мусор
    («пшфрывгшпфшгвыарфгышаргш» вместо «Умные Памятки»).
    """
    if os.name != "nt":
        print("  [i] не Windows — ярлык пропускаем")
        return True

    exe = os.path.join(HERE, "dist", APP_NAME + ".exe")
    workdir = os.path.join(HERE, "dist")
    icon_path = os.path.join(HERE, ICON)

    ps = [
        "$ErrorActionPreference = 'Stop'",
        "$desktop = [Environment]::GetFolderPath('Desktop')",
        f"$link = Join-Path $desktop '{APP_NAME}.lnk'",
        "$shell = New-Object -ComObject WScript.Shell",
        "$sc = $shell.CreateShortcut($link)",
        f"$sc.TargetPath = '{exe}'",
        f"$sc.WorkingDirectory = '{workdir}'",
        "$sc.Description = 'Умные напоминания и планировщик'",
    ]
    if os.path.exists(icon_path):
        ps.append(f"$sc.IconLocation = '{icon_path}'")
    ps.append("$sc.Save()")

    ps_path = os.path.join(os.environ.get("TEMP", HERE), "_ar_shortcut.ps1")
    try:
        # utf-8-sig = UTF-8 с BOM. Именно BOM объясняет PowerShell,
        # что файл в UTF-8, а не в системной ANSI-кодировке.
        with open(ps_path, "w", encoding="utf-8-sig") as f:
            f.write("\r\n".join(ps))
        ok = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                  "-File", ps_path], quiet=True)
    except Exception as exc:
        print(f"  [i] ярлык создать не вышло ({exc})")
        return False
    finally:
        try:
            os.remove(ps_path)
        except OSError:
            pass

    if ok:
        print("  ярлык на рабочем столе готов")
    else:
        print("  [i] ярлык создать не вышло — запускай EXE из папки dist")
    return ok


def main() -> int:
    print()
    line()
    print(f"  {APP_NAME} — сборка программы")
    line()

    os.chdir(HERE)

    if not os.path.exists(os.path.join(HERE, MAIN_SCRIPT)):
        fail(f"рядом нет файла {MAIN_SCRIPT}.\n"
             "  Положи этот скрипт в папку с программой.")
        return 1

    total = 4
    step(1, total, "Проверяем Python")
    if not check_python():
        return 1

    step(2, total, "Ставим зависимости")
    if not install_deps():
        return 1

    step(3, total, "Чистим прошлую сборку")
    clean()

    step(4, total, "Собираем EXE")
    if not build():
        return 1

    print()
    make_shortcut()

    print()
    line()
    print("  Готово!")
    print()
    print(f"  Программа:  dist\\{APP_NAME}.exe")
    print("  Ярлык:      на рабочем столе")
    print()
    print("  Напоминания хранятся в папке:")
    print("  %APPDATA%\\AdaptiveReminder")
    print("  Они лежат отдельно от программы,")
    print("  поэтому не потеряются при пересборке.")
    line()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n  Прервано пользователем.")
        sys.exit(1)
