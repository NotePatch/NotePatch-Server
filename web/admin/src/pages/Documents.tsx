import { Download, Play, Search, Trash2 } from "lucide-react";
import { FormEvent, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Pagination } from "../components/Pagination";
import { StatusBadge } from "../components/StatusBadge";
import {
  apiRequest,
  buildQuery,
  type AdminArtifact,
  type AdminDocument,
  type AdminDocumentDetail,
  type DownloadUrl,
  type Page
} from "../lib/api";
import { compactId, formatBytes, formatDate, jsonText } from "../lib/format";

const PAGE_SIZE = 25;

export function DocumentsPage() {
  const [params, setParams] = useSearchParams();
  const page = Number(params.get("page") || "1");
  const search = params.get("search") || "";
  const status = params.get("status") || "";
  const fileType = params.get("file_type") || "";
  const documentKind = params.get("document_kind") || "";
  const [filters, setFilters] = useState({ search, status, file_type: fileType, document_kind: documentKind });
  const query = useQuery({
    queryKey: ["documents", page, search, status, fileType, documentKind],
    queryFn: () =>
      apiRequest<Page<AdminDocument>>(
        `/admin/documents${buildQuery({
          page,
          page_size: PAGE_SIZE,
          search,
          status,
          file_type: fileType,
          document_kind: documentKind
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
          <h1>文档</h1>
          <p>上传状态、OCR 产物和存储路径</p>
        </div>
      </div>
      <form className="filters" onSubmit={submit}>
        <label>
          搜索
          <input
            value={filters.search}
            onChange={(event) => setFilters({ ...filters, search: event.target.value })}
            placeholder="filename / title"
          />
        </label>
        <label>
          状态
          <select value={filters.status} onChange={(event) => setFilters({ ...filters, status: event.target.value })}>
            <option value="">全部</option>
            <option value="created">created</option>
            <option value="uploaded">uploaded</option>
            <option value="processing">processing</option>
            <option value="ready">ready</option>
            <option value="failed">failed</option>
            <option value="deleted">deleted</option>
          </select>
        </label>
        <label>
          类型
          <select
            value={filters.file_type}
            onChange={(event) => setFilters({ ...filters, file_type: event.target.value })}
          >
            <option value="">全部</option>
            <option value="image">image</option>
            <option value="pdf">pdf</option>
            <option value="docx">docx</option>
            <option value="pptx">pptx</option>
            <option value="other">other</option>
          </select>
        </label>
        <label>
          分类
          <select
            value={filters.document_kind}
            onChange={(event) => setFilters({ ...filters, document_kind: event.target.value })}
          >
            <option value="">全部</option>
            <option value="homework">homework</option>
            <option value="corrected_homework">corrected_homework</option>
            <option value="courseware">courseware</option>
            <option value="note">note</option>
            <option value="exam">exam</option>
            <option value="other">other</option>
          </select>
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
              <th>文件</th>
              <th>用户</th>
              <th>类型</th>
              <th>状态</th>
              <th>大小</th>
              <th>Artifacts</th>
              <th>更新时间</th>
            </tr>
          </thead>
          <tbody>
            {(query.data?.items || []).map((document) => (
              <tr key={document.id}>
                <td>
                  <Link to={`/documents/${document.id}`}>{document.title || document.original_filename}</Link>
                  <div className="subtext">{document.original_filename}</div>
                </td>
                <td>{document.uploaded_by_email || compactId(document.uploaded_by)}</td>
                <td>{document.file_type} / {document.document_kind}</td>
                <td>
                  <StatusBadge value={document.status} />
                </td>
                <td>{formatBytes(document.file_size)}</td>
                <td>{document.artifacts_count}</td>
                <td>{formatDate(document.updated_at)}</td>
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
              ...Object.fromEntries(Object.entries({ search, status, file_type: fileType, document_kind: documentKind }).filter(([, value]) => value))
            })
          }
        />
      </section>
    </section>
  );
}

export function DocumentDetailPage() {
  const { documentId } = useParams();
  const queryClient = useQueryClient();
  const detail = useQuery({
    queryKey: ["document", documentId],
    queryFn: () => apiRequest<AdminDocumentDetail>(`/admin/documents/${documentId}`)
  });
  const artifacts = useQuery({
    queryKey: ["document-artifacts", documentId],
    queryFn: () => apiRequest<AdminArtifact[]>(`/admin/documents/${documentId}/artifacts`),
    enabled: Boolean(documentId)
  });
  const download = useMutation({
    mutationFn: (path: string) => apiRequest<DownloadUrl>(path),
    onSuccess: (payload) => window.open(payload.download_url, "_blank", "noopener,noreferrer")
  });
  const process = useMutation({
    mutationFn: () => apiRequest(`/admin/documents/${documentId}/process`, { method: "POST", body: JSON.stringify({ force_reprocess: true }) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["document", documentId] })
  });
  const remove = useMutation({
    mutationFn: () => apiRequest(`/admin/documents/${documentId}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["document", documentId] })
  });
  const document = detail.data?.document;

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <h1>{document?.title || document?.original_filename || "文档"}</h1>
          <p>{document?.id || ""}</p>
        </div>
        {document ? (<div className="button-row">
          <button className="secondary-button" onClick={() => process.mutate()}><Play size={16}/>重新处理</button>
          <button
            className="secondary-button"
            onClick={() => download.mutate(`/admin/documents/${document.id}/download-url`)}
          >
            <Download size={16} />
            原文件
          </button>
          <button className="secondary-button danger-button" onClick={() => window.confirm("异步彻底清理该文档？") && remove.mutate()}><Trash2 size={16}/>删除</button>
        </div>) : null}
      </div>
      {detail.error ? <div className="error-banner">{detail.error.message}</div> : null}
      {download.error ? <div className="error-banner">{download.error.message}</div> : null}
      {process.error || remove.error ? <div className="error-banner">{(process.error || remove.error)?.message}</div> : null}
      {detail.data ? (
        <div className="detail-grid">
          <section className="panel wide">
            <h2>Metadata</h2>
            <dl className="kv">
              <dt>Workspace</dt>
              <dd className="mono">{detail.data.document.workspace_id}</dd>
              <dt>Object Key</dt>
              <dd className="mono">{detail.data.object_key}</dd>
              <dt>Bucket</dt>
              <dd>{detail.data.bucket}</dd>
              <dt>状态</dt>
              <dd><StatusBadge value={detail.data.document.status} /></dd>
              <dt>MIME</dt>
              <dd>{detail.data.document.mime_type || "-"}</dd>
            </dl>
          </section>
          <section className="panel wide">
            <h2>Artifacts</h2>
            <table>
              <thead>
                <tr>
                  <th>类型</th>
                  <th>MIME</th>
                  <th>大小</th>
                  <th>Object Key</th>
                  <th>创建时间</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {(artifacts.data || []).map((artifact) => (
                  <tr key={artifact.id}>
                    <td>{artifact.artifact_type}</td>
                    <td>{artifact.mime_type || "-"}</td>
                    <td>{formatBytes(artifact.file_size)}</td>
                    <td className="mono table-key">{artifact.object_key}</td>
                    <td>{formatDate(artifact.created_at)}</td>
                    <td>
                      <button
                        className="tiny-button"
                        onClick={() => download.mutate(`/admin/artifacts/${artifact.id}/download-url`)}
                      >
                        下载
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
          <section className="panel wide">
            <h2>JSON</h2>
            <pre>{jsonText(detail.data.metadata)}</pre>
          </section>
        </div>
      ) : null}
    </section>
  );
}
