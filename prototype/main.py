"""
Тонкая обёртка для запуска пакета prototype из командной строки.

Основной способ запуска — entry point `tester` (см. pyproject.toml):
    uv run tester <путь к контракту>

Этот файл оставлен для обратной совместимости:
    python main.py <путь к контракту>
"""

from prototype import main

if __name__ == "__main__":
    main()
