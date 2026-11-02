// The analysis of the probe history (the worker's, hourly): findings with their evidence, and each probe's last week
// against what was expected for it, followed by the next 24 hours' expected band. Statistics, never alerts.
import { LineChart } from '@mantine/charts';
import { Alert, Badge, Card, Group, Select, SimpleGrid, Stack, Table, Text } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api/client';
import type { AnalysisOut, Finding, SensorAnalysis } from '../api/types';
import { Loaded, PageHeader, Section } from '../components/ui';
import { clock, longClock, reading, unitText } from '../lib/format';

const KIND: Record<Finding['kind'], { color: string; label: string }> = {
  frozen: { color: 'red', label: 'frozen' },
  unusual: { color: 'yellow', label: 'unusual' },
  drift: { color: 'orange', label: 'drift' },
  step: { color: 'blue', label: 'level step' },
  recurring: { color: 'grape', label: 'recurring' },
  probe: { color: 'pink', label: 'probe' },
  equipment: { color: 'cyan', label: 'equipment' },
  drip: { color: 'orange', label: 'reactor drip' },
};

export function AnalysisPage() {
  const query = useQuery({ queryKey: ['analysis'], queryFn: api.analysis, refetchInterval: 300_000 });
  return (
    <>
      <PageHeader
        title="Analysis"
        subtitle="Each probe against its own usual day over the last four weeks: what is unusual, drifting, stepped or stuck. Statistics with their evidence, not alerts."
      />
      <Loaded query={query}>{(data) => <Analysis data={data} />}</Loaded>
    </>
  );
}

function Analysis({ data }: { data: AnalysisOut }) {
  const ready = data.sensors.filter((s) => !s.learning);
  const [sensor, setSensor] = useState<string | null>(ready[0]?.sensor ?? null);
  if (!data.computed_at) {
    return <Alert title="Not yet">The first analysis runs within the hour after the worker starts.</Alert>;
  }
  const chosen = ready.find((s) => s.sensor === sensor) ?? ready[0];
  return (
    <Stack gap="lg">
      <Section title={`Findings (${data.findings.length})`} hint={`Computed ${clock(data.computed_at)} from up to ${data.window_days} days.`}>
        {data.findings.length === 0 ? (
          <Text c="dimmed">Nothing out of the ordinary: every probe follows its usual day.</Text>
        ) : (
          <Stack gap="xs">
            {data.findings.map((f, n) => (
              <Card key={`${f.kind}-${f.sensor}-${f.start}-${n}`} padding="sm">
                <Group gap="xs" wrap="wrap" align="baseline">
                  <Badge color={KIND[f.kind].color} variant="light">
                    {KIND[f.kind].label}
                  </Badge>
                  <Text fw={600}>{f.summary}</Text>
                  <Text size="sm" c="dimmed">
                    {f.end ? `${longClock(f.start)} – ${longClock(f.end)}` : `since ${clock(f.start)}`}
                  </Text>
                </Group>
                {switched(f) && (
                  <Text size="xs" c="dimmed" mt={4}>
                    Switched meanwhile: {switched(f)}
                  </Text>
                )}
                {noted(f) && (
                  <Text size="xs" c="dimmed" mt={2}>
                    Journal: {noted(f)}
                  </Text>
                )}
              </Card>
            ))}
          </Stack>
        )}
      </Section>
      {chosen && (
        <Section title="Last week against what was expected">
          <Card padding="md">
            <Group justify="space-between" mb="xs" wrap="wrap">
              <Select
                data={ready.map((s) => ({ value: s.sensor, label: s.label }))}
                value={chosen.sensor}
                onChange={setSensor}
                allowDeselect={false}
                w={260}
                aria-label="Probe"
              />
              <Text size="sm" c="dimmed">
                Band: where a reading starts to count as unusual; then the next 24 hours
              </Text>
            </Group>
            <WeekChart sensor={chosen} />
          </Card>
        </Section>
      )}
      {data.equipment && data.equipment.length > 0 && (
        <Section
          title="What each output draws when it switches"
          hint="From the EnergyBar's current just before and after each switching (the outlet log); a change against the usual is a finding."
        >
          <Card padding={0}>
            <Table.ScrollContainer minWidth={480}>
              <Table>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Output</Table.Th>
                    <Table.Th>Bar</Table.Th>
                    <Table.Th>Usually draws</Table.Th>
                    <Table.Th>This week</Table.Th>
                    <Table.Th>Switchings</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {data.equipment.map((e) => (
                    <Table.Tr key={e.output}>
                      <Table.Td ff="monospace">{e.output}</Table.Td>
                      <Table.Td>{e.current}</Table.Td>
                      <Table.Td>{e.draws.toFixed(1)} A</Table.Td>
                      <Table.Td>{e.lately === undefined ? '' : `${e.lately.toFixed(1)} A`}</Table.Td>
                      <Table.Td>{e.switchings}</Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          </Card>
        </Section>
      )}
      <Section title="Each probe">
        <Card padding={0}>
          <Table.ScrollContainer minWidth={560}>
            <Table>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Probe</Table.Th>
                  <Table.Th>History</Table.Th>
                  <Table.Th>Usual spread</Table.Th>
                  <Table.Th>Daily range</Table.Th>
                  <Table.Th>Next 24 h expected</Table.Th>
                  <Table.Th>Findings</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {data.sensors.map((s) => (
                  <Table.Tr key={s.sensor}>
                    <Table.Td>{s.label}</Table.Td>
                    <Table.Td>{s.learning ? `learning (${s.days} of 7 days)` : `${s.days} days`}</Table.Td>
                    <Table.Td>{s.learning ? '' : `± ${reading(s.spread ?? 0, s.unit)} ${unitText(s.unit)}`}</Table.Td>
                    <Table.Td>{s.learning ? '' : `${reading(s.daily_range ?? 0, s.unit)} ${unitText(s.unit)}`}</Table.Td>
                    <Table.Td>{nextDay(s)}</Table.Td>
                    <Table.Td>{s.learning ? '' : s.findings}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        </Card>
      </Section>
    </Stack>
  );
}

type Switched = { output: string; on: number; off: number };

/** The outputs the unit switched during a finding (from its outlet log), as "SndWrn_I7 on 2× off 1×". */
function switched(f: Finding): string {
  const outlets = (f.evidence.outlets ?? []) as Switched[];
  return outlets.map((o) => `${o.output} on ${o.on}× off ${o.off}×`).join(', ');
}

type Noted = { ts: number; kind: string; text: string };

/** Journal entries near a finding, as "water change (Mon, Oct 4, 10:00): 20 %". */
function noted(f: Finding): string {
  const entries = (f.evidence.journal ?? []) as Noted[];
  return entries.map((e) => `${e.kind} (${longClock(e.ts)}): ${e.text}`).join('; ');
}

function nextDay(s: SensorAnalysis): string {
  if (s.learning || !s.forecast || s.forecast.length === 0) return '';
  const expected = s.forecast.map((f) => f[1]);
  return `${reading(Math.min(...expected), s.unit)} – ${reading(Math.max(...expected), s.unit)} ${unitText(s.unit)}`;
}

function WeekChart({ sensor }: { sensor: SensorAnalysis }) {
  const label = (ts: number) => new Date(ts * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  const rows = [
    ...(sensor.recent ?? []).map(([ts, value, expected, low, high]) => ({ time: label(ts ?? 0), reading: value, expected, low, high })),
    ...(sensor.forecast ?? []).map(([ts, expected, low, high]) => ({ time: label(ts), reading: null, expected, low, high })),
  ];
  if (rows.length === 0) return <Text c="dimmed">No data for the chart.</Text>;
  return (
    <SimpleGrid cols={1}>
      <LineChart
        h={320}
        data={rows}
        dataKey="time"
        withDots={false}
        curveType="linear"
        connectNulls={false}
        yAxisProps={{ domain: ['auto', 'auto'] }}
        valueFormatter={(v) => reading(v, sensor.unit)}
        series={[
          { name: 'high', label: 'band high', color: 'gray.6', strokeDasharray: '4 4' },
          { name: 'expected', label: 'expected', color: 'gray.4' },
          { name: 'reading', label: 'reading', color: 'cyan.5' },
          { name: 'low', label: 'band low', color: 'gray.6', strokeDasharray: '4 4' },
        ]}
      />
    </SimpleGrid>
  );
}
