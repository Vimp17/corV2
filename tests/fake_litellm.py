"""A stand-in for LiteLLM in tests and local demos: OCR, classification, extraction, proposal.

It answers the same Chat Completions requests the service sends, with simple rules instead of
a model: "OCR" returns the text the test registered for the next page, extraction reads
"Поле: значение" lines and "a | b | c" tables, the proposal follows the rating measure. Run it
as a server for a local demo of the dashboard without the real models:

    python -m tests.fake_litellm --port 4010 --end-date 2026-10-19
    MAI_LLM_API_URL=http://127.0.0.1:4010/v1/chat/completions MAI_LLM_MODEL=fake \
    MAI_OCR_MODEL=fake-ocr uvicorn app.main:app
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime

ALIASES = {
    "well_id": ["скважина №", "номер скважины", "скв. №", "скважина"],
    "act_date": ["дата разбора", "дата демонтажа", "дата акта"],
    "sample_date": ["дата отбора пробы", "дата отбора"],
    "passport_date": ["дата составления паспорта", "дата паспорта"],
    "install_date": ["дата спуска оборудования", "дата спуска"],
    "field": ["месторождение"],
    "equipment": ["демонтированное оборудование", "спущенное оборудование", "оборудование"],
    "cause": ["заключение", "причина демонтажа"],
    "corrosion_detected": ["коррозия обнаружена", "обнаружена коррозия"],
    "corrosion_description": ["описание дефектов", "результаты разбора"],
    "corrosion_type": ["вид коррозии"],
    "corrosion_location": ["место обнаружения"],
    "initial_wall_thickness_mm": ["толщина стенки нкт"],
}
TABLE_COLUMNS = {
    "well_id": "скважина", "sample_date": "дата", "water_cut_pct": "обводн", "chlorides_mg_l": "хлорид",
    "co2_pct": "co2", "corrosion_rate_mm_year": "скорость коррозии", "inhibitor_efficiency": "эффективность ингибитора",
}


def _low(text: str) -> str:
    return text.lower().replace("ё", "е")


def _convert(raw: str, field_type: str):
    raw = raw.strip()
    if not raw:
        return None
    if field_type == "date":
        match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", raw)
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1))).isoformat() if match else None
    if field_type == "number":
        match = re.search(r"\d+(?:[.,]\d+)?", raw)
        return float(match.group(0).replace(",", ".")) if match else None
    if field_type == "boolean":
        word = _low(raw)
        return True if word.startswith("да") else False if word.startswith("нет") else None
    return raw


def _from_lines(text: str, fields: dict) -> dict:
    record = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for name, spec in fields.items():
        record[name] = {"value": None, "quote": None}
        if spec["type"] == "measurements":
            continue
        for alias in ALIASES.get(name, []):
            found = False
            for line in lines:
                low = _low(line)
                position = low.find(alias)
                if position != 0:
                    continue
                rest = line[position + len(alias):].lstrip(" №:").strip()
                value = _convert(rest, spec["type"])
                if value is not None:
                    record[name] = {"value": value, "quote": line[:200]}
                    found = True
                    break
            if found:
                break
    return record


def _from_table(text: str, fields: dict) -> list[dict]:
    lines = [line.strip() for line in text.splitlines() if " | " in line]
    header_index = next((index for index, line in enumerate(lines) if "скважина" in _low(line)), None)
    if header_index is None:
        return []
    titles = [cell.strip() for cell in lines[header_index].split("|")]
    headers = [_low(title) for title in titles]
    columns = {name: next((index for index, header in enumerate(headers) if key in header), None)
               for name, key in TABLE_COLUMNS.items()}
    mapped = {index for index in columns.values() if index is not None}
    records = []
    for line in lines[header_index + 1:]:
        cells = [cell.strip() for cell in line.split("|")]
        record = {}
        for name, spec in fields.items():
            if spec["type"] == "measurements":
                # Columns that are not fields of the type become "other indicators".
                others = []
                for index, title in enumerate(titles):
                    if index in mapped or index >= len(cells) or not title:
                        continue
                    number = _convert(cells[index], "number")
                    if number is not None:
                        others.append({"name": title, "value": number, "quote": line[:200]})
                record[name] = {"value": others or None}
                continue
            index = columns.get(name)
            value = _convert(cells[index], spec["type"]) if index is not None and index < len(cells) else None
            record[name] = {"value": value, "quote": line[:200] if value is not None else None}
        if record.get("well_id", {}).get("value"):
            records.append(record)
    return records


class FakeLiteLLM:
    def __init__(self, ocr_text: str = "") -> None:
        self.ocr_text = ocr_text
        self.calls: list[dict] = []

    # The signature of app.core.llm.chat, so tests can monkeypatch it directly.
    def chat(self, messages: list[dict], *, model: str, **_kwargs) -> str:
        return self.respond({"model": model, "messages": messages})

    def respond(self, body: dict) -> str:
        messages = body["messages"]
        system, user = messages[0]["content"], messages[-1]["content"]
        if isinstance(user, list):
            self.calls.append({"kind": "ocr", "model": body["model"]})
            return self.ocr_text
        payload = json.loads(user)
        if "Определи тип документа" in system:
            self.calls.append({"kind": "classify"})
            return json.dumps({"type": "other"})
        if "Ты извлекаешь данные" in system:
            self.calls.append({"kind": "extract", "type": payload["document_type"]})
            text, fields = payload["document_text"], payload["fields"]
            records = _from_table(text, fields) if payload["multiple_records"] else [_from_lines(text, fields)]
            return "```json\n" + json.dumps({"records": records}, ensure_ascii=False) + "\n```"
        if "инженер-технолог" in system:
            self.calls.append({"kind": "proposal", "well_id": payload["well_id"]})
            rating = payload["rating"]
            crew = next((item for item in payload["crews"] if item.get("status") == "available"), None)
            example = next((item for item in payload["response_examples"]
                            if item.get("measure") == rating.get("measure_code")), None)
            return json.dumps({
                "measure": rating.get("measure_code") or "—",
                "summary": f"Скважина {payload['well_id']}: {rating.get('measure')}.",
                "actions": (example or {}).get("actions") or ["Мониторинг в обычном режиме"],
                "deadline": rating.get("deadline"),
                "crew": {"crew_id": crew["crew_id"], "date": rating.get("deadline")} if crew else None,
                "reasoning": f"Индекс {rating.get('index')}; категория {rating.get('category')}.",
                "missing_data": [],
                "based_on": [f"{act['type']} {act['date']}" for act in payload["acts"]][:5],
            }, ensure_ascii=False)
        return "{}"


def serve(port: int, end_date: date) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from scripts.build_demo_documents import scan_lines

    fake = FakeLiteLLM("\n".join(scan_lines(end_date)))

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 (http.server API)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            content = fake.respond(body)
            data = json.dumps({"id": "fake", "object": "chat.completion", "created": int(datetime.now().timestamp()),
                               "model": body.get("model"),
                               "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                            "finish_reason": "stop"}]}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4010)
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    serve(args.port, args.end_date)
