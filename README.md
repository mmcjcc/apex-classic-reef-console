# reef-console

**A local console for a reef aquarium run by a Neptune Apex Classic: long-term history, alerts that name every
condition, water tests, plain-statistics analysis, and an MCP endpoint so Claude can read the tank.**

The Apex Classic is an older controller with little open software around it. Its own tools have gaps: an alarm e-mail
names only one of the conditions that are true, the Classic's e-mail has no TLS (Gmail refuses it), the unit keeps
only about a month of history, and nothing looks at the trends. reef-console runs beside it on any always-on host with
Docker. It reads the Apex's local web interface once a minute, keeps everything in one SQLite file, and gives you:

- **Live status:** every probe, float switch and leak sensor; every outlet's mode (auto or manual) and whether it is
  energized; each EnergyBar's current and the AquaBus modules; Feed cycles and power failures. **Manual overrides are
  listed with the program lines they switch off** (a light held ON by hand no longer obeys its `If Tmp > 82 Then OFF`).
- **History:** one sample a minute in SQLite, charts from an hour to a year. Gaps (the worker was down, the unit did
  not answer) are filled from the Apex's own 10-minute log in quiet hours, and you can import the month the unit still
  holds.
- **Alerts that name every condition:** the conditions come from your Apex's own alarm programs, plus checks the Apex
  does not make. These include an EnergyBar that stopped reporting, a bar with no current while its return pump is on,
  temperature rising 1 °F in 30 minutes, and a fill valve still on after the container is full.
  - Each condition holds for a pending time before it alerts and ends only once it is back inside a clearing band.
  - **One message per alarm lists every condition that is true.** A worker that stops never sends a false all-clear.
  - Prometheus and Alertmanager deliver them by e-mail (STARTTLS) and phone push (ntfy).
- **Water tests:** alkalinity, calcium, magnesium, phosphate, nitrate, salinity and more, entered on the phone or
  through Claude. Units are converted (143 ppm alkalinity is 8.0 dKH; specific gravity becomes ppt). An implausible
  value is questioned before it is stored. Probes are compared with the matching tests ("pH reads 0.15 above your
  last three tests: recalibrate?").
- **Analysis, every hour, with its evidence:**
  - each probe's expected daily pattern;
  - unusual spells, drifts (with the days until an alert limit at that rate), level steps and frozen probes;
  - departures that recur at the same time most days;
  - what each outlet draws when it switches (a heater or pump that starts drawing less);
  - a 24-hour forecast band.
  
  Never paged, always beside what the Apex switched meanwhile and your journal entries.
- **Calcium reactor page:**
  - the CO2 solenoid's minutes per day and the effluent pH, by day and by night;
  - alkalinity, calcium and magnesium tests with their change per day;
  - whether calcium and alkalinity moved in the ratio a reactor dissolves them in;
  - an "effluent drip slowing?" warning when the CO2 runs 30 % less with its pH band unchanged.
- **Program history:** the Apex's programs read once a day, kept as dated versions with line-by-line differences.
- **Journal and weekly report:** the journal holds water changes, dosing, calibrations and equipment. The weekly report
  covers alerts, findings, each probe's week against the one before, the tests that are due, energy per EnergyBar
  (and its cost) and program changes; its headline is mailed.
- **An installable web app** (a PWA), phone-first with a dark theme, and a JSON API behind it.
- **MCP for Claude Code and other MCP clients:** 15 tools to read the status, history, conditions, events, programs,
  tests, analysis, the reactor and the reports. Recording a water test or a journal entry needs a confirmation.

**It never writes to the Apex.** It changes no outlet and edits no program; those stay in Fusion or on the Apex's own
pages. Outlet control with approvals, time limits and interlocks is designed but not built. When it comes, it will be
off by default: outlets keep animals alive.

## Status

A working tool on one tank since October 2026. It has been tried on **one Apex Classic** (AC4, firmware 4.53_4D19),
with three EnergyBar 8s, a PM1, a PM2 and an input module. Other Classic setups should work, since the names come
from your own unit, but they are untested. The newer Apex models (with their `/rest` interface) have not been tried.
Issues and pull requests are welcome.

## Quick start

You need the Apex Classic's address and the user and password of its own web pages, and a Linux host with Docker
Compose on the same network (always on, since it is the thing watching the tank).

```bash
git clone https://github.com/mmcjcc/apex-classic-reef-console.git reef-console && cd reef-console
cp config/site.example.yaml config/site.yaml   # then edit: the Apex's address, your sensors, outlets and limits
cp .env.example .env                           # then edit: the Apex's user and password
docker compose build                           # runs every test, then builds the image
```

Make a user for the web pages. The command asks for the password and prints one line, which goes into
`config/users.txt` (`viewer` reads, `agent` and `admin` may also record tests and journal entries):

```bash
docker compose run --rm --no-deps reef-web python -m reefweb.auth alex admin
```

Then start it, and open `http://<host>:8001/reef/`:

```bash
docker compose up -d
docker compose logs -f reef-worker   # "reef worker started: polling ..." and no errors
```

For alerts by mail and phone, copy `deploy/alertmanager/alertmanager.example.yml` to
`deploy/alertmanager/alertmanager.yml`, fill in your mail server and ntfy topic, and start the `alerts` profile:

```bash
docker compose --profile alerts up -d
```

Start with the Apex's own alarm e-mail still on, and turn it down once you trust this one. Beyond your LAN, put an
HTTPS reverse proxy in front: [docs/configuration.md](docs/configuration.md#sign-in) covers sign-in, proxies and the
other settings.

## Documentation

| | |
|---|---|
| [Configuration](docs/configuration.md) | the site file key by key, the environment settings, sign-in modes, reverse proxies |
| [Alerts](docs/alerts.md) | how conditions work, turning your Apex's alarm programs into conditions, Prometheus and Alertmanager, ntfy |
| [Analysis](docs/analysis.md) | what each finding means, probes against tests, the calcium reactor page, the weekly report |
| [MCP and Claude](docs/mcp.md) | connecting Claude Code, the tools, what they may and may not do |
| [The Apex Classic's local interface](docs/apex-classic.md) | endpoints, fields and quirks found along the way: useful for any Classic project |
| [Architecture](docs/architecture.md) | the parts, the database, why the worker is gentle with the unit, the security model |
| [Development](docs/development.md) | the layout, tests, running the pages against a dev server |

## Safety

reef-console watches the tank; it does not keep anything alive. Keep the Apex's own programs and alarms as the first
line: Fusion's alarms keep working when this console, its host or your network is down. The console tells you when it
stops (`ReefWorkerDown`), but only if Alertmanager runs somewhere that is still up.

## Licence

MIT, see [LICENSE](LICENSE). Not affiliated with or endorsed by Neptune Systems. Apex, Fusion and EnergyBar are their
trademarks, named here only to say what this works with.
