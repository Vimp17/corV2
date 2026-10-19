"""Generate demo documents for the document pipeline (synthetic, for the DEMO-* wells).

    python scripts/build_demo_documents.py                      # dates relative to today
    python scripts/build_demo_documents.py --end-date 2026-10-19 --out examples/documents

Needs reportlab and Pillow (pip install reportlab pillow) and a DejaVu Sans font. The files
match the sample ДАРС / ХИМИК / ЭРА data of scripts/build_sample_data.py (same --end-date):
  akt_razbora_DEMO-002.pdf          PDF with a text layer: base-inspection act for DEMO-002,
                                    whose demount had no base act (closes the "нет акта" gap);
  akt_demontazha_DEMO-005_scan.jpg  scanned demount act (read by the OCR model);
  protokol_khim_analiza_15.xlsx     laboratory protocol kept as an Excel form, DEMO-012/013;
  pasport_DEMO-011.pdf              well passport: field, equipment with coated tubing, run date;
  sluzhebnaya_zapiska.pdf           a memo — not one of the document types ("не распознан").
Every value is synthetic and describes no real company, equipment or person.
"""
from __future__ import annotations

import argparse
import random
from datetime import date, timedelta
from pathlib import Path

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/DejaVuSans.ttf",
]


def _font_path() -> str:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("DejaVuSans.ttf not found; install fonts-dejavu or edit FONT_CANDIDATES.")


def d(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def base_act_lines(end: date) -> list[str]:
    return [
        "ООО «Демо-нефть» (синтетический пример)",
        "",
        "АКТ РАЗБОРА ОБОРУДОВАНИЯ НА БАЗЕ № 47",
        "",
        f"Дата разбора: {d(end - timedelta(days=141))}",
        "Месторождение: Озёрное",
        "Скважина № DEMO-002",
        "Оборудование: ЭЦН5-60, НКТ 73 мм гладкие (без покрытия)",
        "",
        "Комиссия в составе мастера базы и инженера-технолога провела разбор оборудования,",
        "поднятого со скважины после демонтажа.",
        "",
        "Результаты разбора: на внутренней поверхности НКТ обнаружены язвенные поражения",
        "глубиной до 2,1 мм, на муфтах — отложения солей.",
        "Коррозия обнаружена: да",
        "Вид коррозии: язвенная (углекислотная)",
        "Место обнаружения: тело трубы НКТ, 3–5 трубы от насоса",
        "Заключение: причина отказа — коррозионный износ НКТ.",
        "",
        "Мастер базы ____________ / Иванов И. И. (вымышленное лицо)",
        "Инженер-технолог ____________ / Петров П. П. (вымышленное лицо)",
    ]


def scan_lines(end: date) -> list[str]:
    return [
        "АКТ ДЕМОНТАЖА СКВАЖИННОГО ОБОРУДОВАНИЯ",
        "ООО «Демо-нефть» (синтетический пример)",
        f"Дата демонтажа: {d(end - timedelta(days=9))}",
        "Месторождение: Озёрное",
        "Скважина № DEMO-005",
        "Демонтированное оборудование: ЭЦН5-125, НКТ 73 мм",
        "Причина демонтажа: снижение подачи, негерметичность НКТ",
        "Описание дефектов: свищ в теле 12-й трубы НКТ, следы коррозии",
        "Коррозия обнаружена: да",
        "Мастер ____________ (вымышленное лицо)",
    ]


def passport_lines(end: date) -> list[str]:
    return [
        "ПАСПОРТ СКВАЖИНЫ",
        "ООО «Демо-нефть» (синтетический пример)",
        "",
        "Скважина № DEMO-011",
        "Месторождение: Озёрное",
        "Назначение: добывающая",
        f"Дата составления паспорта: {d(end - timedelta(days=20))}",
        "",
        "Конструкция и оборудование",
        "Спущенное оборудование: ЭЦН5-50, НКТ 73 мм с внутренним полимерным покрытием",
        f"Дата спуска оборудования: {d(end - timedelta(days=200))}",
        "Толщина стенки НКТ: 5,5 мм",
        "",
        "Паспорт составлен по данным эксплуатационной службы (вымышленные данные).",
    ]


def memo_lines(end: date) -> list[str]:
    return [
        "СЛУЖЕБНАЯ ЗАПИСКА",
        f"от {d(end - timedelta(days=3))}",
        "",
        "О проведении планового совещания по итогам квартала.",
        "Прошу обеспечить явку руководителей участков в конференц-зал в 10:00.",
        "",
        "Начальник отдела (вымышленное лицо)",
    ]


def lab_rows(end: date) -> list[list]:
    return [
        ["DEMO-012", d(end - timedelta(days=6)), 44, 21500, 1.2, 0.11, 0.86],
        ["DEMO-013", d(end - timedelta(days=6)), 51, 19800, 1.1, 0.12, 0.84],
    ]


def write_pdf(path: Path, lines: list[str], font: str) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(TTFont("DejaVuSans", font))
    page = canvas.Canvas(str(path), pagesize=A4)
    page.setTitle(path.stem)
    width, height = A4
    y = height - 60
    for index, line in enumerate(lines):
        bold_title = index < 4 and line.isupper()
        page.setFont("DejaVuSans", 14 if bold_title else 11)
        if bold_title:
            page.drawCentredString(width / 2, y, line)
        else:
            page.drawString(55, y, line)
        y -= 20 if line else 10
    page.showPage()
    page.save()


def write_scan(path: Path, lines: list[str], font: str) -> None:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    image = Image.new("L", (1240, 1754), 250)   # A4 at 150 dpi, grey paper
    draw = ImageDraw.Draw(image)
    title = ImageFont.truetype(font, 34)
    body = ImageFont.truetype(font, 28)
    y = 120
    for index, line in enumerate(lines):
        draw.text((110, y), line, fill=25, font=title if index == 0 else body)
        y += 62 if index == 0 else 52
    rng = random.Random(7)
    for _ in range(2500):                       # paper noise
        draw.point((rng.randrange(1240), rng.randrange(1754)), fill=rng.randrange(150, 230))
    image = image.rotate(0.8, fillcolor=250, resample=Image.BICUBIC).filter(ImageFilter.GaussianBlur(0.6))
    image.save(path, quality=80)


def write_lab_workbook(path: Path, end: date) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Протокол"
    sheet["A1"] = "ПРОТОКОЛ ХИМИЧЕСКОГО АНАЛИЗА № 15"
    sheet["A1"].font = Font(bold=True, size=13)
    sheet["A2"] = "Лаборатория ООО «Демо-нефть» (синтетический пример)"
    sheet["A3"] = f"Дата протокола: {d(end - timedelta(days=5))}"
    sheet["A4"] = "Анализ попутно-добываемой воды"
    headers = ["Скважина", "Дата отбора пробы", "Обводнённость, %", "Хлориды, мг/л", "CO2, %",
               "Скорость коррозии, мм/год", "Эффективность ингибитора"]
    for column, header in enumerate(headers, start=1):
        sheet.cell(6, column, header).font = Font(bold=True)
        sheet.column_dimensions[chr(64 + column)].width = 22
    for row_index, row in enumerate(lab_rows(end), start=7):
        for column, value in enumerate(row, start=1):
            sheet.cell(row_index, column, value)
    sheet["A10"] = "Лаборант ____________ (вымышленное лицо)"
    workbook.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--out", type=Path, default=Path("examples/documents"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    font = _font_path()
    write_pdf(args.out / "akt_razbora_DEMO-002.pdf", base_act_lines(args.end_date), font)
    write_scan(args.out / "akt_demontazha_DEMO-005_scan.jpg", scan_lines(args.end_date), font)
    write_lab_workbook(args.out / "protokol_khim_analiza_15.xlsx", args.end_date)
    write_pdf(args.out / "pasport_DEMO-011.pdf", passport_lines(args.end_date), font)
    write_pdf(args.out / "sluzhebnaya_zapiska.pdf", memo_lines(args.end_date), font)
    print(f"Demo documents written to {args.out}")


if __name__ == "__main__":
    main()
