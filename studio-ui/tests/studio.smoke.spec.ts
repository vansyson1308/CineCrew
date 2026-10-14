// CineCrew Studio UI — browser smoke test (OFFLINE FIXTURE mode, fresh STUDIO_HOME, real server + worker).
// Everything is driven through the UI: create, approve, natural-language edit, retry, cancel→resume, export,
// and error states (export refused before clips exist, ambiguous edit question).
import { expect, Locator, Page, test } from "@playwright/test";

const LONG = 240_000;
const shots = (n: number) => `test-results/screenshots/${n}`;

const chip = (page: Page, key: string) => page.getByTestId(`job-${key}`);
const attempts = async (l: Locator) => Number(await l.getAttribute("data-attempts"));

async function waitKeyframesAccepted(page: Page) {
  await expect(page.getByTestId("kf-accepted")).toHaveText("6/6", { timeout: LONG });
}

async function openEpisode(page: Page, title: RegExp) {
  await page.getByRole("link", { name: "Giao Hàng Phố Cổ" }).first().click();
  await page.getByRole("link", { name: title }).click();
  await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
}

test("director workspace: create, approve, edit, retry, cancel+resume, export, error states", async ({ page }) => {
  // ── a) create a project from the template (offline mode by default) ─────────────────
  await page.goto("/");
  await expect(page.getByText("Chưa có dự án nào")).toBeVisible();
  await expect(page.getByRole("radio", { name: /OFFLINE FIXTURE/ })).toBeChecked();
  await page.getByRole("button", { name: "Tạo dự án" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Giao Hàng Phố Cổ" })).toBeVisible();
  await expect(page.getByTestId("mode-badge").first()).toHaveText("OFFLINE FIXTURE (không gọi API trả phí)");
  // the live switch requires explicit confirmation + a budget: opening and cancelling changes nothing
  await page.getByRole("button", { name: "Chuyển sang LIVE…" }).click();
  await expect(page.getByRole("button", { name: "Xác nhận chuyển sang LIVE" })).toBeDisabled();
  await page.getByRole("dialog").getByRole("button", { name: "Hủy" }).click();
  await expect(page.getByTestId("mode-badge").first()).toHaveAttribute("data-mode", "offline_fixture");

  // ── g1) error state: episode 2 cannot be exported before its clips exist ───────────
  await page.getByRole("link", { name: /Tập 2: Mưa phố cổ/ }).click();
  await expect(page.getByRole("heading", { level: 1, name: /Tập 2/ })).toBeVisible();
  await page.getByRole("button", { name: "Xuất phim 9:16" }).click();
  const refused = page.getByTestId("export-refused");
  await expect(refused).toBeVisible({ timeout: LONG });
  await expect(refused).toContainText("Studio từ chối xuất bản hoàn chỉnh");
  await expect(refused).toContainText(/Thiếu media: .*clip:shot_0\d/);

  // g2) ambiguous natural-language edit -> the API's question is shown, nothing applied
  await page.getByLabel(/Mô tả thay đổi/).fill("làm cho đẹp hơn");
  await page.getByRole("button", { name: "Xem trước thay đổi" }).click();
  await expect(page.getByTestId("edit-question")).toContainText("Chưa hiểu yêu cầu");
  await expect(page.getByRole("button", { name: "Áp dụng thay đổi" })).toHaveCount(0);

  // ── e) "resume": cancel a waiting clip job, approve, then retry it and watch it finish ──
  await waitKeyframesAccepted(page);
  const ep2clip = chip(page, "clip:shot_02");
  await expect(ep2clip).toHaveAttribute("data-state", "blocked");
  await ep2clip.getByRole("button", { name: "Hủy" }).click();
  await expect(ep2clip).toHaveAttribute("data-state", "cancelled");
  await page.getByRole("button", { name: "Duyệt storyboard" }).click();
  await expect(page.getByText("Storyboard đã duyệt")).toBeVisible();
  // every other clip renders; the cancelled one stays cancelled and blocks a final export
  await expect(chip(page, "clip:shot_06")).toHaveAttribute("data-state", "done", { timeout: LONG });
  await expect(chip(page, "clip:shot_01")).toHaveAttribute("data-state", "done", { timeout: LONG });
  await expect(ep2clip).toHaveAttribute("data-state", "cancelled");
  await expect(page.getByTestId("readiness-reason")).toContainText("clip:shot_02");
  await ep2clip.getByRole("button", { name: "Thử lại" }).click();
  await expect(ep2clip).toHaveAttribute("data-state", "done", { timeout: LONG });
  await expect(page.getByTestId("readiness-badge").first()).toHaveText("HOÀN CHỈNH", { timeout: LONG });
  await expect(page.getByTestId("event-log")).toContainText("clip:shot_02");

  // ── b) episode 1: keyframes accepted -> approve storyboard -> clips render ──────────
  await openEpisode(page, /Tập 1: Đơn hàng số 47/);
  await waitKeyframesAccepted(page);
  await expect(page.getByText(/chờ bạn duyệt storyboard/)).toBeVisible();
  await page.getByRole("button", { name: "Duyệt storyboard" }).click();
  await expect(page.getByText("Đã duyệt storyboard — clip bắt đầu render.")).toBeVisible();
  await expect(page.getByTestId("readiness-badge").first()).toHaveText("HOÀN CHỈNH", { timeout: LONG });
  for (let i = 1; i <= 6; i++) await expect(chip(page, `clip:shot_0${i}`)).toHaveAttribute("data-state", "done");
  await page.screenshot({ path: shots(1) + "-storyboard-approved.png", fullPage: true });

  // ── c) edit one shot in words: preview -> only that shot's jobs re-run -> apply ──────
  const before: Record<string, number> = {};
  for (const k of ["kf:shot_01", "clip:shot_01", "kf:shot_03", "clip:shot_03", "clip:shot_04"]) before[k] = await attempts(chip(page, k));
  await page.getByTestId("shot-3").getByRole("button", { name: "Sửa shot này bằng lời…" }).click();
  const box = page.getByLabel(/Mô tả thay đổi/);
  await expect(box).toHaveValue("Đổi ánh sáng cảnh 3 thành ");
  await box.pressSequentially("ánh trăng xanh");
  await page.getByRole("button", { name: "Xem trước thay đổi" }).click();
  const pv = page.getByTestId("edit-preview");
  await expect(pv).toBeVisible();
  const affected = pv.getByTestId("affected-jobs");
  await expect(affected).toContainText("kf:shot_03");
  await expect(affected).toContainText("clip:shot_03");
  await expect(affected.getByRole("listitem")).toHaveCount(2);
  await expect(pv).toContainText("Giữ nguyên từ bộ nhớ đệm: 16 job");
  await page.screenshot({ path: shots(2) + "-edit-preview.png", fullPage: false });
  await page.getByRole("button", { name: "Áp dụng thay đổi" }).click();
  await expect(page.getByText(/Đã áp dụng — phiên bản v2/)).toBeVisible();
  await expect(page.getByTestId("shot-3").getByTestId("lighting")).toHaveText("ánh trăng xanh");
  await expect(chip(page, "clip:shot_03")).toHaveAttribute("data-attempts", String(before["clip:shot_03"] + 1), { timeout: LONG });
  await expect(chip(page, "clip:shot_03")).toHaveAttribute("data-state", "done", { timeout: LONG });
  await expect(page.getByTestId("readiness-badge").first()).toHaveText("HOÀN CHỈNH", { timeout: LONG });
  expect(await attempts(chip(page, "kf:shot_03"))).toBe(before["kf:shot_03"] + 1);
  // untouched shots were not re-rendered
  for (const k of ["kf:shot_01", "clip:shot_01", "clip:shot_04"]) expect(await attempts(chip(page, k))).toBe(before[k]);
  await expect(page.getByRole("button", { name: "Khôi phục v1" })).toBeVisible();

  // ── d) retry one job ────────────────────────────────────────────────────────────────
  const c1 = chip(page, "clip:shot_01");
  await c1.getByRole("button", { name: "Thử lại" }).click();
  await expect(c1.getByText("Đã yêu cầu chạy lại.")).toBeVisible();
  await expect(c1).toHaveAttribute("data-attempts", String(before["clip:shot_01"] + 1), { timeout: LONG });
  await expect(c1).toHaveAttribute("data-state", "done", { timeout: LONG });
  // compare versions of that shot's clip: both attempts listed
  await c1.getByRole("button", { name: "Xem các phiên bản" }).click();
  const dlg = page.getByRole("dialog");
  await expect(dlg.getByText(/^Lượt 2/)).toBeVisible();
  await expect(dlg.getByText(/^Lượt 1/)).toBeVisible();
  await dlg.getByRole("button", { name: "Đóng" }).click();
  await expect(page.getByTestId("readiness-badge").first()).toHaveText("HOÀN CHỈNH", { timeout: LONG });

  // ── f) export 9:16 -> player + inspection checks table ─────────────────────────────
  await page.getByRole("radio", { name: /9:16/ }).check();
  await page.getByRole("button", { name: "Xuất phim 9:16" }).click();
  const result = page.getByTestId("export-result");
  await expect(result).toBeVisible({ timeout: LONG });
  await expect(result).toContainText("BẢN HOÀN CHỈNH");
  await expect(page.getByTestId("film-player")).toBeVisible();
  const rows = page.getByTestId("checks-table").locator("tbody tr");
  await expect(rows).toHaveCount(8);
  await expect(page.getByTestId("checks-table")).toContainText("1080x1920");
  await expect(result).toContainText("Khớp khẩu hình");
  await expect(result.getByRole("link", { name: "Phụ đề SRT" })).toBeVisible();
  const film = await page.getByTestId("film-player").getAttribute("src");
  const head = await page.request.get(film!);
  expect(head.ok()).toBeTruthy();
  expect(head.headers()["content-type"]).toContain("video/mp4");
  await result.scrollIntoViewIfNeeded();
  await page.screenshot({ path: shots(3) + "-export-result.png", fullPage: true });
});
