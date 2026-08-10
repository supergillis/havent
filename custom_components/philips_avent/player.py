"""The LAN player page: a browser negotiating with the camera directly.

Served by stream_server.py at `GET /player/{camera_id}?t=<token>` when the
`lan_player` option has opened the signaling server to the LAN. The page is a
plain WebRTC client speaking the same go2rtc ws source protocol as everything
else that dials `/avent/{camera_id}` — it is just another consumer. Media
flows camera → browser peer-to-peer over the LAN; neither Home Assistant nor
go2rtc touches a single RTP packet, which is what makes this page a useful
last-resort viewer and a clean A/B probe when the go2rtc chain misbehaves.

One camera, one consumer: opening this page while a Home Assistant live view
runs replaces that stream's session (stream_server.py, RECENT_ANSWER), so the
page carries a dismissible note saying exactly that.

No secrets are rendered into the page. The JS reads the camera id from its
own path and the token from its own query string, so the only place either
appears is the URL the visitor already typed. The one interpolated value is
the camera's display name — escaped, it comes from the Tuya account.
"""
from __future__ import annotations

import html

_NAME_SLOT = "__CAMERA_NAME__"

#: One self-contained page: no external assets, no build step, verifiable by
#: eye. Kept as a plain string with a placeholder (not str.format) so the
#: CSS/JS braces stay untouched.
PLAYER_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__CAMERA_NAME__</title>
<style>
  html, body { margin: 0; height: 100%; background: #000; font: 14px system-ui, sans-serif; }
  video { width: 100%; height: 100%; object-fit: contain; background: #000; }
  #state {
    position: fixed; top: 12px; left: 12px; padding: 4px 10px; border-radius: 4px;
    color: #ddd; background: rgba(0, 0, 0, 0.55); pointer-events: none;
  }
  #note {
    position: fixed; bottom: 12px; left: 12px; right: 12px; padding: 8px 12px;
    border-radius: 4px; color: #ccc; background: rgba(30, 30, 30, 0.9);
  }
  #note button { float: right; background: none; border: none; color: #ccc; font-size: 16px; }
</style>
</head>
<body>
<video id="video" autoplay muted playsinline></video>
<div id="state">connecting…</div>
<div id="note">
  <button onclick="note.remove()">&#215;</button>
  One consumer per camera: close the Home Assistant live view while using
  this page, or the two will keep replacing each other's stream.
  Tap the video to unmute.
</div>
<script>
// Division of labor with the server: sdp.py reorders m-lines audio-first,
// strips a=extmap and maps directions into what the camera will answer, so
// this page only has to offer plain recvonly audio+video (audio added first
// anyway, keeping the mapping trivial) and speak the go2rtc ws source
// protocol: send {"type":"webrtc/offer"}, the answer is the next message,
// candidates flow both ways after it.
const video = document.getElementById("video");
const state = document.getElementById("state");
const note = document.getElementById("note");

const cameraId = location.pathname.split("/").pop();
const token = new URLSearchParams(location.search).get("t") || "";
const wsUrl = (location.protocol === "https:" ? "wss://" : "ws://")
  + location.host + "/avent/" + cameraId + "?t=" + encodeURIComponent(token);

video.addEventListener("click", () => { video.muted = !video.muted; });

// Reconnect gently: every retry dials a real Tuya session against the
// camera's 3-5 slot pool, so back off from 3s to 30s instead of hammering.
let attempt = 0;
let pc = null;

function show(text) { state.textContent = text; }

function retry(reason) {
  if (pc) { pc.close(); pc = null; }
  const delay = Math.min(3000 * 2 ** attempt, 30000);
  attempt += 1;
  show(reason + " — retrying in " + Math.round(delay / 1000) + "s");
  setTimeout(connect, delay);
}

function connect() {
  show("connecting…");
  // No ICE servers: the camera and this browser are on the same LAN, and
  // host candidates are exactly how the rest of the integration works.
  pc = new RTCPeerConnection();
  pc.addTransceiver("audio", { direction: "recvonly" });
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.ontrack = (event) => { video.srcObject = event.streams[0]; };
  pc.onconnectionstatechange = () => {
    if (!pc) return;
    if (pc.connectionState === "connected") { attempt = 0; show("live"); }
    if (pc.connectionState === "failed") fail("connection failed");
  };

  const ws = new WebSocket(wsUrl);
  let answered = false;
  // An error frame is followed by the server closing the socket; one
  // failure, one retry.
  let settled = false;
  const fail = (reason) => { if (!settled) { settled = true; retry(reason); } };

  pc.onicecandidate = (event) => {
    if (event.candidate && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "webrtc/candidate", value: event.candidate.candidate }));
    }
  };
  ws.onopen = async () => {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    ws.send(JSON.stringify({ type: "webrtc/offer", value: offer.sdp }));
  };
  ws.onmessage = async (event) => {
    const frame = JSON.parse(event.data);
    if (frame.type === "webrtc/answer") {
      answered = true;
      await pc.setRemoteDescription({ type: "answer", sdp: frame.value });
    } else if (frame.type === "webrtc/candidate" && frame.value) {
      // Everything is BUNDLEd, so pinning the candidate to the first
      // m-line is always right.
      await pc.addIceCandidate({ candidate: frame.value, sdpMLineIndex: 0 });
    } else if (frame.type === "error") {
      fail(frame.value);
    }
  };
  // The server closes the socket once negotiation is over; only a close
  // BEFORE the answer means the dial failed.
  ws.onclose = () => { if (!answered) fail("signaling closed"); };
}

connect();
</script>
</body>
</html>
"""


def render(camera_name: str) -> str:
    """The player page for one camera, display name escaped."""
    return PLAYER_HTML.replace(_NAME_SLOT, html.escape(camera_name))
