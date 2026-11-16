"""Real Chromium acceptance tests. Only the disposable mai_test database is permitted.

MAI_TEST_DATABASE_URL=... python scripts/browser_smoke.py [--package extracted/mai-demo]
The app runs from a temporary copy with no .env or external services. Artifacts stay in
standalone/dist/browser-qa; Playwright is a development dependency, never a runtime one.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

from playwright.sync_api import expect, sync_playwright
import requests
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "standalone/dist/browser-qa"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Source(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/crew"):
            body = json.dumps({"crew_availability": [{"crew_id": "QA-1", "status": "available"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = b"Synthetic QA source: access denied"
            self.send_response(401)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def exercise(page, base: str, data: Path, db):
    page.goto(base + "/dashboard")
    expect(page.get_by_text("Данных по скважинам пока нет", exact=True)).to_be_visible()
    page.locator("#run").click()
    expect(page.locator("#notice")).to_contain_text("Список скважин пуст", timeout=30000)
    assert "Рейтинг пересчитан" not in page.locator("#notice").inner_text()

    page.locator("#docFiles").set_input_files([str(data / f"sample_{s}.xlsx") for s in ("dars", "khimik", "era")])
    expect(page.locator("#dataState")).to_contain_text("14 из 14", timeout=90000)
    expect(page.locator("#rows .wellbtn")).to_have_count(13)
    page.screenshot(path=ARTIFACTS / "rating-1280.png")
    assert page.locator('#tiles').bounding_box()['y'] < page.locator('.alerts').bounding_box()['y']
    for selector in ('#fieldFilter','#tableView'):
        assert page.locator(selector).evaluate("el => getComputedStyle(el).backgroundPositionX") == 'calc(100% - 12px)'
    expect(page.locator('#pprSub')).to_contain_text('критичность')
    expect(page.locator('#sec-ppr .legend')).to_contain_text('Высокая — ППР')


    def open_well(well="DEMO-001"):
        page.locator("#rows").get_by_role("button", name=f"Открыть карточку скважины {well}", exact=True).click()
        expect(page.locator("#cardTitle")).to_have_text(f"Скважина {well}")

    open_well()
    models = page.locator('#cardBody .model-block')
    assert models.nth(1).bounding_box()['y'] - (models.nth(0).bounding_box()['y'] + models.nth(0).bounding_box()['height']) >= 14
    co2 = page.locator('#wellCo2 details.rules-box')
    expect(co2).not_to_have_attribute('open', '')
    expect(co2.locator('summary')).to_contain_text('мм/год')
    co2.locator('summary').click()
    expect(co2.locator('table.rules').first).to_contain_text('Настройки')
    expect(co2.locator('input, form, button, pre, p, ul')).to_have_count(0)
    expect(page.locator('#co2Feature, #co2Nav')).to_have_count(0)
    comment = page.get_by_label("Комментарий", exact=True)
    comment.fill("QA: сохранённое решение")
    def changed_automatic(route):
        payload = route.fetch().json()
        for well in payload['wells']:
            if well['well_id'] == 'DEMO-001':
                well['co2_assessment']['scenarios'][0]['rate_mm_year'] = 88
                well['co2_assessment']['input_hash'] = 'qa-auto-refresh'
        route.fulfill(json=payload)
    page.route('**/api/v1/rating', changed_automatic)
    page.evaluate('refresh({keepNotice:true, auto:true})')
    expect(co2.locator('summary')).to_contain_text('88')
    expect(co2).to_have_attribute('open', '')
    expect(comment).to_have_value('QA: сохранённое решение')
    page.unroute('**/api/v1/rating')

    dialogs = []

    def decline(dialog):
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", decline)
    page.get_by_role("button", name="Закрыть карточку", exact=True).click()
    expect(page.locator("#wellDrawer")).to_have_class("drawer open")
    expect(comment).to_have_value("QA: сохранённое решение")
    assert dialogs and "Несохранённые" in dialogs[-1]
    open_well()  # clicking the same well must not reconstruct its form
    expect(comment).to_have_value("QA: сохранённое решение")
    page.route("**/api/v1/rating/wells/DEMO-001", lambda route: route.fulfill(
        status=503, content_type="application/json", body='{"detail":"QA: временная ошибка сохранения"}'))
    page.get_by_role("button", name="Сохранить", exact=True).click()
    expect(page.locator("#notice")).to_contain_text("QA: временная ошибка")
    expect(comment).to_have_value("QA: сохранённое решение")
    page.unroute("**/api/v1/rating/wells/DEMO-001")
    page.get_by_role("button", name="Сохранить", exact=True).click()
    expect(page.locator("#notice")).to_contain_text("сохранен", ignore_case=True)
    expect(page.get_by_role("button", name="Сохранить", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Закрыть карточку", exact=True).click()
    open_well()
    expect(page.get_by_label("Комментарий", exact=True)).to_have_value("QA: сохранённое решение")
    page.get_by_role("button", name="Закрыть карточку", exact=True).click()
    page.remove_listener("dialog", decline)

    delayed = []
    page.route("**/api/v1/rating/wells/DEMO-001", lambda route: delayed.append(route))
    open_well()
    expect(page.locator("#aiBox")).to_contain_text("Загружаем")
    open_well("DEMO-002")
    expect(page.locator("#aiBox")).to_contain_text("ИИ-агент не настроен")
    assert delayed
    with page.expect_response("**/api/v1/rating/wells/DEMO-001") as old_response:
        delayed[0].fulfill(status=500, content_type="application/json", body='{"detail":"QA: поздняя ошибка A"}')
    old_response.value.finished()
    expect(page.locator("#aiBox")).not_to_contain_text("QA: поздняя ошибка A")
    page.unroute("**/api/v1/rating/wells/DEMO-001")
    page.get_by_role("button", name="Закрыть карточку", exact=True).click()

    dars = page.locator("#sourceCards > .box").filter(has=page.get_by_role("heading", name="ДАРС", exact=True))
    dars.get_by_label("Проверить скважину", exact=True).fill("DEMO-001")
    dars.get_by_role("button", name="Проверить без импорта", exact=True).click()
    expect(dars).to_contain_text("HTTP 401")
    crew = page.locator("#sourceCards > .box").filter(has=page.get_by_role("heading", name="Бригады", exact=True))
    crew.get_by_label("Проверить скважину", exact=True).fill("DEMO-001")
    crew.get_by_role("button", name="Проверить без импорта", exact=True).click()
    expect(crew).to_contain_text("Данные не импортированы")
    expect(crew).to_contain_text("Записей: 1")

    # A failed retry with an accepted contribution, plus a disposable failure with no data.
    from app.db.schema import documents
    from sqlalchemy import select
    with db.engine().begin() as conn:
        doc_id = conn.scalar(select(documents.c.document_id).where(documents.c.filename == "sample_dars.xlsx"))
        conn.execute(documents.update().where(documents.c.document_id == doc_id).values(status="failed"))
    page.locator("#docFiles").set_input_files({"name": "QA-invalid.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.7\nQA damaged PDF\n%%EOF"})
    if not page.locator("#docHistory").is_visible():
        page.locator("#docHistToggle").click()
    expect(page.locator("#docRows")).to_contain_text("Предыдущая принятая версия учитывается", timeout=30000)
    expect(page.locator("#docRows > tr.drow").filter(has_text="QA-invalid.pdf")).to_contain_text("Ошибка", timeout=30000)
    page.locator("#docSearch").fill("sample_dars")
    expect(page.locator("#docRows > tr.drow")).to_have_count(1)
    page.locator("#docRows > tr.drow").click()
    expect(page.get_by_role("button", name="Скачать оригинал", exact=True)).to_be_visible()
    original_control = page.get_by_role("button", name="Скачать оригинал", exact=True).element_handle()
    page.locator("#refresh").click()
    expect(page.locator("#refresh")).to_be_enabled()
    assert original_control.is_visible(), "Refreshing documents must preserve the expanded detail nodes"
    page.locator("#docReset").click()
    page.locator("#docClear").click()
    page.get_by_role("menuitem", name="Без принятых данных", exact=False).click()
    expect(page.locator("#notice")).to_contain_text("Удалено документов: 1", timeout=30000)
    assert requests.get(base + "/api/v1/documents").json()["matching_count"] == 3
    assert len(db.load_telemetry("DEMO-001")) > 0

    # More than one page: reach an old file, search it, then clean only these synthetic rows.
    now = datetime.now(timezone.utc)
    with db.engine().begin() as conn:
        conn.execute(documents.insert(), [{"document_id": f"00000000-0000-0000-0000-{i:012d}",
            "sha256": hashlib.sha256(f"browser-qa-{i}".encode()).hexdigest(),
            "filename": f"QA-history-{i:03d}.pdf", "channel": "upload", "file_kind": "pdf",
            "size_bytes": 1, "storage_path": "qa/unused", "status": "not_recognized", "attempts": 1,
            "created_at": now - timedelta(days=1, seconds=i)} for i in range(51)])
    page.locator("#refresh").click()
    expect(page.locator("#docRows > tr.drow")).to_have_count(50)
    page.locator("#docNext").click()
    expect(page.get_by_text("QA-history-050.pdf", exact=True)).to_be_visible()
    page.locator("#docPrev").click()
    expect(page.locator("#docRows > tr.drow")).to_have_count(50)
    page.locator("#docSearch").fill("QA-history-050")
    expect(page.locator("#docRows > tr.drow")).to_have_count(1)
    expect(page.get_by_text("QA-history-050.pdf", exact=True)).to_be_visible()
    page.locator("#docReset").click()
    page.locator("#docClear").click()
    page.get_by_role("menuitem", name="Без принятых данных", exact=False).click()
    expect(page.locator("#notice")).to_contain_text("Удалено документов: 51", timeout=30000)

    page.locator("#tableView").select_option("full")
    expect(page.locator("#ratingTable")).to_have_class("table-box")
    page.locator("#tableView").select_option("compact")
    for width, height in ((1366, 768), (390, 844), (640, 720)):
        page.set_viewport_size({"width": width, "height": height})
        page.locator("#search").scroll_into_view_if_needed()
        expect(page.locator('.sidenav a[href="#sec-rating"]')).to_have_attribute("aria-current", "true")
        assert page.evaluate("document.body.scrollWidth <= document.documentElement.clientWidth")
        page.screenshot(path=ARTIFACTS / f"rating-{width}.png")
    page.set_viewport_size({"width": 1280, "height": 720})
    page.locator("#search").focus()
    page.keyboard.press("Tab")
    expect(page.locator("#fieldFilter")).to_be_focused()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=ROOT)
    args = parser.parse_args()
    url = os.environ.get("MAI_TEST_DATABASE_URL", "")
    if not url or make_url(url).database != "mai_test":
        raise SystemExit("Browser QA requires the disposable mai_test database via MAI_TEST_DATABASE_URL")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    for name in ("failure.png", "failure.html"):
        (ARTIFACTS / name).unlink(missing_ok=True)
    source = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    threading.Thread(target=source.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="mai-browser-qa-") as temp:
        folder = Path(temp)
        package = folder / "package"
        for name in ("app", "dashboard", "config"):
            shutil.copytree(args.package / name, package / name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("BUILD_INFO.json",):
            if (args.package / name).is_file():
                shutil.copy2(args.package / name, package / name)
        data = folder / "data"
        subprocess.run([sys.executable, str(ROOT / "scripts/build_sample_data.py"), "--end-date", date.today().isoformat(), "--out", str(data)], check=True)
        for key in list(os.environ):
            if key.startswith("MAI_"):
                del os.environ[key]
        os.environ.update(MAI_DATABASE_URL=url, MAI_DATABASE_SCHEMA="browser_qa", MAI_API_KEY="",
                          MAI_DOCS_STORAGE_DIR=str(folder / "documents"), MAI_DOCS_INBOX_DIR="",
                          MAI_DOCS_WORKER="true", MAI_SOURCE_MAX_RETRIES="0",
                          MAI_SOURCE_DARS_URL=f"http://127.0.0.1:{source.server_port}/denied",
                          MAI_SOURCE_CREW_SCHEDULE_URL=f"http://127.0.0.1:{source.server_port}/crew",
                          PYTHONPATH=str(package), PYTHONDONTWRITEBYTECODE="1")
        sys.path.insert(0, str(package))
        from app import db
        db.init_db()
        with db.engine().begin() as conn:
            for table in reversed(db.metadata.sorted_tables):
                conn.execute(table.delete())
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        with (ARTIFACTS / "server.log").open("w") as log:
            server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)], cwd=package, stdout=log, stderr=log)
            try:
                for _ in range(120):
                    if server.poll() is not None:
                        raise RuntimeError("QA server exited; see server.log")
                    try:
                        if requests.get(base + "/health/ready", timeout=1).ok:
                            break
                    except requests.RequestException:
                        pass
                    time.sleep(.25)
                else:
                    raise RuntimeError("QA server did not become ready")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    context = browser.new_context(viewport={"width": 1280, "height": 720}, locale="ru-RU")
                    context.tracing.start(screenshots=True, snapshots=True, sources=True)
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    try:
                        exercise(page, base, data, db)
                        assert not errors, errors
                        print("Browser QA: first use, decisions, late response, CO2, documents, sources, responsive layout: OK")
                    except Exception:
                        page.screenshot(path=ARTIFACTS / "failure.png", full_page=True)
                        (ARTIFACTS / "failure.html").write_text(page.content(), encoding="utf-8")
                        raise
                    finally:
                        context.tracing.stop(path=ARTIFACTS / "trace.zip")
                        browser.close()
            finally:
                server.terminate()
                server.wait(timeout=15)
                source.shutdown()


if __name__ == "__main__":
    main()
