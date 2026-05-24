"""add nazvanie to works

Что делает эта миграция?
    Добавляет колонку «nazvanie» (наименование работы) в таблицу works.
    Колонка хранит текстовое описание работы из «Перечня работ».

    Зачем нужна отдельная колонка?
        Код работы (kod_raboty) — технический идентификатор из SAP, например «WO-12345».
        Наименование — человекочитаемое описание: «Ремонт насосного агрегата ЦНС-60».
        Разделение позволяет загружать наименования отдельно от потребностей.

    Порядок загрузки данных после миграции:
        1. maps import-works works_list.xlsx      — загружаем наименования
        2. maps import-requirements req.xlsx      — потребности (работы уже есть → обновление)
        3. maps allocate                          — распределение

    Тип данных:
        Text — неограниченная длина строки (PostgreSQL TEXT).
        nullable=True — поле необязательное (не все работы могут иметь наименование).

Revision ID: a3f1c8e20b47
Revises: 9aff7eeed93d
Create Date: 2026-05-24

"""
from alembic import op
import sqlalchemy as sa

# ── Идентификаторы миграции ──
# revision: уникальный ID этой миграции
# down_revision: ID предыдущей (родительской) миграции — цепочка миграций
revision = 'a3f1c8e20b47'
down_revision = '9aff7eeed93d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Применить миграцию: добавить колонку nazvanie в таблицу works.

    op.add_column — добавляет новую колонку в существующую таблицу.
    Существующие строки получат NULL в этой колонке (nullable=True).
    Данные в остальных колонках не затрагиваются.
    """
    op.add_column(
        'works',                                # Имя таблицы в PostgreSQL
        sa.Column(
            'nazvanie',                         # Имя новой колонки
            sa.Text(),                          # Тип: TEXT (неограниченная длина)
            nullable=True,                      # Разрешаем NULL (поле необязательное)
        )
    )


def downgrade() -> None:
    """
    Откатить миграцию: удалить колонку nazvanie из таблицы works.

    op.drop_column удаляет колонку вместе со всеми данными в ней.
    Используется при откате: alembic downgrade -1
    """
    op.drop_column('works', 'nazvanie')
