# go2rtc RTSP Restream (HLS, recording and casting for the builtin backend)

Give the built-in backend back what the add-on always had: HLS, `camera.record`, casting and
snapshots that share a running stream — by registering the WebRTC producer as a named go2rtc
stream and pointing `stream_source()` at go2rtc's own RTSP output. Amends
`2026-08-09-containerless-camera-design.md`, whose "Deliberately not supported" list this deletes
most of. Facts below were verified 2026-08-09 against HA core `dev`
(`homeassistant/components/go2rtc/`), go2rtc v1.9.14 source, `python-go2rtc-client` `main`, and a
live go2rtc `/api` endpoint; each is sourced where it matters.

## Problem

`stream_source()` on the builtin backend returns `webrtc:ws://127.0.0.1:38555/avent/<id>?t=<tok>`.
HA's go2rtc provider registers that happily (WebRTC live view works), but HA's **stream
component** — the machinery behind HLS, `camera.record` and casting — feeds the same URL to
PyAV/ffmpeg, which cannot open it and logs `Protocol not found`, forever.

The original design dismissed this as inherent: "go2rtc's RTSP is loopback-only and
`stream_source()` is no longer RTSP." That conflated two consumers. Loopback-only RTSP does block
the *tablet-without-HA* case, but the stream component runs **in the HA process, on the same host
as the managed go2rtc** — HA core's own config template pins the RTSP listener to
`127.0.0.1:18554` (`go2rtc/server.py`), and loopback RTSP is exactly what the provider's own
`ffmpeg:` sources consume. The features were reachable all along; the URL scheme was the only
thing in the way.

## Goals

- HLS, `camera.record` and casting work on the builtin backend, **with sound**. Silent recordings
  on a baby monitor are worse than no recordings; audio is in scope, not a follow-up.
- Live view keeps working in every configuration this change touches, unconditionally. Never trade
  live view for HLS: when go2rtc's RTSP is unusable, degrade to today's `webrtc:` URL and log once.
- Still exactly one Tuya session. The producer stream is dialled once; live view, HLS, recording
  and frame grabs all fan out from go2rtc — restoring the Go bridge's "one session, N consumers"
  property. The ws layer's one-consumer-per-camera constraint (`stream_server.py`,
  `RECENT_ANSWER`) is **unchanged** — fan-out happens above it, in go2rtc; what changes is that
  fewer things now have a reason to dial the ws endpoint a second time.

## Non-goals

- **Removing `FrameCache` / flipping `use_stream_for_stills`.** Untouched. go2rtc's frame handler
  still stops the producer it dialled; the cache stays the guard against the ~360 sessions/hour
  incident. Free win anyway: a frame grab's dial of the `_camera` stream now attaches to a hot
  `_src` RTSP instead of opening a fresh Tuya session — evaluate cache removal separately.
- **Remote go2rtc support.** The signaling server binds loopback (`stream_server.py`), so a go2rtc
  on another host can't dial the producer today either. The degradation table covers it honestly;
  building for it is not worth it.
- **The tablet URL.** Unchanged; go2rtc's own page remains the answer.
- **The `tuya://` endgame.** Upstreaming mobile-SDK auth into go2rtc stays the long-term plan
  (ROADMAP "Later"); this change neither advances nor blocks it.

## Design

Two go2rtc streams per camera, with names that can never collide:

```
philips_avent_<id>_src      ["webrtc:ws://127.0.0.1:38555/avent/<id>?t=<tok>",
  (ours, via PUT /api/streams)   "ffmpeg:philips_avent_<id>_src#audio=aac"]
        ▲ RTSP (loopback)                  ▲ pulls _src's own RTSP, PCMU → AAC, on demand
        │
philips_avent_<id>_camera   [stream_source() result, "ffmpeg:..._camera#audio=opus"]
  (HA's provider, as today)
```

- `stream_source()` returns `rtsp://127.0.0.1:<rtsp_port>/philips_avent_<id>_src?video&audio=aac`.
  The stream component gets H.264 + AAC and nothing else; go2rtc's RTSP consumer query filters
  codecs, so PyAV never sees the PCMU track and the "which audio track is first" ambiguity never
  arises.
- HA's provider keeps doing what it does: it registers whatever `stream_source()` returns (any
  scheme in `GET /api/schemes`; `rtsp` qualifies), re-adds only when no producer URL matches
  (`go2rtc/__init__.py`), so upgrades self-heal on the next offer and a stable URL causes no churn.
- go2rtc consuming its own loopback RTSP is not novel: HA's provider itself attaches
  `ffmpeg:<name>#audio=opus` — ffmpeg pulling go2rtc's own RTSP of that same stream — to every
  camera it registers. This design does the same across two names.

### Audio

HA's stream component hard-filters audio to `AUDIO_CODECS = {"aac", "mp3"}` (`stream/const.py`)
and drops everything else; the camera sends PCMU. Hence the `ffmpeg:<src>#audio=aac` second source
on `_src` — the exact self-referencing transcode pattern HA core uses — plus the `?audio=aac`
consumer filter. go2rtc starts sources on demand per requested codec, so the transcode runs only
while something consumes AAC (a recording, an HLS viewer, a cast), not during plain live view.

Honesty note for the README: this is 8 kHz telephone-band G.711 re-wrapped as AAC. Recordings
carry cries and voice fine; the transcode adds no fidelity the camera never sent.

### Live view stays single-hop: the WHEP override

With `stream_source()` now RTSP, the provider's default path would make live view a chain —
camera → `_src` → loopback RTSP → `_camera` → browser — and its audio a **double transcode**
(PCMU → AAC → opus), because `_camera`'s source is the AAC-filtered RTSP URL. That downgrades the
most-used feature to prop up the less-used ones, which violates the second goal. So the camera
entity overrides `async_handle_async_webrtc_offer` to negotiate WHEP directly against `_src`
(native H.264 + PCMU, one hop, zero transcode), exactly as the Frigate integration does against
its go2rtc (`frigate-hass-integration/custom_components/frigate/camera.py`), using
`go2rtc_client`'s WHEP forward (`forward_whep_sdp_offer(source_name, WebRTCSdpOffer) ->
WebRTCSdpAnswer` — models, not strings). **In scope, not a stretch goal — but a
separately-committed, separately-revertible task**, and any failure falls back to `super()` (the
provider's double-hop path), so the worst case is inefficiency, never a black screen.

Session teardown must follow the offer: HA's websocket handler unconditionally calls
`close_webrtc_session(session_id)`, and the base `Camera` delegates it to the go2rtc provider —
which pops the session from its book-keeping **without a default**, so a session the provider
never negotiated is a `KeyError` on every live-view teardown. The entity therefore tracks which
session ids it delegated to `super()` and overrides `close_webrtc_session` to delegate only
those; WHEP-negotiated sessions need only local bookkeeping, since a go2rtc WHEP session ends
with its peer connection. Candidates are asymmetric and safe: the provider ignores
unknown-session candidates at debug level, so `async_on_webrtc_candidate` needs no override.

### Finding the RTSP endpoint

Nothing public exposes the RTSP address. `go2rtc_client.ApplicationInfo` carries only `version`;
the managed instance's API rides a **unix socket with per-boot `local_auth` credentials and an
`allow_paths` whitelist** (`go2rtc/server.py`), so every request must reuse the session object
from `hass.data["go2rtc"]` — the same private-but-honest reach-in `preload.py` already documents.
The probe is a raw `GET {url}/api` on that session — bare `/api` **is** on the managed
instance's `allow_paths` whitelist (HA dev `server.py`: `["/", "/api", "/api/frame.jpeg",
"/api/preload", "/api/schemes", "/api/streams", "/api/webrtc", "/api/ws"]`), so no
resolved-elsewhere plan B is needed. go2rtc's rtsp module registers `app.Info["rtsp"]` (verified
in v1.9.14 `internal/rtsp/rtsp.go` and against a live instance), so the response carries
`{"rtsp": {"listen": ...}}`.

The probe's verdict divides into **permanent** (config-shaped, cacheable) and **transient**
(retry-shaped), and only permanent verdicts may pick the `webrtc:` fallback — because HA's
`Camera.async_create_stream` calls `stream_source()` **at most once per entity lifetime** and
freezes the result into its cached `Stream`. A transient hiccup that handed the stream component
the ws URL would kill HLS until entry reload; handing it the stable RTSP URL instead is harmless,
since HA's stream worker retries its source with backoff and starts working the moment `_src`
exists. Decision table, `parse_rtsp_endpoint(listen, api_host)` plus the transient policy:

| situation | result | cached? |
|---|---|---|
| `"127.0.0.1:18554"` (managed), API host loopback | `127.0.0.1:18554` | yes |
| `":8554"` (all interfaces), API host loopback | `127.0.0.1:8554` | yes |
| `":8554"`, API host remote | `<api_host>:8554` | yes |
| loopback-bound listen, API host remote | permanent no-RTSP → `webrtc:` URL, log HLS unavailable | yes |
| listen absent / empty / unparsable | permanent no-RTSP → `webrtc:` URL, log once | yes |
| no `hass.data["go2rtc"]` slot at all | `webrtc:` URL (HA Core without go2rtc; with `after_dependencies` this is config-shaped) | no — rechecked per call |
| probe request fails (timeout, network), API host loopback | **provisional** `127.0.0.1:18554` — the managed pin, version-coupled and logged as such | no — re-probed next call |
| probe request fails, API host remote | `webrtc:` URL (cannot guess a remote endpoint; remote is unsupported anyway) | no |

### Registration: idempotent, inside `stream_source()` — and honest about who calls it

`PUT /api/streams` silently replaces an existing stream without stopping its producers
(`internal/streams/streams.go`), and API-registered streams die with go2rtc — whose watchdog
respawns it on process death or a stalled health check. The existing re-arm hooks cannot cover
that: `on_answered` only fires after go2rtc dials the ws endpoint, which never happens while
`_src` is unregistered. So registration follows `preload.py`'s check-then-PUT-under-lock idiom
and lives **inside `stream_source()`**, with a PUT only when the stream is absent or its producer
list differs from the two sources above.

Be precise about what re-invokes it, because HA's stream component does **not**: `Camera.
async_create_stream` calls `stream_source()` at most once per entity lifetime and caches the
`Stream` object (nothing calls `Stream.update_source`). The paths that actually re-run
registration are: the provider's `_update_stream_source` — which calls `stream_source()` on
every `async_get_image`, i.e. at most once per `FrameCache` TTL while dashboards poll; the WHEP
override, on every live-view open; and a best-effort pass at entry setup (restoring the
cold-start property: go2rtc knows the stream before the first dial). That coverage recovers from
a go2rtc restart within one thumbnail cycle or one live-view open — and the frozen-`Stream` case
is defused by the stable-URL policy above: the cached RTSP URL never changes, so a stream opened
during an outage starts working as soon as any of those paths re-registers `_src`.
`manifest.json` gains `after_dependencies: ["go2rtc"]` so the setup pass stops racing go2rtc's
own setup.

### Naming

`go2rtc_producer_name(cam_id)` = `philips_avent_<id>_src`, built beside `go2rtc_stream_name()`
(= `..._camera`, mirroring HA's `get_camera_identifier`) in `const.py`. The two must never be
equal: if HA's provider ever registered *its* name over ours, `_camera`'s source would become its
own restream — a self-consuming loop go2rtc does not guard against. The helper asserts the names
differ and a test pins both suffixes.

### `keep_stream_running` retargets — and gets safer

The stream holding the Tuya session is now `_src`, so the preloader arms
`philips_avent_<id>_src`. Arming only `_camera` would hold an RTSP consumer whose producer could
still be stopped; arming both buys nothing. Side benefit: HA's provider enables/disables preload
for **camera-identifier names** according to the `preload_stream` camera preference (the
behind-our-back disarm `preload.py`'s docstring documents) — `_src` is not a camera identifier,
so the option leaves that blast radius entirely. `async_disable` sweeps **both** names, or an
upgrade strands a `_camera` preload armed by an older version.

Lifecycle hygiene: `async_remove_entry` best-effort-disables the preload for both names — on an
**external** go2rtc, an orphaned `_src` preload would otherwise keep dialling a dead loopback
signaling port unbounded. Removal only, not unload: unload runs on every options reload, and a
disable-then-re-arm cycle there would churn the producer's Tuya session per config change. The
inert stream *definition* left behind (`go2rtc_client` has no streams-delete) is accepted:
nothing dials an unpreloaded, unconsumed stream, and on the managed instance it dies with the
next HA restart anyway. **Downgrade is an accepted risk, documented not solved:** an older
version's `async_disable` sweeps only `_camera`, so a `_src` preload armed by this version
survives a downgrade — the release notes say to turn `keep_stream_running` off before
downgrading. **Open question:** whether go2rtc's
preload consumer (default query `video&audio`) starts the ffmpeg AAC source or is satisfied by the
ws producer's PCMU. If it pins the transcode 24/7, pass `audio_codec_filter` on
`preload.enable()` (the parameter exists in `go2rtc_client`) restricting it to the native codecs.

## Failure modes and guards

- **Redial pressure.** go2rtc's ffmpeg sources redial eagerly during failures (`stream_server.py`,
  `COOLDOWN` note), and `_src` now carries one of its own. The existing 25 s circuit breaker and
  6 s `ANSWER_TIMEOUT` are the guard and must not be weakened; the soak test must include a
  failing-camera window with a recording active.
- **Availability semantics change.** Stream-component failures (cooldown, full pool) can now
  surface as camera-entity unavailability flapping — a path the `webrtc:` design never exercised.
  Watch it in the soak; no pre-emptive code.
- **Secrets.** The registered src embeds `?t=<token>`; it lives only inside go2rtc, which already
  holds the same token in `_camera`'s ws source today — no new exposure. Log stream *names*, never
  srcs. The RTSP URL itself is token-free. Diagnostics' `stream_token` redaction (96485b0) remains
  sufficient. The unauthenticated loopback RTSP endpoint is readable by any local process — one
  sentence in the README, same posture as the managed go2rtc itself.
- **Version coupling.** `go2rtc_client` pins servers to `1.9.13 ≤ v < 2.0.0`; the provisional
  18554 constant couples to HA's config template. Both are the price of the private surface; both
  are logged degradations, not crashes.
- **Multi-entry port skew.** `_async_start_streaming` keeps the **first** entry's signaling port
  and only warns when a second entry asks for a different one — but a ws URL built from the
  second entry's *option* would register a `_src` pointing at nothing. Pre-existing bug shape
  (`camera.py:45` builds from the entry option today); the fix here is to build `ws_url` from the
  server's actual `server.port`, which this change does for builtin cameras.

## Sequencing and acceptance

Lands **after** the ROADMAP's overnight soak of the builtin backend passes — this change
multiplies the ways consumers can start the producer, and building it on an unverified foundation
compounds risk. Acceptance test: a `camera.record` clip that plays back **with sound** while live
view stays up, on one Tuya session, with the vendor app still able to connect.

## Docs to amend

- `2026-08-09-containerless-camera-design.md`: rewrite the "Deliberately not supported" HLS bullet
  (now conditional on go2rtc RTSP) and the loopback-only rationale.
- `README.md` backend table: HLS/record/casting → "Yes, via go2rtc's RTSP" for builtin; add the
  8 kHz audio sentence.
- `ROADMAP.md` "Known limits": drop the HLS bullet.
- `preload.py` module docstring: retarget to `_src`, note the escaped blast radius.
- `const.py` stale comments: the `CONF_KEEP_STREAM_RUNNING` block ("would feed our webrtc: URL to
  ffmpeg/HLS") and `builtin_stream_url`'s docstring framing ("what `stream_source()` returns")
  both become wrong once `stream_source()` is RTSP — rewrite alongside the code they describe.

## References

- HA go2rtc component: `homeassistant/components/go2rtc/{__init__,server}.py` (dev, 2026-08-09)
- go2rtc v1.9.14: `internal/{api/api,rtsp/rtsp,streams/streams}.go`
- `python-go2rtc-client`: `go2rtc_client/{rest,models}.py`
- Frigate WHEP precedent: `frigate-hass-integration/custom_components/frigate/camera.py`
- Amends: `docs/superpowers/specs/2026-08-09-containerless-camera-design.md`
