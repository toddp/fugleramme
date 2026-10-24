"""Test that a detector - a BirdNET-Go, BirdWeather stations or a camera log -
returns everything the frame needs.

examples:
  fugleramme-check
      test the detector saved on the admin page
  fugleramme-check --detector http://pi.local:8090 --password secret
      test another detector, without saving it
  fugleramme-check --detector https://app.birdweather.com/stations/12345
      test a BirdWeather station, which its ID alone also names
  fugleramme-check --detector 12345,67890
      test several BirdWeather stations, read as one
  fugleramme-check --detector ~/critter-cam/results/detections.log
      test a wildlife camera's detection log

Set FUGLERAMME_LOG_LEVEL=DEBUG to also log every request it makes.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import languages
from .api import ApiSource, probe
from .birdweather import BirdWeatherSource
from .camera import CameraSource
from .config import DEFAULT_CONFIG_PATH, DEFAULT_DETECTOR_URL
from .settings import SettingsStore, from_env
from .source import (
    BIRDNET_GO,
    BIRDWEATHER,
    CAMERA,
    Source,
    Unavailable,
    station_id,
    station_pages,
    with_scheme,
)

_OK, _FAIL = "ok  ", "FAIL"


def _describe(value: Any) -> str:
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return ", ".join(value)
    if isinstance(value, list):
        return f"{len(value)} species"
    return str(value)


def _line(ok: bool, label: str, detail: str, started: float) -> None:
    took = time.monotonic() - started
    print(f"{_OK if ok else _FAIL} {label:<22} {detail} ({took:.1f}s)")


def _check(label: str, run) -> bool:
    started = time.monotonic()
    try:
        _line(True, label, _describe(run()), started)
        return True
    except Unavailable as error:
        _line(False, label, str(error), started)
    except Exception as error:  # a shape the frame cannot read is a failure too
        _line(False, label, f"{type(error).__name__}: {error}", started)
    return False


def _latest(source: Source) -> str:
    newest = source.latest()
    return "none" if newest is None else f"{newest.scientific_name} at {newest.detected_at}"


def _first(source: Source) -> str:
    life = source.life_list()
    return f"{len(life)} species, earliest {life[0].first_seen.date()}" if life else "none"


def _languages(cache_dir: Path) -> list[str]:
    """The selectable languages, or why there are none. Listing "sci" as a pass
    would report the exact failure this check exists to catch as working."""
    offered = sorted(languages.catalog(cache_dir))
    failure = languages.catalog_failure()
    if failure:
        raise Unavailable(failure)
    return offered


def _stations(source: BirdWeatherSource) -> int:
    """A line per station, as the admin's Detector row lists them; how many failed."""
    started = time.monotonic()
    try:
        about = source.about()
    except Unavailable as error:
        _line(False, "reachable", str(error), started)
        return 1
    for id_, row in about.items():
        named = str(row.get("name") or "") if row else "no such station"
        _line(row is not None, f"station {id_}", named, started)
    return sum(row is None for row in about.values())


def _source(
    url: str, username: str, password: str, kind: str
) -> ApiSource | BirdWeatherSource | CameraSource:
    if kind == BIRDWEATHER:
        return BirdWeatherSource(url)
    if kind == CAMERA:
        return CameraSource(url)
    return ApiSource(url, username, password)


def _kind_of(detector: str) -> tuple[str, str]:
    """(address, kind) for a `--detector`, read as what it says: a path is a
    camera log, a station's page or ID BirdWeather's, anything else BirdNET-Go."""
    if detector.startswith(("/", "~", ".")):
        return detector, CAMERA
    pages = station_pages(detector)
    if all(station_id(page) for page in pages):
        return ", ".join(pages), BIRDWEATHER
    return with_scheme(detector), BIRDNET_GO


def run(url: str, username: str, password: str, cache_dir: Path, kind: str = BIRDNET_GO) -> int:
    source = _source(url, username, password, kind)
    languages.use(source)
    print(f"{url}\n")
    if isinstance(source, BirdWeatherSource):
        unreached = _stations(source)
    else:
        started = time.monotonic()
        # The admin's own test, not a bare /health: it reads what the frame reads.
        # "names" is reachable - the locale list it holds back is the check below.
        state, detail = probe(url, username, password, kind)
        reachable = state in ("ok", "names")
        _line(reachable, "reachable", detail or "answered", started)
        unreached = int(not reachable)
    checks = [
        ("species, 6 hours", lambda: source.species_since(6)),
        ("species, 24 hours", lambda: source.species_since(24)),
        ("species, all time", lambda: source.species_since(0)),
        ("latest detection", lambda: _latest(source)),
        ("life list", lambda: _first(source)),
        ("name languages", lambda: _languages(cache_dir)),
    ]
    passed = [_check(label, run_one) for label, run_one in checks]
    failed = passed.count(False) + unreached
    print(f"\n{failed} failed" if failed else "\nall good")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--detector",
        help="base URL, BirdWeather stations' pages or IDs separated by commas, or a "
        f"camera log's path (default: saved, or {DEFAULT_DETECTOR_URL})",
    )
    parser.add_argument("--username", default="", help="only if BirdNET-Go's client id was changed")
    parser.add_argument("--password", default="", help="BirdNET-Go's password (default: saved)")
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH, help="settings file to read"
    )
    args = parser.parse_args()
    logging.basicConfig(format="%(levelname)s %(message)s")
    logging.getLogger("fugleramme").setLevel(os.environ.get("FUGLERAMME_LOG_LEVEL", "WARNING"))

    # Same seeds as the service, so the check reports the detector it would use.
    saved = SettingsStore(args.config, from_env()).get()
    url, kind = (
        _kind_of(args.detector) if args.detector else (saved.detector_address, saved.detector_kind)
    )
    if not url:
        unset = "camera log" if kind == CAMERA else "BirdWeather station"
        sys.exit(f"No {unset} is saved: name one with --detector")
    sys.exit(
        run(
            url,
            args.username or saved.detector_username,
            args.password or saved.detector_password,
            args.config.parent,
            kind,
        )
    )


if __name__ == "__main__":
    main()
