// CineCrew Studio UI — "Sửa bằng lời": natural language -> typed patch -> impact/cost preview -> confirm -> apply.
import { FormEvent, forwardRef, useImperativeHandle, useRef, useState } from "react";
import { api, ApiError, fmtUsd } from "./api";
import { ErrorBox, Notice, useAction } from "./common";

interface ImpactItem {
  key: string;
  kind: string;
  reason: string;
  estimate_usd: number | null;
}
interface Preview {
  patch: { ops: { target: string; field: string; value: unknown; shot_id?: string | null; asset_id?: string | null }[]; resolver?: string; ambiguous?: boolean; question?: string | null };
  applicable: boolean;
  question?: string | null;
  lock_conflicts?: string[];
  impact?: { episode_id: string | null; episode: string; affected: ImpactItem[]; kept_cached: string[] }[];
  episodes_affected?: number;
  model_jobs_affected?: number;
  estimate_usd?: number;
  estimate_unknown_for?: string[];
  mode?: string;
  requires_confirmation?: boolean;
  warning?: string | null;
}

export interface EditPanelHandle {
  prefill: (text: string) => void;
}

const REASON: Record<string, string> = { changed: "đầu vào thay đổi", "depends on a changed input": "phụ thuộc vào thứ vừa đổi", "identity reference sheet": "ảnh tham chiếu danh tính" };

export const EditPanel = forwardRef<EditPanelHandle, { episodeId: string; onApplied: () => void }>(function EditPanel({ episodeId, onApplied }, ref) {
  const [text, setText] = useState("");
  const [pv, setPv] = useState<Preview | null>(null);
  const [confirm, setConfirm] = useState(false);
  const preview = useAction();
  const apply = useAction();
  const box = useRef<HTMLTextAreaElement>(null);
  const section = useRef<HTMLElement>(null);

  useImperativeHandle(ref, () => ({
    prefill: (t: string) => {
      setText(t);
      setPv(null);
      section.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      window.setTimeout(() => {
        box.current?.focus();
        box.current?.setSelectionRange(t.length, t.length);
      }, 50);
    },
  }));

  const doPreview = async (e: FormEvent) => {
    e.preventDefault();
    setPv(null);
    setConfirm(false);
    apply.setMessage(null);
    apply.setError(null);
    const r = await preview.run(() => api<Preview>(`/api/episodes/${episodeId}/edits/preview`, { body: { text } }));
    if (r) setPv(r);
  };
  const doApply = async () => {
    const r = await apply.run(
      () => api<{ version: number; sync: Record<string, { changed: string[]; added: string[] }> }>(`/api/episodes/${episodeId}/edits/apply`, { body: { text, confirm } }),
      (x) => {
        const changed = Object.values(x.sync ?? {}).flatMap((s) => [...(s.changed ?? []), ...(s.added ?? [])]);
        return `Đã áp dụng — phiên bản v${x.version}. Render lại: ${changed.length ? changed.join(", ") : "không có job nào"}.`;
      },
    );
    if (r) {
      setPv(null);
      onApplied();
    }
  };

  const question = pv && !pv.applicable ? pv.question ?? pv.patch.question : null;
  const allowApply = pv && pv.applicable && (!pv.requires_confirmation || confirm);

  return (
    <section className="panel" aria-labelledby="edit-h" ref={section}>
      <h2 id="edit-h">Sửa bằng lời</h2>
      <form onSubmit={doPreview}>
        <label htmlFor="edit-text">Mô tả thay đổi (ghi rõ cảnh, thuộc tính và giá trị mới sau “thành”)</label>
        <textarea
          id="edit-text"
          ref={box}
          rows={2}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setPv(null);
          }}
          placeholder="Đổi ánh sáng cảnh 3 thành ánh trăng xanh"
        />
        <button type="submit" disabled={preview.busy || !text.trim()}>
          {preview.busy ? "Đang phân tích…" : "Xem trước thay đổi"}
        </button>
      </form>
      <ErrorBox error={preview.error} title="Không xem trước được" />

      {question && (
        <div className="question" role="alert" data-testid="edit-question">
          <strong>Studio cần bạn nói rõ hơn:</strong> {question}
        </div>
      )}

      {pv && pv.patch.ops.length > 0 && (
        <div className="preview" data-testid="edit-preview">
          <h3>Bản vá (resolver: {pv.patch.resolver})</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Đối tượng</th>
                  <th>Thuộc tính</th>
                  <th>Giá trị mới</th>
                </tr>
              </thead>
              <tbody>
                {pv.patch.ops.map((o, i) => (
                  <tr key={i}>
                    <td>{o.target === "shot" ? o.shot_id : o.asset_id}</td>
                    <td>{o.field}</td>
                    <td>{typeof o.value === "string" ? o.value : JSON.stringify(o.value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {pv.warning && <p className="warn-text">{pv.warning}</p>}
          <p>
            Ảnh hưởng: <strong>{pv.model_jobs_affected}</strong> job ở <strong>{pv.episodes_affected}</strong> tập · Ước tính chi phí:{" "}
            <strong>{fmtUsd(pv.estimate_usd)}</strong> ({pv.mode === "live" ? "LIVE" : "OFFLINE FIXTURE, không tốn tiền"})
            {pv.estimate_unknown_for && pv.estimate_unknown_for.length > 0 && (
              <span className="warn-text"> · không rõ giá: {pv.estimate_unknown_for.join(", ")}</span>
            )}
          </p>
          {pv.impact?.map((imp) => (
            <div key={imp.episode} className="impact">
              <h4>{imp.episode}</h4>
              {imp.affected.length === 0 ? (
                <p className="muted">Không job nào phải render lại.</p>
              ) : (
                <ul data-testid="affected-jobs">
                  {imp.affected.map((a) => (
                    <li key={a.key}>
                      <code>{a.key}</code> ({a.kind}) — {REASON[a.reason] ?? a.reason} — {fmtUsd(a.estimate_usd)}
                    </li>
                  ))}
                </ul>
              )}
              <p className="muted">Giữ nguyên từ bộ nhớ đệm: {imp.kept_cached.length} job</p>
            </div>
          ))}
          {pv.lock_conflicts && pv.lock_conflicts.length > 0 && (
            <div className="error-box" role="alert">
              Xung đột khóa — không thể áp dụng cho tới khi mở khóa: {pv.lock_conflicts.join("; ")}
            </div>
          )}
          {pv.requires_confirmation && (
            <label className="inline confirm">
              <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
              Tôi đã xem tác động (thay đổi danh tính / nhiều tập) và đồng ý áp dụng.
            </label>
          )}
          <button type="button" className="primary" onClick={doApply} disabled={!allowApply || apply.busy}>
            {apply.busy ? "Đang áp dụng…" : "Áp dụng thay đổi"}
          </button>
        </div>
      )}
      <Notice text={apply.message} />
      <ErrorBox error={apply.error} title={apply.error instanceof ApiError && apply.error.status === 409 ? "Bị từ chối" : "Áp dụng thất bại"} />
    </section>
  );
});
