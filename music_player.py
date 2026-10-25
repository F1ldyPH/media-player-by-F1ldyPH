"""
music_player.py
---------------
Точка входа для Deep Space Music Player.
Весь код интерфейса находится в пакете ui/.
"""

import os
import sys

# Подавляем предупреждения Qt Multimedia ("Unknown property content")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")

from PySide6.QtWidgets import QApplication

from ui.main_player import MusicPlayer, _apply_dark_palette


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    _apply_dark_palette(app)
    window = MusicPlayer()
    # Выход через трей (QApplication.quit()) минует closeEvent — явно
    # закрываем окно, чтобы сессия сохранилась, а потоки корректно остановились.
    app.aboutToQuit.connect(window.close)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
