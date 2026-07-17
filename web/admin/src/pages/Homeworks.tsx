import { Play, Plus, Save, Trash2 } from "lucide-react";
import { FormEvent, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiRequest, type Homework, type Mistake } from "../lib/api";
import { StatusBadge } from "../components/StatusBadge";
import { formatDate } from "../lib/format";

type Reference = { id: string; document_id: string; reference_type: string; created_at: string };

export function HomeworksPage() {
  const queryClient = useQueryClient();
  const [workspaceId, setWorkspaceId] = useState("");
  const [title, setTitle] = useState("");
  const homeworks = useQuery({ queryKey: ["homeworks"], queryFn: () => apiRequest<Homework[]>("/admin/homeworks") });
  const create = useMutation({
    mutationFn: () => apiRequest<Homework>("/admin/homeworks", { method: "POST", body: JSON.stringify({ workspace_id: workspaceId, title }) }),
    onSuccess: () => { setTitle(""); queryClient.invalidateQueries({ queryKey: ["homeworks"] }); }
  });
  function submit(event: FormEvent) { event.preventDefault(); create.mutate(); }
  return <section className="page">
    <div className="page-header"><div><h1>作业</h1><p>评分配置、依据、批改和删除</p></div></div>
    <form className="filters" onSubmit={submit}><label>Workspace ID<input value={workspaceId} onChange={(e) => setWorkspaceId(e.target.value)}/></label><label>标题<input value={title} onChange={(e) => setTitle(e.target.value)}/></label><button className="primary-button" disabled={!workspaceId || !title}><Plus size={16}/>创建作业</button></form>
    {create.error ? <div className="error-banner">{create.error.message}</div> : null}
    <section className="panel"><table><thead><tr><th>标题</th><th>Workspace</th><th>状态</th><th>满分</th><th>创建时间</th></tr></thead><tbody>{(homeworks.data || []).map((item) => <tr key={item.id}><td><Link to={`/homeworks/${item.id}`}>{item.title}</Link></td><td className="mono">{item.workspace_id}</td><td><StatusBadge value={item.status}/></td><td>{item.max_score}</td><td>{formatDate(item.created_at)}</td></tr>)}</tbody></table></section>
  </section>;
}

export function HomeworkDetailPage() {
  const { homeworkId = "" } = useParams();
  const queryClient = useQueryClient();
  const homework = useQuery({ queryKey: ["homework", homeworkId], queryFn: () => apiRequest<Homework>(`/admin/homeworks/${homeworkId}`) });
  const references = useQuery({ queryKey: ["homework-references", homeworkId], queryFn: () => apiRequest<Reference[]>(`/admin/homeworks/${homeworkId}/references`) });
  const [rubric, setRubric] = useState("");
  const [maxScore, setMaxScore] = useState("100");
  const [documentId, setDocumentId] = useState("");
  const [referenceType, setReferenceType] = useState("answer_key");
  const save = useMutation({
    mutationFn: () => apiRequest(`/admin/homeworks/${homeworkId}/grading-config`, { method: "PATCH", body: JSON.stringify({ rubric_text: rubric || null, max_score: Number(maxScore) }) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["homework", homeworkId] })
  });
  const grade = useMutation({ mutationFn: () => apiRequest(`/admin/homeworks/${homeworkId}/grade`, { method: "POST", body: "{}" }) });
  const remove = useMutation({ mutationFn: () => apiRequest(`/admin/homeworks/${homeworkId}`, { method: "DELETE" }) });
  const addReference = useMutation({
    mutationFn: () => apiRequest(`/admin/homeworks/${homeworkId}/references`, { method: "POST", body: JSON.stringify({ document_id: documentId, reference_type: referenceType }) }),
    onSuccess: () => { setDocumentId(""); queryClient.invalidateQueries({ queryKey: ["homework-references", homeworkId] }); }
  });
  const deleteReference = useMutation({
    mutationFn: (id: string) => apiRequest(`/admin/homeworks/${homeworkId}/references/${id}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["homework-references", homeworkId] })
  });
  const error = save.error || grade.error || remove.error || addReference.error || deleteReference.error;
  return <section className="page">
    <div className="page-header"><div><h1>{homework.data?.title || "作业"}</h1><p>{homeworkId}</p></div><div className="button-row"><button className="secondary-button" onClick={() => grade.mutate()}><Play size={16}/>批改</button><button className="secondary-button danger-button" onClick={() => window.confirm("删除作业及评分结果？") && remove.mutate()}><Trash2 size={16}/>删除</button></div></div>
    {error ? <div className="error-banner">{error.message}</div> : null}
    <section className="panel form-panel"><h2>评分配置</h2><label>Rubric<textarea value={rubric} placeholder={homework.data?.rubric_text || "评分标准"} onChange={(e) => setRubric(e.target.value)}/></label><label>满分<input type="number" value={maxScore} onChange={(e) => setMaxScore(e.target.value)}/></label><button className="primary-button" onClick={() => save.mutate()}><Save size={16}/>保存配置</button></section>
    <section className="panel"><h2>评分依据</h2><div className="inline-form"><input placeholder="Document ID" value={documentId} onChange={(e) => setDocumentId(e.target.value)}/><select value={referenceType} onChange={(e) => setReferenceType(e.target.value)}><option value="answer_key">answer_key</option><option value="rubric">rubric</option></select><button className="secondary-button" onClick={() => addReference.mutate()}><Plus size={16}/>添加</button></div><table><thead><tr><th>类型</th><th>Document</th><th>创建时间</th><th></th></tr></thead><tbody>{(references.data || []).map((item) => <tr key={item.id}><td>{item.reference_type}</td><td className="mono">{item.document_id}</td><td>{formatDate(item.created_at)}</td><td><button className="tiny-button" onClick={() => deleteReference.mutate(item.id)}>移除</button></td></tr>)}</tbody></table></section>
  </section>;
}

export function MistakesPage() {
  const queryClient = useQueryClient();
  const mistakes = useQuery({ queryKey: ["mistakes"], queryFn: () => apiRequest<Mistake[]>("/admin/mistakes") });
  const update = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) => apiRequest(`/admin/mistakes/${id}`, { method: "PATCH", body: JSON.stringify({ status }) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["mistakes"] })
  });
  return <section className="page"><div className="page-header"><div><h1>错题</h1><p>知识点、说明和复习状态</p></div></div>{update.error ? <div className="error-banner">{update.error.message}</div> : null}<section className="panel"><table><thead><tr><th>知识点</th><th>说明</th><th>Workspace</th><th>状态</th><th>创建时间</th></tr></thead><tbody>{(mistakes.data || []).map((item) => <tr key={item.id}><td>{item.knowledge_point || "-"}</td><td>{item.description}</td><td className="mono">{item.workspace_id}</td><td><select value={item.status} onChange={(e) => update.mutate({ id: item.id, status: e.target.value })}><option value="open">open</option><option value="resolved">resolved</option><option value="ignored">ignored</option></select></td><td>{formatDate(item.created_at)}</td></tr>)}</tbody></table></section></section>;
}
