# The Apex Classic's local interface

Notes on the Neptune Apex Classic's local web interface, as read from one unit (an AC4 on firmware 4.53_4D19) while
building this project. They may help anyone writing software for the Classic. Everything here was read from the unit
unless marked otherwise. This project only reads; it never writes to the unit.

## Access

- Plain HTTP on port 80 with **HTTP Basic authentication**: the user and password of the Apex's own web pages. There
  is no TLS, so the password crosses your LAN with every request. That is a limit of the device; keep it on a trusted
  network, ideally a VLAN of its own.
- Everything is under `/cgi-bin/`. There is no `/rest/` interface on the Classic (the newer Apex models have one).
- Fusion (Neptune's cloud), the Apex's own pages and any local client share one small embedded web server.

## Endpoints

| Request | Answer | Time on the unit |
|---|---|---|
| `GET /cgi-bin/status.json` (also `status.xml`) | the live state: inputs, outputs, Feed, power, clock | 0.2 s |
| `GET /cgi-bin/datalog.json?sdate=YYMMDD&days=0` | the probe log, 10-minute steps | about 8 s per day |
| `GET /cgi-bin/outlog.json?sdate=YYMMDD&days=0` | outlet changes | 0.4 s per day |
| `GET /cgi-bin/program.xml` | every output's program (about 19 KB for 39 outputs) | 5 s |

### status.json

```json
{"istat": {
  "hostname": "apex", "software": "4.53_4D19", "hardware": "1.0", "serial": "AC4:12345",
  "timezone": "-3.00", "date": 1791766052,
  "feed": {"name": 0, "active": 0},
  "power": {"failed": 1791076600, "restored": 1791076673},
  "inputs": [{"did": "base_Temp", "type": "Temp", "name": "Tmp", "value": 79.0},
             {"did": "6_1", "type": "Cond", "name": "Condx6", "value": 35.5},
             {"did": "base_I1", "type": "digital", "name": "sumpLo", "value": 0}],
  "outputs": [{"did": "9_8", "type": "outlet", "name": "mainpump_9_8", "status": ["AON", "", "OK", ""]}],
  "link": {"...": "your Fusion link key: keep it out of anything you share"}
}}
```

- **`date` and the power times are true UTC epoch seconds.** The unit's local time is `date` plus its `timezone`
  setting, in hours.
- **`did`** is `<AquaBus address>_<n>` for module devices: `9_8` is outlet 8 of the EnergyBar at address 9, `3_0` that
  bar's current input. Base unit devices are `base_<x>`: `base_Temp`, `base_pH`, `base_I1` for switch input 1.
- **Switches** (`type: digital`) read **0 when OPEN and 60 when CLOSED**.
- **Outputs:** `status[0]` is the mode: `AON` and `AOF` are automatic (the program says on or off); `ON` and `OFF` are
  a manual override. A variable-speed output in AUTO shows its profile's name there, with an `intensity`.
  `status[2]` is the health, `OK` while the module reports it. `type` is `outlet`, `variable`, `alert` (the alarm
  outlets) or `virtual`.
- **Virtual outlets** (`Cntl_A1`...`Cntl_A8`, default names like Light, Pump, Heater) switch no socket. They are
  listed among the outputs and can look like a phantom EnergyBar.
- **Power:** "out now" means `failed` is later than `restored`. The two timestamps stay from the last event, so a
  recent `restored` is a recent power failure.
- **`feed.active`** is set while a Feed cycle runs; `name` says which one.
- **`link`** holds the unit's Fusion link key. Remove it before you post a status.json anywhere.

### datalog.json

`{"ilog": {"serial", "timezone", "record": [{"date": <UTC epoch>, "data": [{"name", "did", "type", "value": "80.8"}]}]}}`:
every probe every 10 minutes (144 records a day, about 92 KB).

- `sdate` is `YYMMDD`, or `YYMMDDhhmm`, and means midnight **in the unit's timezone**, not UTC.
- `days=0` returns one day. `days=1` returned **two** (the start day plus one).
- The unit kept about 40 days. The log is said to share a small flash memory with the web pages.
- It is slow: one day took 8 s, two days 15-21 s, and the unit serves nothing else meanwhile. Ask for one day at a
  time, minutes apart, in quiet hours.

### outlog.json

`{"olog": {"serial", "record": [{"date": <UTC epoch>, "did": "3_3", "name": "Skim_3_2", "value": "ON"}]}}`.

- **`value` is whether the output was energized, not its mode.** A program cycle and a manual override look alike
  here.
- It can leave out outputs that switch at the same moment, such as several lights on one schedule. Polling
  status.json sees them.
- It does show short cycles that a once-a-minute poll misses, so this project reads it hourly to complete the record.

### program.xml

`<proglist><outlet><name/><outputID/><outputType/><program/></outlet>...</proglist>`: each output's program as text,
one statement per line (`Fallback OFF`, `Set ON`, `If Tmp > 82.0 Then OFF`, `If Time 12:00 to 20:30 Then ON`,
`Defer 005:00 Then ON`, `If FeedA 000 Then OFF`, `OSC 000:00/005:00/005:00 Then ON`, ...). The alarm outlets
(`EmailAlm`, `SndAlm`, `SndWrn`) carry the alarm conditions. Outputs get renamed, so key anything long-lived to the
`did`, not the name.

## The clock

- The unit's `timezone` setting is an offset in hours (`-3.00`), and its programs' `If Time` lines run on that clock.
- The unit observed here **changed its setting with daylight time**: `-3.00` became `-4.00` on the night US daylight
  time ended. Read the offset with each poll and remember when it changes. A night window in a program is then
  matched correctly on both sides of the change.
- The setting on this unit was an hour away from the house's own zone, all year. That is easy to miss: check yours
  against Fusion before you read any `If Time` line.
- Like any small device's clock, the unit's can drift or step after a power problem. Use your own clock as the time
  axis and keep the unit's as a diagnostic (this project flags a difference of more than a few minutes).

## Being gentle

The Classic is a small embedded web server. Other owners report pages failing and the unit restarting under heavy
use, and a two-day log request kept it busy for 20 seconds. This project:

- sends **one request at a time**, through one client, with a priority order (status first);
- backs off when an answer takes longer than 5 s or fails (2, then 5, then 10 minutes) and asks nothing else
  meanwhile;
- polls status.json once a minute, the outlet log hourly and program.xml daily;
- asks for the probe log only to fill a gap, one day per request, five minutes apart, at night, only while status
  answers are quick, and stops for the night at the first error.

## Writing

This project does not write to the unit. The open-source Home Assistant integration
[itchannel/apex-ha](https://github.com/itchannel/apex-ha) sets outlets on the Classic through `/cgi-bin/status.cgi`.
That is not tried here. If you write outlets, remember the unit's programs keep running: a manual override switches
off every safety line in that outlet's program until it goes back to AUTO.
