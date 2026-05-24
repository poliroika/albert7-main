import React from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { ThemeProvider } from './context/ThemeContext';
import { WorkspaceProvider } from './context/WorkspaceContext';
import { Toaster } from './components/ui/sonner';
import AppShell from './components/layout/AppShell';
import Landing from './pages/Landing';
import Chat from './pages/Chat';
import MemoryGraph from './pages/MemoryGraph';
import MemoryLab from './pages/MemoryLab';
import Runs from './pages/Runs';
import Logs from './pages/Logs';
import Dashboard from './pages/Dashboard';
import Workspaces from './pages/Workspaces';
import Settings from './pages/Settings';
import MCPRegistry from './pages/MCPRegistry';
import './App.css';

class AppErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  render() {
    const { error } = this.state;
    if (error) {
      return (
        <div className="min-h-screen bg-background text-foreground p-8 max-w-2xl mx-auto">
          <h1 className="text-lg font-semibold mb-3">Ошибка в интерфейсе</h1>
          <p className="text-sm text-muted-foreground mb-4">
            Откройте консоль браузера (F12). Частая причина — ответ HTML вместо JSON с{' '}
            <code className="text-xs bg-secondary px-1 rounded">/api/*</code> (только static без web_bridge).
          </p>
          <pre className="text-xs text-destructive whitespace-pre-wrap font-mono border border-border rounded-md p-3 overflow-auto">
            {String(error?.message || error)}
          </pre>
        </div>
      );
    }
    return this.props.children;
  }
}

function App() {
  return (
    <ThemeProvider>
      <AppErrorBoundary>
        <BrowserRouter>
          <WorkspaceProvider>
            <Routes>
              <Route path="/" element={<Landing />} />
              <Route element={<AppShell />}>
                <Route path="/chat" element={<Chat />} />
                <Route path="/chat/:threadId" element={<Chat />} />
                <Route path="/memory" element={<MemoryGraph />} />
                <Route path="/memory-lab" element={<MemoryLab />} />
                <Route path="/runs" element={<Runs />} />
                <Route path="/runs/:runId" element={<Runs />} />
                <Route path="/logs" element={<Logs />} />
                <Route path="/dashboard" element={<Dashboard />} />
                <Route path="/workspaces" element={<Workspaces />} />
                <Route path="/mcp" element={<MCPRegistry />} />
                <Route path="/settings" element={<Settings />} />
              </Route>
            </Routes>
            <Toaster position="bottom-right" richColors />
          </WorkspaceProvider>
        </BrowserRouter>
      </AppErrorBoundary>
    </ThemeProvider>
  );
}

export default App;
