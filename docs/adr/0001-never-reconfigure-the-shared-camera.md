# 0001 — Never reconfigure the shared camera for our own session

**Status:** Accepted (2026-08-29)

## Context

The monitor is not our device. In a normal household it serves, at the same time:

- the parent unit or the vendor app, which is what someone actually relies on to hear the baby,
- the vendor app on a second phone,
- and, last in line, this integration.

Tuya's IPC signalling has two channels, and the difference between them is easy to miss. Frames on
`PROTOCOL_SESSION` (offer, answer, candidate, disconnect) are scoped to the session that sends
them. Frames on `PROTOCOL_CONTROL` are **device-wide mode changes** — they reconfigure the camera
itself, for everyone attached to it.

The built-in backend sent one of those: `resolution` (`cmdValue: 0`, HD), 1.5 s after every
answered handshake, in the hope of getting the 1080p main stream instead of the 720p sub-stream.
It read as a harmless per-session preference. It was not.

Every time the integration started, the camera reconfigured its encoder and the household's parent
unit lost its audio and had to be started again by hand. The cost landed on the one consumer that
matters most, at the exact moment our convenience feature ran.

## Decision

**The integration never sends a device-wide control command as a side effect of its own session
lifecycle.** Session-scoped frames only: offer, answer, candidate, disconnect.

`Session.send_resolution` stays in `signaling.py` as a deliberate lever — something a future
feature could expose as an explicit user action ("request HD") — but nothing calls it
automatically, and nothing may call it from a connect path.

The rule generalises past this one frame: stream-type selection, image settings, and any future
DP write that changes camera-wide state must be user-initiated, never a reflex of connecting.
When such a feature is built, it belongs behind an entity or service the owner operates knowingly,
not inside a handshake.

## Consequences

- Other consumers keep their audio and video when Home Assistant starts, reloads, or reconnects.
- We accept whatever stream mode the camera is currently in. If resolution ever drops to 720p, we
  gained nothing by shouting anyway (see Evidence) — and the fix is a user-facing control, not a
  connect-time command.
- One less moving part in the handshake, and one less way for a restart to disturb the household.
- Anyone re-adding an automatic control frame will find this ADR before they find the bug.

## Evidence

- **Field, 2026-08-29:** the owner reported that starting the integration interrupted the baby
  monitor's audio, requiring a manual restart of the monitor. The only device-wide frame we sent
  on that path was `resolution`, 1.5 s after the answer.
- **The command never demonstrably worked.** `ROADMAP.md`'s "720p question" records months of the
  camera serving the 1280×720 sub-stream while this exact command asked for HD on every session.
  The 1920×1080 stream observed from 2026-08-11 onward appeared after a camera power cycle, not
  after a command — it survives restarts of the integration that no longer send one.
- **Channel semantics:** `signaling.py` publishes `resolution` on `PROTOCOL_CONTROL`, alongside
  device-level commands, whereas offer/answer/candidate/disconnect ride `PROTOCOL_SESSION`. The
  disconnect frame is explicitly session-scoped by design (it names our own session id and cannot
  express someone else's); `resolution` carries no session scoping at all.

## Related

- `custom_components/philips_avent/stream_server.py` — the removed `RESOLUTION_DELAY` timer, with
  the reason at the call site.
- `docs/superpowers/specs/2026-08-09-containerless-camera-design.md` — the session pool, the other
  half of "this camera is shared": slots are finite and abandoned ones are reclaimed slowly.
- [ADR 0002](0002-rebase-timestamps-for-live-view.md) — the other 2026-08-29 decision, and an
  example of fixing our own side of a problem instead of asking the camera to change.
