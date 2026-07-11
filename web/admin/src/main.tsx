import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { Layout } from "./components/Layout";
import { apiRequest, getTokens, type AdminMe } from "./lib/api";
import { DocumentDetailPage, DocumentsPage } from "./pages/Documents";
import { LoginPage } from "./pages/Login";
import { OverviewPage } from "./pages/Overview";
import { SystemPage } from "./pages/System";
import { TaskDetailPage, TasksPage } from "./pages/Tasks";
import { UserDetailPage, UsersPage } from "./pages/Users";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 5000
    }
  }
});

function RequireAdmin() {
  const hasTokens = Boolean(getTokens()?.access_token);
  const me = useQuery({
    queryKey: ["admin-me"],
    queryFn: () => apiRequest<AdminMe>("/admin/me"),
    enabled: hasTokens,
    retry: false
  });

  if (!hasTokens) return <Navigate to="/login" replace />;
  if (me.isLoading) return <div className="boot">Loading</div>;
  if (me.error) return <Navigate to="/login" replace />;
  return <Layout />;
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<RequireAdmin />}>
            <Route path="/" element={<OverviewPage />} />
            <Route path="/users" element={<UsersPage />} />
            <Route path="/users/:userId" element={<UserDetailPage />} />
            <Route path="/documents" element={<DocumentsPage />} />
            <Route path="/documents/:documentId" element={<DocumentDetailPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
            <Route path="/system" element={<SystemPage />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
