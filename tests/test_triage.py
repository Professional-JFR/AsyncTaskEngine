from app.triage import find_duplicate, predict_severity, suggest_runbook


def test_predicts_severity_from_highest_priority_matching_rule() -> None:
    assert predict_severity("API outage", "Warning: slow responses") == "critical"


def test_finds_duplicate_by_token_similarity() -> None:
    incidents = [
        {
            "incident_id": "INC-12345678",
            "title": "Payment API timeout",
            "description": "Checkout requests are timing out",
        }
    ]

    assert (
        find_duplicate(
            "Payment API timeout",
            "Checkout requests time out",
            incidents,
        )
        == "INC-12345678"
    )


def test_suggests_default_runbook_when_no_service_rule_matches() -> None:
    assert suggest_runbook("Job delayed", "No matching service", None).startswith("RB-000")
