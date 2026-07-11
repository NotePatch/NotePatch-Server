import { Search } from "lucide-react";
import { FormEvent, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { Pagination } from "../components/Pagination";
import { apiRequest, buildQuery, type AdminUserDetail, type AdminUserListItem, type Page } from "../lib/api";
import { compactId, formatDate } from "../lib/format";

const PAGE_SIZE = 25;

export function UsersPage() {
  const [params, setParams] = useSearchParams();
  const page = Number(params.get("page") || "1");
  const search = params.get("search") || "";
  const [searchInput, setSearchInput] = useState(search);
  const query = useQuery({
    queryKey: ["users", page, search],
    queryFn: () =>
      apiRequest<Page<AdminUserListItem>>(
        `/admin/users${buildQuery({ page, page_size: PAGE_SIZE, search })}`
      )
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
  const query = useQuery({
    queryKey: ["user", userId],
    queryFn: () => apiRequest<AdminUserDetail>(`/admin/users/${userId}`)
  });
  const data = query.data;

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <h1>{data?.user.email || "用户"}</h1>
          <p>{data?.user.full_name || data?.user.id || ""}</p>
        </div>
      </div>
      {query.error ? <div className="error-banner">{query.error.message}</div> : null}
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
