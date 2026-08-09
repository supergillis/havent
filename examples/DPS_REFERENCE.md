# Camera DPS (Data Points) Reference

DPS are Tuya's mechanism for device control. Each data point has an ID, a code name,
a type, and read/write permissions. Values are read via `tuya.m.device.get` and
written via `tuya.m.device.dp.publish`.

## Reading DPS

```python
device = client.get_device("YOUR_DEVICE_ID")
dps = device["dps"]
temperature = dps["207"]  # raw value, divide by 100 for °C
```

## Writing DPS

```python
client.set_dps("YOUR_DEVICE_ID", {"138": True})   # night light on
client.set_dps("YOUR_DEVICE_ID", {"158": 50})      # brightness 50%
client.set_dps("YOUR_DEVICE_ID", {"201": "play"})   # play lullaby
```

## Complete DPS Map

### SenseIQ Sleep Tracking (DPS 1–21)

SenseIQ is not a separate Philips service: it is this block of ordinary Tuya
data points on the low id range, delivered through the same `tuya.m.device.get`
poll and LAN push as everything else. Schema read from a live SCD953
(firmware 1.4.0, 2026-08-09):

| ID | Code | Name | Type | Mode | Values/Range |
|----|------|------|------|------|-------------|
| 1 | `sleepiq_switch` | SenseIQ on/off | bool | rw | true/false |
| 2 | `cry_trans_switch` | Cry translation on/off | bool | rw | true/false (subscription feature) |
| 3 | `sleepiq_status` | Live SenseIQ status | string | ro | JSON, e.g. `{"r":"b","br":29}` — `br` = breathing rate in breaths/min (**confirmed**); `r` = live sensing status, `b`=breathing (**documented**), NOT the sleep stage |
| 4 | `sleep_session_data` | Current sleep session | raw | ro | base64(hex(JSON)): `{"st":<epoch>,"sd":<s>,"css":"d","cssd":<s>,"ssd":[{"l":302}]}`; `sd = Σssd + cssd`; `css` = sleep state |
| 5 | `sleepiq_consent` | SenseIQ consent | bool | rw | GDPR consent flag |
| 6 | `senseiq_diagnostics` | SenseIQ diagnostics | raw | ro | opaque |
| 7 | `sensiq_diag_consent` | Diagnostics consent | bool | rw | true/false |
| 8 | `awake_delay` | Baby awake time delay | value | rw | 0–3600 s |
| 9 | `cry_trans_result` | Cry translation result | enum | ro | `0`–`8` (subscription feature) |
| 10 | `sleepiq_area` | Detection area | string | rw | JSON: `{"num":1,"region0":{...}}` |
| 11 | `awake_switch` | Baby awake alert on/off | bool | rw | true/false |
| 12 | `cry_det_switch` | Cry alert on/off | bool | rw | true/false |
| 13 | `no_senseiq_switch` | No-signal alert on/off | bool | rw | true/false |
| 14 | `cry_trans_subscr` | Cry translation subscription | string | rw | JSON with `days_left`, `status`, `type` |
| 15 | `no_senseiq_signal` | No SenseIQ signal | bool | ro | no-signal indicator (pairs with DP 13 alert). Observed `true` on a healthy monitor; polarity not confirmed by any public source — do not surface as a `problem` sensor yet |
| 16 | `refurbish_counter` | Refurbishment counter | value | ro | 0–1000 |
| 17 | `cry_trans_token` | Cry translation token | raw | rw | cloud auth blob |
| 18 | `device_errors` | Errors | bitmap | ro | fault bitmap |
| 19 | `bu_logs` | Base unit logs | raw | ro | base64(hex(JSON)) |
| 20 | `pu_logs` | Parent unit logs | raw | ro | base64(hex(JSON)) |
| 21 | `ext_functions` | Extended functions | value | rw | feature bitmask |

The SenseIQ vocabulary, as far as paired observations against the Philips app
have established it (live SCD953, 2026-08-09) — the labels matter, because they
are the difference between evidence and a guess:

| Field | Value | Meaning | Status |
|-------|-------|---------|--------|
| DPS 3 `br` | number | breathing rate, breaths per minute (27, 28, 30, 33 all matched the app) | **confirmed** |
| DPS 4 `css` | `d` | deep sleep (app showed "deep sleep" at the same moment) | **confirmed** |
| DPS 4 `css` / `ssd` keys | `l` | light sleep — the `ssd` segments alternate `l`/`d` exactly as sleep cycles do | *inferred* |
| DPS 4 `css` awake | letter unknown | the app's third sleep stage is **active-awake**; its `css` code has never been seen here, so it stays unmapped | **documented, not observed** |
| DPS 3 `r` | `"b"` | **live sensing status, not the sleep stage.** APK strings enumerate the set as *moving / breathing / no-signal / out-of-crib / analyzing*; `b` = breathing. It held `b` for an hour because the baby breathed throughout while the DPS 4 `css` stage cycled | **documented** |

The integration translates only the pinned codes: DPS 4 `css` `d` → deep and
`l` → light, DPS 3 `r` `b` → breathing; anything else — including the awake
stage code and the four unobserved status letters — reads unknown, with the
raw letter kept on the entity's attributes as evidence. DP 11/12/13 are plain
alert switches, and DP 15 is relayed as a raw diagnostic flag with no problem
semantics until its polarity is proven. The `r`
status set, the three sleep stages (active-awake / light / deep) and DP 13/15's
roles are documented in a public static RE of the same Philips Baby Monitor+ APK
(github.com/eisbaw/babymonitor-client) and Philips' support pages; the exact
`css`/`r` letter encodings are not — only the human labels — so the awake letter
is still unproven.

### Video & Image

| ID | Code | Name | Type | Mode | Values/Range |
|----|------|------|------|------|-------------|
| 101 | `basic_indicator` | LED status | bool | rw | true/false |
| 102 | `ipc_flip` | Image rotation | enum | rw | `flip_none`, `flip_horizontal_mirror`, `flip_vertical_mirror`, `flip_rotate_180` |
| 237 | `privacy_switch` | Privacy mode (camera off) | enum | rw | `0` (off), `1` (on) |

### Night Light

| ID | Code | Name | Type | Mode | Values/Range |
|----|------|------|------|------|-------------|
| 138 | `bulb_switch` | Night light on/off | bool | rw | true/false |
| 158 | `floodlight_lightness` | Brightness | value | rw | 1–100 (step 1) |
| 204 | `nightlight_color` | Color | string | rw | color string |
| 240 | `nightlight_timer` | Auto-off timer (seconds) | value | rw | 1–5400 |
| 241 | `light_timer_switch` | Timer enabled | bool | rw | true/false |
| 242 | `light_timer_display` | Timer remaining (seconds) | value | ro | -1–86400 |

### Lullabies

| ID | Code | Name | Type | Mode | Values/Range |
|----|------|------|------|------|-------------|
| 201 | `play_control` | Playback control | enum | rw | `play`, `pause`, `stop`, `next`, `prev` |
| 202 | `play` | Play specific track | string | rw | track identifier |
| 203 | `play_mode` | Loop mode | enum | rw | `loop`, `loop1`, `shuffle` |
| 209 | `play_volume` | Volume | value | rw | 44–100 (step 1) |
| 243 | `lullaby_timer_switch` | Timer enabled | bool | rw | true/false |
| 244 | `lullaby_timer` | Auto-stop timer (seconds) | value | rw | 0–5400 |
| 245 | `lullaby_display` | Timer remaining (seconds) | value | ro | -1–86400 |
| 246 | `play_state` | Current state | enum | rw | `playing`, `stopping` |
| 248 | `play_current` | Currently playing | string | rw | JSON: `{"bizcode":"phi-no-bm","id":3542155,"errcode":0}` |
| 249 | `voice_upgrade` | Custom recording update | string | rw | — |

### Temperature Sensor

| ID | Code | Name | Type | Mode | Values/Range |
|----|------|------|------|------|-------------|
| 207 | `sensor_temperature` | Temperature (°C × 100) | value | ro | 0–5000 (scale 2). Value 2250 = 22.50°C |
| 208 | `temp_report` | Temperature (°F × 100) | value | ro | 0–500 (scale 2) |
| 231 | `temp_max_switch` | High temp alert on | bool | rw | true/false |
| 232 | `temp_min_switch` | Low temp alert on | bool | rw | true/false |
| 233 | `temp_max_cvalue` | High temp threshold (°C × 100) | value | rw | 0–4000 (step 100) |
| 234 | `temp_min_cvalue` | Low temp threshold (°C × 100) | value | rw | 0–4000 (step 100) |
| 235 | `temp_max_fvalue` | High temp threshold (°F) | string | rw | — |
| 236 | `temp_min_fvalue` | Low temp threshold (°F) | string | rw | — |

### Motion & Sound Detection

| ID | Code | Name | Type | Mode | Values/Range |
|----|------|------|------|------|-------------|
| 106 | `motion_sensitivity` | Motion sensitivity | enum | rw | `0` (off), `1` (low), `2` (high) |
| 134 | `motion_switch` | Motion alert on/off | bool | rw | true/false |
| 168 | `motion_area_switch` | Area detection on | bool | rw | true/false |
| 169 | `motion_area` | Detection area | string | rw | JSON: `{"num":1,"region0":{"x":0,"y":0,"xlen":100,"ylen":100}}` |
| 250 | `motion_detection` | Motion event (read-only) | string | ro | event data |
| 139 | `decibel_switch` | Sound detection on/off | bool | rw | true/false |
| 140 | `decibel_sensitivity` | Sound sensitivity | enum | rw | `0` (off), `1` (low), `2` (high) |
| 141 | `decibel_upload` | Sound event (read-only) | string | ro | `decibel_upload` when triggered |
| 212 | `initiative_message` | Alarm record with snapshot pointer | raw | rw | base64 JSON: `{"cmd":"ipc_motion","alarm":true,"time":...,"files":[...]}`; one slot holding the newest alarm. `cmd` is a reused Tuya code: `ipc_motion`/`ipc_bang`/`ipc_cry` are real alerts, but `ipc_custom` is the cry-translation result "baby needs to burp" (Zoundream), not a motion/sound event |
| 239 | `monitor_sensitivity` | Background monitoring | enum | rw | `0`, `1`, `2`, `3` |

### Two-Way Audio

| ID | Code | Name | Type | Mode | Values/Range |
|----|------|------|------|------|-------------|
| 252 | `pu_talking` | Parent unit talkback | enum | rw | `0` (off), `1` (on) |
| 253 | `app_talking` | App talkback | enum | rw | `0` (off), `1` (on) |
| 251 | `background_mode` | Background audio mode | bool | rw | true/false |

### System

| ID | Code | Name | Type | Mode | Values/Range |
|----|------|------|------|------|-------------|
| 205 | `power_status` | Power state | enum | ro | `0` (battery), `1` (plugged) |
| 206 | `OTA_message` | Firmware update | enum | rw | `0`, `1`, `2` |
| 247 | `device_poweroff` | Power off device | enum | rw | `0`, `1` |
| 254 | `bu_reset` | Base unit reset | string | ro | — |
| 255 | `timer_report` | Report timer | enum | rw | `0`, `1` |

## Video Quality

Video quality is controlled via the WebRTC session, not DPS. The `rtc.config.get`
response includes `vedioClaritys: [2, 4, 8]`:

| Value | Quality |
|-------|---------|
| 2 | HD (1920×1080) — main stream |
| 4 | SD (1280×720) — sub stream |
| 8 | Audio only |

Set the desired quality when initiating the WebRTC connection by selecting the
appropriate stream type in the SDP offer.

## Signal Strength

Not available via DPS. Can be read from the device info's network status
or via `tuya.m.device.upgrade.rssi.info.query`.

## Examples

### Turn on night light at 30% brightness
```python
client.set_dps(cam_id, {"138": True, "158": 30})
```

### Play lullaby, volume 50%, auto-stop after 30 minutes
```python
client.set_dps(cam_id, {
    "201": "play",
    "209": 50,
    "243": True,
    "244": 1800,
})
```

### Stop lullaby
```python
client.set_dps(cam_id, {"201": "stop"})
```

### Read temperature
```python
device = client.get_device(cam_id)
temp_raw = device["dps"]["207"]  # e.g. 2250
temp_c = temp_raw / 100          # 22.50 °C
```

### Enable motion + sound alerts
```python
client.set_dps(cam_id, {
    "134": True,   # motion alert on
    "106": "2",    # high sensitivity
    "139": True,   # sound alert on
    "140": "2",    # high sensitivity
})
```

### Enable talkback (two-way audio)
```python
client.set_dps(cam_id, {"253": "1"})  # app talking on
# Audio is sent via WebRTC data channel (backchannel)
```

### Privacy mode (camera off, audio only)
```python
client.set_dps(cam_id, {"237": "1"})  # privacy on
client.set_dps(cam_id, {"237": "0"})  # privacy off
```
