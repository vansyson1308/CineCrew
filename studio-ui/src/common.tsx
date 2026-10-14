// CineCrew Studio UI — shared small components and hooks.
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

export const STATE_LABEL: Record<string, string> = {
  pending: "chờ xử lý",
  ready: "sẵn sàng chạy",
  running: "đang chạy",
  done: "xong",
  blocked: "bị chặn",
  waiting: "đang đợi",
  needs_review: "cần xem lại",
  failed: "lỗi",
  cancelled: "đã hủy",
  obsolete: "lỗi thời",
};

export const VERDICT_LABEL: Record<string, string> = {
  accepted: "đã chấp nhận",
  rejected: "bị từ chối",
  pending: "chưa đánh giá",
  unreviewed: "chưa duyệt (tự động qua)",
  review_error: "lỗi khi duyệt",
};

export const READINESS_LABEL: Record<string, string> = {
  final: "HOÀN CHỈNH",
  draft: "BẢN NHÁP",
  blocked: "CHƯA XUẤT ĐƯỢC",
};

export function ModeBadge({ mode }: { mode: string }) {
  const live = mode === "live";
  return (
    <span className={`mode-badge ${live ? "live" : "offline"}`} data-testid="mode-badge" data-mode={mode}>
      {live ? "LIVE (có chi phí)" : "OFFLINE FIXTURE (không gọi API trả phí)"}
    </span>
  );
}

export function ReadinessBadge({ status }: { status: string }) {
  return (
    <span className={`readiness readiness-${status}`} data-testid="readiness-badge">
      {READINESS_LABEL[status] ?? status}
    </span>
  );
}

export function ErrorBox({ error, title }: { error: unknown; title?: string }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="error-box" role="alert">
      {title && <strong>{title}: </strong>}
      {msg}
      {error instanceof ApiError && error.status > 0 && <span className="muted"> (HTTP {error.status})</span>}
    </div>
  );
}

export function Notice({ text, kind = "ok" }: { text: string | null; kind?: "ok" | "warn" }) {
  if (!text) return null;
  return (
    <p className={`notice notice-${kind}`} role="status">
      {text}
    </p>
  );
}

/** Runs an async action, tracks busy / error / result message for the caller to render. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [message, setMessage] = useState<string | null>(null);
  const run = useCallback(async <T,>(fn: () => Promise<T>, okMessage?: string | ((r: T) => string)): Promise<T | undefined> => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const r = await fn();
      if (okMessage) setMessage(typeof okMessage === "function" ? okMessage(r) : okMessage);
      return r;
    } catch (e) {
      setError(e);
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, message, run, setError, setMessage };
}

/** Calls `fn` every `ms` while `active`; also immediately when (re)activated. */
export function usePoll(fn: () => void, ms: number, active: boolean) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => ref.current(), ms);
    return () => window.clearInterval(t);
  }, [ms, active]);
}

export function fileToBase64(f: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] ?? "");
    r.onerror = () => reject(r.error);
    r.readAsDataURL(f);
  });
}
