# 0002 — Rebase timestamps in a dedicated live-view stream

**Status:** Accepted (2026-08-29)

## Context

This camera does not advance its video RTP clock. Measured on the live device:

| stream | video timestamps | audio timestamps |
|--------|------------------|------------------|
| `_src` (camera → go2rtc, what live view used) | `0.000000` on **every** packet | 0.02, 0.04, 0.06 … |
| `_src_aac` (ffmpeg hop → HLS) | +11 µs per frame (1 tick at 90 kHz) | normal |
| a Reolink camera through go2rtc (control) | 0.05, 0.10, 0.15 … | — |

Audio advancing correctly on the *same* stream, through the *same* hop, rules out go2rtc and rules
out the measurement. The camera is the source of the fault, permanently — not per session, not
per boot.

A player needs an advancing clock to schedule frames. Fed `_src` directly, browsers rendered video
erratically while audio stayed smooth: the exact field report ("sound is smooth, video is very
flaky").

Recordings and HLS were already immune, but for a reason that does not extend to live view: Home
Assistant's stream component stamps arrival times itself when the camera entity sets
`use_wallclock_as_timestamps` (added 2026-08-11). **That code is only on the recording path.** Live
view runs camera → go2rtc → browser and never enters it, which is why the earlier fix looked
complete and was not. The original Go bridge rebased timestamps in its own media path for this
same reason — evidence that sat in this repository, unread, for weeks.

Nothing we own touches the media: our signalling server negotiates a session and then media flows
directly to go2rtc. There is no place in Python to repair the clock.

## Decision

**Live view negotiates against a third go2rtc stream, `philips_avent_<id>_src_live`, whose only
source is `ffmpeg:<_src>#video=copy#audio=copy#async`.**

- `#async` is go2rtc's own flag for `-use_wallclock_as_timestamps 1 -async 1`
  (`internal/ffmpeg/ffmpeg.go`) — the repair, expressed in go2rtc's vocabulary rather than a
  hand-rolled input template.
- **Both tracks are copied.** PCMU is a codec browsers decode natively, so no transcode happens;
  the hop exists solely to rebase the clock. Video is copied too — no re-encoding anywhere.
- `whep_answer` tries `_src_live` first and falls back to `_src`. The fallback is exactly the
  behaviour that existed before this decision, so the worst case of the new hop is the live view
  we already had, never a dead player.
- Session cost is unchanged: `_src_live` consumes `_src`'s RTSP, exactly as `_src_aac` does, so the
  camera still sees one Tuya session.

## Consequences

- **Live view is smooth** (confirmed by the owner the same evening).
- **Costs:** one more ffmpeg process while someone is watching, a slightly slower start, and a
  fraction of a second more latency. For a baby monitor, a smooth picture a beat later beats a
  stuttering picture now.
- Three derived streams now exist per camera (`_src`, `_src_aac`, `_src_live`). Registration,
  preload sweeps, and name-distinctness asserts must all keep covering the set — the asserts in
  `const.py` are what make a missed one loud instead of silent.
- Timestamps become arrival times, so network jitter becomes timestamp jitter. Over a loopback
  restream that is negligible; it would not be over a WAN.
- If go2rtc ever grows a native Tuya producer with OEM credentials (the roadmap's upstream item),
  the rebasing belongs *there* and this stream disappears. That is the better fix; this is the one
  available without waiting for upstream.

## Evidence

- **Frozen clock, 2026-08-29:** 2601 video packets sampled from `_src` over two minutes carried a
  single distinct pts value (`0.000000`), while audio on the same stream advanced at 20 ms
  intervals and a control camera through go2rtc advanced normally.
- **The repair works, measured twice.** A manual ffmpeg hop with wallclock stamping produced 194
  distinct dts values and an exact duration for a 10 s capture. After deployment, probing
  `_src_live` through go2rtc gave 80 distinct advancing pts in 80 packets (0.052, 0.098, 0.150,
  0.205 …) with `h264` + `pcm_mulaw` intact.
- **Same root cause as the 2026-08-11 recording wedge:** a 54-minute, 1.1 GB recording whose MP4
  claimed 0.9 s duration — the +11 µs/frame collapse, inherited by the ffmpeg hop from the frozen
  input. That incident was originally misattributed to a go2rtc attach race; the spec carries the
  correction.

## Related

- `docs/superpowers/specs/2026-08-09-go2rtc-rtsp-restream-design.md` — the stream topology and the
  corrected timestamp section.
- `custom_components/philips_avent/const.py` — `go2rtc_live_name`, where the name and the reason
  live together.
- [ADR 0001](0001-never-reconfigure-the-shared-camera.md) — the companion decision: fix our own
  side rather than command the shared device.
