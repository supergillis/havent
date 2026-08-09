"""Registering the builtin backend's producer stream in go2rtc.

The camera's `stream_source()` used to hand out the signaling `webrtc:ws`
URL directly; HA's stream component (HLS, `camera.record`, casting) cannot
open that scheme, so those features were lost. Instead we register the
producer as go2rtc stream `philips_avent_<id>_src` — sources: the ws URL
plus an `ffmpeg:#audio=aac` transcode, because HA's stream component drops
any audio that is not AAC/MP3 and the camera sends PCMU — and hand out
go2rtc's RTSP URL for it. Everything fans out from that one stream, so one
Tuya session serves live view, HLS and frame grabs together.

Where go2rtc's RTSP listens must be probed, not assumed: HA's managed
instance pins it to 127.0.0.1:18554 in its config template, user-run ones
default to :8554 on any host. The probe is `GET /api` (bare `/api` is on
the managed instance's allow_paths whitelist) and every request rides
`hass.data["go2rtc"]`'s own session — the managed API is a unix socket
with per-boot local_auth, unreachable any other way.

The one rule everything here serves: never break live view to gain HLS.
HA's `Camera.async_create_stream` calls `stream_source()` at most once per
entity lifetime and freezes the result into its cached Stream. So only a
permanent, config-shaped verdict (no go2rtc, RTSP disabled, remote
go2rtc with loopback RTSP) may return the ws fallback; transient trouble
returns the stable RTSP URL anyway — the stream worker retries its
source, and registration catches up via the provider's frame grabs, the
WHEP override and the setup pass. `PUT /api/streams` silently replaces a
live stream without stopping its producers, so registration is
check-then-PUT under a lock, exactly like preload.py's arming.
"""
from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
_ALL_INTERFACES = ("", "0.0.0.0", "::", "[::]")

#: Where HA's config template pins the managed instance's RTSP listener.
#: Used only as the PROVISIONAL endpoint when the /api probe fails
#: transiently on a loopback API host — never cached, re-probed next call,
#: version-coupled to HA's template and the log says so. The point is that
#: stream_source()'s result is frozen into HA's cached Stream, so a
#: transient failure must still yield the stable RTSP URL (see spec).
MANAGED_RTSP = ("127.0.0.1", 18554)


def parse_rtsp_endpoint(listen: str | None, api_host: str) -> tuple[str, int] | None:
    """(host, port) HA's stream worker can dial, or None to fall back."""
    if not listen:
        return None
    host, _, port_text = listen.rpartition(":")
    try:
        port = int(port_text)
    except ValueError:
        return None
    api_local = api_host in _LOOPBACK_HOSTS
    if host in _ALL_INTERFACES:
        return ("127.0.0.1", port) if api_local else (api_host, port)
    if host in _LOOPBACK_HOSTS:
        return ("127.0.0.1", port) if api_local else None
    return (host, port)
