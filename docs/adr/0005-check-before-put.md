# 0005 — Check before PUT when registering go2rtc streams

## Status

Accepted 2026-08-10.

## Context

`PUT /api/streams` replaces a stream's entry without stopping the producers already running under
it. Those orphans keep retrying, and they fight the new producer over the signalling endpoint,
which serves one consumer. A blind PUT on every stream open churned 45 Tuya sessions in 7 minutes.

Comparing sources to decide whether to PUT is harder than it looks. go2rtc reports an **idle**
producer as the source string we configured, and an **active** one in resolved form — an ffmpeg
source comes back as the expanded `exec:ffmpeg …` command line. A plain string comparison
therefore reports a mismatch exactly while the stream is running, which is the worst moment to
replace it.

## Decision

Read `GET /api/streams` first, then PUT only in two cases: the stream is missing, or every
reported producer is in configured form and ours is not among them. Any producer in resolved form
means the stream is running, so skip — even when the source genuinely changed. All of it under a
lock, so concurrent opens register once.

The same "never disturb a running producer" rule governs preload, with one exception: an **idle**
producer whose preload is listed must be re-armed, because go2rtc dials a preload once and a
failed dial leaves an entry that never retries.

## Consequences

Registration is safe to call from every path — live view, frame grabs, setup — and it repairs
itself after a go2rtc restart.

A stale source is corrected late: it waits until the stream is idle or the entry reloads. That is
the intended trade. A wrong skip costs one stale source; a wrong PUT costs the household its
monitor.
