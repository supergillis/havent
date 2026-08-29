# Architecture decision records

One file per decision that was expensive to learn and would be expensive to undo. An ADR is not a
design document — `docs/superpowers/specs/` holds those, and they describe how a thing works. An
ADR records **why a choice was made, what it costs, and what evidence forced it**, so the next
person (or the next model) does not re-litigate it from scratch or quietly revert it.

Write one when a decision meets any of these:

- it was learned from the field rather than from documentation,
- it trades one real good against another (latency vs smoothness, quality vs politeness),
- the obvious-looking alternative is wrong for a non-obvious reason.

Format: Status, Context, Decision, Consequences, Evidence. Keep the evidence section concrete —
measurements with numbers and dates, not recollections. Number files sequentially and never
renumber; supersede instead, linking both ways.

| ADR | Title | Status |
|----:|-------|--------|
| [0001](0001-never-reconfigure-the-shared-camera.md) | Never reconfigure the shared camera for our own session | Accepted |
| [0002](0002-rebase-timestamps-for-live-view.md) | Rebase timestamps in a dedicated live-view stream | Accepted |
