"""
Временный диагностический скрипт.

Нужен только для получения списка моделей,
которые реально доступны через Deepcode OpenAI-compatible API.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


# Загружаем настройки из prototype/.env.
PROTOTYPE_DIR = Path(__file__).resolve().parent
load_dotenv(PROTOTYPE_DIR / ".env")


# Старый пользовательский SSL_CERT_FILE для этого проекта не нужен.
os.environ.pop("SSL_CERT_FILE", None)


def main() -> None:
    """
    Подключиться к Deepcode API и вывести доступные ID моделей.
    """

    client = OpenAI(
        api_key=os.environ["DEEPCODE_API_KEY"],
        base_url=os.environ["DEEPCODE_BASE_URL"],
    )

    models = client.models.list()

    print("Доступные модели:")

    for model in models.data:
        print(f"- {model.id}")


if __name__ == "__main__":
    main()