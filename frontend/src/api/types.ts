// The JSON reef-web's API answers (worker/reefweb). Times are UTC epoch seconds. The frontend tests render answers
// the backend's own tests produced (src/test/api/*.json, copied in by the image build), so these types are checked
// against the real thing on every build.

export type Severity = 'critical' | 'warning';
export type Series = [number, number][]; // [ts, value]

export interface Me {
  name: string;
  role: string;
  can_write: boolean;
  home: { label: string; href: string } | null; // a link back to a home page (REEF_HOME_URL), at the foot of the menu
}

export interface Poll {
  ok: boolean;
  at: number | null;
  error: string | null;
  last_good: number | null;
  current: boolean;
}

export interface ConditionState {
  id: string;
  alarm: string;
  severity: Severity;
  subject: string;
  text: string;
  active: boolean;
  held: boolean;
  since: number | null;
  detail: string;
}

export interface Probe {
  name: string;
  label: string;
  unit: string;
  value: number;
  alarm: boolean;
  history: Series;
}

export interface SwitchInput {
  name: string;
  label: string;
  closed: boolean;
  alarm: boolean;
}

export interface ModuleState {
  id: string;
  kind: string;
  address: string;
  connected: boolean;
  why: string;
  amps: number | null;
  alarm: boolean;
  history: Series;
}

export interface Output {
  name: string;
  did: string;
  type: string;
  mode: string;
  health: string;
  intensity: number | null;
  manual: boolean;
  energized: boolean;
}

export interface OutputGroup {
  id: string;
  title: string;
  outputs: Output[];
  unused?: number;
}

export interface Override {
  name: string;
  mode: string;
  since: number | null;
  ignored: string[];
  program_known: boolean;
}

export interface EventItem {
  ts: number;
  kind: string;
  subject: string;
  detail: string;
}

export interface Apex {
  serial: string;
  software: string;
  timezone: string;
  feed_active: boolean;
  power_failed: number | null;
  power_restored: number | null;
  latency: number;
  received_at: number;
}

export interface Overview {
  now: number;
  poll: Poll;
  alerts: ConditionState[];
  history_since: number | null;
  polling_since: number | null;
  events: EventItem[];
  apex: Apex | null;
  probes: Probe[];
  switches: SwitchInput[];
  modules: ModuleState[];
  groups: OutputGroup[];
  overrides: Override[];
  energy: Energy;
  external_sensors: { name: string; label: string; unit: string }[];
}

export interface EnergyBar {
  module: string;
  amps: number;
  kwh_per_day: number;
  cost_per_month: number | null;
}

export interface Energy {
  volts: number;
  price_per_kwh: number | null;
  bars: EnergyBar[]; // the last 7 days
}

export interface WaterTest {
  id: number;
  measured_at: number;
  parameter: string;
  value: number;
  unit: string;
  entered_value: number;
  entered_unit: string;
  kit: string;
  kit_error: number | null;
  note: string;
  entered_at: number;
  entered_by: string;
  source: string;
}

export interface LatestTest extends WaterTest {
  label: string;
  previous: WaterTest | null;
}

export interface Parameter {
  id: string;
  label: string;
  unit: string;
  units: string[];
  plausible: [number, number];
  decimals: number;
}

export interface TestsOut {
  tests: WaterTest[];
  latest: LatestTest[];
  parameters: Parameter[];
}

export interface WaterTestIn {
  parameter: string;
  value: number;
  unit: string | null;
  measured_at: number;
  kit: string;
  kit_error: number | null;
  note: string;
  unusual_ok: boolean;
}

export interface VersionSummary {
  id: number;
  ts: number;
  serial: string;
  outputs: number;
  first: boolean;
  summary: string;
}

export interface ProgramChange {
  output: string;
  change: 'added' | 'removed' | 'changed';
  lines: [string, string][]; // [op, text]: "+", "-" or " "
}

export interface ProgramVersion {
  id: number;
  ts: number;
  serial: string;
  previous: { id: number; ts: number } | null;
  changes: ProgramChange[];
  programs: Record<string, string[]>;
}

export interface History {
  sensor: string;
  label: string;
  unit: string;
  step_seconds?: number;
  points?: [number, number, number, number][]; // [ts, mean, min, max]
  state_before?: string | null;
  changes?: [number, string][];
  tests?: BesideProbe[]; // the manual tests of what this probe measures
}

export interface BesideProbe {
  measured_at: number;
  value: number;
  unit: string;
  kit: string;
  probe: number | null;
  difference: number | null;
}

export interface Condition {
  id: string;
  alarm: string;
  severity: Severity;
  text: string;
  kind: string;
  subject: string;
  limit: number | null;
  clear: number | null;
  pending_seconds: number;
  active: boolean;
  held: boolean;
  since: number | null;
  detail: string | null;
}

export interface Finding {
  kind: 'unusual' | 'drift' | 'step' | 'frozen' | 'recurring' | 'probe' | 'equipment' | 'drip';
  sensor: string;
  start: number | null;
  end: number | null;
  summary: string;
  evidence: Record<string, unknown>;
}

export interface SensorAnalysis {
  sensor: string;
  label: string;
  unit: string;
  learning: boolean;
  days: number;
  spread?: number;
  daily_range?: number;
  findings?: number;
  recent?: (number | null)[][]; // [ts, reading, expected, low, high] every 30 minutes, last 7 days
  forecast?: number[][]; // [ts, expected, low, high] every 30 minutes, next 24 hours
}

export interface Fingerprint {
  output: string;
  current: string; // the EnergyBar's current input
  draws: number; // amps, the median step when it switches
  switchings: number;
  lately?: number;
}

export interface AnalysisOut {
  computed_at: number | null;
  window_days: number | null;
  sensors: SensorAnalysis[];
  findings: Finding[];
  equipment?: Fingerprint[];
}

export interface ReportSummary {
  id: number;
  ts: number;
  period_start: number;
  period_end: number;
  headline: string;
}

export interface Report {
  id: number | null; // null: a preview, not kept
  ts: number;
  period_start: number;
  period_end: number;
  headline: string;
  sections: { title: string; lines: string[] }[];
}

export interface JournalEntry {
  id: number;
  ts: number;
  kind: string;
  text: string;
  entered_at: number;
  entered_by: string;
  source: string;
}

export interface JournalOut {
  entries: JournalEntry[];
  kinds: string[];
}

export interface ReactorDay {
  day: string; // YYYY-MM-DD, the house's civil day
  co2_minutes: number | null; // null: before the minute polls began
  co2_night_minutes: number | null;
  switchings: number | null;
  effluent_day: number | null;
  effluent_night: number | null;
}

export interface ReactorTest {
  ts: number;
  value: number;
  unit: string;
  kit: string;
  kit_error: number | null;
  per_day: number | null;
}

export interface ReactorBalance {
  from: number;
  to: number;
  alk_change: number;
  ca_change: number;
  ca_expected: number;
  margin: number;
  verdict: 'in step' | 'calcium behind' | 'calcium ahead';
}

export interface ReactorOut {
  configured: boolean;
  co2: string;
  effluent: string;
  night_apex: [string, string] | null;
  ca_per_dkh: number;
  days: ReactorDay[];
  tests: Record<string, ReactorTest[]>;
  balance: ReactorBalance[];
  note: string | null;
  since: number;
  drip: Finding | null;
}

export type RangeName = '1h' | '6h' | '24h' | '7d' | '30d' | '90d' | '365d';
