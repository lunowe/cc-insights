from cc_insights import ids


def test_ids_are_deterministic():
    a = ids.session_id("host", "codex", "abc")
    b = ids.session_id("host", "codex", "abc")
    assert a == b and len(a) == ids.ID_LEN


def test_different_inputs_differ():
    assert ids.session_id("h", "codex", "a") != ids.session_id("h", "claude_code", "a")


def test_none_and_empty_are_distinct():
    assert ids.make_id(None) != ids.make_id("")


def test_separator_prevents_field_smearing():
    """('ab','c') and ('a','bc') must not collide."""
    assert ids.make_id("ab", "c") != ids.make_id("a", "bc")
