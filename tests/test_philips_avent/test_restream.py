"""Tests for the go2rtc restream glue (restream.py).

The rules being guarded (the why is in restream.py's module docstring):
stream_source()'s result is frozen into HA's cached Stream, so only a
permanent, config-shaped verdict may pick the webrtc: fallback — transient
trouble must still yield the stable RTSP URL. And `PUT /api/streams`
silently replaces a live stream, so registration checks before it PUTs.
"""
from restream import parse_rtsp_endpoint


def test_managed_instance_loopback_rtsp():
    assert parse_rtsp_endpoint("127.0.0.1:18554", "localhost") == ("127.0.0.1", 18554)


def test_all_interfaces_on_local_api_host():
    assert parse_rtsp_endpoint(":8554", "127.0.0.1") == ("127.0.0.1", 8554)


def test_all_interfaces_on_remote_api_host():
    assert parse_rtsp_endpoint(":8554", "192.168.1.2") == ("192.168.1.2", 8554)


def test_remote_host_with_loopback_rtsp_is_unusable():
    # go2rtc could dial itself, but HA's PyAV could not reach it — and
    # stream_source() feeds both, so this must degrade to the ws URL.
    assert parse_rtsp_endpoint("127.0.0.1:8554", "192.168.1.2") is None


def test_bracketed_ipv6_loopback_is_loopback():
    # rpartition(":") leaves the host as "[::1]", which must not fall
    # through to the treat-as-remote branch.
    assert parse_rtsp_endpoint("[::1]:8554", "localhost") == ("127.0.0.1", 8554)
    assert parse_rtsp_endpoint("[::1]:8554", "192.168.1.2") is None


def test_absent_empty_or_garbage_listen():
    for listen in (None, "", ":", "nonsense", ":notaport"):
        assert parse_rtsp_endpoint(listen, "localhost") is None
