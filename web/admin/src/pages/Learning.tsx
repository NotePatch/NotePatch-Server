import { BookOpen, Download, Highlighter, Layers3, RefreshCw, Save, Trash2 } from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { HtmlNoteEditor, HtmlNotePreview } from "../components/HtmlNoteEditor";

import {
  apiRequest,
  type DownloadUrl,
  type FlashcardDeck,
  type FlashcardDeckDetail,
  type KnowledgeChunk,
  type LearningUnit,
  type StudyNote
} from "../lib/api";
import { formatDate } from "../lib/format";

export function LearningPage() {
  const units = useQuery({
    queryKey: ["learning-units"],
    queryFn: () => apiRequest<LearningUnit[]>("/admin/learning-units")
  });
  return (
    <section className="page">
      <div className="page-header">
        <div><h1>学习单元与笔记</h1><p>知识块、电子笔记版本和下游生成任务</p></div>
      </div>
      {units.error ? <div className="error-banner">{units.error.message}</div> : null}
      <section className="panel">
        <table><thead><tr><th>学习单元</th><th>Workspace</th><th>学科</th><th>年级</th><th>更新时间</th></tr></thead>
          <tbody>{(units.data || []).map((unit) => <tr key={unit.id}>
            <td><Link to={`/learning/${unit.id}`}>{unit.title}</Link><div className="subtext">{unit.topic || "-"}</div></td>
            <td className="mono">{unit.workspace_id}</td><td>{unit.subject || "-"}</td><td>{unit.grade_level || "-"}</td><td>{formatDate(unit.updated_at)}</td>
          </tr>)}</tbody>
        </table>
      </section>
    </section>
  );
}

type NoteContent = { id: string; title: string; html: string; version_no: number };

export function LearningDetailPage() {
  const { learningUnitId = "" } = useParams();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<"edit" | "preview">("edit");
  const [title, setTitle] = useState("");
  const [html, setHtml] = useState("");
  const [editSummary, setEditSummary] = useState("");
  const [unitForm, setUnitForm] = useState({ title: "", subject: "", grade_level: "", topic: "" });
  const units = useQuery({ queryKey: ["learning-units"], queryFn: () => apiRequest<LearningUnit[]>("/admin/learning-units") });
  const unit = useMemo(() => units.data?.find((item) => item.id === learningUnitId), [units.data, learningUnitId]);
  const notes = useQuery({
    queryKey: ["study-notes", learningUnitId],
    queryFn: () => apiRequest<StudyNote[]>(`/admin/learning-units/${learningUnitId}/notes`),
    enabled: Boolean(learningUnitId)
  });
  const chunks = useQuery({
    queryKey: ["knowledge-chunks", learningUnitId],
    queryFn: () => apiRequest<KnowledgeChunk[]>(`/admin/knowledge-chunks?learning_unit_id=${learningUnitId}`),
    enabled: Boolean(learningUnitId)
  });
  const decks = useQuery({
    queryKey: ["flashcard-decks", learningUnitId],
    queryFn: () => apiRequest<FlashcardDeck[]>(`/admin/learning-units/${learningUnitId}/flashcard-decks`),
    enabled: Boolean(learningUnitId)
  });
  const latestDeck = decks.data?.[0];
  const deckDetail = useQuery({
    queryKey: ["flashcard-deck", latestDeck?.id],
    queryFn: () => apiRequest<FlashcardDeckDetail>(`/admin/flashcard-decks/${latestDeck?.id}`),
    enabled: Boolean(latestDeck?.id)
  });
  const latest = notes.data?.[0];
  const content = useQuery({
    queryKey: ["study-note-content", latest?.id],
    queryFn: () => apiRequest<NoteContent>(`/admin/notes/${latest?.id}/content`),
    enabled: Boolean(latest?.id)
  });
  useEffect(() => {
    if (content.data) { setTitle(content.data.title); setHtml(content.data.html); }
  }, [content.data]);
  useEffect(() => {
    if (unit) setUnitForm({ title: unit.title, subject: unit.subject || "", grade_level: unit.grade_level || "", topic: unit.topic || "" });
  }, [unit]);

  const revise = useMutation({
    mutationFn: () => apiRequest(`/admin/learning-units/${learningUnitId}/notes/${latest?.id}/revisions`, {
      method: "POST", body: JSON.stringify({ title, html, edit_summary: editSummary || null })
    }),
    onSuccess: async () => {
      setEditSummary("");
      await queryClient.invalidateQueries({ queryKey: ["study-notes", learningUnitId] });
    }
  });
  const regenerate = useMutation({
    mutationFn: () => apiRequest(`/admin/learning-units/${learningUnitId}/notes/regenerate`, { method: "POST", body: "{}" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["tasks"] })
  });
  const generateFlashcards = useMutation({
    mutationFn: () => apiRequest(`/admin/learning-units/${learningUnitId}/flashcards`, { method: "POST", body: "{}" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["flashcard-decks", learningUnitId] })
  });
  const highlight = useMutation({ mutationFn: () => apiRequest(`/admin/learning-units/${learningUnitId}/highlight`, { method: "POST", body: "{}" }) });
  const removeNote = useMutation({
    mutationFn: (id: string) => apiRequest(`/admin/notes/${id}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["study-notes", learningUnitId] })
  });
  const updateUnit = useMutation({
    mutationFn: () => apiRequest(`/admin/learning-units/${learningUnitId}`, { method: "PATCH", body: JSON.stringify(unitForm) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["learning-units"] })
  });
  const removeUnit = useMutation({
    mutationFn: () => apiRequest(`/admin/learning-units/${learningUnitId}`, { method: "DELETE" }),
    onSuccess: () => window.location.assign("/learning")
  });
  const download = useMutation({
    mutationFn: (kind: string) => apiRequest<DownloadUrl>(`/admin/notes/${latest?.id}/download-url?kind=${kind}`),
    onSuccess: (result) => window.open(result.download_url, "_blank", "noopener,noreferrer")
  });
  function submit(event: FormEvent) { event.preventDefault(); revise.mutate(); }
  const error = revise.error || regenerate.error || generateFlashcards.error || highlight.error || removeNote.error || updateUnit.error || removeUnit.error || download.error;

  return <section className="page">
    <div className="page-header"><div><h1>{unit?.title || "学习单元"}</h1><p>{learningUnitId}</p></div><div className="button-row">
      <button className="secondary-button" onClick={() => regenerate.mutate()}><RefreshCw size={16}/>重新生成</button><button className="secondary-button" onClick={() => generateFlashcards.mutate()}><Layers3 size={16}/>生成闪卡</button><button className="secondary-button" onClick={() => highlight.mutate()}><Highlighter size={16}/>错题高亮</button></div>
    </div>
    {error ? <div className="error-banner">{error.message}</div> : null}
    <div className="detail-grid">
      <section className="panel wide form-panel"><h2>学习单元信息</h2><div className="inline-form"><label>标题<input value={unitForm.title} onChange={(e) => setUnitForm({ ...unitForm, title: e.target.value })}/></label><label>学科<input value={unitForm.subject} onChange={(e) => setUnitForm({ ...unitForm, subject: e.target.value })}/></label><label>年级<input value={unitForm.grade_level} onChange={(e) => setUnitForm({ ...unitForm, grade_level: e.target.value })}/></label><label>主题<input value={unitForm.topic} onChange={(e) => setUnitForm({ ...unitForm, topic: e.target.value })}/></label></div><div className="button-row"><button className="primary-button" onClick={() => updateUnit.mutate()}><Save size={16}/>保存单元</button><button className="secondary-button danger-button" onClick={() => window.confirm("删除学习单元及其派生笔记/知识块？原始文档会保留。") && removeUnit.mutate()}><Trash2 size={16}/>删除单元</button></div></section>
      <section className="panel wide">
        <div className="panel-header"><h2>最新笔记</h2>{latest ? <div className="button-row">
          <button className="tiny-button" onClick={() => download.mutate("html")}><Download size={14}/>HTML</button>
          <button className="tiny-button danger-button" onClick={() => window.confirm("删除该笔记版本？") && removeNote.mutate(latest.id)}><Trash2 size={14}/>删除版本</button>
        </div> : null}</div>
        {latest ? <form className="note-editor" onSubmit={submit}>
          <div className="segmented"><button type="button" className={tab === "edit" ? "active" : ""} onClick={() => setTab("edit")}>编辑</button><button type="button" className={tab === "preview" ? "active" : ""} onClick={() => setTab("preview")}>预览</button></div>
          {tab === "edit" ? <><label>标题<input value={title} onChange={(e) => setTitle(e.target.value)}/></label><HtmlNoteEditor value={html} onChange={setHtml}/><label>修改摘要<input value={editSummary} onChange={(e) => setEditSummary(e.target.value)}/></label></> : <HtmlNotePreview html={html}/>} 
          <div className="form-actions"><span>保存后创建 v{latest.version_no + 1}</span><button className="primary-button" disabled={revise.isPending || !html.trim()}><Save size={16}/>保存新版本</button></div>
        </form> : <div className="empty-state"><BookOpen size={28}/><p>暂无笔记，可在知识块就绪后重新生成。</p></div>}
      </section>
      <section className="panel wide"><h2>版本历史</h2><table><thead><tr><th>版本</th><th>标题</th><th>来源</th><th>高亮</th><th>创建时间</th></tr></thead><tbody>{(notes.data || []).map((note) => <tr key={note.id}><td>v{note.version_no}</td><td>{note.title}</td><td>{note.edit_origin || "skill"}</td><td>{note.highlighted_html_object_key ? "是" : "否"}</td><td>{formatDate(note.created_at)}</td></tr>)}</tbody></table></section>
      <section className="panel wide"><h2>最新闪卡组</h2>{deckDetail.data ? <table><thead><tr><th>排序</th><th>正面</th><th>背面</th><th>权重</th></tr></thead><tbody>{deckDetail.data.cards.map((card) => <tr key={card.id}><td>{card.rank}</td><td>{card.front}</td><td>{card.back}</td><td>{card.priority_score.toFixed(2)}</td></tr>)}</tbody></table> : <div className="empty-state"><Layers3 size={28}/><p>暂无持久化闪卡组。</p></div>}</section>
      <section className="panel wide"><h2>知识块</h2><table><thead><tr><th>内容</th><th>类型</th><th>文档</th><th>创建时间</th></tr></thead><tbody>{(chunks.data || []).map((chunk) => <tr key={chunk.id}><td>{chunk.content}</td><td>{chunk.source_type || "-"}</td><td className="mono">{chunk.document_id || "-"}</td><td>{formatDate(chunk.created_at)}</td></tr>)}</tbody></table></section>
    </div>
  </section>;
}
