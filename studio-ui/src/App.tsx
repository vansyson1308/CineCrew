// CineCrew Studio UI — shell: hash routing, API health banner.
import { useEffect, useState } from "react";
import { api } from "./api";
import { usePoll } from "./common";
import ProjectsPage from "./ProjectsPage";
import ProjectPage from "./ProjectPage";
import EpisodePage from "./EpisodePage";

type Route = { name: "projects" } | { name: "project"; id: string } | { name: "episode"; id: string };

function parse(hash: string): Route {
  const m = hash.replace(/^#\/?/, "").split("/");
  if (m[0] === "p" && m[1]) return { name: "project", id: m[1] };
  if (m[0] === "e" && m[1]) return { name: "episode", id: m[1] };
  return { name: "projects" };
}

export default function App() {
  const [route, setRoute] = useState<Route>(parse(window.location.hash));
  const [apiDown, setApiDown] = useState<string | null>(null);

  useEffect(() => {
    const on = () => {
      setRoute(parse(window.location.hash));
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);

  const check = () =>
    api<{ ok: boolean; ffmpeg: boolean }>("/api/health")
      .then((h) => setApiDown(h.ffmpeg ? null : "Máy chủ chạy nhưng thiếu ffmpeg: không render / xuất được."))
      .catch((e) => setApiDown(`Máy chủ Studio không phản hồi: ${(e as Error).message}`));
  useEffect(() => {
    check();
  }, []);
  usePoll(check, 10000, true);

  return (
    <>
      <a className="skip-link" href="#main">
        Bỏ qua tới nội dung
      </a>
      <header className="app-header">
        <a href="#/" className="brand">
          CineCrew Studio
        </a>
        <span className="muted">Bàn làm việc của đạo diễn</span>
      </header>
      {apiDown && (
        <div className="error-box banner" role="alert" data-testid="api-down">
          {apiDown} — đang thử kết nối lại…
        </div>
      )}
      <main id="main" tabIndex={-1}>
        {route.name === "projects" && <ProjectsPage />}
        {route.name === "project" && <ProjectPage key={route.id} id={route.id} />}
        {route.name === "episode" && <EpisodePage key={route.id} id={route.id} />}
      </main>
    </>
  );
}
