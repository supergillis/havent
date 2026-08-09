"""Tests for SenseIQ sleep-tracking payload decoding (DPS 3 and 4).

The base64 sample is a real DPS 4 value read from a live SCD953 on 2026-08-09:
base64 wrapping the ASCII hex of the JSON, which is the double encoding this
firmware actually uses. The DPS 3 samples are the two live status readings
observed the same day.
"""

import base64
import json

from senseiq import (
    decode_senseiq_payload,
    session_attributes,
    session_duration,
    session_start,
    status_attributes,
    status_code,
)

# Verbatim from a live tuya.m.device.get, DPS 4. Decodes (base64 -> hex ->
# JSON) to {"st":1786297106,"sd":1864,"css":"d","cssd":1562,"ssd":[{"l":302}]}.
LIVE_SESSION_B64HEX = (
    "N2IyMjczNzQyMjNhMzEzNzM4MzYzMjM5MzczMTMwMzYyYzIyNzM2NDIyM2EzMTM4MzYzNDJj"
    "MjI2MzczNzMyMjNhMjI2NDIyMmMyMjYzNzM3MzY0MjIzYTMxMzUzNjMyMmMyMjczNzM2NDIy"
    "M2E1YjdiMjI2YzIyM2EzMzMwMzI3ZDVkN2Q="
)
LIVE_SESSION = {"st": 1786297106, "sd": 1864, "css": "d", "cssd": 1562, "ssd": [{"l": 302}]}

# Live DPS 3 values: plain JSON, unlike DPS 4.
LIVE_STATUS = '{"r":"b","br":29}'


def b64(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


def b64hex(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode().hex().encode()).decode()


class TestDecodeLivePayloads:
    def test_decodes_the_live_session_record(self):
        assert decode_senseiq_payload(LIVE_SESSION_B64HEX) == LIVE_SESSION

    def test_decodes_the_live_status(self):
        assert decode_senseiq_payload(LIVE_STATUS) == {"r": "b", "br": 29}

    def test_session_arithmetic_holds_on_the_live_sample(self):
        # sd == sum of completed segment durations + time in the current state,
        # which held on every live sample and anchors the field interpretation.
        payload = decode_senseiq_payload(LIVE_SESSION_B64HEX)
        segments = sum(next(iter(seg.values())) for seg in payload["ssd"])
        assert segments + payload["cssd"] == payload["sd"]


class TestDecodeShapes:
    def test_dict_passes_through(self):
        assert decode_senseiq_payload(LIVE_SESSION) is LIVE_SESSION

    def test_plain_json(self):
        assert decode_senseiq_payload('{"st": 5}') == {"st": 5}

    def test_single_base64_wrapping(self):
        assert decode_senseiq_payload(b64(LIVE_SESSION)) == LIVE_SESSION

    def test_double_base64_hex_wrapping(self):
        assert decode_senseiq_payload(b64hex(LIVE_SESSION)) == LIVE_SESSION

    def test_none_and_empty(self):
        assert decode_senseiq_payload(None) is None
        assert decode_senseiq_payload("") is None
        assert decode_senseiq_payload("   ") is None

    def test_garbage(self):
        assert decode_senseiq_payload("not base64 at all!") is None
        assert decode_senseiq_payload(base64.b64encode(b"neither hex nor json").decode()) is None
        assert decode_senseiq_payload(12345) is None

    def test_json_that_is_not_an_object(self):
        assert decode_senseiq_payload("[1, 2, 3]") is None
        assert decode_senseiq_payload(b64(LIVE_SESSION).replace("e", "f", 1)) is None


class TestSessionStart:
    def test_live_value(self):
        assert session_start(LIVE_SESSION) == 1786297106

    def test_milliseconds_are_folded_to_seconds(self):
        assert session_start({"st": 1786297106000}) == 1786297106

    def test_missing_or_bad(self):
        assert session_start(None) is None
        assert session_start({}) is None
        assert session_start({"st": "soon"}) is None
        assert session_start({"st": True}) is None
        assert session_start({"st": 0}) is None
        assert session_start({"st": -5}) is None


class TestSessionDuration:
    def test_live_value(self):
        assert session_duration(LIVE_SESSION) == 1864

    def test_zero_is_a_valid_duration(self):
        assert session_duration({"sd": 0}) == 0

    def test_missing_or_bad(self):
        assert session_duration(None) is None
        assert session_duration({}) is None
        assert session_duration({"sd": "long"}) is None
        assert session_duration({"sd": True}) is None
        assert session_duration({"sd": -1}) is None


class TestStatus:
    def test_live_code(self):
        assert status_code(decode_senseiq_payload(LIVE_STATUS)) == "b"

    def test_unknown_codes_pass_through_verbatim(self):
        # The vocabulary is undecoded, so any non-empty string is relayed
        # rather than mapped; interpretation is deliberately not attempted.
        assert status_code({"r": "zz"}) == "zz"

    def test_missing_or_bad_code_reads_unknown(self):
        assert status_code(None) is None
        assert status_code({}) is None
        assert status_code({"r": ""}) is None
        assert status_code({"r": 3}) is None

    def test_attributes_carry_the_rest_verbatim(self):
        assert status_attributes(decode_senseiq_payload(LIVE_STATUS)) == {"br": 29}
        assert status_attributes(None) == {}


class TestSessionAttributes:
    def test_live_sample(self):
        assert session_attributes(LIVE_SESSION) == {
            "current_state": "d",
            "current_state_duration": 1562,
            "state_segments": [{"l": 302}],
        }

    def test_partial_and_missing(self):
        assert session_attributes({"css": "d"}) == {"current_state": "d"}
        assert session_attributes({}) == {}
        assert session_attributes(None) == {}
