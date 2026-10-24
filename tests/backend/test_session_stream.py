import json

import pytest

from backend.core.session_stream import cursor_value, encode_event


@pytest.mark.parametrize(("after", "last", "expected"), [
    (None, None, None), ("2", "5", 5), ("8", "5", 8), ("0", None, 0),
    ("bad", "3", None), ("-1", None, None), (None, "4", 4),
])
def test_cursor_protocol_uses_newest_valid_cursor_or_requests_snapshot(after, last, expected):
    assert cursor_value(after, last) == expected


def test_event_framing_encodes_text_as_json_and_uses_durable_sequence():
    encoded = encode_event("session", {"event_id": "event", "message_id": "message"}, 42)
    assert encoded.startswith("id: 42\nevent: session\ndata: ")
    assert json.loads(encoded.split("data: ", 1)[1]) == {"event_id": "event", "message_id": "message"}
