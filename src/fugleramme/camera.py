"""A wildlife camera's detection log as the frame's detection source.

A camera pipeline that classifies motion clips with SpeciesNet appends a line
per clip to its `detections.log`, the fields two spaces apart:

    2026-10-21 20:17:57  driveway__2026-10-21t23-26-26-00-00  cricetidae family  best=0.96  frames=1/10
    2026-10-21 20:03:13  driveway__2026-10-21t19-59-22-00-00  no-confident-animal  frames=0/26
    2026-10-21 20:03:13  driveway__2026-10-21t19-59-22-00-00  no-frames-extracted

Only the first shape is a detection: when it was logged in the pipeline's local
time, the clip, the label, the best score and the frames that held the animal.
A clip is named `<camera>__<when>`, `<when>` being Blink's UTC time with its
punctuation turned to hyphens. That is when the animal was there, so it is the
detection's time: a clip can be classified hours after it was recorded.

A label is a common name or a taxon ("white-tailed deer", "cricetidae family"),
never a scientific name, and most are not birds, so this source applies no bird
filter (`taxa.is_bird` would drop every one). An animals file beside the log
folds labels to scientific names, the key the artwork, the body masses and the
label dictionaries share with every other detector, and names each animal:

    {"Sylvilagus floridanus": {"name": "rabbit",
                               "labels": ["eastern cottontail", "cricetidae family"]}}

A label no record claims passes through as it is ("mammal"). The names are the
yard's own and English, so `common_names` offers them as English; the label
shows the scientific name only under SCIENTIFIC.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .source import CAMERA, Detection, Species, Unavailable

log = logging.getLogger(__name__)

# Where the animals are read from unless named: beside the log.
ANIMALS = "animals.json"

_LINE = re.compile(
    r"(?P<logged>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)  (?P<clip>\S+)  (?P<label>.+?)"
    r"  best=(?P<best>[\d.]+)  frames=\d+/\d+"
)
# 2026-10-21T23:26:26+00:00 as the clip name slugs it. The slug drops the
# offset's sign, so only UTC's own reads unambiguously.
_RECORDED = re.compile(r".+__(\d{4})-(\d\d)-(\d\d)t(\d\d)-(\d\d)-(\d\d)-00-00")


def _recorded(clip: str, logged: str) -> datetime:
    """When the clip was recorded, in the frame's zone, or when it was logged
    for a clip whose name does not say."""
    found = _RECORDED.fullmatch(clip)
    if found:
        try:
            year, month, day, hour, minute, second = map(int, found.groups())
            return datetime(year, month, day, hour, minute, second, tzinfo=UTC).astimezone()
        except ValueError:
            pass
    return datetime.fromisoformat(logged).astimezone()


def _animals(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """({lowercased label: scientific name}, {scientific name: common name}).
    Optional: no file, or one that cannot be read, leaves every label as the
    camera gave it and names nothing. Keys starting with "_" are comments; a
    record that is not one is skipped, so one typo costs only its animal."""
    try:
        loaded = json.loads(path.read_text())
    except FileNotFoundError:
        return {}, {}
    except (OSError, ValueError) as error:
        log.warning("Ignoring the camera's animals in %s: %s", path, error)
        return {}, {}
    if not isinstance(loaded, dict):
        log.warning("Ignoring the camera's animals in %s: not a JSON object", path)
        return {}, {}
    folds: dict[str, str] = {}
    names: dict[str, str] = {}
    for key, record in loaded.items():
        scientific = key.strip()
        if not scientific or scientific.startswith("_") or not isinstance(record, dict):
            continue
        name, labels = record.get("name"), record.get("labels")
        if not isinstance(name, str) or not name.strip():
            continue
        names[scientific] = name.strip()
        for label in labels if isinstance(labels, list) else ():
            if isinstance(label, str) and label.strip():
                folds[label.strip().lower()] = scientific
    return folds, names


def _parse(text: str, folds: dict[str, str]) -> tuple[Detection, ...]:
    """The log's detections in line order, each numbered by its line."""
    detections = []
    for number, line in enumerate(text.splitlines(), 1):
        found = _LINE.fullmatch(line.rstrip())
        if found is None:
            continue  # a clip with nothing in it, or a line still being written
        try:
            at = _recorded(found["clip"], found["logged"])
            best = float(found["best"])
        except ValueError:
            continue
        label = found["label"].strip()
        name = folds.get(label.lower(), label)
        detections.append(Detection(number, at, name, best, found["clip"]))
    return tuple(detections)


class CameraSource:
    """A `source.Source` over one camera pipeline's detection log."""

    kind = CAMERA

    def __init__(self, path: str | Path, animals_path: str | Path | None = None):
        self.base_url = str(path)
        # Nothing to sign in to, so which log is all that tells two sources apart.
        self.station = self.base_url
        self._path = Path(path).expanduser()
        self._animals = (
            Path(animals_path).expanduser() if animals_path else self._path.with_name(ANIMALS)
        )
        self._held: tuple[tuple, tuple[Detection, ...]] | None = None
        self._named: tuple[int | None, dict[str, str]] | None = None

    def _version(self) -> tuple:
        try:
            logged = self._path.stat()
        except OSError as error:
            raise Unavailable(f"{self._path} unreadable: {error}") from error
        return logged.st_ino, logged.st_size, logged.st_mtime_ns, self._animals_version()

    def _animals_version(self) -> int | None:
        try:
            return self._animals.stat().st_mtime_ns
        except OSError:
            return None

    def _detections(self) -> tuple[Detection, ...]:
        """Parsed again only when the log or the animals change, since one page
        asks for them several times over."""
        version = self._version()
        if self._held is not None and self._held[0] == version:
            return self._held[1]
        try:
            text = self._path.read_text(errors="replace")
        except OSError as error:
            raise Unavailable(f"{self._path} unreadable: {error}") from error
        detections = _parse(text, _animals(self._animals)[0])
        self._held = (version, detections)
        return detections

    def _since(self, hours: float) -> list[Detection]:
        if hours <= 0:
            return list(self._detections())
        since = datetime.now(UTC) - timedelta(hours=hours)
        return [d for d in self._detections() if d.detected_at >= since]

    # -- source ------------------------------------------------------------

    def latest(self) -> Detection | None:
        newest = self.recent(1)
        return newest[0] if newest else None

    def recent(self, limit: int = 20) -> list[Detection]:
        """Newest recorded first, which is not the log's order."""
        newest = sorted(self._detections(), key=lambda d: (d.detected_at, d.id), reverse=True)
        return newest[:limit]

    def species_since(self, hours: float = 24) -> list[tuple[str, int]]:
        counts = Counter(d.scientific_name for d in self._since(hours))
        return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))

    def last_heard(self, hours: float = 24) -> list[str]:
        last: dict[str, datetime] = {}
        for d in self._since(hours):
            last[d.scientific_name] = max(last.get(d.scientific_name, d.detected_at), d.detected_at)
        return sorted(last, key=lambda name: (-last[name].timestamp(), name))

    def life_list(self) -> list[Species]:
        first: dict[str, datetime] = {}
        for d in self._detections():
            first[d.scientific_name] = min(
                first.get(d.scientific_name, d.detected_at), d.detected_at
            )
        species = [Species(name, at) for name, at in first.items()]
        return sorted(species, key=lambda s: (s.first_seen, s.scientific_name))

    def stats(self) -> dict:
        ever = self.species_since(0)
        best: dict[str, float] = {}
        for d in self._detections():
            best[d.scientific_name] = max(best.get(d.scientific_name, 0.0), d.confidence)
        return {
            "total": sum(n for _name, n in ever),
            "species": len(ever),
            "last_24h": sum(n for _name, n in self.species_since(24)),
            "top": [{"scientific_name": name, "n": n, "best": best[name]} for name, n in ever[:10]],
        }

    def common_names(self) -> dict[str, dict[str, str]]:
        """{"en": {scientific name: common name}} off the animals file, which
        `languages` offers as English; {} with no file. Read again only when
        the file changes, since every render asks."""
        version = self._animals_version()
        if self._named is None or self._named[0] != version:
            self._named = (version, _animals(self._animals)[1])
        names = self._named[1]
        return {"en": dict(names)} if names else {}

    def close(self) -> None:
        """Nothing held open: every call reads the file."""
