# SenseIQ and the wider Tuya API surface

What the Tuya cloud API and DPS space actually offer for the SCD9xx that the integration does not
use yet, and what is worth building. Written 2026-08-09 against a live SCD953 (firmware `1.4.0`,
`productId 7d9…nww`) with a bounded set of read-only cloud calls: `smartlife.m.user.info.get`,
`m.life.home.space.list`, `tuya.m.device.get`, and one `smartlife.m.rtc.config.get`. No media
session, MQTT, or LAN call was made. Verified facts are marked; guesses are labelled *inferred*.

## Headline: SenseIQ is Tuya data, carried as DPS

**Verified.** SenseIQ is not a separate Philips backend. It is a block of Tuya data points on the
low DPS range (ids 1–21), fully visible in `tuya.m.device.get`'s `schema` and `dps`, and — like
temperature — pushable over the LAN protocol. The device schema names them in plain English:

| DPS | code | name | type | what it is | HA entity? |
|----:|------|------|------|-----------|------------|
| 1 | `sleepiq_switch` | SenseIQ on/off | bool rw | master enable | switch |
| 2 | `cry_trans_switch` | Cry translation on/off | bool rw | enables the paid cry-AI | switch |
| 3 | `sleepiq_status` | SenseIQ status | string ro | **live sleep/awake signal**, e.g. `{"r":"b","br":33}` | sensor |
| 4 | `sleep_session_data` | Sleep session data | raw ro | current session: `{"st":<start>,"sd":<dur s>,"css":"d","cssd":<state dur>,"ssd":[{"l":302}]}` | sensor + attrs |
| 5 | `sleepiq_consent` | SenseIQ consent | bool rw | GDPR consent flag | no |
| 6 | `senseiq_diagnostics` | diagnostics | raw ro | opaque | no |
| 7 | `sensiq_diag_consent` | diag consent | bool rw | — | no |
| 8 | `awake_delay` | Baby awake time delay | value rw 0–3600 | grace before an awake alert (=180 s) | number |
| 9 | `cry_trans_result` | Cry translation result | enum ro 0–8 | 9-way cry reason (paid) | sensor (paid) |
| 10 | `sleepiq_area` | SenseIQ area | string rw | detection ROI JSON | no |
| 11 | `awake_switch` | Baby awake alert on/off | bool rw | — | switch |
| 12 | `cry_det_switch` | Cry alert on/off | bool rw | — | switch |
| 13 | `no_senseiq_switch` | No-signal alert on/off | bool rw | — | switch |
| 14 | `cry_trans_subscr` | Cry trans subscription | string rw | `{"days_left":46,"status":"active","type":"f"}` (free trial) | sensor (diag) |
| 15 | `no_senseiq_signal` | No SenseIQ signal | bool ro | baby not currently sensed | binary_sensor |
| 16 | `refurbish_counter` | Refurbishment counter | value ro | — | no |
| 17 | `cry_trans_token` | Cry translation token | raw rw | cloud auth for cry-AI | no |
| 18 | `device_errors` | Errors | bitmap ro | fault bitmap | binary_sensor (problem, diag) |
| 19 / 20 | `bu_logs` / `pu_logs` | base/parent-unit logs | raw ro | e.g. `{"type":"security","talkback_src":"app"}` | no |
| 21 | `ext_functions` | Extended functions | value rw | feature bitmask | no |

**The core SenseIQ signal is DPS 3 and DPS 4.** DPS 3 is the instantaneous state; DPS 4 is the
current sleep session (start time, running duration, current sleep state `css` and a per-state
timeline `ssd`). `css:"d"` and the `{"r":"b",…}` shape are undecoded — *inferred* to be sleep-stage
codes; the exact vocabulary needs a few hours of watching the values change against known baby
state. Cry translation (DPS 2/9/14/17) is the one genuinely cloud-backed, subscription-gated piece:
it ships audio to `aispeech.tuyaeu.com` (seen in the user-info domain map) and is a free trial here.

## What we already use vs. what is there

Already used by the integration: `tuya.m.device.get` (state), `tuya.m.device.dp.publish` (writes),
`tuya.m.device.upgrade.rssi.info.query` (RSSI), `smartlife.m.user.info.get`,
`m.life.home.space.list`, `smartlife.m.rtc.config.get`, plus the four discovery fallbacks in
`api.py`. Notable unused things the same calls already return:

- **The whole SenseIQ block above** — pure DPS, no new endpoint needed.
- **Firmware / OTA** — `verSw` (`1.4.0`) in `device.get`, plus DPS 206 `OTA_message`. Could back a
  diagnostic sensor or a HA `update` entity. *Inferred* update-check endpoint exists
  (`tuya.m.device.upgrade.*`, sibling of the RSSI call we already use).
- **Event snapshots** — DPS 212 (`initiative_message`, not "alarm_record") carries a JSON pointer to
  the JPEG the camera uploaded: bucket `ty-eu-storage30`, a file path and a per-file key. `events.py`
  already parses this. Turning the pointer into an image needs an IPC storage/signed-URL call
  (*inferred* `tuya.m.ipc.*`); worth it for a real event thumbnail.
- **Cloud storage / recording** — skill reports `cloudStorage:3`, `doorbellStorage:1`,
  `supportWebrtcRecord:true`. Timeline/clip retrieval exists but is subscription-gated and needs a
  media session, so it is outside the probing fence and out of scope here.
- **No humidity.** There is no humidity DPS on this hardware — climate sensing is temperature only
  (DPS 207/208, already surfaced). Worth stating plainly so nobody hunts for it.

Minor corrections to `examples/DPS_REFERENCE.md`: DPS 209 `play_volume` min is **44**, not 1; DPS 212
code is `initiative_message`; DPS 4 sub-stream is **1280×720**, not 640×360.

## History vs. current state

**Verified:** the DPS only expose the *current* sleep session (DPS 4) and *current* state (DPS 3) —
one live session, no multi-day archive. The multi-night charts in the Baby Monitor+ app are
*inferred* to come either from an app-side store or a Tuya statistics endpoint
(`tuya.m.*.statistics.*` / device log APIs exist on the platform but were **not** probed — no
endpoint-name guessing under the fence). The honest recommendation: **do not chase a proprietary
stats API.** Expose DPS 3/4 as live sensors and let Home Assistant's own recorder build the trend —
that is what HA history is for, and it needs no reverse engineering.

## The 720p question

**Mechanism found; one experiment left to run.** The skill blob confirms the camera can do 1080p:

```
webrtc:115  (= speaker|clarity|record bits all set; clarity = bit5 IS on)
videos: [ {streamType:2, codecType:2(H264), 1920x1080},
          {streamType:4, codecType:2(H264), 1280x720} ]
vedioClaritys:[2,4,8]
```

So the hardware, codec (H.264, not HEVC) and the clarity capability are all present. What we send is
wrong. In `signaling.py`, `send_offer` sets the offer's `stream_type` to `_stream_type(skill)`,
which returns the **raw skill `streamType`** — `2` for the 1080p stream. But the offer's
`stream_type` field is a **mapped enum**, not the skill value: `0 = main stream (HD)`,
`1 = sub stream (SD)`. go2rtc proves this — its `SendOffer` maps skill `2→0` and `4→1` before
sending (`pkg/tuya/mqtt.go`). We send a literal `2`, which is out of range, so the camera falls
back to its default sub-stream: 720p. The Go bridge had the same bug for H.264 (it only remapped on
the HEVC path), and the Python port carried it over "verbatim."

**Ruled out:** HEVC/datachannel path (camera is H.264); the 312 resolution message value (we already
send `cmdValue:0` = HD, 1.5 s after the answer — correct value, and go2rtc uses the same `0`); clarity
capability (bit 5 is set). The offer's `stream_type` is the one thing still wrong.

**Already tried once, inconclusively (2026-08-09, prototype).** The mapped value was sent —
`stream_type: 0` for hd, with the same post-answer `send_resolution(0)` — and an 8-second capture
still probed as 1280x720. That result is *not* decisive: `ffprobe` reports the dimensions the
container was opened with, so a camera that switches to the main stream a second or two in would
still show 720p in that clip. Whoever runs this next should capture for 30 s or more and check
frame dimensions over time (or read the answered SDP), rather than trusting a short clip's header.

**Next experiment (one sitting, one file):** in `signaling.py`, map the skill `streamType` to the
offer enum before sending — `2→0`, `4→1` — exactly as `go2rtc/pkg/tuya/mqtt.go:SendOffer` does, so
the offer carries `stream_type:0`. Keep the post-answer `send_resolution(0)`. Dial the camera and
check the answered video resolution. If it already answers 1080p, the 312 message becomes belt-and-
braces; if it still holds 720p, the fallback candidate is the `send_resolution` timing (go2rtc fires
it on `PeerConnectionStateConnected`, we fire it on a 1.5 s timer after the answer).

## Priorities

1. **720p offer fix** — one-line mapping, biggest user-visible win, README already claims 1080p.
2. **Sleep state sensors (DPS 3, 4) + no-signal binary sensor (DPS 15)** — the actual point of
   SenseIQ, all live DPS already flowing through the coordinator; HA recorder gives the history.
3. **SenseIQ control switches (DPS 1, 11, 12, 13) and `awake_delay` number (DPS 8)** — cheap, they
   are ordinary rw DPS just like the ones already mapped.
4. **Firmware/OTA diagnostic + `device_errors` problem sensor (DPS 18, `verSw`)** — low effort,
   diagnostic value.
5. **Event snapshot image from DPS 212** — worth it but needs an unverified IPC storage call; do it
   after 1–4.
6. **Not worth it:** cry-translation result (DPS 9) is subscription-gated cloud AI with a 9-way
   opaque enum; cloud recording/clip timelines need a media session and a subscription; a
   reconstructed sleep-score model; any proprietary long-term statistics endpoint.
