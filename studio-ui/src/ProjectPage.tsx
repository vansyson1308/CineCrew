// CineCrew Studio UI — project view: mode, budget, asset library, episodes.
import { FormEvent, useEffect, useRef, useState } from "react";
import { api, Asset, BudgetSummary, fmtTime, fmtUsd, mediaUrl, ProjectDetail, Template, BUSY_STATES } from "./api";
import { ErrorBox, fileToBase64, ModeBadge, Notice, ReadinessBadge, STATE_LABEL, useAction, usePoll, VERDICT_LABEL } from "./common";

interface LedgerEntry {
  id: string;
  kind: string;
  state: string;
  amount_usd: number;
  note?: string;
  cost_basis?: string;
  created_at: number;
}

export default function ProjectPage({ id }: { id: string }) {
  const [p, setP] = useState<ProjectDetail | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);

  const load = () =>
    api<ProjectDetail>(`/api/projects/${id}`)
      .then((x) => {
        setP(x);
        setLoadError(null);
      })
      .catch(setLoadError);
  useEffect(() => {
    load();
  }, [id]);
  const refsBusy = !!p?.assets.some((a) => a.reference_job && BUSY_STATES.includes(a.reference_job.state));
  usePoll(load, refsBusy ? 1500 : 6000, !!p);

  if (!p)
    return (
      <div className="page">
        <p>
          <a href="#/">← Dự án</a>
        </p>
        {loadError ? <ErrorBox error={loadError} title="Không tải được dự án" /> : <p aria-busy="true">Đang tải dự án…</p>}
      </div>
    );

  return (
    <div className="page">
      <nav aria-label="Đường dẫn" className="crumbs">
        <a href="#/">Dự án</a> / <span>{p.name}</span>
      </nav>
      <div className="title-row">
        <h1>{p.name}</h1>
        <ModeBadge mode={p.mode} />
      </div>
      <ErrorBox error={loadError} title="Lỗi làm mới" />
      <ModePanel p={p} onChange={setP} />
      <EpisodesPanel p={p} reload={load} />
      <BudgetPanel p={p} reload={load} />
      <AssetsPanel p={p} reload={load} />
    </div>
  );
}

function ModePanel({ p, onChange }: { p: ProjectDetail; onChange: (p: ProjectDetail) => void }) {
  const dlg = useRef<HTMLDialogElement>(null);
  const [budget, setBudget] = useState(String(p.budget_limit_usd || ""));
  const [ack, setAck] = useState(false);
  const act = useAction();
  const live = p.mode === "live";

  const toLive = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(
      () => api<ProjectDetail>(`/api/projects/${p.id}`, { method: "PATCH", body: { mode: "live", confirm_live: true, budget_limit_usd: Number(budget) } }),
      "Đã chuyển sang LIVE.",
    );
    if (r) {
      onChange(r);
      dlg.current?.close();
    }
  };
  const toOffline = async () => {
    const r = await act.run(
      () => api<ProjectDetail>(`/api/projects/${p.id}`, { method: "PATCH", body: { mode: "offline_fixture" } }),
      "Đã quay về OFFLINE FIXTURE.",
    );
    if (r) onChange(r);
  };

  return (
    <section className={`panel mode-panel ${live ? "is-live" : ""}`} aria-labelledby="mode-h">
      <h2 id="mode-h">Chế độ chạy</h2>
      {live ? (
        <p>
          Dự án đang ở chế độ <strong>LIVE</strong>: mọi lần render đều gọi nhà cung cấp thật và tính tiền vào ngân sách.
        </p>
      ) : (
        <p>
          Dự án đang ở chế độ <strong>OFFLINE FIXTURE</strong>: ảnh, clip và giọng là dữ liệu mẫu tạo cục bộ (ffmpeg), chỉ
          để kiểm tra quy trình. Không gọi API trả phí.
        </p>
      )}
      {live ? (
        <button type="button" onClick={toOffline} disabled={act.busy}>
          Quay về OFFLINE FIXTURE
        </button>
      ) : (
        <button type="button" className="danger" onClick={() => dlg.current?.showModal()}>
          Chuyển sang LIVE…
        </button>
      )}
      <Notice text={act.message} />
      <ErrorBox error={act.error} title="Đổi chế độ thất bại" />
      <dialog ref={dlg} aria-labelledby="live-dlg-h">
        <form onSubmit={toLive}>
          <h3 id="live-dlg-h">Chuyển sang LIVE (có chi phí)?</h3>
          <p>
            Ở chế độ LIVE, Studio gọi các API tạo ảnh / video / giọng nói thật và <strong>tốn tiền</strong>. Các job đang chờ
            sẽ được chạy lại bằng nhà cung cấp thật. Nhập ngân sách tối đa; Studio sẽ chặn mọi lệnh gọi vượt quá.
          </p>
          <label>
            Ngân sách tối đa (USD)
            <input type="number" min="0" step="0.5" required value={budget} onChange={(e) => setBudget(e.target.value)} />
          </label>
          <label className="inline">
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            Tôi xác nhận cho phép gọi API trả phí trong giới hạn này.
          </label>
          <ErrorBox error={act.error} />
          <div className="row">
            <button type="button" onClick={() => dlg.current?.close()}>
              Hủy
            </button>
            <button type="submit" className="danger" disabled={!ack || !(Number(budget) > 0) || act.busy}>
              Xác nhận chuyển sang LIVE
            </button>
          </div>
        </form>
      </dialog>
    </section>
  );
}

function BudgetPanel({ p, reload }: { p: ProjectDetail; reload: () => void }) {
  const [ledger, setLedger] = useState<{ summary: BudgetSummary; entries: LedgerEntry[] } | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [limit, setLimit] = useState(String(p.budget.limit_usd));
  const act = useAction();
  const load = () => api<{ summary: BudgetSummary; entries: LedgerEntry[] }>(`/api/projects/${p.id}/ledger`).then(setLedger).catch(setErr);
  useEffect(() => {
    load();
  }, [p]);
  const s = ledger?.summary ?? p.budget;

  const save = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(
      () => api(`/api/projects/${p.id}`, { method: "PATCH", body: { budget_limit_usd: Number(limit) } }),
      `Đã đặt giới hạn ${fmtUsd(Number(limit))}.`,
    );
    if (r) reload();
  };

  return (
    <section className="panel" aria-labelledby="budget-h">
      <h2 id="budget-h">Ngân sách</h2>
      <dl className="stats">
        <div>
          <dt>Giới hạn</dt>
          <dd>{fmtUsd(s.limit_usd)}</dd>
        </div>
        <div>
          <dt>Đã chi</dt>
          <dd>{fmtUsd(s.committed_usd)}</dd>
        </div>
        <div>
          <dt>Đang giữ chỗ</dt>
          <dd>{fmtUsd(s.reserved_usd)}</dd>
        </div>
        <div>
          <dt>Còn lại</dt>
          <dd>{fmtUsd(s.remaining_usd)}</dd>
        </div>
      </dl>
      <form onSubmit={save} className="row">
        <label>
          Giới hạn mới (USD)
          <input type="number" min="0" step="0.5" value={limit} onChange={(e) => setLimit(e.target.value)} />
        </label>
        <button type="submit" disabled={act.busy}>
          Lưu giới hạn
        </button>
      </form>
      <Notice text={act.message} />
      <ErrorBox error={act.error} title="Không đổi được giới hạn" />
      <ErrorBox error={err} title="Không tải được sổ chi phí" />
      <details>
        <summary>Sổ chi phí ({ledger?.entries.length ?? 0} dòng gần nhất)</summary>
        {ledger && ledger.entries.length === 0 && <p className="empty">Chưa có khoản chi nào.</p>}
        {ledger && ledger.entries.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Thời gian</th>
                  <th>Loại</th>
                  <th>Trạng thái</th>
                  <th>USD</th>
                  <th>Ghi chú</th>
                </tr>
              </thead>
              <tbody>
                {ledger.entries.slice(0, 50).map((e) => (
                  <tr key={e.id}>
                    <td>{fmtTime(e.created_at)}</td>
                    <td>{e.kind}</td>
                    <td>{e.state}</td>
                    <td>{fmtUsd(e.amount_usd)}</td>
                    <td>{e.note ?? e.cost_basis ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </details>
    </section>
  );
}

function EpisodesPanel({ p, reload }: { p: ProjectDetail; reload: () => void }) {
  const [tpl, setTpl] = useState<Template | null>(null);
  const [num, setNum] = useState("");
  const [title, setTitle] = useState("");
  const act = useAction();
  useEffect(() => {
    api<Template[]>("/api/templates")
      .then((ts) => {
        const t = ts.find((x) => x.template_id === p.settings.template_id) ?? null;
        setTpl(t);
        if (t?.episodes.length) setNum(String(t.episodes[t.episodes.length - 1].number));
      })
      .catch(() => setTpl(null));
  }, [p.settings.template_id]);

  const add = async (e: FormEvent) => {
    e.preventDefault();
    const n = Number(num);
    const r = await act.run(
      () => api<{ id: string; number: number }>(`/api/projects/${p.id}/episodes`, { body: { title: title.trim(), from_template_episode: n } }),
      (x) => `Đã thêm tập ${x.number}.`,
    );
    if (r) {
      setTitle("");
      reload();
    }
  };

  return (
    <section className="panel" aria-labelledby="eps-h">
      <h2 id="eps-h">Các tập</h2>
      {p.episodes.length === 0 ? (
        <p className="empty">Dự án chưa có tập nào.</p>
      ) : (
        <ul className="card-list">
          {p.episodes.map((e) => (
            <li key={e.id} className="card episode-card">
              <h3>
                <a href={`#/e/${e.id}`}>
                  Tập {e.number}: {e.title}
                </a>
              </h3>
              <ReadinessBadge status={e.readiness} />
              <p className="muted">
                phiên bản v{e.version} · storyboard {e.storyboard_approved ? "đã duyệt" : "chưa duyệt"}
              </p>
            </li>
          ))}
        </ul>
      )}
      {tpl ? (
        <form onSubmit={add} className="row">
          <label>
            Thêm tập từ mẫu
            <select value={num} onChange={(e) => setNum(e.target.value)}>
              {tpl.episodes.map((e) => (
                <option key={e.number} value={e.number}>
                  Tập mẫu {e.number}: {e.title}
                </option>
              ))}
            </select>
          </label>
          <label>
            Tiêu đề (để trống = giữ tiêu đề mẫu)
            <input value={title} onChange={(e) => setTitle(e.target.value)} />
          </label>
          <button type="submit" disabled={act.busy || !num}>
            Thêm tập
          </button>
        </form>
      ) : (
        <p className="muted">Dự án này không dựa trên mẫu; thêm tập bằng kịch bản (chế độ LIVE) qua API.</p>
      )}
      <Notice text={act.message} />
      <ErrorBox error={act.error} title="Không thêm được tập" />
    </section>
  );
}

function AssetsPanel({ p, reload }: { p: ProjectDetail; reload: () => void }) {
  const act = useAction();
  const [kind, setKind] = useState("character");
  const [name, setName] = useState("");
  const [assetId, setAssetId] = useState("");
  const [identity, setIdentity] = useState("");

  const add = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(
      () => api(`/api/projects/${p.id}/assets`, { body: { kind, name: name.trim(), asset_id: assetId.trim(), identity } }),
      `Đã thêm tài sản ${assetId}.`,
    );
    if (r) {
      setName("");
      setAssetId("");
      setIdentity("");
      reload();
    }
  };

  return (
    <section className="panel" aria-labelledby="assets-h">
      <h2 id="assets-h">Thư viện tài sản</h2>
      <p className="muted">
        Ảnh tham chiếu chuẩn giữ nhân vật, bối cảnh và đạo cụ nhất quán giữa các shot. Khóa tài sản để không ai sửa danh tính.
      </p>
      {p.assets.length === 0 ? (
        <p className="empty">Chưa có tài sản nào.</p>
      ) : (
        <ul className="asset-grid">
          {p.assets.map((a) => (
            <AssetCard key={a.id} a={a} reload={reload} />
          ))}
        </ul>
      )}
      <details>
        <summary>Thêm tài sản</summary>
        <form onSubmit={add} className="form-grid">
          <label>
            Loại
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="character">nhân vật (character)</option>
              <option value="location">bối cảnh (location)</option>
              <option value="prop">đạo cụ (prop)</option>
              <option value="logo">logo</option>
              <option value="music">nhạc (music)</option>
              <option value="voice">giọng (voice)</option>
            </select>
          </label>
          <label>
            Tên
            <input required value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label>
            Mã ổn định (asset_id)
            <input required pattern="[a-z][a-z0-9_]*" placeholder="vd. char_lan" value={assetId} onChange={(e) => setAssetId(e.target.value)} />
          </label>
          <label className="span-2">
            Mô tả danh tính / ngoại hình
            <textarea rows={2} value={identity} onChange={(e) => setIdentity(e.target.value)} />
          </label>
          <div className="span-2">
            <button type="submit" disabled={act.busy}>
              Thêm tài sản
            </button>
          </div>
        </form>
      </details>
      <Notice text={act.message} />
      <ErrorBox error={act.error} title="Không thêm được tài sản" />
    </section>
  );
}

function AssetCard({ a, reload }: { a: Asset; reload: () => void }) {
  const act = useAction();
  const fileRef = useRef<HTMLInputElement>(null);
  const locked = !!a.locked;
  const j = a.reference_job;
  const isAudio = a.kind === "music" || a.kind === "voice";

  const toggleLock = async () => {
    const r = await act.run(() => api(`/api/assets/${a.id}/lock`, { body: { locked: !locked, what: "shot" } }), locked ? "Đã mở khóa." : "Đã khóa.");
    if (r) reload();
  };
  const upload = async (e: FormEvent) => {
    e.preventDefault();
    const f = fileRef.current?.files?.[0];
    if (!f) {
      act.setError(new Error("Chưa chọn tệp."));
      return;
    }
    const r = await act.run(
      async () => api(`/api/assets/${a.id}/media`, { body: { filename: f.name, data_b64: await fileToBase64(f) } }),
      `Đã tải lên ${f.name}; các shot dùng tài sản này sẽ được render lại.`,
    );
    if (r) {
      if (fileRef.current) fileRef.current.value = "";
      reload();
    }
  };

  return (
    <li className="card asset-card" aria-label={`Tài sản ${a.name}`}>
      <div className="thumb">
        {a.preview_sha && !isAudio ? (
          <img src={mediaUrl(a.preview_sha)} alt={`Ảnh tham chiếu của ${a.name}`} loading="lazy" />
        ) : a.preview_sha && isAudio ? (
          <audio controls src={mediaUrl(a.preview_sha)} aria-label={`Âm thanh ${a.name}`} />
        ) : (
          <span className="muted">chưa có ảnh tham chiếu</span>
        )}
      </div>
      <h3>
        {a.name} {locked && <span className="tag">đã khóa</span>}
      </h3>
      <p className="muted">
        {a.kind} · <code>{a.data.asset_id}</code> · v{a.version}
        {a.canonical_media ? " · ảnh chuẩn do bạn cung cấp" : ""}
      </p>
      {a.data.identity && <p className="small">{a.data.identity}</p>}
      {j && (
        <p className="small">
          Bản tham chiếu: {STATE_LABEL[j.state] ?? j.state} · {VERDICT_LABEL[j.verdict] ?? j.verdict}
          {j.fixture && <span className="tag fixture">fixture</span>}
          {j.error && <span className="err-text"> — {j.error}</span>}
        </p>
      )}
      <div className="row">
        <button type="button" onClick={toggleLock} disabled={act.busy} aria-pressed={locked}>
          {locked ? "Mở khóa" : "Khóa"}
        </button>
      </div>
      <form onSubmit={upload} className="upload">
        <label>
          Tải ảnh/logo/nhạc chuẩn
          <input ref={fileRef} type="file" accept="image/*,audio/*" disabled={locked} />
        </label>
        <button type="submit" disabled={act.busy || locked}>
          Tải lên
        </button>
      </form>
      <Notice text={act.message} />
      <ErrorBox error={act.error} />
    </li>
  );
}
