import { ActionIcon, Anchor, AppShell, Burger, Group, NavLink, ScrollArea, Text, Tooltip, useMantineColorScheme } from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import {
  IconBrain,
  IconCalendarEvent,
  IconChartLine,
  IconCode,
  IconDroplet,
  IconFish,
  IconFlask,
  IconFlame,
  IconMoon,
  IconNotebook,
  IconReport,
  IconSun,
} from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { Link, useLocation } from 'react-router';
import { api } from '../api/client';
import type { Me } from '../api/types';
import { PollBadge } from '../components/ui';

const NAV = [
  { to: '/', label: 'Now', icon: IconDroplet },
  { to: '/charts', label: 'Charts', icon: IconChartLine },
  { to: '/tests', label: 'Water tests', icon: IconFlask },
  { to: '/journal', label: 'Journal', icon: IconNotebook },
  { to: '/reactor', label: 'Reactor', icon: IconFlame },
  { to: '/analysis', label: 'Analysis', icon: IconBrain },
  { to: '/reports', label: 'Reports', icon: IconReport },
  { to: '/programs', label: 'Programs', icon: IconCode },
  { to: '/events', label: 'Events', icon: IconCalendarEvent },
];

export function AppLayout({ me, children }: { me: Me; children: ReactNode }) {
  const [opened, { toggle, close }] = useDisclosure();
  const location = useLocation();
  const { colorScheme, toggleColorScheme } = useMantineColorScheme();
  // the same query the Now page uses, so the header and the page never disagree
  const overview = useQuery({ queryKey: ['overview'], queryFn: api.overview, refetchInterval: 30_000 });
  const alerts = overview.data?.alerts.length ?? 0;

  return (
    <AppShell header={{ height: 56 }} navbar={{ width: 200, breakpoint: 'sm', collapsed: { mobile: !opened } }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between" wrap="nowrap">
          <Group gap="sm" wrap="nowrap">
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" aria-label="Toggle navigation" />
            <IconFish size={24} color="var(--mantine-color-cyan-5)" />
            <Text fw={700} visibleFrom="xs">
              Aquarium
            </Text>
            {overview.data && <PollBadge overview={overview.data} />}
          </Group>
          <Group gap="xs" wrap="nowrap">
            <Text size="sm" c="dimmed" visibleFrom="sm">
              {me.name}
            </Text>
            <Tooltip label={colorScheme === 'dark' ? 'Light theme' : 'Dark theme'}>
              <ActionIcon variant="subtle" color="gray" onClick={toggleColorScheme} aria-label="Toggle colour scheme">
                {colorScheme === 'dark' ? <IconSun size={18} /> : <IconMoon size={18} />}
              </ActionIcon>
            </Tooltip>
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar p="xs">
        <AppShell.Section grow component={ScrollArea}>
          {NAV.map((item) => {
            const active = item.to === '/' ? location.pathname === '/' : location.pathname.startsWith(item.to);
            return (
              <NavLink
                key={item.to}
                component={Link}
                to={item.to}
                label={item.label}
                active={active}
                leftSection={<item.icon size={18} stroke={1.6} />}
                rightSection={
                  item.to === '/' && alerts > 0 ? (
                    <Text size="xs" c="red" fw={700}>
                      {alerts}
                    </Text>
                  ) : null
                }
                onClick={close}
              />
            );
          })}
        </AppShell.Section>
        {me.home && (
          <AppShell.Section>
            <Anchor href={me.home.href} size="xs" c="dimmed" px="sm" py={6} display="block">
              {me.home.label}
            </Anchor>
          </AppShell.Section>
        )}
      </AppShell.Navbar>

      <AppShell.Main>{children}</AppShell.Main>
    </AppShell>
  );
}
