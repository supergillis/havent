"""Tests for the SDP rewriting between a WebRTC consumer and the camera.

The fixtures are real captures from an SCD953 (identifiers scrubbed): the offer
go2rtc sends as a `webrtc:ws://` source, and the answer the camera returns.
"""
from pathlib import Path

import pytest
from sdp import SdpError, rewrite_answer, rewrite_offer

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
    def test_audio_first_no_backchannel_no_extmap(self):
        """The offer the camera actually gets: audio before video (a
        video-first offer makes it answer with an empty video section), the
        backchannel section dropped, every `a=extmap` stripped (long offers
        get truncated), recvonly throughout, CRLF line endings."""
        # Guard the fixture: this is the shape the rewriting exists for.
        assert kinds(GO2RTC_OFFER) == ["video", "audio", "audio"]
        assert "a=extmap" in GO2RTC_OFFER

        out = rewrite_offer(GO2RTC_OFFER)
        assert kinds(out) == ["audio", "video"]
        assert "a=group:BUNDLE 1 0" in out  # only the forwarded mids, reordered
        assert "a=extmap" not in out
        assert out.count("a=recvonly") == 2
        assert "a=sendrecv" not in out
        assert out.endswith("\r\n")
        assert "\n" not in out.replace("\r\n", "")

    def test_talkback_asks_for_two_way_audio(self):
        out = rewrite_offer(GO2RTC_OFFER, talkback=True)
        audio, video = out.split("m=video")[0], "m=video" + out.split("m=video")[1]
        assert "a=sendrecv" in audio
        assert "a=recvonly" in video


class TestRewriteAnswer:
    def test_restores_the_consumer_order(self):
        # Guard the fixture: this is the shape the rewriting has to undo.
        assert kinds(CAMERA_ANSWER) == ["audio", "video"]

        out = rewrite_answer(CAMERA_ANSWER, GO2RTC_OFFER)
        assert kinds(out) == ["video", "audio", "audio"]
        assert mids(out) == mids(GO2RTC_OFFER)
        # Answered sections are passed through untouched.
        assert "a=rtpmap:96 H264/90000" in out
        assert "a=rtpmap:0 PCMU/8000" in out
        assert "a=ice-pwd:PASSWORD" in out

    def test_unanswered_section_is_rejected_but_keeps_its_codec_lines(self):
        # Port 0 rejects the backchannel, but the rtpmap/fmtp lines must
        # stay: without them go2rtc fails with "payload type not found".
        out = rewrite_answer(CAMERA_ANSWER, GO2RTC_OFFER)
        backchannel = sections(out)[2]
        assert backchannel.split()[1] == "0"
        rejected = out.split("m=audio 0 ")[1]
        assert "a=rtpmap:" in rejected
        assert "a=group:BUNDLE 0 1" in out  # the rejected mid is not bundled

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


class TestDirectionClamping:
    def test_the_answer_direction_is_clamped_to_the_offer(self):
        # The camera always answers its audio sendrecv. pion shrugs at that,
        # browsers do not: against a recvonly offer it becomes sendonly...
        assert "a=sendrecv" in CAMERA_ANSWER
        assert "a=sendrecv" not in rewrite_answer(CAMERA_ANSWER, GO2RTC_OFFER)
        # ...but survives a talkback offer, which really asked for two-way audio.
        talkback_offer = rewrite_offer(GO2RTC_OFFER, talkback=True)
        assert "a=sendrecv" in rewrite_answer(CAMERA_ANSWER, talkback_offer)
