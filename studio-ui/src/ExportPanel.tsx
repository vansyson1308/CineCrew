// CineCrew Studio UI — export (film + subtitles + manifest + inspection) and comic carousel.
import { useState } from "react";
import { api, ApiError, Episode, ExportView, fmtTime, fmtUsd, Readiness } from "./api";
import { ErrorBox, Notice, READINESS_LABEL, useAction } from "./common";

const FILE_LABEL: Record<string, string> = {
  film: "Phim (MP4)",
  srt: "Phụ đề SRT",
  ass: "Phụ đề ASS",
  contact_sheet: "Contact sheet",
  manifest: "Manifest (JSON)",
  report: "Báo cáo (Markdown)",
  pdf: "PDF truyện tranh",
};
const CHECK_LABEL: Record<string, string> = {
  dimensions: "Kích thước khung",
  frame_rate: "Tốc độ khung hình",
  duration: "Thời lượng",
  audio_stream: "Có luồng âm thanh",
  no_black_segments: "Không có đoạn đen",
  no_clipping: "Âm thanh không bị vỡ",
  subtitle_timing: "Thời gian phụ đề",
  every_shot_present: "Đủ mọi shot",
};

const show = (v: unknown) => (typeof v === "string" || typeof v === "number" ? String(v) : JSON.stringify(v));

export function ExportPanel({ ep, defaultBurn }: { ep: Episode; defaultBurn: boolean }) {
  const [aspect, setAspect] = useState<"9:16" | "16:9">("9:16");
  const [burn, setBurn] = useState(defaultBurn);
  const [allowDraft, setAllowDraft] = useState(false);
  const [result, setResult] = useState<ExportView | null>(null);
  const [refused, setRefused] = useState<{ detail: string; readiness: Readiness } | null>(null);
  const [comic, setComic] = useState<ExportView | null>(null);
  const act = useAction();
  const comicAct = useAction();
  const open = useAction();

  const doExport = async () => {
    setRefused(null);
    setResult(null);
    const r = await act.run(async () => {
      try {
        return await api<ExportView>(`/api/episodes/${ep.id}/export`, { body: { aspect, burn_subtitles: burn, allow_draft: allowDraft } });
      } catch (e) {
        // 409 = refused because the episode is not final: show the readiness reasons, not a generic error
        if (e instanceof ApiError && e.status === 409 && e.body && typeof e.body === "object" && "readiness" in e.body) {
          setRefused(e.body as { detail: string; readiness: Readiness });
          return null;
        }
        throw e;
      }
    });
    if (r) setResult(r);
  };
  const doComic = async () => {
    setComic(null);
    const r = await comicAct.run(async () => {
      try {
        return await api<ExportView>(`/api/episodes/${ep.id}/comic`, { body: {} });
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) throw new Error(`Chưa tạo được truyện tranh: ${e.message}`);
        throw e;
      }
    });
    if (r) setComic(r);
  };
  const openExport = async (id: string) => {
    const r = await open.run(() => api<ExportView>(`/api/exports/${id}`));
    if (r) {
      if (r.kind === "comic") setComic(r);
      else {
        setRefused(null);
        setResult(r);
      }
    }
  };

  return (
    <section className="panel" aria-labelledby="export-h">
      <h2 id="export-h">Xuất phim</h2>
      <div className="row wrap">
        <fieldset>
          <legend>Khung hình</legend>
          <label className="inline">
            <input type="radio" name={`aspect-${ep.id}`} checked={aspect === "9:16"} onChange={() => setAspect("9:16")} /> 9:16 (dọc)
          </label>
          <label className="inline">
            <input type="radio" name={`aspect-${ep.id}`} checked={aspect === "16:9"} onChange={() => setAspect("16:9")} /> 16:9 (ngang)
          </label>
        </fieldset>
        <label className="inline">
          <input type="checkbox" checked={burn} onChange={(e) => setBurn(e.target.checked)} /> In cứng phụ đề
        </label>
        <label className="inline">
          <input type="checkbox" checked={allowDraft} onChange={(e) => setAllowDraft(e.target.checked)} /> Cho phép bản nháp
        </label>
      </div>
      <div className="row">
        <button type="button" className="primary" onClick={doExport} disabled={act.busy} aria-busy={act.busy}>
          {act.busy ? "Đang xuất phim… (có thể mất ~30 giây)" : `Xuất phim ${aspect}`}
        </button>
        <button type="button" onClick={doComic} disabled={comicAct.busy} aria-busy={comicAct.busy}>
          {comicAct.busy ? "Đang tạo truyện tranh…" : "Tạo truyện tranh (carousel)"}
        </button>
      </div>
      <ErrorBox error={act.error} title="Xuất phim thất bại" />
      {refused && (
        <div className="error-box" role="alert" data-testid="export-refused">
          <strong>Studio từ chối xuất bản hoàn chỉnh</strong> ({READINESS_LABEL[refused.readiness.status] ?? refused.readiness.status}):{" "}
          {refused.readiness.reason}
          {refused.readiness.missing.length > 0 && <p>Thiếu media: {refused.readiness.missing.join(", ")}</p>}
          {refused.readiness.not_accepted.length > 0 && <p>Chưa được chấp nhận: {refused.readiness.not_accepted.join(", ")}</p>}
          <p className="small">Bật “Cho phép bản nháp” để xuất một bản xem trước có nhãn NHÁP.</p>
        </div>
      )}
      {result && <ExportResult x={result} />}

      <ErrorBox error={comicAct.error} />
      {comic && <ComicResult x={comic} />}

      {ep.exports.length > 0 && (
        <details>
          <summary>Các lần xuất trước ({ep.exports.length})</summary>
          <ul>
            {ep.exports.map((x) => (
              <li key={x.id}>
                {fmtTime(x.created_at)} · {x.kind} · {x.status}{" "}
                <button type="button" className="link" onClick={() => openExport(x.id)}>
                  Mở {x.id}
                </button>
              </li>
            ))}
          </ul>
          <ErrorBox error={open.error} />
        </details>
      )}
    </section>
  );
}

function ExportResult({ x }: { x: ExportView }) {
  const m = x.manifest ?? {};
  const v = m.video ?? {};
  const checks: { check: string; expected: unknown; actual: unknown; pass: boolean }[] = m.inspection?.checks ?? [];
  const costs = m.costs ?? {};
  return (
    <div className="export-result" data-testid="export-result">
      <h3>
        Kết quả: {x.status === "final" ? "BẢN HOÀN CHỈNH" : `BẢN XEM TRƯỚC (${x.status})`}{" "}
        {x.reused && <span className="tag">dùng lại từ bộ nhớ đệm, không render lại</span>}
      </h3>
      <Notice text={x.reused ? "Không có gì thay đổi kể từ lần xuất trước — trả lại đúng tệp cũ." : null} />
      {m.fixture_notice && <p className="warn-text">{m.fixture_notice}</p>}
      {x.files.film && (
        <video className="film" controls src={x.files.film} aria-label="Phim đã xuất" data-testid="film-player" />
      )}
      <ul className="files">
        {Object.entries(x.files)
          .filter(([k]) => k !== "film")
          .map(([k, url]) => (
            <li key={k}>
              <a href={url} target="_blank" rel="noreferrer">
                {FILE_LABEL[k] ?? k}
              </a>
            </li>
          ))}
        {x.files.film && (
          <li>
            <a href={x.files.film} download>
              Tải phim
            </a>
          </li>
        )}
      </ul>
      <dl className="stats">
        <div>
          <dt>Độ phân giải gốc</dt>
          <dd>{v.native_resolution}</dd>
        </div>
        <div>
          <dt>Độ phân giải giao</dt>
          <dd>
            {v.delivery_resolution} ({v.aspect})
          </dd>
        </div>
        <div>
          <dt>Phóng to</dt>
          <dd>{v.upscaled ? `CÓ — x${v.scale_factor}` : "không"}</dd>
        </div>
        <div>
          <dt>Chi phí đã chi</dt>
          <dd>{fmtUsd(costs.committed_usd)}</dd>
        </div>
      </dl>
      {v.note && <p className={v.upscaled ? "warn-text" : "muted"}>{v.upscaled ? "Lưu ý: bản giao là ảnh phóng to (upscale) từ bản render gốc, không phải độ phân giải gốc." : v.note}</p>}
      <p>
        <strong>Khớp khẩu hình (lip-sync):</strong> {m.lip_sync}
      </p>
      <p className="small muted">
        Shot được chấp nhận: {costs.accepted_shots ?? "-"} · lượt tạo: {costs.generated_clip_attempts ?? "-"} · chi phí / phút hoàn chỉnh:{" "}
        {fmtUsd(costs.cost_per_accepted_minute_usd)}
      </p>
      <h4>
        Kiểm tra media {m.inspection?.all_pass ? <span className="pill pill-done">tất cả đạt</span> : <span className="pill pill-failed">có mục không đạt</span>}
      </h4>
      <div className="table-wrap">
        <table data-testid="checks-table">
          <caption className="sr-only">Kết quả kiểm tra tệp phim bằng ffprobe</caption>
          <thead>
            <tr>
              <th>Kiểm tra</th>
              <th>Mong đợi</th>
              <th>Thực tế</th>
              <th>Kết quả</th>
            </tr>
          </thead>
          <tbody>
            {checks.map((c) => (
              <tr key={c.check}>
                <td>{CHECK_LABEL[c.check] ?? c.check}</td>
                <td>{show(c.expected)}</td>
                <td>{show(c.actual)}</td>
                <td className={c.pass ? "ok-text" : "err-text"}>{c.pass ? "Đạt" : "Không đạt"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ComicResult({ x }: { x: ExportView }) {
  const slides = Object.entries(x.files)
    .filter(([k]) => k.startsWith("slide_"))
    .sort(([a], [b]) => a.localeCompare(b));
  return (
    <div className="comic" data-testid="comic-result">
      <h3>
        Truyện tranh ({slides.length} trang) {x.reused && <span className="tag">dùng lại</span>}
      </h3>
      {x.files.pdf && (
        <p>
          <a href={x.files.pdf} target="_blank" rel="noreferrer">
            Mở PDF
          </a>
        </p>
      )}
      <ul className="slides">
        {slides.map(([k, url], i) => (
          <li key={k}>
            <img src={url} alt={`Trang ${i + 1}`} loading="lazy" />
          </li>
        ))}
      </ul>
    </div>
  );
}

