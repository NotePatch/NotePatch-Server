import { RefreshCw } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { apiRequest, type AdminOverview } from "../lib/api";
import { StatusBadge } from "../components/StatusBadge";

export function OverviewPage() {
  const overview = useQuery({
    queryKey: ["overview"],
    queryFn: () => apiRequest<AdminOverview>("/admin/overview"),
    refetchInterval: 15000
  });

  const data = overview.data;
  return (
    <section className="page">
      <div className="page-header">
        <div>
          <h1>总览</h1>
          <p>用户、文档、任务和队列状态</p>
        </div>
        <button className="secondary-button" onClick={() => overview.refetch()}>
          <RefreshCw size={16} />
          刷新
        </button>
      </div>

      {overview.error ? <div className="error-banner">{overview.error.message}</div> : null}

      <div className="metric-grid">
        <Metric label="用户" value={data?.users_count} to="/users" />
        <Metric label="文档" value={data?.documents_count} to="/documents" />
        <Metric label="Ready 文档" value={data?.ready_documents_count} to="/documents?status=ready" />
        <Metric label="任务" value={data?.tasks_count} to="/tasks" />
        <Metric label="失败任务" value={data?.failed_tasks_count} to="/tasks?status=failed" tone="danger" />
        <Metric label="OCR Artifacts" value={data?.ocr_artifacts_count} to="/documents" />
      </div>

      <section className="panel">
        <div className="panel-header">
          <h2>队列</h2>
          <Link to="/system">系统状态</Link>
        </div>
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>Redis Key</th>
              <th>长度</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {(data?.queue_lengths || []).map((queue) => (
              <tr key={queue.name}>
                <td>{queue.name}</td>
                <td className="mono">{queue.redis_key}</td>
                <td>{queue.length ?? "-"}</td>
                <td>
                  <StatusBadge value={queue.status} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </section>
  );
}

function Metric({ label, value, to, tone }: { label: string; value?: number; to: string; tone?: "danger" }) {
  return (
    <Link className={`metric ${tone === "danger" ? "metric-danger" : ""}`} to={to}>
      <span>{label}</span>
      <strong>{value ?? "-"}</strong>
    </Link>
  );
}
