"""Preset normalization and diffs, and the restore check built on them."""


def test_per_scene_values_compare_within_tolerance():
    """2026-10-28: 'DECAY 0.787 -> 0.787' was reported as a change (exact list compare)."""
    from qcsuite import presets
    a = {"value": 0.787, "scenes": [0.787] * 8}
    b = {"value": 0.787, "scenes": [0.7870004] * 8}
    assert presets._same(a, b)
    assert not presets._same(a, {"value": 0.787, "scenes": [0.787] * 7 + [0.5]})


def test_restore_check_separates_documented_limits():
    from qcsuite import selftest
    block = {"row": 0, "col": 1, "model": "Room", "model_id": 1, "category": "Reverb",
             "params": {"DECAY": {"index": 0, "value": 0.5, "scenes": [0.5, 0.6]},
                        "MIX": {"index": 1, "value": 0.3}},
             "bypass": {"scene_mode": True, "scenes": [True, False]}}
    source = {"blocks": [block]}
    label = selftest.presets.block_label(block)
    changes = [{"kind": "changed", "where": f"{label} DECAY"},
               {"kind": "changed", "where": f"{label} バイパス"},
               {"kind": "changed", "where": f"{label} MIX"}]
    expected, real = selftest.split_restore_limits(source, changes)
    assert [c["where"] for c in real] == [f"{label} MIX"]
    assert len(expected) == 2
