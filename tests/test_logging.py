import json
import logging

from app.logging_config import JsonFormatter, request_id_context


def test_json_logs_include_event_and_request_context() -> None:
    token = request_id_context.set("request-123")
    try:
        record = logging.LogRecord(
            "test",
            logging.WARNING,
            __file__,
            1,
            "retrying",
            (),
            None,
        )
        record.event = "database.retry"
        record.attempt = 2

        entry = json.loads(JsonFormatter().format(record))
    finally:
        request_id_context.reset(token)

    assert entry["event"] == "database.retry"
    assert entry["request_id"] == "request-123"
    assert entry["attempt"] == 2
