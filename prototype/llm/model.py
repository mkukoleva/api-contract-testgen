"""
Этот модуль отвечает за подключение к языковой модели.

Модель доступна через OpenAI-compatible API Deepcode.

Параметры подключения:
- API-ключ;
- адрес API;
- имя модели

загружаются из файла prototype/.env.

Также здесь очищаются устаревшие SSL-настройки окружения,
которые могут мешать подключению к Deepcode API.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI


# Определяем абсолютный путь до папки prototype.
PROTOTYPE_DIR = Path(__file__).resolve().parents[1]

# Загружаем настройки проекта из prototype/.env.
#
# load_dotenv добавляет переменные из файла .env
# в окружение текущего Python-процесса.
load_dotenv(PROTOTYPE_DIR / ".env")


def _clear_unused_ssl_settings() -> None:
    """
    Удалить устаревшие пользовательские настройки SSL.

    Ранее на компьютере использовалась переменная SSL_CERT_FILE
    для подключения к другому сервису.

    Для Deepcode этот пользовательский сертификат не нужен.
    Если оставить переменную, HTTP-клиент может попытаться
    загрузить старый или несуществующий файл сертификата.

    После удаления переменной HTTP-клиент использует
    стандартное хранилище доверенных сертификатов.
    """

    ssl_cert_file = os.getenv("SSL_CERT_FILE")

    if ssl_cert_file:
        print(
            "[LLM] Обнаружен пользовательский SSL_CERT_FILE: "
            f"{ssl_cert_file}"
        )

        # Удаляем переменную только внутри текущего Python-процесса.
        # Настройки всей операционной системы этим не изменяются.
        os.environ.pop("SSL_CERT_FILE", None)

        print(
            "[LLM] SSL_CERT_FILE отключён для текущего запуска."
        )


def build_model() -> ChatOpenAI:
    """
    Создать и настроить языковую модель для ИИ-агента.

    Returns:
        Настроенный объект ChatOpenAI.

    Raises:
        RuntimeError:
            Если в .env отсутствует API-ключ,
            адрес API или имя модели.
    """

    # Перед созданием HTTP-клиента очищаем старую SSL-настройку.
    # _clear_unused_ssl_settings()

    api_key = os.getenv("DEEPCODE_API_KEY")
    base_url = os.getenv("DEEPCODE_BASE_URL")
    model_name = os.getenv("DEEPCODE_MODEL")

    if not api_key:
        raise RuntimeError(
            "DEEPCODE_API_KEY не указан в prototype/.env"
        )

    if not base_url:
        raise RuntimeError(
            "DEEPCODE_BASE_URL не указан в prototype/.env"
        )

    if not model_name:
        raise RuntimeError(
            "DEEPCODE_MODEL не указан в prototype/.env"
        )

    print(f"[LLM] Используется модель: {model_name}")
    print(f"[LLM] API: {base_url}")

    return ChatOpenAI(
        model=model_name,
        base_url=base_url,
        api_key=api_key,

        # Для агента желательно получать максимально
        # стабильные и предсказуемые решения.
        temperature=0,

        # Ограничиваем максимальное ожидание ответа модели.
        timeout=60,

        # При временной сетевой ошибке разрешаем
        # одну автоматическую повторную попытку.
        max_retries=1,
    )