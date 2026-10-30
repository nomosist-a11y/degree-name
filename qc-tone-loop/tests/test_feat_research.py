"""Research tools: truncation, the sniffer on the real Transport, outbound, diff."""

import json
import queue
import time

import pytest
from pyquadcortex import framing, registry
from pyquadcortex.client import QuadCortex
from pyquadcortex.proto import ProductionAutomation_pb2 as pa
from pyquadcortex.transport import Transport

from qcsuite.features import research


class FakeHID:
    """An hidapi-like device: inbound reports from a queue, writes recorded."""

    def __init__(self):
        self.inbox = queue.Queue()
        self.written = []

    def read(self, size, timeout_ms):
        try:
            return self.inbox.get(timeout=timeout_ms / 1000)
        except queue.Empty:
            return []

    def write(self, report):
        self.written.append(bytes(report))
        return len(report)

    def push(self, message_type: int, payload: bytes):
        for report in framing.encode_message(message_type, payload):
            self.inbox.put(bytes([framing.IN_REPORT_ID]) + report[1:])

    def push_message(self, message):
        self.push(registry.type_for(type(message)), message.SerializeToString())


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_truncate_keeps_short_values_and_hashes_long_ones():
    data = {"a": "x" * 10, "b": "y" * 600, "c": list(range(300)), "d": {"e": ["z" * 300]}}
    out = research.truncate(data, string_limit=256, list_limit=256)
    assert out["a"] == "x" * 10
    assert out["b"]["_truncated"] == "string" and out["b"]["len"] == 600
    assert len(out["b"]["sha256"]) == 16 and out["b"]["head"] == "y" * 48
    assert out["c"][-1] == {"_truncated": "list", "len": 300} and len(out["c"]) == 257
    assert out["d"]["e"][0]["_truncated"] == "string"


def test_sniffer_records_decoded_raw_and_outbound_then_unhooks():
    hid = FakeHID()
    t = Transport(hid, keepalive_interval=60)
    t.start()
    try:
        qc = QuadCortex(t)
        with research.Sniffer(qc) as sniffer:
            grid = pa.GridMessage(action=pa.MessageAction.UPDATE)
            model = grid.preset.chains.add().models.add()
            model.hash = 14000
            model.params.add().param_values.add().string_value = "a" * 64 + "Corn Vixen 1"
            hid.push_message(grid)
            # A License push whose body is not protobuf: pyquadcortex drops it
            # before dispatch, the sniffer still logs it.
            junk = b"\xff\xfe not protobuf \x00" * 3
            hid.push(pa.CortexMessageType.Enum.Value("License"), junk)
            hid.push_message(pa.KeepAliveMessage(action=pa.MessageAction.UPDATE))
            qc.set_param(0, 2, param_index=5, text="b" * 64 + "Darkglass VMT 1")
            assert _wait_for(lambda: len(sniffer.records) >= 3)
        kinds = {(r["dir"], r["type"], r["kind"]) for r in sniffer.records}
        assert ("in", "Grid", "msg") in kinds
        assert ("in", "License", "raw") in kinds
        assert ("out", "Grid", "msg") in kinds
        assert sniffer.excluded_counts.get("KeepAlive") == 1
        incoming = next(r for r in sniffer.records if r["dir"] == "in" and r["type"] == "Grid")
        text = json.dumps(incoming)
        assert "Corn Vixen 1" in text and incoming["action"] == "UPDATE"
        raw = next(r for r in sniffer.records if r["kind"] == "raw")
        assert raw["decoded"] is False and raw["len"] == len(junk)
        assert raw["head_hex"].startswith("fffe")
        # After the window the transport is back to its own methods.
        assert "_dispatch" not in t.__dict__ and "send" not in t.__dict__
        hid.push_message(grid)
        time.sleep(0.3)
        assert sum(1 for r in sniffer.records if r["dir"] == "in" and r["type"] == "Grid") == 1
    finally:
        t.stop()


def test_sniffer_refuses_a_client_without_the_expected_internals():
    with pytest.raises(RuntimeError, match="0.40.0"):
        research.Sniffer(object())


def test_mock_sniff_is_labelled_synthetic(rig):
    records = research.sniff(rig, 0.05, simulate="capture")
    header, footer = records[0], records[-1]
    assert header["mock"] and header["synthetic"] and "実機の通信ではありません" in header["note"]
    calls = [r for r in records if r["kind"] == "call"]
    assert calls and all(r["synthetic"] for r in calls)
    assert any(r["type"] == "fake.set_block" for r in calls)
    assert footer["count"] == len(calls)
    with pytest.raises(StopIteration):
        next(r for r in records if r["kind"] == "msg")


@pytest.mark.parametrize("action", ["mic", "plugin"])
def test_mock_simulations(rig, action):
    rig.device.read_stored("/media/p4/Presets/kami", 4)
    records = research.sniff(rig, 0.01, simulate=action)
    assert any(r["kind"] == "call" for r in records)


def test_outbound_matches_what_set_capture_sends():
    records = research.outbound("set-capture", row=0, col=3, key="c" * 64, name="Corn Vixen 2")
    header, *messages = records
    assert header["offline"] and len(messages) == 1
    m = messages[0]
    assert m["type"] == "Grid" and m["dir"] == "out" and m["action"] == "UPDATE"
    flat = json.dumps(m["data"])
    assert "Corn Vixen 2" in flat and '"index": 5' in flat
    block = research.outbound("set-block", row=1, col=2, model=14001)[1]
    assert block["data"]["preset"]["chains"][0]["models"][0]["hash"] == 14001
    with pytest.raises(ValueError):
        research.outbound("rename")


def test_diff_finds_types_paths_and_values():
    a = [{"kind": "header"},
         {"kind": "msg", "dir": "in", "type": "Grid", "data": {"preset": {"chains": [
             {"models": [{"hash": 14000, "notify_flag": True}]}]}}},
         {"kind": "msg", "dir": "in", "type": "DefaultParameters", "data": {"row": 0}}]
    b = [{"kind": "header", "synthetic": True},
         {"kind": "msg", "dir": "in", "type": "Grid", "data": {"preset": {"chains": [
             {"models": [{"hash": 14001}]}]}}},
         {"kind": "raw", "dir": "in", "type": "License", "len": 10, "sha256": "x"}]
    d = research.diff(a, b)
    assert d["only_in_a"] == ["in:DefaultParameters"] and d["only_in_b"] == ["in:License"]
    fields = d["fields"]["in:Grid"]
    assert fields["paths_only_in_a"] == ["preset.chains[].models[].notify_flag"]
    assert fields["values_differ"]["preset.chains[].models[].hash"] == {"a": ["14000"],
                                                                         "b": ["14001"]}
    assert d["synthetic"]
    text = research.diff_text(d)
    assert "モック" in text and "A にだけある項目" in text and "B のみ" in text
    assert research.diff(a, b, types=["License"])["only_in_b"] == ["in:License"]


def test_log_round_trip(tmp_path):
    path = research.write_log(tmp_path / "x.jsonl", [{"kind": "header", "note": "日本語"}])
    assert research.read_log(path) == [{"kind": "header", "note": "日本語"}]
    (tmp_path / "bad.jsonl").write_text('{"kind": "header"}\n{oops\n')
    with pytest.raises(ValueError, match="bad.jsonl:2"):
        research.read_log(tmp_path / "bad.jsonl")


def test_dependencies_and_read_state_on_the_mock(rig):
    rows = research.dependencies(rig, "kami")
    assert len(rows) == 20 and rows[0]["name"] == "Henriksen"
    assert rows[0]["product_dependencies"] == []
    state = research.read_state(rig, "License")
    assert state[0]["synthetic"] and state[1]["kind"] == "note"
    with pytest.raises(ValueError, match="読み取り対象外"):
        research.read_state(rig, "GlobalEQ")


def test_cli_sniff_outbound_and_diff(tmp_path, capsys):
    from qcsuite.cli import main
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    main(["--mock", "research", "sniff", "--seconds", "0.05", "--simulate", "plugin",
          "--out", str(a)])
    main(["research", "outbound", "set-text", "--row", "0", "--col", "4", "--index", "0",
          "--text", "NG_412 Ribbon R121", "--out", str(b)])
    capsys.readouterr()
    main(["--json", "research", "diff", str(a), str(b)])
    d = json.loads(capsys.readouterr().out)
    assert d["synthetic"] and "out:Grid" in d["only_in_b"]
    assert research.read_log(a)[0]["mock"] is True


def test_measure_detects_a_change_and_a_non_change(rig):
    device = rig.device
    device.read_stored("/media/p4/Presets/kami", 4)
    research.measure(rig, "before")
    research.measure(rig, "again")
    same = research.compare_measurements(True, "before", "again")
    assert not same["changed"] and same["mock"]
    amp = next(b for b in device.grid()["blocks"] if b["category"] == "Guitar Amplifier")
    knob = next(p["name"] for p in amp["params"] if p["name"] in ("TREBLE",))
    device.apply_patch({"ops": [{"op": "set_param", "row": 0, "col": amp["col"],
                                 "param": knob, "value": 1.0}]})
    research.measure(rig, "treble-up")
    moved = research.compare_measurements(True, "before", "treble-up")
    assert moved["changed"] and moved["shape_diff_db"] >= research.CHANGED_SHAPE_DB
    # A string write (the cab's mic) reads back but does not change the sound.
    cab = next(b for b in device.grid()["blocks"] if b["category"].startswith("Cabsim"))
    device.set_text(cab["row"], cab["col"], 0, "NG_412 Ribbon R121")
    research.measure(rig, "mic-from-host")
    assert not research.compare_measurements(True, "treble-up", "mic-from-host")["changed"]
    with pytest.raises(ValueError, match="ありません"):
        research.compare_measurements(True, "before", "nope")


def test_cli_measure(capsys):
    from qcsuite.cli import main
    main(["--mock", "research", "measure", "--label", "x"])
    main(["--mock", "research", "measure", "--label", "y"])
    capsys.readouterr()
    main(["--mock", "--json", "research", "measure", "--compare", "x", "y"])
    assert json.loads(capsys.readouterr().out)["changed"] is False
