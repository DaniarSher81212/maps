"""
main.py — Точка входа в систему MAPS (CLI)

Что такое CLI?
    Command Line Interface — интерфейс командной строки.
    Вместо кнопок и форм — команды в терминале.
    Пример: maps allocate  — запускает распределение.

Почему Typer?
    Typer — современная Python-библиотека для CLI.
    Преимущества:
      - Автоматически генерирует --help для каждой команды
      - Типизация через аннотации Python
      - Красивый вывод через Rich (цвета, таблицы)

Полный список команд:
    ─────────────────────────────────────────────────────────────
    Инициализация:
        maps init                 — создать таблицы в PostgreSQL

    Импорт данных (порядок важен!):
        maps import-requirements FILE     — потребности (обычные работы)
        maps import-emergency FILE        — аварийные работы (наивысший приоритет)
        maps import-stock FILE            — складские остатки (партии)
        maps import-supplies FILE         — поставки (материалы в пути)
        maps import-writeoffs FILE        — фактические списания (Слой 1)
        maps import-issued FILE           — выдано не списано (Слой 2)

    Алгоритм:
        maps allocate             — запустить распределение
        maps export               — экспортировать текущие результаты в Excel

    Утилиты:
        maps status               — состояние базы данных
        maps show                 — детали текущей сессии
    ─────────────────────────────────────────────────────────────

Рекомендуемый порядок работы:
    1. maps init
    2. maps import-requirements data/requirements.xlsx
    3. maps import-emergency data/emergency.xlsx        (если есть)
    4. maps import-stock data/stock.xlsx
    5. maps import-supplies data/supplies.xlsx          (если есть)
    6. maps import-writeoffs data/writeoffs.xlsx        (если есть)
    7. maps import-issued data/issued.xlsx              (если есть)
    8. maps allocate
    9. maps export

Примеры:
    python main.py init
    python main.py import-requirements data/sample_requirements.xlsx
    python main.py allocate --session "план_2026_06"
    python main.py export
"""

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from app.core.config import settings

# Создаём экземпляр Typer — это и есть наше CLI-приложение
app = typer.Typer(
    name="maps",
    help=(
        "MAPS — Material Allocation & Planning System\n\n"
        "Система автоматического распределения материалов для строительно-монтажных работ.\n\n"
        "Порядок запуска: init → import-requirements → import-stock → allocate → export"
    ),
    add_completion=False,
)

# Rich Console — для красивого вывода с цветами и таблицами
console = Console()


# =============================================================================
# Команда: init — инициализация базы данных
# =============================================================================

@app.command()
def init() -> None:
    """
    Инициализировать базу данных: создать все таблицы.

    Выполняется ОДИН РАЗ при первом запуске системы.
    Безопасно вызывать повторно: существующие данные НЕ удаляются
    (таблицы создаются только если их нет).

    Пример:
        maps init
    """
    console.print(Panel(
        f"[bold]Подключение к PostgreSQL[/bold]\n"
        f"Хост: {settings.db_host}:{settings.db_port}\n"
        f"База: {settings.db_name}",
        title="MAPS — Инициализация",
    ))

    from app.db.database import check_connection, create_all_tables

    with console.status("Проверяем подключение к PostgreSQL..."):
        if not check_connection():
            console.print("[red]✗ Не удалось подключиться к PostgreSQL![/red]")
            console.print(
                "\nПроверьте настройки в файле .env:\n"
                f"  DB_HOST={settings.db_host}\n"
                f"  DB_PORT={settings.db_port}\n"
                f"  DB_NAME={settings.db_name}\n"
                f"  DB_USER={settings.db_user}"
            )
            raise typer.Exit(code=1)

    console.print("[green]✓ Подключение успешно[/green]")

    with console.status("Создаём таблицы..."):
        create_all_tables()

    console.print("[green]✓ Все таблицы созданы/проверены[/green]")
    console.print(
        "\nСистема готова к работе! Следующий шаг:\n"
        "  [cyan]maps import-requirements data/sample_requirements.xlsx[/cyan]"
    )


@app.command(name="reset-data")
def reset_data() -> None:
    """
    Полный сброс всех данных БД (таблицы остаются, данные удаляются).

    Используется для повторного запуска тестов без пересоздания схемы.
    Порядок очистки учитывает FK-зависимости: сначала зависимые таблицы.

    ВНИМАНИЕ: операция необратима — все загруженные данные будут удалены!

    Пример:
        maps reset-data
    """
    console.print("[bold yellow]Сброс всех данных БД...[/bold yellow]")

    from app.db.database import get_session
    from sqlalchemy import text

    # Таблицы перечислены в порядке зависимостей:
    # сначала таблицы, у которых нет зависящих от них других таблиц,
    # затем родительские (orders matters due to FK).
    tables = (
        "stock_movements",
        "allocation_results",
        "deficit_records",
        "requirements",
        "writeoffs",
        "issued_not_written_off",
        "supply_lines",
        "supplies",
        "stock_batches",
        "warehouses",
        "works",
        "materials",
        "allocation_sessions",
    )

    with get_session() as session:
        for table in tables:
            session.execute(text(f"TRUNCATE {table} RESTART IDENTITY CASCADE"))
        # Транзакция коммитится при выходе из get_session() (если нет исключений)

    console.print("[green]✓ Все данные удалены, последовательности сброшены[/green]")
    console.print("Теперь можно заново импортировать данные:")
    console.print("  [cyan]maps import-requirements ...[/cyan]")


# =============================================================================
# Команды импорта данных
# =============================================================================

@app.command(name="import-requirements")
def import_requirements_cmd(
    file: Path = typer.Argument(..., help="Путь к Excel файлу с потребностями"),
    sheet: str = typer.Option("0", "--sheet", "-s", help="Имя или номер листа (по умолчанию первый)"),
) -> None:
    """
    Импортировать потребности обычных работ в материалах из Excel.

    Формат файла (заголовки колонок):
        Код работы, Системный номер, Потребность, Прогнозная цена,
        Дата начала, Дата окончания, Приоритет, Филиал, Завод, ...

    Прогнозная цена (колонка «Прогнозная цена»):
        Цена за единицу из исходной заявки.
        Используется для расчёта обеспечённости по стоимости
        и стоимости «К закупу» в итоговом отчёте.

    Пример:
        maps import-requirements data/requirements.xlsx
        maps import-requirements data/requirements.xlsx --sheet "Потребности"
    """
    if not file.exists():
        console.print(f"[red]✗ Файл не найден: {file}[/red]")
        raise typer.Exit(code=1)

    console.print(f"Импорт потребностей из: [cyan]{file}[/cyan]")

    from app.services.import_service import import_requirements

    with console.status("Импортируем данные..."):
        try:
            sheet_name: int | str = int(sheet) if sheet.isdigit() else sheet
            stats = import_requirements(file, sheet_name=sheet_name)
        except Exception as e:
            console.print(f"[red]✗ Ошибка импорта: {e}[/red]")
            raise typer.Exit(code=1)

    _print_import_stats("Потребности", stats)


@app.command(name="import-emergency")
def import_emergency_cmd(
    file: Path = typer.Argument(..., help="Путь к Excel файлу с аварийными работами"),
    sheet: str = typer.Option("0", "--sheet", "-s", help="Имя или номер листа"),
) -> None:
    """
    Импортировать аварийные работы из отдельного Excel файла.

    Что такое аварийные работы?
        Аварии на оборудовании, нарушения безопасности, критичные инциденты.
        Они получают наивысший приоритет в очереди распределения материалов —
        ПЕРЕД всеми обычными работами, вне зависимости от их дат и приоритетов.

    Формат файла:
        Такой же, как для обычных потребностей (import-requirements).
        Все работы в этом файле автоматически помечаются как аварийные.

    Сортировка аварийных работ:
        Только по дате начала (ASC). Поле «Приоритет» не учитывается.

    Пример:
        maps import-emergency data/emergency_works.xlsx
    """
    if not file.exists():
        console.print(f"[red]✗ Файл не найден: {file}[/red]")
        raise typer.Exit(code=1)

    console.print(f"Импорт [bold red]АВАРИЙНЫХ[/bold red] работ из: [cyan]{file}[/cyan]")

    from app.services.import_service import import_emergency_works

    with console.status("Импортируем аварийные работы..."):
        try:
            sheet_name: int | str = int(sheet) if sheet.isdigit() else sheet
            stats = import_emergency_works(file, sheet_name=sheet_name)
        except Exception as e:
            console.print(f"[red]✗ Ошибка импорта: {e}[/red]")
            raise typer.Exit(code=1)

    _print_import_stats("Аварийные работы", stats)


@app.command(name="import-works")
def import_works_cmd(
    file: Path = typer.Argument(..., help="Путь к Excel файлу с перечнем работ"),
    sheet: str = typer.Option("0", "--sheet", "-s", help="Имя или номер листа"),
) -> None:
    """
    Импортировать перечень работ с наименованиями из Excel.

    Файл содержит мастер-данные: наименование работы, даты начала/окончания,
    филиал, завод. Работы создаются или обновляются (по коду работы).

    Загружается ДО import-requirements чтобы при создании работ через потребности
    наименования уже были в БД. Можно загружать и ПОСЛЕ — обновит существующие.

    Не удаляет существующие данные — только добавляет/обновляет работы.
    Поля обновляются только если в файле передано непустое значение.

    Формат файла:
        Код работы, Наименование работы, Дата начала, Дата окончания,
        Филиал, Завод, Приоритет, Статус, Тип работы

    Пример:
        maps import-works data/works_list.xlsx
        maps import-works data/works_list.xlsx --sheet "Перечень работ"
    """
    if not file.exists():
        console.print(f"[red]✗ Файл не найден: {file}[/red]")
        raise typer.Exit(code=1)

    console.print(f"Импорт перечня работ из: [cyan]{file}[/cyan]")

    from app.services.import_service import import_works

    with console.status("Импортируем перечень работ..."):
        try:
            sheet_name: int | str = int(sheet) if sheet.isdigit() else sheet
            stats = import_works(file, sheet_name=sheet_name)
        except Exception as e:
            console.print(f"[red]✗ Ошибка импорта: {e}[/red]")
            raise typer.Exit(code=1)

    _print_import_stats("Перечень работ", stats)


@app.command(name="import-stock")
def import_stock_cmd(
    file: Path = typer.Argument(..., help="Путь к Excel файлу со складскими остатками"),
    sheet: str = typer.Option("0", "--sheet", "-s", help="Имя или номер листа"),
) -> None:
    """
    Импортировать складские остатки (партии материалов) из Excel.

    Формат файла:
        Код склада, Системный номер, Количество, Стоимость за ед,
        Номер партии, Дата поступления, Филиал склада, Завод склада, ...

    ВНИМАНИЕ: Существующие складские остатки будут УДАЛЕНЫ и заменены
    данными из файла! Это соответствует логике «снимок склада на текущий момент».

    Пример:
        maps import-stock data/stock.xlsx
    """
    if not file.exists():
        console.print(f"[red]✗ Файл не найден: {file}[/red]")
        raise typer.Exit(code=1)

    console.print(f"Импорт складских остатков из: [cyan]{file}[/cyan]")
    console.print("[yellow]⚠ Существующие остатки будут заменены[/yellow]")

    from app.services.import_service import import_stock

    with console.status("Импортируем данные..."):
        try:
            sheet_name: int | str = int(sheet) if sheet.isdigit() else sheet
            stats = import_stock(file, sheet_name=sheet_name)
        except Exception as e:
            console.print(f"[red]✗ Ошибка импорта: {e}[/red]")
            raise typer.Exit(code=1)

    _print_import_stats("Складские остатки", stats)


@app.command(name="import-supplies")
def import_supplies_cmd(
    file: Path = typer.Argument(..., help="Путь к Excel файлу с поставками"),
    sheet: str = typer.Option("0", "--sheet", "-s", help="Имя или номер листа"),
) -> None:
    """
    Импортировать поставки (материалы в пути) из Excel.

    Поставки — это материалы, заказанные по договору, которые ещё не прибыли.
    Алгоритм резервирует их для работ (Слой 4 распределения).

    Важно: Поставки привязаны к ФИЛИАЛУ.
    Работа получает поставки только своего филиала (work.filial == supply.filial).

    Формат файла:
        Договор, Поставщик, Филиал, Дата поставки,
        Системный номер, Количество, Стоимость за ед, Статус

    ВНИМАНИЕ: Существующие поставки будут УДАЛЕНЫ и заменены!

    Пример:
        maps import-supplies data/supplies.xlsx
    """
    if not file.exists():
        console.print(f"[red]✗ Файл не найден: {file}[/red]")
        raise typer.Exit(code=1)

    console.print(f"Импорт поставок из: [cyan]{file}[/cyan]")
    console.print("[yellow]⚠ Существующие поставки будут заменены[/yellow]")

    from app.services.import_service import import_supplies

    with console.status("Импортируем данные..."):
        try:
            sheet_name: int | str = int(sheet) if sheet.isdigit() else sheet
            stats = import_supplies(file, sheet_name=sheet_name)
        except Exception as e:
            console.print(f"[red]✗ Ошибка импорта: {e}[/red]")
            raise typer.Exit(code=1)

    _print_import_stats("Поставки", stats)


@app.command(name="import-writeoffs")
def import_writeoffs_cmd(
    file: Path = typer.Argument(..., help="Путь к Excel файлу с фактическими списаниями"),
    sheet: str = typer.Option("0", "--sheet", "-s", help="Имя или номер листа"),
) -> None:
    """
    Импортировать фактические списания материалов на работы (Слой 1).

    Что такое фактические списания?
        Материалы, которые уже документально списаны на работу в SAP.
        Это самый надёжный источник покрытия потребности.
        Алгоритм обрабатывает их ПЕРВЫМИ.

    ВАЖНО: Сначала импортируйте потребности (import-requirements),
    иначе строки будут пропущены (работы не найдены в БД).

    Формат файла:
        Код работы, Системный номер, Количество,
        Стоимость за ед, Сумма, Номер документа, Дата списания

    ВНИМАНИЕ: Существующие записи списаний будут УДАЛЕНЫ и заменены!

    Пример:
        maps import-writeoffs data/writeoffs.xlsx
    """
    if not file.exists():
        console.print(f"[red]✗ Файл не найден: {file}[/red]")
        raise typer.Exit(code=1)

    console.print(f"Импорт фактических списаний (Слой 1) из: [cyan]{file}[/cyan]")
    console.print("[yellow]⚠ Существующие записи списаний будут заменены[/yellow]")

    from app.services.import_service import import_writeoffs

    with console.status("Импортируем данные..."):
        try:
            sheet_name: int | str = int(sheet) if sheet.isdigit() else sheet
            stats = import_writeoffs(file, sheet_name=sheet_name)
        except Exception as e:
            console.print(f"[red]✗ Ошибка импорта: {e}[/red]")
            raise typer.Exit(code=1)

    _print_import_stats("Фактические списания", stats)


@app.command(name="import-issued")
def import_issued_cmd(
    file: Path = typer.Argument(..., help="Путь к Excel файлу «Выдано не списано»"),
    sheet: str = typer.Option("0", "--sheet", "-s", help="Имя или номер листа"),
) -> None:
    """
    Импортировать данные «Выдано не списано» (Слой 2).

    Что такое «Выдано не списано»?
        Материал уже выдан со склада на объект (бригада получила физически),
        но документ списания в SAP ещё не оформлен.
        Алгоритм обрабатывает их ВТОРЫМИ (после фактических списаний).

    Поле «Код склада» — информативное. Остатки склада НЕ изменяются.

    ВАЖНО: Сначала импортируйте потребности (import-requirements).

    Формат файла:
        Код работы, Системный номер, Количество,
        Стоимость за ед, Сумма, Код склада, Дата выдачи

    ВНИМАНИЕ: Существующие записи будут УДАЛЕНЫ и заменены!

    Пример:
        maps import-issued data/issued_not_written_off.xlsx
    """
    if not file.exists():
        console.print(f"[red]✗ Файл не найден: {file}[/red]")
        raise typer.Exit(code=1)

    console.print(f"Импорт «Выдано не списано» (Слой 2) из: [cyan]{file}[/cyan]")
    console.print("[yellow]⚠ Существующие записи будут заменены[/yellow]")

    from app.services.import_service import import_issued_not_written_off

    with console.status("Импортируем данные..."):
        try:
            sheet_name: int | str = int(sheet) if sheet.isdigit() else sheet
            stats = import_issued_not_written_off(file, sheet_name=sheet_name)
        except Exception as e:
            console.print(f"[red]✗ Ошибка импорта: {e}[/red]")
            raise typer.Exit(code=1)

    _print_import_stats("Выдано не списано", stats)


# =============================================================================
# Команда: allocate — запуск распределения
# =============================================================================

@app.command()
def allocate(
    session_id: Optional[str] = typer.Option(
        None, "--session", "-s",
        help="ID сессии (по умолчанию генерируется автоматически как YYYYMMDD_HHMMSS_XXXXX)"
    ),
) -> None:
    """
    Запустить алгоритм распределения материалов по всем 5 слоям.

    Порядок обработки работ:
        1. Аварийные работы (по дате начала ASC)
        2. Обычные работы (по дате начала ASC — главный, затем приоритет)

    Слои распределения для каждой потребности:
        Слой 1: Фактические списания (WriteOff)
        Слой 2: Выдано не списано (IssuedNotWrittenOff)
        Слой 3: Склад своего завода → своего филиала (FIFO)
        Слой 4: Поставки своего филиала
        Слой 5: К закупу (дефицит)
        + Анализ: Возможное перемещение из других филиалов

    Результаты сохраняются в БД.
    Используйте 'maps export' для создания Excel-отчёта.

    Примеры:
        maps allocate
        maps allocate --session "сценарий_июнь_2026"
    """
    from app.allocation.engine import AllocationEngine
    from app.db.database import get_session

    console.print(Panel(
        "[bold]Запуск распределения материалов[/bold]\n\n"
        "Порядок обработки:\n"
        "  1. Аварийные работы (is_emergency=True)\n"
        "  2. Обычные работы (по дате начала → приоритет)\n\n"
        "Слои покрытия:\n"
        "  Слой 1: Фактические списания\n"
        "  Слой 2: Выдано не списано\n"
        "  Слой 3: Склад (свой завод/филиал)\n"
        "  Слой 4: Поставки (свой филиал)\n"
        "  Слой 5: К закупу",
        title="MAPS — Распределение",
    ))

    with console.status("Выполняется распределение..."):
        try:
            with get_session() as session:
                from app.api.routes.settings import get_or_create_settings
                planning_year = get_or_create_settings(session).planning_year
                engine = AllocationEngine(session, session_id=session_id, planning_year=planning_year)
                result_session = engine.run()
                # Считываем атрибуты пока сессия ещё открыта —
                # после выхода из блока объект отсоединяется от сессии
                sid            = result_session.id
                total_req      = result_session.total_requirements
                total_alloc    = result_session.total_allocated
                total_deficit  = result_session.total_deficit
        except Exception as e:
            console.print(f"[red]✗ Ошибка распределения: {e}[/red]")
            raise typer.Exit(code=1)

    console.print(f"\n[green]✓ Распределение завершено![/green]")
    console.print(f"  ID сессии: [cyan]{sid}[/cyan]")

    # Итоговая таблица результатов
    table = Table(title="Результаты распределения")
    table.add_column("Показатель", style="bold")
    table.add_column("Значение", justify="right")
    table.add_row("Обработано потребностей", str(total_req))
    table.add_row("Строк распределения",    str(total_alloc))
    table.add_row(
        "Позиций дефицита",
        str(total_deficit),
        style="red" if total_deficit > 0 else "green",
    )
    console.print(table)

    console.print(f"\nДля экспорта в Excel:")
    console.print(f"  [cyan]maps export[/cyan]")


# =============================================================================
# Команда: export — экспорт результатов в Excel
# =============================================================================

@app.command()
def export(
    output_dir: Optional[str] = typer.Option(
        None, "--output", "-o",
        help=f"Папка для сохранения (по умолчанию: {settings.export_dir})"
    ),
) -> None:
    """
    Экспортировать результаты текущего распределения в Excel (4 листа).

    Всегда экспортирует единственную (текущую) сессию.
    Если распределение ещё не запускалось — сообщает об этом.

    Структура Excel-отчёта:
        1. «Распределение» — широкая таблица: все слои для каждой потребности.
           Колонки: Работа | Материал | Потребность | Сп-е | Выд-но | Склад | Поставки | К закупу | %
        2. «Движение склада» — детальное движение по партиям
        3. «Остатки складов» — что осталось на складах после распределения
        4. «Возможное перемещение» — анализ: склады других филиалов

    Пример:
        maps export
        maps export --output /tmp/reports
    """
    from sqlalchemy import text

    from app.db.database import get_session
    from app.services.export_service import export_allocation_results

    # Находим единственную текущую сессию
    try:
        with get_session() as session:
            row = session.execute(text(
                "SELECT id FROM allocation_sessions ORDER BY started_at DESC LIMIT 1"
            )).fetchone()
    except Exception as e:
        console.print(f"[red]✗ Ошибка подключения к БД: {e}[/red]")
        raise typer.Exit(code=1)

    if row is None:
        console.print("[yellow]Нет данных для экспорта. Сначала запустите: maps allocate[/yellow]")
        raise typer.Exit(code=1)

    session_id = row[0]
    console.print(f"Экспорт текущей сессии: [cyan]{session_id}[/cyan]")

    with console.status("Формируем Excel отчёт (4 листа)..."):
        try:
            file_path = export_allocation_results(session_id, output_dir=output_dir)
        except Exception as e:
            console.print(f"[red]✗ Ошибка экспорта: {e}[/red]")
            raise typer.Exit(code=1)

    console.print(f"[green]✓ Файл создан: {file_path}[/green]")
    console.print("\nСтруктура отчёта:")
    console.print("  1. «Распределение» — главная широкая таблица")
    console.print("  2. «Движение склада» — детально по партиям")
    console.print("  3. «Остатки складов» — что осталось")
    console.print("  4. «Возможное перемещение» — анализ других филиалов")


# =============================================================================
# Команда: status — состояние системы
# =============================================================================

@app.command()
def status() -> None:
    """
    Показать текущее состояние базы данных MAPS.

    Выводит количество записей в каждой таблице.

    Пример:
        maps status
    """
    from sqlalchemy import text

    from app.db.database import get_session

    console.print(Panel("[bold]Состояние базы данных MAPS[/bold]", title="MAPS — Статус"))

    try:
        with get_session() as session:
            # Список таблиц для подсчёта записей
            tables = [
                # (имя_таблицы_в_бд, понятное_название)
                ("works",                     "Работы (всего)"),
                ("works",                     "Аварийные работы",    "WHERE is_emergency = TRUE"),
                ("materials",                 "Материалы"),
                ("requirements",              "Потребности"),
                ("warehouses",                "Склады"),
                ("stock_batches",             "Партии на складах"),
                ("supplies",                  "Поставки"),
                ("supply_lines",              "Строки поставок"),
                ("writeoffs",                 "Фактические списания (Слой 1)"),
                ("issued_not_written_off",    "Выдано не списано (Слой 2)"),
                ("allocation_sessions",       "Сессии распределения"),
                ("allocation_results",        "Строки распределения"),
                ("deficit_records",           "Записи дефицита (К закупу)"),
            ]

            table = Table(title="Количество записей в таблицах")
            table.add_column("Таблица / Группа", style="bold")
            table.add_column("Записей", justify="right")

            for entry in tables:
                tbl_name = entry[0]
                label    = entry[1]
                where    = entry[2] if len(entry) > 2 else ""
                try:
                    # noqa: S608 — запрос не содержит пользовательских данных
                    sql = f"SELECT COUNT(*) FROM {tbl_name} {where}"  # noqa: S608
                    count = session.execute(text(sql)).scalar()
                    color = "green" if count and count > 0 else "dim"
                    table.add_row(label, f"[{color}]{count}[/{color}]")
                except Exception:
                    table.add_row(label, "[red]ошибка[/red]")

            console.print(table)

    except Exception as e:
        console.print(f"[red]✗ Ошибка подключения к БД: {e}[/red]")
        raise typer.Exit(code=1)


# =============================================================================
# Команда: show — детальный просмотр текущей сессии
# =============================================================================

@app.command()
def show() -> None:
    """
    Показать детальную информацию о текущей (единственной) сессии распределения.

    Выводит:
        - Общую статистику сессии
        - Топ-10 материалов в дефиците
        - Итоги по слоям покрытия

    Если распределение ещё не запускалось — сообщает об этом.

    Пример:
        maps show
    """
    from sqlalchemy import text

    from app.db.database import get_session as db_session

    try:
        with db_session() as session:
            # --- Находим текущую (единственную) сессию ---
            sess_row = session.execute(text("""
                SELECT id, status, started_at, completed_at,
                       total_requirements, total_allocated, total_deficit
                FROM allocation_sessions
                ORDER BY started_at DESC
                LIMIT 1
            """)).fetchone()

            if sess_row is None:
                console.print("[yellow]Нет данных. Сначала запустите: maps allocate[/yellow]")
                raise typer.Exit(code=0)

            session_id = sess_row.id

            # --- Топ дефицитных материалов ---
            deficit_rows = session.execute(text("""
                SELECT m.sys_nomer, m.naimenovanie, d.deficit_qty, m.ed_izm
                FROM deficit_records d
                JOIN materials m ON m.id = d.material_id
                WHERE d.session_id = :sid
                ORDER BY d.deficit_qty DESC
                LIMIT 10
            """), {"sid": session_id}).fetchall()

            # --- Итоги по слоям ---
            # allocation_results: istochnik = тип источника, kolichestvo = количество
            layer_rows = session.execute(text("""
                SELECT istochnik,
                       COUNT(*)         AS cnt,
                       SUM(kolichestvo) AS total_qty
                FROM allocation_results
                WHERE session_id = :sid
                GROUP BY istochnik
                ORDER BY istochnik
            """), {"sid": session_id}).fetchall()

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    # --- Вывод: шапка ---
    status_color = "green" if sess_row.status == "completed" else "red"
    console.print(Panel(
        f"ID: [cyan]{sess_row.id}[/cyan]\n"
        f"Статус: [{status_color}]{sess_row.status}[/{status_color}]\n"
        f"Запущена:  {str(sess_row.started_at)[:19]   if sess_row.started_at   else '—'}\n"
        f"Завершена: {str(sess_row.completed_at)[:19] if sess_row.completed_at else '—'}",
        title="Текущая сессия распределения",
    ))

    # --- Вывод: общая статистика ---
    stats_table = Table(show_header=False, box=None, padding=(0, 2))
    stats_table.add_column("Показатель", style="bold")
    stats_table.add_column("Значение",   justify="right")
    stats_table.add_row("Обработано потребностей", str(sess_row.total_requirements or 0))
    stats_table.add_row("Строк распределения",     str(sess_row.total_allocated   or 0))
    deficit_str = str(sess_row.total_deficit or 0)
    deficit_color = "red" if (sess_row.total_deficit or 0) > 0 else "green"
    stats_table.add_row(
        "Позиций дефицита (К закупу)",
        f"[{deficit_color}]{deficit_str}[/{deficit_color}]",
    )
    console.print(stats_table)

    # --- Вывод: слои ---
    if layer_rows:
        # istochnik — название источника (напр. "sklad", "postavka", "deficit")
        layer_table = Table(title="Итоги по слоям (источники покрытия)")
        layer_table.add_column("Источник",         style="bold")
        layer_table.add_column("Строк",            justify="right")
        layer_table.add_column("Количество всего", justify="right")

        for lr in layer_rows:
            qty = f"{lr.total_qty:,.2f}" if lr.total_qty else "0"
            layer_table.add_row(lr.istochnik or "—", str(lr.cnt), qty)
        console.print(layer_table)

    # --- Вывод: топ дефицита ---
    if deficit_rows:
        def_table = Table(title="Топ-10 материалов в дефиците")
        def_table.add_column("Системный номер", style="cyan")
        def_table.add_column("Наименование")
        def_table.add_column("Дефицит",  justify="right", style="red")
        def_table.add_column("Ед.изм",   justify="center")

        for dr in deficit_rows:
            naim = dr.naimenovanie or ""
            def_table.add_row(
                dr.sys_nomer,
                naim[:50] + "…" if len(naim) > 50 else naim,
                f"{dr.deficit_qty:,.2f}",
                dr.ed_izm or "",
            )
        console.print(def_table)
    else:
        console.print("[green]✓ Дефицитных позиций нет![/green]")

    console.print(
        f"\nДля экспорта в Excel: [cyan]maps export[/cyan]"
    )


# =============================================================================
# ГРУППА: Просмотр данных (чтение из БД)
# =============================================================================

@app.command(name="work-list")
def work_list(
    emergency: bool = typer.Option(False, "--emergency", "-e", help="Показать только аварийные работы"),
    filial: Optional[str] = typer.Option(None, "--filial", "-f", help="Фильтр по филиалу (точное совпадение)"),
    status: Optional[str] = typer.Option(None, "--status", help="Фильтр по статусу: active / completed / cancelled / on_hold"),
    limit: int = typer.Option(50, "--limit", "-n", help="Максимальное количество строк"),
) -> None:
    """
    Показать список работ из базы данных.

    По умолчанию выводит первые 50 работ, отсортированных по дате начала.
    Аварийные работы помечаются красным.

    Примеры:
        maps work-list
        maps work-list --emergency
        maps work-list --filial "Алматы"
        maps work-list --status active --limit 100
    """
    from sqlalchemy import text
    from app.db.database import get_session

    try:
        with get_session() as session:
            # Строим WHERE-условия динамически
            conditions = []
            params: dict = {}
            if emergency:
                conditions.append("is_emergency = TRUE")
            if filial:
                conditions.append("filial = :filial")
                params["filial"] = filial
            if status:
                conditions.append("status = :status")
                params["status"] = status
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            sql = f"""
                SELECT kod_raboty, nazvanie, is_emergency, filial, zavod,
                       data_nachala, data_okonchaniya, prioritet, status
                FROM works {where}
                ORDER BY is_emergency DESC, data_nachala ASC NULLS LAST
                LIMIT :limit
            """  # noqa: S608
            params["limit"] = limit
            rows = session.execute(text(sql), params).fetchall()
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    if not rows:
        console.print("[yellow]Работы не найдены (таблица пуста или не совпадают фильтры)[/yellow]")
        return

    table = Table(title=f"Работы (показано: {len(rows)})", show_lines=False)
    table.add_column("Код работы", style="cyan", no_wrap=True)
    table.add_column("Наименование")
    table.add_column("Аварийная", justify="center")
    table.add_column("Филиал")
    table.add_column("Завод")
    table.add_column("Дата начала")
    table.add_column("Дата конца")
    table.add_column("Пр-т", justify="center")
    table.add_column("Статус")

    for r in rows:
        emerg = "[red]ДА[/red]" if r.is_emergency else "нет"
        d_start = r.data_nachala.strftime("%d.%m.%Y") if r.data_nachala else "—"
        d_end   = r.data_okonchaniya.strftime("%d.%m.%Y") if r.data_okonchaniya else "—"
        nazvanie = (r.nazvanie or "")[:40] + ("…" if (r.nazvanie or "") and len(r.nazvanie) > 40 else "")
        table.add_row(
            r.kod_raboty, nazvanie, emerg,
            r.filial or "—", r.zavod or "—",
            d_start, d_end,
            str(r.prioritet), r.status or "—",
        )
    console.print(table)


@app.command(name="work-show")
def work_show(
    kod: str = typer.Argument(..., help="Код работы (например: W-001-2026)"),
) -> None:
    """
    Показать полную информацию об одной работе и её потребностях.

    Выводит все поля работы и таблицу потребностей:
    какие материалы нужны, сколько распределено, сколько в дефиците.

    Пример:
        maps work-show W-001-2026
    """
    from sqlalchemy import text
    from app.db.database import get_session

    try:
        with get_session() as session:
            work_row = session.execute(text("""
                SELECT id, kod_raboty, nazvanie, is_emergency, filial, podrazdelenie,
                       zavod, data_nachala, data_okonchaniya, prioritet, status
                FROM works WHERE kod_raboty = :kod
            """), {"kod": kod}).fetchone()

            if not work_row:
                console.print(f"[red]✗ Работа не найдена: {kod}[/red]")
                raise typer.Exit(code=1)

            req_rows = session.execute(text("""
                SELECT m.sys_nomer, m.naimenovanie, m.ed_izm,
                       r.potrebnost, r.raspredeleno, r.deficit, r.prognosnaya_tsena
                FROM requirements r
                JOIN materials m ON m.id = r.material_id
                WHERE r.work_id = :wid
                ORDER BY r.deficit DESC, r.potrebnost DESC
            """), {"wid": work_row.id}).fetchall()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    emerg_label = "[bold red]АВАРИЙНАЯ[/bold red]" if work_row.is_emergency else "нет"
    d_start = work_row.data_nachala.strftime("%d.%m.%Y") if work_row.data_nachala else "—"
    d_end   = work_row.data_okonchaniya.strftime("%d.%m.%Y") if work_row.data_okonchaniya else "—"

    console.print(Panel(
        f"Код:           [cyan]{work_row.kod_raboty}[/cyan]\n"
        f"Наименование:  {work_row.nazvanie or '—'}\n"
        f"Аварийная:     {emerg_label}\n"
        f"Филиал:        {work_row.filial or '—'}\n"
        f"Подразделение: {work_row.podrazdelenie or '—'}\n"
        f"Завод:         {work_row.zavod or '—'}\n"
        f"Дата начала:   {d_start}\n"
        f"Дата конца:    {d_end}\n"
        f"Приоритет:     {work_row.prioritet}\n"
        f"Статус:        {work_row.status or '—'}",
        title=f"Работа: {work_row.kod_raboty}",
    ))

    if not req_rows:
        console.print("[yellow]Потребностей нет[/yellow]")
        return

    table = Table(title=f"Потребности ({len(req_rows)} позиций)")
    table.add_column("Системный номер", style="cyan")
    table.add_column("Наименование")
    table.add_column("Ед.изм", justify="center")
    table.add_column("Потребность", justify="right")
    table.add_column("Распределено", justify="right")
    table.add_column("Дефицит", justify="right")
    table.add_column("Прогн.цена", justify="right")

    for r in req_rows:
        naim = (r.naimenovanie or "")[:40] + ("…" if len(r.naimenovanie or "") > 40 else "")
        deficit_str = f"[red]{r.deficit:,.4f}[/red]" if r.deficit > 0 else f"[green]{r.deficit:,.4f}[/green]"
        table.add_row(
            r.sys_nomer, naim, r.ed_izm or "—",
            f"{r.potrebnost:,.4f}", f"{r.raspredeleno:,.4f}",
            deficit_str, f"{r.prognosnaya_tsena:,.4f}",
        )
    console.print(table)


@app.command(name="material-list")
def material_list(
    filter_text: Optional[str] = typer.Option(None, "--filter", "-q", help="Поиск по наименованию или системному номеру (ILIKE)"),
    group: Optional[str] = typer.Option(None, "--group", "-g", help="Фильтр по группе материала"),
    limit: int = typer.Option(50, "--limit", "-n", help="Максимальное количество строк"),
) -> None:
    """
    Показать список материалов из справочника.

    Примеры:
        maps material-list
        maps material-list --filter "кабель"
        maps material-list --group "Кабельная продукция" --limit 100
    """
    from sqlalchemy import text
    from app.db.database import get_session

    try:
        with get_session() as session:
            conditions = []
            params: dict = {"limit": limit}
            if filter_text:
                conditions.append("(LOWER(naimenovanie) LIKE LOWER(:q) OR sys_nomer LIKE :q)")
                params["q"] = f"%{filter_text}%"
            if group:
                conditions.append("gruppa = :group")
                params["group"] = group
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            rows = session.execute(text(
                f"SELECT sys_nomer, naimenovanie, ed_izm, gruppa FROM materials {where} "  # noqa: S608
                f"ORDER BY naimenovanie LIMIT :limit"
            ), params).fetchall()
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    if not rows:
        console.print("[yellow]Материалы не найдены[/yellow]")
        return

    table = Table(title=f"Материалы (показано: {len(rows)})")
    table.add_column("Системный номер", style="cyan", no_wrap=True)
    table.add_column("Наименование")
    table.add_column("Ед.изм", justify="center")
    table.add_column("Группа")

    for r in rows:
        naim = (r.naimenovanie or "")[:60] + ("…" if len(r.naimenovanie or "") > 60 else "")
        table.add_row(r.sys_nomer, naim, r.ed_izm or "—", r.gruppa or "—")
    console.print(table)


@app.command(name="req-list")
def req_list(
    work: Optional[str] = typer.Option(None, "--work", "-w", help="Фильтр по коду работы"),
    material: Optional[str] = typer.Option(None, "--material", "-m", help="Фильтр по системному номеру материала"),
    deficit_only: bool = typer.Option(False, "--deficit-only", "-d", help="Показать только позиции с дефицитом"),
    limit: int = typer.Option(50, "--limit", "-n", help="Максимальное количество строк"),
) -> None:
    """
    Показать потребности (связки работа–материал).

    Примеры:
        maps req-list --work W-001
        maps req-list --deficit-only
        maps req-list --material "000000000010012345"
    """
    from sqlalchemy import text
    from app.db.database import get_session

    try:
        with get_session() as session:
            conditions = []
            params: dict = {"limit": limit}
            if work:
                conditions.append("w.kod_raboty = :work")
                params["work"] = work
            if material:
                conditions.append("m.sys_nomer = :material")
                params["material"] = material
            if deficit_only:
                conditions.append("r.deficit > 0")
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            rows = session.execute(text(f"""
                SELECT w.kod_raboty, m.sys_nomer, m.naimenovanie, m.ed_izm,
                       r.potrebnost, r.raspredeleno, r.deficit, r.prognosnaya_tsena
                FROM requirements r
                JOIN works w ON w.id = r.work_id
                JOIN materials m ON m.id = r.material_id
                {where}
                ORDER BY r.deficit DESC, w.kod_raboty, m.sys_nomer
                LIMIT :limit
            """), params).fetchall()  # noqa: S608
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    if not rows:
        console.print("[yellow]Потребности не найдены[/yellow]")
        return

    table = Table(title=f"Потребности (показано: {len(rows)})")
    table.add_column("Код работы", style="cyan", no_wrap=True)
    table.add_column("Системный номер", no_wrap=True)
    table.add_column("Наименование")
    table.add_column("Ед.изм", justify="center")
    table.add_column("Потребность", justify="right")
    table.add_column("Распределено", justify="right")
    table.add_column("Дефицит", justify="right")

    for r in rows:
        naim = (r.naimenovanie or "")[:40] + ("…" if len(r.naimenovanie or "") > 40 else "")
        d_str = f"[red]{r.deficit:,.4f}[/red]" if r.deficit > 0 else f"{r.deficit:,.4f}"
        table.add_row(
            r.kod_raboty, r.sys_nomer, naim, r.ed_izm or "—",
            f"{r.potrebnost:,.4f}", f"{r.raspredeleno:,.4f}", d_str,
        )
    console.print(table)


@app.command(name="warehouse-list")
def warehouse_list(
    filial: Optional[str] = typer.Option(None, "--filial", "-f", help="Фильтр по филиалу"),
) -> None:
    """
    Показать список складов с остатками.

    Пример:
        maps warehouse-list
        maps warehouse-list --filial "Алматы"
    """
    from sqlalchemy import text
    from app.db.database import get_session

    try:
        with get_session() as session:
            params: dict = {}
            where = ""
            if filial:
                where = "WHERE w.filial = :filial"
                params["filial"] = filial
            rows = session.execute(text(f"""
                SELECT w.id, w.kod_sklada, w.tip_sklada, w.filial, w.zavod,
                       COUNT(sb.id)        AS batch_count,
                       COALESCE(SUM(sb.dostupno), 0) AS total_dostupno
                FROM warehouses w
                LEFT JOIN stock_batches sb ON sb.warehouse_id = w.id
                {where}
                GROUP BY w.id, w.kod_sklada, w.tip_sklada, w.filial, w.zavod
                ORDER BY w.filial, w.kod_sklada
            """), params).fetchall()  # noqa: S608
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    if not rows:
        console.print("[yellow]Склады не найдены[/yellow]")
        return

    table = Table(title=f"Склады ({len(rows)})")
    table.add_column("ID", justify="right")
    table.add_column("Код склада", style="cyan")
    table.add_column("Тип")
    table.add_column("Филиал")
    table.add_column("Завод")
    table.add_column("Партий", justify="right")
    table.add_column("Доступно (сумм.)", justify="right")

    for r in rows:
        table.add_row(
            str(r.id), r.kod_sklada, r.tip_sklada or "—",
            r.filial or "—", r.zavod or "—",
            str(r.batch_count), f"{r.total_dostupno:,.2f}",
        )
    console.print(table)


@app.command(name="stock-list")
def stock_list(
    warehouse: Optional[str] = typer.Option(None, "--warehouse", "-w", help="Код склада (например: SLT-001)"),
    material: Optional[str] = typer.Option(None, "--material", "-m", help="Системный номер материала"),
    filial: Optional[str] = typer.Option(None, "--filial", "-f", help="Фильтр по филиалу склада"),
    available_only: bool = typer.Option(False, "--available-only", "-a", help="Только партии с dostupno > 0"),
    limit: int = typer.Option(50, "--limit", "-n", help="Максимальное количество строк"),
) -> None:
    """
    Показать складские остатки (партии).

    Примеры:
        maps stock-list --warehouse SLT-001
        maps stock-list --material "000000000010012345"
        maps stock-list --filial "Алматы" --available-only
    """
    from sqlalchemy import text
    from app.db.database import get_session

    try:
        with get_session() as session:
            conditions = []
            params: dict = {"limit": limit}
            if warehouse:
                conditions.append("wh.kod_sklada = :warehouse")
                params["warehouse"] = warehouse
            if material:
                conditions.append("m.sys_nomer = :material")
                params["material"] = material
            if filial:
                conditions.append("wh.filial = :filial")
                params["filial"] = filial
            if available_only:
                conditions.append("sb.dostupno > 0")
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            rows = session.execute(text(f"""
                SELECT wh.kod_sklada, wh.filial, m.sys_nomer, m.naimenovanie, m.ed_izm,
                       sb.nomer_partii, sb.kolichestvo, sb.dostupno,
                       sb.stoimost_za_ed, sb.data_postupleniya
                FROM stock_batches sb
                JOIN warehouses wh ON wh.id = sb.warehouse_id
                JOIN materials m ON m.id = sb.material_id
                {where}
                ORDER BY wh.kod_sklada, m.sys_nomer, sb.data_postupleniya
                LIMIT :limit
            """), params).fetchall()  # noqa: S608
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    if not rows:
        console.print("[yellow]Партии не найдены[/yellow]")
        return

    table = Table(title=f"Складские остатки (показано: {len(rows)})")
    table.add_column("Склад", style="cyan")
    table.add_column("Филиал")
    table.add_column("Системный номер", no_wrap=True)
    table.add_column("Наименование")
    table.add_column("Ед.изм", justify="center")
    table.add_column("Партия")
    table.add_column("Кол-во", justify="right")
    table.add_column("Доступно", justify="right")
    table.add_column("Цена", justify="right")
    table.add_column("Дата пост.", justify="center")

    for r in rows:
        naim = (r.naimenovanie or "")[:35] + ("…" if len(r.naimenovanie or "") > 35 else "")
        d = r.data_postupleniya.strftime("%d.%m.%Y") if r.data_postupleniya else "—"
        dostupno_color = "green" if r.dostupno > 0 else "dim"
        table.add_row(
            r.kod_sklada, r.filial or "—", r.sys_nomer, naim, r.ed_izm or "—",
            r.nomer_partii or "—", f"{r.kolichestvo:,.4f}",
            f"[{dostupno_color}]{r.dostupno:,.4f}[/{dostupno_color}]",
            f"{r.stoimost_za_ed:,.4f}", d,
        )
    console.print(table)


@app.command(name="supply-list")
def supply_list(
    filial: Optional[str] = typer.Option(None, "--filial", "-f", help="Фильтр по филиалу поставки"),
    status: Optional[str] = typer.Option(None, "--status", help="Фильтр по статусу: confirmed / cancelled"),
    limit: int = typer.Option(50, "--limit", "-n", help="Максимальное количество строк"),
) -> None:
    """
    Показать поставки (материалы в пути).

    Примеры:
        maps supply-list
        maps supply-list --filial "Алматы"
        maps supply-list --status confirmed
    """
    from sqlalchemy import text
    from app.db.database import get_session

    try:
        with get_session() as session:
            conditions = []
            params: dict = {"limit": limit}
            if filial:
                conditions.append("s.filial = :filial")
                params["filial"] = filial
            if status:
                conditions.append("s.status = :status")
                params["status"] = status
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            rows = session.execute(text(f"""
                SELECT s.id, s.dogovor, s.postavshchik, s.filial, s.zavod,
                       s.data_postavki, s.status,
                       COUNT(sl.id)        AS line_count,
                       COALESCE(SUM(sl.dostupno), 0) AS total_dostupno
                FROM supplies s
                LEFT JOIN supply_lines sl ON sl.supply_id = s.id
                {where}
                GROUP BY s.id, s.dogovor, s.postavshchik, s.filial, s.zavod,
                         s.data_postavki, s.status
                ORDER BY s.data_postavki, s.dogovor
                LIMIT :limit
            """), params).fetchall()  # noqa: S608
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    if not rows:
        console.print("[yellow]Поставки не найдены[/yellow]")
        return

    table = Table(title=f"Поставки (показано: {len(rows)})")
    table.add_column("ID", justify="right")
    table.add_column("Договор", style="cyan")
    table.add_column("Поставщик")
    table.add_column("Филиал")
    table.add_column("Завод")
    table.add_column("Дата пост.", justify="center")
    table.add_column("Статус")
    table.add_column("Позиций", justify="right")
    table.add_column("Доступно (сумм.)", justify="right")

    for r in rows:
        d = r.data_postavki.strftime("%d.%m.%Y") if r.data_postavki else "—"
        postavshchik = (r.postavshchik or "")[:30] + ("…" if len(r.postavshchik or "") > 30 else "")
        table.add_row(
            str(r.id), r.dogovor or "—", postavshchik,
            r.filial or "—", r.zavod or "—",
            d, r.status or "—",
            str(r.line_count), f"{r.total_dostupno:,.2f}",
        )
    console.print(table)


@app.command(name="session-list")
def session_list(
    limit: int = typer.Option(20, "--limit", "-n", help="Максимальное количество сессий"),
) -> None:
    """
    Показать список сессий распределения.

    Пример:
        maps session-list
        maps session-list --limit 5
    """
    from sqlalchemy import text
    from app.db.database import get_session

    try:
        with get_session() as session:
            rows = session.execute(text("""
                SELECT id, status, started_at, completed_at,
                       total_requirements, total_allocated, total_deficit
                FROM allocation_sessions
                ORDER BY started_at DESC
                LIMIT :limit
            """), {"limit": limit}).fetchall()
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    if not rows:
        console.print("[yellow]Сессий нет. Запустите: maps allocate[/yellow]")
        return

    table = Table(title=f"Сессии распределения (последние {limit})")
    table.add_column("ID сессии", style="cyan")
    table.add_column("Статус")
    table.add_column("Запущена")
    table.add_column("Завершена")
    table.add_column("Потребн.", justify="right")
    table.add_column("Строк", justify="right")
    table.add_column("Дефицит", justify="right")

    for r in rows:
        status_color = "green" if r.status == "completed" else "yellow"
        d_start = str(r.started_at)[:19] if r.started_at else "—"
        d_end   = str(r.completed_at)[:19] if r.completed_at else "—"
        deficit_color = "red" if (r.total_deficit or 0) > 0 else "green"
        table.add_row(
            r.id, f"[{status_color}]{r.status}[/{status_color}]",
            d_start, d_end,
            str(r.total_requirements or 0),
            str(r.total_allocated or 0),
            f"[{deficit_color}]{r.total_deficit or 0}[/{deficit_color}]",
        )
    console.print(table)


@app.command(name="deficit-list")
def deficit_list(
    session_id: Optional[str] = typer.Option(None, "--session", "-s", help="ID сессии (по умолчанию — последняя)"),
    material: Optional[str] = typer.Option(None, "--material", "-m", help="Фильтр по системному номеру"),
    limit: int = typer.Option(50, "--limit", "-n", help="Максимальное количество строк"),
) -> None:
    """
    Показать список дефицитных позиций («К закупу»).

    Примеры:
        maps deficit-list
        maps deficit-list --session 20260530_143055_a1b2c
        maps deficit-list --material "000000000010012345"
    """
    from sqlalchemy import text
    from app.db.database import get_session

    try:
        with get_session() as session:
            # Если ID сессии не указан — берём последнюю
            if not session_id:
                row = session.execute(text(
                    "SELECT id FROM allocation_sessions ORDER BY started_at DESC LIMIT 1"
                )).fetchone()
                if not row:
                    console.print("[yellow]Нет сессий. Запустите: maps allocate[/yellow]")
                    raise typer.Exit(code=0)
                session_id = row[0]

            conditions = ["d.session_id = :sid"]
            params: dict = {"sid": session_id, "limit": limit}
            if material:
                conditions.append("m.sys_nomer = :material")
                params["material"] = material
            where = "WHERE " + " AND ".join(conditions)

            rows = session.execute(text(f"""
                SELECT w.kod_raboty, m.sys_nomer, m.naimenovanie, m.ed_izm,
                       d.deficit_qty, d.estimated_cost, d.needed_by
                FROM deficit_records d
                JOIN works w ON w.id = d.work_id
                JOIN materials m ON m.id = d.material_id
                {where}
                ORDER BY d.deficit_qty DESC
                LIMIT :limit
            """), params).fetchall()  # noqa: S608
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    if not rows:
        console.print(f"[green]✓ Дефицита нет в сессии {session_id}[/green]")
        return

    table = Table(title=f"Дефицит — К закупу (сессия: {session_id}, показано: {len(rows)})")
    table.add_column("Код работы", style="cyan", no_wrap=True)
    table.add_column("Системный номер", no_wrap=True)
    table.add_column("Наименование")
    table.add_column("Ед.изм", justify="center")
    table.add_column("Дефицит", justify="right", style="red")
    table.add_column("Оценка стоим.", justify="right")
    table.add_column("Нужно к", justify="center")

    for r in rows:
        naim = (r.naimenovanie or "")[:40] + ("…" if len(r.naimenovanie or "") > 40 else "")
        d = r.needed_by.strftime("%d.%m.%Y") if r.needed_by else "—"
        table.add_row(
            r.kod_raboty, r.sys_nomer, naim, r.ed_izm or "—",
            f"{r.deficit_qty:,.4f}", f"{r.estimated_cost:,.2f}", d,
        )
    console.print(table)


@app.command(name="settings-show")
def settings_show() -> None:
    """
    Показать текущие системные настройки.

    Пример:
        maps settings-show
    """
    from sqlalchemy import text
    from app.db.database import get_session

    try:
        with get_session() as session:
            row = session.execute(text(
                "SELECT planning_year, updated_at FROM system_settings WHERE id = 1"
            )).fetchone()
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    if not row:
        console.print("[yellow]Настройки не инициализированы. Запустите: maps init[/yellow]")
        return

    console.print(Panel(
        f"Год планирования:  [cyan]{row.planning_year}[/cyan]\n"
        f"Обновлено:         {str(row.updated_at)[:19]}",
        title="Системные настройки MAPS",
    ))
    console.print(
        "\nДля изменения: [cyan]maps set-year 2027[/cyan]"
    )


# =============================================================================
# ГРУППА: Точечное изменение — Работы
# =============================================================================

@app.command(name="work-add")
def work_add(
    kod: str = typer.Argument(..., help="Уникальный код работы (из SAP PS/PM)"),
    nazvanie: Optional[str] = typer.Option(None, "--nazvanie", help="Наименование работы"),
    filial: Optional[str] = typer.Option(None, "--filial", "-f", help="Филиал (например: Алматы)"),
    zavod: Optional[str] = typer.Option(None, "--zavod", "-z", help="Код завода SAP (например: AL01)"),
    podrazdelenie: Optional[str] = typer.Option(None, "--podrazdelenie", help="Подразделение"),
    tip: Optional[str] = typer.Option(None, "--tip", help="Тип работы (например: Ремонт, Монтаж)"),
    priority: int = typer.Option(3, "--priority", "-p", help="Приоритет: 1 (высший), 2 (средний), 3 (низший)"),
    start: Optional[str] = typer.Option(None, "--start", help="Дата начала в формате ДД.ММ.ГГГГ"),
    end: Optional[str] = typer.Option(None, "--end", help="Дата окончания в формате ДД.ММ.ГГГГ"),
    emergency: bool = typer.Option(False, "--emergency", "-e", help="Пометить как аварийную работу"),
    status: str = typer.Option("active", "--status", help="Статус: active / completed / cancelled / on_hold"),
) -> None:
    """
    Добавить новую работу в базу данных.

    Если работа с таким кодом уже существует — выдаст ошибку (используйте work-update).

    Примеры:
        maps work-add W-2026-001 --nazvanie "Ремонт насоса ЦН-101" --filial "Алматы" --zavod "AL01" --priority 1 --start 01.06.2026 --end 30.06.2026
        maps work-add AVR-2026-007 --emergency --nazvanie "Авария трубопровода" --filial "Шымкент" --start 15.06.2026
    """
    from app.db.database import get_session
    from app.db.models import Work

    d_start = _parse_date(start) if start else None
    d_end   = _parse_date(end)   if end   else None

    try:
        with get_session() as session:
            # Проверяем, нет ли работы с таким кодом
            from sqlalchemy import text
            exists = session.execute(
                text("SELECT id FROM works WHERE kod_raboty = :kod"), {"kod": kod}
            ).fetchone()
            if exists:
                console.print(f"[red]✗ Работа с кодом {kod!r} уже существует. Используйте: maps work-update[/red]")
                raise typer.Exit(code=1)

            work = Work(
                kod_raboty=kod,
                nazvanie=nazvanie,
                filial=filial,
                zavod=zavod,
                podrazdelenie=podrazdelenie,
                tip_raboty=tip,
                prioritet=priority,
                data_nachala=d_start,
                data_okonchaniya=d_end,
                is_emergency=emergency,
                status=status,
            )
            session.add(work)
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    emerg_str = " [bold red][АВАРИЙНАЯ][/bold red]" if emergency else ""
    console.print(f"[green]✓ Работа добавлена: {kod}{emerg_str}[/green]")


@app.command(name="work-update")
def work_update(
    kod: str = typer.Argument(..., help="Код работы для обновления"),
    nazvanie: Optional[str] = typer.Option(None, "--nazvanie", help="Новое наименование"),
    filial: Optional[str] = typer.Option(None, "--filial", "-f", help="Новый филиал"),
    zavod: Optional[str] = typer.Option(None, "--zavod", "-z", help="Новый завод"),
    podrazdelenie: Optional[str] = typer.Option(None, "--podrazdelenie", help="Новое подразделение"),
    tip: Optional[str] = typer.Option(None, "--tip", help="Новый тип работы"),
    priority: Optional[int] = typer.Option(None, "--priority", "-p", help="Новый приоритет: 1 / 2 / 3"),
    start: Optional[str] = typer.Option(None, "--start", help="Новая дата начала ДД.ММ.ГГГГ"),
    end: Optional[str] = typer.Option(None, "--end", help="Новая дата окончания ДД.ММ.ГГГГ"),
    status: Optional[str] = typer.Option(None, "--status", help="Новый статус: active / completed / cancelled / on_hold"),
    emergency: Optional[bool] = typer.Option(None, "--emergency/--no-emergency", help="Изменить признак аварийности"),
) -> None:
    """
    Обновить поля существующей работы.

    Обновляются только переданные поля — остальные не меняются.

    Примеры:
        maps work-update W-2026-001 --status completed
        maps work-update W-2026-001 --priority 1 --end 15.07.2026
        maps work-update AVR-001 --emergency
    """
    from sqlalchemy import text
    from app.db.database import get_session

    # Собираем только те поля, которые нужно изменить
    updates: dict = {}
    if nazvanie is not None:    updates["nazvanie"]       = nazvanie
    if filial is not None:      updates["filial"]         = filial
    if zavod is not None:       updates["zavod"]          = zavod
    if podrazdelenie is not None: updates["podrazdelenie"] = podrazdelenie
    if tip is not None:         updates["tip_raboty"]     = tip
    if priority is not None:    updates["prioritet"]      = priority
    if status is not None:      updates["status"]         = status
    if emergency is not None:   updates["is_emergency"]   = emergency
    if start is not None:       updates["data_nachala"]    = _parse_date(start)
    if end is not None:         updates["data_okonchaniya"] = _parse_date(end)

    if not updates:
        console.print("[yellow]Не передано ни одного поля для обновления.[/yellow]")
        console.print("Используйте --help чтобы увидеть доступные флаги.")
        raise typer.Exit(code=0)

    try:
        with get_session() as session:
            work_row = session.execute(
                text("SELECT id FROM works WHERE kod_raboty = :kod"), {"kod": kod}
            ).fetchone()
            if not work_row:
                console.print(f"[red]✗ Работа не найдена: {kod}[/red]")
                raise typer.Exit(code=1)

            # Строим SET-часть из словаря updates
            set_clause = ", ".join(f"{col} = :{col}" for col in updates)
            params = {**updates, "wid": work_row.id}
            session.execute(
                text(f"UPDATE works SET {set_clause} WHERE id = :wid"), params  # noqa: S608
            )
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Работа обновлена: {kod}[/green]")
    for field, value in updates.items():
        console.print(f"  {field} = {value}")


@app.command(name="work-delete")
def work_delete(
    kod: str = typer.Argument(..., help="Код работы для удаления"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Подтвердить удаление без запроса"),
) -> None:
    """
    Удалить работу и все её потребности из базы данных.

    Удаляет КАСКАДНО: потребности, результаты распределения, дефицит.
    Операция НЕОБРАТИМА.

    Примеры:
        maps work-delete W-2026-001 --yes
        maps work-delete AVR-2026-007
    """
    from sqlalchemy import text
    from app.db.database import get_session

    if not yes:
        # Запрашиваем подтверждение у пользователя
        confirm = typer.confirm(
            f"Удалить работу {kod!r} со ВСЕМИ потребностями и результатами распределения?",
            default=False,
        )
        if not confirm:
            console.print("[yellow]Отменено[/yellow]")
            raise typer.Exit(code=0)

    try:
        with get_session() as session:
            work_row = session.execute(
                text("SELECT id FROM works WHERE kod_raboty = :kod"), {"kod": kod}
            ).fetchone()
            if not work_row:
                console.print(f"[red]✗ Работа не найдена: {kod}[/red]")
                raise typer.Exit(code=1)
            # CASCADE удалит requirements и allocation_results для этой работы
            session.execute(text("DELETE FROM works WHERE id = :wid"), {"wid": work_row.id})
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Работа удалена: {kod}[/green]")


# =============================================================================
# ГРУППА: Точечное изменение — Потребности
# =============================================================================

@app.command(name="req-set")
def req_set(
    kod: str = typer.Argument(..., help="Код работы"),
    sys_nomer: str = typer.Argument(..., help="Системный номер материала (18-значный)"),
    amount: str = typer.Argument(..., help="Количество (используйте запятую: 25,500)"),
    price: str = typer.Option("0", "--price", "-p", help="Прогнозная цена за единицу (используйте запятую)"),
) -> None:
    """
    Добавить или обновить потребность работы в материале.

    Если потребность уже существует — обновляет количество и цену.
    Если нет — создаёт новую запись.

    ВАЖНО: Работа и материал должны существовать в базе данных.

    Примеры:
        maps req-set W-2026-001 000000000010012345 100,5
        maps req-set W-2026-001 000000000010012345 100,5 --price 1500,75
    """
    from sqlalchemy import text
    from app.db.database import get_session
    from decimal import Decimal, InvalidOperation

    # Конвертируем строки с запятой в Decimal
    try:
        qty   = Decimal(amount.replace(",", ".").replace(" ", ""))
        price_val = Decimal(price.replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        console.print("[red]✗ Неверный формат числа. Используйте запятую для дробной части: 100,5[/red]")
        raise typer.Exit(code=1)

    try:
        with get_session() as session:
            work_row = session.execute(
                text("SELECT id FROM works WHERE kod_raboty = :kod"), {"kod": kod}
            ).fetchone()
            if not work_row:
                console.print(f"[red]✗ Работа не найдена: {kod}. Сначала добавьте через maps work-add[/red]")
                raise typer.Exit(code=1)

            mat_row = session.execute(
                text("SELECT id FROM materials WHERE sys_nomer = :s"), {"s": sys_nomer}
            ).fetchone()
            if not mat_row:
                console.print(f"[red]✗ Материал не найден: {sys_nomer}. Сначала добавьте через maps material-add[/red]")
                raise typer.Exit(code=1)

            # Upsert: обновляем если есть, вставляем если нет
            existing = session.execute(text("""
                SELECT id FROM requirements WHERE work_id = :wid AND material_id = :mid
            """), {"wid": work_row.id, "mid": mat_row.id}).fetchone()

            if existing:
                session.execute(text("""
                    UPDATE requirements
                    SET potrebnost = :qty, prognosnaya_tsena = :price,
                        raspredeleno = 0, deficit = 0
                    WHERE id = :rid
                """), {"qty": qty, "price": price_val, "rid": existing.id})
                action = "обновлена"
            else:
                session.execute(text("""
                    INSERT INTO requirements (work_id, material_id, potrebnost, prognosnaya_tsena, raspredeleno, deficit)
                    VALUES (:wid, :mid, :qty, :price, 0, 0)
                """), {"wid": work_row.id, "mid": mat_row.id, "qty": qty, "price": price_val})
                action = "добавлена"
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Потребность {action}:[/green]")
    console.print(f"  Работа:    {kod}")
    console.print(f"  Материал:  {sys_nomer}")
    console.print(f"  Кол-во:    {qty:,.4f}")
    console.print(f"  Цена:      {price_val:,.4f}")


@app.command(name="req-delete")
def req_delete(
    kod: str = typer.Argument(..., help="Код работы"),
    sys_nomer: str = typer.Argument(..., help="Системный номер материала"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Подтвердить без запроса"),
) -> None:
    """
    Удалить одну потребность (связку работа–материал).

    Пример:
        maps req-delete W-2026-001 000000000010012345 --yes
    """
    from sqlalchemy import text
    from app.db.database import get_session

    if not yes:
        confirm = typer.confirm(
            f"Удалить потребность: работа {kod!r}, материал {sys_nomer!r}?",
            default=False,
        )
        if not confirm:
            console.print("[yellow]Отменено[/yellow]")
            raise typer.Exit(code=0)

    try:
        with get_session() as session:
            result = session.execute(text("""
                DELETE FROM requirements
                WHERE work_id = (SELECT id FROM works WHERE kod_raboty = :kod)
                  AND material_id = (SELECT id FROM materials WHERE sys_nomer = :sys)
            """), {"kod": kod, "sys": sys_nomer})
            if result.rowcount == 0:  # type: ignore[attr-defined]
                console.print(f"[red]✗ Потребность не найдена: работа {kod!r}, материал {sys_nomer!r}[/red]")
                raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Потребность удалена: {kod} / {sys_nomer}[/green]")


# =============================================================================
# ГРУППА: Точечное изменение — Материалы
# =============================================================================

@app.command(name="material-add")
def material_add(
    sys_nomer: str = typer.Argument(..., help="Системный номер материала (18-значный SAP-код)"),
    nazvanie: str = typer.Argument(..., help="Наименование материала"),
    ed_izm: str = typer.Argument(..., help="Единица измерения (кг, шт, м, л и т.д.)"),
    group: Optional[str] = typer.Option(None, "--group", "-g", help="Группа материала"),
) -> None:
    """
    Добавить материал в справочник.

    Системный номер должен быть уникальным (обычно 18-значный SAP MM-код).
    Если такой номер уже есть — выдаст ошибку.

    Примеры:
        maps material-add 000000000010012345 "Кабель ВВГ 3х2,5" "м"
        maps material-add 000000000010098765 "Болт М16х60" "шт" --group "Крепёжные изделия"
    """
    from app.db.database import get_session
    from app.db.models import Material
    from sqlalchemy import text

    try:
        with get_session() as session:
            exists = session.execute(
                text("SELECT id FROM materials WHERE sys_nomer = :s"), {"s": sys_nomer}
            ).fetchone()
            if exists:
                console.print(f"[red]✗ Материал {sys_nomer!r} уже есть в справочнике[/red]")
                raise typer.Exit(code=1)
            session.add(Material(
                sys_nomer=sys_nomer,
                naimenovanie=nazvanie,
                ed_izm=ed_izm,
                gruppa=group,
            ))
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Материал добавлен: {sys_nomer} — {nazvanie}[/green]")


@app.command(name="material-update")
def material_update(
    sys_nomer: str = typer.Argument(..., help="Системный номер материала"),
    nazvanie: Optional[str] = typer.Option(None, "--nazvanie", help="Новое наименование"),
    ed_izm: Optional[str] = typer.Option(None, "--unit", help="Новая единица измерения"),
    group: Optional[str] = typer.Option(None, "--group", "-g", help="Новая группа материала"),
) -> None:
    """
    Обновить поля материала в справочнике.

    Обновляются только переданные поля.

    Примеры:
        maps material-update 000000000010012345 --nazvanie "Кабель ВВГнг 3х2,5"
        maps material-update 000000000010012345 --unit "пог.м" --group "Кабельная продукция"
    """
    from sqlalchemy import text
    from app.db.database import get_session

    updates: dict = {}
    if nazvanie is not None: updates["naimenovanie"] = nazvanie
    if ed_izm is not None:   updates["ed_izm"]       = ed_izm
    if group is not None:    updates["gruppa"]        = group

    if not updates:
        console.print("[yellow]Не передано ни одного поля для обновления.[/yellow]")
        raise typer.Exit(code=0)

    try:
        with get_session() as session:
            mat_row = session.execute(
                text("SELECT id FROM materials WHERE sys_nomer = :s"), {"s": sys_nomer}
            ).fetchone()
            if not mat_row:
                console.print(f"[red]✗ Материал не найден: {sys_nomer}[/red]")
                raise typer.Exit(code=1)
            set_clause = ", ".join(f"{col} = :{col}" for col in updates)
            params = {**updates, "mid": mat_row.id}
            session.execute(
                text(f"UPDATE materials SET {set_clause} WHERE id = :mid"), params  # noqa: S608
            )
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Материал обновлён: {sys_nomer}[/green]")


# =============================================================================
# ГРУППА: Точечное изменение — Склады и складские остатки
# =============================================================================

@app.command(name="warehouse-add")
def warehouse_add(
    kod: str = typer.Argument(..., help="Уникальный код склада (например: SLT-001)"),
    filial: str = typer.Option(..., "--filial", "-f", help="Филиал склада (например: Алматы)"),
    zavod: str = typer.Option(..., "--zavod", "-z", help="Код завода SAP (например: AL01)"),
    tip: Optional[str] = typer.Option(None, "--tip", "-t", help="Тип склада (например: Центральный, Цеховой)"),
) -> None:
    """
    Добавить склад в справочник.

    Примеры:
        maps warehouse-add SLT-001 --filial "Алматы" --zavod "AL01"
        maps warehouse-add SHY-002 --filial "Шымкент" --zavod "SH01" --tip "Цеховой"
    """
    from app.db.database import get_session
    from app.db.models import Warehouse
    from sqlalchemy import text

    try:
        with get_session() as session:
            exists = session.execute(
                text("SELECT id FROM warehouses WHERE kod_sklada = :k"), {"k": kod}
            ).fetchone()
            if exists:
                console.print(f"[red]✗ Склад {kod!r} уже существует[/red]")
                raise typer.Exit(code=1)
            session.add(Warehouse(
                kod_sklada=kod,
                filial=filial,
                zavod=zavod,
                tip_sklada=tip,
            ))
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Склад добавлен: {kod} (филиал: {filial}, завод: {zavod})[/green]")


@app.command(name="stock-add")
def stock_add(
    warehouse_kod: str = typer.Argument(..., help="Код склада"),
    sys_nomer: str = typer.Argument(..., help="Системный номер материала"),
    amount: str = typer.Argument(..., help="Количество (используйте запятую: 250,500)"),
    price: str = typer.Argument(..., help="Стоимость за единицу (используйте запятую: 1500,75)"),
    date: Optional[str] = typer.Option(None, "--date", "-d", help="Дата поступления ДД.ММ.ГГГГ"),
    batch: Optional[str] = typer.Option(None, "--batch", "-b", help="Номер партии"),
) -> None:
    """
    Добавить новую партию материала на склад.

    Каждый вызов создаёт НОВУЮ партию. Для замены всех остатков
    используйте import-stock.

    Примеры:
        maps stock-add SLT-001 000000000010012345 500,0 1250,50 --date 01.06.2026
        maps stock-add SLT-001 000000000010012345 100,0 1300,00 --batch "4500123456" --date 15.05.2026
    """
    from sqlalchemy import text
    from app.db.database import get_session
    from app.db.models import StockBatch
    from decimal import Decimal, InvalidOperation

    try:
        qty_val   = Decimal(amount.replace(",", ".").replace(" ", ""))
        price_val = Decimal(price.replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        console.print("[red]✗ Неверный формат числа. Используйте запятую для дробной части[/red]")
        raise typer.Exit(code=1)

    d_date = _parse_date(date) if date else None

    try:
        with get_session() as session:
            wh_row = session.execute(
                text("SELECT id FROM warehouses WHERE kod_sklada = :k"), {"k": warehouse_kod}
            ).fetchone()
            if not wh_row:
                console.print(f"[red]✗ Склад не найден: {warehouse_kod}. Добавьте через maps warehouse-add[/red]")
                raise typer.Exit(code=1)

            mat_row = session.execute(
                text("SELECT id FROM materials WHERE sys_nomer = :s"), {"s": sys_nomer}
            ).fetchone()
            if not mat_row:
                console.print(f"[red]✗ Материал не найден: {sys_nomer}. Добавьте через maps material-add[/red]")
                raise typer.Exit(code=1)

            session.add(StockBatch(
                warehouse_id=wh_row.id,
                material_id=mat_row.id,
                kolichestvo=qty_val,
                dostupno=qty_val,      # Новая партия: доступно = общее количество
                stoimost_za_ed=price_val,
                data_postupleniya=d_date,
                nomer_partii=batch,
            ))
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Партия добавлена:[/green]")
    console.print(f"  Склад:       {warehouse_kod}")
    console.print(f"  Материал:    {sys_nomer}")
    console.print(f"  Количество:  {qty_val:,.4f}")
    console.print(f"  Цена:        {price_val:,.4f}")
    if d_date:
        console.print(f"  Дата пост.:  {d_date.strftime('%d.%m.%Y')}")


@app.command(name="stock-delete")
def stock_delete(
    warehouse_kod: str = typer.Argument(..., help="Код склада"),
    sys_nomer: str = typer.Argument(..., help="Системный номер материала"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Подтвердить без запроса"),
) -> None:
    """
    Удалить ВСЕ партии материала с конкретного склада.

    Удаляет все записи stock_batches для указанной пары склад–материал.
    Для удаления всех остатков используйте maps reset-stock.

    Пример:
        maps stock-delete SLT-001 000000000010012345 --yes
    """
    from sqlalchemy import text
    from app.db.database import get_session

    if not yes:
        confirm = typer.confirm(
            f"Удалить ВСЕ партии материала {sys_nomer!r} со склада {warehouse_kod!r}?",
            default=False,
        )
        if not confirm:
            console.print("[yellow]Отменено[/yellow]")
            raise typer.Exit(code=0)

    try:
        with get_session() as session:
            result = session.execute(text("""
                DELETE FROM stock_batches
                WHERE warehouse_id = (SELECT id FROM warehouses WHERE kod_sklada = :wk)
                  AND material_id  = (SELECT id FROM materials WHERE sys_nomer = :s)
            """), {"wk": warehouse_kod, "s": sys_nomer})
            deleted = result.rowcount  # type: ignore[attr-defined]
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Удалено партий: {deleted}[/green]")


# =============================================================================
# ГРУППА: Точечное изменение — Поставки
# =============================================================================

@app.command(name="supply-add")
def supply_add(
    dogovor: str = typer.Argument(..., help="Номер договора поставки"),
    postavshchik: str = typer.Argument(..., help="Наименование поставщика"),
    filial: str = typer.Argument(..., help="Филиал получателя (должен совпадать с work.filial)"),
    date_str: str = typer.Argument(..., metavar="DATE", help="Дата поставки ДД.ММ.ГГГГ"),
    zavod: Optional[str] = typer.Option(None, "--zavod", "-z", help="Код завода"),
    warehouse_kod: Optional[str] = typer.Option(None, "--warehouse", "-w", help="Код склада назначения"),
    status: str = typer.Option("confirmed", "--status", help="Статус: confirmed / cancelled"),
) -> None:
    """
    Добавить новую поставку (заголовок без строк).

    После создания добавьте материалы через maps supply-add-line.

    Примеры:
        maps supply-add "ДГ-2026-001" "ТОО Снабжение" "Алматы" 15.07.2026
        maps supply-add "ДГ-2026-002" "АО Металлторг" "Шымкент" 01.08.2026 --zavod SH01 --warehouse SHY-001
    """
    from app.db.database import get_session
    from app.db.models import Supply
    from sqlalchemy import text

    d_date = _parse_date(date_str)

    wh_id = None
    try:
        with get_session() as session:
            # Если указан склад — проверяем что он существует
            if warehouse_kod:
                wh_row = session.execute(
                    text("SELECT id FROM warehouses WHERE kod_sklada = :k"), {"k": warehouse_kod}
                ).fetchone()
                if not wh_row:
                    console.print(f"[red]✗ Склад не найден: {warehouse_kod}[/red]")
                    raise typer.Exit(code=1)
                wh_id = wh_row.id

            session.add(Supply(
                dogovor=dogovor,
                postavshchik=postavshchik,
                filial=filial,
                zavod=zavod,
                data_postavki=d_date,
                warehouse_id=wh_id,
                status=status,
            ))
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Поставка создана: {dogovor}[/green]")
    console.print(f"  Поставщик: {postavshchik}")
    console.print(f"  Филиал:    {filial}")
    console.print(f"  Дата:      {d_date.strftime('%d.%m.%Y')}")
    console.print(f"\nДобавьте материалы: [cyan]maps supply-add-line {dogovor!r} <sys_nomer> <qty> <price>[/cyan]")


@app.command(name="supply-add-line")
def supply_add_line(
    dogovor: str = typer.Argument(..., help="Номер договора поставки"),
    sys_nomer: str = typer.Argument(..., help="Системный номер материала"),
    amount: str = typer.Argument(..., help="Количество (используйте запятую: 100,500)"),
    price: str = typer.Argument(..., help="Стоимость за единицу (используйте запятую: 2500,00)"),
) -> None:
    """
    Добавить строку материала в существующую поставку.

    Примеры:
        maps supply-add-line "ДГ-2026-001" 000000000010012345 500,0 1250,00
        maps supply-add-line "ДГ-2026-001" 000000000010098765 1000,0 75,50
    """
    from sqlalchemy import text
    from app.db.database import get_session
    from app.db.models import SupplyLine
    from decimal import Decimal, InvalidOperation

    try:
        qty_val   = Decimal(amount.replace(",", ".").replace(" ", ""))
        price_val = Decimal(price.replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        console.print("[red]✗ Неверный формат числа[/red]")
        raise typer.Exit(code=1)

    try:
        with get_session() as session:
            sup_row = session.execute(
                text("SELECT id FROM supplies WHERE dogovor = :d"), {"d": dogovor}
            ).fetchone()
            if not sup_row:
                console.print(f"[red]✗ Поставка не найдена: {dogovor}. Создайте через maps supply-add[/red]")
                raise typer.Exit(code=1)

            mat_row = session.execute(
                text("SELECT id FROM materials WHERE sys_nomer = :s"), {"s": sys_nomer}
            ).fetchone()
            if not mat_row:
                console.print(f"[red]✗ Материал не найден: {sys_nomer}[/red]")
                raise typer.Exit(code=1)

            session.add(SupplyLine(
                supply_id=sup_row.id,
                material_id=mat_row.id,
                kolichestvo=qty_val,
                dostupno=qty_val,
                stoimost_za_ed=price_val,
            ))
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Строка добавлена в поставку {dogovor!r}:[/green]")
    console.print(f"  Материал:   {sys_nomer}")
    console.print(f"  Количество: {qty_val:,.4f}")
    console.print(f"  Цена:       {price_val:,.4f}")


@app.command(name="supply-delete")
def supply_delete(
    dogovor: str = typer.Argument(..., help="Номер договора поставки"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Подтвердить без запроса"),
) -> None:
    """
    Удалить поставку со всеми строками материалов.

    Пример:
        maps supply-delete "ДГ-2026-001" --yes
    """
    from sqlalchemy import text
    from app.db.database import get_session

    if not yes:
        confirm = typer.confirm(
            f"Удалить поставку {dogovor!r} со всеми строками материалов?",
            default=False,
        )
        if not confirm:
            console.print("[yellow]Отменено[/yellow]")
            raise typer.Exit(code=0)

    try:
        with get_session() as session:
            sup_row = session.execute(
                text("SELECT id FROM supplies WHERE dogovor = :d"), {"d": dogovor}
            ).fetchone()
            if not sup_row:
                console.print(f"[red]✗ Поставка не найдена: {dogovor}[/red]")
                raise typer.Exit(code=1)
            # Строки удалятся каскадно через FK supply_lines.supply_id
            session.execute(text("DELETE FROM supplies WHERE id = :sid"), {"sid": sup_row.id})
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Поставка удалена: {dogovor}[/green]")


# =============================================================================
# ГРУППА: Согласованные перемещения
# =============================================================================

@app.command(name="import-transfers")
def import_transfers_cmd(
    file: Path = typer.Argument(..., help="Путь к Excel файлу с согласованными перемещениями"),
    sheet: str = typer.Option("0", "--sheet", "-s", help="Имя или номер листа"),
) -> None:
    """
    Импортировать согласованные межфилиальные перемещения из Excel (пакетная загрузка).

    Что такое согласованные перемещения?
        После первого распределения система показывает «Возможное перемещение» —
        склады других филиалов, которые могут закрыть дефицит.
        Руководитель согласовывает конкретные перемещения, менеджер вносит
        их в Excel и загружает этим файлом. При следующем запуске maps allocate
        система учтёт их как реальный источник материалов (Слой 3в).

    Формат файла:
        Системный номер, Код склада-источника, Филиал-получатель, Количество

    ВНИМАНИЕ: Существующие перемещения будут УДАЛЕНЫ и заменены.

    Пример:
        maps import-transfers data/approved_transfers.xlsx
    """
    if not file.exists():
        console.print(f"[red]✗ Файл не найден: {file}[/red]")
        raise typer.Exit(code=1)

    console.print(f"Импорт согласованных перемещений из: [cyan]{file}[/cyan]")
    console.print("[yellow]⚠ Существующие перемещения будут заменены[/yellow]")

    from app.services.import_service import import_approved_transfers

    with console.status("Импортируем данные..."):
        try:
            sheet_name: int | str = int(sheet) if sheet.isdigit() else sheet
            stats = import_approved_transfers(file, sheet_name=sheet_name)
        except Exception as e:
            console.print(f"[red]✗ Ошибка импорта: {e}[/red]")
            raise typer.Exit(code=1)

    _print_import_stats("Согласованные перемещения", stats)
    console.print("\nТеперь запустите распределение повторно: [cyan]maps allocate[/cyan]")


@app.command(name="transfer-list")
def transfer_list(
    filial: Optional[str] = typer.Option(None, "--filial", "-f", help="Фильтр по филиалу-получателю"),
    material: Optional[str] = typer.Option(None, "--material", "-m", help="Фильтр по системному номеру"),
) -> None:
    """
    Показать список согласованных межфилиальных перемещений.

    Пример:
        maps transfer-list
        maps transfer-list --filial "Алматы"
    """
    from sqlalchemy import text
    from app.db.database import get_session

    try:
        with get_session() as session:
            conditions = []
            params: dict = {}
            if filial:
                conditions.append("at.to_filial = :filial")
                params["filial"] = filial
            if material:
                conditions.append("m.sys_nomer = :material")
                params["material"] = material
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            rows = session.execute(text(f"""
                SELECT at.id, m.sys_nomer, m.naimenovanie, m.ed_izm,
                       wh.kod_sklada, wh.filial AS from_filial,
                       at.to_filial, at.kolichestvo, at.dostupno,
                       at.uploaded_at
                FROM approved_transfers at
                JOIN materials m ON m.id = at.material_id
                JOIN warehouses wh ON wh.id = at.from_warehouse_id
                {where}
                ORDER BY at.to_filial, m.sys_nomer
            """), params).fetchall()  # noqa: S608
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    if not rows:
        console.print("[yellow]Согласованных перемещений нет[/yellow]")
        return

    table = Table(title=f"Согласованные перемещения ({len(rows)})")
    table.add_column("ID", justify="right")
    table.add_column("Системный номер", style="cyan", no_wrap=True)
    table.add_column("Наименование")
    table.add_column("Ед.изм", justify="center")
    table.add_column("Склад-источник")
    table.add_column("Откуда (филиал)")
    table.add_column("Куда (филиал)")
    table.add_column("Кол-во", justify="right")
    table.add_column("Доступно", justify="right")

    for r in rows:
        naim = (r.naimenovanie or "")[:30] + ("…" if len(r.naimenovanie or "") > 30 else "")
        dostupno_color = "green" if r.dostupno > 0 else "dim"
        table.add_row(
            str(r.id), r.sys_nomer, naim, r.ed_izm or "—",
            r.kod_sklada, r.from_filial or "—", r.to_filial,
            f"{r.kolichestvo:,.4f}",
            f"[{dostupno_color}]{r.dostupno:,.4f}[/{dostupno_color}]",
        )
    console.print(table)


@app.command(name="transfer-add")
def transfer_add(
    sys_nomer: str = typer.Argument(..., help="Системный номер материала"),
    from_warehouse: str = typer.Argument(..., help="Код склада-источника"),
    to_filial: str = typer.Argument(..., help="Филиал-получатель"),
    amount: str = typer.Argument(..., help="Согласованное количество (запятая: 150,500)"),
) -> None:
    """
    Добавить одну запись согласованного перемещения вручную.

    Используется когда нужно добавить одну позицию без загрузки Excel.
    Для пакетной загрузки используйте maps import-transfers.

    Пример:
        maps transfer-add 000000000010012345 SLT-001 "Шымкент" 250,0
    """
    from sqlalchemy import text
    from app.db.database import get_session
    from app.db.models import ApprovedTransfer
    from decimal import Decimal, InvalidOperation

    try:
        qty_val = Decimal(amount.replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        console.print("[red]✗ Неверный формат числа[/red]")
        raise typer.Exit(code=1)

    try:
        with get_session() as session:
            mat_row = session.execute(
                text("SELECT id FROM materials WHERE sys_nomer = :s"), {"s": sys_nomer}
            ).fetchone()
            if not mat_row:
                console.print(f"[red]✗ Материал не найден: {sys_nomer}[/red]")
                raise typer.Exit(code=1)

            wh_row = session.execute(
                text("SELECT id FROM warehouses WHERE kod_sklada = :k"), {"k": from_warehouse}
            ).fetchone()
            if not wh_row:
                console.print(f"[red]✗ Склад не найден: {from_warehouse}[/red]")
                raise typer.Exit(code=1)

            session.add(ApprovedTransfer(
                material_id=mat_row.id,
                from_warehouse_id=wh_row.id,
                to_filial=to_filial,
                kolichestvo=qty_val,
                dostupno=qty_val,
            ))
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Перемещение добавлено:[/green]")
    console.print(f"  Материал:  {sys_nomer}")
    console.print(f"  Откуда:    {from_warehouse}")
    console.print(f"  Куда:      {to_filial}")
    console.print(f"  Кол-во:    {qty_val:,.4f}")


@app.command(name="transfer-delete")
def transfer_delete(
    transfer_id: int = typer.Argument(..., help="ID записи перемещения (из maps transfer-list)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Подтвердить без запроса"),
) -> None:
    """
    Удалить одну запись согласованного перемещения по ID.

    ID можно узнать командой: maps transfer-list

    Пример:
        maps transfer-delete 42 --yes
    """
    from sqlalchemy import text
    from app.db.database import get_session

    if not yes:
        confirm = typer.confirm(f"Удалить перемещение ID={transfer_id}?", default=False)
        if not confirm:
            console.print("[yellow]Отменено[/yellow]")
            raise typer.Exit(code=0)

    try:
        with get_session() as session:
            result = session.execute(
                text("DELETE FROM approved_transfers WHERE id = :tid"), {"tid": transfer_id}
            )
            if result.rowcount == 0:  # type: ignore[attr-defined]
                console.print(f"[red]✗ Перемещение ID={transfer_id} не найдено[/red]")
                raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Перемещение ID={transfer_id} удалено[/green]")


# =============================================================================
# ГРУППА: Пакетные сбросы (выборочное удаление данных)
# =============================================================================

@app.command(name="reset-works")
def reset_works(
    yes: bool = typer.Option(False, "--yes", "-y", help="Подтвердить без запроса"),
) -> None:
    """
    Удалить ВСЕ работы, потребности и результаты распределения.

    Оставляет нетронутыми: материалы, склады, остатки, поставки.
    Используется для полной перезагрузки перечня работ.

    Пример:
        maps reset-works --yes
    """
    _selective_reset(
        tables=["stock_movements", "allocation_results", "deficit_records",
                "requirements", "writeoffs", "issued_not_written_off",
                "allocation_sessions", "works"],
        label="Работы, потребности и результаты распределения",
        yes=yes,
    )


@app.command(name="reset-stock")
def reset_stock(
    yes: bool = typer.Option(False, "--yes", "-y", help="Подтвердить без запроса"),
) -> None:
    """
    Удалить все складские остатки (партии) и склады.

    Оставляет нетронутыми: работы, материалы, поставки, результаты.
    Используется для замены снимка склада на новый.

    Пример:
        maps reset-stock --yes
    """
    _selective_reset(
        tables=["stock_movements", "stock_batches", "warehouses"],
        label="Складские остатки (партии) и справочник складов",
        yes=yes,
    )


@app.command(name="reset-requirements")
def reset_requirements(
    yes: bool = typer.Option(False, "--yes", "-y", help="Подтвердить без запроса"),
) -> None:
    """
    Удалить все потребности, списания, «выдано не списано» и результаты.

    Работы и материалы остаются. Использутся для перезагрузки
    операционных данных перед новым циклом распределения.

    Пример:
        maps reset-requirements --yes
    """
    _selective_reset(
        tables=["stock_movements", "allocation_results", "deficit_records",
                "requirements", "writeoffs", "issued_not_written_off",
                "allocation_sessions"],
        label="Потребности, списания, выдано, результаты распределения",
        yes=yes,
    )


@app.command(name="reset-supplies")
def reset_supplies(
    yes: bool = typer.Option(False, "--yes", "-y", help="Подтвердить без запроса"),
) -> None:
    """
    Удалить все поставки и строки поставок.

    Пример:
        maps reset-supplies --yes
    """
    _selective_reset(
        tables=["supply_lines", "supplies"],
        label="Поставки и строки поставок",
        yes=yes,
    )


@app.command(name="reset-sessions")
def reset_sessions(
    yes: bool = typer.Option(False, "--yes", "-y", help="Подтвердить без запроса"),
) -> None:
    """
    Удалить все сессии распределения и их результаты.

    Исходные данные (работы, потребности, склады) остаются.
    После этого можно запустить maps allocate заново.

    Пример:
        maps reset-sessions --yes
    """
    _selective_reset(
        tables=["stock_movements", "allocation_results",
                "deficit_records", "allocation_sessions"],
        label="Сессии распределения и результаты",
        yes=yes,
    )


@app.command(name="session-delete")
def session_delete(
    session_id: str = typer.Argument(..., help="ID сессии для удаления"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Подтвердить без запроса"),
) -> None:
    """
    Удалить одну конкретную сессию распределения.

    Удаляет каскадно: результаты, движения склада, дефицит.
    ID можно найти командой: maps session-list

    Пример:
        maps session-delete 20260530_143055_a1b2c --yes
    """
    from sqlalchemy import text
    from app.db.database import get_session

    if not yes:
        confirm = typer.confirm(
            f"Удалить сессию {session_id!r} со всеми результатами?", default=False
        )
        if not confirm:
            console.print("[yellow]Отменено[/yellow]")
            raise typer.Exit(code=0)

    try:
        with get_session() as session:
            exists = session.execute(
                text("SELECT id FROM allocation_sessions WHERE id = :sid"), {"sid": session_id}
            ).fetchone()
            if not exists:
                console.print(f"[red]✗ Сессия не найдена: {session_id}[/red]")
                raise typer.Exit(code=1)
            session.execute(
                text("DELETE FROM allocation_sessions WHERE id = :sid"), {"sid": session_id}
            )
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Сессия удалена: {session_id}[/green]")


# =============================================================================
# ГРУППА: Настройки и диагностика
# =============================================================================

@app.command(name="set-year")
def set_year(
    year: int = typer.Argument(..., help="Год планирования (например: 2026)"),
) -> None:
    """
    Установить год планирования.

    Год планирования определяет «точку отсчёта» для алгоритма распределения:
    работы, дата окончания которых раньше 1 января указанного года,
    считаются завершёнными и НЕ участвуют в распределении.

    Пример:
        maps set-year 2026
        maps set-year 2027
    """
    from sqlalchemy import text
    from app.db.database import get_session

    if year < 2000 or year > 2100:
        console.print("[red]✗ Некорректный год. Допустимый диапазон: 2000–2100[/red]")
        raise typer.Exit(code=1)

    try:
        with get_session() as session:
            # Upsert: создать запись если нет (id=1 — синглтон)
            existing = session.execute(
                text("SELECT id FROM system_settings WHERE id = 1")
            ).fetchone()
            if existing:
                session.execute(
                    text("UPDATE system_settings SET planning_year = :year WHERE id = 1"),
                    {"year": year},
                )
            else:
                session.execute(
                    text("INSERT INTO system_settings (id, planning_year) VALUES (1, :year)"),
                    {"year": year},
                )
    except Exception as e:
        console.print(f"[red]✗ Ошибка: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Год планирования установлен: {year}[/green]")
    console.print(
        f"  Работы с датой окончания ранее 01.01.{year} "
        "будут считаться завершёнными."
    )


@app.command(name="db-check")
def db_check() -> None:
    """
    Проверить подключение к базе данных и показать статистику таблиц.

    Полезно для диагностики при проблемах с подключением.
    Показывает версию PostgreSQL и количество строк во всех таблицах.

    Пример:
        maps db-check
    """
    from sqlalchemy import text
    from app.db.database import get_session, check_connection

    console.print(Panel(
        f"Хост:    {settings.db_host}:{settings.db_port}\n"
        f"База:    {settings.db_name}\n"
        f"Юзер:    {settings.db_user}",
        title="Подключение к PostgreSQL",
    ))

    with console.status("Проверяем подключение..."):
        ok = check_connection()

    if not ok:
        console.print("[red]✗ Нет подключения к PostgreSQL![/red]")
        console.print("\nПроверьте настройки в .env файле.")
        raise typer.Exit(code=1)

    console.print("[green]✓ Подключение установлено[/green]")

    try:
        with get_session() as session:
            # Версия PostgreSQL
            pg_version = session.execute(text("SELECT version()")).scalar()
            console.print(f"Версия: [dim]{(pg_version or '')[:60]}...[/dim]\n")

            # Количество строк в каждой таблице
            all_tables = [
                "works", "materials", "requirements", "warehouses",
                "stock_batches", "supplies", "supply_lines",
                "writeoffs", "issued_not_written_off", "approved_transfers",
                "allocation_sessions", "allocation_results",
                "stock_movements", "deficit_records", "system_settings",
            ]
            table = Table(title="Размер таблиц")
            table.add_column("Таблица", style="bold")
            table.add_column("Строк", justify="right")
            table.add_column("Состояние")

            for tbl in all_tables:
                try:
                    cnt = session.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar()  # noqa: S608
                    color = "green" if cnt and cnt > 0 else "dim"
                    state = "заполнена" if cnt and cnt > 0 else "пустая"
                    table.add_row(tbl, f"[{color}]{cnt}[/{color}]", state)
                except Exception:
                    table.add_row(tbl, "[red]?[/red]", "[red]ошибка[/red]")
    except Exception as e:
        console.print(f"[red]✗ Ошибка запроса: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(table)


# =============================================================================
# Команда: serve — запуск веб-сервера FastAPI
# =============================================================================

@app.command()
def serve(
    host: str = typer.Option(
        "0.0.0.0", "--host", "-h",
        help="Адрес для прослушивания. 0.0.0.0 = доступен с других устройств в сети.",
    ),
    port: int = typer.Option(
        8000, "--port", "-p",
        help="Порт веб-сервера.",
    ),
    reload: bool = typer.Option(
        False, "--reload",
        help="Перезапускать сервер при изменении файлов (только для разработки).",
    ),
) -> None:
    """
    Запустить веб-сервер FastAPI с дашбордом.

    После запуска открой в браузере:
        http://localhost:8000        — дашборд (импорт, распределение, скачать отчёт)
        http://localhost:8000/docs   — автодокументация API

    Ссылка для скачивания Excel с другого компа:
        http://<IP_этого_компа>:8000/api/export/<ID_сессии>
        Пример: http://192.168.1.100:8000/api/export/20260523_143055_a1b2c

    Примеры:
        maps serve
        maps serve --port 9000
        maps serve --reload        (режим разработки)
    """
    import uvicorn

    console.print(Panel(
        f"[bold]Запуск веб-сервера MAPS[/bold]\n\n"
        f"Адрес дашборда:  [cyan]http://localhost:{port}[/cyan]\n"
        f"API документация: [cyan]http://localhost:{port}/docs[/cyan]\n\n"
        f"Для остановки нажми [bold]Ctrl+C[/bold]",
        title="MAPS — Web Server",
    ))

    uvicorn.run(
        "app.api.main:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


# =============================================================================
# Вспомогательные функции
# =============================================================================

def _parse_date(date_str: str):
    """
    Разобрать дату из строки формата ДД.ММ.ГГГГ и вернуть объект datetime.date.

    Почему именно ДД.ММ.ГГГГ?
        Это стандартный формат в российском/казахстанском делопроизводстве
        и формат, принятый во всех входных/выходных файлах MAPS.
        Например: "01.06.2026" → date(2026, 6, 1).
    """
    from datetime import datetime
    try:
        return datetime.strptime(date_str.strip(), "%d.%m.%Y").date()
    except ValueError:
        console.print(f"[red]✗ Неверный формат даты: {date_str!r}. Ожидается ДД.ММ.ГГГГ, например: 01.06.2026[/red]")
        raise typer.Exit(code=1)


def _selective_reset(tables: list[str], label: str, yes: bool) -> None:
    """
    Удалить данные из указанных таблиц с подтверждением пользователя.

    Принимает упорядоченный список таблиц (сначала зависимые, потом родительские).
    Используется командами reset-* для выборочной очистки данных.

    Параметры:
        tables — список таблиц в порядке удаления (учитывает FK-зависимости)
        label  — описание для пользователя (что именно удаляется)
        yes    — True = без запроса подтверждения
    """
    from sqlalchemy import text
    from app.db.database import get_session

    if not yes:
        confirm = typer.confirm(
            f"Удалить ВСЕ данные: {label}? Операция НЕОБРАТИМА.",
            default=False,
        )
        if not confirm:
            console.print("[yellow]Отменено[/yellow]")
            raise typer.Exit(code=0)

    try:
        with get_session() as session:
            for tbl in tables:
                session.execute(text(f"TRUNCATE {tbl} RESTART IDENTITY CASCADE"))  # noqa: S608
    except Exception as e:
        console.print(f"[red]✗ Ошибка очистки: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]✓ Данные удалены: {label}[/green]")
    for tbl in tables:
        console.print(f"  • {tbl}")


def _print_import_stats(name: str, stats: dict[str, int]) -> None:
    """
    Красиво вывести таблицу статистики после импорта.

    Ключ "errors" выделяется красным если > 0.
    Ключ "skipped" выделяется жёлтым если > 0.
    Остальные ключи — зелёным.
    """
    table = Table(title=f"Результат импорта: {name}")
    table.add_column("Показатель", style="bold")
    table.add_column("Значение", justify="right")

    for key, value in stats.items():
        if key == "errors" and value > 0:
            color = "red"
        elif key == "skipped" and value > 0:
            color = "yellow"
        else:
            color = "green"
        table.add_row(key, f"[{color}]{value}[/{color}]")

    console.print(table)


# =============================================================================
# Точка входа
# =============================================================================

if __name__ == "__main__":
    # При прямом запуске: python main.py <команда>
    # После установки (pip install -e .): maps <команда>
    app()
