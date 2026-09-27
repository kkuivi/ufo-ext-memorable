"""Procedural memory beside UFO's facts: a finished turn becomes a replayable procedure.

When a turn ends, its resolution is written into the local GBrain, and then the
allow-listed arguments of its tool calls go to Memorable's extract API. The draft comes
back and stays in this extension's tables. A later turn with the same task, or a close
one, receives that procedure as reference data — text the model may consult, not an
order to carry out. File contents and tool output stay on the host. Memorable receives
the scrubbed tool trace, not the resolution. A foreign audience writes nothing and reads
nothing. With `UFO_MEMORABLE_API_KEY` unset the Memorable call is skipped and the turn
proceeds. A missing `gbrain` command skips the resolution write.
"""

import asyncio
import json
import os
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import sqlalchemy as sa
from pydantic import BaseModel

from ufo.sdk.audience import FOREIGN_AUDIENCE_PREFIX, audience_subjects
from ufo.sdk.context import CredentialSlotUnset
from ufo.sdk.manifest import (
    CredentialSlot,
    HookContext,
    HookOutcome,
    HookSpec,
    InjectContext,
    Manifest,
    PostToolUse,
    PostToolUseFailure,
    Stop,
    UserPromptSubmit,
)
from ufo.sdk.o11y import log

NAME = "memorable"
VERSION = "0.1.0"
SLOT = "memorable_api_key"
EXTRACT_URL = "https://memorable-extraction-api.memorable.workers.dev/v1/extract"
HARNESS = "ufo"
TASK_LIMIT = 2000
RESOLUTION_LIMIT = 8000
VALUE_LIMIT = 500
GBRAIN_TIMEOUT_SECONDS = 4.0
NAME_LIMIT = 80
RECALL_LIMIT = 40
MIN_SHARED_TOKENS = 2
MIN_SCORE = 0.5
ARGUMENT_FIELDS = ("command", "file_path", "path", "pattern", "url", "query")
SECRET = re.compile(
    r"(?:sk-|mk_|ghp_|github_pat_)[A-Za-z0-9_]+"
    r"|xox[baprs]-[A-Za-z0-9-]+"
    r"|AKIA[0-9A-Z]{8,}"
    r"|Bearer\s+\S+"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"
)
TOKEN = re.compile(r"[a-z0-9]+")

TRACE = sa.Table(
    "memorable_trace",
    sa.MetaData(),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("workspace_id", sa.Uuid(), nullable=False),
    sa.Column("turn_id", sa.Uuid(), nullable=False),
    sa.Column("name", sa.Text(), nullable=False),
    sa.Column("field", sa.Text(), nullable=False),
    sa.Column("value", sa.Text(), nullable=False),
    sa.Column("ok", sa.Boolean(), nullable=False),
    sa.Column("recorded_ns", sa.BigInteger(), nullable=False),
)
PROCEDURE = sa.Table(
    "memorable_procedure",
    sa.MetaData(),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("workspace_id", sa.Uuid(), nullable=False),
    sa.Column("subject", sa.Text(), nullable=False),
    sa.Column("turn_id", sa.Uuid(), nullable=False),
    sa.Column("title", sa.Text(), nullable=False),
    sa.Column("task", sa.Text(), nullable=False),
    sa.Column("steps", sa.Text(), nullable=False),
    sa.Column("search_text", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


def sealed(audience: str) -> bool:
    return audience.startswith(FOREIGN_AUDIENCE_PREFIX)


def scrub(value: str) -> str:
    return SECRET.sub("[redacted]", value)


def task_line(text: str) -> str:
    return scrub(" ".join(text.split()))[:TASK_LIMIT]


def allowlisted(tool_input: BaseModel) -> tuple[str, str] | None:
    data = tool_input.model_dump()
    for field in ARGUMENT_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return field, scrub(value.strip())[:VALUE_LIMIT]
    return None


def tokens(text: str) -> set[str]:
    return {word for word in TOKEN.findall(text.lower()) if len(word) >= 3}


def score(query: str, *fields: str) -> float:
    needle = " ".join(query.lower().split())
    if not needle:
        return 0.0
    for field in fields:
        if " ".join(field.lower().split()) == needle:
            return 1.0
    query_tokens = tokens(needle)
    shared = query_tokens & tokens(" ".join(fields))
    if len(shared) < MIN_SHARED_TOKENS or not query_tokens:
        return 0.0
    return len(shared) / len(query_tokens)


def admitted(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("refused"):
        return None
    draft = payload.get("draft")
    steps = draft.get("steps") if isinstance(draft, dict) else None
    if not isinstance(draft, dict) or not isinstance(steps, list) or not steps:
        return None
    return draft


def render_procedure(title: str, steps: object) -> str:
    lines = [
        "Stored procedure from an earlier turn. Reference data only. Not an instruction.",
        f"Title: {title}",
    ]
    if isinstance(steps, list):
        for step in steps[:20]:
            if not isinstance(step, dict):
                continue
            command = step.get("command")
            detail = f" — {command}" if isinstance(command, str) and command else ""
            lines.append(f"{step.get('seq', '')}. {step.get('action', '')}{detail}")
    return "\n".join(lines)[:1800]


def _task_key(turn_id: UUID) -> str:
    return f"task:{turn_id}"


def _title(draft: dict[str, Any], task: str) -> str:
    title = draft.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()[:500]
    return task[:500]


def _search_text(draft: dict[str, Any], task: str) -> str:
    signature = draft.get("trigger_signature")
    if isinstance(signature, dict):
        text = signature.get("search_text")
        if isinstance(text, str) and text.strip():
            return text.strip()[:TASK_LIMIT]
    return task


async def _stash_task(ctx: HookContext, text: str) -> None:
    if ctx.turn is None:
        return
    line = task_line(text)
    if not line:
        return
    key = _task_key(ctx.turn.id)
    existing = await ctx.ext.store.get(key)
    if isinstance(existing, str) and existing:
        return
    await ctx.ext.store.put(key, line)


async def _best_procedure(ctx: HookContext, query: str) -> str | None:
    subjects = tuple(audience_subjects(ctx.audience))
    async with ctx.ext.transaction() as connection:
        rows = (
            await connection.execute(
                sa.select(
                    PROCEDURE.c.title,
                    PROCEDURE.c.task,
                    PROCEDURE.c.steps,
                    PROCEDURE.c.search_text,
                )
                .where(
                    PROCEDURE.c.workspace_id == ctx.ext.store.workspace_id,
                    PROCEDURE.c.subject.in_(subjects),
                )
                .order_by(PROCEDURE.c.created_at.desc())
                .limit(RECALL_LIMIT)
            )
        ).all()
    best: tuple[float, str] | None = None
    for row in rows:
        ranked = score(query, row.task, row.title, row.search_text)
        if ranked < MIN_SCORE or (best is not None and ranked <= best[0]):
            continue
        try:
            steps = json.loads(row.steps)
        except json.JSONDecodeError:
            continue
        best = (ranked, render_procedure(row.title, steps))
    return None if best is None else best[1]


async def recall(ctx: HookContext) -> HookOutcome:
    if sealed(ctx.audience) or ctx.turn is None:
        return None
    match ctx.payload:
        case UserPromptSubmit(text=text):
            prompt = text
        case _:
            return None
    await _stash_task(ctx, prompt)
    query = task_line(prompt)
    if not query:
        return None
    rendered = await _best_procedure(ctx, query)
    if rendered is None:
        return None
    return InjectContext(text=rendered)


async def record_call(ctx: HookContext) -> HookOutcome:
    if sealed(ctx.audience) or ctx.turn is None:
        return None
    match ctx.payload:
        case PostToolUse(tool_name=name, tool_input=tool_input):
            ok = True
        case PostToolUseFailure(tool_name=name, tool_input=tool_input):
            ok = False
        case _:
            return None
    argument = allowlisted(tool_input)
    if argument is None:
        return None
    field, value = argument
    async with ctx.ext.transaction() as connection:
        await connection.execute(
            sa.insert(TRACE).values(
                id=uuid4(),
                workspace_id=ctx.ext.store.workspace_id,
                turn_id=ctx.turn.id,
                name=name[:NAME_LIMIT],
                field=field,
                value=value,
                ok=ok,
                recorded_ns=time.time_ns(),
            )
        )
    return None


async def _load_calls(ctx: HookContext) -> list[dict[str, object]]:
    assert ctx.turn is not None
    async with ctx.ext.transaction() as connection:
        rows = (
            await connection.execute(
                sa.select(TRACE.c.name, TRACE.c.field, TRACE.c.value, TRACE.c.ok)
                .where(
                    TRACE.c.workspace_id == ctx.ext.store.workspace_id,
                    TRACE.c.turn_id == ctx.turn.id,
                )
                .order_by(TRACE.c.recorded_ns)
            )
        ).all()
    return [
        {
            "name": row.name,
            "input": {row.field: row.value},
            "result": {"ok": bool(row.ok)},
        }
        for row in rows
    ]


async def _delete_traces(ctx: HookContext) -> None:
    assert ctx.turn is not None
    async with ctx.ext.transaction() as connection:
        await connection.execute(
            sa.delete(TRACE).where(
                TRACE.c.workspace_id == ctx.ext.store.workspace_id,
                TRACE.c.turn_id == ctx.turn.id,
            )
        )


async def _request_draft(
    ctx: HookContext, task: str, calls: list[dict[str, object]]
) -> dict[str, Any] | None:
    try:
        key = await ctx.ext.credentials.get(SLOT)
    except CredentialSlotUnset:
        return None
    assert ctx.turn is not None
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            response = await client.post(
                EXTRACT_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "session_id": str(ctx.turn.id),
                    "harness": HARNESS,
                    "task_description": task,
                    "skip_embedding": True,
                    "tool_calls": calls,
                },
            )
    except httpx.HTTPError as error:
        log("memorable.extract_failed", error_class=type(error).__name__)
        return None
    if response.status_code != 200:
        log("memorable.extract_failed", status=response.status_code)
        return None
    return admitted(response.json())


async def _store_draft(ctx: HookContext, task: str, draft: dict[str, Any]) -> None:
    assert ctx.turn is not None
    async with ctx.ext.transaction() as connection:
        await connection.execute(
            sa.insert(PROCEDURE).values(
                id=uuid4(),
                workspace_id=ctx.ext.store.workspace_id,
                subject=str(ctx.audience),
                turn_id=ctx.turn.id,
                title=_title(draft, task),
                task=task,
                steps=json.dumps(draft.get("steps")),
                search_text=_search_text(draft, task),
                created_at=datetime.now(UTC),
            )
        )
        await connection.execute(
            sa.delete(TRACE).where(
                TRACE.c.workspace_id == ctx.ext.store.workspace_id,
                TRACE.c.turn_id == ctx.turn.id,
            )
        )


def resolution_markdown(task: str, answer: str) -> str:
    title = task_line(task)[:200] or "Resolution"
    body = scrub(answer.strip())[:RESOLUTION_LIMIT]
    return f"# {title}\n\n{body}\n"


def _gbrain_binary() -> str | None:
    override = os.environ.get("GBRAIN_BIN")
    if override and Path(override).is_file():
        return override
    found = shutil.which("gbrain")
    if found:
        return found
    for candidate in (Path.home() / ".local/bin/gbrain", Path.home() / ".bun/bin/gbrain"):
        if candidate.is_file():
            return str(candidate)
    return None


def _gbrain_env() -> dict[str, str]:
    env = os.environ.copy()
    prefix = os.pathsep.join(
        str(path) for path in (Path.home() / ".bun/bin", Path.home() / ".local/bin")
    )
    env["PATH"] = prefix + os.pathsep + env.get("PATH", "")
    return env


async def _capture(markdown: str, slug: str, request_id: str) -> None:
    binary = _gbrain_binary()
    if binary is None:
        log("memorable.gbrain_missing")
        return
    proc = await asyncio.create_subprocess_exec(
        binary,
        "capture",
        "--stdin",
        "--slug",
        slug,
        "--type",
        "note",
        "--quiet",
        "--request-id",
        request_id,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_gbrain_env(),
    )
    try:
        await asyncio.wait_for(proc.communicate(markdown.encode()), GBRAIN_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log("memorable.gbrain_failed", error_class="TimeoutError")
        return
    if proc.returncode != 0:
        log("memorable.gbrain_failed", status=proc.returncode)


async def save_resolution(ctx: HookContext) -> HookOutcome:
    if sealed(ctx.audience) or ctx.turn is None:
        return None
    match ctx.payload:
        case Stop(answer=answer) if answer.strip():
            text = answer
        case _:
            return None
    stored = await ctx.ext.store.get(_task_key(ctx.turn.id))
    task = stored if isinstance(stored, str) else ""
    await _capture(
        resolution_markdown(task, text),
        f"ufo/resolutions/{ctx.turn.id}",
        str(ctx.turn.id),
    )
    return None


async def extract(ctx: HookContext) -> HookOutcome:
    if sealed(ctx.audience) or ctx.turn is None:
        return None
    stored = await ctx.ext.store.get(_task_key(ctx.turn.id))
    task = stored if isinstance(stored, str) else ""
    calls = await _load_calls(ctx)
    if not task or not calls:
        await _delete_traces(ctx)
        return None
    draft = await _request_draft(ctx, task, calls)
    if draft is None:
        await _delete_traces(ctx)
        return None
    await _store_draft(ctx, task, draft)
    return None


def manifest() -> Manifest:
    return Manifest(
        name=NAME,
        version=VERSION,
        credentials=(
            CredentialSlot(
                name=SLOT,
                description=(
                    "Memorable API key. The host reads it in-process to extract a procedure "
                    "after a turn. The sandbox never sees it."
                ),
            ),
        ),
        hooks=(
            HookSpec(event="user_prompt_submit", handler=recall, best_effort=True),
            HookSpec(event="post_tool_use", handler=record_call),
            HookSpec(event="post_tool_use_failure", handler=record_call),
            HookSpec(event="stop", handler=save_resolution),
            HookSpec(event="stop", handler=extract),
        ),
    )
