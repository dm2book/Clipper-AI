import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../api/auth";
import { useResource } from "../api/hooks";
import type { PageUploadOut } from "../api/types";

const PAGES = [
  { to: "/", label: "Overview", icon: "◎", end: true },
  { to: "/channels", label: "Channels", icon: "▦" },
  { to: "/sources", label: "Sources", icon: "▤" },
  { to: "/queue", label: "Upload Queue", icon: "◷", badge: true },
  { to: "/published", label: "Published", icon: "▶" },
  { to: "/analytics", label: "Analytics", icon: "◔" },
  { to: "/settings", label: "Settings", icon: "⚙" },
];

export function Shell() {
  const { me, signOut } = useAuth();
  // Only for the queue badge. A failure here must not take the shell with it,
  // so it is read defensively and simply omitted when absent.
  const queue = useResource<PageUploadOut>("/uploads?limit=1");

  return (
    <div className="shell">
      <nav className="sidebar">
        <div className="brand">
          <span className="brand-mark">C</span>
          ClipForge AI
        </div>

        {PAGES.map((page) => (
          <NavLink
            key={page.to}
            to={page.to}
            end={page.end}
            className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}
          >
            <span className="nav-icon" aria-hidden="true">{page.icon}</span>
            {page.label}
            {page.badge && queue.data && queue.data.total > 0 && (
              <span className="nav-count">{queue.data.total}</span>
            )}
          </NavLink>
        ))}

        <div className="sidebar-foot">
          <div className="who">
            <strong>{me?.email ?? "—"}</strong>
            {me?.role} · {me?.memberships.find((m) => m.tenant_id === me.tenant_id)
              ?.tenant_name || me?.tenant_id}
          </div>
          <button className="btn btn-sm" style={{ width: "100%", marginTop: 8 }}
                  onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </nav>

      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}

export function PageHead({
  title,
  sub,
  action,
}: {
  title: string;
  sub?: string;
  action?: React.ReactNode;
}) {
  return (
    <header className="page-head">
      <div>
        <h1 className="page-title">{title}</h1>
        {sub && <p className="page-sub">{sub}</p>}
      </div>
      {action}
    </header>
  );
}
