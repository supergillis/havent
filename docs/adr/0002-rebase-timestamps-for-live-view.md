# 0002 — Rebase timestamps in a dedicated live-view stream

## Status

Accepted 2026-08-29.

## Context

The camera never advances its video clock. Every video packet on `_src` reports pts `0.000000`,
while audio on the same stream advances normally (0.02, 0.04, 0.06 …) and a control camera through
go2rtc advances too. The camera is the fault, permanently.

A player needs an advancing clock to schedule frames. Browsers therefore rendered video erratically
while audio stayed smooth — the reported "sound is smooth, video is very flaky".

Recordings and HLS escaped this because Home Assistant's stream component stamps arrival times
itself. Live view never enters that code: media goes camera → go2rtc → browser. Nothing we own
touches those packets, so the clock cannot be repaired in Python. The original Go bridge rebased
timestamps in its own media path for this same reason.

## Decision

Live view negotiates against a third stream, `philips_avent_<id>_src_live`, whose source is
`ffmpeg:<_src>#video=copy#audio=copy#async`.

`#async` is go2rtc's own flag for `-use_wallclock_as_timestamps 1 -async 1`. Both tracks are
copied — PCMU plays in browsers, so the hop only rebases the clock and encodes nothing.
`whep_answer` falls back to `_src` if the new stream cannot answer, which is the behaviour that
existed before this change.

## Consequences

Live view is smooth. Measured after deployment: 80 advancing timestamps in 80 packets, video and
audio intact.

Cost: one more ffmpeg process while someone watches, a slower start, and a fraction of a second of
latency. Timestamps are now arrival times, so network jitter becomes timestamp jitter; over
loopback that is small.

Session count does not change — `_src_live` consumes `_src` like `_src_aac` does.

Three derived streams now exist per camera. Registration, preload sweeps, and the name asserts in
`const.py` must cover all of them.

If go2rtc gains a native Tuya producer, rebasing belongs there and this stream goes away.
