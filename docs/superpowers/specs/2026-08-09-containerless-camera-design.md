# Containerless Camera Streaming (the "builtin" backend)

Expose the Avent camera to Home Assistant **without shipping our own Docker container**, keeping a
real `camera.*` entity and a URL a tablet can open without HA. Built and verified against a real
SCD953 on 2026-08-09, against `homeassistant==2026.8.1` and `AlexxIT/go2rtc@v1.9.14` (the version
HA bundles). The code: `custom_components/philips_avent/{signaling,stream_server,sdp,frame_cache,
preload}.py`, behind the `stream_backend` option; the add-on remains the default.

## Why not the add-on

The container costs three add-on cards kept in lockstep, a multi-arch image build per release, a
version-skew failure mode that is entirely ours, no path for HA Container/Core users, and ~5 000
lines of Go we own. The media path was never the problem: signaling goes over Tuya's cloud MQTT and
the H.264/SRTP media runs peer-to-peer over the LAN. What the container really did was *terminate*
that WebRTC session and republish it — and HA already ships a process for exactly that: go2rtc.

## How the pieces fit

```
camera.stream_source() -> "webrtc:ws://127.0.0.1:38555/avent/<cam_id>?t=<secret>"
                                            │
HA go2rtc provider ── streams.add() ──▶ go2rtc ──ws──▶ aiohttp endpoint inside the integration
                                            │              │ tuya API calls (api.py)
                                            │              │ tuya MQTT signaling (signaling.py)
                                            └── SRTP/H.264 ◀┘ ...offer/answer relayed
                                                 direct from the camera over the LAN
```

- HA's go2rtc provider registers any scheme go2rtc supports (`GET /api/schemes`), so returning a
  `webrtc:ws://` URL from `stream_source()` needs no extra plumbing.
- go2rtc's ws source protocol: it sends `{"type":"webrtc/offer"}` and requires the answer as **the
  very next message**; candidates arriving earlier must be buffered (`stream_server.py`).
- go2rtc closes the signaling websocket the moment ICE connects, so the Tuya session's lifetime
  must not hang off the socket. It ends on the camera's own `disconnect`, on replacement by a new
  dial, or on a 120 s linger.
- One MQTT connection per account (`SignalingHub`), shared by every camera and session: Tuya's
  client id has nothing per-session in it, so a second connection is kicked off the broker by the
  first. This is also why the add-on and the builtin backend must never run on one account —
  setup deletes the add-on's config file and says so in the log.
- The camera only answers a particular shape of offer — audio-first m-lines, no `a=extmap`, one
  audio + one video section — and its answer needs reshaping for the consumer. The rules and their
  discovery are in `WHITEPAPER.md` ("The camera is picky about the offer it will answer") and
  `sdp.py`; the captured fixtures in `tests/test_philips_avent/fixtures/` are the ground truth.
- Stills come from a 60 s TTL cache (`frame_cache.py`) with `use_stream_for_stills = False`,
  because go2rtc's `/api/frame.jpeg` dials and stops the producer per frame — HA's 10 s thumbnail
  poll opened ~360 Tuya sessions/hour that way and exhausted the camera's 3–5 slot session pool
  (see WHITEPAPER, "The session pool").
- Session hygiene, all field-learned: an unanswered offer means a full pool, so `ANSWER_TIMEOUT`
  is 6 s and a 25 s circuit breaker then refuses to dial — no cloud call, no offer — so the pool
  can drain. Abandoned sessions (answer timeout, handshake failure, replacement) get an explicit
  `disconnect` for our own session id; a stream that may still be flowing (linger expiry, unload,
  server stop) is never disconnected. An earlier revision concluded the `disconnect` frame was
  peer-scoped and removed it entirely; that was refuted — the observation was a misread (the
  "surviving" session had merely been replaced by the next snapshot poll) and contaminated by a
  second go2rtc dialling the same endpoint. The frame is session-scoped, and both go2rtc's
  `pkg/tuya` and this repo's Go bridge send it for their own session at teardown.
- `keep_stream_running` (default off) holds one permanent session via go2rtc's own preload API —
  NOT HA's `preload_stream` camera preference, which would feed the `webrtc:ws://` URL to ffmpeg
  ("Protocol not found", forever). go2rtc's `PUT /api/preload` is destructive (stops and redials
  the producer), so the preloader checks before it PUTs; a blind PUT on every answer is a feedback
  loop. Full rationale in `preload.py`'s docstring.
- The tablet URL: go2rtc terminates the media, so go2rtc serves it — `go2rtc: debug_ui: true` and
  `http://<ha-ip>:11984/stream.html?src=philips_avent_<unique_id>&mode=webrtc` (WebRTC only; HA
  enables no mp4/hls modules). A browser player page negotiating directly with the camera was
  built, verified to RTP flowing, and removed as an unneeded second HTTP surface; it is in git
  history.

## Verified on hardware

- go2rtc 1.9.14 consuming the ws endpoint through the real modules: H.264 1280×720 + PCMU out of
  go2rtc's RTSP, no container anywhere in the path; the camera answers in ~0.12 s and trickles
  host, srflx and TURN-relay candidates within ~150 ms.
- In Home Assistant itself (official image under rootless podman, host networking): server binds
  on setup, camera negotiates through HA's managed go2rtc.
- A missing or wrong token and an unknown camera id are refused with 403 before any Tuya call.
- The camera echoes the offer's payload-type numbering rather than forcing its own.
- This SCD953 is H.264 only (streamType 2 = 1080p main, 4 = 720p sub, audio L16/16000); the
  `stream_type` field sent in the offer is the skill's own streamType, matching the Go bridge —
  an early draft sent 0/1 from a go2rtc comment, and parity with the field-tested bridge won.
- Not yet field-verified (unit-tested only): the circuit breaker, disconnect-on-abandonment and
  the snapshot cache — the acceptance test is the vendor app connecting promptly while HA holds a
  live stream. The 3–5 slot pool and ~20-minute zombie reclaim are TuyaOS firmware documentation,
  not measurements on this unit.

## Deliberately not supported

- **HLS, `camera.record`, casting** — go2rtc's RTSP is loopback-only and `stream_source()` is no
  longer RTSP. Fair trade for a baby monitor; the add-on backend still offers them.
- **HEVC** — the datachannel/fmp4 path has no equivalent in go2rtc's generic `webrtc:` source;
  Avent models stream H.264. `signaling.py` refuses HEVC skills with a clear error.
- **HA remote from the camera's LAN** — HA clears go2rtc's ICE servers; same-LAN is unaffected
  (the camera's host candidate wins, exactly as with the add-on).
- **HA Core in a venv** has no managed go2rtc; users run their own and set `go2rtc: url:`.
- **Two-way audio end-to-end** — the `talkback` option shapes the offer (`sendrecv` audio stays
  opt-in; issue #72: it stops a playing lullaby), but the path is unproven on this backend.
- **One consumer per camera.** A dial seconds after an answered dial means a second go2rtc is
  attached; the server warns loudly but proceeds, because it cannot distinguish that from a
  legitimate quick reopen and refusing would strand the user with no stream.

## Open questions and the endgame

- The stream arrives at 720p, not the advertised 1080p: the camera acks both the offer's
  `stream_type` and the protocol-312 `resolution` command and keeps sending the sub-stream. Check
  whether the add-on actually delivers 1080p today — this may be long-standing.
- Phase 3 (not started): flip the default backend, deprecate the add-on cards, delete the Go tree
  from the build. Blocked on field verification above.
- The endgame is upstreaming a mobile-SDK auth mode into go2rtc's own `tuya://` source (~300 lines
  against its `TuyaAPI` interface, plus HA allowlisting the module); then `stream_source()`
  collapses to one `tuya://` URL and this shim becomes the fallback for old HA versions.
