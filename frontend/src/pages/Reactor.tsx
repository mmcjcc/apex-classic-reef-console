// The calcium reactor: how long the CO2 ran each day (in its night window and outside it), the effluent pH by day and by
// night, the alkalinity, calcium and magnesium tests with their change per day, and whether calcium and alkalinity moved
// together (about 7.15 ppm of calcium per dKH, the ratio the reactor dissolves them in).
import { BarChart, LineChart } from '@mantine/charts';
import { Alert, Badge, Card, Group, SimpleGrid, Stack, Table, Text } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api/client';
import type { RangeName, ReactorOut, ReactorTest } from '../api/types';
import { Loaded, PageHeader, RangeSelect, Section } from '../components/ui';
import { clock, longClock } from '../lib/format';

const DAYS: Record<string, number> = { '7d': 7, '30d': 30, '90d': 90 };

export function ReactorPage() {
  const [range, setRange] = useState<RangeName>('30d');
  const query = useQuery({ queryKey: ['reactor', range], queryFn: () => api.reactor(DAYS[range] ?? 30), refetchInterval: 300_000 });
  return (
    <>
      <PageHeader title="Calcium reactor" subtitle="CO2 time and effluent pH per day, against your alkalinity and calcium tests.">
        <RangeSelect value={range} onChange={setRange} ranges={['7d', '30d', '90d']} />
      </PageHeader>
      <Loaded query={query}>{(data) => <Reactor data={data} />}</Loaded>
    </>
  );
}

function short(day: string): string {
  return new Date(`${day}T12:00:00`).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function Reactor({ data }: { data: ReactorOut }) {
  if (!data.configured) return <Alert title="No reactor configured">Add a reactor section to config/site.yaml.</Alert>;
  const known = data.days.filter((d) => d.co2_minutes !== null);
  const co2 = known.map((d) => ({
    day: short(d.day),
    night: d.co2_night_minutes ?? 0,
    day_time: (d.co2_minutes ?? 0) - (d.co2_night_minutes ?? 0),
  }));
  const effluent = data.days.map((d) => ({ day: short(d.day), day_ph: d.effluent_day, night_ph: d.effluent_night }));
  return (
    <Stack gap="lg">
      {data.drip && (
        <Alert color="orange" title="Check the effluent drip">
          {data.drip.summary}
        </Alert>
      )}
      {data.note && (
        <Alert color="blue" variant="light">
          {data.note}
        </Alert>
      )}
      <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
        <Card padding="md">
          <Text fw={600} mb="xs">
            CO2 on, minutes a day ({data.co2})
          </Text>
          {co2.length === 0 ? (
            <Text c="dimmed">No switching history yet (it starts with the minute polls).</Text>
          ) : (
            <BarChart
              h={260}
              data={co2}
              dataKey="day"
              type="stacked"
              series={[
                { name: 'day_time', label: 'outside the night window', color: 'cyan.6' },
                { name: 'night', label: 'night window', color: 'indigo.5' },
              ]}
            />
          )}
          {data.night_apex && (
            <Text size="xs" c="dimmed" mt={4}>
              Night window {data.night_apex[0]}–{data.night_apex[1]} on the Apex's clock.
            </Text>
          )}
        </Card>
        <Card padding="md">
          <Text fw={600} mb="xs">
            Effluent pH ({data.effluent})
          </Text>
          <LineChart
            h={260}
            data={effluent}
            dataKey="day"
            withDots={false}
            connectNulls
            yAxisProps={{ domain: ['auto', 'auto'] }}
            valueFormatter={(v) => v.toFixed(2)}
            series={[
              { name: 'day_ph', label: 'by day', color: 'cyan.5' },
              { name: 'night_ph', label: 'night window', color: 'indigo.4' },
            ]}
          />
          <Text size="xs" c="dimmed" mt={4}>
            Lower effluent pH dissolves more media: more alkalinity and calcium per litre through the reactor.
          </Text>
        </Card>
      </SimpleGrid>
      <Section title="Do calcium and alkalinity move together?" hint={`Expected: about ${data.ca_per_dkh} ppm of calcium per dKH of alkalinity.`}>
        {data.balance.length === 0 ? (
          <Text c="dimmed">Not enough paired tests yet.</Text>
        ) : (
          <Stack gap="xs">
            {data.balance.map((b) => (
              <Card key={b.from} padding="sm">
                <Group gap="xs" wrap="wrap">
                  <Badge color={b.verdict === 'in step' ? 'teal' : 'yellow'} variant="light">
                    {b.verdict}
                  </Badge>
                  <Text size="sm">
                    {clock(b.from)} – {clock(b.to)}: alkalinity {b.alk_change >= 0 ? '+' : ''}
                    {b.alk_change.toFixed(2)} dKH, calcium {b.ca_change >= 0 ? '+' : ''}
                    {b.ca_change.toFixed(0)} ppm (expected {b.ca_expected >= 0 ? '+' : ''}
                    {b.ca_expected.toFixed(0)} ± {b.margin.toFixed(0)})
                  </Text>
                </Group>
              </Card>
            ))}
          </Stack>
        )}
        <Text size="xs" c="dimmed" mt="xs">
          Corals and precipitation use both in this ratio. A gap means something adds one without the other (media with
          magnesium carbonate in it, then magnesium rises; a buffer or calcium supplement; the salt mix) or a test is off.
        </Text>
      </Section>
      <Section title="Tests">
        <SimpleGrid cols={{ base: 1, md: 3 }} spacing="md">
          {(['alkalinity', 'calcium', 'magnesium'] as const).map((p) => (
            <TestList key={p} title={p[0].toUpperCase() + p.slice(1)} tests={data.tests[p] ?? []} />
          ))}
        </SimpleGrid>
      </Section>
      <Section title="Per day">
        <Card padding={0}>
          <Table.ScrollContainer minWidth={520}>
            <Table>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Day</Table.Th>
                  <Table.Th>CO2 on</Table.Th>
                  <Table.Th>of it at night</Table.Th>
                  <Table.Th>Switchings</Table.Th>
                  <Table.Th>Effluent by day / night</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {[...data.days].reverse().map((d) => (
                  <Table.Tr key={d.day}>
                    <Table.Td>{short(d.day)}</Table.Td>
                    <Table.Td>{d.co2_minutes === null ? '—' : `${d.co2_minutes} min`}</Table.Td>
                    <Table.Td>{d.co2_night_minutes === null ? '—' : `${d.co2_night_minutes} min`}</Table.Td>
                    <Table.Td>{d.switchings ?? '—'}</Table.Td>
                    <Table.Td>
                      {d.effluent_day?.toFixed(2) ?? '—'} / {d.effluent_night?.toFixed(2) ?? '—'}
                    </Table.Td>
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

function TestList({ title, tests }: { title: string; tests: ReactorTest[] }) {
  return (
    <Card padding="sm">
      <Text fw={600} mb={4}>
        {title}
      </Text>
      {tests.length === 0 ? (
        <Text size="sm" c="dimmed">
          No test in this range.
        </Text>
      ) : (
        [...tests].reverse().map((t) => (
          <Group key={t.ts} justify="space-between" wrap="nowrap">
            <Text size="sm">{longClock(t.ts)}</Text>
            <Text size="sm" fw={500}>
              {t.value.toFixed(t.unit === 'dKH' ? 2 : 0)} {t.unit}
              {t.per_day !== null && (
                <Text span size="xs" c="dimmed">
                  {' '}
                  ({t.per_day >= 0 ? '+' : ''}
                  {t.per_day.toFixed(t.unit === 'dKH' ? 2 : 1)}/day)
                </Text>
              )}
            </Text>
          </Group>
        ))
      )}
    </Card>
  );
}
