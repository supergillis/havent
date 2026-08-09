"""Constants for the Philips Avent Baby Monitor integration."""
from __future__ import annotations

import string
from urllib.parse import quote

DOMAIN = "philips_avent"

# Tuya Mobile SDK credentials (static per APK version)
# These are functional identifiers, equivalent to OAuth client_id/secret
# in other HA integrations. Same for all users of the Philips Avent app.
TUYA_SIGNING_KEY = (
    "com.philips.ph.babymonitorplus"
    "_D2:D6:95:A1:1D:1B:84:F9:25:A9:45:6E:27:F4:45:E9:FD:87:C3:74"
    ":63:AA:8A:34:32:A6:6A:23:3B:0F:D5:0F"
    "_8n459nxk9g98gqgcwrpk3csv97uuwajm"
    "_a3nfht4ufwfw9cmkspaftv4x89cx58qx"
)
TUYA_APP_KEY = "wx3at9qprkhskvkcsyhm"
TUYA_PACKAGE_NAME = "com.philips.ph.babymonitorplus"
TUYA_CH_KEY = "071d81fa"

# Default data center (Central Europe). A Tuya account is bound to one data
# center and its session id is rejected by the others, so the real host is
# resolved per account at login time by `region.py` and persisted in the config
# entry. These two constants are only the fallback for entries created before
# that resolution existed (issues #44, #58).
TUYA_API_URL = "https://a1.tuyaeu.com/api.json"
TUYA_DEFAULT_COUNTRY_CODE = "39"

# MQTT is never addressed by a constant: the login response and
# `smartlife.m.user.info.get` both return the account's own broker in their
# `domain` block (`mobileMqttsUrl`, e.g. m1.tuyaeu.com for EU accounts,
# m1.tuyaus.com for American ones) and the bridge connects to that.
TUYA_MQTT_PORT = 8883

# Device model naming (issue #42). The Tuya productId does not distinguish
# models within the SCD9xx family — the same id ("selj2idknqhjnids") has been
# reported by both SCD951 and SCD953/26 units — so the displayed model stays
# generic. Add an entry here only when a productId is confirmed to belong to
# exactly one model.
PRODUCT_ID_TO_MODEL: dict[str, str] = {}
DEFAULT_MODEL = "Avent Baby Monitor"

# DPS codes
# SenseIQ sleep tracking lives on the low DPS range (ids 1-21), unlike every
# other feature. DPS 3 and 4 carry JSON payloads decoded by senseiq.py; the
# rest of the block (consent flags, cry-translation subscription, detection
# area) is deliberately not surfaced.
DPS_SENSEIQ_SWITCH = "1"
DPS_SENSEIQ_STATUS = "3"
DPS_SLEEP_SESSION = "4"
# The three SenseIQ alert toggles: plain rw bools per the device schema
# (`awake_switch`, `cry_det_switch`, `no_senseiq_switch`). DPS 13 pairs with
# the DPS 15 no-signal flag the way DPS 134/139 pair with their events.
DPS_AWAKE_ALERT_SWITCH = "11"
DPS_CRY_ALERT_SWITCH = "12"
DPS_NO_SIGNAL_ALERT_SWITCH = "13"
DPS_NO_SENSEIQ_SIGNAL = "15"
DPS_NIGHT_LIGHT = "138"
DPS_BRIGHTNESS = "158"
DPS_LIGHT_COLOR = "204"
DPS_LIGHT_TIMER = "240"
DPS_LIGHT_TIMER_SWITCH = "241"
DPS_TEMPERATURE = "207"
DPS_TEMPERATURE_F = "208"
DPS_MOTION_SWITCH = "134"
DPS_MOTION_SENSITIVITY = "106"
DPS_SOUND_SWITCH = "139"
DPS_SOUND_SENSITIVITY = "140"
DPS_LULLABY_CONTROL = "201"
DPS_LULLABY_VOLUME = "209"
DPS_LULLABY_MODE = "203"
DPS_LULLABY_STATE = "246"
DPS_LULLABY_TIMER_SWITCH = "243"
DPS_LULLABY_TIMER = "244"
DPS_PRIVACY_MODE = "237"
DPS_POWER_STATUS = "205"
DPS_FLIP = "102"
DPS_APP_TALKING = "253"
# Alert delivery differs by family and by negotiated LAN protocol version. A
# decrypted capture (#51) found none of these three in the LAN DP_QUERY set, and
# on a 3.3 session none of them ever arrived as a push. On 3.5, DPS 212 does
# arrive: an SCD951 owner measured a motion record pushed with the sensor firing
# 1.3 seconds later, where the poll took 35 (#61). Sound on that same monitor
# still came through the poll, so coordinator.py keeps the fast poll for monitors
# reporting alarms in 212 rather than counting on the push.
DPS_ALERT_EVENT = "250"
DPS_DECIBEL_EVENT = "141"
# Alarm record with the snapshot the camera uploaded. The SCD951 and SCD953
# family reports motion here instead of on DPS 250 (issues #61, #42); see
# events.py for the payload. One slot holding the newest alarm, not a queue.
DPS_ALARM_RECORD = "212"

LULLABY_TRACK_MAP = {
    3542154: ("Baa Baa Black Sheep", "lullabies"),
    3542155: ("Brahms' Lullaby", "lullabies"),
    3542156: ("Rock-a-Bye Baby", "lullabies"),
    3542157: ("Golden Slumbers", "lullabies"),
    3542158: ("Hush Little Baby", "lullabies"),
    3542159: ("Mother's Shh", "noise"),
    3542160: ("Calming River", "noise"),
    3542161: ("Heartbeat", "noise"),
    3542162: ("Vacuum Cleaner", "noise"),
    3542163: ("White Noise", "noise"),
    3542164: ("Garden Bird Song", "nature_sounds"),
    3542165: ("Valley Wind", "nature_sounds"),
    3542166: ("Ocean Shore", "nature_sounds"),
    3542167: ("Night-time Nature", "nature_sounds"),
    3542168: ("Rain Shower", "nature_sounds"),
}

TIMER_OPTIONS = {
    "Off": 0,
    "5 min": 300,
    "10 min": 600,
    "20 min": 1200,
    "30 min": 1800,
    "60 min": 3600,
    "90 min": 5400,
}
TIMER_SECONDS_TO_LABEL = {v: k for k, v in TIMER_OPTIONS.items()}

LULLABY_TRACKS = [name for name, _ in LULLABY_TRACK_MAP.values()]
LULLABY_ID_BY_NAME = {name: tid for tid, (name, _) in LULLABY_TRACK_MAP.items()}

CONF_SID = "sid"
CONF_API_HOST = "api_host"
# Phone device id, generated once and kept, see api.new_device_id and issue #73.
CONF_DEVICE_ID = "device_id"
CONF_COUNTRY_CODE = "country_code"
CONF_ECODE = "ecode"
CONF_PARTNER = "partner_identity"
CONF_UID = "uid"
CONF_CAMERA_ID = "camera_id"
CONF_CAMERA_NAME = "camera_name"
CONF_BRIDGE_PORT = "bridge_port"
DEFAULT_BRIDGE_PORT = 38554
CONF_BRIDGE_HOST = "bridge_host"
DEFAULT_BRIDGE_HOST = "localhost"
# Two-way audio. Off by default: asking the camera for it makes the monitor stop
# a playing lullaby and restart it with a fresh timer when the stream closes
# (issue #72), so listening must not imply talking.
CONF_TALKBACK = "talkback"
DEFAULT_TALKBACK = False

# Where the video comes from. "addon" is the Docker bridge that has always
# served RTSP on CONF_BRIDGE_PORT; "builtin" does the Tuya signaling inside
# Home Assistant and lets its bundled go2rtc carry the media, which needs no
# container at all. Run one or the other, never both: they would derive the
# same Tuya MQTT client id and knock each other off the broker.
CONF_STREAM_BACKEND = "stream_backend"
BACKEND_ADDON = "addon"
BACKEND_BUILTIN = "builtin"
DEFAULT_STREAM_BACKEND = BACKEND_ADDON
STREAM_BACKENDS = [BACKEND_ADDON, BACKEND_BUILTIN]

# The built-in backend's own HTTP port. It listens on loopback only: the one
# thing that dials it is the go2rtc that Home Assistant runs beside us.
CONF_SIGNALING_PORT = "signaling_port"
DEFAULT_SIGNALING_PORT = 38555
# Carried in the signaling URL. The socket is already loopback-only, so this is
# just the second lock: nothing else on the host can open a camera session.
CONF_STREAM_TOKEN = "stream_token"

# Keep the built-in backend's stream permanently connected. Off by default:
# it trades continuous LAN streaming from the camera — whether or not anyone
# is watching — for instant stream opens and zero Tuya session churn, the
# same one-long-lived-session behaviour the Go bridge has always had. Only
# meaningful on the builtin backend. Driven through go2rtc's own preload
# API against the producer stream, NOT Home Assistant's `preload_stream`
# camera preference: that is another integration's user preference, and it
# would arm the provider's `_camera` stream instead of the producer that
# holds the Tuya session (see preload.py for the full why).
CONF_KEEP_STREAM_RUNNING = "keep_stream_running"
DEFAULT_KEEP_STREAM_RUNNING = False


def uses_builtin_backend(options) -> bool:
    """Whether these entry options stream through Home Assistant, not the add-on."""
    return options.get(CONF_STREAM_BACKEND, DEFAULT_STREAM_BACKEND) == BACKEND_BUILTIN


#: Characters a go2rtc stream name keeps as-is; everything else is
#: percent-encoded. Mirrors _SAFE_CHARS in Home Assistant's go2rtc/util.py.
_GO2RTC_SAFE_CHARS = string.ascii_letters + string.digits + "._-"


def go2rtc_stream_name(cam_id: str) -> str:
    """The name go2rtc knows this camera's stream by.

    Mirrors `get_camera_identifier` in Home Assistant's go2rtc integration
    (homeassistant/components/go2rtc/util.py): the camera platform's name
    plus the entity's unique_id, percent-encoded. Our camera unique_id is
    f"{cam_id}_camera" (camera.py), so the stream name is
    "philips_avent_<cam_id>_camera". The two MUST stay in sync — this name
    is what the keep_stream_running option arms go2rtc's preload with, and
    a mismatch would arm a stream that does not exist.
    """
    return quote(f"{DOMAIN}_{cam_id}_camera", safe=_GO2RTC_SAFE_CHARS)


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


def builtin_stream_url(port: int, token: str, cam_id: str) -> str:
    """The signaling endpoint go2rtc dials to open this camera's session.

    This URL is registered as the producer stream's source (restream.py);
    what stream_source() hands out is normally the producer's RTSP URL,
    and this ws URL is only the last-resort fallback when go2rtc has no
    RTSP the stream component could reach. Pure on purpose: restream.py
    must stay importable without Home Assistant.
    """
    return f"webrtc:ws://127.0.0.1:{port}/avent/{cam_id}?t={token}"


def sanitize_rtsp_path(name: str, cam_id: str) -> str:
    """Convert a camera display name into an RTSP path component.

    Spaces, forward slashes and backslashes are replaced with underscores.
    If the result is empty or consists only of an underscore, falls back to
    the camera id. Returns the path component WITHOUT a leading slash; the
    caller composes the URL.

    Mirrors `pkg/storage/path.go::SanitizeRTSPPath` in the Go bridge. The two
    helpers MUST stay in sync — they determine whether the integration's RTSP
    URL matches the path the bridge serves.
    """
    safe = name.replace(" ", "_").replace("/", "_").replace("\\", "_")
    if safe == "" or safe == "_":
        safe = cam_id
    return safe


def build_rtsp_url(host: str, port: int, name: str, cam_id: str) -> str:
    """Build the RTSP URL Home Assistant should pull the stream from.

    The host defaults to localhost, which only holds when the bridge runs
    alongside Home Assistant (add-on, or same container). A bridge in its own
    container or on another machine is not reachable there, and the resulting
    "Connection refused" makes the camera entity flap between Unavailable and
    Idle, because Home Assistant marks a camera unavailable while its stream
    cannot be opened (issue #62).
    """
    return f"rtsp://{host or DEFAULT_BRIDGE_HOST}:{port}/{sanitize_rtsp_path(name, cam_id)}"
