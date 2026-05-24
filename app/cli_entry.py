# app/cli_entry.py — точка входа CLI
#
# Зачем этот файл?
#   main.py лежит в корне проекта и не является частью установленного пакета.
#   Когда пользователь запускает команду `maps`, Python ищет модуль по sys.path,
#   но корень проекта там может не быть (особенно при editable install на сервере).
#
#   Пакет `app` устанавливается корректно всегда. Поэтому точка входа CLI
#   указывает сюда, а мы уже отсюда добавляем корень проекта в sys.path
#   и импортируем typer-приложение из main.py.
#
# Точка входа в pyproject.toml:
#   maps = "app.cli_entry:app"

import sys
from pathlib import Path

# Добавляем корень проекта (папку выше app/) в sys.path,
# чтобы Python мог найти и импортировать main.py
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from main import app  # noqa: E402 — импорт после изменения sys.path намеренно

__all__ = ["app"]
