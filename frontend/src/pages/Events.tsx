// Events, newest first: alerts that started and ended, Apex power failures and clock steps, program changes, new
// modules, and the history filled from the unit's own log.
import { Card, Select, Table, Text } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api/client';
import type { RangeName } from '../api/types';
import { Loaded, PageHeader, RangeSelect } from '../components/ui';
import { EVENT_TEXT, longClock } from '../lib/format';

const KINDS = [
  { value: '', label: 'Everything' },
  { value: 'condition', label: 'Alerts' },
  { value: 'apex', label: 'Apex (power, clock, programs)' },
  { value: 'module', label: 'Modules' },
  { value: 'history', label: 'History' },
  { value: 'analysis', label: 'Analysis findings' },
];

export function EventsPage() {
  const [range, setRange] = useState<RangeName>('7d');
  const [kind, setKind] = useState<string>('');
  const query = useQuery({ queryKey: ['events', range, kind], queryFn: () => api.events(range, kind || undefined), refetchInterval: 60_000 });
  return (
    <>
      <PageHeader title="Events">
        <Select data={KINDS} value={kind} onChange={(v) => setKind(v ?? '')} allowDeselect={false} w={240} aria-label="Kind" />
        <RangeSelect value={range} onChange={setRange} ranges={['24h', '7d', '30d', '365d']} />
      </PageHeader>
      <Loaded query={query}>
        {(data) =>
          data.events.length === 0 ? (
            <Text c="dimmed">Nothing in this range.</Text>
          ) : (
            <Card padding={0}>
              <Table.ScrollContainer minWidth={560}>
                <Table>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>Time</Table.Th>
                      <Table.Th>What</Table.Th>
                      <Table.Th>About</Table.Th>
                      <Table.Th>Detail</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {data.events.map((e, n) => (
                      <Table.Tr key={`${e.ts}-${n}`}>
                        <Table.Td className="nowrap">{longClock(e.ts)}</Table.Td>
                        <Table.Td className="nowrap">{EVENT_TEXT[e.kind] ?? e.kind}</Table.Td>
                        <Table.Td ff="monospace">{e.subject}</Table.Td>
                        <Table.Td>{e.detail}</Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </Table.ScrollContainer>
            </Card>
          )
        }
      </Loaded>
    </>
  );
}
