"""
Batch deploy — fire multiple pipelines in parallel, track each independently.

Kept separate from the single-app Deploy page because the concerns differ: this
is about orchestrating N triggers at once and surfacing a live status grid, not
about one carefully-parameterised deploy.

State lives in st.session_state (keyed per batch) so the grid survives the reruns
Streamlit does on every interaction.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from backend import trigger_pipeline, record_deployment

MAX_PARALLEL = 6


@dataclass
class JobStatus:
    app_name: str
    state: str = "queued"          # queued | running | success | failed
    pipeline_id: int | None = None
    message: str = ""
    web_url: str = ""
    started_at: str = ""
    finished_at: str = ""


@dataclass
class Batch:
    branch: str
    params: dict
    jobs: dict = field(default_factory=dict)     # app_name -> JobStatus
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def done(self):
        return all(j.state in ("success", "failed") for j in self.jobs.values())

    @property
    def summary(self):
        s = sum(1 for j in self.jobs.values() if j.state == "success")
        f = sum(1 for j in self.jobs.values() if j.state == "failed")
        return {"total": len(self.jobs), "success": s, "failed": f,
                "pending": len(self.jobs) - s - f}


def new_batch(app_names: list, branch: str, params: dict) -> Batch:
    b = Batch(branch=branch, params=params)
    for name in app_names:
        b.jobs[name] = JobStatus(app_name=name)
    return b


def _run_one(app_name: str, branch: str, params: dict) -> JobStatus:
    js = JobStatus(app_name=app_name, state="running",
                   started_at=datetime.now().isoformat(timespec="seconds"))
    result = trigger_pipeline(app_name, branch, params)
    js.finished_at = datetime.now().isoformat(timespec="seconds")

    if result.get("ok"):
        js.state = "success"
        js.pipeline_id = result.get("pipeline_id")
        js.web_url = result.get("web_url", "")
        js.message = f"Pipeline #{js.pipeline_id} created"
        record_deployment(app_name, "Success")
    else:
        js.state = "failed"
        js.message = result.get("error", "trigger failed")
        record_deployment(app_name, "Failed")
    return js


def execute_batch(batch: Batch) -> Batch:
    """
    Fire all jobs in parallel and collect results.

    Returns once every trigger has returned. Triggering is quick (an API call);
    the pipelines themselves then run in CI — this tracks the trigger outcome,
    and the Overview/diagnostics pages track what the pipeline does next.
    """
    names = list(batch.jobs.keys())
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(names))) as pool:
        futures = {pool.submit(_run_one, n, batch.branch, batch.params): n for n in names}
        for fut in futures:
            n = futures[fut]
            try:
                batch.jobs[n] = fut.result()
            except Exception as e:
                batch.jobs[n].state = "failed"
                batch.jobs[n].message = str(e)
    return batch


# ============================================================ Excel bulk deploy
# Rows come from an uploaded spreadsheet; each carries its own CI target. Params
# are shared (from the selected channel). Runs in waves of `batch_size` — apps
# within a wave go in parallel, waves run in sequence.

from ci_triggers import trigger as ci_trigger


@dataclass
class BulkJob:
    app_name: str
    ci: str                        # gitlab | jenkins
    row: dict = field(default_factory=dict)
    state: str = "queued"          # queued | running | success | failed | skipped
    ref_id: str = ""               # pipeline id / build number
    deploy_id: str = ""            # dashboard's own ID — used for diagnosis
    message: str = ""
    web_url: str = ""
    wave: int = 0


@dataclass
class BulkRun:
    channel: str
    params: dict
    batch_size: int
    on_failure: str = "continue"   # continue | stop | pause
    jobs: list = field(default_factory=list)   # list[BulkJob]
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    stopped: bool = False

    @property
    def summary(self):
        c = {"success": 0, "failed": 0, "queued": 0, "running": 0, "skipped": 0}
        for j in self.jobs:
            c[j.state] = c.get(j.state, 0) + 1
        c["total"] = len(self.jobs)
        return c

    @property
    def waves(self):
        """Group jobs into ordered waves of batch_size."""
        out = []
        for i in range(0, len(self.jobs), self.batch_size):
            out.append(self.jobs[i:i + self.batch_size])
        return out


def new_bulk_run(rows, channel, params, batch_size, on_failure="continue") -> BulkRun:
    """Build a run from validated Excel rows (only _valid rows are included)."""
    run = BulkRun(channel=channel, params=params, batch_size=max(1, batch_size),
                  on_failure=on_failure)
    wave_size = run.batch_size
    for idx, r in enumerate([r for r in rows if r.get("_valid")]):
        run.jobs.append(BulkJob(
            app_name=r["app_name"], ci=r["ci"], row=r,
            wave=idx // wave_size,
        ))
    return run


def _run_bulk_one(job: BulkJob, params: dict) -> BulkJob:
    import uuid
    job.state = "running"
    deploy_id = "dep-" + uuid.uuid4().hex[:10]
    job.deploy_id = deploy_id
    result = ci_trigger(job.row, params)
    if result.get("ok"):
        job.state = "success"
        job.ref_id = str(result.get("pipeline_id", ""))
        job.web_url = result.get("web_url", "")
        job.message = f"{result['ci']} #{job.ref_id} ({result.get('status','')})"
        record_deployment(job.app_name, "Success", deploy_id=deploy_id,
                          ci=result.get("ci"), ref_id=job.ref_id, web_url=job.web_url)
    else:
        job.state = "failed"
        job.message = result.get("error", "trigger failed")
        record_deployment(job.app_name, "Failed", deploy_id=deploy_id,
                          ci=result.get("ci"), ref_id=str(result.get("pipeline_id", "")),
                          web_url=result.get("web_url", ""))
    return job


def execute_wave(run: BulkRun, wave_index: int) -> BulkRun:
    """
    Run a single wave in parallel. Returns after the wave completes.
    The UI drives waves one at a time so it can render between them and honor
    stop/pause. Respects on_failure='stop' by marking remaining jobs skipped.
    """
    if run.stopped:
        return run

    wave = run.waves[wave_index]
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(wave))) as pool:
        futures = {pool.submit(_run_bulk_one, j, run.params): j for j in wave}
        for fut in futures:
            j = futures[fut]
            try:
                fut.result()
            except Exception as e:
                j.state = "failed"
                j.message = str(e)

    if run.on_failure == "stop" and any(j.state == "failed" for j in wave):
        run.stopped = True
        for later in run.jobs:
            if later.state == "queued":
                later.state = "skipped"
                later.message = "skipped — earlier wave failed"
    return run
