"""
Конвертация Markdown-файлов документации MAPS в PDF.
Использует reportlab + шрифт DejaVu для поддержки кириллицы.
"""

import re
import sys
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Preformatted,
    HRFlowable, Table, TableStyle, KeepTogether
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ── Шрифты ──────────────────────────────────────────────────────────────────
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
pdfmetrics.registerFont(TTFont("DejaVu",         FONT_DIR / "DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("DejaVu-Bold",    FONT_DIR / "DejaVuSans-Bold.ttf"))
pdfmetrics.registerFont(TTFont("DejaVu-Italic",  FONT_DIR / "DejaVuSans-Oblique.ttf"))
pdfmetrics.registerFont(TTFont("DejaVu-Mono",    FONT_DIR / "DejaVuSansMono.ttf"))
pdfmetrics.registerFont(TTFont("DejaVu-MonoBold",FONT_DIR / "DejaVuSansMono-Bold.ttf"))

# ── Стили ────────────────────────────────────────────────────────────────────
BASE_SIZE = 10
LEAD = 14

STYLES = {
    "normal": ParagraphStyle("normal", fontName="DejaVu",     fontSize=BASE_SIZE, leading=LEAD, spaceAfter=4),
    "h1":     ParagraphStyle("h1",     fontName="DejaVu-Bold", fontSize=18, leading=24, spaceAfter=8, spaceBefore=14, textColor=colors.HexColor("#1a1a2e")),
    "h2":     ParagraphStyle("h2",     fontName="DejaVu-Bold", fontSize=14, leading=20, spaceAfter=6, spaceBefore=12, textColor=colors.HexColor("#16213e")),
    "h3":     ParagraphStyle("h3",     fontName="DejaVu-Bold", fontSize=12, leading=16, spaceAfter=4, spaceBefore=8,  textColor=colors.HexColor("#0f3460")),
    "h4":     ParagraphStyle("h4",     fontName="DejaVu-Bold", fontSize=10, leading=14, spaceAfter=3, spaceBefore=6),
    "bullet": ParagraphStyle("bullet", fontName="DejaVu",     fontSize=BASE_SIZE, leading=LEAD, spaceAfter=3, leftIndent=12, bulletIndent=0),
    "code":   ParagraphStyle("code",   fontName="DejaVu-Mono", fontSize=8,  leading=12, textColor=colors.HexColor("#333333")),
    "quote":  ParagraphStyle("quote",  fontName="DejaVu-Italic", fontSize=BASE_SIZE, leading=LEAD, spaceAfter=4, leftIndent=16, textColor=colors.HexColor("#555555")),
}


def escape_xml(text: str) -> str:
    """Экранирование символов для XML/PDF."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def inline_format(text: str) -> str:
    """Применяет инлайновое форматирование: **bold**, `code`, *italic*.
    Порядок: код сначала вырезается в плейсхолдеры, потом bold/italic, потом подставляется.
    Это предотвращает конфликт `*` внутри кода с italic-паттерном.
    """
    # Шаг 1: вырезаем `code` в плейсхолдеры
    placeholders: list[str] = []

    def save_code(m: re.Match) -> str:
        idx = len(placeholders)
        placeholders.append(f'<font name="DejaVu-Mono" size="8">{escape_xml(m.group(1))}</font>')
        return f"\x00CODE{idx}\x00"

    text = re.sub(r'`([^`]+)`', save_code, text)

    # Шаг 2: **bold** и __bold__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__',     r'<b>\1</b>', text)

    # Шаг 3: *italic* — только одиночные звёздочки (после удаления ** выше)
    text = re.sub(r'\*([^*\n]+?)\*', r'<i>\1</i>', text)

    # Шаг 4: возвращаем `code` из плейсхолдеров
    for idx, code_html in enumerate(placeholders):
        text = text.replace(f"\x00CODE{idx}\x00", code_html)

    return text


def parse_table(lines: list[str]) -> Table | None:
    """Разбирает Markdown-таблицу в объект Table."""
    rows = []
    for line in lines:
        if re.match(r'\s*\|[-:| ]+\|\s*$', line):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        rows.append(cells)
    if not rows:
        return None

    col_count = max(len(r) for r in rows)
    data = []
    for i, row in enumerate(rows):
        row += [''] * (col_count - len(row))
        font = "DejaVu-Bold" if i == 0 else "DejaVu"
        data.append([
            Paragraph(inline_format(escape_xml(cell)), ParagraphStyle(
                f"cell{i}", fontName=font, fontSize=9, leading=13
            ))
            for cell in row
        ])

    col_w = (A4[0] - 40*mm) / col_count
    t = Table(data, colWidths=[col_w] * col_count, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  colors.HexColor("#dee2e6")),
        ("FONTNAME",      (0, 0), (-1, 0),  "DejaVu-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#adb5bd")),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, colors.HexColor("#f8f9fa")]),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
    ]))
    return t


def md_to_flowables(md_text: str) -> list:
    """Преобразует Markdown в список объектов ReportLab (flowables)."""
    flowables = []
    lines = md_text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]

        # ── Блоки кода (```) ─────────────────────────────────────────────────
        if line.strip().startswith("```"):
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            # Каждую строку кода — отдельным Preformatted (позволяет разбивать по страницам)
            flowables.append(Spacer(1, 4))
            for code_line in code_lines:
                flowables.append(Preformatted(code_line, STYLES["code"]))
            flowables.append(Spacer(1, 4))
            i += 1
            continue

        # ── Markdown-таблицы ─────────────────────────────────────────────────
        if line.strip().startswith("|") and "|" in line:
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i])
                i += 1
            tbl = parse_table(table_lines)
            if tbl:
                flowables.append(Spacer(1, 4))
                flowables.append(tbl)
                flowables.append(Spacer(1, 6))
            continue

        # ── Горизонтальная черта ──────────────────────────────────────────────
        if re.match(r'^---+\s*$', line):
            flowables.append(Spacer(1, 4))
            flowables.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")))
            flowables.append(Spacer(1, 4))
            i += 1
            continue

        # ── Заголовки ─────────────────────────────────────────────────────────
        m = re.match(r'^(#{1,4})\s+(.+)', line)
        if m:
            level = len(m.group(1))
            text = escape_xml(m.group(2).strip())
            text = inline_format(text)
            style = STYLES.get(f"h{level}", STYLES["h4"])
            flowables.append(Paragraph(text, style))
            i += 1
            continue

        # ── Цитата (>) ───────────────────────────────────────────────────────
        if line.startswith(">"):
            text = line.lstrip("> ").strip()
            text = inline_format(escape_xml(text))
            flowables.append(Paragraph(text, STYLES["quote"]))
            i += 1
            continue

        # ── Маркированный список ──────────────────────────────────────────────
        m = re.match(r'^(\s*)[-*+]\s+(.*)', line)
        if m:
            indent = len(m.group(1))
            text = inline_format(escape_xml(m.group(2)))
            bullet_char = "•" if indent == 0 else "◦"
            left = 12 + indent * 8
            style = ParagraphStyle(
                f"bullet_{indent}", fontName="DejaVu", fontSize=BASE_SIZE,
                leading=LEAD, spaceAfter=2, leftIndent=left, bulletIndent=left - 12,
            )
            flowables.append(Paragraph(f"{bullet_char}  {text}", style))
            i += 1
            continue

        # ── Нумерованный список ───────────────────────────────────────────────
        m = re.match(r'^(\s*)\d+\.\s+(.*)', line)
        if m:
            indent = len(m.group(1))
            num = re.match(r'^\s*(\d+)\.', line).group(1)
            text = inline_format(escape_xml(m.group(2)))
            left = 16 + indent * 8
            style = ParagraphStyle(
                f"num_{indent}", fontName="DejaVu", fontSize=BASE_SIZE,
                leading=LEAD, spaceAfter=2, leftIndent=left, bulletIndent=left - 16,
            )
            flowables.append(Paragraph(f"{num}.  {text}", style))
            i += 1
            continue

        # ── Пустая строка ──────────────────────────────────────────────────────
        if not line.strip():
            flowables.append(Spacer(1, 4))
            i += 1
            continue

        # ── Обычный абзац ─────────────────────────────────────────────────────
        text = inline_format(escape_xml(line.strip()))
        flowables.append(Paragraph(text, STYLES["normal"]))
        i += 1

    return flowables


def build_pdf(md_path: Path, out_path: Path) -> None:
    print(f"  {md_path.name}  →  {out_path.name}")
    md_text = md_path.read_text(encoding="utf-8")

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=20*mm, bottomMargin=20*mm,
        title=md_path.stem,
    )

    flowables = md_to_flowables(md_text)
    doc.build(flowables)
    size_kb = out_path.stat().st_size // 1024
    print(f"  ✓  {out_path}  ({size_kb} KB)")


if __name__ == "__main__":
    docs_dir = Path(__file__).parent
    root_dir = docs_dir.parent

    files = {
        root_dir / "README.md":        docs_dir / "README.pdf",
        docs_dir / "ИНСТРУКЦИЯ.md":    docs_dir / "ИНСТРУКЦИЯ.pdf",
    }

    print("Генерация PDF-документации MAPS\n")
    for md_file, pdf_file in files.items():
        if not md_file.exists():
            print(f"  ✗  Файл не найден: {md_file}")
            sys.exit(1)
        build_pdf(md_file, pdf_file)

    print("\nГотово.")
