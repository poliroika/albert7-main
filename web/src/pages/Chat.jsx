import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useWorkspace } from '../context/WorkspaceContext';
import { ScrollArea } from '../components/ui/scroll-area';
import PanelResizeHandle from '../components/layout/PanelResizeHandle';
import MessageCard from '../components/chat/MessageCard';
import Composer from '../components/chat/Composer';
import TimelinePanel from '../components/chat/TimelinePanel';
import ThreadList from '../components/chat/ThreadList';
import { UserInputRequestCard, PermissionRequestCard } from '../components/chat/AgentRequestCard';
import {
  listThreads, createThread, listMessages, sendMessage,
  getRun, getRunSteps, listModels, listTools, startRun, cancelRun,
  listUserInputRequests, listPermissionRequests, getSettings, deleteThread,
} from '../lib/api';
import { Loader2, Trash2 } from 'lucide-react';
import { Button } from '../components/ui/button';
import { toast } from 'sonner';

const LS_THREAD = 'a7.panel.chatThreadList';
const LS_TIMELINE = 'a7.panel.chatTimeline';

function readPanelWidth(key, fallback, min, max) {
  try {
    const raw = localStorage.getItem(key);
    if (raw == null) return fallback;
    const n = Number(raw);
    if (!Number.isFinite(n)) return fallback;
    return Math.min(max, Math.max(min, n));
  } catch {
    return fallback;
  }
}

export default function Chat() {
  const { threadId } = useParams();
  const navigate = useNavigate();
  const { activeWorkspace } = useWorkspace();

  const [threads, setThreads] = useState([]);
  const [activeThread, setActiveThread] = useState(null);
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [currentRun, setCurrentRun] = useState(null);
  const [runSteps, setRunSteps] = useState([]);
  const [models, setModels] = useState([]);
  const [tools, setTools] = useState([]);
  const [selectedModel, setSelectedModel] = useState('');
  const [maxRounds, setMaxRounds] = useState('120');
  const [maxVerifyRetries, setMaxVerifyRetries] = useState('20');
  const [harnessMode, setHarnessMode] = useState(false);
  const [harnessCandidates, setHarnessCandidates] = useState('3');

  // Agent communication state
  const [pendingUserRequests, setPendingUserRequests] = useState([]);
  const [pendingPermissions, setPendingPermissions] = useState([]);

  const messagesEndRef = useRef(null);
  const pollRef = useRef(null);
  const requestPollRef = useRef(null);

  const [threadListWidth, setThreadListWidth] = useState(() =>
    readPanelWidth(LS_THREAD, 240, 180, 420),
  );
  const [timelineWidth, setTimelineWidth] = useState(() =>
    readPanelWidth(LS_TIMELINE, 380, 260, 640),
  );

  useEffect(() => {
    try {
      localStorage.setItem(LS_THREAD, String(threadListWidth));
    } catch {
      /* ignore */
    }
  }, [threadListWidth]);

  useEffect(() => {
    try {
      localStorage.setItem(LS_TIMELINE, String(timelineWidth));
    } catch {
      /* ignore */
    }
  }, [timelineWidth]);

  // Load models and tools, set default model from first available
  useEffect(() => {
    listModels().then(data => {
      setModels(data);
      if (data.length > 0) setSelectedModel(data[0].id);
    }).catch(console.error);
    listTools().then(setTools).catch(console.error);
  }, []);

  // Load threads for workspace
  useEffect(() => {
    if (!activeWorkspace) return;
    getSettings(activeWorkspace.id).then(settings => {
      if (settings?.max_rounds !== undefined) setMaxRounds(String(settings.max_rounds));
      if (settings?.max_verify_retries !== undefined) setMaxVerifyRetries(String(settings.max_verify_retries));
    }).catch(console.error);
    listThreads(activeWorkspace.id).then(data => {
      setThreads(data);
      if (threadId) {
        const found = data.find(t => t.id === threadId);
        if (found) setActiveThread(found);
      }
    }).catch(console.error);
  }, [activeWorkspace, threadId]);

  // Scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, pendingUserRequests, pendingPermissions]);

  // Poll for pending agent requests while a run is active
  const pollAgentRequests = useCallback(async (runId) => {
    if (!runId) return;
    try {
      const [userReqs, permReqs] = await Promise.all([
        listUserInputRequests({ run_id: runId, status: 'pending' }),
        listPermissionRequests({ run_id: runId, status: 'pending' }),
      ]);
      setPendingUserRequests(userReqs);
      setPendingPermissions(permReqs);
    } catch (e) {
      // silently ignore
    }
  }, []);

  // Poll for run completion and agent requests
  const pollRun = useCallback(async (runId) => {
    try {
      const run = await getRun(runId);
      setCurrentRun(run);
      const steps = await getRunSteps(runId);
      setRunSteps(steps);

      // Also poll for agent communication requests
      await pollAgentRequests(runId);

      if (run.status === 'completed' || run.status === 'failed' || run.status === 'cancelled') {
        clearInterval(pollRef.current);
        clearInterval(requestPollRef.current);
        setPendingUserRequests([]);
        setPendingPermissions([]);
        if (activeThread) {
          const msgs = await listMessages(activeThread.id);
          setMessages(msgs);
        }
        setSending(false);
      } else if (activeThread) {
        const msgs = await listMessages(activeThread.id);
        setMessages(msgs);
      }
    } catch (err) {
      console.error(err);
    }
  }, [activeThread, pollAgentRequests]);

  // Load messages for active thread (and resume polling if a run is still alive)
  useEffect(() => {
    if (!activeThread) {
      setMessages([]);
      return;
    }
    setLoading(true);
    listMessages(activeThread.id).then(async (data) => {
      setMessages(data);
      setLoading(false);
      const lastWithRun = [...data].reverse().find(m => m.run_id);
      if (lastWithRun?.run_id) {
        try {
          const run = await getRun(lastWithRun.run_id);
          setCurrentRun(run);
          const steps = await getRunSteps(lastWithRun.run_id);
          setRunSteps(steps);
          if (run && (run.status === 'running' || run.status === 'queued')) {
            setSending(true);
            if (pollRef.current) clearInterval(pollRef.current);
            pollRef.current = setInterval(() => pollRun(run.id), 1500);
          } else {
            setSending(false);
          }
        } catch (e) {
          console.error('Failed to load last run:', e);
        }
      }
    }).catch(err => {
      console.error(err);
      setLoading(false);
    });
  }, [activeThread, pollRun]);

  const handleSend = async (content) => {
    if (!activeWorkspace || !content.trim()) return;

    let thread = activeThread;
    if (!thread) {
      thread = await createThread({
        workspace_id: activeWorkspace.id,
        title: content.slice(0, 50),
      });
      setThreads(prev => [thread, ...prev]);
      setActiveThread(thread);
      navigate(`/chat/${thread.id}`);
    }

    setSending(true);
    setPendingUserRequests([]);
    setPendingPermissions([]);
    try {
      const result = await sendMessage(thread.id, {
        content,
        model: selectedModel,
        max_rounds: Number(maxRounds),
        max_verify_retries: Number(maxVerifyRetries),
        harness_mode: harnessMode,
        harness_candidates: harnessMode ? Number(harnessCandidates) : undefined,
      });
      if (harnessMode) {
        toast.info(`Harness mode активен — запускаю ${harnessCandidates} кандидата(ов) параллельно`);
      }

      setMessages(prev => [
        ...prev,
        ...(result.user_message ? [result.user_message] : []),
        result.message,
      ]);
      setCurrentRun(result.run);
      setRunSteps([]);

      // Poll run status and agent requests every 800ms
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(() => pollRun(result.run.id), 800);
    } catch (err) {
      console.error(err);
      setSending(false);
    }
  };

  const handleRunTaskMain = async () => {
    if (!activeWorkspace || sending) return;
    setSending(true);
    setPendingUserRequests([]);
    setPendingPermissions([]);
    try {
      let thread = activeThread;
      if (!thread) {
        thread = await createThread({
          workspace_id: activeWorkspace.id,
          title: 'Run TASK_MAIN.md',
        });
        setThreads(prev => [thread, ...prev]);
        setActiveThread(thread);
        navigate(`/chat/${thread.id}`);
      }
      const run = await startRun({
        workspace_id: activeWorkspace.id,
        thread_id: thread.id,
        model: selectedModel,
        max_rounds: Number(maxRounds),
        max_verify_retries: Number(maxVerifyRetries),
        harness_mode: harnessMode,
        harness_candidates: harnessMode ? Number(harnessCandidates) : undefined,
      });
      if (harnessMode) {
        toast.info(`Harness mode активен — запускаю ${harnessCandidates} кандидата(ов) параллельно`);
      }
      setCurrentRun(run);
      setRunSteps([]);
      const msgs = await listMessages(thread.id);
      setMessages(msgs);
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(() => pollRun(run.id), 800);
    } catch (err) {
      console.error(err);
      setSending(false);
    }
  };

  const handleCancelRun = useCallback(async () => {
    if (!currentRun?.id) return;
    try {
      await cancelRun(currentRun.id);
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
      setCurrentRun(prev => (prev ? { ...prev, status: 'cancelled' } : prev));
      setSending(false);
    } catch (err) {
      console.error(err);
    }
  }, [currentRun?.id]);

  const handleSelectThread = (thread) => {
    setActiveThread(thread);
    setCurrentRun(null);
    setRunSteps([]);
    setPendingUserRequests([]);
    setPendingPermissions([]);
    navigate(`/chat/${thread.id}`);
  };

  const handleNewThread = async () => {
    setActiveThread(null);
    setMessages([]);
    setCurrentRun(null);
    setRunSteps([]);
    setPendingUserRequests([]);
    setPendingPermissions([]);
    navigate('/chat');
  };

  const handleDeleteThread = useCallback(
    async (thread) => {
      if (!thread?.id) return;
      if (!window.confirm(`Удалить чат «${thread.title || 'без названия'}»? Сообщения будут удалены безвозвратно.`)) {
        return;
      }
      const deletingActiveThread = activeThread?.id === thread.id;
      try {
        const result = await deleteThread(thread.id);
        setThreads((prev) => prev.filter((t) => t.id !== thread.id));
        const cleanupCount = (result?.run_results || []).reduce(
          (acc, r) => acc + (r?.report?.removed_count || 0),
          0,
        );
        if (result?.detached_run_ids?.length) {
          toast.success(
            `Чат удалён, связанный run оставлен в Runs пока worker завершается (очищено ${cleanupCount} артефактов)`
          );
        } else if (cleanupCount > 0) {
          toast.success(`Чат удалён, очищено ${cleanupCount} артефактов`);
        } else {
          toast.success('Чат удалён');
        }
        if (deletingActiveThread) {
          if (pollRef.current) {
            clearInterval(pollRef.current);
            pollRef.current = null;
          }
          if (requestPollRef.current) {
            clearInterval(requestPollRef.current);
            requestPollRef.current = null;
          }
          setActiveThread(null);
          setMessages([]);
          setCurrentRun(null);
          setRunSteps([]);
          setPendingUserRequests([]);
          setPendingPermissions([]);
          setSending(false);
          navigate('/chat');
        }
      } catch (err) {
        console.error(err);
        const detail = err?.response?.data?.reason || err?.message || 'Неизвестная ошибка';
        toast.error(`Не удалось удалить чат: ${detail}`);
      }
    },
    [activeThread?.id, navigate],
  );

  // Cleanup polling on unmount (read refs at teardown so latest intervals are cleared)
  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      // eslint-disable-next-line react-hooks/exhaustive-deps -- clear active interval on unmount; ref is intentionally read here
      if (requestPollRef.current) clearInterval(requestPollRef.current);
    };
  }, []);

  // When the tab regains focus, immediately reconcile run + messages
  // (Chrome throttles/freezes setInterval in background tabs which can leave
  // the chat showing "running" long after the agent actually finished).
  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState !== 'visible') return;
      if (!activeThread) return;
      listMessages(activeThread.id).then(setMessages).catch(() => {});
      if (currentRun?.id) {
        getRun(currentRun.id).then((run) => {
          setCurrentRun(run);
          if (run && (run.status === 'completed' || run.status === 'failed' || run.status === 'cancelled')) {
            if (pollRef.current) clearInterval(pollRef.current);
            setSending(false);
          }
        }).catch(() => {});
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('focus', onVisibility);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('focus', onVisibility);
    };
  }, [activeThread, currentRun]);

  const cancelRunEnabled =
    sending &&
    currentRun &&
    (currentRun.status === 'running' || currentRun.status === 'queued');

  const bumpThreadList = useCallback((dx) => {
    setThreadListWidth((w) => Math.min(420, Math.max(180, w + dx)));
  }, []);

  const bumpTimeline = useCallback((dx) => {
    setTimelineWidth((w) => Math.min(640, Math.max(260, w - dx)));
  }, []);

  return (
    <div className="flex h-[calc(100vh-56px)] min-w-0">
      {/* Thread sidebar — ширина тянется за вертикальной ручкой справа */}
      <div
        className="hidden md:flex shrink-0 flex-col min-h-0 min-w-0 border-r border-border/70 bg-card/20"
        style={{ width: threadListWidth }}
      >
        <ThreadList
          threads={threads}
          activeThread={activeThread}
          onSelect={handleSelectThread}
          onNew={handleNewThread}
          onDelete={handleDeleteThread}
        />
      </div>
      <PanelResizeHandle
        className="hidden md:block self-stretch bg-border/30"
        onResize={bumpThreadList}
      />

      {/* Main chat area */}
      <div className="flex flex-1 min-w-0 min-h-0">
        {/* Chat column */}
        <div className="flex flex-1 flex-col min-w-0 min-h-0">
          {activeThread ? (
            <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border/70 bg-card/25 px-4 py-2.5 lg:px-8">
              <h2 className="min-w-0 truncate text-sm font-medium text-foreground">
                {activeThread.title || 'Без названия'}
              </h2>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8 shrink-0 gap-1.5 text-xs text-muted-foreground hover:border-destructive/50 hover:bg-destructive/10 hover:text-destructive"
                onClick={() => handleDeleteThread(activeThread)}
                data-testid="delete-active-thread"
              >
                <Trash2 className="h-3.5 w-3.5" />
                Удалить диалог
              </Button>
            </div>
          ) : null}
          {/* Messages */}
          <ScrollArea className="flex-1 px-4 lg:px-8 py-4">
            {loading ? (
              <div className="flex items-center justify-center h-40">
                <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
              </div>
            ) : messages.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-full min-h-[400px] text-center">
                <div className="w-14 h-14 rounded-xl bg-blue-500/10 border border-blue-500/20 flex items-center justify-center mb-4">
                  <span className="text-lg font-semibold text-blue-400">A7</span>
                </div>
                <h2 className="text-lg font-semibold mb-2">Umbrella</h2>
                <p className="text-sm text-muted-foreground max-w-md leading-relaxed">
                  Напишите задачу — агент выполнит её, используя доступные инструменты. Во время выполнения агент может задавать вопросы и запрашивать права.
                </p>
              </div>
            ) : (
              <div className="max-w-[800px] mx-auto space-y-5">
                {messages.map((msg, i) => (
                  <MessageCard key={msg.id || i} message={msg} />
                ))}

                {/* Agent communication cards — shown while run is active */}
                {pendingPermissions.map(req => (
                  <PermissionRequestCard
                    key={req.id}
                    request={req}
                    onResolved={(id) => setPendingPermissions(prev => prev.filter(r => r.id !== id))}
                  />
                ))}
                {pendingUserRequests.map(req => (
                  <UserInputRequestCard
                    key={req.id}
                    request={req}
                    onAnswered={(id) => setPendingUserRequests(prev => prev.filter(r => r.id !== id))}
                  />
                ))}

                {sending && pendingUserRequests.length === 0 && pendingPermissions.length === 0 && (
                  <div className="flex items-center gap-2 text-sm text-muted-foreground py-3">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    <span>Агент работает...</span>
                  </div>
                )}
                <div ref={messagesEndRef} />
              </div>
            )}
          </ScrollArea>

          {/* Composer */}
          <Composer
            onSend={handleSend}
            sending={sending}
            models={models}
            tools={tools}
            selectedModel={selectedModel}
            onModelChange={setSelectedModel}
            onRunTaskMain={handleRunTaskMain}
            onCancelRun={handleCancelRun}
            cancelEnabled={cancelRunEnabled}
            maxRounds={maxRounds}
            maxVerifyRetries={maxVerifyRetries}
            onMaxRoundsChange={setMaxRounds}
            onMaxVerifyRetriesChange={setMaxVerifyRetries}
            harnessMode={harnessMode}
            onHarnessModeChange={setHarnessMode}
            harnessCandidates={harnessCandidates}
            onHarnessCandidatesChange={setHarnessCandidates}
          />
        </div>

        <PanelResizeHandle
          className="hidden lg:block self-stretch bg-border/30"
          onResize={bumpTimeline}
        />
        <div
          className="hidden lg:flex shrink-0 flex-col min-h-0 min-w-0"
          style={{ width: timelineWidth }}
        >
          <TimelinePanel run={currentRun} steps={runSteps} />
        </div>
      </div>
    </div>
  );
}
