"""The conformance check: the same assertions against the fake or a real station."""

from __future__ import annotations

import sys

import pytest

from fugleramme import check, fake
from fugleramme.check import run
from fugleramme.settings import SettingsStore
from fugleramme.source import BIRDNET_GO, BIRDWEATHER, CAMERA


def test_a_healthy_detector_answers_everything(detector, tmp_path, capsys):
    url, _httpd = detector()
    assert run(url, "", "", tmp_path) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_a_detector_that_is_gone_fails_every_query(detector, tmp_path, capsys):
    url, httpd = detector()
    httpd.shutdown()
    httpd.server_close()

    assert run(url, "", "", tmp_path) == 1
    out = capsys.readouterr().out
    assert "FAIL reachable" in out and "failed" in out


def test_a_private_detector_fails_without_credentials(detector, tmp_path):
    url, _httpd = detector(password="hunter2")
    assert run(url, "", "", tmp_path) == 1
    assert run(url, "birdnet", "hunter2", tmp_path) == 0


def test_a_detector_that_only_gates_the_names_fails_the_language_check(detector, tmp_path, capsys):
    """Every detection answers, so the check would otherwise report a station
    the frame cannot get a single common name out of as all good (#45)."""
    url, _httpd = detector(password="hunter2", private=False)

    assert run(url, "", "", tmp_path) == 1
    out = capsys.readouterr().out
    assert "ok   species, 24 hours" in out
    assert "FAIL name languages" in out and "needs a password" in out

    assert run(url, "", "hunter2", tmp_path) == 0


def test_a_birdweather_station_answers_everything(detector, tmp_path, capsys):
    url, _httpd = detector(birdweather=True)
    assert run(f"{url}/stations/{fake.STATION_ID}", "", "", tmp_path, BIRDWEATHER) == 0
    out = capsys.readouterr().out
    assert f"ok   station {fake.STATION_ID}              {fake.STATION_NAME}" in out


def test_several_stations_are_a_line_each(detector, tmp_path, capsys):
    url, _httpd = detector(birdweather=True)
    stations = f"{url}/stations/{fake.STATION_ID}, {url}/stations/404"
    assert run(stations, "", "", tmp_path, BIRDWEATHER) == 1  # the missing one fails
    out = capsys.readouterr().out
    assert f"ok   station {fake.STATION_ID}              {fake.STATION_NAME}" in out
    assert "FAIL station 404            no such station" in out
    assert "ok   species, all time" in out  # the other station still answers


def test_a_station_birdweather_does_not_have_is_named_as_such(detector, tmp_path, capsys):
    url, _httpd = detector(birdweather=True)
    assert run(f"{url}/stations/404", "", "", tmp_path, BIRDWEATHER) == 1
    assert "FAIL station 404            no such station" in capsys.readouterr().out


def test_a_camera_log_answers_everything(tmp_path, capsys):
    log = tmp_path / "detections.log"
    log.write_text(
        "2026-10-21 20:17:57  driveway__2026-10-21t23-26-26-00-00  cricetidae family"
        "  best=0.96  frames=1/10\n"
    )
    assert run(str(log), "", "", tmp_path, CAMERA) == 0
    out = capsys.readouterr().out
    assert "ok   reachable              1 detections" in out
    assert "ok   name languages         en, sci" in out  # English from its animals file


def test_a_camera_log_that_is_not_there_fails(tmp_path, capsys):
    assert run(str(tmp_path / "detections.log"), "", "", tmp_path, CAMERA) == 1
    assert "FAIL reachable" in capsys.readouterr().out


def test_another_detector_is_read_as_what_its_address_says(tmp_path, monkeypatch):
    """A station's page, or its ID, is BirdWeather's whatever is saved, and a
    path a camera's log; with no address given the saved type and its own
    address stand."""
    config = tmp_path / "settings.json"
    station = "https://app.birdweather.com/stations/1"
    SettingsStore(config).update(detector_kind=BIRDWEATHER, detector_station=station)
    ran = []
    monkeypatch.setattr(check, "run", lambda url, _u, _p, _dir, kind: ran.append((url, kind)) or 0)

    for argv in (
        ["31086"],
        ["31086,15982"],
        ["http://pi.local:8090"],
        ["pi.local:8090"],
        ["~/critter-cam/results/detections.log"],
        ["./detections.log"],
        [],
    ):
        detector = ["--detector", *argv] if argv else []
        monkeypatch.setattr(sys, "argv", ["fugleramme-check", "--config", str(config), *detector])
        try:
            check.main()
        except SystemExit:
            pass

    assert ran == [
        ("https://app.birdweather.com/stations/31086", BIRDWEATHER),
        (
            "https://app.birdweather.com/stations/31086, https://app.birdweather.com/stations/15982",
            BIRDWEATHER,
        ),
        ("http://pi.local:8090", BIRDNET_GO),
        ("http://pi.local:8090", BIRDNET_GO),  # no scheme is plain HTTP
        ("~/critter-cam/results/detections.log", CAMERA),
        ("./detections.log", CAMERA),
        (station, BIRDWEATHER),
    ]


def test_no_station_saved_is_said_without_asking(tmp_path, monkeypatch):
    config = tmp_path / "settings.json"
    SettingsStore(config).update(detector_kind=BIRDWEATHER)
    monkeypatch.setattr(check, "run", lambda *args: pytest.fail("asked BirdWeather"))
    monkeypatch.setattr(sys, "argv", ["fugleramme-check", "--config", str(config)])
    with pytest.raises(SystemExit, match="No BirdWeather station is saved"):
        check.main()

    SettingsStore(config).update(detector_kind=CAMERA)
    with pytest.raises(SystemExit, match="No camera log is saved"):
        check.main()
