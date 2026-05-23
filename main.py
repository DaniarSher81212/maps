"""
main.py — Точка входа в систему MAPS (CLI)

Что такое CLI?
    Command Line Interface — интерфейс командной строки.
    Позволяет запускать функции системы из терминала командами.

Почему Typer?
    Typer — современная библиотека для создания CLI команд на Python.
    Преимущества перед argparse:
      - Автоматические подсказки при вводе команд (Tab)
      - Автоматическая генерация --help
      - Типизация через аннотации Python
      - Красивый вывод через Rich

Доступные команды:
    maps init              — создать таблицы в PostgreSQL
    maps import-requirements FILE  — импортировать потребности из Excel
    maps import-stock FILE         — импортировать складские остатки
    maps import-supplies FILE      — импортировать поставки
    maps allocate                  — запустить распределение
    maps export SESSION_ID         — экспортировать результаты в Excel
    maps status                    — показать текущее состояние БД

Примеры использования:
    python main.py init
    python main.py import-requirements data/sample_requirements.xlsx
    python main.py import-stock data/sample_stock.xlsx
    python main.py import-supplies data/sample_supplies.xlsx
    python main.py allocate
    python main.py export 20260523_143000_abc12
    python main.py status

    Или после установки пакета (pip install -e .):
    maps init
    maps allocate
"""

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from app.core.config import settings

# Создаём экземпляр приложения Typer
# help= — текст который показывается в: maps --help
app = typer.Typer(
    name="maps",
    help="MAPS — Material Allocation & Planning System\n\nСистема распределения материалов для СМР.",
    add_completion=False,  # Отключаем автодополнение (можно включить позже)
)

# Rich Console — для красивого вывода в терминал
console = Console()


# =============================================================================
# Команда: init — инициализация базы данных
# =============================================================================

@app.command()
def init() -> None:
    """
    Инициализировать базу данных: создать все таблицы.

    Вызывается один раз при первом запуске системы.
    Безопасно вызывать повторно — существующие данные НЕ удаляются.

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

    # Проверяем доступность БД
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

    # Создаём таблицы
    with console.status("Создаём таблицы..."):
        create_all_tables()

    console.print("[green]✓ Все таблицы созданы/проверены[/green]")
    console.print("\nСистема готова к работе! Следующий шаг:")
    console.print("  [cyan]maps import-requirements data/sample_requirements.xlsx[/cyan]")


# =============================================================================
# Команды импорта
# =============================================================================

@app.command(name="import-requirements")
def import_requirements_cmd(
    file: Path = typer.Argument(..., help="Путь к Excel файлу с потребностями"),
    sheet: str = typer.Option("0", "--sheet", "-s", help="Имя или номер листа (по умолчанию первый)"),
) -> None:
    """
    Импортировать потребности работ в материалах из Excel файла.

    Ожидаемые колонки в Excel:
        Код работы, Системный номер, Потребность, Дата начала, Приоритет, ...

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

    # Красивая таблица со статистикой
    _print_import_stats("Потребности", stats)


@app.command(name="import-stock")
def import_stock_cmd(
    file: Path = typer.Argument(..., help="Путь к Excel файлу со складскими остатками"),
    sheet: str = typer.Option("0", "--sheet", "-s", help="Имя или номер листа"),
) -> None:
    """
    Импортировать складские остатки (партии) из Excel файла.

    ВНИМАНИЕ: Существующие остатки будут заменены данными из файла!
    Это соответствует логике "снимок склада на текущий момент".

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

    _print_import_stats("Остатки", stats)


@app.command(name="import-supplies")
def import_supplies_cmd(
    file: Path = typer.Argument(..., help="Путь к Excel файлу с поставками"),
    sheet: str = typer.Option("0", "--sheet", "-s", help="Имя или номер листа"),
) -> None:
    """
    Импортировать поставки (материалы в пути) из Excel файла.

    ВНИМАНИЕ: Существующие поставки будут заменены данными из файла!

    Пример:
        maps import-supplies data/supplies.xlsx
    """
    if not file.exists():
        console.print(f"[red]✗ Файл не найден: {file}[/red]")
        raise typer.Exit(code=1)

    console.print(f"Импорт поставок из: [cyan]{file}[/cyan]")

    from app.services.import_service import import_supplies

    with console.status("Импортируем данные..."):
        try:
            sheet_name: int | str = int(sheet) if sheet.isdigit() else sheet
            stats = import_supplies(file, sheet_name=sheet_name)
        except Exception as e:
            console.print(f"[red]✗ Ошибка импорта: {e}[/red]")
            raise typer.Exit(code=1)

    _print_import_stats("Поставки", stats)


# =============================================================================
# Команда: allocate — запуск распределения
# =============================================================================

@app.command()
def allocate(
    session_id: Optional[str] = typer.Option(
        None, "--session", "-s",
        help="ID сессии (по умолчанию генерируется автоматически)"
    ),
) -> None:
    """
    Запустить алгоритм распределения материалов.

    Алгоритм:
        1. Сортирует работы по приоритету
        2. Распределяет материалы со складов (FIFO)
        3. Резервирует из поставок (если склада не хватает)
        4. Фиксирует дефицит

    Результаты сохраняются в БД. Используйте 'maps export' для Excel-отчёта.

    Пример:
        maps allocate
        maps allocate --session "сценарий_2026_05"
    """
    from app.allocation.engine import AllocationEngine
    from app.db.database import get_session

    console.print(Panel(
        "[bold]Запуск распределения материалов[/bold]",
        title="MAPS — Распределение",
    ))

    with console.status("Выполняется распределение..."):
        try:
            with get_session() as session:
                engine = AllocationEngine(session, session_id=session_id)
                result_session = engine.run()
        except Exception as e:
            console.print(f"[red]✗ Ошибка распределения: {e}[/red]")
            raise typer.Exit(code=1)

    # Итоговая таблица результатов
    console.print(f"\n[green]✓ Распределение завершено![/green]")
    console.print(f"  ID сессии: [cyan]{result_session.id}[/cyan]")

    table = Table(title="Результаты распределения")
    table.add_column("Показатель", style="bold")
    table.add_column("Значение", justify="right")
    table.add_row("Обработано потребностей", str(result_session.total_requirements))
    table.add_row("Строк распределения",    str(result_session.total_allocated))
    table.add_row("Позиций дефицита",       str(result_session.total_deficit), style="red" if result_session.total_deficit > 0 else "green")
    console.print(table)

    console.print(f"\nДля экспорта в Excel:")
    console.print(f"  [cyan]maps export {result_session.id}[/cyan]")


# =============================================================================
# Команда: export — экспорт результатов
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
    Экспортировать результаты распределения в Excel.

    Создаёт файл с 4 листами:
        - Распределение
        - Движение склада
        - Дефицит
        - Обеспеченность

    Пример:
        maps export 20260523_143000_abc12
        maps export 20260523_143000_abc12 --output /tmp/reports
    """
    from app.services.export_service import export_allocation_results

    console.print(f"Экспорт сессии: [cyan]{session_id}[/cyan]")

    with console.status("Формируем Excel отчёт..."):
        try:
            file_path = export_allocation_results(session_id, output_dir=output_dir)
        except Exception as e:
            console.print(f"[red]✗ Ошибка экспорта: {e}[/red]")
            raise typer.Exit(code=1)

    console.print(f"[green]✓ Файл создан: {file_path}[/green]")


# =============================================================================
# Команда: status — состояние системы
# =============================================================================

@app.command()
def status() -> None:
    """
    Показать текущее состояние базы данных.

    Выводит количество записей в каждой таблице и последние сессии.

    Пример:
        maps status
    """
    from sqlalchemy import text

    from app.db.database import get_session

    console.print(Panel("[bold]Состояние базы данных MAPS[/bold]", title="MAPS — Статус"))

    try:
        with get_session() as session:
            # Считаем записи в каждой таблице
            tables = [
                ("works", "Работы"),
                ("materials", "Материалы"),
                ("requirements", "Потребности"),
                ("warehouses", "Склады"),
                ("stock_batches", "Партии на складах"),
                ("supplies", "Поставки"),
                ("supply_lines", "Строки поставок"),
                ("allocation_sessions", "Сессии распределения"),
                ("allocation_results", "Строки распределения"),
                ("deficit_records", "Записи дефицита"),
            ]

            table = Table(title="Количество записей")
            table.add_column("Таблица", style="bold")
            table.add_column("Записей", justify="right")

            for table_name, label in tables:
                try:
                    count = session.execute(
                        text(f"SELECT COUNT(*) FROM {table_name}")  # noqa: S608
                    ).scalar()
                    table.add_row(label, str(count))
                except Exception:
                    table.add_row(label, "[red]ошибка[/red]")

            console.print(table)

            # Последние 5 сессий
            try:
                sessions = session.execute(text("""
                    SELECT id, status, started_at,
                           total_requirements, total_deficit
                    FROM allocation_sessions
                    ORDER BY started_at DESC
                    LIMIT 5
                """)).fetchall()

                if sessions:
                    sess_table = Table(title="Последние сессии распределения")
                    sess_table.add_column("ID сессии")
                    sess_table.add_column("Статус")
                    sess_table.add_column("Дата")
                    sess_table.add_column("Потребностей", justify="right")
                    sess_table.add_column("Дефицит", justify="right")

                    for s in sessions:
                        sess_table.add_row(
                            s.id, s.status,
                            str(s.started_at)[:19],
                            str(s.total_requirements),
                            str(s.total_deficit),
                        )
                    console.print(sess_table)
            except Exception:
                pass  # Таблица ещё не создана

    except Exception as e:
        console.print(f"[red]✗ Ошибка подключения к БД: {e}[/red]")
        raise typer.Exit(code=1)


# =============================================================================
# Вспомогательные функции
# =============================================================================

def _print_import_stats(name: str, stats: dict[str, int]) -> None:
    """Красиво вывести статистику импорта."""
    table = Table(title=f"Результат импорта: {name}")
    table.add_column("Показатель", style="bold")
    table.add_column("Значение", justify="right")

    for key, value in stats.items():
        color = "red" if key == "errors" and value > 0 else "green"
        table.add_row(key, f"[{color}]{value}[/{color}]")

    console.print(table)


# =============================================================================
# Точка входа
# =============================================================================

if __name__ == "__main__":
    # При прямом запуске: python main.py <команда>
    app()
