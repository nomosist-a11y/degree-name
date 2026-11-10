"""The fake unit reproduces the player's content and the measured traps."""

from collections import Counter

import pyquadcortex as pq
import pytest

from qcsuite.device import fake as fake_mod
from qcsuite.device import fake_data
from qcsuite.errors import ForbiddenCall

USER = "/media/p4/Presets"


def test_folder_counts_match_the_real_unit(fake):
    counts = {f.name: f.occupied for f in fake.list_folders()
              if f.key.startswith(USER) or f.key == fake_data.FACTORY_KEY}
    assert counts == {"My Presets": 51, "Favorite": 21, "kami": 20, "Cory Wong": 15,
                      "Live": 0, "Artist": 0, "Song": 0, "Workshop": 0,
                      "Factory Library": 256}


def test_duplicate_and_factory_name_overlaps(fake):
    folders = {}
    for key, slots in fake.folders.items():
        if key.startswith(USER):
            for s in slots.values():
                folders.setdefault(s.name, set()).add(key)
    shared = [n for n, where in folders.items() if len(where) > 1]
    assert len(shared) == 20
    factory = {s.name for s in fake.folders[fake_data.FACTORY_KEY].values()}
    assert len(set(folders) & factory) == 10
    assert len(fake.captures()) == 2078
    assert Counter(c[2] for c in fake._captures)["Corn Vixen 1"] == 1


def test_catalog_times_out_then_works():
    f = fake_mod.FakeQuadCortex(fake_mod.FakeConfig(catalog_timeouts=1))
    with pytest.raises(TimeoutError):
        f.catalog
    assert f.catalog.get(14000).name == "Eltron 30"
    assert f.catalog.get(14001).name == "Capture 2"


def test_first_read_is_dropped_once():
    f = fake_mod.FakeQuadCortex(fake_mod.FakeConfig(catalog_timeouts=0))
    with pytest.raises(TimeoutError):
        f.read_current_preset()
    assert f.read_current_preset() is not None


def test_capture_block_is_placed_although_refused(fake):
    with pytest.raises(pq.BlockRefused):
        fake.set_block(0, 6, 14000)
    assert any(b.row == 0 and b.column == 6 and b.model_id == 14000
               for b in pq.blocks(fake.read_current_preset()))


def test_host_written_capture_reads_back_but_does_not_sound(fake):
    fake.read_preset(f"{USER}/My Presets", 0)
    entry = type("C", (), {"key": "a" * 64, "name": "Corn Vixen 1"})
    fake.set_block(0, 7, 14000, verify=False)
    fake.set_capture(0, 7, entry, model=None, params={0: 0.9})
    p = fake.read_current_preset()
    assert pq.param_state(p, 0, 7, 5).values[0] == "a" * 64 + "Corn Vixen 1"
    assert pq.param_state(p, 0, 7, 0).values[0] == pytest.approx(0.9)
    sound = fake.audible_chain()["rows"][0]["blocks"][-1]
    assert sound["passthrough"] and "キャプチャー" in sound["reason"]


def test_string_params_and_capture_knobs_do_not_change_the_sound(fake):
    fake.read_preset(f"{USER}/My Presets", 0)
    cab = next(b for b in pq.blocks(fake.live)
               if fake._catalog_obj[b.model_id].category.startswith("Cabsim"))
    before = next(b for b in fake.audible_chain()["rows"][0]["blocks"] if b["col"] == cab.column)
    fake.set_param(0, cab.column, param_index=0, text="NG_412_Ribbon R121")
    after = next(b for b in fake.audible_chain()["rows"][0]["blocks"] if b["col"] == cab.column)
    assert pq.param_state(fake.live, 0, cab.column, 0).values[0] == "NG_412_Ribbon R121"
    assert after["texts"] == before["texts"]


def test_unowned_plugin_passes_through_and_owned_one_sounds(fake):
    plini = fake._catalog_obj.find("Plini Lead").id
    snob = fake._catalog_obj.find("The Amp Snob").id
    fake.set_block(0, 6, plini, verify=False)
    fake.set_block(2, 0, snob, verify=False)
    blocks = {(b["col"], r["row"]): b for r in fake.audible_chain()["rows"] for b in r["blocks"]}
    assert blocks[(6, 0)]["passthrough"]
    assert not blocks[(0, 2)]["passthrough"]


def test_save_dedups_names_to_20_characters(fake):
    key = f"{USER}/Live"
    fake.save_current_preset(key, 0, "Cali Basswalk [Ret1]")
    assert fake.save_current_preset(key, 1, "Cali Basswalk [Ret1]", confirm=True) == \
        "Cali Basswalk [Ret_1"
    assert fake.save_current_preset(key, 2, "Laney test", confirm=True) == "Laney test"
    assert fake.save_current_preset(key, 3, "Laney test", confirm=True) == "Laney test_1"


def test_copy_preset_recalls_the_source_and_drops_tags(fake):
    before = fake.audio_interruptions
    stored = fake.copy_preset(fake_data.FACTORY_KEY, 0, f"{USER}/Song", to_position=0)
    assert fake.audio_interruptions == before + 1
    assert fake.loaded_from == (fake_data.FACTORY_KEY, 0)
    assert stored == fake.folders[fake_data.FACTORY_KEY][0].name
    assert list(fake.folders[f"{USER}/Song"][0].preset.tags) == []


def test_listings_lag_after_a_file_operation(fake):
    key = f"{USER}/Workshop"
    fake.save_current_preset(key, 0, "x")
    assert fake.list_presets(key) == []          # stale for one read
    assert [e.name for e in fake.list_presets(key)] == ["x"]


def test_new_setlist_lists_as_empty(fake):
    key = fake.create_setlist("_qcsuite_sandbox")
    folder = next(f for f in fake.list_folders() if f.key == key)
    assert (folder.occupied, folder.slots) == (0, 256)


def test_whole_unit_settings_are_forbidden(fake):
    for name in ("set_global_eq", "set_input_port", "set_usb_port", "update_settings"):
        with pytest.raises(ForbiddenCall):
            getattr(fake, name)(1)
    with pytest.raises(AttributeError):
        fake.no_such_method


def test_read_current_has_no_side_effects_and_recall_interrupts(fake):
    fake.read_preset(f"{USER}/kami", 0)
    fake.set_param(0, 0, param_index=0, value=0.123)
    n = fake.audio_interruptions
    p = fake.read_current_preset()
    assert fake.audio_interruptions == n
    assert pq.param_state(p, 0, 0, 0).values[0] == pytest.approx(0.123)
    fake.read_preset(f"{USER}/kami", 0)
    assert fake.audio_interruptions == n + 1


def test_new_block_inherits_the_cells_bypass(fake):
    fake.live.bypass[1].colBypass[3].sceneBypass[0].bypass = True
    fake.set_block(1, 3, fake_data.factory_models.Reverb.HALL, verify=False)
    assert pq.bypass_state(fake.live, 1, 3).scenes[0] is True


def test_favourite_with_wrong_folder_is_ignored_silently(fake):
    # "Cali Basswalk" lives only in the Factory Library: naming it under My
    # Presets is the mismatch pyquadcortex's docstring describes.
    wrong = type("E", (), {"name": "Cali Basswalk", "folder_key": f"{USER}/My Presets",
                           "is_factory": False})
    with pytest.raises(TimeoutError):
        fake.add_favorite(wrong)
    assert len(fake.favorites()) == 3
    right = type("E", (), {"name": "Cali Basswalk", "folder_key": fake_data.FACTORY_KEY,
                           "is_factory": True})
    fake.add_favorite(right)
    assert len(fake.favorites()) == 4


def test_state_round_trip(fake, tmp_path):
    fake.save_current_preset(f"{USER}/Live", 5, "kept")
    path = tmp_path / "state.pkl"
    fake.save_state(path)
    other = fake_mod.FakeQuadCortex(fake_mod.FakeConfig(catalog_timeouts=0,
                                                        first_read_dropped=False))
    other.load_state(path)
    assert other.folders[f"{USER}/Live"][5].name == "kept"


def test_cortex_control_open_blocks_connect():
    with pytest.raises(pq.DeviceNotFoundError):
        fake_mod.connect(fake_mod.FakeConfig(cortex_control_open=True))
