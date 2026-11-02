import { Alert, Button, Center, Loader, Text } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { Navigate, Route, Routes } from 'react-router';
import { ApiError, api } from './api/client';
import { AppLayout } from './layout/AppLayout';
import { AnalysisPage } from './pages/Analysis';
import { ChartsPage } from './pages/Charts';
import { EventsPage } from './pages/Events';
import { JournalPage } from './pages/Journal';
import { NowPage } from './pages/Now';
import { ProgramsPage } from './pages/Programs';
import { ReactorPage } from './pages/Reactor';
import { ReportsPage } from './pages/Reports';
import { TestsPage } from './pages/Tests';

export function App() {
  const me = useQuery({ queryKey: ['me'], queryFn: api.me, retry: false, staleTime: 60_000 });

  if (me.isPending) {
    return (
      <Center h="100vh">
        <Loader />
      </Center>
    );
  }
  if (me.isError) {
    if (me.error instanceof ApiError && me.error.status === 401) {
      if (me.error.signIn) {
        window.location.assign(me.error.signIn); // another service holds the sign-in (forward mode)
        return null;
      }
      return (
        <Center h="100vh" p="md">
          <Alert color="blue" title="Sign-in needed">
            <Text size="sm" mb="sm">
              The aquarium console needs a sign-in.
            </Text>
            <Button size="xs" onClick={() => window.location.reload()}>
              Sign in
            </Button>
          </Alert>
        </Center>
      );
    }
    return (
      <Center h="100vh" p="md">
        <Alert color="red" title="The aquarium console could not start">
          {me.error.message}
        </Alert>
      </Center>
    );
  }
  return (
    <AppLayout me={me.data}>
      <Routes>
        <Route path="/" element={<NowPage />} />
        <Route path="/charts" element={<ChartsPage />} />
        <Route path="/tests" element={<TestsPage me={me.data} />} />
        <Route path="/analysis" element={<AnalysisPage />} />
        <Route path="/journal" element={<JournalPage me={me.data} />} />
        <Route path="/reactor" element={<ReactorPage />} />
        <Route path="/reports" element={<ReportsPage />} />
        <Route path="/programs" element={<ProgramsPage />} />
        <Route path="/events" element={<EventsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AppLayout>
  );
}
