"""The hardware test runner, exercised end to end against the fake unit."""

import pytest

from qcsuite import selftest
from qcsuite.cliutil import Ctx


def test_steps_are_ordered_as_the_plan_requires():
    steps = selftest.steps_table()
    groups = [s["group"] for s in steps]
    assert groups == sorted(groups)                     # 0 準備 .. 5 後片付け
    first_checks = [s["title"] for s in steps if s["group"] == "1 最初の確認"]
    assert "上限" in first_checks[0] and "VOLUME" in first_checks[1]
    assert steps[-1]["title"].startswith("sandbox の削除")
    assert all(s["expect"] and s["on_fail"] for s in steps)


def test_mock_run_passes_and_restores_the_folders(tmp_path):
    report = tmp_path / "report.md"
    code = selftest.run_mock(Ctx(mock=True, yes=True), report_path=report)
    text = report.read_text()
    assert code == 0, text
    assert "| FAIL |" not in text and "完了" in text
    assert "_qcsuite_sandbox" in text


def test_no_room_for_a_setlist_stops_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setattr(selftest.Run, "ask_text", lambda self, q, default: "8")
    report = tmp_path / "report.md"
    code = selftest.run_mock(Ctx(mock=True, yes=True), report_path=report)
    text = report.read_text()
    assert code == 1
    assert "| STOP |" in text
    assert "sandbox の作成 | SKIP" in text
    # cleanup still runs and finds the folders unchanged
    assert "sandbox の削除とフォルダ数の照合 | PASS" in text


def test_inert_lane_volume_is_reported_as_a_failure(tmp_path, monkeypatch):
    from qcsuite.device import fake as fake_mod
    original = fake_mod.FakeConfig.__init__

    def inert(self, *a, **k):
        original(self, *a, **k)
        self.lane_volume_inert = True
    monkeypatch.setattr(fake_mod.FakeConfig, "__init__", inert)
    report = tmp_path / "report.md"
    selftest.run_mock(Ctx(mock=True, yes=True), report_path=report)
    line = next(l for l in report.read_text().splitlines() if "Lane Output VOLUME" in l)
    assert "| FAIL |" in line
