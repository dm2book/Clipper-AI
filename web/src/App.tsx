import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./api/auth";
import { Shell } from "./components/Shell";
import { Skeleton } from "./components/ui";
import { Analytics } from "./pages/Analytics";
import { Channels } from "./pages/Channels";
import { Login } from "./pages/Login";
import { Overview } from "./pages/Overview";
import { Published } from "./pages/Published";
import { Queue } from "./pages/Queue";
import { Settings } from "./pages/Settings";
import { Sources } from "./pages/Sources";

function Gate() {
  const { me, loading } = useAuth();

  // The stored token is checked against the API before anything renders. A
  // dashboard that trusts localStorage flashes a full UI and then empties it
  // when the first request 401s.
  if (loading) {
    return (
      <div style={{ padding: 40, maxWidth: 420 }}>
        <Skeleton rows={4} />
      </div>
    );
  }
  if (!me) return <Login />;

  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Overview />} />
        <Route path="channels" element={<Channels />} />
        <Route path="sources" element={<Sources />} />
        <Route path="queue" element={<Queue />} />
        <Route path="published" element={<Published />} />
        <Route path="analytics" element={<Analytics />} />
        <Route path="settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

export function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Gate />
      </AuthProvider>
    </BrowserRouter>
  );
}
