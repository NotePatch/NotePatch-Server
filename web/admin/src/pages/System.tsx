import { RefreshCw } from "lucide-react";
import { useQuery } from "@tanstack/react-query";

import { StatusBadge } from "../components/StatusBadge";
import { apiRequest, type AdminQueueStatus, type AdminServiceStatus } from "../lib/api";

export function SystemPage() {
  const queues = useQuery({
    queryKey: ["queues"],
    queryFn: () => apiRequest<{ queues: AdminQueueStatus[] }>("/admin/queues"),
    refetchInterval: 10000
  });
  const services = useQuery({
    queryKey: ["services"],
    queryFn: () => apiRequest<{ services: AdminServiceStatus[] }>("/admin/services"),
    refetchInterval: 15000
  });

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <h1>系统</h1>
          <p>队列、存储和外部服务</p>
        </div>
        <button
          className="secondary-button"
          onClick={() => {
            queues.refetch();
            services.refetch();
          }}
        >
          <RefreshCw size={16} />
          刷新
        </button>
      </div>
      <div className="detail-grid">
        <section className="panel wide">
          <h2>Queues</h2>
          <table>
            <thead>
              <tr>
                <th>名称</th>
                <th>Redis Key</th>
                <th>长度</th>
                <th>状态</th>
                <th>错误</th>
              </tr>
            </thead>
            <tbody>
              {(queues.data?.queues || []).map((queue) => (
                <tr key={queue.name}>
                  <td>{queue.name}</td>
                  <td className="mono">{queue.redis_key}</td>
                  <td>{queue.length ?? "-"}</td>
                  <td><StatusBadge value={queue.status} /></td>
                  <td className="truncate">{queue.error || "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
        <section className="panel wide">
          <h2>Services</h2>
          <table>
            <thead>
              <tr>
                <th>名称</th>
                <th>状态</th>
                <th>延迟</th>
                <th>详情</th>
              </tr>
            </thead>
            <tbody>
              {(services.data?.services || []).map((service) => (
                <tr key={service.name}>
                  <td>{service.name}</td>
                  <td><StatusBadge value={service.status} /></td>
                  <td>{service.latency_ms ?? "-"} ms</td>
                  <td className="truncate">{service.detail || "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </section>
  );
}
