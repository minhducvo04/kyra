"""Focus blocks: the daytime half of the attention arc.

Plan and the evidence behind every constant here:
docs/plans/2026-09-08-attention-environment.md.

The short version of that review, because it is why this module looks the way
it does. Almost everything that reliably helps attention is a *removal*: no
lyrics, no speech, no evening blue light, no ninety minutes without a break.
The additions people sell - broadband noise, amplitude-modulated music,
binaural beats - are small, heterogeneous or single-study, and the largest of
them (noise) has opposite signs for listeners with and without attention
difficulties. A population effect of g = 0.25 that is negative for half the
population says nothing about one person.

So this module does two separate things and keeps them separate:

1. **The removals are post-conditions.** The catalogue is a set of synthesis
   specs rather than a folder of tracks, so there is nowhere for a song with
   lyrics to live; the gain the browser is allowed to use comes from the
   server, not from a slider; every layer fades; every plan cues a break inside
   MAX_MINUTES_WITHOUT_BREAK; the evening theme can only darken and warm.
   tests/test_focus.py fails if any of that is loosened.

2. **The additions are an experiment**, not a feature. `ScheduledPlanner`
   assigns one of four arms per block in a balanced, reproducible, shuffled
   order, a reaction-time probe runs at both ends, and
   scripts/focus_report.py reports per condition with the noise floor stated.
   Nothing here claims a benefit; the report is what would earn one.

Blinding, honestly: the plan the browser receives has to carry the synthesis
spec, because the browser is what makes the sound. Duc is blind by convention
(the HUD does not name the arm until the block ends), not by construction - he
could read the network response if he wanted to. `focus_report.py` says so
next to its numbers.

`FocusPlanner` is the usual Strategy shape: `ScheduledPlanner` runs the
experiment now, and a planner that has learned Duc's answers can replace it
later without touching the tools or the endpoints.
"""
from __future__ import annotations

import random
import statistics
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, insert, select, update

from companion.db import engine_for_store
from companion.paths import DATA_DIR
from companion.schema import focus_sessions as FS
from companion.tools import Tool

DB_PATH = DATA_DIR / "focus.db"

# --- The guardrails. Every number here is a finding, not a preference. -------

# Software gain is not sound pressure: the app cannot know Duc's system volume
# or his headphones' sensitivity, so this bounds what the app may *ask* for and
# the rest is his volume knob. It exists because the one broadband-noise result
# that favours a typical listener used ~45 dB against office noise, and because
# a focus layer that competes with thinking is the failure mode.
GAIN_CEILING = 0.30

# Apple's guidance for the Vision Pro is a break every 20-30 minutes (eye strain
# from the vergence-accommodation conflict and a lowered blink rate), and the
# micro-break meta-analysis (vigour d = 0.36) supports frequent short pauses.
BREAK_INTERVAL_MINUTES = 25
MAX_MINUTES_WITHOUT_BREAK = 30

# Sound never stops dead - it fades. The same rule the health plan sets for the
# sleep sound, for the same reason: an abrupt stop is itself a startle.
MIN_FADE_SECONDS = 1.0
FADE_SECONDS = 3.0

MIN_BLOCK_MINUTES = 5
MAX_BLOCK_MINUTES = 120

# Evening light suppresses melatonin; the theme may only get dimmer and warmer
# past this hour, never brighter. Local hour, read as given.
WIND_DOWN_HOUR = 21

# A block whose tab was closed. Reaped rather than left blocking the next start.
STALE_GRACE_MINUTES = 30

# --- The catalogue: synthesis specs, not media. ------------------------------

# The whole "no lyrics, no speech" post-condition is this tuple. A spec says how
# to build a sound from oscillators and filters; there is no field that could
# name a track, and tests/test_focus.py asserts that.
PROCEDURAL_KINDS = ("silence", "noise", "modulated_pad", "binaural")

CONDITIONS: dict[str, dict] = {
    # The control. Also the honest default: in a quiet room the evidence does
    # not support adding anything.
    "silence": {"kind": "silence"},
    # Pink rather than white: same 1/f family as the meta-analysed studies and
    # judged less aversive. Framed as a mask for a noisy room, not a stimulant.
    "noise_mask": {"kind": "noise", "color": "pink"},
    # The Brain.fm mechanism, reproduced in two nodes: a soft harmonic pad whose
    # gain is modulated at 16 Hz (the beta rate that helped high-symptom
    # listeners in Woods et al. 2024).
    "modulated": {"kind": "modulated_pad", "mod_hz": 16.0, "carrier_hz": 220.0, "depth": 0.6},
    # The only carrier/beat pair that improved anything in the preregistered
    # parametric study was gamma on 340 Hz; 16 Hz beta is the arm that matches
    # the modulated pad, so the two differ in mechanism and not in rate.
    "binaural": {"kind": "binaural", "beat_hz": 16.0, "carrier_hz": 340.0},
}

# Per-condition gain, all at or under the ceiling. Silence asks for nothing so a
# bug cannot make the control audible.
CONDITION_GAIN: dict[str, float] = {
    "silence": 0.0,
    "noise_mask": 0.16,
    "modulated": 0.12,
    "binaural": 0.10,
}

PROBE_PHASES = ("start", "end")


class FocusBlockRunning(Exception):
    """A block is already running. Starting a second one would make both
    unreadable, so it is refused rather than silently ending the first."""


class NoActiveFocusBlock(Exception):
    pass


@dataclass(frozen=True)
class Theme:
    """What the light does. `brightness` and `warmth` are 0..1 and exist so the
    'evening never brightens' rule can be a test rather than an intention; the
    HUD and the headset each map them onto their own controls (CSS tokens, and
    a passthrough surroundings effect)."""

    brightness: float
    warmth: float
    evening: bool


DAY_THEME = Theme(brightness=1.0, warmth=0.0, evening=False)
EVENING_THEME = Theme(brightness=0.55, warmth=1.0, evening=True)


def theme_for(now: datetime) -> Theme:
    """Local-time hour read as given (pass `datetime.now().astimezone()`), the
    same choice `ConversationManager._build_system` makes: "evening" has to mean
    Duc's evening, not UTC's."""
    return EVENING_THEME if now.hour >= WIND_DOWN_HOUR else DAY_THEME


@dataclass
class FocusPlan:
    """What the front door is told to do for one block. Validated on
    construction, so an invalid plan cannot reach a browser."""

    condition: str
    minutes: int
    break_every_minutes: int
    gain: float
    fade_seconds: float
    evening: bool
    spec: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.condition not in CONDITIONS:
            raise ValueError(f"unknown condition {self.condition!r}; have {sorted(CONDITIONS)}")
        if not MIN_BLOCK_MINUTES <= self.minutes <= MAX_BLOCK_MINUTES:
            raise ValueError(f"minutes must be {MIN_BLOCK_MINUTES}-{MAX_BLOCK_MINUTES}, got {self.minutes}")
        if self.gain > GAIN_CEILING:
            raise ValueError(f"gain {self.gain} is above the ceiling {GAIN_CEILING}")
        if not 0 < self.break_every_minutes <= MAX_MINUTES_WITHOUT_BREAK:
            raise ValueError(
                f"break_every_minutes must be 1-{MAX_MINUTES_WITHOUT_BREAK}, got {self.break_every_minutes}"
            )
        if self.fade_seconds < MIN_FADE_SECONDS:
            raise ValueError(f"fade must be at least {MIN_FADE_SECONDS}s, got {self.fade_seconds}")

    @property
    def break_offsets(self) -> list[int]:
        """Minutes into the block at which to cue a break. Never at the end -
        the end of the block is its own event."""
        return list(range(self.break_every_minutes, self.minutes, self.break_every_minutes))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["break_offsets"] = self.break_offsets
        return d


class FocusPlanner(ABC):
    """Decides what one block should be. Swapping this is how "Kyra learned
    what works for Duc" replaces "Kyra is running an experiment", with no
    change to the tools, the endpoints or the browser."""

    @abstractmethod
    def plan(self, *, now: datetime, completed: int, minutes: int, condition: str | None = None) -> FocusPlan:
        ...


class ScheduledPlanner(FocusPlanner):
    """Block randomisation: each consecutive run of four blocks contains each
    arm exactly once, in an order that is shuffled per cycle and reproducible
    from the completed count alone.

    Both halves matter. Balance means an arm cannot be over-represented in a
    good week. Shuffling per cycle means the arm does not track the time of day
    - a fixed order would put the same condition in every morning block, and the
    morning would get the credit. Reproducibility means the schedule can be
    replayed when reading the report, and needs no stored state.
    """

    def plan(self, *, now: datetime, completed: int, minutes: int, condition: str | None = None) -> FocusPlan:
        if not MIN_BLOCK_MINUTES <= minutes <= MAX_BLOCK_MINUTES:
            raise ValueError(f"minutes must be {MIN_BLOCK_MINUTES}-{MAX_BLOCK_MINUTES}, got {minutes}")
        if condition is None:
            condition = self._assign(completed)
        elif condition not in CONDITIONS:
            raise ValueError(f"unknown condition {condition!r}; have {sorted(CONDITIONS)}")
        theme = theme_for(now)
        return FocusPlan(
            condition=condition,
            minutes=minutes,
            break_every_minutes=min(BREAK_INTERVAL_MINUTES, minutes),
            gain=min(CONDITION_GAIN[condition], GAIN_CEILING),
            fade_seconds=FADE_SECONDS,
            evening=theme.evening,
            spec=dict(CONDITIONS[condition]),
        )

    @staticmethod
    def _assign(completed: int) -> str:
        names = sorted(CONDITIONS)
        cycle, position = divmod(max(completed, 0), len(names))
        random.Random(cycle).shuffle(names)
        return names[position]


@dataclass
class FocusSession:
    id: int
    condition: str
    task: str
    planned_minutes: int
    started_at: str
    ended_at: str | None
    abandoned: bool
    probe_start_ms: float | None
    probe_start_lapses: int | None
    probe_end_ms: float | None
    probe_end_lapses: int | None
    rating: int | None
    note: str | None

    @property
    def probe_delta_ms(self) -> float | None:
        """End minus start: positive means Duc got slower across the block,
        which is the vigilance decrement the whole exercise is about."""
        if self.probe_start_ms is None or self.probe_end_ms is None:
            return None
        return self.probe_end_ms - self.probe_start_ms


def _row(r) -> FocusSession:
    return FocusSession(
        id=r.id, condition=r.condition, task=r.task, planned_minutes=r.planned_minutes,
        started_at=r.started_at, ended_at=r.ended_at, abandoned=bool(r.abandoned),
        probe_start_ms=r.probe_start_ms, probe_start_lapses=r.probe_start_lapses,
        probe_end_ms=r.probe_end_ms, probe_end_lapses=r.probe_end_lapses,
        rating=r.rating, note=r.note,
    )


class FocusStore:
    def __init__(self, path: Path | str | None = None, *, engine: Engine | None = None):
        self._engine = engine or engine_for_store(DB_PATH, path)

    # --- writes ---------------------------------------------------------

    def start(self, *, condition: str, minutes: int, task: str = "") -> FocusSession:
        if condition not in CONDITIONS:
            raise ValueError(f"unknown condition {condition!r}; have {sorted(CONDITIONS)}")
        if not MIN_BLOCK_MINUTES <= minutes <= MAX_BLOCK_MINUTES:
            raise ValueError(f"minutes must be {MIN_BLOCK_MINUTES}-{MAX_BLOCK_MINUTES}, got {minutes}")
        self._reap()
        if (running := self.active()) is not None:
            raise FocusBlockRunning(f"block {running.id} is still running ({running.condition}, {running.task!r})")
        now = datetime.now(UTC).isoformat()
        with self._engine.begin() as conn:
            res = conn.execute(insert(FS).values(
                condition=condition, task=task, planned_minutes=minutes,
                started_at=now, ended_at=None, abandoned=0,
            ))
        return self.get(res.inserted_primary_key[0])

    def end(self, session_id: int, *, rating: int | None = None, note: str = "") -> FocusSession:
        if rating is not None and not 1 <= rating <= 5:
            raise ValueError(f"rating must be 1-5, got {rating}")
        with self._engine.begin() as conn:
            done = conn.execute(update(FS).where(FS.c.id == session_id, FS.c.ended_at.is_(None)).values(
                ended_at=datetime.now(UTC).isoformat(), rating=rating, note=note,
            )).rowcount
        if not done:
            raise NoActiveFocusBlock(f"no running block with id {session_id}")
        return self.get(session_id)

    def record_probe(self, session_id: int, *, phase: str, median_ms: float, lapses: int) -> FocusSession:
        if phase not in PROBE_PHASES:
            raise ValueError(f"phase must be one of {PROBE_PHASES}, got {phase!r}")
        values = {f"probe_{phase}_ms": float(median_ms), f"probe_{phase}_lapses": int(lapses)}
        with self._engine.begin() as conn:
            if not conn.execute(update(FS).where(FS.c.id == session_id).values(**values)).rowcount:
                raise KeyError(f"no focus block with id {session_id}")
        return self.get(session_id)

    # --- reads ----------------------------------------------------------

    def get(self, session_id: int) -> FocusSession:
        with self._engine.connect() as conn:
            row = conn.execute(select(FS).where(FS.c.id == session_id)).first()
        if row is None:
            raise KeyError(f"no focus block with id {session_id}")
        return _row(row)

    def active(self) -> FocusSession | None:
        self._reap()
        with self._engine.connect() as conn:
            row = conn.execute(
                select(FS).where(FS.c.ended_at.is_(None), FS.c.abandoned == 0).order_by(FS.c.id.desc())
            ).first()
        return _row(row) if row else None

    def list(self, limit: int = 200, include_abandoned: bool = False) -> list[FocusSession]:
        q = select(FS).where(FS.c.ended_at.is_not(None))
        if not include_abandoned:
            q = q.where(FS.c.abandoned == 0)
        with self._engine.connect() as conn:
            rows = conn.execute(q.order_by(FS.c.started_at.desc()).limit(limit)).all()
        return [_row(r) for r in rows]

    def completed_count(self) -> int:
        """What the planner counts to assign the next arm: finished, not
        abandoned. A closed tab must not advance the experiment."""
        with self._engine.connect() as conn:
            rows = conn.execute(select(FS.c.id).where(FS.c.ended_at.is_not(None), FS.c.abandoned == 0)).all()
        return len(rows)

    # --- housekeeping ---------------------------------------------------

    def _reap(self) -> None:
        """Mark blocks that outran their length plus a grace period as
        abandoned. Reaped on read rather than by a timer: there is no scheduler
        in the server, and a block nobody has looked at since does not need
        closing at the exact minute."""
        cutoff = datetime.now(UTC)
        with self._engine.begin() as conn:
            rows = conn.execute(select(FS.c.id, FS.c.started_at, FS.c.planned_minutes)
                                .where(FS.c.ended_at.is_(None), FS.c.abandoned == 0)).all()
            stale = [
                r.id for r in rows
                if datetime.fromisoformat(r.started_at) + timedelta(minutes=r.planned_minutes + STALE_GRACE_MINUTES)
                < cutoff
            ]
            if stale:
                conn.execute(update(FS).where(FS.c.id.in_(stale)).values(abandoned=1))

    def _force_started_at(self, session_id: int, started_at: str) -> None:
        """Test seam: age a block so the reaper can be exercised without waiting."""
        with self._engine.begin() as conn:
            conn.execute(update(FS).where(FS.c.id == session_id).values(started_at=started_at))


# --- The report ---------------------------------------------------------------

# Below this, an arm is a rumour. Four arms at eight blocks is about two weeks of
# ordinary work, which is what the plan asks Duc for.
MIN_BLOCKS_PER_ARM = 8


@dataclass
class ArmSummary:
    condition: str
    blocks: int
    median_delta_ms: float | None      # end minus start: positive is the vigilance decrement
    delta_spread_ms: float | None      # interquartile range, the noise floor for this arm
    median_end_lapses: float | None
    mean_rating: float | None


@dataclass
class FocusReport:
    arms: list[ArmSummary]
    total_blocks: int
    first: str | None
    last: str | None
    min_blocks_per_arm: int

    @property
    def arms_ready(self) -> list[str]:
        return [a.condition for a in self.arms if a.blocks >= self.min_blocks_per_arm]

    @property
    def readable(self) -> bool:
        return len(self.arms_ready) == len(CONDITIONS)

    def verdict(self) -> str:
        """The one line that must never over-claim.

        Two gates before anything is ranked: every arm has enough blocks, and the
        gap between the best and second-best exceeds the widest within-arm spread.
        The second gate is the one that matters - with eight blocks an arm, a
        20 ms difference is noise, and the same lesson has already been paid for
        twice on this project (router round 4's 4-point seed swing, and the search
        eval where one query moved the score 3.8 points).
        """
        if not self.readable:
            short = len(CONDITIONS) - len(self.arms_ready)
            return (f"Not readable yet: {short} of {len(CONDITIONS)} arms have fewer than "
                    f"{self.min_blocks_per_arm} blocks. Keep working; the schedule balances itself.")
        scored = [a for a in self.arms if a.median_delta_ms is not None]
        if len(scored) < len(CONDITIONS):
            return "Not readable yet: some arms have no probe data, so the arms cannot be compared."
        scored.sort(key=lambda a: a.median_delta_ms)
        best, second = scored[0], scored[1]
        gap = second.median_delta_ms - best.median_delta_ms
        widest = max((a.delta_spread_ms or 0.0) for a in scored)
        if gap <= widest:
            return (f"The arms are not separated: {best.condition} leads {second.condition} by "
                    f"{gap:.0f} ms, inside the widest within-arm spread of {widest:.0f} ms. "
                    f"That is a tie, not a winner.")
        return (f"{best.condition} is ahead: {gap:.0f} ms better than {second.condition}, "
                f"outside the widest within-arm spread of {widest:.0f} ms. Worth making the default "
                f"and re-checking after another {self.min_blocks_per_arm} blocks each.")


def _spread(values: list[float]) -> float | None:
    """Interquartile range, or the plain range when there are too few points for
    quartiles to mean anything. Never returns 0 silently for a single value."""
    if len(values) < 2:
        return None
    if len(values) < 4:
        return max(values) - min(values)
    q = statistics.quantiles(values, n=4)
    return q[2] - q[0]


def build_report(sessions: list[FocusSession], min_blocks_per_arm: int = MIN_BLOCKS_PER_ARM) -> FocusReport:
    usable = [s for s in sessions if s.ended_at and not s.abandoned]
    arms = []
    for condition in sorted(CONDITIONS):
        rows = [s for s in usable if s.condition == condition]
        deltas = [s.probe_delta_ms for s in rows if s.probe_delta_ms is not None]
        lapses = [float(s.probe_end_lapses) for s in rows if s.probe_end_lapses is not None]
        ratings = [float(s.rating) for s in rows if s.rating is not None]
        arms.append(ArmSummary(
            condition=condition,
            blocks=len(rows),
            median_delta_ms=statistics.median(deltas) if deltas else None,
            delta_spread_ms=_spread(deltas),
            median_end_lapses=statistics.median(lapses) if lapses else None,
            mean_rating=round(statistics.fmean(ratings), 2) if ratings else None,
        ))
    stamps = sorted(s.started_at for s in usable)
    return FocusReport(
        arms=arms, total_blocks=len(usable),
        first=stamps[0] if stamps else None, last=stamps[-1] if stamps else None,
        min_blocks_per_arm=min_blocks_per_arm,
    )


def render_report(report: FocusReport) -> str:
    lines = [
        "FOCUS BLOCKS - what the conditions actually did",
        "=" * 62,
        f"{report.total_blocks} completed blocks"
        + (f", {report.first[:10]} to {report.last[:10]}" if report.first else ""),
        "",
        f"{'condition':<12}{'blocks':>7}{'RT drift':>11}{'spread':>9}{'lapses':>8}{'rating':>8}",
    ]
    for a in report.arms:
        drift = f"{a.median_delta_ms:+.0f} ms" if a.median_delta_ms is not None else "-"
        spread = f"±{a.delta_spread_ms:.0f}" if a.delta_spread_ms is not None else "-"
        lapses = f"{a.median_end_lapses:.0f}" if a.median_end_lapses is not None else "-"
        rating = f"{a.mean_rating:.1f}" if a.mean_rating is not None else "-"
        lines.append(f"{a.condition:<12}{a.blocks:>7}{drift:>11}{spread:>9}{lapses:>8}{rating:>8}")
    lines += [
        "",
        "RT drift is the median change in reaction time across a block; lower is better",
        "(less slowing). Spread is the interquartile range within that arm - the noise floor.",
        "",
        report.verdict(),
        "",
        "Caveats that travel with these numbers:",
        "  - n=1. This says what happened to Duc, not what happens to people.",
        "  - Blind by convention: the panel does not name the arm, but the synthesis",
        "    spec is in the browser payload, so a determined look would unblind it.",
        "  - Blocks differ in task, time of day and sleep; the schedule balances the",
        "    order of the arms, nothing else.",
    ]
    return "\n".join(lines)


# --- Tools -------------------------------------------------------------------

_BLIND_NOTE = (
    "Do not tell Duc which audio condition was assigned - the experiment is blind until the block ends."
)


class StartFocusBlockTool(Tool):
    name = "start_focus_block"
    description = (
        "Start a focus block for Duc: a timed stretch of work with a chosen sound environment, break cues, and a "
        "reaction-time probe at each end. Use when he says he is starting work, wants to focus, asks for focus "
        "music or a focus session, or asks to start a pomodoro. " + _BLIND_NOTE
    )
    input_schema = {
        "type": "object",
        "properties": {
            "minutes": {"type": "integer", "description": "Block length in minutes (5-120). Default 50."},
            "task": {"type": "string", "description": "What Duc said he is working on, in a few words."},
        },
    }

    def __init__(self, store: FocusStore | None = None, planner: FocusPlanner | None = None):
        self._store = store or FocusStore()
        self._planner = planner or ScheduledPlanner()

    def run(self, minutes: int = 50, task: str = "") -> dict:
        try:
            plan = self._planner.plan(
                now=datetime.now().astimezone(), completed=self._store.completed_count(), minutes=int(minutes)
            )
            session = self._store.start(condition=plan.condition, minutes=plan.minutes, task=task)
        except FocusBlockRunning as e:
            return {"error": str(e)}
        except ValueError as e:
            return {"error": str(e)}
        # The condition is deliberately absent: this dict is what Claude reads
        # back to Duc, and naming the arm would unblind him.
        # The offsets, not the interval: a 25-minute block has an interval of 25 and
        # no cue at all (a cue never lands on the end of the block), and reporting the
        # interval let a real turn tell Duc about a break that would never fire.
        return {
            "started": True, "id": session.id, "minutes": plan.minutes, "task": session.task,
            "break_cues_at_minutes": plan.break_offsets, "evening": plan.evening,
        }


class EndFocusBlockTool(Tool):
    name = "end_focus_block"
    description = (
        "End the running focus block and record how it went. Use when Duc says he is done, finished, stopping, or "
        "answers how a block went. Ask him for a 1-5 rating and one word on why if he has not said."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "rating": {"type": "integer", "description": "How the block went, 1 (bad) to 5 (great). Optional."},
            "note": {"type": "string", "description": "One or two words from Duc on why. Optional."},
        },
    }

    def __init__(self, store: FocusStore | None = None):
        self._store = store or FocusStore()

    def run(self, rating: int | None = None, note: str = "") -> dict:
        session = self._store.active()
        if session is None:
            return {"error": "no focus block is running"}
        try:
            ended = self._store.end(session.id, rating=rating, note=note)
        except ValueError as e:
            return {"error": str(e)}
        # Now that it is over, naming the arm is the point - it is how Duc
        # learns what he just worked under.
        return {
            "ended": True, "id": ended.id, "condition": ended.condition, "task": ended.task,
            "rating": ended.rating, "note": ended.note, "probe_delta_ms": ended.probe_delta_ms,
        }


class FocusStatusTool(Tool):
    name = "focus_status"
    description = (
        "Report whether a focus block is running, how long is left, and how many blocks have been completed. Use "
        "when Duc asks how long he has been working, how much is left, or how his focus blocks are going. "
        + _BLIND_NOTE
    )
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, store: FocusStore | None = None):
        self._store = store or FocusStore()

    def run(self) -> dict:
        completed = self._store.completed_count()
        session = self._store.active()
        if session is None:
            return {"running": False, "completed_blocks": completed}
        elapsed = (datetime.now(UTC) - datetime.fromisoformat(session.started_at)).total_seconds() / 60
        return {
            "running": True, "id": session.id, "task": session.task,
            "elapsed_minutes": round(elapsed, 1),
            "remaining_minutes": round(max(session.planned_minutes - elapsed, 0), 1),
            "completed_blocks": completed,
        }


def focus_tools(store: FocusStore | None = None, planner: FocusPlanner | None = None) -> list[Tool]:
    store = store or FocusStore()
    return [StartFocusBlockTool(store, planner), EndFocusBlockTool(store), FocusStatusTool(store)]
