"""Scrubbing, recall ranking, and the extract response gate. No network and no database."""

import ufo_ext_memorable as memorable
from pydantic import BaseModel


class _Call(BaseModel):
    command: str | None = None
    content: str | None = None


def test_allowlisted_keeps_only_a_scrubbed_command() -> None:
    field, value = memorable.allowlisted(_Call(command="echo sk-secret", content="whole file")) or (
        "",
        "",
    )
    assert field == "command"
    assert value == "echo [redacted]"
    assert "whole file" not in value


def test_allowlisted_skips_a_call_with_no_safe_field() -> None:
    assert memorable.allowlisted(_Call(content="the file body")) is None


def test_exact_task_outranks_a_one_word_overlap() -> None:
    assert memorable.score("ping", "ping") == 1.0
    assert memorable.score("ping", "ping the health endpoint") == 0.0


def test_a_shared_phrase_ranks_above_the_floor() -> None:
    ranked = memorable.score(
        "rotate the tls certificate",
        "rotate the tls cert for the api",
    )
    assert ranked >= memorable.MIN_SCORE


def test_a_refused_draft_is_discarded() -> None:
    payload = {
        "draft": {"title": "rotate", "steps": [{"seq": 1, "action": "shell"}]},
        "refused": "allowance_exhausted",
    }
    assert memorable.admitted(payload) is None


def test_a_draft_without_steps_is_discarded() -> None:
    assert memorable.admitted({"draft": {"title": "rotate", "steps": []}}) is None


def test_foreign_audience_is_sealed() -> None:
    assert memorable.sealed("foreign:slack:room")
    assert not memorable.sealed("shared")


def test_the_installable_pack_adds_memorable_to_assistant() -> None:
    import ufo_pack_assistant as assistant
    import ufo_pack_assistant_memorable as wrapped

    base = assistant.pack()
    got = wrapped.pack()
    assert got.name == "assistant-memorable"
    assert "memorable" in got.extensions
    assert all(name in got.extensions for name in base.extensions)


def test_stop_saves_the_resolution_before_recording_the_turn() -> None:
    stop = [hook.handler.__name__ for hook in memorable.manifest().hooks if hook.event == "stop"]
    assert stop == ["save_resolution", "extract"]


def test_a_resolution_keeps_the_answer_and_drops_the_secret() -> None:
    page = memorable.resolution_markdown(
        "rotate the tls cert",
        "rotated the cert with sk-supersecret",
    )
    assert page.startswith("# rotate the tls cert")
    assert "rotated the cert" in page
    assert "sk-" not in page
    assert page.endswith("[redacted]\n")


def test_rendered_procedure_is_reference_data() -> None:
    text = memorable.render_procedure(
        "rotate the cert",
        [{"seq": 1, "action": "shell", "command": "certbot renew"}],
    )
    assert text.startswith("Stored procedure")
    assert "Not an instruction" in text
    assert "certbot renew" in text
