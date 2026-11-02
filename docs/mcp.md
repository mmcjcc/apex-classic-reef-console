# MCP and Claude

reef-web serves an MCP endpoint at `/reef/mcp` (Streamable HTTP), so Claude Code, the Claude apps and other MCP
clients can read the tank. Ask "how did pH do this week?", "why did salinity drop last night?", "is the reactor
keeping up with alkalinity?", or "alk 8.1 dKH this morning, Hanna kit". The last one records a test after you have
seen what will be stored.

**Nothing over MCP changes an outlet, a program or anything on the Apex.** The tools read the worker's database; the
only writes are water tests and journal entries, each shown first and stored only when called again with
`confirm=true`.

## Connecting

Make a user for the client in `config/users.txt` (basic mode). Give it the role `agent` if it may record tests and
journal entries, or `viewer` if it only reads:

```bash
docker compose run --rm --no-deps reef-web python -m reefweb.auth claude agent
```

The client sends `Authorization: Bearer claude:<that password>`. For Claude Code, a `.mcp.json` in a project folder
keeps the token out of the file:

```json
{
  "mcpServers": {
    "reef": {
      "type": "http",
      "url": "https://reef.example.com/reef/mcp",
      "headers": { "Authorization": "Bearer ${REEF_MCP_TOKEN}" }
    }
  }
}
```

Then set `REEF_MCP_TOKEN=claude:<password>` in your environment. Or add it from the command line:

```bash
claude mcp add --transport http reef https://reef.example.com/reef/mcp --header "Authorization: Bearer claude:<password>"
```

The endpoint answers only to the host name in `REEF_PUBLIC_URL` (and `REEF_EXTRA_HOSTS`): use the same name in the
URL. In forward mode the token is one of the sign-in service's tokens; in none mode any token works.

## Tools

Times come back as ISO 8601 in the site file's `timezone`.

| Tool | Reads |
|---|---|
| `status` | the tank now: whether the Apex answers, the alerts, every probe and switch, modules, Feed, power |
| `outputs` | every output by EnergyBar: its mode (AON/AOF automatic, ON/OFF manual) and whether it is energized |
| `sensor_history` | one input's history (1h to 365d): a probe as time, mean, min and max per step; a switch as its changes |
| `output_history` | one output's changes of mode or energized state |
| `conditions` | every alert condition: limit, clearing band, pending time, severity, whether it is active now |
| `events` | alerts started and ended, power failures, clock steps, program changes, new modules, analysis findings |
| `programs` | the Apex's programs as last read (once a day): every output's lines, or one output's |
| `program_history` | the dated versions of the programs, and one version's line-by-line differences |
| `water_tests` | manual tests, newest first, with the kit, its error and who entered them |
| `analysis` | the hourly analysis: findings with evidence, each probe's expected pattern and 24-hour forecast |
| `reactor` | the calcium reactor's CO2 minutes and effluent pH per day, the tests, and the calcium-alkalinity balance |
| `journal` | water changes, dosing, calibrations, maintenance, equipment, livestock |
| `weekly_report` | the latest weekly report as text, an earlier one, or a preview of the last 7 days |
| `record_water_test` | stores a test after a preview (`confirm=true`); an implausible value needs `unusual_ok=true` too |
| `record_journal_entry` | stores a journal entry after a preview (`confirm=true`) |

Recording needs an `agent` or `admin` user; a `viewer` gets an error that says so.
