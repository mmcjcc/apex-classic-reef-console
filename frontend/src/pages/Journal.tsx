// The tank's journal: water changes, dosing, probe calibrations, maintenance, equipment and livestock changes. The
// analysis shows the entries near each finding, so a step on the day of a water change explains itself.
import { Anchor, Button, Card, Group, Select, SimpleGrid, Stack, Table, Text, TextInput } from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api/client';
import type { JournalOut, Me } from '../api/types';
import { Loaded, PageHeader, Section } from '../components/ui';
import { localInput, longClock } from '../lib/format';

export function JournalPage({ me }: { me: Me }) {
  const query = useQuery({ queryKey: ['journal'], queryFn: () => api.journal(), refetchInterval: 300_000 });
  return (
    <>
      <PageHeader title="Journal" subtitle="What was done to the tank: the analysis shows the entries near each finding." />
      <Loaded query={query}>{(data) => <Journal data={data} me={me} />}</Loaded>
    </>
  );
}

function Journal({ data, me }: { data: JournalOut; me: Me }) {
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteJournal(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['journal'] }),
    onError: (error: Error) => notifications.show({ color: 'red', title: 'Not deleted', message: error.message }),
  });
  return (
    <Stack gap="lg">
      {me.can_write && <EntryForm kinds={data.kinds} />}
      <Section title="Entries">
        {data.entries.length === 0 ? (
          <Text c="dimmed">Nothing written yet.</Text>
        ) : (
          <Card padding={0}>
            <Table.ScrollContainer minWidth={560}>
              <Table>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>When</Table.Th>
                    <Table.Th>What</Table.Th>
                    <Table.Th>Entry</Table.Th>
                    <Table.Th>By</Table.Th>
                    {me.can_write && <Table.Th />}
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {data.entries.map((e) => (
                    <Table.Tr key={e.id}>
                      <Table.Td className="nowrap">{longClock(e.ts)}</Table.Td>
                      <Table.Td className="nowrap">{e.kind}</Table.Td>
                      <Table.Td>{e.text}</Table.Td>
                      <Table.Td c="dimmed">
                        {e.entered_by} ({e.source})
                      </Table.Td>
                      {me.can_write && (
                        <Table.Td>
                          <Anchor
                            component="button"
                            size="sm"
                            onClick={() => {
                              if (window.confirm('Delete this entry? It stays in the database, marked deleted.')) remove.mutate(e.id);
                            }}
                          >
                            delete
                          </Anchor>
                        </Table.Td>
                      )}
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          </Card>
        )}
      </Section>
    </Stack>
  );
}

function EntryForm({ kinds }: { kinds: string[] }) {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<string>(kinds[0] ?? 'water change');
  const [time, setTime] = useState(localInput(new Date()));
  const [text, setText] = useState('');
  const [saved, setSaved] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => api.addJournal({ ts: new Date(time).getTime() / 1000, kind, text }),
    onSuccess: (entry) => {
      setSaved(`Written: ${entry.kind}, ${longClock(entry.ts)}.`);
      setText('');
      void queryClient.invalidateQueries({ queryKey: ['journal'] });
    },
    onError: (error: Error) => notifications.show({ color: 'red', title: 'Not written', message: error.message }),
  });
  return (
    <Card padding="md">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (text.trim()) save.mutate();
        }}
      >
        <Text fw={600} mb="xs">
          Write an entry
        </Text>
        <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }} spacing="sm">
          <Select label="What" data={kinds} value={kind} onChange={(v) => setKind(v ?? kind)} allowDeselect={false} />
          <TextInput label="When" type="datetime-local" value={time} onChange={(e) => setTime(e.currentTarget.value)} required />
          <TextInput
            label="Entry"
            placeholder="e.g. 20 % water change, new salt batch"
            value={text}
            onChange={(e) => setText(e.currentTarget.value)}
            maxLength={1000}
            required
            style={{ gridColumn: 'span 2' }}
          />
        </SimpleGrid>
        <Group justify="space-between" mt="sm">
          <Text c="teal" size="sm">
            {saved}
          </Text>
          <Button type="submit" loading={save.isPending}>
            Write
          </Button>
        </Group>
      </form>
    </Card>
  );
}
