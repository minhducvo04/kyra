"""The Health export summary reports shape (counts, days, sample gaps), never a reading."""
import zipfile

import pytest

from companion.health_export import find_export, summarize

HR = "HKQuantityTypeIdentifierHeartRate"
WATCH = "&lt;&lt;HKDevice: 0x1&gt;, name:Apple Watch, manufacturer:Apple Inc., model:Watch, hardware:Watch7,5, software:27.0&gt;"


def _record(kind: str, start: str, value: str = "61", device: str = WATCH) -> str:
    return (
        f'<Record type="{kind}" sourceName="Synthetic Watch" device="{device}" unit="count/min" '
        f'startDate="2030-01-01 {start} -0500" endDate="2030-01-01 {start} -0500" value="{value}"/>'
    )


def _export(tmp_path, body: str):
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("apple_health_export/export.xml", f"<HealthData>{body}</HealthData>")
    return path


def test_counts_each_type_with_its_day_range(tmp_path):
    body = _record(HR, "08:00:00") + _record(HR, "08:05:00") + _record("HKQuantityTypeIdentifierStepCount", "09:00:00")
    summary = summarize(_export(tmp_path, body))
    assert summary["types"][HR] == {"count": 2, "first_day": "2030-01-01", "last_day": "2030-01-01"}
    assert summary["types"]["HKQuantityTypeIdentifierStepCount"]["count"] == 1
    assert summary["watch_hardware"] == ["Watch7,5"]


def test_heart_rate_gaps_split_by_workout(tmp_path):
    # Outside a workout: 08:00, 08:05 and 08:10, two 300 s gaps. Inside 10:00 to 10:01: samples 5 s apart.
    body = (
        _record(HR, "08:00:00") + _record(HR, "08:05:00") + _record(HR, "08:10:00")
        + _record(HR, "10:00:00") + _record(HR, "10:00:05") + _record(HR, "10:00:10")
        + '<Workout workoutActivityType="HKWorkoutActivityTypeOther" '
        'startDate="2030-01-01 10:00:00 -0500" endDate="2030-01-01 10:01:00 -0500"/>'
    )
    gaps = summarize(_export(tmp_path, body))["heart_rate_gaps"]
    assert gaps["in_workout"]["median_s"] == 5
    assert gaps["in_workout"]["count"] == 2
    assert gaps["outside_workout"]["median_s"] == 300
    # The 08:10 to 10:00 jump is a gap outside a workout too, and the largest.
    assert gaps["outside_workout"]["max_s"] == 6600


def test_phone_heart_rate_records_are_not_counted_as_watch_gaps(tmp_path):
    body = _record(HR, "08:00:00", device="&lt;&lt;HKDevice: 0x2&gt;, name:iPhone, hardware:iPhone18,2&gt;")
    summary = summarize(_export(tmp_path, body))
    assert summary["heart_rate_gaps"]["outside_workout"]["count"] == 0
    assert summary["watch_hardware"] == []


def test_no_reading_appears_in_the_summary(tmp_path):
    body = _record(HR, "08:00:00", value="173") + _record(HR, "08:05:00", value="174")
    assert "173" not in repr(summarize(_export(tmp_path, body)))


def test_find_export_accepts_the_zip_or_the_unzipped_folder(tmp_path):
    health = tmp_path / "health"
    (health / "apple_health_export").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        find_export(health)
    xml = health / "apple_health_export" / "export.xml"
    xml.write_text("<HealthData/>")
    assert find_export(health) == xml
    zipped = _export(health, "")
    assert find_export(health) == zipped
