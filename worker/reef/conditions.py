"""The alert conditions, judged by the worker over several polls.

A condition starts once it has held for its pending time (no single bad sample alerts). A probe condition ends
when the value is back inside the limit by its clearing band; a switch, module or current condition ends once it
has been false for its pending time. When the Apex does not answer, nothing changes: the last known state stays.
A critical condition hides the warning conditions on the same sensor."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .apex import Status, module_of
from .site import Condition, Module, Site

NO_CURRENT_AMPS = 0.05  # the EB8 reports 0.1 A steps; anything below this is "no current"


@dataclass
class State:
    active: bool = False
    since: float | None = None  # when it became active
    starting: float | None = None  # when the condition began to hold, while not yet active
    ending: float | None = None  # when it began to be false, while active (switches, modules, current)
    detail: str = ""


@dataclass(frozen=True)
class Change:
    condition: Condition
    active: bool  # True: started; False: ended
    detail: str


def module_check(module: Module, status: Status) -> tuple[bool, str]:
    """(connected, why not). An EB8 is connected while each of its outlets is listed with health "OK" and its
    current input is there; another module while any of its inputs or outputs is listed."""
    outputs = [o for o in status.outputs.values() if module_of(o.did) == module.address]
    inputs = [i for i in status.inputs.values() if module_of(i.did) == module.address]
    if module.kind == "EB8":
        if not outputs:
            return False, "none of its outlets is listed"
        bad = [f"{o.name}={o.health or 'none'}" for o in outputs if o.health != "OK"]
        if bad:
            return False, "outlet health " + ", ".join(bad)
        if module.current and module.current not in status.inputs:
            return False, f"its current input {module.current} is missing"
        return True, ""
    if not outputs and not inputs:
        return False, "none of its inputs is listed"
    return True, ""


class Engine:
    def __init__(self, site: Site) -> None:
        self.site = site
        self.states: dict[str, State] = {c.id: State() for c in site.conditions}
        # recent readings of the sensors a rise or fall condition watches, (ts, value), as long as the longest window
        self.window = {c.subject: 0.0 for c in site.conditions if c.kind in ("rise", "fall")}
        for c in site.conditions:
            if c.kind in ("rise", "fall"):
                self.window[c.subject] = max(self.window[c.subject], c.minutes * 60)
        self.recent: dict[str, deque[tuple[float, float]]] = {name: deque() for name in self.window}

    def remember(self, status: Status, now: float) -> None:
        """Keep the readings the rate conditions need (a restart starts the windows afresh)."""
        for name, recent in self.recent.items():
            reading = status.inputs.get(name)
            if reading is not None:
                recent.append((now, reading.value))
            while recent and recent[0][0] < now - self.window[name] - 300:
                recent.popleft()

    def change_over(self, name: str, minutes: float, now: float) -> tuple[float, float] | None:
        """(change, the value it changed from) over the last `minutes`, or None while the window is not yet covered."""
        recent = self.recent.get(name)
        if not recent or recent[0][0] > now - minutes * 60 + 90:
            return None
        start = next((v for ts, v in recent if ts >= now - minutes * 60 - 60), None)
        if start is None:
            return None
        return recent[-1][1] - start, start

    def restore(self, active: dict[str, float]) -> None:
        """Conditions that were active when the worker stopped (id -> since); the next poll judges them again."""
        for cid, since in active.items():
            if cid in self.states:
                self.states[cid] = State(active=True, since=since)

    def _truth(self, c: Condition, status: Status, now: float = 0.0) -> tuple[bool | None, bool, str]:
        """(holds, back inside its band, detail). holds None: this poll says nothing about the condition."""
        if c.kind in ("rise", "fall"):
            found = self.change_over(c.subject, c.minutes, now) if c.subject in status.inputs else None
            if found is None or c.limit is None:
                return None, False, ""
            change, start = found
            moved = change if c.kind == "rise" else -change
            detail = f"{c.subject} {start:g} -> {status.inputs[c.subject].value:g} in {c.minutes:g} min"
            return moved >= c.limit, moved < c.limit / 2, detail
        if c.kind in ("above", "below"):
            reading = status.inputs.get(c.subject)
            if reading is None or c.limit is None:
                return None, False, ""
            value = reading.value
            if c.kind == "above":
                return value > c.limit, value <= c.limit - c.clear, f"{c.subject} = {value:g}"
            return value < c.limit, value >= c.limit + c.clear, f"{c.subject} = {value:g}"
        if c.kind in ("closed", "open"):
            reading = status.inputs.get(c.subject)
            if reading is None:
                return None, False, ""
            holds = reading.closed if c.kind == "closed" else not reading.closed
            detail = f"{c.subject} is {'CLOSED' if reading.closed else 'OPEN'}"
            if c.output is not None:  # only while that output is on as well
                output = status.outputs.get(c.output)
                if output is None:
                    return None, False, ""
                holds = holds and output.energized
                detail += f", {c.output} {'on' if output.energized else 'off'}"
            return holds, not holds, detail
        module = self.site.module(c.subject)
        if module is None:
            return None, False, ""
        if c.kind == "module_lost":
            connected, why = module_check(module, status)
            return not connected, connected, why
        # no_current: an outlet that always draws is energized and the bar shows nothing
        reading = status.inputs.get(module.current or "")
        if reading is None:
            return None, False, ""  # a missing current input is the module check's business
        on = [o for o in module.draws_when_on if o in status.outputs and status.outputs[o].energized]
        holds = bool(on) and reading.value < NO_CURRENT_AMPS
        return holds, not holds, f"{module.current} = {reading.value:g} A with {', '.join(on) or 'nothing'} on"

    def evaluate(self, status: Status, now: float) -> list[Change]:
        changes: list[Change] = []
        self.remember(status, now)
        for c in self.site.conditions:
            state = self.states[c.id]
            holds, inside, detail = self._truth(c, status, now)
            if holds is None:
                continue
            if not state.active:
                state.ending = None
                if not holds:
                    state.starting = None
                    continue
                state.starting = state.starting if state.starting is not None else now
                if now - state.starting >= c.pending:
                    state.active, state.since, state.starting, state.detail = True, now, None, detail
                    changes.append(Change(c, True, detail))
                continue
            state.detail = detail if holds else state.detail
            if c.kind in ("above", "below", "rise", "fall"):
                if inside:
                    self.states[c.id] = State()
                    changes.append(Change(c, False, detail))
                continue
            if holds:
                state.ending = None
                continue
            state.ending = state.ending if state.ending is not None else now
            if now - state.ending >= c.pending:
                self.states[c.id] = State()
                changes.append(Change(c, False, detail))
        return changes

    def active(self) -> list[tuple[Condition, State]]:
        """The active conditions to report: a critical one hides the warnings on the same sensor or module."""
        on = [(c, self.states[c.id]) for c in self.site.conditions if self.states[c.id].active]
        critical = {c.subject for c, _ in on if c.severity == "critical"}
        return [(c, s) for c, s in on if c.severity == "critical" or c.subject not in critical]
