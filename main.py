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
        maps export SESSION_ID    — экспортировать результаты в Excel

    Утилиты:
        maps status               — состояние базы данных
        maps sessions             — список сессий распределения
        maps show SESSION_ID      — детали конкретной сессии
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
    9. maps export <ID_сессии>

Примеры:
    python main.py init
    python main.py import-requirements data/sample_requirements.xlsx
    python main.py allocate --session "план_2026_06"
    python main.py export 20260523_143000_abc12
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
    Используйте 'maps export SESSION_ID' для создания Excel-отчёта.

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
                engine = AllocationEngine(session, session_id=session_id)
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
    console.print(f"  [cyan]maps export {sid}[/cyan]")


# =============================================================================
# Команда: export — экспорт результатов в Excel
# =============================================================================

@app.command()
def export(
    session_id: str = typer.Argument(..., help="ID сессии распределения"),
    output_dir: Optional[str] = typer.Option(
        None, "--output", "-o",
        help=f"Папка для сохранения (по умолчанию: {settings.export_dir})"
    ),
) -> None:
    """
    Экспортировать результаты распределения в Excel (4 листа).

    Структура Excel-отчёта:
        1. «Распределение» — широкая таблица: все слои для каждой потребности.
           Колонки: Работа | Материал | Потребность | Сп-е | Выд-но | Склад | Поставки | К закупу | %
        2. «Движение склада» — детальное движение по партиям
        3. «Остатки складов» — что осталось на складах после распределения
        4. «Возможное перемещение» — анализ: склады других филиалов

    Примеры:
        maps export 20260523_143000_abc12
        maps export 20260523_143000_abc12 --output /tmp/reports
    """
    from app.services.export_service import export_allocation_results

    console.print(f"Экспорт сессии: [cyan]{session_id}[/cyan]")

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

    Выводит:
        - Количество записей в каждой таблице
        - Последние 5 сессий распределения

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

            # Последние 5 сессий распределения
            try:
                sessions = session.execute(text("""
                    SELECT id, status, started_at,
                           total_requirements, total_allocated, total_deficit
                    FROM allocation_sessions
                    ORDER BY started_at DESC
                    LIMIT 5
                """)).fetchall()

                if sessions:
                    sess_table = Table(title="Последние сессии распределения")
                    sess_table.add_column("ID сессии")
                    sess_table.add_column("Статус")
                    sess_table.add_column("Дата запуска")
                    sess_table.add_column("Потребностей", justify="right")
                    sess_table.add_column("Распределено", justify="right")
                    sess_table.add_column("Дефицит", justify="right")

                    for s in sessions:
                        status_color = "green" if s.status == "completed" else "red"
                        deficit_color = "red" if s.total_deficit > 0 else "green"
                        sess_table.add_row(
                            s.id,
                            f"[{status_color}]{s.status}[/{status_color}]",
                            str(s.started_at)[:19],
                            str(s.total_requirements),
                            str(s.total_allocated),
                            f"[{deficit_color}]{s.total_deficit}[/{deficit_color}]",
                        )
                    console.print(sess_table)
            except Exception:
                pass  # Таблица ещё не создана — пропускаем

    except Exception as e:
        console.print(f"[red]✗ Ошибка подключения к БД: {e}[/red]")
        raise typer.Exit(code=1)


# =============================================================================
# Команда: sessions — список сессий распределения
# =============================================================================

@app.command()
def sessions(
    limit: int = typer.Option(20, "--limit", "-n", help="Сколько последних сессий показать"),
) -> None:
    """
    Показать список сессий распределения в виде таблицы.

    Выводит все сессии начиная с последней: ID, статус, дату, количество
    потребностей, строк распределения и записей дефицита.

    Примеры:
        maps sessions
        maps sessions --limit 5
    """
    from sqlalchemy import text

    from app.db.database import get_session as db_session

    try:
        with db_session() as session:
            rows = session.execute(text("""
                SELECT id, status, started_at, completed_at,
                       total_requirements, total_allocated, total_deficit
                FROM allocation_sessions
                ORDER BY started_at DESC
                LIMIT :limit
            """), {"limit": limit}).fetchall()
    except Exception as e:
        console.print(f"[red]✗ Ошибка подключения к БД: {e}[/red]")
        raise typer.Exit(code=1)

    if not rows:
        console.print("[yellow]Сессий пока нет. Запустите: maps allocate[/yellow]")
        return

    table = Table(title=f"Сессии распределения (последние {limit})")
    table.add_column("ID сессии",         style="cyan",  no_wrap=True)
    table.add_column("Статус",            justify="center")
    table.add_column("Запущена",          no_wrap=True)
    table.add_column("Завершена",         no_wrap=True)
    table.add_column("Потребностей",      justify="right")
    table.add_column("Распределено",      justify="right")
    table.add_column("Дефицит",           justify="right")

    for r in rows:
        status_color = "green" if r.status == "completed" else "red"
        deficit_color = "red" if (r.total_deficit or 0) > 0 else "green"
        started   = str(r.started_at)[:19]   if r.started_at   else "—"
        completed = str(r.completed_at)[:19] if r.completed_at else "—"
        table.add_row(
            r.id,
            f"[{status_color}]{r.status}[/{status_color}]",
            started,
            completed,
            str(r.total_requirements or 0),
            str(r.total_allocated   or 0),
            f"[{deficit_color}]{r.total_deficit or 0}[/{deficit_color}]",
        )

    console.print(table)
    console.print(
        f"\nДля деталей сессии: [cyan]maps show <ID_сессии>[/cyan]\n"
        f"Для экспорта отчёта: [cyan]maps export <ID_сессии>[/cyan]"
    )


# =============================================================================
# Команда: show — детальный просмотр одной сессии
# =============================================================================

@app.command()
def show(
    session_id: str = typer.Argument(..., help="ID сессии распределения"),
) -> None:
    """
    Показать детальную информацию об одной сессии распределения.

    Выводит:
        - Общую статистику сессии
        - Топ-10 материалов в дефиците
        - Итоги по слоям покрытия

    Примеры:
        maps show 20260524_143000_abc12
    """
    from sqlalchemy import text

    from app.db.database import get_session as db_session

    try:
        with db_session() as session:
            # --- Общая информация о сессии ---
            sess_row = session.execute(text("""
                SELECT id, status, started_at, completed_at,
                       total_requirements, total_allocated, total_deficit
                FROM allocation_sessions
                WHERE id = :sid
            """), {"sid": session_id}).fetchone()

            if sess_row is None:
                console.print(f"[red]✗ Сессия не найдена: {session_id}[/red]")
                console.print("Список сессий: [cyan]maps sessions[/cyan]")
                raise typer.Exit(code=1)

            # --- Топ дефицитных материалов ---
            # deficit_records.material_id → materials напрямую
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
        title="Сессия распределения",
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
        # istochnik — русское название источника (напр. "Склад", "Поставки", "К закупу")
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
        f"\nДля экспорта в Excel: [cyan]maps export {session_id}[/cyan]"
    )


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
