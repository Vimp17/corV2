from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.response_rules import RuleBook
from app.core.response_policy import evaluate

NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def register():
    return RuleBook.model_validate({"schema_version": 2, "revision": 1, "rules": [{
        "id": "sample", "revision": 1, "status": "test", "action": "Увеличить частоту отбора проб.",
        "explanation": "Превышены тестовые пороги.", "conditions": {"all": [
            {"field": "measurements.co2_pct", "op": "gt", "value": 1.5, "max_age_days": 30},
            {"field": "measurements.water_cut_pct", "op": "gte", "value": 80, "max_age_days": 30}]}}]})


def point(**kwargs):
    return SimpleNamespace(timestamp=NOW, source_id="KHIMIK", co2_pct=2., water_cut_pct=80., **kwargs)


def test_and_boundaries_and_demo_rules_cannot_run_in_approved_mode():
    assert len(evaluate(register(), [point()], now=NOW, mode="demo")["matched"]) == 1
    assert evaluate(register(), [point()], now=NOW, mode="approved")["matched"] == []
    p = point(); p.co2_pct = 1.5
    assert evaluate(register(), [p], now=NOW, mode="demo")["matched"] == []


@pytest.mark.parametrize("change", ["old", "future", "missing", "invalid", "snapshot", "fill", "conflict"])
def test_unreliable_data_never_confirms_thresholds(change):
    p = point(); points = [p]
    if change == "old": p.timestamp -= timedelta(days=31)
    elif change == "future": p.timestamp += timedelta(seconds=1)
    elif change == "missing": p.co2_pct = None
    elif change == "invalid": p.co2_pct = 100
    elif change == "snapshot": p.field_sources = {"co2_pct": "SNAPSHOT"}
    elif change == "fill": p.imputed_fields = ["co2_pct"]
    else:
        q = deepcopy(p); q.co2_pct = 1.; points.append(q)
    result = evaluate(register(), points, now=NOW, mode="demo", snapshot_ids={"SNAPSHOT"})
    assert result["matched"] == []
    assert result["missing_data"]


def test_or_missing_and_freshness_boundaries():
    data = register().model_dump()
    rule = data["rules"][0]
    rule["conditions"]["any"] = rule["conditions"].pop("all")
    p = point(); p.co2_pct = None
    assert evaluate(RuleBook.model_validate(data), [p], now=NOW, mode="demo")["matched"]
    data["rules"][0]["conditions"] = {"all": [{"field": "measurements.co2_pct", "op": "is_missing", "max_age_days": 30}]}
    assert evaluate(RuleBook.model_validate(data), [p], now=NOW, mode="demo")["matched"]
    p = point(); p.timestamp -= timedelta(days=30)
    assert evaluate(register(), [p], now=NOW, mode="demo")["matched"]


def test_server_discards_invented_actions_summary_and_explanations():
    from app.core.proposal_agent import _check

    policy = evaluate(register(), [point()], now=NOW, mode="demo")
    context = {"today": NOW.date().isoformat(), "rating": {"measure_code": "РИ", "index": 70},
               "crews": [], "acts": [], "documents": [], "response_policy": policy}
    answer = {"measure": "ППР", "actions": ["Выдуманная операция"], "summary": "Выдуманная операция",
              "reasoning": "Выдуманная операция", "based_on": ["Выдуманная операция"]}
    checked = _check(answer, context)
    assert checked["actions"] == [policy["matched"][0]["action"]]
    assert checked["measure"] == "РИ"
    assert "Выдуманная" not in str(checked)
    assert checked["rule_evaluation"]["revision"] == 1


def test_approved_mode_requires_engineering_rules_and_rejects_synthetic_measurements():
    data = register().model_dump()
    data["rules"][0].update(status="approved", reviewer="Тест инженера", reviewed_on="2026-11-16", basis="Тест")
    book = RuleBook.model_validate(data)
    assert evaluate(book, [point()], now=NOW, mode="approved")["matched"]
    assert not evaluate(book, [point(synthetic=True)], now=NOW, mode="approved")["matched"]


@pytest.mark.parametrize("failure", ["not_configured", "unavailable", "invalid_json"])
def test_rule_proposal_works_without_llm_and_is_saved(monkeypatch, failure):
    from dataclasses import replace
    from app.core import proposal_agent as agent

    policy = evaluate(register(), [point()], now=NOW, mode="demo")
    context = {"today": NOW.date().isoformat(), "rating": {"measure_code": "РИ", "index": 70},
               "crews": [], "acts": [], "documents": [], "response_policy": policy}
    monkeypatch.setattr(agent, "build_context", lambda well: context)
    monkeypatch.setattr(agent.proposal_language, "for_model", lambda *args: {})
    monkeypatch.setattr(agent, "settings", replace(agent.settings, llm_api_url="" if failure == "not_configured" else "http://fake",
                                                 llm_model="fake"))
    def chat(*args, **kwargs):
        if failure == "unavailable": raise agent.llm.LLMError("unavailable")
        if failure == "not_configured": raise AssertionError("LLM must not be called")
        return "invalid JSON"
    monkeypatch.setattr(agent.llm, "chat", chat)
    saved = []
    monkeypatch.setattr(agent.db, "save_ai_proposal", lambda well, value: saved.append((well, value)))
    result = agent.generate("TEST-1")
    assert result["actions"] == [policy["matched"][0]["action"]]
    assert saved == [("TEST-1", result)]
    assert result["crew"] is None
