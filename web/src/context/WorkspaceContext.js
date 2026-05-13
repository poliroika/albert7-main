import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
import { listWorkspaces, createWorkspace, getSettings } from '../lib/api';

const WorkspaceContext = createContext();
const defaultWorkspaceId = 'world_prediction';

export function WorkspaceProvider({ children }) {
  const [workspaces, setWorkspaces] = useState([]);
  const [activeWorkspace, setActiveWorkspace] = useState(null);
  const [loading, setLoading] = useState(true);
  /** Resolved LLM id from server `.env` (see `get_settings` / `resolve_default_ouroboros_model`). */
  const [serverLlmModel, setServerLlmModel] = useState(null);

  const fetchWorkspaces = useCallback(async () => {
    try {
      const data = await listWorkspaces();
      const list = Array.isArray(data) ? data : [];
      setWorkspaces(list);
      setActiveWorkspace(prev => {
        if (!list.length) {
          return prev;
        }
        const savedId = localStorage.getItem('umbrella-active-workspace');
        const found = list.find(w => w.id === savedId);
        if (prev) {
          const still = list.find(w => w.id === prev.id);
          return still || prev;
        }
        const defaultWorkspace = list.find(w => w.id === defaultWorkspaceId);
        return found || defaultWorkspace || list[0];
      });
    } catch (err) {
      console.error('Failed to fetch workspaces:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchWorkspaces();
  }, [fetchWorkspaces]);

  useEffect(() => {
    if (!activeWorkspace?.id) {
      setServerLlmModel(null);
      return;
    }
    setServerLlmModel(null);
    let cancelled = false;
    getSettings(activeWorkspace.id)
      .then((s) => {
        if (!cancelled && s?.default_model) {
          setServerLlmModel(String(s.default_model));
        }
      })
      .catch(() => {
        if (!cancelled) setServerLlmModel(null);
      });
    return () => { cancelled = true; };
  }, [activeWorkspace?.id]);

  const switchWorkspace = (ws) => {
    setActiveWorkspace(ws);
    localStorage.setItem('umbrella-active-workspace', ws.id);
  };

  const createNewWorkspace = async (name, description = '') => {
    const ws = await createWorkspace({ name, description });
    setWorkspaces(prev => [ws, ...prev]);
    switchWorkspace(ws);
    return ws;
  };

  return (
    <WorkspaceContext.Provider value={{
      workspaces, activeWorkspace, loading, serverLlmModel,
      switchWorkspace, createNewWorkspace, fetchWorkspaces,
    }}>
      {children}
    </WorkspaceContext.Provider>
  );
}

export const useWorkspace = () => useContext(WorkspaceContext);
