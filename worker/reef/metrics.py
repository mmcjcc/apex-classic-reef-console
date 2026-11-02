"""What Prometheus reads from the worker (port 9106). Readings, outlets and modules only while the latest poll is
current: an old value must never pass for the present. The conditions always: their state is the worker's
judgement, which a silent Apex does not change, and the alert rules (deploy/prometheus/rules/reef.yml) keep it
firing while the worker is down."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from prometheus_client.core import GaugeMetricFamily

from .apex import Status, module_of
from .conditions import Engine, module_check
from .site import Site


class Metrics:
    def __init__(self, site: Site, engine: Engine) -> None:
        self.site = site
        self.engine = engine
        self.status: Status | None = None  # the latest good poll
        self.failing = False  # the latest poll failed
        self.report: tuple[float, str] | None = None  # the latest weekly report: (when, headline)
        self.notices: dict[str, tuple[float, str]] = {}  # notice id -> (when it was last sent, its text)
        self.max_age = max(180.0, 3 * site.poll_seconds)

    def update(self, status: Status) -> None:
        self.status, self.failing = status, False

    def failed(self) -> None:
        self.failing = True

    def collect(self) -> Iterator[Any]:
        now = time.time()
        status = self.status
        current = status is not None and not self.failing and now - status.received_at < self.max_age
        yield GaugeMetricFamily("reef_apex_up", "1 if the latest poll of the Apex worked and is recent", value=float(current))
        yield GaugeMetricFamily(
            "reef_apex_last_successful_poll_timestamp_seconds",
            "Time of the last poll the Apex answered (0 = none yet)",
            value=status.received_at if status else 0.0,
        )

        active = {c.id for c, _ in self.engine.active()}
        cond = GaugeMetricFamily(
            "reef_condition_active",
            "1 while an alert condition holds (after its pending time; a critical one hides warnings on the same subject)",
            labels=["alarm", "condition", "severity", "subject", "summary"],
        )
        since = GaugeMetricFamily(
            "reef_condition_since_timestamp_seconds", "When an active condition began", labels=["condition"]
        )
        for c in self.site.conditions:
            cond.add_metric([c.alarm, c.id, c.severity, c.subject, c.text], float(c.id in active))
            state = self.engine.states[c.id]
            if c.id in active and state.since is not None:
                since.add_metric([c.id], state.since)
        yield cond
        yield since
        if self.notices:
            notice = GaugeMetricFamily(
                "reef_notice_timestamp_seconds",
                "When a one-time notice was last sent (Alertmanager mails it once)",
                labels=["notice", "summary"],
            )
            for nid, (ts, text) in sorted(self.notices.items()):
                notice.add_metric([nid, text[:200]], ts)
            yield notice
        if self.report is not None:
            published = GaugeMetricFamily(
                "reef_report_published_timestamp_seconds",
                "When the latest weekly report was made, with its headline (Alertmanager mails the headline once)",
                labels=["headline"],
            )
            published.add_metric([self.report[1][:200]], self.report[0])
            yield published

        if status is None:
            return
        if status.power_failed:
            yield GaugeMetricFamily(
                "reef_apex_power_failed_timestamp_seconds", "The Apex's last power failure", value=status.power_failed
            )
        if status.power_restored:
            yield GaugeMetricFamily(
                "reef_apex_power_restored_timestamp_seconds", "When the Apex's power came back", value=status.power_restored
            )
        if not current:
            return
        yield GaugeMetricFamily("reef_apex_poll_latency_seconds", "How long the latest status.json took", value=status.latency)
        offset = status.clock_offset()
        if offset is not None:
            yield GaugeMetricFamily("reef_apex_clock_offset_seconds", "The Apex's clock minus the worker's", value=offset)
        yield GaugeMetricFamily("reef_feed_active", "1 while a Feed cycle runs", value=float(status.feed_active))

        values = GaugeMetricFamily(
            "reef_sensor_value", "A probe's latest value", labels=["sensor", "did", "type", "unit", "label"]
        )
        switches = GaugeMetricFamily("reef_switch_closed", "1 while a switch input is CLOSED", labels=["sensor", "did", "label"])
        for reading in sorted(status.inputs.values(), key=lambda r: r.did):
            sensor = self.site.sensors.get(reading.name)
            if sensor is not None and not sensor.active:
                continue
            label = sensor.label if sensor else reading.name
            if reading.type == "digital":
                switches.add_metric([reading.name, reading.did, label], float(reading.closed))
            else:
                unit = sensor.unit if sensor else ""
                values.add_metric([reading.name, reading.did, reading.type, unit, label], reading.value)
        yield values
        yield switches

        labels = ["outlet", "did", "module", "type"]
        energized = GaugeMetricFamily("reef_outlet_energized", "1 while an output is on", labels=labels)
        manual = GaugeMetricFamily("reef_outlet_manual", "1 while an output is in a manual override (ON or OFF)", labels=labels)
        healthy = GaugeMetricFamily("reef_outlet_healthy", "1 while the Apex reports the output's health as OK", labels=labels)
        for out in sorted(status.outputs.values(), key=lambda o: o.did):
            key = [out.name, out.did, module_of(out.did), out.type]
            energized.add_metric(key, float(out.energized))
            manual.add_metric(key, float(out.manual))
            healthy.add_metric(key, float(out.health == "OK"))
        yield from (energized, manual, healthy)

        modules = GaugeMetricFamily(
            "reef_module_connected", "1 while an expected AquaBus module reports", labels=["module", "kind"]
        )
        amps = GaugeMetricFamily("reef_module_current_amps", "An EnergyBar's current", labels=["module"])
        for m in self.site.modules:
            connected, _why = module_check(m, status)
            modules.add_metric([m.id, m.kind], float(connected))
            if m.current and m.current in status.inputs:
                amps.add_metric([m.id], status.inputs[m.current].value)
        yield modules
        yield amps
