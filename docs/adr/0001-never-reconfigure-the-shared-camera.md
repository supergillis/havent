# 0001 — Never reconfigure the shared camera for our own session

## Status

Accepted 2026-08-29.

## Context

The monitor serves the parent unit, the vendor app, and this integration at the same time. It is
not our device.

Tuya's signalling has two channels. Frames on `PROTOCOL_SESSION` — offer, answer, candidate,
disconnect — belong to the session that sends them. Frames on `PROTOCOL_CONTROL` change the camera
for everyone.

We sent one of those: `resolution` (HD), 1.5 seconds after every answered handshake, to ask for the
1080p stream. The camera reconfigured its encoder each time and the household's parent unit lost
its audio, which then had to be started by hand.

The command also never worked. The camera served 720p for months while we asked for HD on every
session. Today's 1080p arrived after a power cycle, and it survives restarts that send no command.

## Decision

The integration sends session-scoped frames only. No device-wide control command runs as a side
effect of connecting.

`Session.send_resolution` stays in the code as a lever a future feature can expose as an explicit
user action. Nothing calls it automatically.

The rule covers everything of that shape: stream-type selection, image settings, and any DP write
that changes camera-wide state must be triggered by the owner, not by a handshake.

## Consequences

Other consumers keep their audio and video when Home Assistant starts or reconnects.

We take whatever stream mode the camera is in. If resolution drops to 720p, the answer is a control
the owner operates, not a command at connect time. Watch the frame size after the next restart to
confirm the command was never what produced 1080p.
