import { KeyRound, Plus, Save, Search, Trash2, UserX } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Pagination } from "../components/Pagination";
import { apiRequest, buildQuery, type AdminUserDetail, type AdminUserListItem, type Page } from "../lib/api";
import { compactId, formatDate } from "../lib/format";

const PAGE_SIZE = 25;

export function UsersPage() {
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const page = Number(params.get("page") || "1");
  const search = params.get("search") || "";
  const [searchInput, setSearchInput] = useState(search);
  const [newEmail, setNewEmail] = useState("");
  const [newName, setNewName] = useState("");
  const query = useQuery({
    queryKey: ["users", page, search],
    queryFn: () =>
      apiRequest<Page<AdminUserListItem>>(
        `/admin/users${buildQuery({ page, page_size: PAGE_SIZE, search })}`
      )
  });
  const create = useMutation({
    mutationFn: () => apiRequest<{ temporary_password: string }>("/admin/users", { method: "POST", body: JSON.stringify({ email: newEmail, full_name: newName || null }) }),
    onSuccess: (result) => {
      window.alert(`临时密码（仅显示一次）：${result.temporary_password}`);
      setNewEmail(""); setNewName(""); queryClient.invalidateQueries({ queryKey: ["users"] });
    }
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setParams({ page: "1", ...(searchInput ? { search: searchInput } : {}) });
  }

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <h1>用户</h1>
          <p>账号、个人 workspace 和数据量</p>
        </div>
      </div>
      <form className="filters" onSubmit={submit}>
        <label>
          搜索
          <input value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="email / name" />
        </label>
        <button className="secondary-button">
          <Search size={16} />
          查询
        </button>
      </form>
      <form className="filters" onSubmit={(event) => { event.preventDefault(); create.mutate(); }}>
        <label>新用户邮箱<input type="email" value={newEmail} onChange={(event) => setNewEmail(event.target.value)}/></label>
        <label>姓名<input value={newName} onChange={(event) => setNewName(event.target.value)}/></label>
        <button className="primary-button" disabled={!newEmail}><Plus size={16}/>创建用户</button>
      </form>
      {create.error ? <div className="error-banner">{create.error.message}</div> : null}
      {query.error ? <div className="error-banner">{query.error.message}</div> : null}
      <section className="panel">
        <table>
          <thead>
            <tr>
              <th>邮箱</th>
              <th>姓名</th>
              <th>Workspace</th>
              <th>文档</th>
              <th>任务</th>
              <th>创建时间</th>
            </tr>
          </thead>
          <tbody>
            {(query.data?.items || []).map((user) => (
              <tr key={user.id}>
                <td>
                  <Link to={`/users/${user.id}`}>{user.email}</Link>
                </td>
                <td>{user.full_name || "-"}</td>
                <td className="mono" title={user.workspace_id || ""}>
                  {compactId(user.workspace_id)}
                </td>
                <td>{user.documents_count}</td>
                <td>{user.tasks_count}</td>
                <td>{formatDate(user.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <Pagination
          page={page}
          pageSize={PAGE_SIZE}
          total={query.data?.total || 0}
          onPageChange={(next) => setParams({ page: String(next), ...(search ? { search } : {}) })}
        />
      </section>
    </section>
  );
}

export function UserDetailPage() {
  const { userId } = useParams();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["user", userId],
    queryFn: () => apiRequest<AdminUserDetail>(`/admin/users/${userId}`)
  });
  const data = query.data;
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [phone, setPhone] = useState("");
  const [historyEnabled, setHistoryEnabled] = useState(true);
  const [confirmEmail, setConfirmEmail] = useState("");
  const update = useMutation({
    mutationFn: (body: Record<string, unknown>) => apiRequest(`/admin/users/${userId}`, { method: "PATCH", body: JSON.stringify(body) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["user", userId] })
  });
  const resetPassword = useMutation({
    mutationFn: () => apiRequest<{ temporary_password: string }>(`/admin/users/${userId}/reset-password`, { method: "POST", body: "{}" }),
    onSuccess: (result) => window.alert(`临时密码（仅显示一次）：${result.temporary_password}`)
  });
  const purge = useMutation({
    mutationFn: () => apiRequest(`/admin/users/${userId}?confirm_email=${encodeURIComponent(confirmEmail)}`, { method: "DELETE" }),
    onSuccess: () => window.alert("用户删除已进入异步清理队列")
  });
  const actionError = update.error || resetPassword.error || purge.error;
  useEffect(() => {
    if (data) {
      setEmail(data.user.email);
      setFullName(data.user.full_name || "");
      setUsername(data.user.username || "");
      setPhone(data.user.phone || "");
      setHistoryEnabled(data.user.ai_history_enabled);
    }
  }, [data]);

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <h1>{data?.user.email || "用户"}</h1>
          <p>{data?.user.full_name || data?.user.id || ""}</p>
        </div>
      </div>
      {query.error ? <div className="error-banner">{query.error.message}</div> : null}
      {actionError ? <div className="error-banner">{actionError.message}</div> : null}
      {data ? (
        <div className="detail-grid">
          <section className="panel">
            <h2>账号</h2>
            <dl className="kv">
              <dt>ID</dt>
              <dd className="mono">{data.user.id}</dd>
              <dt>状态</dt>
              <dd>{data.user.is_active ? "active" : "inactive"}</dd>
              <dt>创建时间</dt>
              <dd>{formatDate(data.user.created_at)}</dd>
            </dl>
          </section>
          <section className="panel form-panel">
            <h2>账号操作</h2>
            <label>邮箱<input value={email} onChange={(event) => setEmail(event.target.value)}/></label><label>姓名<input value={fullName} onChange={(event) => setFullName(event.target.value)}/></label><label>用户名<input value={username} onChange={(event) => setUsername(event.target.value)}/></label><label>手机号<input value={phone} onChange={(event) => setPhone(event.target.value)}/></label><label className="checkbox-label"><input type="checkbox" checked={historyEnabled} onChange={(event) => setHistoryEnabled(event.target.checked)}/>AI 历史参与上下文</label>
            <div className="button-row"><button className="secondary-button" onClick={() => update.mutate({ email, full_name: fullName || null, username: username || null, phone: phone || null, ai_history_enabled: historyEnabled })}><Save size={15}/>保存</button><button className="secondary-button" onClick={() => update.mutate({ is_active: !data.user.is_active })}><UserX size={15}/>{data.user.is_active ? "禁用" : "启用"}</button><button className="secondary-button" onClick={() => resetPassword.mutate()}><KeyRound size={15}/>重置密码</button></div>
            <label>输入邮箱确认物理删除<input value={confirmEmail} onChange={(event) => setConfirmEmail(event.target.value)}/></label>
            <button className="secondary-button danger-button" disabled={confirmEmail !== data.user.email} onClick={() => window.confirm("永久删除该用户及全部数据？此操作不可恢复。") && purge.mutate()}><Trash2 size={15}/>永久删除</button>
          </section>
          <section className="panel">
            <h2>Workspace</h2>
            <dl className="kv">
              <dt>ID</dt>
              <dd className="mono">{data.workspace?.id || "-"}</dd>
              <dt>名称</dt>
              <dd>{data.workspace?.name || "-"}</dd>
              <dt>类型</dt>
              <dd>{data.workspace?.type || "-"}</dd>
            </dl>
          </section>
          <section className="panel">
            <h2>计数</h2>
            <StatusMap values={data.counts} />
          </section>
          <section className="panel">
            <h2>文档状态</h2>
            <StatusMap values={data.document_status_counts} />
          </section>
          <section className="panel">
            <h2>任务状态</h2>
            <StatusMap values={data.task_status_counts} />
          </section>
        </div>
      ) : null}
    </section>
  );
}

function StatusMap({ values }: { values: Record<string, number> }) {
  const entries = Object.entries(values);
  if (!entries.length) return <p className="empty">无数据</p>;
  return (
    <div className="status-map">
      {entries.map(([key, value]) => (
        <div key={key}>
          <span>{key}</span>
          <strong>{value}</strong>
        </div>
      ))}
    </div>
  );
}
