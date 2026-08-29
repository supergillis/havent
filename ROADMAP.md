# Roadmap

Where this project is, and what is worth doing next. Written 2026-08-09, reworked 2026-08-11 after
the go2rtc restream field tests and a survey of how the mature camera integrations (Reolink, ONVIF,
Nest, Ring, Frigate, UniFi Protect) handle the same problems.

The table is ordered by value per unit of effort — low-effort high-gain work first — not by
ambition. Effort: **S** hours, **M** a day or two, **L** a project. Priority: **P1** do next,
**P2** queued, **P3** when the itch strikes. Items marked *(open question)* have a stated way to
settle them; if you cannot say how you would find out, it does not belong on this list.

## Where things stand

- **Streaming without the add-on works.** Tuya signalling runs in Python inside the integration and
  Home Assistant's bundled go2rtc terminates the media. With the RTSP restream, HLS, `camera.record`
  and casting work too, and the cold-start warm-up keeps PyAV's hard 5-second dial from losing the
  race. The add-on's remaining advantage is field time, not features.
- **SenseIQ is readable.** Sleep state, breathing rate, session start and duration, sensing status
  (breathing and movement pinned on live hardware), and switches for the three alerts.
- **The camera's session pool is the thing to respect.** It holds 3–5 sessions, forcibly closes new
  ones when full, and reclaims abandoned ones slowly. Everything HA-side now fans out from a single
  session by design; exhausting the pool requires external help.
- **The ecosystem survey found no design threats** — it validated the native WebRTC contract, the
  ffmpeg self-reference restream, and warm-before-dial. What it found instead is user-facing polish
  we lack: events, repairs, availability semantics.

## The list

| # | Item | Effort | Gain | Priority |
|---|------|--------|------|----------|
| 1 | Soak the built-in backend | S | High | P1 |
| 2 | Availability and log hygiene | S | High | P1 |
| 3 | Repairs issues instead of log warnings | S | High | P1 |
| 4 | Event entity for monitor alerts | M | High | P1 |
| 5 | Bump the bridge container to the matching version | S | Med | P1 |
| 6 | Catch the awake sleep-stage code *(open question)* | S | Med | P1 |
| 7 | Pin the remaining sensing-status codes *(open question)* | S | Med | P2 |
| 8 | Entity hygiene audit | S | Med | P2 |
| 9 | Re-assert preload after a go2rtc restart | S | Med | P2 |
| 10 | Decide what DPS 15 means *(open question)* | S | Low | P2 |
| 11 | Image and device settings as entities | M | High | P2 |
| 12 | Minimal clips media source | M | Med | P2 |
| 13 | Confirm the 1080p delivery *(open question)* | M | Med | P2 |
| 14 | Multi-stream camera entities | M | Med | P3 |
| 15 | Version-skew guard for the bridge contract | M | Med | P3 |
| 16 | Speaker audio as a media_player *(open question)* | M | Med | P3 |
| 17 | Video diary controls | M | Med | P3 |
| 18 | Message Center | L | High | P3 |
| 19 | Upstream: built-in backend and go2rtc `tuya://` OEM auth | L | High | P3 |

## Notes per item

1. **Soak the built-in backend.** A night of normal use with the vendor app still connecting while
   Home Assistant streams, plus the two cold-start checks: `camera.record` from cold produces a
   clip with sound, and an HLS consumer renders on first request. This is the gate; nothing below
   11 should merge before it holds.
2. **Availability and log hygiene.** The integration never reads the device's cloud `online` flag,
   so a powered-off camera holds stale sensor values and looks alive; and "Device not found on LAN"
   logs every 20 seconds forever. Read the online flag into availability, log LAN presence only on
   state change, and keep stream failure separate from device availability (Reolink's split).
3. **Repairs issues.** The problems we currently whisper into the log deserve
   `issue_registry.async_create_issue`: go2rtc unreachable, both backends configured, the
   stop-the-add-on advice after a backend switch, persistent LAN darkness while cloud works, and
   the go2rtc client version pin failing. Delete each issue on recovery.
4. **Event entity.** One `event` entity per monitor with the alarm types as `event_types`, raw
   command as an attribute. Unknown commands (`ipc_custom`, observed live) then still fire and are
   automatable instead of being a warning and dropped data. SenseIQ cry/awake/no-signal fit the
   same shape. UniFi Protect is the model, including dedup per event id.
5. **Bridge version bump.** The add-on fallback is only a fallback if both halves are on the same
   release; a mismatched pair fails in ways the 2026.8.0 release notes describe.
6. **Awake sleep-stage code.** Turn on the Awake Alert and record what DPS 4 `css` reads at
   wake-up. `d` (deep, confirmed) and `l` (light, inferred) are known; one observation completes
   the vocabulary and makes Sleep State worth automating on.
7. **Sensing-status codes.** `b` = breathing and `m` = movement are pinned; the documented set also
   has no-signal, out-of-crib and analyzing, and `r == "network"` is documented as the no-signal
   condition. Each observation is one dictionary entry.
8. **Entity hygiene audit.** One pass over the ~15 entities per monitor: diagnostics categorized,
   curiosity-value sensors disabled by default, so a fresh install looks curated. UniFi's
   discipline, applied once.
9. **Preload re-assert.** If go2rtc restarts mid-run, stream registrations self-heal on the next
   open, but an armed `keep_stream_running` preload is lost until the next HA restart. Re-assert it
   from the bookkeeping task when go2rtc comes back. Only matters with the option on.
10. **DPS 15.** `no_senseiq_signal` reads `True` on a healthy monitor with a live breathing rate.
    It ships as a plain diagnostic with no problem semantics precisely because the polarity is
    unproven. Watch it flip against a known room state.
11. **Image and device settings.** Night vision, flip and mirror, brightness, contrast, WDR, OSD
    watermark, mic sensitivity, speaker volume, status light, privacy mode — all plain DP-backed
    controls of the same shape as the switches that already work. The largest pile of low-risk,
    daily-useful functionality available.
12. **Clips media source.** ~150 lines: one identifier scheme over a clips folder, resolved to
    `/local/…`. Unlocked by `camera.record` working; turns "on cry event → record 60 s" into a
    clip browsable from a phone. The baby-monitor-sized version of Frigate's media browser — skip
    the NVR-scale version forever.
13. **The 1080p question.** The camera served 1280×720 all through development, then delivered
    1920×1080 after a power cycle. Confirm with a 30-second capture checking frame dimensions over
    time, and record what flips it. Supersedes the old "720p mystery" entry: the answer may simply
    be camera state, not transport.
14. **Multi-stream entities.** The camera has a latent main/sub split (`stream_type` 2 vs 4).
    Reolink's pattern — a second camera entity per stream, disabled by default, each description
    carrying its stream key — makes the resolution behaviour observable instead of mysterious.
    Queue behind item 13 and the restream soak; needs a stream_type dimension in producer naming.
15. **Version-skew guard.** Stamp a `config_version` into the bridge JSON contract, have the bridge
    tolerate unknown fields, and raise a repairs issue when the running add-on predates a field the
    integration needs. Today this is documented tester discipline; make it visible at runtime.
16. **Speaker audio.** The frontend cannot do mic talkback for anyone (core has no microphone
    surface), but UniFi's shape — a `media_player` that plays a file or TTS through the camera
    speaker — may be reachable: the lullaby DPS machinery proves we can command audio. The open
    question is whether an arbitrary-audio path exists in the bounded Tuya surface; investigate
    before building.
17. **Video diary.** Event-triggered recording with per-type enables and sensitivity, all
    DP-backed. Detection runs on the firmware; we would only expose the controls.
18. **Message Center.** The cloud event timeline, with a short encrypted clip per event. Motion,
    sound and cry events with actual video in Home Assistant — the best feature left, and the most
    work: cloud API, clip fetch, decryption.
19. **Upstream.** Send the built-in backend to `thekoma/aventproxy` once the soak proves it.
    Separately, propose a generic OEM-credential auth mode for go2rtc's `tuya://` source — signing
    key, app key and package name as URL parameters. Open an issue there before writing the Go;
    note that Home Assistant's bundled go2rtc would also need `tuya` in its module allowlist,
    which is the harder half.

## Probably not

- **A cloud-free LAN path.** Signalling over TCP 6668 with KCP/AES media is real and proven, but it
  is a separate project: nothing standard speaks that transport, so it means writing a media stack.
- **Chasing a SenseIQ history API.** The DPS carry only the session in progress; the recorder turns
  that into history for free.
- **Mic talkback through the stock frontend.** Blocked in HA core itself — the camera platform has
  no microphone surface and the go2rtc provider sends offers unidirectionally. Vendor app or the
  AlexxIT WebRTC card until core grows the contract. Item 16 is the honest substitute.
- **A firmware `update` entity.** The Tuya surface exposes at best a version string; an update
  entity that cannot install is decoration.
- **Hardware-accelerated transcode.** The restream's 8 kHz mono AAC costs negligible CPU; even
  Frigate does not bother documenting acceleration for audio.

## Accepted costs

- A closed dashboard holds a Tuya session slot ~2 minutes past the last viewer (ICE disconnect
  detection plus the producer linger); no lever exists until go2rtc's client exposes WHEP session
  handles.
- Native WebRTC cameras advertise no HLS to the dashboard card, so a WHEP failure shows an error
  card rather than degrading; HLS still serves recording, casting and API consumers.
- Tuya `sid` sessions are not proactively refreshed — MFA makes silent re-login impossible — so
  invalidation lands as a reauth prompt, wired from both the coordinator and the stream server.

## Known limits of the built-in backend

- HEVC cameras are unsupported. The Avent models are H.264.
- Home Assistant only bundles go2rtc for container installs. On Home Assistant Core in a venv you
  need your own go2rtc and `go2rtc: url:`.
- Run one backend or the other, never both on one account: they derive the same Tuya MQTT client id
  and knock each other off the broker.

## Sources

Protocol findings are recorded in `WHITEPAPER.md`; design decisions and what was verified on
hardware are in `docs/superpowers/specs/`. Several SenseIQ facts are corroborated by an independent
reverse-engineering of the same app, [eisbaw/babymonitor-client](https://github.com/eisbaw/babymonitor-client)
(MIT), which targets the SCD921/923 and reaches the camera over Tuya's `imm` transport rather than
standard WebRTC — so where it and this project disagree about the media path, both are likely right
about their own firmware.
