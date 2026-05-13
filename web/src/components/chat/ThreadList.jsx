import React from 'react';
import { ScrollArea } from '../ui/scroll-area';
import { Button } from '../ui/button';
import { Plus, MessageSquare, Trash2 } from 'lucide-react';
import { cn } from '@/lib/utils';

export default function ThreadList({ threads, activeThread, onSelect, onNew, onDelete, className }) {
  return (
    <div className={cn('flex h-full w-full min-w-0 flex-col', className)}>
      <div className="p-3 border-b border-border/70">
        <Button
          variant="outline"
          size="sm"
          className="w-full justify-start gap-2 h-8 text-xs"
          onClick={onNew}
          data-testid="new-thread-button"
        >
          <Plus className="h-3.5 w-3.5" /> New Chat
        </Button>
      </div>
      <ScrollArea className="flex-1">
        <div className="p-2 space-y-0.5">
          {threads.map(thread => (
            <div
              key={thread.id}
              className={`group flex items-start gap-1 rounded-md px-2.5 py-2 text-sm transition-colors duration-150 ${
                activeThread?.id === thread.id
                  ? 'bg-accent text-foreground shadow-[inset_0_0_0_1px_hsl(var(--border))]'
                  : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
              }`}
            >
              <button
                type="button"
                onClick={() => onSelect(thread)}
                className="min-w-0 flex-1 text-left"
                data-testid="thread-list-item"
              >
                <div className="flex items-center gap-2">
                  <MessageSquare className="h-3.5 w-3.5 shrink-0" />
                  <span className="truncate text-xs">{thread.title || 'Untitled'}</span>
                </div>
                {thread.message_count > 0 && (
                  <span className="ml-5 text-[10px] text-muted-foreground/70">{thread.message_count} messages</span>
                )}
              </button>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-6 w-6 shrink-0 text-muted-foreground opacity-100 hover:text-destructive md:opacity-0 md:group-hover:opacity-100"
                onClick={(event) => {
                  event.stopPropagation();
                  onDelete?.(thread);
                }}
                data-testid="delete-thread-button"
                title="Delete chat"
              >
                <Trash2 className="h-3 w-3" />
              </Button>
            </div>
          ))}
          {threads.length === 0 && (
            <p className="text-xs text-muted-foreground text-center py-4">No conversations yet</p>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
