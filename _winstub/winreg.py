"""Заглушка winreg для запуска Windows-кода на Linux (только для тестов).

Хранит «реестр» в памяти, чтобы можно было проверить логику автозагрузки.
"""
HKEY_CURRENT_USER = "HKCU"
HKEY_LOCAL_MACHINE = "HKLM"
KEY_SET_VALUE = 0x0002
KEY_READ = 0x20019
REG_SZ = 1

_STORE = {}


class _Key:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def OpenKey(root, path, reserved=0, access=0):
    return _Key(f"{root}\\{path}")


def CreateKey(root, path):
    return _Key(f"{root}\\{path}")


def SetValueEx(key, name, reserved, type_, value):
    _STORE.setdefault(key.path, {})[name] = value


def QueryValueEx(key, name):
    store = _STORE.get(key.path, {})
    if name not in store:
        raise FileNotFoundError(f"Нет значения {name}")
    return store[name], REG_SZ


def DeleteValue(key, name):
    store = _STORE.get(key.path, {})
    if name not in store:
        raise FileNotFoundError(f"Нет значения {name}")
    del store[name]


def CloseKey(key):
    pass
