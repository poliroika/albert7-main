import React from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useWorkspace } from '../../context/WorkspaceContext';
import { ScrollArea } from '../ui/scroll-area';
import { Button } from '../ui/button';
import { Separator } from '../ui/separator';
import {
  MessageSquare, GitBranch, Play, FileText, BarChart3,
  Layers, Settings as SettingsIcon, PanelLeftClose, PanelLeft,
  Plus, ChevronDown, Zap, Plug
} from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '../ui/dropdown-menu';

const navItems = [
  { path: '/chat', label: 'Chat', icon: MessageSquare },
  { path: '/memory', label: 'Memory Graph', icon: GitBranch },
  { path: '/memory-lab', label: 'Memory Lab', icon: Zap },
  { path: '/runs', label: 'Runs', icon: Play },
  { path: '/logs', label: 'Logs', icon: FileText },
  { path: '/dashboard', label: 'Dashboard', icon: BarChart3 },
  { path: '/workspaces', label: 'Workspaces', icon: Layers },
  { path: '/mcp', label: 'MCP Registry', icon: Plug },
  { path: '/settings', label: 'Settings', icon: SettingsIcon },
];

export default function Sidebar({ collapsed, onToggle, expandedWidth = 240 }) {
  const location = useLocation();
  const { workspaces, activeWorkspace, switchWorkspace, createNewWorkspace } = useWorkspace();

  const isActive = (path) => {
    if (path === '/chat') return location.pathname.startsWith('/chat');
    if (path === '/runs') return location.pathname.startsWith('/runs');
    return location.pathname === path;
  };

  return (
    <aside
      data-testid="app-sidebar"
      style={{ width: collapsed ? 64 : expandedWidth }}
      className="flex flex-col border-r border-border bg-card/40 shrink-0 min-h-0"
    >
      {/* Logo */}
      <div className="flex h-14 items-center gap-2.5 border-b border-border px-4">
        <div className="flex h-7 w-7 items-center justify-center rounded-md bg-blue-500/10 border border-blue-500/20">
          <Zap className="h-3.5 w-3.5 text-blue-400" />
        </div>
        {!collapsed && (
          <span className="text-sm font-semibold tracking-tight">Umbrella</span>
        )}
      </div>

      {/* Workspace Switcher */}
      {!collapsed && (
        <div className="px-3 py-3">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                className="w-full justify-between text-xs font-normal h-8 bg-secondary/40 hover:bg-secondary/60 border border-border/50"
                data-testid="workspace-switcher"
              >
                <span className="truncate text-muted-foreground">{activeWorkspace?.name || 'Select Workspace'}</span>
                <ChevronDown className="h-3 w-3 opacity-40" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent className="w-[210px]" align="start">
              {workspaces.map(ws => (
                <DropdownMenuItem key={ws.id} onClick={() => switchWorkspace(ws)} className="text-xs">
                  {ws.name}
                </DropdownMenuItem>
              ))}
              <Separator className="my-1" />
              <DropdownMenuItem onClick={() => createNewWorkspace('New Workspace')} className="text-xs">
                <Plus className="mr-2 h-3 w-3" /> New Workspace
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      )}

      {!collapsed && <Separator />}

      {/* Navigation */}
      <ScrollArea className="flex-1 px-2 py-2">
        <nav className="flex flex-col gap-0.5">
          {navItems.map(item => {
            const active = isActive(item.path);
            return (
              <Link
                key={item.path}
                to={item.path}
                data-testid="sidebar-nav-item"
                className={`relative flex items-center gap-2.5 rounded-md px-3 py-2 text-[13px] transition-all duration-160 ${
                  active
                    ? 'bg-accent text-foreground font-medium'
                    : 'text-muted-foreground hover:text-foreground hover:bg-accent/50'
                } ${collapsed ? 'justify-center px-0' : ''}`}
                title={collapsed ? item.label : undefined}
              >
                {/* Active indicator bar */}
                {active && (
                  <div className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-4 rounded-r-full bg-blue-500" />
                )}
                <item.icon className={`h-4 w-4 shrink-0 ${active ? 'text-blue-400' : ''}`} />
                {!collapsed && <span>{item.label}</span>}
              </Link>
            );
          })}
        </nav>
      </ScrollArea>

      {/* Collapse toggle */}
      <div className="border-t border-border p-2">
        <Button
          variant="ghost"
          size="sm"
          onClick={onToggle}
          className={`w-full h-8 ${collapsed ? 'justify-center px-0' : 'justify-start'} text-muted-foreground hover:text-foreground`}
        >
          {collapsed ? <PanelLeft className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
          {!collapsed && <span className="ml-2 text-[11px]">Collapse</span>}
        </Button>
      </div>
    </aside>
  );
}
