import { MessageSquare, Trash2 } from "lucide-react";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiRequest, type ChatMessage, type Conversation } from "../lib/api";
import { StatusBadge } from "../components/StatusBadge";
import { formatDate } from "../lib/format";

export function ConversationsPage() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const conversations = useQuery({ queryKey: ["conversations"], queryFn: () => apiRequest<Conversation[]>("/admin/conversations") });
  const messages = useQuery({
    queryKey: ["conversation-messages", selected],
    queryFn: () => apiRequest<ChatMessage[]>(`/admin/conversations/${selected}/messages`),
    enabled: Boolean(selected)
  });
  const remove = useMutation({
    mutationFn: (id: string) => apiRequest(`/admin/conversations/${id}`, { method: "DELETE" }),
    onSuccess: () => { setSelected(null); queryClient.invalidateQueries({ queryKey: ["conversations"] }); }
  });
  return <section className="page">
    <div className="page-header"><div><h1>AI 会话</h1><p>只允许查看与软删除，不提供管理员代发消息</p></div></div>
    {remove.error ? <div className="error-banner">{remove.error.message}</div> : null}
    <div className="split-view">
      <section className="panel"><table><thead><tr><th>会话</th><th>Workspace</th><th>更新时间</th></tr></thead><tbody>{(conversations.data || []).map((item) => <tr key={item.id} className={selected === item.id ? "selected-row" : ""} onClick={() => setSelected(item.id)}><td><MessageSquare size={14}/> {item.title}</td><td className="mono">{item.workspace_id}</td><td>{formatDate(item.updated_at)}</td></tr>)}</tbody></table></section>
      <section className="panel conversation-panel"><div className="panel-header"><h2>消息</h2>{selected ? <button className="tiny-button danger-button" onClick={() => window.confirm("软删除该会话？") && remove.mutate(selected)}><Trash2 size={14}/>删除会话</button> : null}</div>
        {!selected ? <p className="empty">选择会话查看消息</p> : <div className="message-list">{(messages.data || []).map((message) => <article key={message.id} className={`message message-${message.role}`}><div><strong>{message.role}</strong><StatusBadge value={message.status}/><span>{formatDate(message.created_at)}</span></div><p>{message.content || message.error_message || "-"}</p></article>)}</div>}
      </section>
    </div>
  </section>;
}
