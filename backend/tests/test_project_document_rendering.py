"""Project-document rendering owns one physical CPU stage, never a DB session."""

from __future__ import annotations

import asyncio
import contextvars
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from pypdf import PdfReader
from sqlalchemy import func, select

from app.core import storage
from app.models.audit import AuditEvent
from app.models.document import Document
from app.models.project_link import ProjectDocument
from app.services import doc_templates, project_profit


@pytest.fixture(autouse=True)
def renderer_owner(monkeypatch):
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-project-document")
    monkeypatch.setattr(doc_templates, "_DOCUMENT_RENDER_EXECUTOR", executor)
    monkeypatch.setattr(doc_templates, "_DOCUMENT_RENDER_SLOT", threading.BoundedSemaphore(1))
    yield executor
    current = doc_templates._DOCUMENT_RENDER_EXECUTOR
    executor.shutdown(wait=True, cancel_futures=True)
    if current is not None and current is not executor:
        current.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def detached_service(monkeypatch):
    calls = {"io_threads": [], "attachments": []}

    async def own(_db, _org, _template):
        calls["io_threads"].append(threading.get_ident())
        return SimpleNamespace(
            active=True,
            kind="contract",
            name="Synthetic contract",
            body="Project {{project.code}}\n\nLiteral <review> & {{unknown.token}}",
        )

    async def context(_db, _org, _project, **_kw):
        calls["io_threads"].append(threading.get_ident())
        return {"project.code": "TEST-1"}

    async def attach(_db, _org, _project, **kw):
        calls["io_threads"].append(threading.get_ident())
        calls["attachments"].append(kw)
        return SimpleNamespace(**kw)

    monkeypatch.setattr(doc_templates, "_own", own)
    monkeypatch.setattr(doc_templates, "build_context", context)
    monkeypatch.setattr(project_profit, "attach_document", attach)
    return calls


async def generate():
    return await doc_templates.generate_project_document(
        object(), "org", "project", template_id="template", template_scope="own"
    )


@pytest.mark.asyncio
async def test_real_project_document_renderer_leaves_the_loop(detached_service, monkeypatch):
    loop_thread = threading.get_ident()
    renderer_threads = []
    real = doc_templates.to_pdf

    def traced(title, text):
        renderer_threads.append(threading.get_ident())
        return real(title, text)

    monkeypatch.setattr(doc_templates, "to_pdf", traced)
    row, text = await generate()
    assert row.filename == "TEST-1-contract.pdf"
    assert text == "Project TEST-1\n\nLiteral <review> & {{unknown.token}}"
    pdf = PdfReader(io.BytesIO(row.data))
    assert "Literal <review> & {{unknown.token}}" in pdf.pages[0].extract_text()
    assert renderer_threads and all(t != loop_thread for t in renderer_threads)
    assert detached_service["io_threads"] == [loop_thread] * 3


@pytest.mark.asyncio
async def test_post_enqueue_thread_start_failure_drains_inputs_and_recovers(
    renderer_owner, monkeypatch
):
    def fail_start():
        raise RuntimeError("controlled thread start failure after enqueue")

    monkeypatch.setattr(renderer_owner, "_adjust_thread_count", fail_start)
    with pytest.raises(RuntimeError, match="controlled thread start failure"):
        await doc_templates._render_document_async("Title", "Body", {})
    queued = []
    while not renderer_owner._work_queue.empty():
        queued.append(renderer_owner._work_queue.get_nowait())
    assert all(item is None for item in queued), "failed submit retained an unowned input job"
    assert doc_templates._DOCUMENT_RENDER_EXECUTOR is None
    text, pdf = await doc_templates._render_document_async("Title", "Recovered", {})
    assert text == "Recovered" and pdf.startswith(b"%PDF-")


async def wait_started(event):
    assert await asyncio.to_thread(event.wait, 2), "controlled worker did not start"


async def drain(executor):
    await asyncio.wait_for(asyncio.wrap_future(executor.submit(lambda: None)), timeout=2)


def blocked_renderer(monkeypatch, *, raises=False):
    started, release = threading.Event(), threading.Event()
    calls = []

    def render(title, text):
        calls.append((title, text))
        started.set()
        if not release.wait(2):
            raise AssertionError("test did not release its owned renderer")
        if raises:
            raise ValueError("controlled physical render failure")
        return b"%PDF-test-owned"

    monkeypatch.setattr(doc_templates, "to_pdf", render)
    return started, release, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 3])
async def test_running_cancel_keeps_physical_admission_without_attachment(
    detached_service, renderer_owner, monkeypatch, cancel_count
):
    started, release, calls = blocked_renderer(monkeypatch)
    task = asyncio.create_task(generate())
    try:
        await wait_started(started)
        for _ in range(cancel_count):
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(3):
            with pytest.raises(doc_templates.ProjectDocumentRenderingBusy):
                await generate()
        assert len(calls) == 1 and detached_service["attachments"] == []
    finally:
        release.set()
        await drain(renderer_owner)
    assert detached_service["attachments"] == []
    row, _text = await generate()
    assert row.data == b"%PDF-test-owned" and len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 3])
async def test_queued_cancel_does_not_release_or_cancel_the_accepted_job(
    detached_service, renderer_owner, monkeypatch, cancel_count
):
    holding, release = threading.Event(), threading.Event()

    def occupy_worker():
        holding.set()
        assert release.wait(2)

    blocker = renderer_owner.submit(occupy_worker)
    await wait_started(holding)
    render_calls = []
    monkeypatch.setattr(doc_templates, "to_pdf", lambda *_: render_calls.append(1) or b"%PDF-test")
    task = asyncio.create_task(generate())
    try:
        await asyncio.sleep(0)
        assert renderer_owner._work_queue.qsize() == 1
        for _ in range(cancel_count):
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(3):
            with pytest.raises(doc_templates.ProjectDocumentRenderingBusy):
                await generate()
        assert renderer_owner._work_queue.qsize() == 1
        assert render_calls == [] and detached_service["attachments"] == []
    finally:
        release.set()
        await asyncio.wrap_future(blocker)
        await drain(renderer_owner)
    assert render_calls == [1] and detached_service["attachments"] == []
    await generate()
    assert render_calls == [1, 1] and len(detached_service["attachments"]) == 1


@pytest.mark.asyncio
async def test_cancelled_caller_consumes_late_physical_exception_and_recovers(
    detached_service, renderer_owner, monkeypatch
):
    started, release, _calls = blocked_renderer(monkeypatch, raises=True)
    loop = asyncio.get_running_loop()
    prior_handler = loop.get_exception_handler()
    unexpected = []
    loop.set_exception_handler(lambda _loop, context: unexpected.append(context))
    task = asyncio.create_task(generate())
    try:
        await wait_started(started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await drain(renderer_owner)
        await asyncio.sleep(0)
        assert unexpected == [] and detached_service["attachments"] == []
        monkeypatch.setattr(doc_templates, "to_pdf", lambda *_: b"%PDF-recovered")
        await generate()
        assert len(detached_service["attachments"]) == 1
    finally:
        release.set()
        await drain(renderer_owner)
        loop.set_exception_handler(prior_handler)


def test_loop_closure_does_not_release_or_lose_the_physical_owner(
    detached_service, renderer_owner, monkeypatch
):
    started, release, _calls = blocked_renderer(monkeypatch, raises=True)
    unexpected = []

    async def cancelled_request():
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: unexpected.append(context)
        )
        task = asyncio.create_task(generate())
        await wait_started(started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(cancelled_request())  # The owning request loop now closes.

        async def refused():
            with pytest.raises(doc_templates.ProjectDocumentRenderingBusy):
                await generate()

        asyncio.run(refused())
        assert detached_service["attachments"] == []
    finally:
        release.set()
        renderer_owner.submit(lambda: None).result(timeout=2)
    assert unexpected == [] and detached_service["attachments"] == []
    monkeypatch.setattr(doc_templates, "to_pdf", lambda *_: b"%PDF-recovered")
    asyncio.run(generate())
    assert len(detached_service["attachments"]) == 1


@pytest.mark.asyncio
async def test_renderer_exception_propagates_before_attachment_and_releases(
    detached_service, monkeypatch
):
    def fail(*_):
        raise ValueError("controlled render failure")

    monkeypatch.setattr(doc_templates, "to_pdf", fail)
    with pytest.raises(ValueError, match="controlled render failure"):
        await generate()
    assert detached_service["attachments"] == []
    monkeypatch.setattr(doc_templates, "to_pdf", lambda *_: b"%PDF-recovered")
    await generate()
    assert len(detached_service["attachments"]) == 1


@pytest.mark.asyncio
async def test_pre_enqueue_submit_failure_releases_and_recovers(renderer_owner, monkeypatch):
    def refuse(*_, **_kw):
        raise RuntimeError("controlled submit failure")

    monkeypatch.setattr(renderer_owner, "submit", refuse)
    with pytest.raises(RuntimeError, match="controlled submit failure"):
        await doc_templates._render_document_async("Title", "Body", {})
    assert doc_templates._DOCUMENT_RENDER_EXECUTOR is None
    text, pdf = await doc_templates._render_document_async("Title", "Recovered", {})
    assert text == "Recovered" and pdf.startswith(b"%PDF-")


@pytest.mark.asyncio
async def test_repeated_post_enqueue_failures_retain_no_input_jobs(monkeypatch):
    real_executor = ThreadPoolExecutor
    retired = []
    stale_entries = []

    def failed_pool(**kw):
        executor = real_executor(**kw)
        retired.append(executor)

        def fail_after_enqueue():
            # Retain the callable only as a test probe, not as a live queue.
            item = executor._work_queue.get_nowait()
            stale_entries.append(item)
            executor._work_queue.put(item)
            raise RuntimeError("controlled post enqueue failure")

        monkeypatch.setattr(executor, "_adjust_thread_count", fail_after_enqueue)
        return executor

    monkeypatch.setattr(doc_templates, "ThreadPoolExecutor", failed_pool)
    monkeypatch.setattr(doc_templates, "_DOCUMENT_RENDER_EXECUTOR", None)
    rendered = []
    monkeypatch.setattr(doc_templates, "to_pdf", lambda *_: rendered.append(1) or b"%PDF-test")
    for _ in range(3):
        with pytest.raises(RuntimeError, match="controlled post enqueue failure"):
            await doc_templates._render_document_async("Title", "Detached body", {})
        assert doc_templates._DOCUMENT_RENDER_EXECUTOR is None
    for executor in retired:
        pending = []
        while not executor._work_queue.empty():
            pending.append(executor._work_queue.get_nowait())
        assert all(item is None for item in pending)
        assert executor._shutdown
    # Even a captured delayed entry must refuse its cancelled physical proxy.
    for item in stale_entries:
        execute = item.args[0]
        cells = {
            name: cell.cell_contents
            for name, cell in zip(execute.__code__.co_freevars, execute.__closure__, strict=True)
        }
        assert cells["inputs"] == []
        execute()  # Exercise our proxy even if the executor Future was cancelled.
    assert rendered == []


@pytest.mark.asyncio
async def test_submit_error_after_worker_started_keeps_ownership(
    detached_service, renderer_owner, monkeypatch
):
    started, release, calls = blocked_renderer(monkeypatch)

    class StartedThenRaises:
        work = None

        def submit(self, *args):
            self.work = renderer_owner.submit(*args)
            assert started.wait(2)
            raise RuntimeError("controlled submit error after worker start")

        def shutdown(self, **kw):
            renderer_owner.shutdown(**kw)

    owner = StartedThenRaises()
    monkeypatch.setattr(doc_templates, "_DOCUMENT_RENDER_EXECUTOR", owner)
    try:
        with pytest.raises(RuntimeError, match="controlled submit error after worker start"):
            await generate()
        with pytest.raises(doc_templates.ProjectDocumentRenderingBusy):
            await generate()
        assert calls and detached_service["attachments"] == []
    finally:
        release.set()
        await asyncio.wrap_future(owner.work)
    assert detached_service["attachments"] == []
    await generate()
    assert len(detached_service["attachments"]) == 1


@pytest.mark.asyncio
async def test_worker_context_is_empty_per_job_and_input_context_is_detached(
    renderer_owner, monkeypatch
):
    request_context = contextvars.ContextVar("test_project_document_request", default=None)
    request_context.set("secret-request-identity")
    seen = []

    def render(_title, text):
        seen.append((request_context.get(), text))
        request_context.set("prior-worker-value")
        return b"%PDF-test"

    monkeypatch.setattr(doc_templates, "to_pdf", render)
    holding, release = threading.Event(), threading.Event()

    def occupy_worker():
        holding.set()
        assert release.wait(2)

    blocker = renderer_owner.submit(occupy_worker)
    await wait_started(holding)
    ctx = {"project.code": "reviewed"}
    first = asyncio.create_task(
        doc_templates._render_document_async("Title", "{{project.code}}", ctx)
    )
    try:
        await asyncio.sleep(0)
        ctx["project.code"] = "later-change"
    finally:
        release.set()
        await asyncio.wrap_future(blocker)
    assert (await first)[0] == "reviewed"
    await doc_templates._render_document_async("Title", "Second", {})
    assert seen == [(None, "reviewed"), (None, "Second")]
    assert request_context.get() == "secret-request-identity"


@pytest.mark.asyncio
async def test_real_pdf_bytes_and_text_match_existing_renderer(monkeypatch):
    from reportlab import rl_config

    monkeypatch.setattr(rl_config, "invariant", 1)  # Test-only stable ReportLab metadata.
    body = "Project {{project.code}}\n\n<review> & {{unknown.token}}"
    ctx = {"project.code": "EXACT-1"}
    expected_text = doc_templates.render(body, ctx)
    expected_pdf = doc_templates.to_pdf("Exact title", expected_text)
    text, pdf = await doc_templates._render_document_async("Exact title", body, ctx)
    assert text == expected_text and pdf == expected_pdf


async def seed_project_template(client):
    project = await client.post(
        "/api/v1/masters/projects", json={"code": "RENDER-1", "name": "Render fixture"}
    )
    assert project.status_code == 201, project.text
    template = await client.post(
        "/api/v1/templates",
        json={"name": "Render fixture", "kind": "contract", "body": "Project {{project.code}}"},
    )
    assert template.status_code == 201, template.text
    return project.json()["id"], template.json()["id"]


async def side_effect_counts(db):
    counts = []
    for model in (ProjectDocument, Document, AuditEvent):
        counts.append(await db.scalar(select(func.count()).select_from(model)))
    return tuple(counts)


@pytest.mark.asyncio
async def test_asgi_busy_and_cancel_do_not_save_store_or_audit_and_recover(
    auth_client, db_session, renderer_owner, monkeypatch
):
    project_id, template_id = await seed_project_template(auth_client)
    route = f"/api/v1/masters/projects/{project_id}/generate-document"
    body = {"template_scope": "own", "template_id": template_id}
    before = await side_effect_counts(db_session)
    store = storage.get_storage()
    assert isinstance(store, storage.MemoryStorage)
    before_objects = dict(store._data)
    real_pdf = doc_templates.to_pdf
    started, release, calls = blocked_renderer(monkeypatch)
    active = asyncio.create_task(auth_client.post(route, json=body))
    try:
        await wait_started(started)
        busy = await auth_client.post(route, json=body)
        assert busy.status_code == 503
        assert busy.json()["code"] == "project_document_render_busy"
        assert "No document was saved by this attempt" in busy.json()["detail"]
        assert len(calls) == 1
        assert await side_effect_counts(db_session) == before
        assert store._data == before_objects
        active.cancel()
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active
        still_busy = await auth_client.post(route, json=body)
        assert still_busy.status_code == 503
    finally:
        release.set()
        await drain(renderer_owner)
    assert await side_effect_counts(db_session) == before
    assert store._data == before_objects
    monkeypatch.setattr(doc_templates, "to_pdf", real_pdf)
    recovered = await auth_client.post(route, json=body)
    assert recovered.status_code == 201, recovered.text
    doc = recovered.json()
    assert doc["filename"] == "RENDER-1-contract.pdf" and doc["kind"] == "contract"
    after = await side_effect_counts(db_session)
    assert after == tuple(value + 1 for value in before)
    assert len(store._data) == len(before_objects) + 1
    download = await auth_client.get(
        f"/api/v1/masters/projects/{project_id}/documents/{doc['id']}/download"
    )
    assert download.status_code == 200
    assert "Project RENDER-1" in PdfReader(io.BytesIO(download.content)).pages[0].extract_text()
