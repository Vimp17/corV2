"""Validate an Excel register and save a complete JSON snapshot."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile
from zipfile import BadZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.response_rules_excel import read_register


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверить Excel-реестр и преобразовать в JSON")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument("--replace", action="store_true", help="Разрешить замену существующего JSON")
    args = parser.parse_args()
    temporary = None
    try:
        if args.output.suffix.lower() != ".json":
            raise ValueError("Выходной файл должен иметь расширение .json")
        if args.output.resolve() == args.source.resolve():
            raise ValueError("Excel и выходной JSON должны быть разными файлами")
        book = read_register(args.source, revision=args.revision)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(book.model_dump_json(indent=2) + "\n")
        if args.replace:
            os.replace(temporary, args.output)
        else:
            os.link(temporary, args.output)  # Fails atomically if destination exists.
        print(f"Проверено и сохранено мероприятий: {len(book.rules)}. Активный JSON перечитывается при новом предложении.")
        return 0
    except (OSError, ValueError, BadZipFile) as exc:
        print(f"Ошибка импорта: {exc}", file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
