# Analysis

Every hour the worker looks at the last four weeks of each probe in 10-minute steps, using its own polls and the
Apex's log. It works out what each probe is expected to read and reports what departs from that. It uses plain
statistics, with no model to take on trust: every finding carries its evidence, and **nothing here pages anyone**.
Findings appear on the Analysis page, in the weekly report and through MCP, and each new one is logged once as an
event. Beside each finding are what the Apex switched meanwhile (its outlet log) and the journal entries near it.
After a water change the drop is then explained, not mysterious.

A probe needs 7 days of history before it is judged; until then the page says it is learning.

## Expected value and spread

For each probe, **expected = trend + daily profile**:

- **Trend:** the median of the 24 hours around each hour. It is computed on the readings with their daily cycle taken
  out, so the newest hours are not pulled by it.
- **Daily profile:** for each 10-minute slot of the day, the median of the reading minus the trend over the window. It
  is the tank's own day: lights, the reactor's CO2, the heaters, the fan.
- **Spread:** 1.4826 times the median absolute residual. This is a standard deviation that ignores outliers, never
  below the probe's resolution.

The daily slots are counted in UTC. If the Apex's clock follows daylight time, its schedules move an hour twice a year,
and the profile takes a week or two to settle after the change.

## Findings

| Finding | What it says | Threshold |
|---|---|---|
| **unusual** | the reading left its expected band for a while (last 7 days) | 4 spreads, and at least 0.5 °F (0.3 °C), 0.15 pH, 25 mV or 0.5 ppt, for 30 minutes or more |
| **drift** | the daily medians of the last 14 days trend one way; with the days until an alert limit is reached at that rate, when that is within 30 days | a Theil-Sen slope beyond 1 °F (0.55 °C), 0.1 pH, 40 mV or 0.4 ppt a week, with most day pairs agreeing |
| **step** | the level changed on one day: equipment added, removed or failing (EnergyBar currents included) | the best single split beyond the drift thresholds (0.3 A for currents), last 14 days |
| **frozen** | the probe has not changed for 6 hours where it normally moves | and has been that still in under 2 % of its past six-hour blocks (a heated tank at night can be still for hours) |
| **recurring** | a departure on most days at the same time, such as a nightly salinity dip | the daily profile beyond a plausible swing (3 °F, 1.7 °C, 0.6 pH, 100 mV, 0.6 ppt); it never becomes "expected" |
| **equipment** | an output draws different current when it switches than it used to (a lamp or driver failing, a heater element gone, a pump clogging) | the bar's current just before a switch against 5-15 minutes after; the last week off its usual by 0.3 A and 30 % |
| **probe** | a probe disagrees with your manual tests, the same way each time: recalibrate? | its reading around each of the newest two or three tests off by more than 0.1 pH, 0.5 ppt, 0.5 °F or 0.3 °C |
| **drip** | the calcium reactor's effluent drip is slowing (below) | the CO2's daytime minutes down 30 % |

The Analysis page also draws a **24-hour forecast band** per probe: the current trend plus the daily profile, plus or
minus 2 spreads.

EnergyBar currents follow what is switched on, not a daily rhythm, so only their level steps and the equipment
fingerprints are judged. Fingerprints are counted only from switchings the console saw itself: the Apex's own outlet
log leaves out outputs that switch at the same moment.

## The calcium reactor

With a `reactor:` section in the site file, the Reactor page shows:

- **per day:** the CO2 solenoid's minutes on, all day and inside its program's night window, how often it switched,
  and the effluent pH by day and by night;
- **tests:** alkalinity, calcium and magnesium with their change per day;
- **the balance:** between consecutive alkalinity tests that each have a calcium test within a day, whether calcium
  moved about 7.15 ppm per dKH, within the kits' errors (±0.3 dKH and ±20 ppm when a test states none).

The ratio is the chemistry of the media. Pure calcium carbonate (aragonite) gives about 7.15 ppm of calcium with every
dKH of alkalinity. Media with magnesium carbonate in it (dolomite, magnesium media) gives alkalinity with less calcium,
and magnesium instead. Corals and precipitation take both away in the same 7.15 ratio. So a gap means one of these:

- the media is not pure calcium carbonate (magnesium rising says so);
- something else adds one without the other: an alkalinity buffer, a calcium supplement, a salt mix of a different
  balance, denitrification;
- or a test is off.

The CO2's minutes a day stand in for the reactor's flow: with the effluent's pH band unchanged, more water through the
reactor needs more CO2. So the page's numbers answer "is the reactor keeping up?" without a flow meter. The worker
makes no consumption figure, no run-out date and no dosing advice, because the reactor's output is not measured.

**Effluent drip slowing?** A reactor fed through a small needle valve slowly loses flow. Less water needs less CO2 to
hold the band, so the CO2's minutes fall while its program is unchanged. The worker compares the last 3 full days
with the 3 before, counting minutes outside the night window. It flags a drop of 30 % or more when each of the last 3
days is below the earlier average and the earlier average was at least 30 minutes a day. It counts only days after the
CO2 program's lines on the effluent probe last changed. The finding shows on the Analysis and Reactor pages and is
mailed once. It is usually the valve closing up.

The night window (`night_apex`) is read on the Apex's own clock, with the timezone setting it had at each moment: the
worker records every change of that setting, which happens if the unit follows daylight time.

## Water tests

Tests are entered on the Tests page or through MCP. Each keeps the value as entered and in the parameter's own unit,
which charts and trends use:

| Parameter | Unit | Also accepted |
|---|---|---|
| alkalinity | dKH | meq/L, ppm as CaCO3 (1 meq/L = 2.8 dKH = 50 ppm) |
| calcium, magnesium, potassium, strontium | ppm | |
| phosphate | ppm PO4 | ppb PO4; ppm or ppb as phosphorus, P (×3.066; the Hanna ULR checker reads ppb P) |
| nitrate | ppm NO3 | ppm NO3-N (×4.427) |
| salinity | ppt | specific gravity (converted at 25 °C, UNESCO EOS-80) |
| pH (manual) | pH | |
| iodine | ppm | ppb |
| nitrite, ammonia | ppm | |

A value implausible for its parameter (81 dKH, a calcium of 4,300) is questioned and stored only after a second
confirmation. A test can carry its kit and the kit's stated error, which the reactor's balance uses. A deleted test is
marked deleted, not removed.

## The weekly report

On the day and hour of the site file's `report:` (Sunday 09:00 by default) the worker writes one page on how the
tank did:

- the alerts;
- the analysis' findings;
- each probe's week against the week before;
- the water tests, and which are due;
- energy per EnergyBar, and its cost once a tariff is set;
- program changes and the journal;
- how well the Apex answered;
- and the outdoor temperature when it is known.

It is kept on the Reports page (which can also preview the 7 days up to now) and given to Claude through MCP. Its
one-line headline is mailed through the `ReefWeeklyReport` alert.
