"""The well card and the AI agent: data gaps by field, the НТЦ rules, recomputation of one well and the
agent's proposal (its Russian payload, crews, gaps)."""
from __future__ import annotations

from datetime import timedelta

from tests.conftest import TODAY, _upload, _well, needs_db


@needs_db
def test_ai_proposal_uses_rating_crews_and_reports_gaps(client, generated, fake):
    response = client.post("/api/v1/ingestion/crew-schedule?all_wells=true", json={"crew_availability": [
        {"crew_id": "КРС-7", "status": "available", "specialties": ["КРС"],
         "available_from": f"{TODAY.isoformat()}T08:00:00", "available_to": f"{(TODAY + timedelta(days=30)).isoformat()}T20:00:00"},
    ]})
    assert response.status_code == 201, response.text
    _upload(client, generated / "documents" / "sluzhebnaya_zapiska.pdf")
    proposal = client.post("/api/v1/rating/wells/DEMO-002/ai-proposal")
    assert proposal.status_code == 200, proposal.text
    body = proposal.json()
    well = _well(client, "DEMO-002")
    assert body["measure"] == well["measure_code"]
    assert body["crew"]["crew_id"] == "КРС-7"
    assert any("акт разбора на базе после демонтажа" in item for item in body["missing_data"])
    assert well["ai_proposal"]["measure"] == body["measure"]
    assert "ai_proposal" not in client.get("/api/v1/rating").json()["wells"][0]

    # A crew that is not in the schedule is dropped, not invented.
    original = fake.respond
    fake.respond = lambda request: original(request).replace("КРС-7", "КРС-99")
    body = client.post("/api/v1/rating/wells/DEMO-002/ai-proposal").json()
    assert body["crew"] is None
    assert any("КРС-99" in note for note in body["checks"])


def test_data_gaps_name_the_fields_of_each_column():
    from datetime import datetime, timezone

    from app.core.rating_builder import _data_gaps
    from app.core.readiness import Readiness
    from app.schemas import TelemetryPoint

    now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    old = TelemetryPoint(well_id="W-1", timestamp=datetime(2026, 8, 1, 9, tzinfo=timezone.utc), co2_pct=1.2)
    fresh = TelemetryPoint(well_id="W-1", timestamp=datetime(2026, 10, 29, 9, tzinfo=timezone.utc), water_cut_pct=90,
                           chlorides_mg_l=18000, inhibitor_efficiency=0.9, liquid_rate_m3_day=40)
    values = {"field": "Южное", "in_operation": True, "equipment": "ЭЦН-80", "installed_at": now, "oil_pct": 10,
              "category": "medium", "protection": "effective", "sampling": True, "model_ntc": 0.4,
              "demount_act": True, "demount_corrosion": None}
    ready = Readiness(unique_timestamps=8, recent_observations=8, current_signals=["water_cut_pct", "chlorides_mg_l"])
    gaps = {gap["key"]: gap for gap in _data_gaps(values, [old, fresh], {"water_cut_pct": 90, "chlorides_mg_l": 18000,
                                                  "inhibitor_efficiency": 0.9}, ready, [], [], [], now, [])}
    # Filled columns are not listed, whichever source filled them.
    assert set(gaps) == {"category", "environment", "acts", "protection"}
    assert gaps["environment"]["note"] == "устарело: CO₂ (последнее значение 01.08.2026)"
    assert gaps["category"]["note"] == "нет: скорость коррозии"
    assert gaps["acts"]["note"] == "нет: признак коррозии в акте демонтажа"
    assert gaps["protection"]["note"] == "нет: отклонение закачки ингибитора"   # asked for only with an inhibitor
    # The table names the values: CO₂, скорость коррозии…
    assert [item["short"] for item in gaps["environment"]["items"]] == ["CO₂"]

    # No forecast: what is short, by indicator — the measurements of each, outdated and missing values.
    short = Readiness(unique_timestamps=2, recent_observations=2, current_signals=["water_cut_pct"])
    empty = {gap["key"]: gap for gap in _data_gaps({}, [fresh], {}, short, [], [], [], now, [])}
    note = empty["forecast"]["note"]
    assert note.startswith("модели не рассчитаны: замеров с разными датами — 2 из 8 нужных; ")
    assert ("замеров за последние 28 сут. — 2 из 8 нужных: обводнённость — 1, хлориды — 1, "
            "эффективность ингибитора — 1") in note
    assert "показателей за последние 7 сут. — 1 из 2 нужных (есть: обводнённость)" in note
    assert "хлориды — последнее значение 29.10.2026, устарело" in note
    assert "нет значений: CO₂, отклонение закачки ингибитора, скорость коррозии, толщина стенки" in note
    shorts = [item["short"] for item in empty["forecast"]["items"]]
    assert "обводнённость, хлориды, эффективность ингибитора" in shorts and "хлориды" in shorts

    # Computed with the median of the math model in place of a missing input: the input is named.
    imputed = [{"name": "CO₂", "unit": "%", "value": 2.3, "imputed": True},
               {"name": "обводнённость", "unit": "%", "value": 90.0, "imputed": False}]
    scored = {gap["key"]: gap for gap in _data_gaps({**values, "model_math": 0.3}, [old, fresh], {}, ready, [], [], [],
                                                     now, imputed)}
    assert scored["forecast"]["note"] == "мат. модель: CO₂ — нет значения, взята медиана 2,3 %"
    assert [item["short"] for item in scored["forecast"]["items"]] == ["CO₂"]
    assert set(empty) == {"well", "forecast", "category", "environment", "sampling", "protection", "run_days", "liquid"}
    assert empty["run_days"]["note"] == "нет: дата спуска оборудования"
    assert empty["well"]["note"] == "нет: месторождение, статус работы (в работе или нет)"


@needs_db
def test_recompute_one_well_and_unknown_number(client):
    well = client.post("/api/v1/monitor/wells/DEMO-001/analyze").json()
    assert well["index"] is not None and well["rank"] == 1
    # A mistyped number is not added to the rating or the list of wells.
    assert client.post("/api/v1/monitor/wells/NO-SUCH-WELL/analyze").status_code == 404
    assert client.get("/api/v1/rating/wells/NO-SUCH-WELL").status_code == 404
    assert "NO-SUCH-WELL" not in {row["well_id"] for row in client.get("/api/v1/ingestion/wells").json()}


@needs_db
def test_card_lists_every_ntc_rule(client):
    well = _well(client, "DEMO-001")
    rules = well["provenance"]["model_ntc"]["rules"]
    assert len(rules) == 17 and {rule["group"] for rule in rules} == {"Среда", "Защита", "Технология", "Коррозия"}
    points = sum(rule["points"] for rule in rules)
    assert f"{points} баллов из 14" in well["provenance"]["model_ntc"]["note"]
    assert any(rule["points"] == 0 and rule["value"] is not None for rule in rules)   # rules that did not fire too
    # The rating list stays light: the rules come with the well card only.
    listed = next(item for item in client.get("/api/v1/rating").json()["wells"] if item["well_id"] == "DEMO-001")
    assert "rules" not in listed["provenance"]["model_ntc"]
    # The AI agent reads only the rules that fired, one line each.
    from app.core.proposal_agent import build_context

    context = build_context("DEMO-001")
    fired = context["rating"]["ntc_rules_fired"]
    assert len(fired) == sum(1 for rule in rules if rule["points"]) > 0
    assert all(line.endswith(f", +{rule['points']}") for line, rule in zip(fired, [r for r in rules if r["points"]]))


def _proposal_context(**rating):
    return {
        "well_id": "DEMO-002", "today": TODAY.isoformat(),
        "rating": {"index": 72, "measure_code": "РИ", "measure": "Решение инженера", "deadline": None,
                   "category": "Среднекоррозионная", "corrosive_environment": True, "protection": None,
                   "field": "Южное", "equipment": "ЭЦН-80", "run_days": 210, "liquid_m3_day": 85.0,
                   "in_operation": True, "demount_act_recent": True, "demount_corrosion": None,
                   "base_act_recent": False, "base_corrosion": None,
                   "data_gaps": ["Демонтаж и разбор: нет: акт разбора на базе после демонтажа 02.09.2026",
                                 "Среда: нет: CO₂"],
                   "notes": {"base_act": "Нет акта"},
                   "ntc_rules_fired": ["Обводнённость (91 %): Критическая обводнённость, +7"], **rating},
        "acts": [{"date": "2026-09-02", "type": "Демонтаж", "corrosion_detected": None}],
        "failures": [], "lab_samples": [], "other_measurements": [], "nkt_registry": None, "documents": [],
        "latest_measurements": {"обводнённость, %": 91, "хлориды, мг/л": 21000},
        "crews": [{"crew_id": "КРС-7", "status": "available", "specialties": ["КРС"]}],
    }


def test_ai_proposal_payload_is_russian():
    import json
    import re

    from app.core import proposal_language

    context = _proposal_context()
    payload = proposal_language.for_model(context, proposal_language.field_gaps(context, 365), [], 365)
    text = json.dumps(payload, ensure_ascii=False)
    # Keys and service values are Russian: Latin is left only in identifiers and formulas.
    latin = set(re.findall(r"[A-Za-z_]{3,}", text)) - {"DEMO", "null", "true", "false"}   # JSON syntax
    assert not latin, latin
    assert payload["бригады"][0]["статус"] == "свободна"
    assert payload["рейтинг"]["код мероприятия"] == "РИ"
    assert payload["рейтинг"]["сработавшие правила НТЦ (значение: причина, баллы из 14)"] == [
        "Обводнённость (91 %): Критическая обводнённость, +7"]
    assert payload["исторические акты демонтажа и разбора (последние за 365 сут.)"]["акт демонтажа"] == "есть"
    assert proposal_language.clean("measure_code РИ, crew available, water cut 91 %, CO₂ 1,9 %") == \
        "код мероприятия РИ, бригада свободна, обводнённость 91 %, CO₂ 1,9 %"


def test_ai_proposal_gaps_name_fields_and_are_not_repeated():
    from app.core import proposal_language

    context = _proposal_context(run_days=None, equipment=None)
    context["rating"]["data_gaps"].append("Наработка: нет: дата спуска оборудования")
    context["crews"] = []
    gaps = proposal_language.field_gaps(context, 365)
    # The rating columns' gaps (fields and dates), then what only the agent checks (the crew schedule).
    assert gaps == ["Демонтаж и разбор: нет: акт разбора на базе после демонтажа 02.09.2026", "Среда: нет: CO₂",
                    "Наработка: нет: дата спуска оборудования", "Нет расписания бригад — бригада не назначена."]
    # The model's rephrasings of known gaps are dropped; a new gap is kept, once.
    merged = proposal_language.merge_gaps(gaps, [
        "Нет акта разбора на базе после демонтажа 02.09.2026.",
        "Нет данных о дате спуска оборудования, наработка не рассчитана",
        "Нет значения pH попутной воды.", "Нет значения pH попутной воды.", ""])
    assert merged == [*gaps, "Нет значения pH попутной воды."]


@needs_db
def test_card_and_export_show_both_models(client):
    from io import BytesIO

    from openpyxl import load_workbook

    well = _well(client, "DEMO-001")
    inputs = well["provenance"]["model_math"]["inputs"]
    assert well["model_math"] is not None and len(inputs) == 5
    assert "Модель Кокса" in well["provenance"]["model_math"]["note"]
    assert well["index"] == int(max(well["model_ntc"], well["model_math"]) * 100 + 0.5 + 1e-9)
    listed = next(item for item in client.get("/api/v1/rating").json()["wells"] if item["well_id"] == "DEMO-001")
    assert listed["model_math"] == well["model_math"] and "inputs" not in listed["provenance"]["model_math"]
    book = load_workbook(BytesIO(client.get("/api/v1/rating/export.xlsx").content))
    headers = [cell.value for cell in book["Рейтинг"][4]]
    assert "Мат. модель (0–1)" in headers
    row = next(row for row in book["Рейтинг"].iter_rows(min_row=5, values_only=True) if row[1] == "DEMO-001")
    assert row[headers.index("Мат. модель (0–1)")] == well["model_math"]
