"use client";

import { useMutation } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, Suspense, useState } from "react";
import { Notice } from "@/components/ui";
import { api, ApiError, unwrap } from "@/lib/api";
import { setTokens } from "@/lib/session";

const DEMO = { email: "buyer@example.com", password: "buyer-demo-password" };

function LoginForm() {
  const router = useRouter();
  const next = useSearchParams().get("next");
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [form, setForm] = useState({ email: "", password: "", display_name: "" });

  const submit = useMutation({
    mutationFn: async (values: typeof form) => {
      if (mode === "signup") {
        unwrap(await api.POST("/auth/signup", { body: values }));
      }
      const { email, password } = values;
      return unwrap(await api.POST("/auth/login", { body: { email, password } }));
    },
    onSuccess: (tokens) => {
      setTokens(tokens);
      // Only ever send people to a path on this site.
      router.replace(next?.startsWith("/") && !next.startsWith("//") ? next : "/");
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    submit.mutate(form);
  };
  const field = (name: keyof typeof form) => ({
    value: form[name],
    onChange: (event: React.ChangeEvent<HTMLInputElement>) =>
      setForm({ ...form, [name]: event.target.value }),
  });
  const fieldErrors =
    submit.error instanceof ApiError
      ? ((submit.error.extra.errors as { field: string; message: string }[] | undefined) ?? [])
      : [];

  return (
    <div className="mx-auto max-w-md pt-8">
      <h1 className="font-display text-3xl font-bold">
        {mode === "signin" ? "Sign in" : "Create an account"}
      </h1>
      <p className="mt-2 text-sm text-muted">You need an account to hold seats and buy tickets.</p>

      <form onSubmit={onSubmit} className="panel mt-6 space-y-4 p-5">
        {mode === "signup" && (
          <label className="block space-y-1.5">
            <span className="label">Display name</span>
            <input className="input" required maxLength={50} autoComplete="nickname" {...field("display_name")} />
          </label>
        )}
        <label className="block space-y-1.5">
          <span className="label">Email</span>
          <input className="input" type="email" required autoComplete="email" {...field("email")} />
        </label>
        <label className="block space-y-1.5">
          <span className="label">Password</span>
          <input
            className="input"
            type="password"
            required
            minLength={mode === "signup" ? 10 : 1}
            autoComplete={mode === "signup" ? "new-password" : "current-password"}
            {...field("password")}
          />
          {mode === "signup" && <span className="text-xs text-muted">At least 10 characters.</span>}
        </label>

        {submit.isError && (
          <Notice tone="error">
            {submit.error.message}
            {fieldErrors.map((error) => (
              <span key={error.field} className="block">
                {error.field}: {error.message}
              </span>
            ))}
          </Notice>
        )}

        <button type="submit" className="btn btn-primary w-full" disabled={submit.isPending}>
          {submit.isPending ? "One moment" : mode === "signin" ? "Sign in" : "Create account"}
        </button>
      </form>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-sm">
        <button
          type="button"
          className="text-cyan hover:underline"
          onClick={() => {
            submit.reset();
            setMode(mode === "signin" ? "signup" : "signin");
          }}
        >
          {mode === "signin" ? "New here? Create an account" : "Have an account? Sign in"}
        </button>
        <button
          type="button"
          className="btn btn-ghost !py-1.5 text-sm"
          disabled={submit.isPending}
          onClick={() => {
            setMode("signin");
            setForm({ ...DEMO, display_name: "" });
            submit.mutate({ ...DEMO, display_name: "" });
          }}
        >
          Use the demo buyer
        </button>
      </div>
      <p className="mt-3 text-xs text-muted">
        The demo buyer exists after running <code className="font-mono">lobbyd-seed</code>.
      </p>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
