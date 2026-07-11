import { FormEvent, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { login } from "../lib/api";

export function LoginPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const mutation = useMutation({
    mutationFn: () => login(email, password),
    onSuccess: (me) => {
      queryClient.setQueryData(["admin-me"], me);
      navigate("/", { replace: true });
    }
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    mutation.mutate();
  }

  return (
    <main className="login-screen">
      <form className="login-panel" onSubmit={onSubmit}>
        <div className="login-title">
          <ShieldCheck size={28} />
          <div>
            <h1>NotePatch Admin</h1>
            <span>运维管理后台</span>
          </div>
        </div>
        <label>
          邮箱
          <input value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" />
        </label>
        <label>
          密码
          <input
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            type="password"
            autoComplete="current-password"
          />
        </label>
        {mutation.error ? <div className="error-banner">{mutation.error.message}</div> : null}
        <button className="primary-button" disabled={mutation.isPending || !email || !password}>
          登录
        </button>
      </form>
    </main>
  );
}
