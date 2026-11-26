"use client";
import { useEffect, useState } from "react";
import { clearToken, getStoredToken, decodeToken, storeToken, registerUser, loginUser, getCurrentUser } from "@/lib/api";

export default function Login() {
  const [mode, setMode] = useState<"login" | "register" | "token">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [orgName, setOrgName] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [who, setWho] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const t = getStoredToken();
    const info = t ? decodeToken(t) : null;
    if (info) setWho(`${info.sub} · ${info.role}`);
  }, []);

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const { token: newToken } = await loginUser(email, password);
      const info = storeToken(newToken);
      setWho(`${info.sub} · ${info.role}`);
      setEmail(""); setPassword("");
      window.location.assign("/conversation");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Error de login");
    } finally {
      setLoading(false);
    }
  }

  async function handleRegister(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const { token: newToken } = await registerUser(email, orgName, password);
      const info = storeToken(newToken);
      setWho(`${info.sub} · ${info.role}`);
      setEmail(""); setPassword(""); setOrgName("");
      window.location.assign("/conversation");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Error de registro");
    } finally {
      setLoading(false);
    }
  }

  function handleTokenSubmit(e: React.FormEvent) {
    e.preventDefault();
    try {
      const info = storeToken(token);
      setToken("");
      setError(null);
      setWho(`${info.sub} · ${info.role}`);
      window.location.assign("/conversation");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Token inválido");
    }
  }

  function handleLogout() {
    clearToken();
    setWho(null);
  }

  return (
    <main className="mx-auto max-w-xl space-y-4 p-6">
      <h1 className="text-2xl font-semibold">OmniVoice Agent</h1>
      <p className="text-sm text-slate-600">
        Inicia sesión, regístrate o pega tu token JWT.
      </p>

      {who && (
        <p className="rounded border p-3 text-sm bg-green-50 text-green-800" role="status">
          Sesión activa: <strong>{who}</strong>{" "}
          <button type="button" className="ml-2 underline" onClick={handleLogout}>Cerrar sesión</button>
        </p>
      )}

      <div className="flex gap-2 mb-4" role="tablist">
        <button role="tab" aria-selected={mode === "login"}
          onClick={() => setMode("login")}
          className={`px-3 py-1 rounded text-sm ${mode === "login" ? "bg-slate-900 text-white" : "bg-slate-100 text-slate-700"}`}>
          Login
        </button>
        <button role="tab" aria-selected={mode === "register"}
          onClick={() => setMode("register")}
          className={`px-3 py-1 rounded text-sm ${mode === "register" ? "bg-slate-900 text-white" : "bg-slate-100 text-slate-700"}`}>
          Registro
        </button>
        <button role="tab" aria-selected={mode === "token"}
          onClick={() => setMode("token")}
          className={`px-3 py-1 rounded text-sm ${mode === "token" ? "bg-slate-900 text-white" : "bg-slate-100 text-slate-700"}`}>
          Token JWT
        </button>
      </div>

      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}

      {mode === "login" && (
        <form onSubmit={handleLogin} className="space-y-3">
          <label htmlFor="email" className="block text-sm font-medium">Email</label>
          <input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)}
            className="w-full rounded border p-2" required autoComplete="email" />
          <label htmlFor="password" className="block text-sm font-medium">Contraseña</label>
          <input id="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded border p-2" required autoComplete="current-password" />
          <button type="submit" disabled={loading} className="rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-40">
            {loading ? "Entrando..." : "Entrar"}
          </button>
        </form>
      )}

      {mode === "register" && (
        <form onSubmit={handleRegister} className="space-y-3">
          <label htmlFor="orgName" className="block text-sm font-medium">Nombre de la organización</label>
          <input id="orgName" value={orgName} onChange={(e) => setOrgName(e.target.value)}
            className="w-full rounded border p-2" required autoComplete="organization" />
          <label htmlFor="email" className="block text-sm font-medium">Email</label>
          <input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)}
            className="w-full rounded border p-2" required autoComplete="email" />
          <label htmlFor="password" className="block text-sm font-medium">Contraseña (mín. 8 caracteres)</label>
          <input id="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded border p-2" required autoComplete="new-password" minLength={8} />
          <button type="submit" disabled={loading} className="rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-40">
            {loading ? "Registrando..." : "Registrarse"}
          </button>
        </form>
      )}

      {mode === "token" && (
        <form onSubmit={handleTokenSubmit} className="space-y-3">
          <label htmlFor="token" className="block text-sm font-medium">Token de acceso (JWT)</label>
          <textarea id="token" value={token} onChange={(e) => setToken(e.target.value)} rows={5} autoComplete="off" spellCheck={false}
            className="w-full rounded border p-2 font-mono text-xs" required />
          <button type="submit" className="rounded bg-slate-900 px-4 py-2 text-sm text-white">Entrar</button>
        </form>
      )}
    </main>
  );
}