"""Tests for the keep_stream_running glue (preload.py).

The rules being guarded (the why is in preload.py's module docstring):
go2rtc's `PUT /api/preload` is destructive, so arming must check
`GET /api/preload` first and skip the PUT when already armed — a blind
re-PUT on every answer is an endless churn of Tuya sessions. And failures
must be diagnosable: go2rtc_client's bare `Go2RtcClientError from exc` and
aiohttp's bare `TimeoutError` both have an empty str(), so `describe_error`
renders type names and the cause chain.
"""
import asyncio
import logging

import preload as preload_mod
from preload import StreamPreloader, describe_error

NAME = "philips_avent_cam1_camera"


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


def test_rearm_skips_put_when_already_preloaded(monkeypatch):
    """A blind re-PUT would make go2rtc drop and redial the live producer,
    churning the just-answered Tuya session forever. Already armed => no PUT."""
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    pre = StreamPreloader(hass)
    run(pre._async_enable("cam1"))
    run(pre._async_enable("cam1"))  # a second answer fires the hook again
    run(pre._async_enable("cam1"))
    assert enables(state) == [("preload.enable", NAME)]  # exactly one PUT


def test_rearm_after_external_disable(monkeypatch):
    """HA's provider disables preloads it did not ask for (entity register/
    unregister, camera-prefs update); the next answer must re-arm."""
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


def test_camera_answered_schedules_enable(monkeypatch):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    pre = StreamPreloader(hass)

    async def scenario():
        pre.camera_answered("cam1")
        await asyncio.gather(*hass.tasks)

    run(scenario())
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


# -- degradation and diagnosability ----------------------------------------


def test_no_go2rtc_warns_once_then_debug(monkeypatch, caplog):
    hass = FakeHass()  # hass.data has no "go2rtc" slot at all
    pre = StreamPreloader(hass)
    with caplog.at_level(logging.DEBUG, logger="preload"):
        run(pre._async_enable("cam1"))
        run(pre._async_enable("cam1"))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "go2rtc is not available" in warnings[0].message


def test_failure_logs_type_and_cause_chain(monkeypatch, caplog):
    """The field failure logged '(…) ()': a bare exception with an empty
    str(). The warning must name the exception type and its cause."""

    class Go2RtcClientError(Exception):
        pass

    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    try:
        raise Go2RtcClientError from OSError("connect call failed")
    except Go2RtcClientError as err:
        state.fail_list_with = err
    pre = StreamPreloader(hass)
    with caplog.at_level(logging.WARNING, logger="preload"):
        run(pre._async_enable("cam1"))
    assert "(); " not in caplog.text  # the old, useless message shape
    assert "Go2RtcClientError" in caplog.text
    assert "OSError: connect call failed" in caplog.text


def test_enable_failure_never_raises(monkeypatch, caplog):
    hass, state = FakeHass(), FakeState()
    install_go2rtc(monkeypatch, hass, state)
    state.fail_enable_with = TimeoutError()
    pre = StreamPreloader(hass)
    with caplog.at_level(logging.WARNING, logger="preload"):
        run(pre._async_enable("cam1"))  # must not raise
    assert "TimeoutError" in caplog.text


def test_describe_error_survives_cause_cycles():
    a, b = ValueError("a"), ValueError("b")
    a.__cause__, b.__cause__ = b, a
    assert describe_error(a) == "ValueError: a <- caused by ValueError: b"
