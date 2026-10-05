from __future__ import annotations

from collections import defaultdict

from app.schemas import LandmarkValidationRequest, LandmarkValidationResult, ValidationModelMetrics


def _auc(labels: list[int], scores: list[float]) -> float | None:
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return None
    ordered = sorted(zip(scores, labels), key=lambda pair: pair[0])
    rank_sum = 0.0
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        average_rank = (index + 1 + end) / 2.0
        rank_sum += average_rank * sum(label for _, label in ordered[index:end])
        index = end
    return (rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def _pr_auc(labels: list[int], scores: list[float]) -> float | None:
    positives = sum(labels)
    if positives == 0:
        return None
    ordered = sorted(zip(scores, labels), key=lambda pair: pair[0], reverse=True)
    true_positive = false_positive = 0
    previous_recall = 0.0
    area = 0.0
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        group_positive = sum(label for _, label in ordered[index:end])
        group_size = end - index
        true_positive += group_positive
        false_positive += group_size - group_positive
        recall = true_positive / positives
        precision = true_positive / (true_positive + false_positive)
        area += (recall - previous_recall) * precision
        previous_recall = recall
        index = end
    return area


def validate_landmark(request: LandmarkValidationRequest) -> LandmarkValidationResult:
    known_rows = []
    excluded = 0
    horizon = request.horizon_days
    for observation in request.observations:
        positive = observation.event_observed and observation.duration_remaining_days <= horizon
        known = positive or observation.duration_remaining_days >= horizon
        if not known:
            excluded += 1
            continue
        known_rows.append((int(positive), observation))

    score_names = sorted({name for _, row in known_rows for name in row.risk_scores})
    probability_names = sorted({name for _, row in known_rows for name in row.probabilities})
    result: dict[str, ValidationModelMetrics] = {}
    for name in sorted(set(score_names) | set(probability_names)):
        scored = [(label, row.risk_scores[name]) for label, row in known_rows if name in row.risk_scores]
        labels = [label for label, _ in scored]
        scores = [score for _, score in scored]
        prob_rows = [(label, row.probabilities[name]) for label, row in known_rows if name in row.probabilities]
        probabilities = [prob for _, prob in prob_rows]
        prob_labels = [label for label, _ in prob_rows]
        mean_probability = sum(probabilities) / len(probabilities) if probabilities else None
        observed_rate = sum(prob_labels) / len(prob_labels) if prob_labels else None
        brier = (sum((p - y) ** 2 for p, y in zip(probabilities, prob_labels)) / len(probabilities)
                 if probabilities else None)
        result[name] = ValidationModelMetrics(
            known_observations=len(scored) if scored else len(prob_rows),
            events_within_horizon=sum(labels if labels else prob_labels),
            roc_auc=_auc(labels, scores) if scores else None,
            pr_auc=_pr_auc(labels, scores) if scores else None,
            brier_score=brier,
            mean_predicted_probability=mean_probability,
            observed_event_rate=observed_rate,
            calibration_status="available" if probabilities else "not_a_probability",
        )
    return LandmarkValidationResult(
        status="ok" if known_rows else "insufficient_followup",
        landmark_day=request.landmark_day, horizon_days=horizon,
        records_total=len(request.observations), records_excluded_early_censoring=excluded,
        models=result,
        note=("Положительный исход — отказ до горизонта; отрицательный — наблюдение без отказа до горизонта. "
              "Цензурирование до горизонта исключено. ROC/PR оценивают ранжирование; Brier/calibration считаются "
              "только для явно переданных вероятностей. Для production нужна временная внешняя валидация."),
    )
