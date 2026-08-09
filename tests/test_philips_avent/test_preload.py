"""Tests for the keep_stream_running glue (preload.py).

The rules being guarded (the why is in preload.py's module docstring):
go2rtc's `PUT /api/preload` is destructive, so arming must check
`GET /api/preload` first and skip the PUT when already armed — a blind
re-PUT on every answer is an endless churn of Tuya sessions. And failures
must never leak out of the answer hook.
"""
import asyncio
import logging

import preload as preload_mod
from preload import StreamPreloader, describe_error

NAME = "philips_avent_cam1_src"  # the producer stream holds the session


def run(coro):
    return asyncio.run(coro)


class FakeHass:
    """Duck-types the two members StreamPreloader touches."""

    def __init__(self):
        self.data = {}
        self.tasks = []

    def async_create_background_task(self, coro, name):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task


class FakeGo2RtcConfig:
    """Duck-types HA's go2rtc Go2RtcConfig(url, session)."""

    url = "http://localhost:11984/"
    session = object()


class FakeState:
    """Shared backend state across Go2RtcRestClient instantiations.

    preload.py constructs a fresh client per call, so the fake server
    state must live outside the client object.
    """

    def __init__(self):
        self.preloads: dict[str, dict] = {}
        self.streams: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail_list_with: BaseException | None = None
        self.fail_enable_with: BaseException | None = None


class FakePreloadAPI:
    def __init__(self, state):
        self._state = state

    async def list(self):
        self._state.calls.append(("preload.list",))
        if self._state.fail_list_with is not None:
            raise self._state.fail_list_with
        return dict(self._state.preloads)

    async def enable(self, name):
        self._state.calls.append(("preload.enable", name))
        if self._state.fail_enable_with is not None:
            raise self._state.fail_enable_with
        self._state.preloads[name] = {}

    async def disable(self, name):
        self._state.calls.append(("preload.disable", name))
        self._state.preloads.pop(name, None)


class FakeStreamsAPI:
    def __init__(self, state):
        self._state = state

    async def list(self):
        self._state.calls.append(("streams.list",))
        return dict(self._state.streams)


def install_go2rtc(monkeypatch, hass, state):
    """Wire a fake Go2RtcRestClient and a fake hass.data['go2rtc'] slot."""

    class FakeClient:
        def __init__(self, session, url):
            assert session is not None and url
            self.preload = FakePreloadAPI(state)
            self.streams = FakeStreamsAPI(state)

    monkeypatch.setattr(preload_mod, "Go2RtcRestClient", FakeClient)
    hass.data["go2rtc"] = FakeGo2RtcConfig()


def enables(state):
    return [c for c in state.calls if c[0] == "preload.enable"]


# -- arming is idempotent (the churn-loop guard) ---------------------------


def test_repeated_answers_put_once(monkeypatch):
    """A blind re-PUT would make go2rtc drop and redial the live producer,
    churning the just-answered Tuya session forever. Already armed => no PUT.

    Driven through camera_answered, the hook the stream server actually
    calls, so the background-task wiring is covered too."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    pre = StreamPreloader(hass)

    async def scenario():
        for _ in range(3):  # every answer fires the hook again
            pre.camera_answered("cam1")
            await asyncio.gather(*hass.tasks)

    run(scenario())
    assert enables(state) == [("preload.enable", NAME)]  # exactly one PUT


def test_rearm_after_external_disable(monkeypatch):
    """A go2rtc restart silently eats preloads (the provider used to disarm
    the _camera name too, historically); the next answer must re-arm."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    pre = StreamPreloader(hass)
    run(pre._async_enable("cam1"))
    state.preloads.clear()  # the provider (or a go2rtc restart) disarmed us
    run(pre._async_enable("cam1"))
    assert enables(state) == [("preload.enable", NAME)] * 2


def test_concurrent_answers_put_once(monkeypatch):
    """A burst of answers must not race the armed-check into parallel PUTs."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    pre = StreamPreloader(hass)

    async def burst():
        await asyncio.gather(*(pre._async_enable("cam1") for _ in range(5)))

    run(burst())
    assert enables(state) == [("preload.enable", NAME)]


# -- resume and disable at setup -------------------------------------------


def test_resume_arms_only_streams_go2rtc_knows(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.streams[NAME] = {}
    pre = StreamPreloader(hass)
    run(pre.async_resume(["cam1", "cam2"]))
    assert enables(state) == [("preload.enable", NAME)]  # cam2 unknown: no PUT


def test_disable_stops_only_armed_preloads(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.preloads[NAME] = {}
    pre = StreamPreloader(hass)
    run(pre.async_disable(["cam1", "cam2"]))
    disables = [c for c in state.calls if c[0] == "preload.disable"]
    assert disables == [("preload.disable", NAME)]


def test_disable_sweeps_both_names(monkeypatch):
    """An upgrade may leave a _camera preload armed by an older version;
    disabling must stop it too, or go2rtc streams for nobody."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.preloads["philips_avent_cam1_camera"] = {}
    state.preloads["philips_avent_cam1_src"] = {}
    run(StreamPreloader(hass).async_disable(["cam1"]))
    assert state.preloads == {}


# -- degradation -----------------------------------------------------------


def test_enable_failure_never_raises(monkeypatch, caplog):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.fail_enable_with = TimeoutError()
    pre = StreamPreloader(hass)
    with caplog.at_level(logging.WARNING, logger="preload"):
        run(pre._async_enable("cam1"))  # must not raise
    assert "TimeoutError" in caplog.text


def test_module_level_client_reads_the_same_slot(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    assert preload_mod.go2rtc_rest_client(hass) is not None
    hass.data.clear()
    assert preload_mod.go2rtc_rest_client(hass) is None


def test_describe_error_survives_cause_cycles():
    # A __cause__ cycle must not hang the warning path.
    a, b = ValueError("a"), ValueError("b")
    a.__cause__, b.__cause__ = b, a
    assert describe_error(a) == "ValueError: a <- caused by ValueError: b"


def test_describe_error_redacts_query_strings():
    """An HTTP error renders its request URL, and a failed streams PUT
    carries the producer's ws source — token included — in the query."""
    err = RuntimeError(
        "400, message='Bad Request', "
        "url='http://localhost:11984/api/streams?name=x&src=webrtc%3Aws%3A%2F%2F"
        "127.0.0.1%3A38555%2Favent%2Fcam1%3Ft%3Dsecret-tok'"
    )
    text = describe_error(err)
    assert "secret-tok" not in text
    assert "<redacted>" in text
    assert text.startswith("RuntimeError: 400, message='Bad Request'")
