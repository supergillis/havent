# Architecture decision records

Specs in `docs/superpowers/specs/` say how a thing works. These say why it was chosen and what it
costs, so nobody re-litigates a decision or reverts it by accident.

Format: [Michael Nygard's template](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
— Title, Status, Context, Decision, Consequences. Keep them short. Put numbers and dates in the
context, not recollections. Number files in order, never renumber, and supersede rather than edit
a decision away.

Write one when the decision was learned from the field, trades one real good against another, or
has an obvious-looking alternative that is wrong.

| ADR | Title | Status |
|----:|-------|--------|
| [0001](0001-never-reconfigure-the-shared-camera.md) | Never reconfigure the shared camera for our own session | Accepted |
| [0002](0002-rebase-timestamps-for-live-view.md) | Rebase timestamps in a dedicated live-view stream | Accepted |
| [0003](0003-one-tuya-session-per-camera.md) | One Tuya session per camera | Accepted |
| [0004](0004-native-webrtc-camera.md) | Native WebRTC camera, not Home Assistant's go2rtc provider | Accepted |
| [0005](0005-check-before-put.md) | Check before PUT when registering go2rtc streams | Accepted |
