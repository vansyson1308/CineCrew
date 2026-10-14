// CineCrew Studio UI — projects list + create from template.
import { FormEvent, useEffect, useState } from "react";
import { api, fmtUsd, ProjectRow, Template } from "./api";
import { ErrorBox, ModeBadge, useAction } from "./common";

export default function ProjectsPage() {
  const [projects, setProjects] = useState<ProjectRow[] | null>(null);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [templateId, setTemplateId] = useState("hanoi_delivery");
  const [name, setName] = useState("");
  const [mode, setMode] = useState<"offline_fixture" | "live">("offline_fixture");
  const [budget, setBudget] = useState("0");
  const [liveOk, setLiveOk] = useState(false);
  const create = useAction();

  const load = () => {
    setLoadError(null);
    Promise.all([api<ProjectRow[]>("/api/projects"), api<Template[]>("/api/templates")])
      .then(([p, t]) => {
        setProjects(p);
        setTemplates(t);
        if (t.length && !t.some((x) => x.template_id === templateId)) setTemplateId(t[0].template_id);
      })
      .catch(setLoadError);
  };
  useEffect(load, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const p = await create.run(() =>
      api<ProjectRow>("/api/projects", {
        body: {
          template_id: templateId || null,
          name: name.trim() || null,
          mode,
          budget_limit_usd: Number(budget) || 0,
        },
      }),
    );
    if (p) window.location.hash = `#/p/${p.id}`;
  };

  const tpl = templates.find((t) => t.template_id === templateId);
  const liveBlocked = mode === "live" && (!liveOk || !(Number(budget) > 0));

  return (
    <div className="page">
      <h1>Dự án</h1>
      <ErrorBox error={loadError} title="Không tải được danh sách dự án" />
      {loadError ? (
        <button type="button" onClick={load}>
          Thử tải lại
        </button>
      ) : null}

      <section className="panel" aria-labelledby="create-h">
        <h2 id="create-h">Tạo dự án</h2>
        <form onSubmit={submit} className="form-grid">
          <label>
            Mẫu kịch bản
            <select value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
              {templates.map((t) => (
                <option key={t.template_id} value={t.template_id}>
                  {t.title} ({t.template_id})
                </option>
              ))}
            </select>
          </label>
          {tpl && (
            <p className="muted span-2">
              {tpl.kind} · khung {tpl.aspect} ·{" "}
              {tpl.episodes.map((e) => `Tập ${e.number}: ${e.title} (${e.shots} shot)`).join(" · ")}
            </p>
          )}
          <label>
            Tên dự án (tùy chọn)
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder={tpl?.title} />
          </label>
          <fieldset className="span-2">
            <legend>Chế độ chạy</legend>
            <label className="inline">
              <input type="radio" name="mode" checked={mode === "offline_fixture"} onChange={() => setMode("offline_fixture")} />
              <ModeBadge mode="offline_fixture" /> — ảnh/clip/giọng mẫu tạo bằng ffmpeg, miễn phí
            </label>
            <label className="inline">
              <input type="radio" name="mode" checked={mode === "live"} onChange={() => setMode("live")} />
              <ModeBadge mode="live" /> — gọi nhà cung cấp thật, tốn tiền
            </label>
          </fieldset>
          {mode === "live" && (
            <div className="live-warning span-2">
              <label>
                Ngân sách tối đa (USD, bắt buộc &gt; 0)
                <input type="number" min="0" step="0.5" value={budget} onChange={(e) => setBudget(e.target.value)} />
              </label>
              <label className="inline">
                <input type="checkbox" checked={liveOk} onChange={(e) => setLiveOk(e.target.checked)} />
                Tôi hiểu chế độ LIVE sẽ gọi API trả phí trong giới hạn ngân sách này.
              </label>
            </div>
          )}
          <div className="span-2">
            <button type="submit" className="primary" disabled={create.busy || liveBlocked || !templates.length}>
              {create.busy ? "Đang tạo…" : "Tạo dự án"}
            </button>
          </div>
        </form>
        <ErrorBox error={create.error} title="Tạo dự án thất bại" />
      </section>

      <section aria-labelledby="list-h">
        <h2 id="list-h">Danh sách</h2>
        {projects === null && !loadError && <p aria-busy="true">Đang tải…</p>}
        {projects && projects.length === 0 && (
          <p className="empty">Chưa có dự án nào. Hãy tạo dự án đầu tiên từ một mẫu ở trên.</p>
        )}
        {projects && projects.length > 0 && (
          <ul className="card-list">
            {projects.map((p) => (
              <li key={p.id} className="card">
                <h3>
                  <a href={`#/p/${p.id}`}>{p.name}</a>
                </h3>
                <ModeBadge mode={p.mode} />
                <p className="muted">
                  {p.id} · {p.episodes ?? 0} tập · đã chi {fmtUsd(p.budget?.committed_usd ?? 0)} / giới hạn{" "}
                  {fmtUsd(p.budget?.limit_usd ?? p.budget_limit_usd)}
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
