from __future__ import annotations

from _lib.extrapolation_checker import check_extrapolations, infer_asserted_from_text


def test_species_extrapolation_triggers():
    findings = check_extrapolations(
        claim_text="Drug X improves survival in patients.",
        asserted={"species": "human", "endpoint": "hard-clinical"},
        boundary={"species": ["animal"], "endpoint": "surrogate"},
    )
    ids = {f["rule_id"] for f in findings}
    assert "EX-01" in ids
    assert "EX-05" in ids


def test_no_extrapolation_for_human_boundary():
    findings = check_extrapolations(
        claim_text="Drug X improves response rate in patients with metastatic disease.",
        asserted={"species": "human", "endpoint": "surrogate", "disease_stage": "specific"},
        boundary={"species": ["human"], "endpoint": "surrogate", "disease_stage": ["advanced/late"]},
    )
    assert findings == []


def test_infer_asserted_from_text():
    asserted = infer_asserted_from_text("The therapy provides long-term survival benefit in patients.")
    assert asserted["species"] == "human"
    assert asserted["endpoint"] == "hard-clinical"
    assert asserted["timeframe"] == "long-term"

