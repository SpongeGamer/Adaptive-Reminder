"""Заглушка winsound для запуска Windows-кода на Linux (только для тестов)."""
SND_FILENAME = 0x20000
SND_ASYNC = 0x0001
SND_LOOP = 0x0008
SND_PURGE = 0x0040

_played = []


def PlaySound(sound, flags):
    _played.append((sound, flags))
    print(f"[winsound-stub] PlaySound({sound!r}, {flags})")


def Beep(freq, dur):
    print(f"[winsound-stub] Beep({freq}, {dur})")


def MessageBeep(t=0):
    print(f"[winsound-stub] MessageBeep({t})")
