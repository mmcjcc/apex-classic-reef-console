# Alerts

The worker judges every condition itself, once a minute, from the Apex's status. Prometheus turns the conditions
that are true into alerts, and Alertmanager delivers them: **one message per alarm group, listing every condition
of the group that is true**. The Apex's own alarm e-mail names only one of them.

## Conditions

Each entry under `conditions:` in `config/site.yaml` is one condition. There are three kinds:

```yaml
conditions:
  # a limit: above or below, with a clearing band
  - { id: temperature-high, alarm: tank, sensor: Tmp, above: 82.5, clear: 0.3, severity: critical }
  # a switch: when closed or open; optionally only while an output is energized
  - { id: leak, alarm: tank, sensor: Leak1B, when: closed, severity: critical }
  - { id: fill-valve-open-when-full, alarm: tank, sensor: FreshH, when: closed, output: Topoff_9_2, pending: 120,
      severity: critical, text: "The fresh-water container is full and the fill valve is still on" }
  # a rate: a rise or fall of at least this much within so many minutes
  - { id: temperature-rising-fast, alarm: tank, sensor: Tmp, rise: 1.0, minutes: 30, severity: critical }
```

| Key | Default | Meaning |
|---|---|---|
| `id` | required | unique; it names the alert |
| `alarm` | `tank` | the group: one message lists every true condition of the group |
| `sensor` | required | the Apex's input name |
| `severity` | `warning` | `critical` goes to the phone as well as mail, within seconds |
| `above`, `below` | | a limit, in the sensor's unit |
| `clear` | 0 | how far back inside the limit the value must come before the alert ends |
| `when` | `closed` | for switches: `closed` or `open` |
| `output` | | for switches: only while this output is energized |
| `rise`, `fall`, `minutes` | , , 30 | a change of at least this much within this window |
| `pending` | from `defaults` | seconds the condition must hold before it alerts |
| `text` | written for you | the alert's wording ("Temperature (Tmp) above 82.5 °F") |

How they behave:

- **Pending time:** no single bad sample alerts. The defaults are 180 s for probes, 30 s for switches, 60 s for modules,
  180 s for an EnergyBar's current and 60 s for rates (`defaults:` changes them).
- **Ending:** a probe condition ends once the value is back inside the limit by its clearing band. A switch, module or
  current condition ends once it has been false for its pending time. A rate condition ends once the change over its
  window is under half its limit.
- **A critical condition hides the warnings on the same sensor:** "salinity very low" does not come with "salinity low".
- **When the Apex does not answer, nothing changes:** the last known state holds, and `ReefApexUnreadable` says why.
- **When the worker stops, nothing ends:** the alerts that were firing stay firing (no false all-clear), and
  `ReefWorkerDown` says why.
- **Rate conditions need their window** of polls after a start before they judge.
- **Pick rate limits from your own history:** set each above anything the Charts page shows on a normal day.

Two more kinds come from the `modules:` list without being written out. `<id> is not reporting` fires when a module
stops reporting. `<id> shows no current while ... is on` fires when an EnergyBar shows 0.0 A while an outlet that
always draws is energized. [configuration.md](configuration.md#modules) has the details.

### From your Apex's alarm programs

The Apex raises its alarms through alarm outlets (the e-mail outlet `EmailAlm`, the sound outlets `SndAlm` and
`SndWrn`) whose programs turn the outlet on when a line is true. The Programs page shows them as the console last read
them. Each `If` line becomes one condition:

| Apex program line | Condition |
|---|---|
| `If Tmp > 82.5 Then ON` | `{ id: temperature-high, sensor: Tmp, above: 82.5, clear: 0.3 }` |
| `If pH < 7.90 Then ON` | `{ id: ph-low, sensor: pH, below: 7.90, clear: 0.05 }` |
| `If Leak1B CLOSED Then ON` | `{ id: leak, sensor: Leak1B, when: closed }` |
| `If SumpHi OPEN Then ON` | `{ id: sump-high-float, sensor: SumpHi, when: open }` |
| `Defer 005:00 Then ON` | `pending: 300` on the conditions of that program |

Where two alarm outlets watch the same sensor at different limits (a warning sound at 8.0 pH, an e-mail at 7.9), make
the softer one a `warning` and the harder one `critical`. Lines like `If Error ...` or `If Power ...` have their own
checks here: the module checks, and `ReefApexPowerFailure`. Clearing bands are not in the Apex's programs; something
near a probe's normal day-to-day wobble works (0.3 °F, 0.05 pH, 10 mV, 0.2 ppt).

## Notices

A notice is a one-time message when a switch comes into a state that is normal but worth knowing about, such as a
fresh-water container that is full. It is sent once the switch has held for `hold` seconds, and not again until the
switch has been the other way for `rearm` seconds, so a bobbing float sends one message.

```yaml
notices:
  - { id: fresh-full, sensor: FreshH, when: closed, hold: 30, rearm: 1800, text: "The fresh-water container is full" }
```

The calcium reactor's "effluent drip slowing?" finding is mailed the same way, once.

## Delivery: Prometheus and Alertmanager

`docker compose --profile alerts up -d` starts both, configured from `deploy/`:

- `deploy/prometheus/prometheus.yml` scrapes the worker (job `reef-worker`, port 9106).
- `deploy/prometheus/rules/reef.yml` holds the rules below. `deploy/prometheus/tests/reef.yml` tests them:

  ```bash
  docker run --rm -v "$PWD/deploy/prometheus:/p" --entrypoint promtool prom/prometheus:v3.15.0 test rules /p/tests/reef.yml
  ```
- `deploy/alertmanager/alertmanager.yml` (from the example) says where messages go. Critical alerts go to ntfy and mail
  within seconds, and a further condition in the same group follows within a minute. Warnings go by mail with a
  reminder a day. One-off events (a power failure, a notice, the weekly report) are sent once without a "resolved"
  message.

| Alert | When |
|---|---|
| `ReefCritical`, `ReefWarning` | one per active condition, labelled with its alarm group, condition, subject and wording |
| `ReefWorkerDown` | the worker has not answered Prometheus for 5 minutes (open alerts stay open meanwhile) |
| `ReefApexUnreadable` | the worker answers but has not read the Apex for 10 minutes (off the network, wrong password, busy) |
| `ReefApexPowerFailure` | the Apex reports that its power failed and came back; once |
| `ReefNotice` | a notice, or the reactor's drip warning; once |
| `ReefWeeklyReport` | the weekly report's headline; once |

To check delivery end to end, send a test alert:

```bash
docker compose exec alertmanager amtool alert add reef-test severity=warning \
  --annotation=summary="Test from reef-console" --alertmanager.url=http://localhost:9093
```

### Phone push with ntfy

[ntfy](https://ntfy.sh) delivers pushes to its Android and iOS apps; Alertmanager posts to it as a webhook, and ntfy's
built-in `alertmanager` template makes the message readable. Subscribe the app to the topic in your
`alertmanager.yml`. On the public ntfy.sh server anyone who knows a topic can read and post to it, so use a long random
name, or run your own ntfy server with access control.

### Using a Prometheus you already have

Add a scrape job named `reef-worker`, since the rules look for `job="reef-worker"`. Load
`deploy/prometheus/rules/reef.yml`, and route `source="reef"` in your Alertmanager with `group_by: [alertname, alarm]`,
so one message lists a group's conditions. Leave out the `alerts` profile.

## Metrics

The worker publishes these on `:9106/metrics`. Readings, outlets and modules appear only while the latest poll is
recent, so an old value never passes for the present. Conditions appear always, because they are the worker's
judgement.

| Metric | Labels |
|---|---|
| `reef_apex_up` | 1 if the latest poll worked and is recent |
| `reef_apex_last_successful_poll_timestamp_seconds`, `reef_apex_poll_latency_seconds`, `reef_apex_clock_offset_seconds` | |
| `reef_apex_power_failed_timestamp_seconds`, `reef_apex_power_restored_timestamp_seconds` | |
| `reef_condition_active` | `alarm`, `condition`, `severity`, `subject`, `summary` |
| `reef_condition_since_timestamp_seconds` | `condition` |
| `reef_sensor_value` | `sensor`, `did`, `type`, `unit`, `label` |
| `reef_switch_closed` | `sensor`, `did`, `label` |
| `reef_outlet_energized`, `reef_outlet_manual`, `reef_outlet_healthy` | `outlet`, `did`, `module`, `type` |
| `reef_module_connected`, `reef_module_current_amps` | `module` (and `kind`) |
| `reef_feed_active` | 1 while a Feed cycle runs |
| `reef_notice_timestamp_seconds` | `notice`, `summary` |
| `reef_report_published_timestamp_seconds` | `headline` |

They also make a good Grafana dashboard if you run one.
