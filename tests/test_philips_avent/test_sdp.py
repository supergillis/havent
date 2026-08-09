"""Tests for the SDP rewriting between a WebRTC consumer and the camera.

The fixtures are real captures from an SCD953 (identifiers scrubbed): the offer
go2rtc sends as a `webrtc:ws://` source, and the answer the camera returns.
"""
from pathlib import Path

import pytest
from sdp import SdpError, describe, rewrite_answer, rewrite_offer

FIXTURES = Path(__file__).parent / "fixtures"
GO2RTC_OFFER = (FIXTURES / "go2rtc-offer.sdp").read_text()
CAMERA_ANSWER = (FIXTURES / "camera-answer.sdp").read_text()


def sections(sdp: str) -> list[str]:
    return [line for line in sdp.splitlines() if line.startswith("m=")]


def kinds(sdp: str) -> list[str]:
    return [line.split()[0][2:] for line in sections(sdp)]


def mids(sdp: str) -> list[str]:
    return [line[6:] for line in sdp.splitlines() if line.startswith("a=mid:")]


class TestRewriteOffer:
    def test_go2rtc_offer_is_video_audio_audio(self):
        """Guards the assumption the rewriting exists for."""
        assert kinds(GO2RTC_OFFER) == ["video", "audio", "audio"]

    def test_audio_comes_first(self):
        # A video-first offer makes the camera answer with an empty video section.
        assert kinds(rewrite_offer(GO2RTC_OFFER)) == ["audio", "video"]

    def test_drops_the_backchannel_section(self):
        assert len(sections(rewrite_offer(GO2RTC_OFFER))) == 2

    def test_strips_extmap(self):
        assert "a=extmap" in GO2RTC_OFFER
        assert "a=extmap" not in rewrite_offer(GO2RTC_OFFER)

    def test_both_directions_are_recvonly_by_default(self):
        out = rewrite_offer(GO2RTC_OFFER)
        assert out.count("a=recvonly") == 2
        assert "a=sendrecv" not in out

    def test_talkback_asks_for_two_way_audio(self):
        out = rewrite_offer(GO2RTC_OFFER, talkback=True)
        audio, video = out.split("m=video")[0], "m=video" + out.split("m=video")[1]
        assert "a=sendrecv" in audio
        assert "a=recvonly" in video

    def test_bundle_lists_only_the_forwarded_mids(self):
        out = rewrite_offer(GO2RTC_OFFER)
        assert "a=group:BUNDLE 1 0" in out

    def test_lines_end_with_crlf(self):
        out = rewrite_offer(GO2RTC_OFFER)
        assert out.endswith("\r\n")
        assert "\n" not in out.replace("\r\n", "")

    def test_offer_without_media_is_rejected(self):
        with pytest.raises(SdpError):
            rewrite_offer("v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n")

    def test_audio_only_offer_is_forwarded_as_is(self):
        offer = "v=0\r\ns=-\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\na=mid:0\r\na=recvonly\r\n"
        assert kinds(rewrite_offer(offer)) == ["audio"]


class TestRewriteAnswer:
    def test_camera_answer_is_audio_then_video(self):
        """Guards the fixture: this is the shape the rewriting has to undo."""
        assert kinds(CAMERA_ANSWER) == ["audio", "video"]

    def test_restores_the_consumer_order(self):
        out = rewrite_answer(CAMERA_ANSWER, GO2RTC_OFFER)
        assert kinds(out) == ["video", "audio", "audio"]

    def test_mids_line_up_with_the_offer(self):
        out = rewrite_answer(CAMERA_ANSWER, GO2RTC_OFFER)
        assert mids(out) == mids(GO2RTC_OFFER)

    def test_unanswered_section_is_rejected_with_port_zero(self):
        out = rewrite_answer(CAMERA_ANSWER, GO2RTC_OFFER)
        backchannel = sections(out)[2]
        assert backchannel.split()[1] == "0"

    def test_rejected_section_keeps_its_rtpmap(self):
        # Without this go2rtc fails the stream with "payload type not found".
        out = rewrite_answer(CAMERA_ANSWER, GO2RTC_OFFER)
        rejected = out.split("m=audio 0 ")[1]
        assert "a=rtpmap:" in rejected

    def test_answered_sections_are_passed_through_untouched(self):
        out = rewrite_answer(CAMERA_ANSWER, GO2RTC_OFFER)
        assert "a=rtpmap:96 H264/90000" in out
        assert "a=rtpmap:0 PCMU/8000" in out
        assert "a=ice-pwd:PASSWORD" in out

    def test_bundle_excludes_the_rejected_mid(self):
        out = rewrite_answer(CAMERA_ANSWER, GO2RTC_OFFER)
        assert "a=group:BUNDLE 0 1" in out

    def test_wrong_section_count_is_rejected(self):
        audio_only = CAMERA_ANSWER.split("m=video")[0]
        with pytest.raises(SdpError, match="expected 2"):
            rewrite_answer(audio_only, GO2RTC_OFFER)

    def test_empty_video_section_is_rejected(self):
        """The failure the audio-first ordering exists to avoid, captured verbatim.

        Answering a video-first offer, the camera emitted `m=video ... 96` with
        no mid, ICE or rtpmap, and even dropped the CRLF before the audio
        m-line. Catching it here turns a puzzling downstream failure into one
        clear message.
        """
        mangled = (
            "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=group:BUNDLE 0 1\r\n"
            "m=video 9 UDP/TLS/RTP/SAVPF 96\r\nc=IN IP4 0.0.0.0"
            "m=audio 9 UDP/TLS/RTP/SAVPF 0\r\nc=IN IP4 0.0.0.0\r\na=mid:1\r\n"
            "a=sendrecv\r\na=rtpmap:0 PCMU/8000\r\n"
        )
        with pytest.raises(SdpError, match="expected 2"):
            rewrite_answer(mangled, GO2RTC_OFFER)

    def test_answer_without_mids_is_rejected(self):
        stripped = "\r\n".join(
            line for line in CAMERA_ANSWER.splitlines() if not line.startswith("a=mid:")
        ) + "\r\n"
        with pytest.raises(SdpError, match="no mid"):
            rewrite_answer(stripped, GO2RTC_OFFER)


class TestDirectionClamping:
    def test_sendrecv_answer_to_a_recvonly_offer_becomes_sendonly(self):
        # The camera always answers its audio sendrecv. pion shrugs at that,
        # browsers do not.
        assert "a=sendrecv" in CAMERA_ANSWER
        assert "a=sendrecv" not in rewrite_answer(CAMERA_ANSWER, GO2RTC_OFFER)

    def test_sendrecv_survives_a_talkback_offer(self):
        offer = rewrite_offer(GO2RTC_OFFER, talkback=True)
        # Round-trip through the offer the camera actually saw.
        assert "a=sendrecv" in rewrite_answer(CAMERA_ANSWER, offer)


class TestRoundTrip:
    def test_answer_answers_every_offered_section(self):
        out = rewrite_answer(CAMERA_ANSWER, GO2RTC_OFFER)
        assert len(sections(out)) == len(sections(GO2RTC_OFFER))
        assert kinds(out) == kinds(GO2RTC_OFFER)

    def test_describe_summarizes_both_sides(self):
        assert describe(rewrite_offer(GO2RTC_OFFER)).startswith("audio[mid=1 recvonly")
        assert "video[mid=0" in describe(CAMERA_ANSWER)
