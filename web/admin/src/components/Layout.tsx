import { Activity, BookOpen, Brain, CircleAlert, FileText, GraduationCap, ListChecks, LogOut, MessageSquare, Server, ShieldCheck, Users } from "lucide-react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiRequest, logout, type AdminMe } from "../lib/api";

const navItems = [
  { to: "/", label: "总览", icon: Activity },
  { to: "/users", label: "用户", icon: Users },
  { to: "/documents", label: "文档", icon: FileText },
  { to: "/learning", label: "学习与笔记", icon: BookOpen },
  { to: "/knowledge", label: "知识库", icon: Brain },
  { to: "/homeworks", label: "作业", icon: GraduationCap },
  { to: "/mistakes", label: "错题", icon: CircleAlert },
  { to: "/conversations", label: "会话", icon: MessageSquare },
  { to: "/tasks", label: "任务", icon: ListChecks },
  { to: "/operations", label: "操作审计", icon: ShieldCheck },
  { to: "/system", label: "系统", icon: Server }
];

export function Layout() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const me = useQuery({
    queryKey: ["admin-me"],
    queryFn: () => apiRequest<AdminMe>("/admin/me")
  });
  const logoutMutation = useMutation({
    mutationFn: logout,
    onSettled: () => {
      queryClient.clear();
      navigate("/login", { replace: true });
    }
  });

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">NP</span>
          <div>
            <strong>NotePatch</strong>
            <small>Admin</small>
          </div>
        </div>
        <nav className="nav">
          {navItems.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.to === "/"}>
              <item.icon size={18} />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="main">
        <header className="topbar">
          <div>
            <strong>{me.data?.user.email || "Admin"}</strong>
            <span>{me.data?.user.full_name || "运维后台"}</span>
          </div>
          <button className="icon-button" onClick={() => logoutMutation.mutate()} title="退出登录">
            <LogOut size={18} />
            <span>退出</span>
          </button>
        </header>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
