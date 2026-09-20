"""Slice 0 of docs/plans/2026-09-08-attention-environment.md: the guardrails,
written before the feature exists.

Every test here pins something the evidence review decided, so that a later
edit that "just makes the audio a bit louder" or "adds a nice track" fails
rather than quietly undoing the reason the feature was built:

- no lyrics and no speech, ever (the strongest negative finding in the review),
  made structural by the catalogue being procedural specs rather than files;
- a gain ceiling the UI cannot exceed;
- every layer ends with a fade, never a hard stop;
- a break cue inside 30 minutes, always (headset eye strain, and the
  micro-break meta-analysis);
- the evening theme can only darken and warm, never brighten.
"""
from datetime import UTC, datetime, timedelta

import pytest

from companion import focus


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 8, hour, minute, tzinfo=UTC)


class TestCatalogueIsProcedural:
    """The 'no lyrics' rule is a property of the catalogue, not a prompt: there
    is nowhere to put a song, because a condition is a synthesis spec."""

    def test_every_condition_is_a_known_procedural_kind(self):
        assert focus.CONDITIONS, "catalogue must not be empty"
        for name, spec in focus.CONDITIONS.items():
            assert spec["kind"] in focus.PROCEDURAL_KINDS, f"{name} is not procedural"

    def test_no_condition_can_reference_a_file_or_url(self):
        for name, spec in focus.CONDITIONS.items():
            for key, value in spec.items():
                assert key not in ("url", "file", "path", "src", "track"), f"{name}.{key} points at media"
                if isinstance(value, str):
                    assert "://" not in value and "/" not in value, f"{name}.{key} looks like a media reference"

    def test_the_four_experiment_arms_are_present(self):
        # The blinded n=1 experiment needs exactly these arms; silence is the control.
        assert set(focus.CONDITIONS) == {"silence", "noise_mask", "modulated", "binaural"}


class TestGainCeiling:
    def test_a_plan_above_the_ceiling_is_refused(self):
        with pytest.raises(ValueError, match="gain"):
            focus.FocusPlan(condition="noise_mask", minutes=50, break_every_minutes=25,
                            gain=focus.GAIN_CEILING + 0.01, fade_seconds=3.0, evening=False, spec={"kind": "noise"})

    def test_the_planner_never_exceeds_the_ceiling(self):
        planner = focus.ScheduledPlanner()
        for n in range(12):
            plan = planner.plan(now=_dt(10), completed=n, minutes=50)
            assert plan.gain <= focus.GAIN_CEILING

    def test_silence_asks_for_no_gain_at_all(self):
        plan = focus.ScheduledPlanner().plan(now=_dt(10), completed=0, minutes=50, condition="silence")
        assert plan.gain == 0.0


class TestBreakCue:
    @pytest.mark.parametrize("minutes", [5, 25, 30, 50, 90, 120])
    def test_a_break_is_always_cued_within_the_limit(self, minutes):
        plan = focus.ScheduledPlanner().plan(now=_dt(10), completed=0, minutes=minutes)
        assert 0 < plan.break_every_minutes <= focus.MAX_MINUTES_WITHOUT_BREAK

    def test_a_plan_with_too_long_a_gap_is_refused(self):
        with pytest.raises(ValueError, match="break"):
            focus.FocusPlan(condition="silence", minutes=90, break_every_minutes=45,
                            gain=0.0, fade_seconds=3.0, evening=False, spec={"kind": "silence"})

    def test_break_offsets_cover_the_block_and_stop_before_the_end(self):
        plan = focus.ScheduledPlanner().plan(now=_dt(10), completed=0, minutes=50)
        assert plan.break_offsets == [25]
        long_plan = focus.ScheduledPlanner().plan(now=_dt(10), completed=0, minutes=120)
        assert long_plan.break_offsets == [25, 50, 75, 100]
        assert max(long_plan.break_offsets) < 120

    def test_a_block_longer_than_the_maximum_is_refused(self):
        with pytest.raises(ValueError, match="minutes"):
            focus.ScheduledPlanner().plan(now=_dt(10), completed=0, minutes=focus.MAX_BLOCK_MINUTES + 1)


class TestFadeAlways:
    def test_every_plan_carries_a_real_fade(self):
        planner = focus.ScheduledPlanner()
        for n in range(8):
            assert planner.plan(now=_dt(14), completed=n, minutes=50).fade_seconds >= focus.MIN_FADE_SECONDS

    def test_a_plan_without_a_fade_is_refused(self):
        with pytest.raises(ValueError, match="fade"):
            focus.FocusPlan(condition="noise_mask", minutes=50, break_every_minutes=25,
                            gain=0.2, fade_seconds=0.0, evening=False, spec={"kind": "noise"})


class TestEveningNeverBrightens:
    def test_evening_theme_is_dimmer_and_warmer_than_day(self):
        day, evening = focus.theme_for(_dt(10)), focus.theme_for(_dt(23))
        assert evening.brightness <= day.brightness
        assert evening.warmth >= day.warmth

    def test_wind_down_hour_switches_the_theme(self):
        assert focus.theme_for(_dt(focus.WIND_DOWN_HOUR - 1)).evening is False
        assert focus.theme_for(_dt(focus.WIND_DOWN_HOUR)).evening is True

    def test_a_plan_started_in_the_evening_is_marked_evening(self):
        assert focus.ScheduledPlanner().plan(now=_dt(22), completed=0, minutes=50).evening is True
        assert focus.ScheduledPlanner().plan(now=_dt(11), completed=0, minutes=50).evening is False


class TestScheduleIsBalancedAndBlind:
    """A four-arm n=1 experiment is only readable if the arms are balanced and
    the order does not correlate with the time of day or with Duc's mood."""

    def test_every_cycle_of_four_contains_each_condition_once(self):
        planner = focus.ScheduledPlanner()
        for cycle in range(5):
            picked = [planner.plan(now=_dt(10), completed=cycle * 4 + i, minutes=50).condition for i in range(4)]
            assert sorted(picked) == sorted(focus.CONDITIONS)

    def test_the_order_is_not_the_same_every_cycle(self):
        planner = focus.ScheduledPlanner()
        cycles = {
            tuple(planner.plan(now=_dt(10), completed=c * 4 + i, minutes=50).condition for i in range(4))
            for c in range(6)
        }
        assert len(cycles) > 1, "a fixed order lets the time of day track the condition"

    def test_the_schedule_is_reproducible(self):
        a = [focus.ScheduledPlanner().plan(now=_dt(10), completed=n, minutes=50).condition for n in range(8)]
        b = [focus.ScheduledPlanner().plan(now=_dt(10), completed=n, minutes=50).condition for n in range(8)]
        assert a == b


class TestStore:
    def test_start_end_round_trip(self, tmp_path):
        store = focus.FocusStore(tmp_path / "focus.db")
        s = store.start(condition="noise_mask", minutes=50, task="resume tailoring")
        assert s.id > 0 and s.ended_at is None
        assert store.active() is not None
        ended = store.end(s.id, rating=4, note="steady")
        assert ended.ended_at is not None and ended.rating == 4 and ended.note == "steady"
        assert store.active() is None

    def test_probe_numbers_are_stored_per_phase(self, tmp_path):
        store = focus.FocusStore(tmp_path / "focus.db")
        s = store.start(condition="silence", minutes=25, task="reading")
        store.record_probe(s.id, phase="start", median_ms=280.5, lapses=1)
        store.record_probe(s.id, phase="end", median_ms=310.0, lapses=3)
        row = store.get(s.id)
        assert row.probe_start_ms == 280.5 and row.probe_start_lapses == 1
        assert row.probe_end_ms == 310.0 and row.probe_end_lapses == 3

    def test_an_unknown_phase_is_refused(self, tmp_path):
        store = focus.FocusStore(tmp_path / "focus.db")
        s = store.start(condition="silence", minutes=25, task="reading")
        with pytest.raises(ValueError, match="phase"):
            store.record_probe(s.id, phase="middle", median_ms=1.0, lapses=0)

    def test_starting_while_one_is_running_is_refused(self, tmp_path):
        store = focus.FocusStore(tmp_path / "focus.db")
        store.start(condition="silence", minutes=25, task="a")
        with pytest.raises(focus.FocusBlockRunning):
            store.start(condition="silence", minutes=25, task="b")

    def test_an_unknown_condition_is_refused(self, tmp_path):
        store = focus.FocusStore(tmp_path / "focus.db")
        with pytest.raises(ValueError, match="condition"):
            store.start(condition="lofi hip hop radio", minutes=25, task="a")

    def test_completed_counts_only_finished_blocks(self, tmp_path):
        store = focus.FocusStore(tmp_path / "focus.db")
        assert store.completed_count() == 0
        s = store.start(condition="silence", minutes=25, task="a")
        assert store.completed_count() == 0
        store.end(s.id, rating=3, note="")
        assert store.completed_count() == 1

    def test_a_stale_block_is_abandoned_not_counted(self, tmp_path):
        """Duc closes the tab mid-block. The next start must not be blocked
        forever, and an abandoned block must not enter the experiment."""
        store = focus.FocusStore(tmp_path / "focus.db")
        s = store.start(condition="silence", minutes=25, task="a")
        old = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
        store._force_started_at(s.id, old)  # only a test reaches in like this
        assert store.active() is None
        fresh = store.start(condition="noise_mask", minutes=25, task="b")
        assert fresh.id != s.id
        assert store.get(s.id).abandoned is True
        assert store.completed_count() == 0


class TestRating:
    def test_a_rating_outside_one_to_five_is_refused(self, tmp_path):
        store = focus.FocusStore(tmp_path / "focus.db")
        s = store.start(condition="silence", minutes=25, task="a")
        with pytest.raises(ValueError, match="rating"):
            store.end(s.id, rating=9, note="")

    def test_a_rating_is_optional(self, tmp_path):
        store = focus.FocusStore(tmp_path / "focus.db")
        s = store.start(condition="silence", minutes=25, task="a")
        assert store.end(s.id, rating=None, note="").rating is None


class TestReport:
    """The report's job is to refuse to over-claim. These pin the refusals, not
    the arithmetic - the same discipline the router and search evals record: a
    gap smaller than the within-arm spread is a tie, and saying otherwise is how
    a project convinces itself of something twice."""

    def _session(self, condition, start_ms, end_ms, rating=3, n=1):
        return [
            focus.FocusSession(
                id=i, condition=condition, task="", planned_minutes=50,
                started_at=f"2026-09-0{(i % 8) + 1}T10:00:00+00:00", ended_at="2026-09-01T11:00:00+00:00",
                abandoned=False, probe_start_ms=start_ms, probe_start_lapses=0,
                probe_end_ms=end_ms, probe_end_lapses=2, rating=rating, note="",
            )
            for i in range(1, n + 1)
        ]

    def test_an_empty_store_reports_nothing_readable(self):
        report = focus.build_report([])
        assert report.total_blocks == 0
        assert report.readable is False
        assert "Not readable yet" in report.verdict()

    def test_every_arm_appears_even_with_no_data(self):
        assert {a.condition for a in focus.build_report([]).arms} == set(focus.CONDITIONS)

    def test_too_few_blocks_refuses_to_rank(self):
        sessions = []
        for c in focus.CONDITIONS:
            sessions += self._session(c, 300, 320, n=3)
        report = focus.build_report(sessions, min_blocks_per_arm=8)
        assert report.readable is False
        assert "fewer than 8 blocks" in report.verdict()

    def test_a_gap_inside_the_spread_is_called_a_tie(self):
        """The finding this whole file exists to prevent: a 10 ms lead over noisy
        blocks read as a winner."""
        sessions = []
        for c in focus.CONDITIONS:
            # Wide within-arm spread, tiny between-arm difference.
            for delta in (0, 40, 80, 120, 160, 200, 240, 280):
                sessions += self._session(c, 300, 300 + delta + (5 if c == "silence" else 0), n=1)
        report = focus.build_report(sessions, min_blocks_per_arm=8)
        assert report.readable is True
        assert "not separated" in report.verdict()

    def test_a_gap_outside_the_spread_is_reported_as_a_lead(self):
        sessions = []
        for c in focus.CONDITIONS:
            base = 0 if c == "noise_mask" else 400   # one arm far better, all arms tight
            for jitter in (0, 2, 4, 6, 8, 10, 12, 14):
                sessions += self._session(c, 300, 300 + base + jitter, n=1)
        report = focus.build_report(sessions, min_blocks_per_arm=8)
        assert report.readable is True
        assert "noise_mask is ahead" in report.verdict()

    def test_abandoned_blocks_never_enter_the_report(self):
        good = self._session("silence", 300, 310, n=1)
        junk = self._session("silence", 300, 9999, n=1)
        junk[0].abandoned = True
        report = focus.build_report(good + junk)
        assert report.total_blocks == 1
        arm = next(a for a in report.arms if a.condition == "silence")
        assert arm.blocks == 1 and arm.median_delta_ms == 10

    def test_the_rendered_report_always_carries_its_caveats(self):
        text = focus.render_report(focus.build_report([]))
        assert "n=1" in text
        assert "Blind by convention" in text
        assert "interquartile range" in text.lower() or "spread" in text.lower()

    def test_a_single_block_has_no_spread_rather_than_a_spread_of_zero(self):
        report = focus.build_report(self._session("silence", 300, 320, n=1))
        arm = next(a for a in report.arms if a.condition == "silence")
        assert arm.median_delta_ms == 20
        assert arm.delta_spread_ms is None, "one block cannot have a noise floor of zero"


class TestToolsSayOnlyWhatIsTrue:
    def test_a_block_with_no_mid_block_cue_does_not_promise_one(self, tmp_path):
        """Found on a real Claude turn: reporting the 25-minute *interval* for a
        25-minute block made her tell Duc about a break cue that never fires."""
        tool = focus.StartFocusBlockTool(focus.FocusStore(tmp_path / "f.db"))
        assert tool.run(minutes=25, task="a")["break_cues_at_minutes"] == []

    def test_a_longer_block_lists_every_cue(self, tmp_path):
        tool = focus.StartFocusBlockTool(focus.FocusStore(tmp_path / "f.db"))
        assert tool.run(minutes=90, task="a")["break_cues_at_minutes"] == [25, 50, 75]

    def test_starting_never_names_the_arm(self, tmp_path):
        out = focus.StartFocusBlockTool(focus.FocusStore(tmp_path / "f.db")).run(minutes=50, task="a")
        assert "condition" not in out and not any(c in str(out) for c in focus.CONDITIONS)

    def test_ending_does_name_the_arm(self, tmp_path):
        store = focus.FocusStore(tmp_path / "f.db")
        focus.StartFocusBlockTool(store).run(minutes=50, task="a")
        assert focus.EndFocusBlockTool(store).run(rating=4, note="ok")["condition"] in focus.CONDITIONS

    def test_ending_nothing_is_an_error_not_a_crash(self, tmp_path):
        assert "error" in focus.EndFocusBlockTool(focus.FocusStore(tmp_path / "f.db")).run()

    def test_status_with_nothing_running(self, tmp_path):
        out = focus.FocusStatusTool(focus.FocusStore(tmp_path / "f.db")).run()
        assert out == {"running": False, "completed_blocks": 0}
