"""What the frame reads off a wildlife camera's detection log.

The same invariant as the other detectors: a log that cannot be read raises,
and only one that holds nothing reads as no animals. Then what is the camera's
own: lines that are not detections, clips classified long after they were
recorded, and labels folded to artwork keys by an aliases file.
"""

from __future__ import annotations

import json
import time as systime
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fugleramme.camera import CameraSource
from fugleramme.source import Unavailable

CAT, DEER, VOLE = "domestic cat", "white-tailed deer", "cricetidae family"

# Real lines, one of each shape.
REAL = [
    "2026-10-21 20:03:13  driveway__2026-10-21t19-59-22-00-00  no-confident-animal  frames=0/26",
    "2026-10-21 20:17:57  driveway__2026-10-21t23-26-26-00-00  cricetidae family  best=0.96  frames=1/10",
    "2026-10-21 20:03:13  driveway__2026-10-21t19-59-22-00-00  no-frames-extracted",
]

NOW = datetime.now(UTC).replace(microsecond=0)


def _ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def _clip(camera: str, at: datetime) -> str:
    return f"{camera}__{at.astimezone(UTC):%Y-%m-%dt%H-%M-%S}-00-00"


def _logged(at: datetime) -> str:
    return f"{at.astimezone():%Y-%m-%d %H:%M:%S}"


def _seen(hours: float, label: str, best: float, camera: str, logged: float | None = None) -> str:
    """A detection recorded `hours` ago and classified `logged` hours ago,
    a few minutes after it was recorded unless said."""
    at = _ago(hours)
    stamp = _ago(logged) if logged is not None else at + timedelta(minutes=3)
    return f"{_logged(stamp)}  {_clip(camera, at)}  {label}  best={best:.2f}  frames=1/10"


def _nothing(hours: float, shape: str = "no-confident-animal  frames=0/26") -> str:
    at = _ago(hours)
    return f"{_logged(at)}  {_clip('driveway', at)}  {shape}"


# Two cameras across two days, in the order the pipeline classified them: line 5
# was recorded yesterday but logged minutes ago, and line 8 came in after line 7
# though recorded earlier.
LOG = [
    _nothing(1),
    _seen(20, DEER, 0.81, "backyard"),
    _seen(2, CAT, 0.97, "driveway"),
    _nothing(3, "no-frames-extracted"),
    _seen(30, CAT, 0.88, "driveway", logged=0.1),
    _seen(5, DEER, 0.75, "backyard"),
    _seen(1, VOLE, 0.96, "backyard"),
    _seen(4, CAT, 0.66, "driveway", logged=0.05),
]


def id_of(detection) -> int:
    return detection.id


def _write(tmp_path: Path, lines: list[str], name: str = "detections.log") -> Path:
    path = tmp_path / name
    path.write_text("".join(f"{line}\n" for line in lines))
    return path


@pytest.fixture
def camera(tmp_path):
    return CameraSource(_write(tmp_path, LOG))


def _asked(source: CameraSource) -> list:
    return [
        source.latest,
        source.life_list,
        source.stats,
        lambda: source.recent(5),
        lambda: source.species_since(6),
        lambda: source.species_since(0),
        lambda: source.last_heard(24),
    ]


def test_a_log_that_is_not_there_raises(tmp_path):
    for call in _asked(CameraSource(tmp_path / "detections.log")):
        with pytest.raises(Unavailable):
            call()


def test_an_empty_log_reads_as_no_animals(tmp_path):
    quiet = CameraSource(_write(tmp_path, []))
    assert quiet.latest() is None
    assert quiet.recent() == []
    assert quiet.species_since(6) == []
    assert quiet.species_since(0) == []
    assert quiet.last_heard(24) == []
    assert quiet.life_list() == []
    assert quiet.stats()["total"] == 0


def test_only_a_detection_line_is_a_detection(tmp_path):
    """The other two shapes are clips with nothing in them."""
    (only,) = CameraSource(_write(tmp_path, REAL)).recent()
    assert (only.id, only.scientific_name, only.confidence, only.clip_path) == (
        2,
        VOLE,
        0.96,
        "driveway__2026-10-21t23-26-26-00-00",
    )


@pytest.mark.skipif(not hasattr(systime, "tzset"), reason="needs time.tzset")
def test_a_detection_is_when_its_clip_was_recorded_in_the_frames_zone(tmp_path, monkeypatch):
    """The clip's own UTC time, not when it was logged three hours earlier on
    the wall clock; read off a name that says no time, the log's time."""
    garbled = REAL[1].replace("driveway__2026-10-21t23-26-26-00-00", "driveway__clip")
    path = _write(tmp_path, [REAL[1], garbled])
    monkeypatch.setenv("TZ", "America/New_York")
    systime.tzset()
    try:
        recorded, logged = (d.detected_at for d in sorted(CameraSource(path).recent(), key=id_of))
    finally:
        monkeypatch.undo()
        systime.tzset()
    eastern = ZoneInfo("America/New_York")
    assert recorded == datetime(2026, 10, 21, 19, 26, 26, tzinfo=eastern)
    assert logged == datetime(2026, 10, 21, 20, 17, 57, tzinfo=eastern)
    assert recorded.utcoffset() == logged.utcoffset() == timedelta(hours=-4)


def test_species_are_counted_over_the_window_most_seen_first(camera):
    """Yesterday's cat logged minutes ago is not in the last day."""
    assert camera.species_since(24) == [(CAT, 2), (DEER, 2), (VOLE, 1)]
    assert camera.species_since(3) == [(VOLE, 1), (CAT, 1)]  # a tie goes by name
    assert camera.species_since(0) == [(CAT, 3), (DEER, 2), (VOLE, 1)]


def test_last_heard_is_most_recent_first(camera):
    """By each species' newest detection: by its oldest, the deer would lead the cat."""
    assert camera.last_heard(24) == [VOLE, CAT, DEER]
    assert camera.last_heard(1.5) == [VOLE]
    assert camera.last_heard(0) == [VOLE, CAT, DEER]


def test_the_life_list_dates_each_species_to_its_first_detection(camera):
    life = camera.life_list()
    assert [(s.scientific_name, s.first_seen) for s in life] == [
        (CAT, _ago(30)),
        (DEER, _ago(20)),
        (VOLE, _ago(1)),
    ]


def test_recent_is_newest_recorded_first_not_the_logs_order(camera):
    assert [d.id for d in camera.recent(3)] == [7, 3, 8]
    assert [d.id for d in camera.recent()] == [7, 3, 8, 6, 2, 5]
    assert camera.latest() == camera.recent(1)[0]
    assert camera.latest().detected_at == _ago(1)


def test_stats_count_every_detection(camera):
    stats = camera.stats()
    assert (stats["total"], stats["species"], stats["last_24h"]) == (6, 3, 5)
    assert stats["top"][0] == {"scientific_name": CAT, "n": 3, "best": 0.97}


# -- aliases ------------------------------------------------------------------


def test_aliases_beside_the_log_fold_labels_whatever_their_case(tmp_path):
    log = _write(tmp_path, [_seen(1, "Eastern Cottontail", 0.9, "a"), *LOG])
    (tmp_path / "aliases.json").write_text(
        json.dumps({"CRICETIDAE family": "rabbit", "eastern cottontail": "rabbit"})
    )
    camera = CameraSource(log)
    assert camera.species_since(0) == [(CAT, 3), ("rabbit", 2), (DEER, 2)]
    assert camera.latest().scientific_name == "rabbit"
    assert [s.scientific_name for s in camera.life_list()] == [CAT, DEER, "rabbit"]


def test_aliases_named_elsewhere_are_read_from_there(tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({VOLE: "rabbit"}))
    (tmp_path / "aliases.json").write_text(json.dumps({CAT: "cat"}))
    camera = CameraSource(_write(tmp_path, LOG), aliases_path=elsewhere)
    assert camera.species_since(0) == [(CAT, 3), (DEER, 2), ("rabbit", 1)]


def test_aliases_that_cannot_be_read_leave_the_labels_as_they_are(tmp_path):
    (tmp_path / "aliases.json").write_text("{not json")
    assert CameraSource(_write(tmp_path, LOG)).species_since(0) == [(CAT, 3), (DEER, 2), (VOLE, 1)]


def test_the_log_and_aliases_are_read_again_when_they_change(tmp_path):
    log = _write(tmp_path, LOG)
    camera = CameraSource(log)
    assert camera.latest().scientific_name == VOLE

    (tmp_path / "aliases.json").write_text(json.dumps({VOLE: "rabbit"}))
    assert camera.latest().scientific_name == "rabbit"

    with log.open("a") as appended:
        appended.write(_seen(0.5, CAT, 0.9, "driveway") + "\n")
    assert camera.latest().scientific_name == CAT
