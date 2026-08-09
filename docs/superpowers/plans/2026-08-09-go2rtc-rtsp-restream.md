# go2rtc RTSP Restream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the builtin backend, register each camera's WebRTC producer as go2rtc stream `philips_avent_<id>_src` (with an AAC transcode source) and return go2rtc's RTSP URL from `stream_source()`, so HLS, `camera.record` and casting work — with sound — while live view stays single-hop via a WHEP override.

**Architecture:** A new `restream.py` module owns the go2rtc plumbing: a pure `parse_rtsp_endpoint()` decision function, a probed-and-cached RTSP endpoint, and an idempotent check-then-PUT registrar (the `preload.py` lock idiom). `camera.py`'s `stream_source()` delegates to it and falls back to the `webrtc:` URL on any failure. `preload.py` retargets `keep_stream_running` at the producer name. Everything degrades to today's behaviour; live view is never traded for HLS.

**Tech Stack:** Python 3.11+, `go2rtc_client` (via `hass.data["go2rtc"]`'s session — the managed instance's API is unix-socket + local_auth), pytest with the repo's FakeHass/fake-client pattern.

**Spec reference:** `docs/superpowers/specs/2026-08-09-go2rtc-rtsp-restream-design.md`

**Non-negotiable constraints:** Never break live view to gain HLS. `PUT /api/streams` only when the stream is absent or its sources differ (a blind PUT drops live producers). `FrameCache` and `use_stream_for_stills` are not touched. Do not start implementation before the ROADMAP soak test of the builtin backend has passed.

---

## File Structure

**New files:**
- `custom_components/philips_avent/restream.py` — endpoint probe, producer registrar, stream-URL decision
- `tests/test_philips_avent/test_restream.py` — unit tests for all of the above

**Modified files:**
- `custom_components/philips_avent/const.py` — `go2rtc_producer_name()`, `builtin_stream_url()` moves here
- `custom_components/philips_avent/preload.py` — extract `go2rtc_rest_client(hass)` helper; retarget to `_src`; disable sweeps both names
- `custom_components/philips_avent/camera.py` — `stream_source()` delegates to the restreamer; WHEP override
- `custom_components/philips_avent/__init__.py` — build the restreamer, entry-setup registration pass
- `custom_components/philips_avent/manifest.json` — `after_dependencies: ["go2rtc"]`
- `tests/test_philips_avent/test_sanitize.py` — producer-name and pure-URL tests (Tasks 1–2)
- `tests/test_philips_avent/test_preload.py` — names follow the retarget
- `README.md`, `ROADMAP.md`, `docs/superpowers/specs/2026-08-09-containerless-camera-design.md` — per spec "Docs to amend"

**Verification commands (every task):**

```bash
PYTHONPATH=. pytest tests/test_philips_avent/ -v
ruff check custom_components/ examples/ tools/ --ignore E501
```

---

## Task 1: `go2rtc_producer_name()` and the collision guard

**Files:**
- Modify: `custom_components/philips_avent/const.py`
- Modify: `tests/test_philips_avent/test_sanitize.py` (the existing go2rtc-naming tests live here)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_philips_avent/test_sanitize.py`:

```python
def test_producer_name_shape():
    assert go2rtc_producer_name("abc123") == "philips_avent_abc123_src"


def test_producer_name_never_collides_with_camera_stream_name():
    """If the two were ever equal, HA's provider would register the camera
    stream over our producer and its source would become its own restream —
    a self-consuming loop go2rtc does not guard against."""
    for cam_id in ("abc123", "weird id/☂", "", "_camera", "x_src"):
        assert go2rtc_producer_name(cam_id) != go2rtc_stream_name(cam_id)
```

- [ ] **Step 2: Run to verify failure** — `PYTHONPATH=. pytest tests/test_philips_avent/test_sanitize.py -v` → `ImportError: cannot import name 'go2rtc_producer_name'`.

- [ ] **Step 3: Implement**

In `const.py`, next to `go2rtc_stream_name`:

```python
def go2rtc_producer_name(cam_id: str) -> str:
    """The go2rtc stream that holds the camera's actual Tuya session.

    Registered by restream.py with the signaling ws URL as its source;
    everything else — HA's provider stream, HLS, recordings, frame grabs —
    consumes this stream's RTSP. The name MUST differ from
    go2rtc_stream_name() forever: HA's provider owns that name and would
    overwrite ours, turning the camera stream's source into its own
    restream (a loop). The suffixes `_src` vs `_camera` guarantee it; the
    assert guards refactors that touch either.
    """
    name = quote(f"{DOMAIN}_{cam_id}_src", safe=_GO2RTC_SAFE_CHARS)
    assert name != go2rtc_stream_name(cam_id)
    return name
```

- [ ] **Step 4: Tests pass; ruff clean**
- [ ] **Step 5: Commit** — `feat(builtin): name the go2rtc producer stream, and pin it apart from the camera's`

---

## Task 2: Move `builtin_stream_url()` to `const.py` as a pure function

`restream.py` needs the ws URL for registration and fallback, and importing `camera.py` (HA imports) would make it untestable with this repo's plain-pytest setup.

**Files:**
- Modify: `custom_components/philips_avent/const.py`, `camera.py`, `tests/test_philips_avent/test_sanitize.py`

- [ ] **Step 1: Failing test**

```python
def test_builtin_stream_url_is_pure():
    assert (
        builtin_stream_url(38555, "tok", "cam1")
        == "webrtc:ws://127.0.0.1:38555/avent/cam1?t=tok"
    )
```

- [ ] **Step 2: Implement** — move the function to `const.py` with signature `builtin_stream_url(port: int, token: str, cam_id: str) -> str`, and **rewrite its docstring**: it currently explains why a `webrtc:` scheme is fine for `stream_source()`, which stops being true in Task 6 — after that this URL is the producer's registered source and the last-resort fallback, and the docstring should say so. `camera.py`'s `async_setup_entry` builds the arguments from the entry exactly as today and passes the result on; no behaviour change yet.

- [ ] **Step 3: Full test suite passes; ruff clean**
- [ ] **Step 4: Commit** — `refactor(builtin): builtin_stream_url becomes a pure const.py helper`

---

## Task 3: Extract `go2rtc_rest_client(hass)` from `StreamPreloader`

**Files:**
- Modify: `custom_components/philips_avent/preload.py`
- Modify: `tests/test_philips_avent/test_preload.py`

- [ ] **Step 1: Failing test**

```python
def test_module_level_client_reads_the_same_slot(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    assert preload_mod.go2rtc_rest_client(hass) is not None
    hass.data.clear()
    assert preload_mod.go2rtc_rest_client(hass) is None
```

- [ ] **Step 2: Implement** — lift the body of `StreamPreloader._client` to a module-level `go2rtc_rest_client(hass) -> Go2RtcRestClient | None` (same docstring, same `_GO2RTC_DATA` note; the name deliberately does not shadow preload.py's `go2rtc_client` package import); the method becomes `return go2rtc_rest_client(self._hass)`. Pure refactor; the existing preload tests must pass unchanged.

- [ ] **Step 3: Tests pass; ruff clean**
- [ ] **Step 4: Commit** — `refactor(builtin): go2rtc client access shared beyond the preloader`

---

## Task 4: `parse_rtsp_endpoint()` — the degradation decision table

**Files:**
- Create: `custom_components/philips_avent/restream.py`
- Create: `tests/test_philips_avent/test_restream.py`

- [ ] **Step 1: Write the failing tests**

`test_restream.py` header mirrors `test_preload.py` (module docstring naming the guarded rules; plain-import `restream` like `preload`). Tests:

```python
import restream as restream_mod
from restream import parse_rtsp_endpoint


def test_managed_instance_loopback_rtsp():
    assert parse_rtsp_endpoint("127.0.0.1:18554", "localhost") == ("127.0.0.1", 18554)


def test_all_interfaces_on_local_api_host():
    assert parse_rtsp_endpoint(":8554", "127.0.0.1") == ("127.0.0.1", 8554)


def test_all_interfaces_on_remote_api_host():
    assert parse_rtsp_endpoint(":8554", "192.168.1.2") == ("192.168.1.2", 8554)


def test_remote_host_with_loopback_rtsp_is_unusable():
    # go2rtc could dial itself, but HA's PyAV could not reach it — and
    # stream_source() feeds both, so this must degrade to the ws URL.
    assert parse_rtsp_endpoint("127.0.0.1:8554", "192.168.1.2") is None


def test_bracketed_ipv6_loopback_is_loopback():
    # rpartition(":") leaves the host as "[::1]", which must not fall
    # through to the treat-as-remote branch.
    assert parse_rtsp_endpoint("[::1]:8554", "localhost") == ("127.0.0.1", 8554)
    assert parse_rtsp_endpoint("[::1]:8554", "192.168.1.2") is None


def test_absent_empty_or_garbage_listen():
    for listen in (None, "", ":", "nonsense", ":notaport"):
        assert parse_rtsp_endpoint(listen, "localhost") is None
```

- [ ] **Step 2: Verify failure** — `ModuleNotFoundError: restream`.

- [ ] **Step 3: Implement**

`restream.py` opens with a module docstring in the house register: why the RTSP endpoint must be probed (managed = `127.0.0.1:18554`, a literal in HA's config template; externals differ), why every request rides `hass.data["go2rtc"]`'s session (unix socket + per-boot local_auth), and the never-break-live-view rule. Then:

```python
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
_ALL_INTERFACES = ("", "0.0.0.0", "::", "[::]")

#: Where HA's config template pins the managed instance's RTSP listener.
#: Used only as the PROVISIONAL endpoint when the /api probe fails
#: transiently on a loopback API host — never cached, re-probed next call,
#: version-coupled to HA's template and the log says so. The point is that
#: stream_source()'s result is frozen into HA's cached Stream, so a
#: transient failure must still yield the stable RTSP URL (see spec).
MANAGED_RTSP = ("127.0.0.1", 18554)


def parse_rtsp_endpoint(listen: str | None, api_host: str) -> tuple[str, int] | None:
    """(host, port) HA's stream worker can dial, or None to fall back."""
    if not listen:
        return None
    host, _, port_text = listen.rpartition(":")
    try:
        port = int(port_text)
    except ValueError:
        return None
    api_local = api_host in _LOOPBACK_HOSTS
    if host in _ALL_INTERFACES:
        return ("127.0.0.1", port) if api_local else (api_host, port)
    if host in _LOOPBACK_HOSTS:
        return ("127.0.0.1", port) if api_local else None
    return (host, port)
```

- [ ] **Step 4: Tests pass; ruff clean**
- [ ] **Step 5: Commit** — `feat(builtin): decide where (and whether) go2rtc's RTSP is reachable`

---

## Task 5: `Restreamer` — probe, register, decide the stream URL

The heart of the change. One class per entry, constructed with `(hass, ws_url: Callable[[str], str])` where `ws_url = partial(builtin_stream_url, port, token)`.

**Files:**
- Modify: `custom_components/philips_avent/restream.py`
- Modify: `tests/test_philips_avent/test_restream.py`

- [ ] **Step 1: Write the failing tests**

Extend the fakes from `test_preload.py`'s pattern: `FakeStreamsAPI` gains `add(name, sources)` recording `("streams.add", name, tuple(sources))` and updating `state.streams[name]` to producer objects with `.url`; a fake `session.get` serves `{"rtsp": {"listen": ...}}` (configurable, failable). Tests:

```python
WS = "webrtc:ws://127.0.0.1:38555/avent/cam1?t=tok"
SRC = "philips_avent_cam1_src"
WANT = (WS, f"ffmpeg:{SRC}#audio=aac")


def test_rtsp_url_when_probe_and_registration_succeed(...):
    # stream_url("cam1") == f"rtsp://127.0.0.1:18554/{SRC}?video&audio=aac"
    # and exactly one streams.add with WANT


def test_ws_fallback_when_no_go2rtc(...):
    # hass.data empty -> stream_url returns WS, warns once; the slot is
    # rechecked on the next call (not cached)


def test_ws_fallback_when_rtsp_permanently_unusable(...):
    # probe SUCCEEDS with listen="" -> WS, "HLS unavailable" logged once,
    # verdict cached. Only a config-shaped verdict may pick the ws URL:
    # HA's Camera.async_create_stream calls stream_source() at most once
    # per entity lifetime and freezes the result into its cached Stream.


def test_transient_probe_failure_still_returns_rtsp_url(...):
    # session.get raises TimeoutError, api host loopback -> MANAGED_RTSP
    # provisional endpoint, RTSP URL returned, nothing cached. A ws URL
    # here would freeze a dead fallback into the Stream until entry reload.


def test_transient_probe_failure_on_remote_host_returns_ws(...):
    # cannot guess a remote endpoint; remote go2rtc is unsupported anyway


def test_no_put_when_already_registered_with_matching_sources(...):
    # state.streams pre-seeded with WANT -> stream_url twice, zero streams.add


def test_reput_when_sources_differ(...):
    # pre-seeded with a stale token in the ws URL -> exactly one streams.add


def test_reregisters_after_go2rtc_restart(...):
    # stream_url once; state.streams.clear() (watchdog respawn);
    # stream_url again -> a second streams.add. The callers that make this
    # self-healing are the provider's _update_stream_source (per frame
    # grab), the WHEP override (per live-view open) and the setup pass —
    # NOT the stream component, which never re-calls stream_source();
    # on_answered can never fire while _src is unregistered.


def test_concurrent_calls_register_once(...):
    # gather 5x stream_url -> one streams.add (the lock)


def test_registration_failure_still_returns_rtsp_url(...):
    # streams.add raises TimeoutError -> RTSP URL returned anyway, nothing
    # raised, complaint logged once. The URL is stable and HA's stream
    # worker retries its source; the stream starts working the moment a
    # later call registers _src.


def test_probe_success_is_cached_transient_failure_is_not(...):
    # success: second stream_url does no second GET /api
    # timeout: provisional endpoint this call, a real re-probe on the next
```

- [ ] **Step 2: Verify failure**

- [ ] **Step 3: Implement**

Skeleton (idioms from `preload.py`: lock around check-then-PUT, `_complain_once`, `describe_error`, `# noqa: BLE001` on the boundary):

```python
class Restreamer:
    """Registers each camera's producer in go2rtc and hands out its RTSP URL."""

    def __init__(self, hass, ws_url):
        self._hass = hass
        self._ws_url = ws_url
        self._endpoint: tuple[str, int] | None = None  # cached on success only
        self._lock = asyncio.Lock()
        self._warned = False

    async def stream_url(self, cam_id: str) -> str:
        """The producer's RTSP URL, or the webrtc: fallback — but the
        fallback ONLY for permanent, config-shaped verdicts (no go2rtc,
        RTSP disabled, remote-loopback). HA's stream component calls
        stream_source() at most once per entity lifetime and freezes the
        result, so a transient failure must still return the stable RTSP
        URL: the stream worker retries its source, and re-registration
        arrives via the provider's frame grabs, the WHEP override and the
        setup pass."""
        endpoint = await self._rtsp_endpoint()  # None only for permanent verdicts
        if endpoint is None:
            return self._ws_url(cam_id)
        await self._ensure_registered(cam_id)  # best effort; the URL is stable either way
        host, port = endpoint
        return f"rtsp://{host}:{port}/{go2rtc_producer_name(cam_id)}?video&audio=aac"

    def sources(self, cam_id: str) -> list[str]:
        name = go2rtc_producer_name(cam_id)
        return [self._ws_url(cam_id), f"ffmpeg:{name}#audio=aac"]

    # go2rtc access throughout via preload's go2rtc_rest_client(hass)

    async def _ensure_registered(self, cam_id: str) -> bool:
        """Check-then-PUT under the lock. PUT /api/streams silently replaces
        a live stream without stopping its producers, so a blind PUT here is
        the same Tuya-session feedback loop preload.py refuses."""
        ...
```

`_rtsp_endpoint()` GETs `{url}/api` on the shared session — bare `/api` is on the managed
instance's `allow_paths` whitelist (HA dev `server.py`), so the probe needs no permission plan B —
and feeds `parse_rtsp_endpoint`. A successful probe caches its verdict (endpoint or
permanent-no-RTSP) for the entry's lifetime; a failed *request* is transient: return
`MANAGED_RTSP` provisionally when the API host is loopback, `None` when remote, cache nothing.
`_ensure_registered` compares `[p.url for p in streams[name].producers]` against
`self.sources(cam_id)` and PUTs on mismatch. Log names, never sources (the src embeds the token).

- [ ] **Step 4: Tests pass; ruff clean**
- [ ] **Step 5: Commit** — `feat(builtin): register the producer in go2rtc and serve its RTSP from stream_source`

---

## Task 6: Wire it — `camera.py`, `__init__.py`, `manifest.json`

**Files:**
- Modify: `custom_components/philips_avent/camera.py`, `__init__.py`, `manifest.json`

- [ ] **Step 1: `camera.py`** — `AventCamera` takes an optional `restreamer`; `stream_source()` becomes:

```python
    async def stream_source(self) -> str:
        if self._restreamer is not None:
            return await self._restreamer.stream_url(self._cam_id)
        return self._stream_url
```

Add-on cameras keep the static URL path (`restreamer=None`). `FrameCache` and
`use_stream_for_stills` are untouched — verify by diff, not memory.

- [ ] **Step 2: `__init__.py`** — for builtin entries build one `Restreamer` and park it in the
entry's `hass.data` dict for `camera.py`. The `ws_url` partial is built from **`server.port` and
the token, not the entry's port option**: `_async_start_streaming` keeps the first entry's server
and only warns when a second entry asks for a different port, so an option-built URL on that
second entry would register a `_src` pointing at nothing (pre-existing bug shape — `camera.py:45`
does the same today; this fixes it for builtin cameras).

- [ ] **Step 3: one serialized bookkeeping task** — after `async_forward_entry_setups`, replace
the separate preload-resume background task with a single task that first awaits
`restreamer.stream_url(cam_id)` per camera (the cold-start registration pass, best-effort —
failures already degrade inside) and **then** runs `preloader.async_resume`. Two parallel tasks
would let the resume's `streams.list()` race the registration and miss the stream it should arm.

- [ ] **Step 4: `async_remove_entry`** — best-effort `StreamPreloader(hass).async_disable(...)`
for the entry's cameras (which, after Task 7, sweeps both names). Removal only, **not**
`async_unload_entry`: unload runs on every options reload, and disable-then-re-arm there would
churn the producer's Tuya session per config change. The inert stream definition left in an
external go2rtc is accepted and documented in the spec (`go2rtc_client` has no streams-delete;
nothing dials an unpreloaded, unconsumed stream).

- [ ] **Step 5: `manifest.json`** — add `"after_dependencies": ["go2rtc"]` so the setup-time pass stops racing go2rtc's startup. (Soft ordering only; absence still degrades cleanly.)

- [ ] **Step 6: Full suite passes; ruff clean**
- [ ] **Step 7: Commit** — `feat(builtin): cameras stream through go2rtc's RTSP restream`

---

## Task 7: Retarget `keep_stream_running` at the producer

**Files:**
- Modify: `custom_components/philips_avent/preload.py`
- Modify: `custom_components/philips_avent/const.py` (stale comments)
- Modify: `tests/test_philips_avent/test_preload.py`

- [ ] **Step 1: Failing tests** — flip `NAME` to `"philips_avent_cam1_src"` and add:

```python
def test_disable_sweeps_both_names(monkeypatch):
    """An upgrade may leave a _camera preload armed by an older version;
    disabling must stop it too, or go2rtc streams for nobody."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.preloads["philips_avent_cam1_camera"] = {}
    state.preloads["philips_avent_cam1_src"] = {}
    run(StreamPreloader(hass).async_disable(["cam1"]))
    assert state.preloads == {}
```

- [ ] **Step 2: Implement** — `_async_enable`/`async_resume` use `go2rtc_producer_name`;
`async_disable` checks both `go2rtc_producer_name` and `go2rtc_stream_name` per camera. Update the
module docstring: the armed stream is now the producer; HA's provider no longer disarms it behind
our back (that paragraph shrinks to a historical note); `async_resume` finds the stream at setup
**because the same serialized task registers it first** (Task 6 step 3), not by side effect.
Leave `on_answered` re-arm wiring as is — go2rtc restarts still eat preloads, and the answer
remains the earliest signal.

- [ ] **Step 2b: sweep `const.py`'s stale comments** — the `CONF_KEEP_STREAM_RUNNING` block still
says HA's `preload_stream` preference "would feed our webrtc: URL to ffmpeg/HLS"; once
`stream_source()` is RTSP that reason is wrong (the surviving reasons: it is another
integration's user preference, and it would arm the wrong stream). Rewrite it here, where the
behaviour changes.

- [ ] **Step 3: Tests pass; ruff clean**
- [ ] **Step 4: Commit** — `feat(builtin): keep_stream_running holds the producer stream itself`

---

## Task 8: Single-hop live view — the WHEP override (separately revertible)

Without this, live view is a double hop and its audio a PCMU→AAC→opus double transcode (the
`_camera` source is the AAC-filtered RTSP). Frigate's integration is the precedent. One commit;
revert restores the provider path with zero collateral.

**Files:**
- Modify: `custom_components/philips_avent/restream.py`, `camera.py`
- Modify: `tests/test_philips_avent/test_restream.py`

- [ ] **Step 1: Failing tests** — `Restreamer.whep_answer(cam_id, offer_sdp)`: calls the fake
client's `webrtc.forward_whep_sdp_offer(source_name, offer)` with `source_name ==
go2rtc_producer_name(cam_id)` and an offer object carrying the SDP (the fake mirrors the real
model-based signature: `WebRTCSdpOffer` in, `WebRTCSdpAnswer` out), after ensuring registration;
returns the answer's `.sdp` string, or `None` (never raises) when go2rtc is absent,
unregistered-and-unregistrable, or the WHEP call fails.

- [ ] **Step 2: Implement** — `whep_answer` uses
`go2rtc_rest_client(hass).webrtc.forward_whep_sdp_offer(name, WebRTCSdpOffer(offer_sdp))` and
returns `answer.sdp` (WHEP: complete answer, no trickle). In `camera.py`, the offer handler plus
the teardown bookkeeping it obligates:

```python
    async def async_handle_async_webrtc_offer(self, offer_sdp, session_id, send_message):
        """Negotiate against the producer stream directly — one hop, native
        PCMU — as frigate-hass-integration does. Any failure falls back to
        HA's provider (double hop through the _camera stream): worse, never
        broken."""
        answer = None
        if self._restreamer is not None:
            answer = await self._restreamer.whep_answer(self._cam_id, offer_sdp)
        if answer is None:
            self._provider_sessions.add(session_id)
            return await super().async_handle_async_webrtc_offer(offer_sdp, session_id, send_message)
        send_message(WebRTCAnswer(answer))

    def close_webrtc_session(self, session_id: str) -> None:
        """HA's websocket handler calls this unconditionally on teardown,
        and the go2rtc provider pops the session with no default — a
        KeyError for any session it never negotiated. Delegate only the
        sessions we actually gave it; a WHEP session ends with its peer
        connection, so ours need nothing beyond the bookkeeping."""
        if session_id in self._provider_sessions:
            self._provider_sessions.discard(session_id)
            super().close_webrtc_session(session_id)
```

`self._provider_sessions: set[str]` initialised in `__init__`. `async_on_webrtc_candidate` needs
no override — the provider logs unknown-session candidates at debug and moves on (the safe half
of an asymmetric API).

- [ ] **Step 3: Tests pass; ruff clean**
- [ ] **Step 4: Commit** — `feat(builtin): live view negotiates the producer stream directly`

---

## Task 9: Docs

**Files:** `README.md`, `ROADMAP.md`, `docs/superpowers/specs/2026-08-09-containerless-camera-design.md`

- [ ] Per the spec's "Docs to amend" list: README backend table row (HLS/record/casting → "Yes,
  via go2rtc's RTSP") + the 8 kHz honesty sentence + the local-RTSP-readable sentence; ROADMAP
  "Known limits" drops the HLS bullet; the containerless spec's "Deliberately not supported" and
  loopback-only rationale are rewritten to point here. (Its "One consumer per camera" bullet
  stays — the ws-layer constraint is unchanged; fan-out happens above it.)
- [ ] Release-note line: turn `keep_stream_running` off before downgrading — an older version's
  `async_disable` sweeps only the `_camera` name and would strand a `_src` preload.
- [ ] Commit — `docs: HLS, recording and casting land on the builtin backend`

---

## Field verification (after the code, on hardware — not CI)

- [ ] **Acceptance:** `camera.record` produces a clip that plays back **with sound** while live
  view stays up, on one Tuya session, vendor app still connecting.
- [ ] **Probe sanity:** confirm on a stock install that the `/api` probe reports
  `rtsp.listen: "127.0.0.1:18554"` (bare `/api` is whitelisted per HA dev `server.py`; this just
  confirms it holds on the shipping release) and that the `MANAGED_RTSP` provisional path stays
  cold in normal operation.
- [ ] **Open question — preload vs the transcode:** with `keep_stream_running` on and nobody
  watching, check for a resident ffmpeg. If present, add `audio_codec_filter` to
  `preload.enable()` restricting the preload to native codecs.
- [ ] **Open question — transcode start semantics:** confirm the ffmpeg AAC source starts only
  when an AAC consumer attaches (recording/HLS), not on live view.
- [ ] **Redial pressure:** unplug the camera with a recording active; confirm the circuit breaker
  holds and the vendor app reconnects afterwards.
- [ ] Watch for camera-entity availability flapping during cooldown windows (new semantics).
