import React, { useState } from 'react';
import { useWorkspace } from '../context/WorkspaceContext';
import { deleteWorkspace } from '../lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/card';
import { Button } from '../components/ui/button';
import { Input } from '../components/ui/input';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogClose
} from '../components/ui/dialog';
import { Plus, Layers, Trash2, Pencil, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { motion } from 'framer-motion';

export default function Workspaces() {
  const { workspaces, activeWorkspace, switchWorkspace, createNewWorkspace, fetchWorkspaces } = useWorkspace();
  const [newName, setNewName] = useState('');
  const [creating, setCreating] = useState(false);

  const handleCreate = async () => {
    if (!newName.trim()) return;
    setCreating(true);
    try {
      await createNewWorkspace(newName.trim());
      setNewName('');
      toast.success('Workspace created');
    } catch (err) {
      toast.error('Failed to create workspace');
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (wsId) => {
    const ok = window.confirm(
      `Удалить workspace ${wsId}? Будут стёрты исходники, .memory, runs и .umbrella артефакты.`
    );
    if (!ok) return;
    try {
      const result = await deleteWorkspace(wsId);
      fetchWorkspaces();
      const removedCount = result?.report?.removed_count ?? 0;
      const errors = result?.report?.errors ?? [];
      if (errors.length) {
        toast.warning(
          `Workspace удалён частично: ${removedCount} артефактов, ошибки: ${errors.slice(0, 2).join('; ')}`
        );
      } else {
        toast.success(`Workspace удалён, очищено ${removedCount} артефактов`);
      }
    } catch (err) {
      const status = err?.response?.status;
      const detail = err?.response?.data?.reason || err?.message || 'Неизвестная ошибка';
      if (status === 409) {
        toast.error(`Нельзя удалить: ${detail}`);
      } else {
        toast.error(`Не удалось удалить workspace: ${detail}`);
      }
    }
  };

  return (
    <div className="px-4 lg:px-8 py-6">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-xl font-display font-semibold tracking-tight">Workspaces</h2>
          <p className="text-sm text-muted-foreground">{workspaces.length} workspaces</p>
        </div>
        <Dialog>
          <DialogTrigger asChild>
            <Button size="sm" className="gap-2 h-8">
              <Plus className="h-3.5 w-3.5" /> New Workspace
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle className="font-display">Create Workspace</DialogTitle>
            </DialogHeader>
            <div className="space-y-4 pt-2">
              <Input
                placeholder="Workspace name"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
              />
              <div className="flex justify-end gap-2">
                <DialogClose asChild>
                  <Button variant="outline" size="sm">Cancel</Button>
                </DialogClose>
                <Button size="sm" onClick={handleCreate} disabled={creating || !newName.trim()}>
                  {creating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : 'Create'}
                </Button>
              </div>
            </div>
          </DialogContent>
        </Dialog>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {workspaces.map((ws, i) => (
          <motion.div
            key={ws.id}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.22, delay: i * 0.04 }}
          >
            <Card
              className={`bg-card/60 border-border/70 cursor-pointer hover:bg-card/80 transition-colors duration-150 ${
                activeWorkspace?.id === ws.id ? 'ring-1 ring-ring' : ''
              }`}
              onClick={() => switchWorkspace(ws)}
            >
              <CardContent className="p-4">
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-3">
                    <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-secondary/60">
                      <Layers className="h-5 w-5 text-muted-foreground" />
                    </div>
                    <div>
                      <h3 className="text-sm font-semibold">{ws.name}</h3>
                      <p className="text-xs text-muted-foreground">{ws.description || 'No description'}</p>
                    </div>
                  </div>
                  <div className="flex gap-1">
                    <Button
                      variant="ghost" size="icon" className="h-7 w-7"
                      onClick={(e) => { e.stopPropagation(); handleDelete(ws.id); }}
                    >
                      <Trash2 className="h-3 w-3 text-muted-foreground" />
                    </Button>
                  </div>
                </div>
                {activeWorkspace?.id === ws.id && (
                  <div className="mt-3">
                    <span className="text-[10px] text-cyan-400 font-medium">Active</span>
                  </div>
                )}
              </CardContent>
            </Card>
          </motion.div>
        ))}

        {workspaces.length === 0 && (
          <div className="col-span-full flex flex-col items-center justify-center h-40">
            <Layers className="h-8 w-8 text-muted-foreground mb-3" />
            <p className="text-sm text-muted-foreground">No workspaces yet. Create one to get started.</p>
          </div>
        )}
      </div>
    </div>
  );
}
