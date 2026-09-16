"""The hand-kept feature checklist and its slice-weighted progress."""
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from companion.paths import PROJECT_ROOT

STATUSES = ("planned", "red", "building", "done")


@dataclass(frozen=True)
class Slice:
    id: str
    name: str
    status: str


@dataclass(frozen=True)
class Feature:
    id: str
    name: str
    depends_on: list[str]
    slices: list[Slice]

    @property
    def percent(self) -> int:
        return _percent(self.slices)


def _percent(slices: list[Slice]) -> int:
    return round(100 * sum(s.status == "done" for s in slices) / len(slices)) if slices else 0


def load_features(path: Path = PROJECT_ROOT / "docs/features.yaml") -> list[Feature]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    features = []
    feature_ids, slice_ids = set(), set()
    for item in data["features"]:
        if item["id"] in feature_ids:
            raise ValueError(f"Duplicate feature id: {item['id']}")
        feature_ids.add(item["id"])
        slices = []
        for entry in item["slices"]:
            if entry["id"] in slice_ids:
                raise ValueError(f"Duplicate slice id: {entry['id']}")
            if entry["status"] not in STATUSES:
                raise ValueError(f"Unknown slice status: {entry['status']}")
            slice_ids.add(entry["id"])
            slices.append(Slice(**entry))
        features.append(Feature(item["id"], item["name"], item["depends_on"], slices))
    for feature in features:
        for dependency in feature.depends_on:
            if dependency not in feature_ids:
                raise ValueError(f"Unknown dependency for {feature.id}: {dependency}")
    return features


def feature_map(features: list[Feature]) -> dict:
    rows = []
    for feature in features:
        status = "planned"
        if feature.percent == 100:
            status = "done"
        elif any(s.status in ("building", "red") for s in feature.slices):
            status = "building"
        rows.append({**asdict(feature), "percent": feature.percent, "status": status})
    return {"features": rows, "overall": _percent([s for f in features for s in f.slices])}
