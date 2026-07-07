from __future__ import annotations

from _lib.methodology_checker import evaluate_methodology


def test_underpowered_auto_detection():
    report = evaluate_methodology(judgments=[], metadata={"sample_size": 24})
    assert any(x["check_id"] == "METH-03" for x in report["auto_detected"])
    assert report["quality_score"] < 100


def test_critical_without_reason_warns():
    report = evaluate_methodology(judgments=[{"check_id": "METH-10", "finding": "critical"}])
    assert any("METH-10" in w and "requires a reason" in w for w in report["warnings"])


def test_observational_without_adjustment_maps_to_grade():
    report = evaluate_methodology(judgments=[], metadata={"design": "retrospective cohort"})
    assert "risk_of_bias" in report["comparison_to_grade"]
    assert "METH-06" in report["comparison_to_grade"]["risk_of_bias"]

