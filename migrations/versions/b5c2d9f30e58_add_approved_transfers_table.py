"""add approved_transfers table

Что делает эта миграция?
    Создаёт таблицу approved_transfers для хранения согласованных
    межфилиальных перемещений материалов.

    Зачем нужна эта таблица?
        После первого запуска распределения пользователь анализирует лист
        «Возможное перемещение» в отчёте. Там видно: какой склад другого
        филиала мог бы покрыть дефицит. Пользователь согласовывает
        конкретные перемещения и загружает их Excel-файлом.

        При повторном распределении система использует согласованные
        перемещения как дополнительный источник (Слой 3в):
          — Остатки склада-источника УМЕНЬШАЮТСЯ (реальное списание)
          — Дефицит для работ-получателей покрывается

    Структура таблицы:
        material_id        — какой материал перемещается (→ materials.id)
        from_warehouse_id  — склад-источник (→ warehouses.id)
        to_filial          — филиал-получатель (куда идёт материал)
        kolichestvo        — согласованное количество (исходное, не меняется)
        dostupno           — доступный остаток (уменьшается при распределении)
        uploaded_at        — когда загружено (для истории/аудита)

    Поле dostupno сбрасывается (= kolichestvo) перед каждым запуском
    распределения, чтобы результат был воспроизводимым.

Revision ID: b5c2d9f30e58
Revises: a3f1c8e20b47
Create Date: 2026-05-28

"""
from alembic import op
import sqlalchemy as sa

# ── Идентификаторы миграции ──
revision = 'b5c2d9f30e58'
down_revision = 'a3f1c8e20b47'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Создать таблицу approved_transfers и индекс на (material_id, to_filial).

    Почему составной индекс на (material_id, to_filial)?
        При распределении движок ищет согласованные перемещения по паре
        (материал, филиал-получатель). Без индекса — полный скан таблицы
        при каждом обращении, что медленно при большом числе записей.
    """
    op.create_table(
        'approved_transfers',

        # Первичный ключ — автоинкрементный целочисленный ID
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),

        # Связь с таблицей materials: какой материал перемещается
        sa.Column('material_id', sa.Integer(), nullable=False),

        # Связь с таблицей warehouses: со склада какого филиала берём
        sa.Column('from_warehouse_id', sa.Integer(), nullable=False),

        # Строковое название филиала-получателя (как в таблице works.filial)
        sa.Column('to_filial', sa.String(100), nullable=False),

        # Согласованное количество — исходное значение из Excel, не изменяется
        sa.Column('kolichestvo', sa.Numeric(18, 4), nullable=False, server_default='0'),

        # Доступный остаток — уменьшается по мере распределения
        # Сбрасывается (= kolichestvo) перед каждым запуском алгоритма
        sa.Column('dostupno', sa.Numeric(18, 4), nullable=False, server_default='0'),

        # Дата и время загрузки файла (автоматически проставляет PostgreSQL)
        sa.Column('uploaded_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),

        # Первичный ключ
        sa.PrimaryKeyConstraint('id'),

        # Внешний ключ → materials
        sa.ForeignKeyConstraint(
            ['material_id'],
            ['materials.id'],
            name='fk_approved_transfers_material_id',
        ),

        # Внешний ключ → warehouses
        sa.ForeignKeyConstraint(
            ['from_warehouse_id'],
            ['warehouses.id'],
            name='fk_approved_transfers_from_warehouse_id',
        ),
    )

    # Индекс для быстрого поиска по (material_id, to_filial)
    # Это основной паттерн доступа в движке распределения
    op.create_index(
        'ix_approved_transfers_material_filial',
        'approved_transfers',
        ['material_id', 'to_filial'],
    )


def downgrade() -> None:
    """
    Откатить миграцию: удалить индекс и таблицу approved_transfers.

    ВНИМАНИЕ: все загруженные согласованные перемещения будут удалены.
    """
    op.drop_index('ix_approved_transfers_material_filial', table_name='approved_transfers')
    op.drop_table('approved_transfers')
