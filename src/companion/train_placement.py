"""Capacity planning for landing trains using the existing runner registry.

Pure planning: callers own reservations and must recheck admission before launch.
An unavailable runner never blocks another capable runner. The owner Mac is a
fallback, except for test kinds that no remote machine supports.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from companion.runner_pool import Load, Runner, free_cores


@dataclass(frozen=True)
class Request:
    name: str
    priority: int = 99
    kind: str = 'system'


def plan(requests: Sequence[Request], runners: Sequence[Runner], loads: Mapping[str, Load | None], *,
         active: Mapping[str, int] | None = None, mac_admitted: bool = True) -> dict[str, str]:
    """Place in priority/file order, reserving one slot and its CPU share per train.

    Busy transport locks exclude a runner even when its configured slot limit is
    higher. Raising that limit alone never overrides a transport's exclusive lock.
    Unplaced requests stay queued; callers must not turn them into local launches.
    """
    if len({r.name for r in requests}) != len(requests):
        raise ValueError('duplicate train name')
    used = dict(active or {})
    if any(type(count) is not int or count < 0 for count in used.values()):
        raise ValueError('active train counts must be non-negative integers')
    placements = {}
    for request in sorted(requests, key=lambda r: r.priority):
        choices = []
        for machine in runners:
            count = used.get(machine.name, 0)
            if request.kind not in machine.test_kinds or count >= machine.max_parallel_trains:
                continue
            if machine.reach == 'local' and (not mac_admitted or request.kind != 'apple'):
                continue
            headroom = free_cores(machine, loads.get(machine.name))
            # Existing trains already contribute to live load. Reservations also
            # cap free slots, covering the interval before a new process starts.
            slots = machine.max_parallel_trains - count
            headroom = min(headroom, machine.cores * slots / machine.max_parallel_trains)
            if headroom > 0:
                # PC first, then M1, for every queue priority. Priority orders
                # requests; it must not move ordinary tests onto the owner Mac.
                choices.append((machine.reach != 'local', machine.cores * machine.speed,
                                headroom * machine.speed, machine.name))
        if choices:
            name = max(choices)[-1]
            placements[request.name] = name
            used[name] = used.get(name, 0) + 1
    return placements


def related_groups(changes, packages):
    """Connected components by changed file or package, preserving queue priority order."""
    from fnmatch import fnmatchcase

    def keys(paths):
        found = {('file', p) for p in paths}
        for name, package in packages.items():
            if any(any(fnmatchcase(p, pattern) for pattern in (*package['include'], *package.get('tests', [])))
                   and not any(fnmatchcase(p, pattern) for pattern in package.get('exclude', []))
                   for p in paths):
                found.add(('package', name))
        return found

    remaining = {name: keys(paths) for name, paths in changes.items()}
    groups = []
    while remaining:
        first = next(iter(remaining))
        members, shared = {first}, remaining.pop(first)
        while True:
            joined = [name for name, touched in remaining.items() if shared & touched]
            if not joined:
                break
            for name in joined:
                members.add(name)
                shared.update(remaining.pop(name))
        groups.append([name for name in changes if name in members])
    return groups
