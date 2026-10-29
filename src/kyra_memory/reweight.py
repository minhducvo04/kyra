"""Fact weights from outcomes: a fact that keeps misleading ranks lower, one that keeps helping ranks higher.

weight = 0.7 + 0.6 * confidence(wins, losses), only once wins + losses >= MIN_OUTCOMES (below that the target is 1.0),
clamped to [0.7, 1.3]. Each run moves a stored weight at most MAX_STEP toward its target, so one bad batch of outcomes
cannot swing the ranking. Only facts carry outcomes: rules and pinned rows are never touched. The assembler multiplies the
keyword score of a fact by its weight (1.0 when none is stored). Owner-side job (it writes the store); the host runs it
under low power.
"""
from kyra_memory.store import MemoryStore

MIN_OUTCOMES = 5
LOW, HIGH = 0.7, 1.3
MAX_STEP = 0.05


def confidence(wins: int, losses: int) -> float:
    """Laplace-smoothed win rate."""
    return (wins + 1) / (wins + losses + 2)


def target_weight(wins: int, losses: int) -> float:
    if wins + losses < MIN_OUTCOMES:
        return 1.0
    return min(HIGH, max(LOW, 0.7 + 0.6 * confidence(wins, losses)))


def next_weight(current: float, wins: int, losses: int) -> float:
    step = max(-MAX_STEP, min(MAX_STEP, target_weight(wins, losses) - current))
    return round(min(HIGH, max(LOW, current + step)), 6)


def run(store: MemoryStore) -> dict[str, int]:
    """One pass over the active facts; returns counts. Stored weights that are already right are left alone."""
    stored = store.weights()
    updates, up, down = {}, 0, 0
    for fact in store.current("facts"):
        current = stored.get(fact.id, 1.0)
        new = next_weight(current, int(fact.extra.get("wins", 0)), int(fact.extra.get("losses", 0)))
        if new != current:
            updates[fact.id] = new
            up, down = up + (new > current), down + (new < current)
    if updates:
        store.set_weights(updates)
    return {"facts": len(store.current("facts")), "changed": len(updates), "raised": up, "lowered": down}
