# Configuration

Four files hold everything that is yours. None of them is committed (they are in `.gitignore`):

| File | Holds | From |
|---|---|---|
| `config/site.yaml` | the tank: the Apex's address, sensors, modules, alert conditions, the reactor | `config/site.example.yaml` |
| `.env` | the Apex's user and password, the sign-in mode, the public address | `.env.example` |
| `config/users.txt` | the web pages' users (name, role and a password hash) | `python -m reefweb.auth NAME ROLE` |
| `deploy/alertmanager/alertmanager.yml` | where alerts go: your mail server, your ntfy topic | `alertmanager.example.yml` |

After an edit of the site file, restart both services (`docker compose restart reef-worker reef-web`). The users file
is read again when it changes; Alertmanager's after `docker compose restart alertmanager`.

## The site file

YAML. The keys of sensors, outlets and modules are **the Apex's own names**: open
`http://<apex>/cgi-bin/status.json` in a browser (it asks for the Apex's sign-in) to see yours. `inputs` lists the
probes and switches with their `name`; `outputs` the outlets with their `name` and `did` (`9_8` is outlet 8 of the
module at AquaBus address 9).

### timezone, apex

```yaml
timezone: Europe/London       # the house's civil time (an IANA name); default UTC
apex:
  address: 192.168.1.50       # the Apex Classic on your network
  poll_seconds: 60            # status.json this often; default 60
```

`timezone` decides what a "day" is (charts, reactor days, the weekly report) and the times the MCP tools show. The
pages show times in the browser's own zone.

### sensors

```yaml
sensors:
  Tmp: { label: Temperature, unit: "°F" }
  pH: { label: pH, unit: pH, test: ph }
  Tmpx5: { label: PM1 temperature, unit: "°F", active: false }
  sumpLo: { label: Sump low float, unit: switch }
  Outdoor: { label: Outdoor temperature, unit: "°F", external: true }
```

| Key | Default | Meaning |
|---|---|---|
| `label` | the name | what the pages call it |
| `unit` | empty | `°F`, `°C`, `pH`, `mV`, `ppt`, `A` (an EnergyBar's current), `switch` for a float or leak sensor; anything else is shown as written |
| `active` | true | false: no history, no alerts (an empty probe port) |
| `test` | none | the manual test that measures the same thing (`ph`, `salinity`): shown beside the probe, and the probe is compared with it |
| `external` | false | not an Apex input: the outdoor temperature from `weather` (charts and the report only) |

The analysis knows `°F`, `°C`, `pH`, `mV`, `ppt` and `A`; probes in other units are stored, charted and alerted on,
but not analysed. Inputs the Apex reports that are not listed here are stored too, so a sensor added to the file later
comes with its history.

### modules

The AquaBus modules you expect. A listed module that stops reporting raises `<id> is not reporting`; a module that
reports and is not listed is logged once as new and left alone.

```yaml
modules:
  - { id: EB8_9, kind: EB8, address: "9", current: Amp_9, draws_when_on: [mainpump_9_8], severity: critical }
  - { id: PM2_6, kind: PM2, address: "6", severity: critical }
```

| Key | Meaning |
|---|---|
| `id` | your name for it, used in alerts (`EB8_9`) |
| `kind` | `EB8` gets the EnergyBar checks below; any other value (`PM1`, `PM2`, `inputs`, ...) is only a label |
| `address` | its AquaBus address, the number before the underscore in its devices' `did` |
| `severity` | `critical` (default) or `warning` |
| `current` | an EnergyBar's current input (`Amp_9`) |
| `draws_when_on` | outlets that always draw current when energized (a return pump, a reactor pump) |

An EnergyBar counts as lost when none of its outlets is listed, any of them is not reported healthy, or its current
input is missing. Another module counts as lost when none of its inputs or outputs is listed. With `current` and
`draws_when_on`, a bar that shows no current while one of those outlets is on raises `<id> shows no current while ...
is on`: the bar lost power, or the device stopped.

### defaults, conditions, notices

The alert conditions: [alerts.md](alerts.md) explains them and how to import yours from the Apex's alarm programs.

```yaml
defaults: { probe_pending: 180, switch_pending: 30, module_pending: 60, current_pending: 180, rate_pending: 60 }
conditions:
  - { id: temperature-high, alarm: tank, sensor: Tmp, above: 82.5, clear: 0.3, severity: critical }
  - { id: sump-low-float, alarm: tank, sensor: sumpLo, when: closed, severity: critical }
  - { id: temperature-rising-fast, alarm: tank, sensor: Tmp, rise: 1.0, minutes: 30, severity: critical }
notices:
  - { id: fresh-full, sensor: FreshH, when: closed, text: "The fresh-water container is full" }
```

### datalog: history from the unit's own log

```yaml
datalog:
  backfill: false        # true: also fetch the days before the console started (the Classic keeps about a month)
  hours: [22, 6]         # local hours when a day may be asked for (from, to; may wrap midnight); default [1, 5]
  spacing_seconds: 300   # between two requests
  days_back: 40          # the oldest day asked for
```

Gaps after polling began (the worker was down, the unit did not answer) are always filled. Building one day of its
log takes the Apex several seconds, so the worker asks for one day at a time, in these hours, minutes apart, only
while the unit answers its status quickly, and stops for the night at the first error. A logged value is stored only
where no poll covers its 10-minute step. Turn `backfill` on once, the first night, to import the month the unit holds.

### energy, report, weather

```yaml
energy:
  volts: 120              # kWh = amps x volts x hours (the power factor taken as 1)
  price_per_kwh: 0.18     # all-in tariff; null shows kWh only
report:
  weekday: sunday         # the weekly report's day and local hour
  hour: 9
  tests_due: { alkalinity: 7, calcium: 14, magnesium: 30, phosphate: 14, nitrate: 14 }   # days between tests
weather:
  latitude: 51.51         # the outdoor temperature from Open-Meteo (free, no key); null: off
  longitude: -0.13        # two decimals (about a kilometre) is plenty; nothing else is sent
```

The weather needs a sensor with `external: true`; its `unit` (`°F` or `°C`) decides what Open-Meteo is asked for.

### reactor

```yaml
reactor:
  co2: Newco21_3_1                 # the CO2 solenoid's outlet
  effluent: pHx5                   # the effluent pH probe
  night_apex: ["00:00", "05:00"]   # the CO2 program's night window, on the Apex's own clock (optional)
```

Leave the section out if there is no calcium reactor. [analysis.md](analysis.md#the-calcium-reactor) explains the
page.

## Environment settings

`compose.yaml` sets these; `.env` overrides the ones it names.

**Worker**

| Setting | Default | |
|---|---|---|
| `REEF_APEX_USER`, `REEF_APEX_PASSWORD` | | the Apex's web sign-in; with these the address comes from the site file |
| `REEF_SECRET_FILE` | `/run/secrets/apex` | instead of the two above: a CSV file, a header line `ip, username, password` and one row |
| `REEF_SITE_FILE` | `/app/config/site.yaml` | |
| `REEF_DATA_DIR` | `/data` | the SQLite file `reef.db`, and `backups/` |
| `REEF_METRICS_PORT` | `9106` | Prometheus metrics |

**Web app**

| Setting | Default | |
|---|---|---|
| `REEF_AUTH` | `basic` (`forward` when `REEF_AUTH_URL` is set) | the sign-in mode, below |
| `REEF_USERS_FILE` | `/app/config/users.txt` | basic mode's users |
| `REEF_AUTH_URL`, `REEF_AUTH_COOKIE`, `REEF_SIGN_IN_URL` | , `session`, | forward mode, below |
| `REEF_PUBLIC_URL` | `http://localhost:8001/reef` | the address people and MCP clients use |
| `REEF_EXTRA_HOSTS` | | other host names the MCP endpoint answers to, comma-separated |
| `REEF_HOME_URL`, `REEF_HOME_LABEL` | , `Home` | a link back to a home page, at the foot of the menu |
| `REEF_PORT` | `8001` | (compose) the published port |

The MCP endpoint answers only to the host of `REEF_PUBLIC_URL` and `REEF_EXTRA_HOSTS`, a guard against DNS rebinding:
set it to the name you type in the browser.

## Sign-in

Three modes, set with `REEF_AUTH`. In every mode a write from a browser must carry the header
`X-Requested-With: reef-console`, which the app adds and a cross-site form cannot, so another web page cannot record
anything in your name.

**basic** (the default): reef-web's own users in `config/users.txt`, one line each, `name:role:hash`. The browser
shows its own password prompt when the page opens. Roles: `viewer` reads; `agent` and `admin` may also record water
tests and journal entries. Make a line (the password is asked for, never shown):

```bash
docker compose run --rm --no-deps reef-web python -m reefweb.auth alex admin
```

Paste the printed line into `config/users.txt`; the file is read again when it changes. Scripts and MCP clients send
`Authorization: Bearer name:password`. Hashes are PBKDF2-SHA256 with 600,000 iterations. Basic sends the password with
every request, so outside a trusted LAN use HTTPS.

**none**: no sign-in; everyone is `local`, an admin. Only on a network where everyone may read and record. MCP
clients then send any bearer token.

**forward**: another service holds the sign-in, such as a homelab dashboard or an SSO proxy. reef-web asks
`REEF_AUTH_URL` about every caller (cached for a minute). It forwards the caller's session cookie (named by
`REEF_AUTH_COOKIE`) or its `Authorization: Bearer` header, and expects:

```json
{"name": "alex", "role": "admin", "kind": "human", "can_request": true}
```

or a 401/403. `can_request` decides whether the caller may record. A browser without a session is sent to
`REEF_SIGN_IN_URL`. Put a reverse proxy in front that checks every request the same way (Caddy's `forward_auth`,
Traefik's ForwardAuth, nginx's `auth_request`), so the app's files are not served to strangers either.

### HTTPS with a reverse proxy

The pages live under `/reef/`, so they can share a host name with other things. A Caddy example (Caddy gets the
certificate itself for a public name, or uses its internal CA with `tls internal`):

```
reef.example.com {
	redir / /reef/
	reverse_proxy /reef* reef-web:8001
}
```

With a proxy in front, set `REEF_PUBLIC_URL` to the address it serves (`https://reef.example.com/reef`). Do not
publish the console to the internet without a sign-in; a VPN to your LAN is the safer way to reach it from a phone.

## Data and backups

Everything is in one SQLite file, `reef.db`, in the `reef-data` volume. The worker writes a consistent copy every
night to `backups/` beside it and keeps 14. Those copies are on the same disk, so copy one elsewhere now and then:

```bash
docker compose cp reef-worker:/data/backups ./reef-backups
```

To restore, stop the worker, put the copy in place as `/data/reef.db` (and remove `reef.db-wal` and `reef.db-shm`),
and start it again. The schema is created and extended at start, so an update is `git pull`, then
`docker compose build`, then `docker compose up -d`.
