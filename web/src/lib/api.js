import axios from 'axios';

const API_BASE = (process.env.REACT_APP_BACKEND_URL || '') + '/api';

const api = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
});

/**
 * Статические серверы (serve -s и т.п.) часто отдают index.html на неизвестные пути вроде /api/*.
 * Тогда axios получает строку HTML — без проверки ломается код с .find по «списку».
 */
function asJsonArray(data, label) {
  if (Array.isArray(data)) return data;
  if (data == null) return [];
  if (typeof data === 'string' && data.trimStart().startsWith('<')) {
    console.warn(
      `[api] ${label}: ответ похож на HTML, а не JSON-массив. ` +
      'Запущен только фронт без API? Используйте `uv run python -m umbrella.web_bridge` или `npm start` (прокси на bridge).',
    );
    return [];
  }
  return [];
}

// Workspaces
export const createWorkspace = (data) => api.post('/workspaces', data).then(r => r.data);
export const listWorkspaces = () =>
  api.get('/workspaces').then((r) => asJsonArray(r.data, 'GET /workspaces'));
export const getWorkspace = (id) => api.get(`/workspaces/${id}`).then(r => r.data);
export const updateWorkspace = (id, data) => api.patch(`/workspaces/${id}`, data).then(r => r.data);
export const deleteWorkspace = (id) => api.delete(`/workspaces/${id}`).then(r => r.data);

// Threads
export const createThread = (data) => api.post('/threads', data).then(r => r.data);
export const listThreads = (workspaceId) =>
  api.get(`/threads?workspace_id=${workspaceId}`).then((r) => asJsonArray(r.data, 'GET /threads'));
export const getThread = (id) => api.get(`/threads/${id}`).then(r => r.data);
export const deleteThread = (id) => api.delete(`/threads/${id}`).then(r => r.data);

// Messages
export const listMessages = (threadId) =>
  api.get(`/threads/${threadId}/messages`).then((r) => asJsonArray(r.data, 'GET /threads/.../messages'));
export const sendMessage = (threadId, data) => api.post(`/threads/${threadId}/messages`, data).then(r => r.data);

// Runs
export const listRuns = (params) => api.get('/runs', { params }).then(r => r.data);
export const startRun = (data) => api.post('/runs', data).then(r => r.data);
export const getRun = (id) => api.get(`/runs/${id}`).then(r => r.data);
export const getRunSteps = (id) =>
  api.get(`/runs/${id}/steps`).then((r) => asJsonArray(r.data, 'GET /runs/.../steps'));
export const getRunTimeline = (id) =>
  api.get(`/runs/${id}/timeline`).then((r) => r.data || { phases: [] });
export const cancelRun = (id, options) => {
  const body = {};
  if (options && Number.isFinite(options.wait)) body.wait = options.wait;
  if (options && Number.isFinite(options.force_after)) body.force_after = options.force_after;
  return api.post(`/runs/${id}/cancel`, body).then(r => r.data);
};
export const deleteRun = (id, params) => api.delete(`/runs/${id}`, { params }).then(r => r.data);

// Logs
export const listLogs = (params) => api.get('/logs', { params }).then(r => r.data);

// Memory
export const listMemoryNodes = (workspaceId, runId) => {
  const params = { workspace_id: workspaceId };
  if (runId) params.run_id = runId;
  return api.get('/memory', { params })
    .then((r) => asJsonArray(r.data, 'GET /memory'));
};
export const getMemoryNode = (id) => api.get(`/memory/${id}`).then(r => r.data);
export const updateMemoryNode = (id, data) => api.patch(`/memory/${id}`, data).then(r => r.data);
export const deleteMemoryNode = (id, params) =>
  api.delete(`/memory/${id}`, { params }).then(r => r.data);

// Settings
export const getSettings = (workspaceId) => api.get(`/settings?workspace_id=${workspaceId}`).then(r => r.data);
export const updateSettings = (workspaceId, data) => api.patch(`/settings?workspace_id=${workspaceId}`, data).then(r => r.data);

// Dashboard
export const getDashboardStats = (workspaceId) => api.get(`/dashboard/stats?workspace_id=${workspaceId}`).then(r => r.data);

// Models & Tools
export const listModels = () => api.get('/models').then((r) => asJsonArray(r.data, 'GET /models'));
export const listTools = () => api.get('/tools').then((r) => asJsonArray(r.data, 'GET /tools'));

// User Communication — запросы агента к пользователю во время выполнения
export const listUserInputRequests = (params) =>
  api.get('/user-input', { params }).then((r) => asJsonArray(r.data, 'GET /user-input'));
export const answerUserInputRequest = (reqId, answer) =>
  api.post(`/user-input/${reqId}/answer`, { answer }).then(r => r.data);

// Permission Escalation — агент запрашивает права (docker, sudo, install и т.д.)
export const listPermissionRequests = (params) =>
  api.get('/permission-request', { params }).then((r) => asJsonArray(r.data, 'GET /permission-request'));
export const resolvePermissionRequest = (reqId, granted) =>
  api.post(`/permission-request/${reqId}/resolve`, { granted }).then(r => r.data);

// MCP Registry
export const listMcpServers = () =>
  api.get('/mcp/servers').then((r) => asJsonArray(r.data, 'GET /mcp/servers'));
export const addMcpServer = (data) => api.post('/mcp/servers', data).then(r => r.data);
export const updateMcpServer = (id, data) =>
  api.patch(`/mcp/servers/${id}`, data).then(r => r.data);
export const deleteMcpServer = (id) => api.delete(`/mcp/servers/${id}`).then(r => r.data);
export const discoverMcpServers = (data) => api.post('/mcp/discover', data).then(r => r.data);

export default api;
