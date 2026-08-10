# Roadmap

Where this project is, and what is worth doing next. Written 2026-08-09, after the built-in
streaming backend and the SenseIQ entities landed.

Everything here is ordered by value per unit of effort, not by ambition. Items marked **open
question** have a stated way to settle them; if you cannot say how you would find out, it does not
belong on this list.

## Where things stand

- **Streaming without the add-on works.** Tuya signalling runs in Python inside the integration and
  Home Assistant's bundled go2rtc terminates the media, so video goes camera → go2rtc directly over
  the LAN. Behind the `stream_backend` option, default `addon`.
- **SenseIQ is readable.** Sleep state, breathing rate, session start and duration, sensing status,
  and switches for the three alerts.
- **The camera's session pool is the thing to respect.** It holds 3-5 sessions, forcibly closes new
  ones when full, and reclaims abandoned ones slowly. Exhausting it locks out the vendor app too.
  Every design decision in the streaming path exists because of this.

## Now

- [ ] **Soak the built-in backend.** A night of normal use, with the vendor app still connecting
      while Home Assistant streams. That is the acceptance test, and it is what failed on the first
      attempt. Nothing below should be built before this holds.
- [ ] **Bump the bridge container to the matching version.** The add-on fallback is only a fallback
      if both halves are on the same release; a mismatched pair fails in ways the 2026.8.0 release
      notes describe.
- [ ] **Catch the awake sleep-stage code.** Turn on the Awake Alert switch and record what DPS 4
      `css` reads when the baby wakes. `d` (deep, confirmed) and `l` (light, inferred) are known; the
      third state is documented to exist — the app shows active-awake — but its letter has never been
      observed, and nothing public records it. One observation completes the vocabulary and makes
      Sleep State worth automating on.

## Next

- [ ] **Settle the 720p question.** The camera advertises a 1920x1080 main stream, reports
      `vedioClarity: 8` (its highest), and still serves the 1280x720 sub-stream over WebRTC.
      Ruled out: HEVC, the protocol-312 resolution message, a stuck clarity setting. The remaining
      test is the offer's `stream_type` with a capture long enough to see a mid-stream switch —
      30 seconds, checking frame dimensions over time, not an 8-second clip's header.
      **Open question.** The likely answer is that HD is only served over Tuya's proprietary `imm`
      transport, which no standard client speaks; if so, record that and close it.
- [ ] **Image and device settings as entities.** Night vision, flip and mirror, brightness,
      contrast, WDR, OSD watermark, mic sensitivity, speaker volume, status light, privacy mode. All
      plain DP-backed controls of the same shape as the switches that already work. The largest pile
      of low-risk, daily-useful functionality available.
- [ ] **Pin the remaining sensing-status codes.** `b` = breathing is documented and observed; the
      set also contains moving, no-signal, out-of-crib and analyzing, and `r == "network"` is
      documented as the no-signal condition. Each observation is one dictionary entry.
- [ ] **Decide what DPS 15 means.** `no_senseiq_signal` reads `True` on a healthy monitor with a
      live breathing rate. It ships as a plain diagnostic with no problem semantics precisely
      because the polarity is unproven. **Open question:** watch it flip against a known room state.

## Later

- [ ] **Message Center.** The cloud event timeline, with a short encrypted clip per event. This is
      motion, sound and cry events with actual video in Home Assistant — the best feature left, and
      the most work: cloud API, clip fetch, decryption.
- [ ] **Video diary.** Event-triggered recording with per-type enables and sensitivity, all
      DP-backed. Detection runs on the firmware; we would only expose the controls.
- [ ] **Upstream.** Send the built-in backend to `thekoma/aventproxy` once the soak proves it.
      Separately, propose a generic OEM-credential auth mode for go2rtc's `tuya://` source — the
      signing key, app key and package name as URL parameters rather than anything vendor-specific
      baked in. Open an issue there before writing the Go. Note that Home Assistant's bundled go2rtc
      would also need `tuya` added to its module allowlist, which is the harder half.

## Probably not

- **A cloud-free LAN path.** Signalling over TCP 6668 with KCP/AES media is real and proven, and is
  the only known route to 1080p. It is also a separate project rather than an increment on this one:
  nothing standard speaks that transport, so it means writing the media stack.
- **Chasing a SenseIQ history API.** The DPS carry only the session in progress. Home Assistant's
  recorder turns that into history for free; a proprietary statistics endpoint is not worth
  reversing.
- **Two-way audio on the built-in backend.** The offer path supports it, but end-to-end talkback is
  unproven here, and asking the camera for it makes it take its speaker and stop a playing lullaby.

## Known limits of the built-in backend

- No HLS, no `camera.record`, no casting: go2rtc's RTSP is loopback-only and the stream source is
  not an RTSP URL. Home Assistant still advertises HLS and logs `Protocol not found` when something
  asks for it.
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
