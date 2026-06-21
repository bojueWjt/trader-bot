import { LogIn, ShieldCheck } from "lucide-react";
import type { FormEvent, ReactElement } from "react";
import { useState } from "react";
import { login, storeAuthToken } from "../utils/api";


type LoginPageProps = {
  onAuthenticated: () => void;
};


export function LoginPage({ onAuthenticated }: LoginPageProps): ReactElement {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [status, setStatus] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (submitting) {
      return;
    }

    setSubmitting(true);
    setStatus("");
    const result = await login(username.trim(), password);
    setSubmitting(false);

    if (!result) {
      setStatus("Invalid username or password");
      return;
    }

    storeAuthToken(result.token, result.role);
    onAuthenticated();
  }

  return (
    <main className="login-shell">
      <section className="login-panel" aria-label="Login">
        <div className="login-brand">
          <span className="brand-mark">HT</span>
          <div>
            <strong>Hermes Trader</strong>
            <span>Operations</span>
          </div>
        </div>
        <div className="login-heading">
          <ShieldCheck size={20} />
          <div>
            <p className="eyebrow">Secure Access</p>
            <h1>Sign in</h1>
          </div>
        </div>
        <form className="login-form" onSubmit={(event) => void submit(event)}>
          <label>
            <span>Username</span>
            <input
              autoComplete="username"
              name="username"
              onChange={(event) => {
                setUsername(event.target.value);
              }}
              required
              type="text"
              value={username}
            />
          </label>
          <label>
            <span>Password</span>
            <input
              autoComplete="current-password"
              name="password"
              onChange={(event) => {
                setPassword(event.target.value);
              }}
              required
              type="password"
              value={password}
            />
          </label>
          <button className="primary-button" disabled={submitting} type="submit">
            <LogIn size={16} />
            Sign in
          </button>
          {status && <p className="login-status" role="alert">{status}</p>}
        </form>
      </section>
    </main>
  );
}
