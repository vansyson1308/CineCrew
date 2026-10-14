// CineCrew Studio UI — one storyboard shot: keyframe, line, staging, job chips with actions, locks.
import { useRef, useState } from "react";
import { api, Attempt, Episode, fmtTime, fmtUsd, JobDetail, JobView, mediaUrl, Shot } from "./api";
import { ErrorBox, Notice, STATE_LABEL, useAction, VERDICT_LABEL } from "./common";

const JOB_LABEL: Record<string, string> = { kf: "Keyframe", clip: "Clip", tts: "Giọng thoại", narr: "Lời dẫn" };
const CANCELLABLE = ["pending", "ready", "running", "blocked", "waiting"];

export function ShotCard({ shot, episodeId, onChanged, onEdit }: { shot: Shot; episodeId: string; onChanged: (e?: Episode) => void; onEdit: (s: Shot) => void }) {
  const kf = shot.jobs.kf;
  return (
    <article className={`card shot-card ${shot.locked ? "is-locked" : ""}`} id={`shot-${shot.shot_id}`} aria-labelledby={`h-${shot.shot_id}`} data-testid={`shot-${shot.index}`}>
      <h3 id={`h-${shot.shot_id}`}>
        Shot {shot.index} <span className="muted">· {shot.shot_id}</span> {shot.locked && <span className="tag">đã khóa</span>}
      </h3>
      <div className="keyframe">
        {kf?.media_sha ? (
          <img src={mediaUrl(kf.media_sha)} alt={`Keyframe shot ${shot.index}: ${shot.action}`} loading="lazy" />
        ) : (
          <div className="placeholder" aria-label="Chưa có keyframe">
            {kf ? STATE_LABEL[kf.state] ?? kf.state : "chưa có keyframe"}
          </div>
        )}
      </div>
      <p className="line">
        {shot.dialogue ? (
          <>
            <strong>{shot.speaker ?? "?"}:</strong> “{shot.dialogue}”
          </>
        ) : (
          <span className="muted">(không thoại)</span>
        )}
      </p>
      {shot.narration && <p className="small">Lời dẫn: {shot.narration}</p>}
      <p className="small">{shot.action}</p>
      <dl className="meta">
        <dt>Thời lượng</dt>
        <dd>{shot.duration}s</dd>
        <dt>Ánh sáng</dt>
        <dd data-testid="lighting">{shot.lighting}</dd>
        <dt>Máy quay</dt>
        <dd>{[shot.camera.shot_scale, shot.camera.angle, shot.camera.movement].filter(Boolean).join(" · ")}</dd>
      </dl>
      <div className="jobs">
        {(["kf", "tts", "narr", "clip"] as const).map((k) =>
          shot.jobs[k] ? <JobChip key={k} label={JOB_LABEL[k]} shot={shot} job={shot.jobs[k]!} onChanged={onChanged} /> : null,
        )}
      </div>
      <LockToggles shot={shot} episodeId={episodeId} onChanged={onChanged} />
      <button type="button" onClick={() => onEdit(shot)}>
        Sửa shot này bằng lời…
      </button>
    </article>
  );
}

function LockToggles({ shot, episodeId, onChanged }: { shot: Shot; episodeId: string; onChanged: (e?: Episode) => void }) {
  const act = useAction();
  const toggle = async (what: "shot" | "keyframe" | "voice", locked: boolean) => {
    const r = await act.run(() => api<Episode>(`/api/episodes/${episodeId}/shots/${shot.shot_id}/lock`, { body: { what, locked } }));
    if (r) onChanged(r);
  };
  return (
    <fieldset className="locks">
      <legend>Khóa</legend>
      <label className="inline">
        <input type="checkbox" checked={shot.locked} disabled={act.busy} onChange={(e) => toggle("shot", e.target.checked)} /> shot
      </label>
      <label className="inline">
        <input type="checkbox" checked={shot.keyframe_locked} disabled={act.busy} onChange={(e) => toggle("keyframe", e.target.checked)} /> keyframe
      </label>
      <label className="inline">
        <input type="checkbox" checked={shot.voice_locked} disabled={act.busy} onChange={(e) => toggle("voice", e.target.checked)} /> giọng
      </label>
      <ErrorBox error={act.error} title="Không đổi được khóa" />
    </fieldset>
  );
}

export function JobChip({ label, shot, job, onChanged }: { label: string; shot: Shot; job: JobView; onChanged: (e?: Episode) => void }) {
  const act = useAction();
  const [open, setOpen] = useState(false);
  const [remoteId, setRemoteId] = useState("");
  const after = async (p: Promise<unknown>, ok: string) => {
    const r = await act.run(() => p, ok);
    if (r) onChanged();
  };
  const post = (path: string, body: unknown = {}) => api(`/api/jobs/${job.id}/${path}`, { body });
  const unknownSubmission = job.state === "needs_review" && ["unknown", "timed_out"].includes(job.provider_state);
  const isAudio = job.kind === "tts";

  return (
    <section
      className={`job-chip state-${job.state}`}
      aria-label={`${label} shot ${shot.index}`}
      data-testid={`job-${job.key}`}
      data-state={job.state}
      data-verdict={job.verdict}
      data-attempts={job.attempts}
    >
      <header>
        <strong>{label}</strong>
        <span className={`pill pill-${job.state}`}>{STATE_LABEL[job.state] ?? job.state}</span>
        <span className={`pill verdict-${job.verdict}`}>{VERDICT_LABEL[job.verdict] ?? job.verdict}</span>
        {job.cache_hit && <span className="tag" title="Lấy lại từ bộ nhớ đệm, không render lại">cache</span>}
        {job.fixture && <span className="tag fixture" title="Dữ liệu mẫu OFFLINE FIXTURE">fixture</span>}
        {job.locked && <span className="tag">khóa</span>}
      </header>
      <p className="small muted">
        lượt {job.attempts}/{job.max_attempts} · {fmtUsd(job.cost_usd)} · {job.provider_state}
        {job.verdict_source ? ` · nguồn: ${job.verdict_source}` : ""}
      </p>
      {job.error && <p className="err-text small">{job.error}</p>}
      {job.media_sha && job.kind === "video" && (
        <video controls preload="none" poster={mediaUrl(shot.jobs.kf?.media_sha)} src={mediaUrl(job.media_sha)} aria-label={`Clip shot ${shot.index}`} />
      )}
      {job.media_sha && isAudio && <audio controls preload="none" src={mediaUrl(job.media_sha)} aria-label={`${label} shot ${shot.index}`} />}
      <div className="row wrap">
        <button type="button" onClick={() => after(post("retry"), "Đã yêu cầu chạy lại.")} disabled={act.busy}>
          Thử lại
        </button>
        {CANCELLABLE.includes(job.state) && (
          <button type="button" onClick={() => after(post("cancel"), "Đã hủy.")} disabled={act.busy}>
            Hủy
          </button>
        )}
        {job.media_sha && job.verdict !== "accepted" && (
          <button type="button" onClick={() => after(post("verdict", { verdict: "accepted", note: "" }), "Đã chấp nhận.")} disabled={act.busy}>
            Chấp nhận
          </button>
        )}
        {job.media_sha && (
          <button type="button" onClick={() => after(post("verdict", { verdict: "rejected", note: "đạo diễn từ chối" }), "Đã từ chối; sẽ render bản mới.")} disabled={act.busy}>
            Từ chối
          </button>
        )}
        <button type="button" onClick={() => setOpen(true)} aria-expanded={open}>
          Xem các phiên bản
        </button>
      </div>
      {unknownSubmission && (
        <div className="resolve">
          <p className="small">
            Không rõ nhà cung cấp đã nhận lệnh hay chưa. <strong>Đối soát</strong> bằng mã job từ bảng điều khiển của nhà cung
            cấp, hoặc gửi lại (có thể bị tính tiền 2 lần).
          </p>
          <label>
            Mã job phía nhà cung cấp (remote id)
            <input value={remoteId} onChange={(e) => setRemoteId(e.target.value)} />
          </label>
          <div className="row">
            <button type="button" onClick={() => after(post("resolve", { action: "reconcile", remote_id: remoteId || null }), "Đang đối soát.")} disabled={act.busy}>
              Đối soát
            </button>
            <button type="button" className="danger" onClick={() => after(post("resolve", { action: "resubmit" }), "Đã gửi lại.")} disabled={act.busy}>
              Gửi lại
            </button>
          </div>
        </div>
      )}
      <Notice text={act.message} />
      <ErrorBox error={act.error} />
      {open && <VersionsDialog job={job} label={`${label} shot ${shot.index}`} onClose={() => setOpen(false)} onChanged={onChanged} />}
    </section>
  );
}

function VersionsDialog({ job, label, onClose, onChanged }: { job: JobView; label: string; onClose: () => void; onChanged: (e?: Episode) => void }) {
  const ref = useRef<HTMLDialogElement | null>(null);
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const load = useAction();
  const act = useAction();
  const reload = () => load.run(() => api<JobDetail>(`/api/jobs/${job.id}`)).then((d) => d && setDetail(d));

  const setRef = (el: HTMLDialogElement | null) => {
    ref.current = el;
    if (el && !el.open) {
      el.showModal();
      reload();
    }
  };
  const pick = async (a: Attempt) => {
    const r = await act.run(() => api(`/api/jobs/${job.id}/select/${a.id}`, { body: {} }), `Đã chọn lượt ${a.n}.`);
    if (r) {
      onChanged();
      reload();
    }
  };
  const gen = detail?.attempts.filter((a) => a.purpose === "generate") ?? [];

  return (
    <dialog ref={setRef} className="wide" aria-labelledby={`vd-${job.id}`} onClose={onClose}>
      <h3 id={`vd-${job.id}`}>Các phiên bản — {label}</h3>
      {load.busy && !detail && <p aria-busy="true">Đang tải…</p>}
      <ErrorBox error={load.error} />
      {detail && gen.length === 0 && <p className="empty">Chưa có lượt render nào.</p>}
      <ol className="attempts">
        {gen.map((a) => {
          const selected = detail?.media_sha && a.media_sha === detail.media_sha;
          return (
            <li key={a.id} className={`card ${selected ? "selected" : ""}`}>
              <p>
                <strong>Lượt {a.n}</strong> · {fmtTime(a.created_at)} · {a.provider}/{a.model} · {a.provider_state} ·{" "}
                {VERDICT_LABEL[a.verdict ?? ""] ?? a.verdict ?? "-"} · {fmtUsd(a.cost_actual_usd ?? a.cost_estimate_usd)}
                {selected && <span className="tag">đang dùng</span>}
              </p>
              {a.media_sha && job.kind === "image" && <img src={mediaUrl(a.media_sha)} alt={`Lượt ${a.n}`} className="attempt-media" />}
              {a.media_sha && job.kind === "video" && <video controls preload="none" src={mediaUrl(a.media_sha)} className="attempt-media" />}
              {a.media_sha && job.kind === "tts" && <audio controls preload="none" src={mediaUrl(a.media_sha)} />}
              {a.error && <p className="err-text small">{a.error}</p>}
              <details>
                <summary>Prompt và đánh giá</summary>
                <pre className="pre">{String(a.request?.params?.prompt ?? a.request?.params?.text ?? JSON.stringify(a.request?.params ?? {}, null, 1))}</pre>
                {a.review && <pre className="pre">{JSON.stringify(a.review, null, 1)}</pre>}
              </details>
              {a.media_sha && !selected && (
                <button type="button" onClick={() => pick(a)} disabled={act.busy}>
                  Chọn bản này
                </button>
              )}
            </li>
          );
        })}
      </ol>
      <Notice text={act.message} />
      <ErrorBox error={act.error} />
      <button type="button" onClick={() => ref.current?.close()}>
        Đóng
      </button>
    </dialog>
  );
}
