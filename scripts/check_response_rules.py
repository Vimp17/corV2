"""Validate the draft register without SQL, LLM calls or changing the application."""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError
from app.core.response_rules import DEFAULT_PATH, load_rulebook


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка структуры реестра правил и мероприятий")
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args()
    try:
        book = load_rulebook(args.path)
    except (OSError, ValidationError) as exc:
        print(f"Ошибка реестра: {exc}", file=sys.stderr)
        return 1
    print(f"Реестр, редакция {book.revision}: структура корректна.")
    for title, records in (("Мероприятия", book.rules),):
        counts = Counter(item.status for item in records)
        print(f"{title}: " + ", ".join(f"{status}={counts[status]}" for status in
                                    ("draft", "review", "approved", "retired", "test")))
    print("Проверка структуры не подтверждает методику. Правила test применяются только в режиме demo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
