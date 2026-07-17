import { Search, Trash2 } from "lucide-react";
import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiRequest, type KnowledgeChunk } from "../lib/api";
import { formatDate } from "../lib/format";

type SearchResult = KnowledgeChunk & { score: number };

function scoreText(item: KnowledgeChunk | SearchResult): string {
  const score = (item as Partial<SearchResult>).score;
  return typeof score === "number" ? score.toFixed(3) : "-";
}

export function KnowledgePage() {
  const queryClient = useQueryClient();
  const [workspaceId, setWorkspaceId] = useState("");
  const [searchText, setSearchText] = useState("");
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const chunks = useQuery({ queryKey: ["knowledge-chunks-all"], queryFn: () => apiRequest<KnowledgeChunk[]>("/admin/knowledge-chunks") });
  const search = useMutation({
    mutationFn: () => apiRequest<{ items: SearchResult[] }>("/admin/knowledge/search", { method: "POST", body: JSON.stringify({ workspace_id: workspaceId, query: searchText, limit: 20 }) }),
    onSuccess: (payload) => setResults(payload.items)
  });
  const remove = useMutation({
    mutationFn: (id: string) => apiRequest(`/admin/knowledge-chunks/${id}`, { method: "DELETE" }),
    onSuccess: () => { setResults(null); queryClient.invalidateQueries({ queryKey: ["knowledge-chunks-all"] }); }
  });
  function submit(event: FormEvent) { event.preventDefault(); search.mutate(); }
  const items = results || chunks.data || [];
  return <section className="page"><div className="page-header"><div><h1>知识库</h1><p>跨 workspace 查看、语义检索和清理知识块</p></div></div>
    <form className="filters" onSubmit={submit}><label>Workspace ID<input value={workspaceId} onChange={(e) => setWorkspaceId(e.target.value)}/></label><label>检索内容<input value={searchText} onChange={(e) => setSearchText(e.target.value)}/></label><button className="secondary-button" disabled={!workspaceId || !searchText}><Search size={16}/>语义检索</button>{results ? <button type="button" className="tiny-button" onClick={() => setResults(null)}>清除结果</button> : null}</form>
    {search.error || remove.error ? <div className="error-banner">{(search.error || remove.error)?.message}</div> : null}
    <section className="panel"><table><thead><tr><th>内容</th><th>Workspace</th><th>来源</th><th>相似度</th><th>创建时间</th><th></th></tr></thead><tbody>{items.map((item) => <tr key={item.id}><td>{item.content}</td><td className="mono">{item.workspace_id}</td><td>{item.source_type || "-"}</td><td>{scoreText(item)}</td><td>{formatDate(item.created_at)}</td><td><button className="tiny-button danger-button" onClick={() => window.confirm("删除该知识块？") && remove.mutate(item.id)}><Trash2 size={14}/></button></td></tr>)}</tbody></table></section>
  </section>;
}
