import React, { useState, useEffect, useCallback } from 'react';
import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';
import Topbar from './Topbar';
import PanelResizeHandle from './PanelResizeHandle';

const LS_APP_SIDEBAR = 'a7.panel.appSidebarExpanded';

function readSidebarExpandedWidth() {
  try {
    const raw = localStorage.getItem(LS_APP_SIDEBAR);
    if (raw == null) return 240;
    const n = Number(raw);
    return Number.isFinite(n) ? Math.min(360, Math.max(200, n)) : 240;
  } catch {
    return 240;
  }
}

export default function AppShell() {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [sidebarExpandedWidth, setSidebarExpandedWidth] = useState(readSidebarExpandedWidth);

  useEffect(() => {
    try {
      localStorage.setItem(LS_APP_SIDEBAR, String(sidebarExpandedWidth));
    } catch {
      /* ignore */
    }
  }, [sidebarExpandedWidth]);

  const bumpSidebar = useCallback((dx) => {
    setSidebarExpandedWidth((w) => Math.min(360, Math.max(200, w + dx)));
  }, []);

  return (
    <div className="flex h-screen overflow-hidden bg-background min-w-0">
      <Sidebar
        collapsed={sidebarCollapsed}
        onToggle={() => setSidebarCollapsed(!sidebarCollapsed)}
        expandedWidth={sidebarExpandedWidth}
      />
      {!sidebarCollapsed ? (
        <PanelResizeHandle className="self-stretch bg-border/30" onResize={bumpSidebar} />
      ) : null}
      <div className="flex flex-1 flex-col overflow-hidden min-w-0">
        <Topbar sidebarCollapsed={sidebarCollapsed} onToggleSidebar={() => setSidebarCollapsed(!sidebarCollapsed)} />
        <main className="flex-1 overflow-auto min-w-0">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
