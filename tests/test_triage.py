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


def test_defaults_to_low_severity_and_general_runbook() -> None:
    assert predict_severity("Typo on page", "Minor copy fix") == "low"
    assert suggest_runbook("Typo on page", "Minor copy fix", None).startswith("RB-000")


def test_duplicate_detection_ignores_empty_and_dissimilar_incidents() -> None:
    incidents = [
        {"incident_id": "INC-1", "title": "", "description": ""},
        {"incident_id": "INC-2", "title": "Disk full", "description": "logs volume"},
    ]
    assert find_duplicate("Payment API timeout", "Checkout fails", incidents) is None
    assert find_duplicate("", "", incidents) is None
