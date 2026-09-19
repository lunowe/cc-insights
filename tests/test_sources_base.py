import dataclasses

import pytest

from cc_insights.sources import EventKind, RawEvent, SourceAdapter


def _event(**kw):
    base = dict(source="codex", native_session_id="s", native_thread_id="t",
                native_event_id="t:0", ts_ms=1, kind=EventKind.ASSISTANT)
    return RawEvent(**{**base, **kw})


def test_rawevent_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _event().ts_ms = 2


def test_rawevent_rejects_arbitrary_fields():
    """Structural enforcement of the no-content rule: an adapter cannot attach
    prompt or response text to a RawEvent."""
    with pytest.raises((AttributeError, TypeError)):
        _event().message_text = "secret prompt"
    with pytest.raises(TypeError):
        _event(content="secret prompt")


def test_no_content_bearing_fields_in_contract():
    names = {f.name for f in dataclasses.fields(RawEvent)}
    for banned in ("content", "text", "message", "body", "prompt", "response", "args"):
        assert banned not in names


def test_protocol_conformance():
    class Dummy:
        name = "dummy"
        def discover(self): return []
        def parse(self, path, from_byte=0): return iter(())
    assert isinstance(Dummy(), SourceAdapter)
