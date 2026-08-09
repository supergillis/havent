"""SDP rewriting between a WebRTC consumer and the camera.

The camera does not answer an arbitrary offer. Three rules, all established
against an SCD953 (see docs/superpowers/specs/2026-08-09-containerless-camera-design.md):

1. **Audio must come first.** Given a video-first offer the camera answers with
   an empty video section: the `m=video` line, then straight to the audio
   m-line, with no mid, no ICE and no rtpmap. Consumers offer video first, so
   the offer is reordered on the way out and the answer restored on the way in.
2. **Only one audio and one video section.** go2rtc offers a third m-line for
   its audio backchannel. The camera ignores it, so it is dropped outbound and
   answered as rejected (port 0) inbound.
3. **No `a=extmap`.** Long offers get truncated; the Go bridge stripped these
   for the same reason, session-level `a=extmap-allow-mixed` included.

Nothing here imports Home Assistant, so it is unit-testable on its own and
portable to another implementation.
"""
from __future__ import annotations

EOL = "\r\n"
DIRECTIONS = ("sendrecv", "sendonly", "recvonly", "inactive")

# The order the camera wants, which is the reverse of what consumers offer.
CAMERA_ORDER = ("audio", "video")


class SdpError(ValueError):
    """The camera answered with something we cannot hand back to the consumer."""


def _split(sdp: str) -> tuple[list[str], list[list[str]]]:
    """Split into session lines and one list of lines per media section."""
    session: list[str] = []
    sections: list[list[str]] = []
    for line in sdp.replace("\r\n", "\n").split("\n"):
        if not line:
            continue
        if line.startswith("m="):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
        else:
            session.append(line)
    return session, sections


def _join(session: list[str], sections: list[list[str]]) -> str:
    lines = list(session)
    for section in sections:
        lines.extend(section)
    return EOL.join(lines) + EOL


def _attr(section: list[str], key: str) -> str | None:
    prefix = f"a={key}:"
    for line in section:
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _kind(section: list[str]) -> str:
    return section[0].split(maxsplit=1)[0][2:]


def _with_direction(section: list[str], value: str) -> list[str]:
    kept = [line for line in section if not (line.startswith("a=") and line[2:] in DIRECTIONS)]
    return [*kept, f"a={value}"]


def _with_bundle(session: list[str], mids: list[str]) -> list[str]:
    group = f"a=group:BUNDLE {' '.join(mid for mid in mids if mid)}"
    return [group if line.startswith("a=group:BUNDLE") else line for line in session]


def _select(sections: list[list[str]]) -> list[int]:
    """Indices of the sections we forward, in the order the camera wants them.

    The first audio and the first video section; anything else is dropped. Both
    directions derive the mapping from the consumer's offer with this same
    function, so there is no index bookkeeping to keep in sync.
    """
    first: dict[str, int] = {}
    for index, section in enumerate(sections):
        kind = _kind(section)
        if kind in CAMERA_ORDER and kind not in first:
            first[kind] = index
    return [first[kind] for kind in CAMERA_ORDER if kind in first]


def rewrite_offer(offer: str, *, talkback: bool = False) -> str:
    """Turn a consumer's offer into one the camera will answer.

    `talkback` offers the camera two-way audio. It is off by default because an
    offer with a sendrecv audio direction makes the camera take its speaker and
    stop whatever lullaby is playing (issue #72).
    """
    session, sections = _split(offer)
    selected = _select(sections)
    if not selected:
        raise SdpError("offer has no audio or video section")

    session = [line for line in session if not line.startswith("a=extmap")]

    out = []
    for index in selected:
        section = [line for line in sections[index] if not line.startswith("a=extmap")]
        direction = "sendrecv" if talkback and _kind(section) == "audio" else "recvonly"
        out.append(_with_direction(section, direction))

    mids = [_attr(sections[index], "mid") or "" for index in selected]
    return _join(_with_bundle(session, mids), out)


def _clamped(section: list[str], offered: list[str]) -> list[str]:
    """Answer a recvonly offer with sendonly.

    The camera answers its audio section `sendrecv` whatever we offered, which
    is not a legal answer to `recvonly`. pion shrugs; browsers are stricter, so
    the direction is brought back in line with what was actually offered.
    """
    if _direction(offered) == "recvonly" and _direction(section) == "sendrecv":
        return _with_direction(section, "sendonly")
    return section


def rewrite_answer(answer: str, offer: str) -> str:
    """Turn the camera's answer into one that answers the consumer's offer.

    The consumer's WebRTC stack requires one section per offered m-line, in the
    offered order, so the sections the camera never saw come back rejected
    (port 0). They keep their rtpmap lines: a bare dynamic payload type with no
    rtpmap is unresolvable, which go2rtc reports as "payload type not found".
    """
    session, answered = _split(answer)
    _, offered = _split(offer)
    selected = _select(offered)

    if len(answered) != len(selected):
        raise SdpError(
            f"camera answered {len(answered)} media sections, expected {len(selected)}"
        )

    # Pair by mid, since we reordered what the camera saw. Position is the
    # fallback for an answer that echoes no mids.
    by_mid = {_attr(section, "mid"): section for section in answered}
    paired: dict[int, list[str]] = {}
    for index, positional in zip(selected, answered, strict=True):
        mid = _attr(offered[index], "mid")
        section = by_mid.get(mid, positional)
        if _attr(section, "mid") is None:
            raise SdpError(f"camera answered a {_kind(section)} section with no mid")
        paired[index] = _clamped(section, offered[index])

    out = []
    for index, offer_section in enumerate(offered):
        if index in paired:
            out.append(paired[index])
            continue
        media, _port, proto, first_format = offer_section[0].split()[:4]
        out.append([
            f"{media} 0 {proto} {first_format}",
            "c=IN IP4 0.0.0.0",
            f"a=mid:{_attr(offer_section, 'mid') or index}",
            "a=inactive",
            *[line for line in offer_section if line.startswith(("a=rtpmap:", "a=fmtp:"))],
        ])

    mids = [_attr(offered[index], "mid") or "" for index in sorted(selected)]
    return _join(_with_bundle(session, mids), out)


def _direction(section: list[str]) -> str | None:
    return next((line[2:] for line in section if line[2:] in DIRECTIONS), None)


def describe(sdp: str) -> str:
    """One-line summary for logs: kind, mid, direction and payload types."""
    _, sections = _split(sdp)
    return " ".join(
        f"{_kind(section)}[mid={_attr(section, 'mid')} "
        f"{_direction(section) or '?'} "
        f"pt={','.join(section[0].split()[3:])}]"
        for section in sections
    )
