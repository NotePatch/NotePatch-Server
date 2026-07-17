import { RotateCcw, Search, XCircle } from "lucide-react";
import { FormEvent, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Pagination } from "../components/Pagination";
import { StatusBadge } from "../components/StatusBadge";
import {
  apiRequest,
  buildQuery,
  type AdminTask,
  type AdminTaskDetail,
  type AdminTaskEvent,
  type Page
} from "../lib/api";
import { compactId, formatDate, jsonText } from "../lib/format";

const PAGE_SIZE = 25;

export function TasksPage() {
  const [params, setParams] = useSearchParams();
  const page = Number(params.get("page") || "1");
  const status = params.get("status") || "";
  const taskType = params.get("task_type") || "";
  const resourceId = params.get("resource_id") || "";
  const [filters, setFilters] = useState({ status, task_type: taskType, resource_id: resourceId });
  const query = useQuery({
    queryKey: ["tasks", page, status, taskType, resourceId],
    queryFn: () =>
      apiRequest<Page<AdminTask>>(
        `/admin/tasks${buildQuery({
          page,
          page_size: PAGE_SIZE,
          status,
          task_type: taskType,
          resource_id: resourceId
        })}`
      )
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setParams({ page: "1", ...Object.fromEntries(Object.entries(filters).filter(([, value]) => value)) });
  }

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <h1>任务</h1>
          <p>异步 worker 任务和失败原因</p>
        </div>
      </div>
      <form className="filters" onSubmit={submit}>
        <label>
          状态
          <select value={filters.status} onChange={(event) => setFilters({ ...filters, status: event.target.value })}>
            <option value="">全部</option>
            <option value="queued">queued</option>
            <option value="running">running</option>
            <option value="succeeded">succeeded</option>
            <option value="failed">failed</option>
            <option value="cancelled">cancelled</option>
          </select>
        </label>
        <label>
          类型
          <select
            value={filters.task_type}
            onChange={(event) => setFilters({ ...filters, task_type: event.target.value })}
          >
            <option value="">全部</option>
            <option value="document_processing_pipeline">document_processing_pipeline</option>
            <option value="ocr_document">ocr_document</option>
            <option value="grade_homework">grade_homework</option>
            <option value="openclaw_agent_run">openclaw_agent_run</option>
            <option value="build_knowledge_base">build_knowledge_base</option>
            <option value="generate_flashcards">generate_flashcards</option>
          </select>
        </label>
        <label>
          Resource ID
          <input
            value={filters.resource_id}
            onChange={(event) => setFilters({ ...filters, resource_id: event.target.value })}
          />
        </label>
        <button className="secondary-button">
          <Search size={16} />
          查询
        </button>
      </form>
      {query.error ? <div className="error-banner">{query.error.message}</div> : null}
      <section className="panel">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>类型</th>
              <th>状态</th>
              <th>进度</th>
              <th>Resource</th>
              <th>错误</th>
              <th>更新时间</th>
            </tr>
          </thead>
          <tbody>
            {(query.data?.items || []).map((task) => (
              <tr key={task.id}>
                <td className="mono">
                  <Link to={`/tasks/${task.id}`}>{compactId(task.id)}</Link>
                </td>
                <td>{task.task_type}</td>
                <td><StatusBadge value={task.status} /></td>
                <td>{task.progress}%</td>
                <td className="mono">{compactId(task.resource_id)}</td>
                <td className="truncate">{task.error_message || "-"}</td>
                <td>{formatDate(task.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <Pagination
          page={page}
          pageSize={PAGE_SIZE}
          total={query.data?.total || 0}
          onPageChange={(next) =>
            setParams({
              page: String(next),
              ...Object.fromEntries(Object.entries({ status, task_type: taskType, resource_id: resourceId }).filter(([, value]) => value))
            })
          }
        />
      </section>
    </section>
  );
}

export function TaskDetailPage() {
  const { taskId } = useParams();
  const queryClient = useQueryClient();
  const detail = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => apiRequest<AdminTaskDetail>(`/admin/tasks/${taskId}`)
  });
  const events = useQuery({
    queryKey: ["task-events", taskId],
    queryFn: () => apiRequest<AdminTaskEvent[]>(`/admin/tasks/${taskId}/events`),
    enabled: Boolean(taskId),
    refetchInterval: detail.data?.task.status === "running" || detail.data?.task.status === "queued" ? 3000 : false
  });
  const cancel = useMutation({ mutationFn: () => apiRequest(`/admin/tasks/${taskId}/cancel`, { method: "POST", body: "{}" }), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["task", taskId] }) });
  const retry = useMutation({ mutationFn: () => apiRequest(`/admin/tasks/${taskId}/retry`, { method: "POST", body: "{}" }), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["tasks"] }) });

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <h1>{detail.data?.task.task_type || "任务"}</h1>
          <p>{detail.data?.task.id || ""}</p>
        </div><div className="button-row">{detail.data?.task.status === "queued" || detail.data?.task.status === "running" ? <button className="secondary-button" onClick={() => cancel.mutate()}><XCircle size={16}/>取消</button> : null}{detail.data?.task.status === "failed" || detail.data?.task.status === "cancelled" ? <button className="secondary-button" onClick={() => retry.mutate()}><RotateCcw size={16}/>重试</button> : null}</div>
      </div>
      {detail.error ? <div className="error-banner">{detail.error.message}</div> : null}
      {cancel.error || retry.error ? <div className="error-banner">{(cancel.error || retry.error)?.message}</div> : null}
      {detail.data ? (
        <div className="detail-grid">
          <section className="panel">
            <h2>状态</h2>
            <dl className="kv">
              <dt>Status</dt>
              <dd><StatusBadge value={detail.data.task.status} /></dd>
              <dt>Progress</dt>
              <dd>{detail.data.task.progress}%</dd>
              <dt>Workspace</dt>
              <dd className="mono">{detail.data.task.workspace_id}</dd>
              <dt>Resource</dt>
              <dd className="mono">{detail.data.task.resource_id || "-"}</dd>
            </dl>
          </section>
          <section className="panel wide">
            <h2>Payload</h2>
            <pre>{jsonText(detail.data.payload)}</pre>
          </section>
          <section className="panel wide">
            <h2>Result</h2>
            <pre>{jsonText(detail.data.result)}</pre>
          </section>
          <section className="panel wide">
            <h2>Events</h2>
            <div className="timeline">
              {(events.data || []).map((event) => (
                <article key={event.id} className={`timeline-item timeline-${event.level}`}>
                  <div>
                    <strong>{event.event_type}</strong>
                    <span>{formatDate(event.created_at)}</span>
                  </div>
                  <p>{event.message}</p>
                  <pre>{jsonText(event.data)}</pre>
                </article>
              ))}
            </div>
          </section>
        </div>
      ) : null}
    </section>
  );
}
