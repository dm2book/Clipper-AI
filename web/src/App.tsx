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
import { ForgotPassword, ResetPassword } from "./pages/ResetPassword";
import { Settings } from "./pages/Settings";
import { Signup } from "./pages/Signup";
import { Sources } from "./pages/Sources";
import { Verify } from "./pages/Verify";

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
  if (!me) {
    return (
      <Routes>
        <Route path="/signup" element={<Signup />} />
        <Route path="/forgot" element={<ForgotPassword />} />
        <Route path="*" element={<Login />} />
      </Routes>
    );
  }

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
        {/*
          `/verify` and `/reset` sit outside the gate on purpose. Both carry a
          token that *is* the credential, and both must work whether or not
          there is a session — a deployment that blocks unverified sign-in
          would otherwise have accounts that can never verify, because the link
          would bounce to a login they are not yet allowed to complete.
        */}
        <Routes>
          <Route path="/verify" element={<Verify />} />
          <Route path="/reset" element={<ResetPassword />} />
          <Route path="/*" element={<Gate />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
