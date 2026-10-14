// CineCrew Studio UI — episode workspace: readiness, timeline, storyboard, edits, versions, export, live log.
import { useEffect, useRef, useState } from "react";
import { api, BUSY_STATES, Episode, fmtTime, ProjectDetail, Shot, StudioEvent } from "./api";
import { ErrorBox, ModeBadge, Notice, ReadinessBadge, useAction, usePoll } from "./common";
import { ShotCard } from "./ShotCard";
import { EditPanel, EditPanelHandle } from "./EditPanel";
import { ExportPanel } from "./ExportPanel";

interface VersionRow {
  version: number;
  note: string;
  patch: unknown;
  created_at: number;
}

const FIELD_HINT = "ánh sáng";

export default function EpisodePage({ id }: { id: string }) {
  const [ep, setEp] = useState<Episode | null>(null);
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [events, setEvents] = useState<StudioEvent[]>([]);
  const [versionsKey, setVersionsKey] = useState(0);
  const lastEvent = useRef(0);
  const edit = useRef<EditPanelHandle>(null);

  const load = async () => {
    try {
      const e = await api<Episode>(`/api/episodes/${id}`);
      setEp(e);
      setLoadError(null);
      if (!project || project.id !== e.project_id) setProject(await api<ProjectDetail>(`/api/projects/${e.project_id}`));
      loadEvents(e.project_id);
    } catch (err) {
      setLoadError(err);
    }
  };
  const loadEvents = async (pid: string) => {
    try {
      const rows = await api<StudioEvent[]>(`/api/projects/${pid}/events?after=${lastEvent.current}`);
      if (rows.length) {
        lastEvent.current = Math.max(lastEvent.current, ...rows.map((r) => r.id));
        setEvents((old) => [...rows, ...old].slice(0, 300));
      }
    } catch {
      /* the episode load reports connectivity */
    }
  };
  useEffect(() => {
    load();
  }, [id]);

  const allJobs = ep ? [...ep.shots.flatMap((s) => Object.values(s.jobs)), ...ep.references] : [];
  const busy = allJobs.some((j) => j && BUSY_STATES.includes(j.state));
  usePoll(load, busy ? 1500 : 5000, !!ep);

  const onChanged = (e?: Episode) => {
    if (e) setEp(e);
    load();
  };

  if (!ep)
    return (
      <div className="page">
        <p>
          <a href="#/">← Dự án</a>
        </p>
        {loadError ? <ErrorBox error={loadError} title="Không tải được tập phim" /> : <p aria-busy="true">Đang tải tập phim…</p>}
      </div>
    );

  const kfs = ep.shots.map((s) => s.jobs.kf).filter(Boolean);
  const kfAccepted = kfs.filter((j) => j!.state === "done" && ["accepted", "unreviewed"].includes(j!.verdict)).length;
  const total = ep.shots.reduce((a, s) => a + s.duration, 0);
  const counts = Object.entries(ep.counts)
    .map(([k, v]) => `${k}: ${v}`)
    .join(" · ");
  const epEvents = events.filter((e) => !e.episode_id || e.episode_id === ep.id || e.episode_id === ep.project_id);

  return (
    <div className="page">
      <nav aria-label="Đường dẫn" className="crumbs">
        <a href="#/">Dự án</a> / <a href={`#/p/${ep.project_id}`}>{project?.name ?? ep.project_id}</a> / <span>Tập {ep.number}</span>
      </nav>
      <div className="title-row">
        <h1>
          Tập {ep.number}: {ep.title} <span className="muted small">v{ep.version}</span>
        </h1>
        {project && <ModeBadge mode={project.mode} />}
      </div>
      <ErrorBox error={loadError} title="Mất kết nối khi làm mới" />

      <section className={`panel readiness-panel readiness-bg-${ep.readiness.status}`} aria-labelledby="ready-h">
        <h2 id="ready-h">
          Trạng thái xuất: <ReadinessBadge status={ep.readiness.status} />
        </h2>
        <p data-testid="readiness-reason">{ep.readiness.reason}</p>
        {ep.readiness.contains_fixture_media && <p className="warn-text small">Chứa media OFFLINE FIXTURE (dữ liệu mẫu, không phải AI thật).</p>}
        <p className="small muted" aria-live="polite" data-testid="job-counts">
          {busy ? "Đang render… " : "Không có job nào đang chạy. "}
          {counts}
        </p>
      </section>

      <ApprovePanel ep={ep} kfAccepted={kfAccepted} kfTotal={kfs.length} onChanged={onChanged} />

      <section aria-labelledby="tl-h">
        <h2 id="tl-h">Dòng thời gian ({total.toFixed(1)}s)</h2>
        <ol className="timeline" aria-label="Thứ tự shot">
          {ep.shots.map((s) => (
            <li key={s.shot_id} style={{ flexGrow: s.duration }} className={`tl-${s.jobs.clip?.state ?? "none"}`}>
              <a href={`#/e/${ep.id}`} onClick={(e) => {
                e.preventDefault();
                document.getElementById(`shot-${s.shot_id}`)?.scrollIntoView({ behavior: "smooth" });
              }}>
                <span>#{s.index}</span> <span className="small">{s.duration}s</span>
              </a>
            </li>
          ))}
        </ol>
      </section>

      <section aria-labelledby="sb-h">
        <h2 id="sb-h">Storyboard</h2>
        {ep.shots.length === 0 ? (
          <p className="empty">Tập này chưa có shot nào.</p>
        ) : (
          <div className="storyboard">
            {ep.shots.map((s) => (
              <ShotCard key={s.shot_id} shot={s} episodeId={ep.id} onChanged={onChanged} onEdit={(sh: Shot) => edit.current?.prefill(`Đổi ${FIELD_HINT} cảnh ${sh.index} thành `)} />
            ))}
          </div>
        )}
      </section>

      <EditPanel
        ref={edit}
        episodeId={ep.id}
        onApplied={() => {
          setVersionsKey((k) => k + 1);
          load();
        }}
      />
      <VersionsPanel episodeId={ep.id} current={ep.version} refreshKey={versionsKey} onChanged={() => { setVersionsKey((k) => k + 1); load(); }} />
      <ExportPanel ep={ep} defaultBurn={project?.settings?.subtitles?.burn_in ?? true} />

      <section className="panel" aria-labelledby="log-h">
        <h2 id="log-h">Nhật ký tiến độ</h2>
        <ol className="event-log" aria-live="polite" aria-relevant="additions" data-testid="event-log">
          {epEvents.length === 0 && <li className="muted">Chưa có sự kiện.</li>}
          {epEvents.slice(0, 120).map((e) => (
            <li key={e.id} className={`ev-${e.level}`}>
              <time>{fmtTime(e.created_at)}</time> <span className="lvl">{e.level}</span> {e.message}
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}

function ApprovePanel({ ep, kfAccepted, kfTotal, onChanged }: { ep: Episode; kfAccepted: number; kfTotal: number; onChanged: (e?: Episode) => void }) {
  const act = useAction();
  if (ep.storyboard_approved)
    return (
      <section className="panel" aria-labelledby="sb-ok-h">
        <h2 id="sb-ok-h">Storyboard đã duyệt</h2>
        <p className="muted">Clip được render từ các keyframe đã duyệt.</p>
        <Notice text={act.message} />
      </section>
    );
  const ready = kfTotal > 0 && kfAccepted === kfTotal;
  const approve = async () => {
    const r = await act.run(() => api<Episode>(`/api/episodes/${ep.id}/approve-storyboard`, { body: {} }), "Đã duyệt storyboard — clip bắt đầu render.");
    if (r) onChanged(r);
  };
  return (
    <section className="panel approve-panel" aria-labelledby="approve-h">
      <h2 id="approve-h">Duyệt storyboard</h2>
      <p>
        Clip video (tốn kém nhất) <strong>chờ bạn duyệt storyboard</strong>. Hãy xem keyframe, giọng và lời thoại bên dưới,
        sửa nếu cần, rồi duyệt. Keyframe đã chấp nhận: <strong data-testid="kf-accepted">{kfAccepted}/{kfTotal}</strong>.
      </p>
      {ready ? (
        <button type="button" className="primary" onClick={approve} disabled={act.busy}>
          Duyệt storyboard
        </button>
      ) : (
        <p className="muted">Nút duyệt sẽ hiện khi mọi keyframe đã được chấp nhận.</p>
      )}
      <Notice text={act.message} />
      <ErrorBox error={act.error} title="Chưa duyệt được" />
    </section>
  );
}

function VersionsPanel({ episodeId, current, refreshKey, onChanged }: { episodeId: string; current: number; refreshKey: number; onChanged: () => void }) {
  const [rows, setRows] = useState<VersionRow[] | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const act = useAction();
  useEffect(() => {
    api<VersionRow[]>(`/api/episodes/${episodeId}/versions`).then(setRows).catch(setErr);
  }, [episodeId, refreshKey, current]);
  const rollback = async (v: number) => {
    const r = await act.run(() => api(`/api/episodes/${episodeId}/rollback/${v}`, { body: {} }), `Đã khôi phục v${v} thành một phiên bản mới; media cũ lấy lại từ bộ nhớ đệm.`);
    if (r) onChanged();
  };
  return (
    <section className="panel" aria-labelledby="ver-h">
      <h2 id="ver-h">Phiên bản</h2>
      <ErrorBox error={err} title="Không tải được lịch sử" />
      {rows && rows.length === 0 && <p className="empty">Chưa có phiên bản.</p>}
      {rows && rows.length > 0 && (
        <ol className="versions">
          {[...rows].reverse().map((r) => (
            <li key={r.version}>
              <strong>v{r.version}</strong> · {fmtTime(r.created_at)} · {r.note}
              {r.version === current ? (
                <span className="tag">hiện tại</span>
              ) : (
                <button type="button" onClick={() => rollback(r.version)} disabled={act.busy}>
                  Khôi phục v{r.version}
                </button>
              )}
            </li>
          ))}
        </ol>
      )}
      <Notice text={act.message} />
      <ErrorBox error={act.error} title="Khôi phục thất bại" />
    </section>
  );
}
