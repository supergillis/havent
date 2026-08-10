"""Tests for SenseIQ sleep-tracking payload decoding (DPS 3 and 4).

The base64 sample is a real DPS 4 value read from a live SCD953 on 2026-08-09:
base64 wrapping the ASCII hex of the JSON, which is the double encoding this
firmware actually uses. The DPS 3 samples are the two live status readings
observed the same day.
"""

import base64
import json

from senseiq import (
    SENSING_STATUSES,
    SLEEP_STATES,
    breathing_rate,
    decode_senseiq_payload,
    sensing_status,
    session_attributes,
    session_duration,
    session_start,
    sleep_state,
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

# A later live DPS 4 sample (the recorded fields), several sleep cycles in:
# the segments alternate l and d exactly as sleep cycles do, which is the
# evidence behind reading `l` as light sleep, and the durations again sum to
# `sd` exactly (302 + 2258 + 710 + 286 + 354 + 300 == 4210).
LIVE_CYCLES = {
    "sd": 4210,
    "cssd": 300,
    "ssd": [{"l": 302}, {"d": 2258}, {"l": 710}, {"d": 286}, {"l": 354}],
}


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


class TestSleepState:
    def test_deep_is_confirmed(self):
        # The app showed "deep sleep" at the moment css read 'd' (2026-08-09),
        # which is also the state of the LIVE_SESSION sample.
        assert sleep_state(LIVE_SESSION) == "deep"
        assert sleep_state({"css": "d"}) == "deep"

    def test_light_is_inferred_from_the_cycle_alternation(self):
        assert sleep_state({"css": "l"}) == "light"

    def test_unknown_codes_read_unknown_not_the_raw_letter(self):
        # An unrecognised code must not look like a real state: None, so the
        # ENUM sensor shows unknown instead of leaking a letter as a state.
        assert sleep_state({"css": "a"}) is None
        assert sleep_state({"css": "zz"}) is None
        assert sleep_state({"css": "b"}) is None

    def test_missing_or_bad(self):
        assert sleep_state(None) is None
        assert sleep_state({}) is None
        assert sleep_state({"css": ""}) is None
        assert sleep_state({"css": 3}) is None
        assert sleep_state({"css": None}) is None

    def test_surrounding_whitespace_is_tolerated(self):
        assert sleep_state({"css": " d "}) == "deep"

    def test_every_translated_state_is_an_enum_option(self):
        # The ENUM sensor's options list must cover everything sleep_state can
        # return, or HA raises on a valid state.
        assert sleep_state({"css": "d"}) in SLEEP_STATES
        assert sleep_state({"css": "l"}) in SLEEP_STATES

    def test_live_cycle_sample_alternates_and_sums(self):
        # The evidence for `l` = light: strict l/d alternation plus the same
        # sd == Σssd + cssd arithmetic as every other sample.
        keys = [next(iter(seg)) for seg in LIVE_CYCLES["ssd"]]
        assert keys == ["l", "d", "l", "d", "l"]
        segments = sum(next(iter(seg.values())) for seg in LIVE_CYCLES["ssd"])
        assert segments + LIVE_CYCLES["cssd"] == LIVE_CYCLES["sd"]


class TestBreathingRate:
    def test_live_value(self):
        # Confirmed field: the app's breathing-rate display matched `br` on
        # every comparison (27, 28, 30, 33 observed).
        assert breathing_rate(decode_senseiq_payload(LIVE_STATUS)) == 29

    def test_other_observed_values(self):
        for observed in (27, 28, 30, 33):
            assert breathing_rate({"r": "b", "br": observed}) == observed

    def test_zero_is_relayed_verbatim(self):
        assert breathing_rate({"br": 0}) == 0

    def test_missing_or_bad(self):
        assert breathing_rate(None) is None
        assert breathing_rate({}) is None
        assert breathing_rate({"br": "fast"}) is None
        assert breathing_rate({"br": True}) is None
        assert breathing_rate({"br": -1}) is None


class TestSensingStatus:
    def test_live_code_translates_to_breathing(self):
        # `b` = breathing: documented in the APK strings (moving / breathing /
        # no-signal / out-of-crib / analyzing) and consistent with every live
        # sample — it held "b" for an hour of continuous breathing while the
        # DPS 4 sleep stage cycled underneath.
        assert sensing_status(decode_senseiq_payload(LIVE_STATUS)) == "breathing"
        assert sensing_status({"r": "b"}) == "breathing"

    def test_movement_code_translates_to_movement(self):
        # `m` = movement, pinned by paired observation on the live SCD953
        # (2026-08-10 21:22): the device sent {"r":"m","br":0} for one poll
        # while the vendor app showed "Movement". The label is the app's
        # word, not the APK enumeration's "moving".
        assert sensing_status({"r": "m", "br": 0}) == "movement"
        assert sensing_status({"r": "m"}) == "movement"

    def test_unmapped_codes_read_unknown_not_the_raw_letter(self):
        # `b` and `m` are pinned; the other documented statuses have never
        # been observed here, so their letters are unknown. An unmapped code
        # must be None — the ENUM sensor shows unknown — never the raw
        # letter passing as a state.
        assert sensing_status({"r": "zz"}) is None
        assert sensing_status({"r": "d"}) is None

    def test_missing_or_bad_reads_unknown(self):
        assert sensing_status(None) is None
        assert sensing_status({}) is None
        assert sensing_status({"r": ""}) is None
        assert sensing_status({"r": 3}) is None

    def test_every_translated_status_is_an_enum_option(self):
        # The ENUM sensor's options list must cover everything sensing_status
        # can return, or HA raises on a valid state.
        assert sensing_status({"r": "b"}) in SENSING_STATUSES
        assert sensing_status({"r": "m"}) in SENSING_STATUSES

    def test_raw_code_is_kept_verbatim_for_attributes(self):
        assert status_code(decode_senseiq_payload(LIVE_STATUS)) == "b"
        assert status_code({"r": "zz"}) == "zz"
        assert status_code(None) is None
        assert status_code({}) is None
        assert status_code({"r": ""}) is None
        assert status_code({"r": 3}) is None

    def test_attributes_carry_the_raw_code_and_the_rest(self):
        # The untranslated letter rides along as evidence, so the day an
        # unobserved status shows up it is caught in the attributes even
        # though the state reads unknown.
        assert status_attributes(decode_senseiq_payload(LIVE_STATUS)) == {
            "br": 29,
            "status_code": "b",
        }
        assert status_attributes({"r": "zz", "br": 12}) == {"br": 12, "status_code": "zz"}
        assert status_attributes({"br": 12}) == {"br": 12}
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
