// One sensor over a chosen range: a probe as its mean with the lowest and highest value of each step (so a short
// spike is never averaged away), a float or the leak sensor as its changes.
import { LineChart } from '@mantine/charts';
import { Card, Group, Select, Table, Text } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api/client';
import type { History, RangeName } from '../api/types';
import { Loaded, PageHeader, RangeSelect } from '../components/ui';
import { clock, longClock, reading, unitText } from '../lib/format';

const RANGES: RangeName[] = ['24h', '7d', '30d', '90d', '365d'];

export function ChartsPage() {
  const overview = useQuery({ queryKey: ['overview'], queryFn: api.overview, refetchInterval: 30_000 });
  const [sensor, setSensor] = useState<string | null>('Tmp');
  const [range, setRange] = useState<RangeName>('7d');
  const options = [
    ...(overview.data?.probes ?? []).map((p) => ({ value: p.name, label: p.label })),
    ...(overview.data?.modules ?? [])
      .filter((m) => m.amps !== null && m.kind === 'EB8')
      .map((m) => ({ value: `Amp_${m.address}`, label: `${m.id} current` })),
    ...(overview.data?.switches ?? []).map((s) => ({ value: s.name, label: s.label })),
    ...(overview.data?.external_sensors ?? []).map((s) => ({ value: s.name, label: s.label })),
  ];
  const history = useQuery({
    queryKey: ['history', sensor, range],
    queryFn: () => api.history(sensor ?? 'Tmp', range),
    enabled: sensor !== null,
    refetchInterval: 120_000,
  });
  return (
    <>
      <PageHeader title="Charts" subtitle="Each step shows the mean, and the lowest and highest reading in it.">
        <Select data={options} value={sensor} onChange={setSensor} allowDeselect={false} searchable w={240} aria-label="Sensor" />
        <RangeSelect value={range} onChange={setRange} ranges={RANGES} />
      </PageHeader>
      <Loaded query={history}>{(data) => <Chart data={data} />}</Loaded>
    </>
  );
}

function Chart({ data }: { data: History }) {
  if (data.changes) {
    return (
      <Card padding={0}>
        <Table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Time</Table.Th>
              <Table.Th>{data.label}</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            <Table.Tr>
              <Table.Td c="dimmed">before</Table.Td>
              <Table.Td>{data.state_before ?? 'not known'}</Table.Td>
            </Table.Tr>
            {data.changes.map(([ts, state]) => (
              <Table.Tr key={ts}>
                <Table.Td>{longClock(ts)}</Table.Td>
                <Table.Td>{state}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Card>
    );
  }
  const points = data.points ?? [];
  if (points.length === 0) {
    return <Text c="dimmed">No readings in this range.</Text>;
  }
  // labels by the span the data covers (a week's range may hold one day so far): times, days and times, or days
  const covered = points[points.length - 1][0] - points[0][0];
  const label = (ts: number) =>
    covered <= 36 * 3600
      ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : covered <= 4 * 86400
        ? new Date(ts * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })
        : new Date(ts * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
  const rows = points.map(([ts, mean, low, high]) => ({
    time: label(ts),
    mean,
    low,
    high,
  }));
  const means = points.map((p) => p[1]);
  const unit = unitText(data.unit);
  return (
    <Card padding="md">
      <Group justify="space-between" mb="xs">
        <Text fw={600}>
          {data.label} {unit && <Text span c="dimmed">({unit})</Text>}
        </Text>
        <Text size="sm" c="dimmed">
          {clock(points[0][0])} – {clock(points[points.length - 1][0])} · mean {reading(Math.min(...means), data.unit)} to{' '}
          {reading(Math.max(...means), data.unit)} · step {Math.round((data.step_seconds ?? 600) / 60)} min
        </Text>
      </Group>
      <LineChart
        h={340}
        data={rows}
        dataKey="time"
        withDots={false}
        curveType="linear"
        yAxisProps={{ domain: ['auto', 'auto'] }}
        valueFormatter={(v) => reading(v, data.unit)}
        series={[
          { name: 'high', label: 'highest', color: 'gray.6', strokeDasharray: '4 4' },
          { name: 'mean', label: 'mean', color: 'cyan.5' },
          { name: 'low', label: 'lowest', color: 'gray.6', strokeDasharray: '4 4' },
        ]}
      />
      {data.tests && data.tests.length > 0 && (
        <Table mt="md">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Manual test</Table.Th>
              <Table.Th>Test</Table.Th>
              <Table.Th>Probe then</Table.Th>
              <Table.Th>Probe minus test</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data.tests.map((t) => (
              <Table.Tr key={t.measured_at}>
                <Table.Td>
                  {longClock(t.measured_at)} {t.kit && <Text span c="dimmed" size="sm">({t.kit})</Text>}
                </Table.Td>
                <Table.Td>{reading(t.value, data.unit)}</Table.Td>
                <Table.Td>{t.probe === null ? 'no reading' : reading(t.probe, data.unit)}</Table.Td>
                <Table.Td>{t.difference === null ? '' : `${t.difference >= 0 ? '+' : ''}${reading(t.difference, data.unit)}`}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Card>
  );
}
