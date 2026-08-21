import { Search } from "lucide-react";
import { FormEvent, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { Pagination } from "../components/Pagination";
import { StatusBadge } from "../components/StatusBadge";
import {
  apiRequest,
  buildQuery,
  type Page,
  type WorkflowDetail,
  type WorkflowEvent,
  type WorkflowRun
} from "../lib/api";
import { compactId, formatDate, jsonText } from "../lib/format";

const PAGE_SIZE = 25;

export function WorkflowsPage() {
  const [params, setParams] = useSearchParams();
  const page = Number(params.get("page") || "1");
  const status = params.get("status") || "";
  const documentId = params.get("document_id") || "";
  const [filters, setFilters] = useState({ status, document_id: documentId });
  const query = useQuery({
    queryKey: ["workflows", page, status, documentId],
    queryFn: () =>
      apiRequest<Page<WorkflowRun>>(
        "/admin/workflows" +
          buildQuery({
            page,
            page_size: PAGE_SIZE,
            status,
            document_id: documentId
          })
      )
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setParams({
      page: "1",
      ...Object.fromEntries(Object.entries(filters).filter(([, value]) => value))
    });
  }

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <h1>工作流</h1>
          <p>上传、核心处理和增强处理的聚合状态</p>
        </div>
      </div>
      <form className="filters" onSubmit={submit}>
        <label>
          状态
          <select
            value={filters.status}
            onChange={(event) => setFilters({ ...filters, status: event.target.value })}
          >
            <option value="">全部</option>
            <option value="waiting_upload">waiting_upload</option>
            <option value="queued">queued</option>
            <option value="running">running</option>
            <option value="waiting">waiting</option>
            <option value="succeeded">succeeded</option>
            <option value="partially_succeeded">partially_succeeded</option>
            <option value="failed">failed</option>
            <option value="cancelled">cancelled</option>
          </select>
        </label>
        <label>
          Document ID
          <input
            value={filters.document_id}
            onChange={(event) => setFilters({ ...filters, document_id: event.target.value })}
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
              <th>总状态</th>
              <th>核心</th>
              <th>增强</th>
              <th>阶段</th>
              <th>进度</th>
              <th>文档</th>
              <th>更新时间</th>
            </tr>
          </thead>
          <tbody>
            {(query.data?.items || []).map((workflow) => (
              <tr key={workflow.id}>
                <td className="mono">
                  <Link to={"/workflows/" + workflow.id}>{compactId(workflow.id)}</Link>
                </td>
                <td><StatusBadge value={workflow.status} /></td>
                <td><StatusBadge value={workflow.core_status} /></td>
                <td><StatusBadge value={workflow.enrichment_status} /></td>
                <td>{workflow.current_stage || "-"}</td>
                <td>{workflow.progress}%</td>
                <td className="mono">{compactId(workflow.document_id)}</td>
                <td>{formatDate(workflow.updated_at)}</td>
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
              ...Object.fromEntries(
                Object.entries({ status, document_id: documentId }).filter(([, value]) => value)
              )
            })
          }
        />
      </section>
    </section>
  );
}

export function WorkflowDetailPage() {
  const { workflowId } = useParams();
  const detail = useQuery({
    queryKey: ["workflow", workflowId],
    queryFn: () => apiRequest<WorkflowDetail>("/admin/workflows/" + workflowId),
    enabled: Boolean(workflowId),
    refetchInterval: (query) => {
      const value = query.state.data?.workflow.status;
      return value && !["succeeded", "partially_succeeded", "failed", "cancelled"].includes(value)
        ? 3000
        : false;
    }
  });
  const events = useQuery({
    queryKey: ["workflow-events", workflowId],
    queryFn: () => apiRequest<WorkflowEvent[]>("/admin/workflows/" + workflowId + "/events"),
    enabled: Boolean(workflowId),
    refetchInterval: 3000
  });
  const workflow = detail.data?.workflow;

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <h1>工作流详情</h1>
          <p>{workflow?.id || workflowId}</p>
        </div>
      </div>
      {detail.error ? <div className="error-banner">{detail.error.message}</div> : null}
      {workflow ? (
        <div className="detail-grid">
          <section className="panel">
            <h2>状态</h2>
            <dl className="kv">
              <dt>总状态</dt>
              <dd><StatusBadge value={workflow.status} /></dd>
              <dt>核心处理</dt>
              <dd><StatusBadge value={workflow.core_status} /></dd>
              <dt>增强处理</dt>
              <dd><StatusBadge value={workflow.enrichment_status} /></dd>
              <dt>当前阶段</dt>
              <dd>{workflow.current_stage || "-"}</dd>
              <dt>进度</dt>
              <dd>{workflow.progress}%</dd>
              <dt>等待至</dt>
              <dd>{formatDate(workflow.waiting_until)}</dd>
            </dl>
          </section>
          <section className="panel">
            <h2>关联资源</h2>
            <dl className="kv">
              <dt>Workspace</dt>
              <dd className="mono">{workflow.workspace_id}</dd>
              <dt>Document</dt>
              <dd className="mono">{workflow.document_id || "-"}</dd>
              <dt>Learning Unit</dt>
              <dd className="mono">{workflow.learning_unit_id || "-"}</dd>
              <dt>错误</dt>
              <dd>{workflow.error_message || "-"}</dd>
            </dl>
          </section>
          <section className="panel wide">
            <h2>阶段任务</h2>
            <table>
              <thead>
                <tr>
                  <th>阶段</th>
                  <th>分层</th>
                  <th>任务</th>
                  <th>状态</th>
                  <th>进度</th>
                </tr>
              </thead>
              <tbody>
                {(detail.data?.tasks || []).map((item) => (
                  <tr key={item.task.id}>
                    <td>{item.stage}</td>
                    <td>{item.phase}</td>
                    <td className="mono">
                      <Link to={"/tasks/" + item.task.id}>{item.task.task_type}</Link>
                    </td>
                    <td><StatusBadge value={item.task.status} /></td>
                    <td>{item.task.progress}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
          <section className="panel wide">
            <h2>工作流事件</h2>
            <div className="timeline">
              {(events.data || []).map((event) => (
                <article key={event.id} className={"timeline-item timeline-" + event.level}>
                  <div>
                    <strong>{event.event_type}</strong>
                    <span>{event.stage || "-"} · {formatDate(event.created_at)}</span>
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
