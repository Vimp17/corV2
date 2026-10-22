"""The .env file of the standalone build: read, normalise and update it.

The application reads .env itself (app/config.py, UTF-8, variables already set in the environment
win). This module is used by standalone/manage.py and standalone/asgi.py, which must also cope
with a file saved by an old Notepad: a UTF-8 byte order mark or the ANSI code page (cp1251) are
converted back to plain UTF-8 before the application starts.
"""
from __future__ import annotations

from pathlib import Path

BOM = b"\xef\xbb\xbf"


def normalize(path: Path) -> str | None:
    """Rewrite the file as UTF-8 without a byte order mark; returns what was fixed, or None."""
    if not path.is_file():
        return None
    raw = path.read_bytes()
    fixed = None
    if raw.startswith(BOM):
        raw, fixed = raw[len(BOM):], "убрана метка BOM"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text, fixed = raw.decode("cp1251", errors="replace"), "кодировка ANSI (cp1251) переведена в UTF-8"
    if fixed:
        path.write_text(text, encoding="utf-8", newline="")
    return fixed


def read(path: Path) -> dict[str, str]:
    """NAME=value pairs in file order, with the same quoting rules as app/config.py."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    text = path.read_bytes().removeprefix(BOM).decode("utf-8", errors="replace")
    for line in text.splitlines():
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if name:
            values[name] = value
    return values


def render(template: Path, values: dict[str, str]) -> str:
    """The template with its NAME=value lines replaced by the given values.

    Lines of the template keep their order and comments; a name the template comments out
    ("# NAME=…") is uncommented when a value is given. Values without a line in the template
    are added at the end."""
    lines, used = [], set()
    for line in template.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        candidate = stripped[1:].strip() if stripped.startswith("#") else stripped
        name = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        if name and name.replace("_", "").isalnum() and name.isupper() and name in values:
            if stripped.startswith("#") and not values[name]:
                lines.append(line)
            else:
                lines.append(f"{name}={values[name]}")
            used.add(name)
        else:
            lines.append(line)
    extra = [name for name in values if name not in used]
    if extra:
        lines += ["", "# Other settings"] + [f"{name}={values[name]}" for name in extra]
    return "\n".join(lines) + "\n"
