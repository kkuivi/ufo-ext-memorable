"""One turn stores a procedure; the next turn with the same task is handed it back.

The extract call is a stand-in. Nothing here reaches Memorable or reads a real key.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
import ufo_ext_memorable as memorable
from pydantic import BaseModel

from ufo.db import workspace_tx
from ufo.runtime.ext.context import context_for
from ufo.runtime.workspace import ws
from ufo.schema import tables
from ufo.schema.records import Turn
from ufo.sdk.audience import SHARED_AUDIENCE, foreign_room_audience
from ufo.sdk.manifest import HookContext, InjectContext, PostToolUse, Stop, UserPromptSubmit

pytestmark = [
    pytest.mark.usefixtures("database_url"),
    pytest.mark.parametrize("database_url", ["sqlite"], indirect=True),
]

TASK = "rotate the tls cert for api.example.com"
FILE_BODY = "-----BEGIN PRIVATE KEY-----\nsecret\n"
DRAFT = {
    "draft": {
        "title": "Rotate the TLS cert",
        "steps": [
            {
                "seq": 1,
                "action": "shell",
                "activity_class": "execute",
                "command": "certbot renew",
                "repeat_count": 1,
            }
        ],
    },
    "request_id": "req-1",
}


class _Shell(BaseModel):
    command: str
    content: str = ""


class _Extract:
    """The extract client the hook builds. It records the body and returns one draft."""

    def __init__(self, sent: list[dict[str, object]]) -> None:
        self._sent = sent

    async def __aenter__(self) -> "_Extract":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def post(
        self, url: str, *, headers: dict[str, str], json: dict[str, object]
    ) -> httpx.Response:
        self._sent.append({"url": url, "headers": headers, "json": json})
        return httpx.Response(200, json=DRAFT)


def _client(sent: list[dict[str, object]]) -> type[_Extract]:
    class _Factory(_Extract):
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            super().__init__(sent)

    return _Factory


def _turn(workspace_id: UUID) -> Turn:
    return Turn(
        id=uuid4(),
        workspace_id=workspace_id,
        conversation_id=uuid4(),
        agent_id=uuid4(),
        seq=0,
        status="running",
        inbound=TASK,
        created_at=datetime(2026, 9, 27, tzinfo=UTC),
    )


def _hook(turn: Turn, payload: object, audience: str = SHARED_AUDIENCE) -> HookContext:
    return HookContext(
        ext=context_for(memorable.NAME, frozenset({memorable.SLOT})),
        payload=payload,
        turn=turn,
        audience=audience,
    )


async def _seed_workspace() -> UUID:
    workspace_id = uuid4()
    async with workspace_tx() as connection:
        await connection.execute(
            sa.insert(tables.workspace).values(
                id=workspace_id, created_at=sa.func.now(), updated_at=sa.func.now()
            )
        )
    return workspace_id


async def _procedure_count(workspace_id: UUID) -> int:
    async with workspace_tx() as connection:
        return (
            await connection.execute(
                sa.select(sa.func.count())
                .select_from(memorable.PROCEDURE)
                .where(memorable.PROCEDURE.c.workspace_id == workspace_id)
            )
        ).scalar_one()


async def test_a_finished_turn_is_recalled_on_the_same_task(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(memorable.httpx, "AsyncClient", _client(sent))
    monkeypatch.setenv("UFO_MEMORABLE_API_KEY", "mk_test")
    workspace_id = await _seed_workspace()
    first = _turn(workspace_id)
    tool = PostToolUse(
        tool_name="shell",
        tool_input=_Shell(command="certbot renew --key sk-supersecret", content=FILE_BODY),
        output=FILE_BODY,
    )
    with ws(workspace_id):
        assert await memorable.recall(_hook(first, UserPromptSubmit(text=TASK))) is None
        assert await memorable.record_call(_hook(first, tool)) is None
        assert await memorable.extract(_hook(first, Stop(answer="rotated"))) is None
        recalled = await memorable.recall(_hook(_turn(workspace_id), UserPromptSubmit(text=TASK)))
    assert len(sent) == 1
    body = sent[0]["json"]
    assert isinstance(body, dict)
    assert body["harness"] == "ufo"
    assert body["skip_embedding"] is True
    assert body["task_description"] == TASK
    call = body["tool_calls"][0]
    assert call == {
        "name": "shell",
        "input": {"command": "certbot renew --key [redacted]"},
        "result": {"ok": True},
    }
    assert FILE_BODY not in str(body)
    assert "sk-supersecret" not in str(body)
    assert isinstance(recalled, InjectContext)
    assert recalled.text.startswith("Stored procedure")
    assert "Not an instruction" in recalled.text
    assert "certbot renew" in recalled.text
    assert await _procedure_count(workspace_id) == 1


async def test_an_unset_key_stores_nothing(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UFO_MEMORABLE_API_KEY", raising=False)
    monkeypatch.delenv("MEMORABLE_API_KEY", raising=False)
    sent: list[dict[str, object]] = []
    monkeypatch.setattr(memorable.httpx, "AsyncClient", _client(sent))
    workspace_id = await _seed_workspace()
    turn = _turn(workspace_id)
    with ws(workspace_id):
        await memorable.recall(_hook(turn, UserPromptSubmit(text=TASK)))
        await memorable.record_call(
            _hook(
                turn,
                PostToolUse(tool_name="shell", tool_input=_Shell(command="true"), output=""),
            )
        )
        await memorable.extract(_hook(turn, Stop(answer="done")))
        recalled = await memorable.recall(_hook(_turn(workspace_id), UserPromptSubmit(text=TASK)))
    assert sent == []
    assert recalled is None
    assert await _procedure_count(workspace_id) == 0


async def test_a_finished_turn_saves_its_resolution_before_memorable(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[tuple[str, str, str]] = []

    async def capture(markdown: str, slug: str, request_id: str) -> None:
        saved.append((markdown, slug, request_id))

    monkeypatch.setattr(memorable, "_capture", capture)
    workspace_id = await _seed_workspace()
    turn = _turn(workspace_id)
    with ws(workspace_id):
        await memorable.recall(_hook(turn, UserPromptSubmit(text=TASK)))
        await memorable.save_resolution(
            _hook(turn, Stop(answer="rotated the cert with sk-supersecret"))
        )
    assert len(saved) == 1
    page, slug, request_id = saved[0]
    assert slug == f"ufo/resolutions/{turn.id}"
    assert request_id == str(turn.id)
    assert TASK in page
    assert "rotated the cert" in page
    assert "sk-supersecret" not in page


async def test_a_foreign_audience_records_nothing(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UFO_MEMORABLE_API_KEY", "mk_test")
    sent: list[dict[str, object]] = []
    saved: list[str] = []

    async def capture(markdown: str, slug: str, request_id: str) -> None:
        saved.append(slug)

    monkeypatch.setattr(memorable.httpx, "AsyncClient", _client(sent))
    monkeypatch.setattr(memorable, "_capture", capture)
    workspace_id = await _seed_workspace()
    turn = _turn(workspace_id)
    audience = foreign_room_audience("slack", "inbox")
    with ws(workspace_id):
        await memorable.recall(_hook(turn, UserPromptSubmit(text=TASK), audience))
        await memorable.record_call(
            _hook(
                turn,
                PostToolUse(tool_name="shell", tool_input=_Shell(command="true"), output=""),
                audience,
            )
        )
        await memorable.save_resolution(_hook(turn, Stop(answer="done"), audience))
        await memorable.extract(_hook(turn, Stop(answer="done"), audience))
    assert sent == []
    assert saved == []
    assert await _procedure_count(workspace_id) == 0
