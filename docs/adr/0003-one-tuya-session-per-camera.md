# 0003 — One Tuya session per camera

## Status

Accepted. Decided 2026-05, hardened through 2026-08. Written up 2026-08-29.

## Context

The camera serves 3–5 WebRTC sessions at once. When the pool is full it closes the newest arrival,
so a full pool locks out the vendor app and the parent unit. A session abandoned mid-handshake is
not freed at once; the firmware reclaims it after about 20 minutes.

Early builds opened a session per consumer. Home Assistant polls a camera thumbnail every 10
seconds, which meant roughly 360 sessions an hour, and the pool emptied. Later, a destructive
re-registration produced 45 sessions in 7 minutes and locked the household out of its own monitor.

## Decision

Exactly one session per camera. Everything else fans out inside go2rtc from that one producer:
live view, HLS, recordings, casting, and frame grabs all consume the running stream instead of
dialling the camera.

Supporting rules: stills come from a TTL cache, not a fresh session per poll. Unanswered dials
back off exponentially and stop at 10 minutes, so a dark camera is probed rarely enough that our
zombies never fill the pool. With `keep_stream_running` the one session is held open, and a
watchdog redials it when it dies.

## Consequences

Home Assistant no longer competes with the vendor app for slots. Every video feature costs the
same single session.

The session is a single point of failure: when it breaks, everything Home Assistant shows breaks
together. After the camera returns from being switched off, live view waits for the next probe —
up to 10 minutes. Cutting that lag by dialling on LAN reappearance is open work.
