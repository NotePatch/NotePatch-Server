import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiRequest, type AdminAuditLog, type AdminOperation, type Page } from "../lib/api";
import { StatusBadge } from "../components/StatusBadge";
import { formatDate } from "../lib/format";

export function OperationsPage() {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<"operations" | "audit">("operations");
  const operations = useQuery({ queryKey: ["admin-operations"], queryFn: () => apiRequest<Page<AdminOperation>>("/admin/operations"), refetchInterval: 5000 });
  const audit = useQuery({ queryKey: ["admin-audit"], queryFn: () => apiRequest<Page<AdminAuditLog>>("/admin/audit-logs") });
  const retry = useMutation({ mutationFn: (id: string) => apiRequest(`/admin/operations/${id}/retry`, { method: "POST", body: "{}" }), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["admin-operations"] }) });
  return <section className="page">
    <div className="page-header"><div><h1>操作与审计</h1><p>异步删除进度和管理员写操作记录</p></div><button className="secondary-button" onClick={() => { operations.refetch(); audit.refetch(); }}><RefreshCw size={16}/>刷新</button></div>
    <div className="segmented page-tabs"><button className={tab === "operations" ? "active" : ""} onClick={() => setTab("operations")}>异步操作</button><button className={tab === "audit" ? "active" : ""} onClick={() => setTab("audit")}>审计日志</button></div>
    {retry.error ? <div className="error-banner">{retry.error.message}</div> : null}
    {tab === "operations" ? <section className="panel"><table><thead><tr><th>操作</th><th>目标</th><th>状态</th><th>阶段</th><th>Task</th><th>更新时间</th><th></th></tr></thead><tbody>{(operations.data?.items || []).map((item) => <tr key={item.id}><td>{item.operation_type}</td><td className="mono">{item.target_type}:{item.target_id}</td><td><StatusBadge value={item.status}/></td><td>{item.phase || "-"}</td><td className="mono">{item.task_id || "-"}</td><td>{formatDate(item.updated_at)}</td><td>{item.status === "failed" ? <button className="tiny-button" onClick={() => retry.mutate(item.id)}>重试</button> : null}</td></tr>)}</tbody></table></section> : <section className="panel"><table><thead><tr><th>管理员</th><th>动作</th><th>目标</th><th>Workspace</th><th>时间</th></tr></thead><tbody>{(audit.data?.items || []).map((item) => <tr key={item.id}><td>{item.actor_email}</td><td>{item.action}</td><td className="mono">{item.target_type}:{item.target_id}</td><td className="mono">{item.workspace_id || "-"}</td><td>{formatDate(item.created_at)}</td></tr>)}</tbody></table></section>}
  </section>;
}
