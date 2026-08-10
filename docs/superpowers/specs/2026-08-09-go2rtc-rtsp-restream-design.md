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
  incident. Free win anyway: a frame grab now hits `_src` directly (`Restreamer.snapshot`) and
  attaches to a hot producer instead of opening a fresh Tuya session — evaluate cache removal
  separately.
- **Remote go2rtc support.** The signaling server binds loopback (`stream_server.py`), so a go2rtc
  on another host can't dial the producer today either. The degradation table covers it honestly;
  building for it is not worth it.
- **The tablet URL.** Unchanged; go2rtc's own page remains the answer.
- **The `tuya://` endgame.** Upstreaming mobile-SDK auth into go2rtc stays the long-term plan
  (ROADMAP "Later"); this change neither advances nor blocks it.

## Design

Two go2rtc streams carry everything for a builtin camera — the split is load-bearing (field
lesson, 2026-08-10, see "The one-consumer stream stays single-source" below):

```
philips_avent_<id>_src      ["webrtc:ws://127.0.0.1:38555/avent/<id>?t=<tok>"]   ← the ONLY source
  (ours, via PUT /api/streams)
        ▲ WHEP: live view, negotiated by the camera entity itself
        ▲ frame.jpeg: stills, at most once per FrameCache TTL
        ▲ RTSP (loopback): consumed by _src_aac's ffmpeg below — fan-out, one Tuya session

philips_avent_<id>_src_aac  ["ffmpeg:philips_avent_<id>_src#video=copy#audio=aac"]
  (ours, via PUT /api/streams)
        ▲ RTSP (loopback): HA's stream component — HLS, camera.record, casts

philips_avent_<id>_camera   (HA's provider's name for a camera's stream. For builtin
  cameras NO provider is attached at all — the entity is native WebRTC, see the live-view
  section — so this stream exists only for add-on cameras, and as a stale leftover after an
  upgrade, dying with go2rtc's next restart. The name must still never collide with ours.)
```

- `stream_source()` returns `rtsp://127.0.0.1:<rtsp_port>/philips_avent_<id>_src_aac` — no
  codec-filter query needed: `_src_aac` carries exactly H.264 + AAC by construction, so PyAV
  cannot pick up a codec HA's stream would drop.
- go2rtc consuming its own loopback RTSP is not novel: HA's provider itself attaches
  `ffmpeg:<name>#audio=opus` — ffmpeg pulling go2rtc's own RTSP — to every camera it registers.
  `_src_aac`'s source is the same mechanism, pointed at a *different* name.

### The one-consumer stream stays single-source

The first field build carried the AAC transcode as a *second source on `_src` itself* — HA
core's own self-referencing pattern. On this backend that pattern is a trap: the ws endpoint
serves exactly one consumer per camera (`stream_server.py`), and a second source gives go2rtc
something of its own to start, EOF and redial while the real session runs. Combined with the
compare defect below it produced the 2026-08-10 storm: a camera session replaced every ~5 s
until live view died. HA's pattern is safe only because its first source is a fan-out-capable
RTSP; ours is not. Hence the rule: **`_src` has exactly one source, forever.** The transcode
lives on `_src_aac`, whose ffmpeg dials `_src`'s RTSP — a fan-out consumer, never the camera.

### Audio

HA's stream component hard-filters audio to `AUDIO_CODECS = {"aac", "mp3"}` (`stream/const.py`)
and drops everything else; the camera sends PCMU. Hence `_src_aac`: ffmpeg pulls `_src`'s RTSP,
copies the video (`#video=copy` is load-bearing — `#audio=aac` alone renders `-vn`, audio only;
go2rtc `internal/ffmpeg`) and transcodes PCMU→AAC. go2rtc starts sources on demand, so the
transcode runs only while something consumes `_src_aac` (a recording, an HLS viewer, a cast),
not during plain live view.

Honesty note for the README: this is 8 kHz telephone-band G.711 re-wrapped as AAC. Recordings
carry cries and voice fine; the transcode adds no fidelity the camera never sent.

### Live view: native WHEP, single hop — and no provider at all

With `stream_source()` now RTSP, letting HA's go2rtc provider drive live view would make it a
chain — camera → `_src` → loopback RTSP → `_camera` → browser — and its audio a **double
transcode** (PCMU → AAC → opus), because `_camera`'s source would be the AAC-filtered RTSP URL.
That downgrades the most-used feature to prop up the less-used ones, which violates the second
goal. So the builtin camera negotiates WHEP directly against `_src` (native H.264 + PCMU, one
hop, zero transcode), as the Frigate integration does against its go2rtc
(`frigate-hass-integration/custom_components/frigate/camera.py`), using `go2rtc_client`'s
`forward_whep_sdp_offer(source_name, WebRTCSdpOffer) -> WebRTCSdpAnswer` — models, not strings.

Overriding `async_handle_async_webrtc_offer` has a consequence HA makes non-negotiable: the
entity becomes **native WebRTC**. HA detects the override at the *class* level
(`homeassistant/components/camera/__init__.py`: `_supports_native_async_webrtc =
type(self).async_handle_async_webrtc_offer != Camera.async_handle_async_webrtc_offer`) and then
never attaches a WebRTC provider to the entity. Three things follow:

- **No fallback exists.** A failed WHEP negotiation cannot fall back to the provider path — it is
  unreachable by construction, not merely unbuilt. The entity sends `WebRTCError` to the frontend
  and live view is down until go2rtc recovers.
- **The override must not leak onto the add-on cameras**, whose live view IS the provider. The
  builtin camera is therefore its own entity class (`AventBuiltinCamera`); the shared base keeps
  no WebRTC overrides.
- **Stills cannot use the provider either** (`webrtc_provider` is None on a native entity), so
  the frame cache is fed by `Restreamer.snapshot` — go2rtc's `frame.jpeg` on `_src` — sharing a
  hot producer for free, one Tuya session per cache miss when cold: the price the provider frame
  path paid.

Teardown and candidates, verified against HA core dev: the base `close_webrtc_session` is a
**no-op when no provider is attached**, so it needs no override (an earlier draft tracked
delegated sessions to dodge a provider `KeyError`; with no provider there is nothing to dodge).
`async_on_webrtc_candidate` on the base *raises* for a provider-less camera, and the frontend
trickles its local candidates regardless — so the entity overrides it as a documented no-op:
WHEP returned a complete answer, candidates are noise.

Still separately committed and separately revertible: a revert restores the provider path
(double hop, provider-registered `_camera`, provider-served stills) with zero collateral.

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
registration are: the WHEP negotiation, on every live-view open; `Restreamer.snapshot`, at most
once per `FrameCache` TTL while dashboards poll; and a best-effort pass at entry setup
(restoring the cold-start property: go2rtc knows the stream before the first dial). That
coverage recovers from a go2rtc restart within one thumbnail cycle or one live-view open — and
the frozen-`Stream` case
is defused by the stable-URL policy above: the cached RTSP URL never changes, so a stream opened
during an outage starts working as soon as any of those paths re-registers `_src`.
`manifest.json` gains `after_dependencies: ["go2rtc"]` so the setup pass stops racing go2rtc's
own setup.

### Cold start: the chain must be warm before PyAV dials

Field, 2026-08-10 23:54: a cold `camera.record` failed with `Error opening stream (Invalid data
found when processing input)`, no clip was written, HLS clients spun forever, and every 15 s
stream-worker retry dialled a fresh camera session. With the chain warm, a direct ffprobe of
`_src_aac` succeeded and showed exactly the promised H.264 + AAC. The mechanism: PyAV opens RTSP
with a **hardcoded 5 s socket timeout** (`stream/_convert_stream_options`, `stimeout: 5000000`)
and there is no supported hook to raise it — `Camera.stream_options` exists but is
schema-limited to `rtsp_transport`/`use_wallclock_as_timestamps`/`extra_part_wait_time`
(`STREAM_OPTIONS_SCHEMA`); that is the prong-1 verdict, recorded here so nobody monkeypatches.
A cold `_src_aac` needs go2rtc to launch ffmpeg, ffmpeg to dial `_src`, the ws source to build a
Tuya session and a first keyframe — ~5–8 s measured, longer than PyAV's patience.

So `stream_url()` **warms the chain before the URL goes out**: if go2rtc does not already report
an active producer on `_src_aac`, it arms a *temporary* preload there (an API-side consumer, the
same lever `keep_stream_running` uses — on a different name), polls until the producer is active
(bounded at 8 s: `stream_source()` itself runs under HA's 10 s
`CAMERA_STREAM_SOURCE_TIMEOUT`, and PyAV's 5 s only starts after), and releases that preload 90 s
later — by then the real consumer holds the chain, and if none ever came the chain winds down
rather than streaming the camera for nobody. The check-then-enable is serialized so concurrent
opens arm once; no lock is held across the polls; a preload armed by anyone else is neither
re-armed (a preload PUT drops and redials its consumer) nor released. `keep_stream_running`'s
own preload lives on `_src` and is untouched. The setup pass registers with `warm=False` —
warming at every HA start would open a camera session for nobody. Warm-up failure or timeout
still returns the stable URL: the stream worker retries, warmer each time.

### Naming

`go2rtc_producer_name(cam_id)` = `philips_avent_<id>_src` and `go2rtc_aac_name(cam_id)` =
`philips_avent_<id>_src_aac`, built beside `go2rtc_stream_name()` (= `..._camera`, mirroring
HA's `get_camera_identifier`) in `const.py`. All three must stay pairwise distinct: if HA's
provider ever registered *its* name over ours, `_camera`'s source would become its own restream —
a self-consuming loop go2rtc does not guard against — and `_src`/`_src_aac` colliding would
register the transcode over the producer. The helpers assert distinctness and a test pins all
three suffixes.

### `keep_stream_running` retargets — and gets safer

The stream holding the Tuya session is now `_src`, so the preloader arms
`philips_avent_<id>_src`. Arming only `_camera` would hold an RTSP consumer whose producer could
still be stopped; arming both buys nothing. Side benefit: HA's provider enables/disables preload
for **camera-identifier names** according to the `preload_stream` camera preference (the
behind-our-back disarm `preload.py`'s docstring documents) — `_src` is not a camera identifier,
so the option leaves that blast radius entirely. `async_disable` sweeps **all three** names
(`_src`, `_src_aac`, legacy `_camera`), or an upgrade strands a preload armed under a name this
version no longer arms.

Lifecycle hygiene: `async_remove_entry` best-effort-disables the preload for every name — on an
**external** go2rtc, an orphaned `_src` preload would otherwise keep dialling a dead loopback
signaling port unbounded. Removal only, not unload: unload runs on every options reload, and a
disable-then-re-arm cycle there would churn the producer's Tuya session per config change. The
inert stream *definition* left behind (`go2rtc_client` has no streams-delete) is accepted:
nothing dials an unpreloaded, unconsumed stream, and on the managed instance it dies with the
next HA restart anyway. **Downgrade is an accepted risk, documented not solved:** an older
version's `async_disable` sweeps only `_camera`, so a `_src` preload armed by this version
survives a downgrade — the release notes say to turn `keep_stream_running` off before
downgrading. The transcode-pinning question the first design carried is dissolved by the stream
split: `_src` has only the ws source, so its preload consumer has nothing else to start —
`_src_aac` is never preloaded and its ffmpeg runs only while a recording/HLS consumer exists.

## Failure modes and guards

- **Redial pressure.** go2rtc's ffmpeg sources redial eagerly during failures (`stream_server.py`,
  `COOLDOWN` note). `_src` carries none of its own any more — `_src_aac`'s ffmpeg dials `_src`'s
  RTSP, so its retries land on go2rtc, not the camera. The existing 25 s circuit breaker and
  6 s `ANSWER_TIMEOUT` remain the guard for the ws endpoint and must not be weakened; the soak
  test must include a failing-camera window with a recording active.
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
