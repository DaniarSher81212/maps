"""
MAPS — Material Allocation & Planning System
Система автоматизированного распределения материалов
для строительно-монтажных работ.

Структура пакета:
    app/core/         — конфигурация и логирование
    app/db/           — подключение к PostgreSQL, определение таблиц
    app/models/       — Pydantic-схемы для валидации данных
    app/repositories/ — слой работы с БД (CRUD операции)
    app/allocation/   — алгоритм распределения материалов
    app/services/     — сервисы импорта/экспорта Excel
    app/utils/        — вспомогательные утилиты
"""
