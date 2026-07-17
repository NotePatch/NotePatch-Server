export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8001/api/v1";

const TOKEN_KEY = "notepatch_admin_tokens";

export type TokenBundle = {
  access_token: string;
  refresh_token: string;
  expires_at: string;
};

export type AdminUser = {
  id: string;
  email: string;
  full_name?: string | null;
  username?: string | null;
  phone?: string | null;
  is_active: boolean;
  must_change_password: boolean;
  ai_history_enabled: boolean;
  created_at: string;
};

export type LearningUnit = {
  id: string;
  workspace_id: string;
  title: string;
  subject?: string | null;
  grade_level?: string | null;
  topic?: string | null;
  knowledge_revision: number;
  attempt_revision: number;
  notes_generated_revision: number;
  note_generation_due_at?: string | null;
  created_at: string;
  updated_at: string;
};

export type StudyNote = {
  id: string;
  workspace_id: string;
  learning_unit_id: string;
  version_no: number;
  title: string;
  html_object_key: string;
  json_object_key: string;
  highlighted_html_object_key?: string | null;
  knowledge_point_ids: string[];
  source_version_id?: string | null;
  edit_origin?: string | null;
  edit_summary?: string | null;
  created_at: string;
};

export type FlashcardDeck = {
  id: string;
  workspace_id: string;
  learning_unit_id: string;
  study_note_version_id: string;
  version_no: number;
  attempt_revision: number;
  weighting_config: Record<string, number>;
  created_at: string;
};

export type Flashcard = {
  id: string;
  knowledge_point_id: string;
  front: string;
  back: string;
  priority_score: number;
  priority_factors: Record<string, number>;
  rank: number;
};

export type FlashcardDeckDetail = { deck: FlashcardDeck; cards: Flashcard[] };

export type KnowledgeChunk = {
  id: string;
  workspace_id: string;
  document_id?: string | null;
  subject?: string | null;
  source_type?: string | null;
  content: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type Homework = {
  id: string;
  workspace_id: string;
  title: string;
  description?: string | null;
  document_id?: string | null;
  status: string;
  rubric_text?: string | null;
  max_score: number;
  created_at: string;
};

export type Mistake = {
  id: string;
  workspace_id: string;
  knowledge_point?: string | null;
  description: string;
  status: string;
  created_at: string;
};

export type Conversation = {
  id: string;
  workspace_id: string;
  user_id: string;
  title: string;
  last_message_at?: string | null;
  created_at: string;
  updated_at: string;
};

export type ChatMessage = {
  id: string;
  role: string;
  content: string;
  status: string;
  error_message?: string | null;
  created_at: string;
};

export type AdminOperation = {
  id: string;
  operation_type: string;
  target_type: string;
  target_id: string;
  status: string;
  phase?: string | null;
  task_id?: string | null;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
};

export type AdminAuditLog = {
  id: string;
  actor_email: string;
  action: string;
  target_type: string;
  target_id: string;
  workspace_id?: string | null;
  created_at: string;
};

export type AdminWorkspace = {
  id: string;
  name: string;
  type: string;
  owner_user_id: string;
  created_at: string;
};

export type Page<T> = {
  page: number;
  page_size: number;
  total: number;
  items: T[];
};

export type AdminUserListItem = AdminUser & {
  workspace_id?: string | null;
  workspace_name?: string | null;
  documents_count: number;
  tasks_count: number;
};

export type AdminUserDetail = {
  user: AdminUser;
  workspace?: AdminWorkspace | null;
  counts: Record<string, number>;
  document_status_counts: Record<string, number>;
  task_status_counts: Record<string, number>;
};

export type AdminDocument = {
  id: string;
  workspace_id: string;
  uploaded_by: string;
  uploaded_by_email?: string | null;
  title?: string | null;
  original_filename: string;
  mime_type?: string | null;
  file_size?: number | null;
  file_type: string;
  document_kind: string;
  status: string;
  artifacts_count: number;
  created_at: string;
  updated_at: string;
};

export type AdminDocumentDetail = {
  document: AdminDocument;
  bucket: string;
  object_key: string;
  storage_backend: string;
  upload_id?: string | null;
  tus_upload_url?: string | null;
  sha256?: string | null;
  metadata: Record<string, unknown>;
};

export type AdminArtifact = {
  id: string;
  workspace_id: string;
  document_id: string;
  artifact_type: string;
  bucket: string;
  object_key: string;
  mime_type?: string | null;
  file_size?: number | null;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type AdminTask = {
  id: string;
  workspace_id: string;
  task_type: string;
  status: string;
  resource_type?: string | null;
  resource_id?: string | null;
  progress: number;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
};

export type AdminTaskDetail = {
  task: AdminTask;
  payload: Record<string, unknown>;
  result?: Record<string, unknown> | null;
};

export type AdminTaskEvent = {
  id: string;
  task_id: string;
  workspace_id: string;
  event_type: string;
  level: string;
  message: string;
  progress?: number | null;
  data: Record<string, unknown>;
  created_at: string;
};

export type AdminQueueStatus = {
  name: string;
  redis_key: string;
  length?: number | null;
  status: string;
  error?: string | null;
};

export type AdminServiceStatus = {
  name: string;
  status: string;
  detail?: string | null;
  latency_ms?: number | null;
};

export type AdminOverview = {
  users_count: number;
  documents_count: number;
  uploaded_documents_count: number;
  ready_documents_count: number;
  tasks_count: number;
  failed_tasks_count: number;
  queued_tasks_count: number;
  running_tasks_count: number;
  ocr_artifacts_count: number;
  learning_units_count: number;
  study_notes_count: number;
  homeworks_count: number;
  open_mistakes_count: number;
  queue_lengths: AdminQueueStatus[];
};

export type DownloadUrl = {
  id: string;
  resource_type: string;
  filename: string;
  mime_type?: string | null;
  expires_in: number;
  download_url: string;
};

export type AdminMe = {
  user: AdminUser;
  admin: boolean;
};

export function getTokens(): TokenBundle | null {
  const raw = localStorage.getItem(TOKEN_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as TokenBundle;
  } catch {
    localStorage.removeItem(TOKEN_KEY);
    return null;
  }
}

export function setTokens(tokens: TokenBundle): void {
  localStorage.setItem(TOKEN_KEY, JSON.stringify(tokens));
}

export function clearTokens(): void {
  localStorage.removeItem(TOKEN_KEY);
}

async function parseJson<T>(response: Response): Promise<T> {
  const text = await response.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

let refreshPromise: Promise<boolean> | null = null;

async function performRefreshAccessToken(): Promise<boolean> {
  const tokens = getTokens();
  if (!tokens?.refresh_token) return false;
  const attemptedRefreshToken = tokens.refresh_token;
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: attemptedRefreshToken })
    });
  } catch {
    return false;
  }
  if (!response.ok) {
    if (getTokens()?.refresh_token === attemptedRefreshToken) clearTokens();
    return false;
  }
  const payload = await parseJson<TokenBundle>(response);
  setTokens(payload);
  return true;
}

async function refreshAccessToken(): Promise<boolean> {
  if (!refreshPromise) {
    refreshPromise = performRefreshAccessToken().finally(() => {
      refreshPromise = null;
    });
  }
  return refreshPromise;
}

export async function apiRequest<T>(path: string, init: RequestInit = {}, retry = true): Promise<T> {
  const tokens = getTokens();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (!(init.body instanceof FormData) && init.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  if (tokens?.access_token) {
    headers.set("Authorization", `Bearer ${tokens.access_token}`);
  }
  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });
  if (response.status === 401 && retry && (await refreshAccessToken())) {
    return apiRequest<T>(path, init, false);
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await parseJson<{ detail?: string }>(response);
      detail = payload.detail || detail;
    } catch {
      // Keep the HTTP status text.
    }
    throw new Error(detail);
  }
  return parseJson<T>(response);
}

export async function login(email: string, password: string): Promise<AdminMe> {
  const response = await fetch(`${API_BASE_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ email, password })
  });
  if (!response.ok) {
    throw new Error("登录失败");
  }
  const tokens = await parseJson<TokenBundle>(response);
  setTokens(tokens);
  try {
    return await apiRequest<AdminMe>("/admin/me");
  } catch (error) {
    clearTokens();
    throw error;
  }
}

export async function logout(): Promise<void> {
  const tokens = getTokens();
  if (tokens?.refresh_token) {
    try {
      await fetch(`${API_BASE_URL}/auth/logout`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: tokens.refresh_token })
      });
    } catch {
      // Local logout should still complete if the backend is unavailable.
    }
  }
  clearTokens();
}

export function buildQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  });
  const text = search.toString();
  return text ? `?${text}` : "";
}
