# 0004 — Native WebRTC camera, not Home Assistant's go2rtc provider

## Status

Accepted 2026-08-10.

## Context

Home Assistant can attach a go2rtc "provider" to a camera and handle WebRTC for you. The provider
consumes the URL `stream_source()` returns. Ours carries AAC audio for the recorder, so every
viewer would get PCMU transcoded to AAC and then to opus — two conversions the camera's own codecs
do not need.

Home Assistant decides native versus provider at the **class** level: if a camera class overrides
`async_handle_async_webrtc_offer`, that class never gets a provider, and its `stream_source()` is
used only by the recorder.

## Decision

The built-in camera answers WebRTC itself. It sends the browser's offer to go2rtc over WHEP and
returns the answer. Stills come from go2rtc's frame handler through a TTL cache.

This lives in its own class, `AventBuiltinCamera`. The add-on backend keeps the plain
`AventCamera`, which keeps its provider, because the override would strip the provider from every
camera sharing the class.

## Consequences

Live view is one hop with the camera's own codecs and no transcode per viewer. Frigate and Nest do
the same thing, so the contract is well travelled: a no-op candidate handler, `WebRTCError` on
failure, no close override.

The dashboard card advertises WebRTC only, so it has no HLS fallback — a failed offer shows an
error card. HLS still serves recordings, casting, and API consumers. We own the error mapping and
the stills path.

Anyone moving the offer handler into a shared base class silently disables the add-on backend's
live view.
