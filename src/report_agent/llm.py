"""The model surface for stage 2: one fixed call, and one agent loop.

Two shapes, because the plan's six steps are two kinds of work. Triage, cluster
and write are *fixed* calls - one prompt in, one JSON object out, the same
pattern as the existing gates. Carry-forward, deep read and challenge are
genuinely agentic: they decide what to read next based on what they just read,
which is the only way the port recovery in report_plan.md §2.3 can happen at
all.

Both shapes return JSON validated against a schema the caller supplies, and both
count tokens. Neither keeps a conversation between steps: state between steps
lives on disk as artefacts, so a step can be re-run without replaying the one
before it.

Failure policy differs from the gates on purpose. A gate fails *open* because a
dropped item is invisible; the assessor fails *closed* - a step that cannot
produce valid output raises, because a half-assessed week rendered as a report
is worse than no report.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from src.logger import get_logger
from src.report_agent.tools import BundleTools, ToolError, dispatch, tool_specs

logger = get_logger(__name__)


class AssessorError(RuntimeError):
    """A step could not produce usable output. Stage 2 stops rather than guess."""


class ModelConfigError(AssessorError):
    """No key, unknown model, malformed request - fix the setup, do not retry."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    tool_calls: int = 0

    def add(self, response: Any) -> None:
        self.calls += 1
        usage = getattr(response, "usage", None)
        if usage:
            self.input_tokens += usage.input_tokens or 0
            self.output_tokens += usage.output_tokens or 0

    def as_dict(self) -> dict[str, int]:
        return {"model_calls": self.calls, "tool_calls": self.tool_calls,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


@dataclass
class Assessor:
    """A model client pinned to one snapshot, shared by every step of a run."""
    model: str
    timeout: float = 300.0
    usage_by_step: dict[str, Usage] = field(default_factory=dict)
    _client: Any = None

    def __post_init__(self) -> None:
        from dotenv import load_dotenv

        load_dotenv()
        if not os.getenv("OPENAI_API_KEY"):
            raise ModelConfigError(
                "OPENAI_API_KEY is not set; it belongs in .env on the laptop")
        import openai

        self._openai = openai
        self._client = openai.OpenAI(timeout=self.timeout, max_retries=3)

    def usage(self, step: str) -> Usage:
        return self.usage_by_step.setdefault(step, Usage())

    def totals(self) -> dict[str, Any]:
        total = Usage()
        for step_usage in self.usage_by_step.values():
            total.calls += step_usage.calls
            total.tool_calls += step_usage.tool_calls
            total.input_tokens += step_usage.input_tokens
            total.output_tokens += step_usage.output_tokens
        return {"total": total.as_dict(),
                "by_step": {name: u.as_dict() for name, u in self.usage_by_step.items()}}

    # -- fixed call -----------------------------------------------------

    def structured(self, step: str, system: str, user: str, schema: dict[str, Any],
                   *, effort: str = "medium") -> dict[str, Any]:
        """One prompt, one JSON object back. Retried once on an unusable reply."""
        fmt = _json_format(step, schema)
        last_error = ""
        for attempt in (1, 2):
            response = self._create(instructions=system, input=user, effort=effort,
                                    text={"format": fmt})
            self.usage(step).add(response)
            try:
                return _parse(response.output_text)
            except AssessorError as exc:
                last_error = str(exc)
                logger.warning("[assess] %s attempt %d: %s", step, attempt, exc)
                user = (f"{user}\n\nYour previous reply could not be used "
                        f"({last_error}). Reply with valid JSON for the schema only.")
        raise AssessorError(f"{step}: no usable reply after two attempts: {last_error}")

    # -- agent loop -----------------------------------------------------

    def agent(self, step: str, system: str, user: str, schema: dict[str, Any],
              tools: BundleTools, *, effort: str = "high",
              max_tool_calls: int = 30,
              on_call: Callable[[str, dict], None] | None = None) -> dict[str, Any]:
        """Read-decide-read until the model answers, bounded by a call budget.

        The budget is a rail, not a throttle: when it runs out the model is told
        so and asked to answer from what it has read, rather than being cut off
        mid-thought and leaving the step with nothing.
        """
        fmt = _json_format(step, schema)
        spent = 0
        # A budget that is only ever asked politely is not a budget: a model that
        # keeps calling tools after the nudge would loop forever. Two reminders,
        # then the step fails and the run stops, which is visible.
        nudges = 0
        previous_id: str | None = None
        payload: Any = user
        while True:
            response = self._create(instructions=system, input=payload, effort=effort,
                                    text={"format": fmt}, tools=tool_specs(),
                                    previous_response_id=previous_id)
            self.usage(step).add(response)
            previous_id = response.id
            requests = [item for item in response.output
                        if getattr(item, "type", "") == "function_call"]
            if not requests:
                return _parse(response.output_text)

            outputs = []
            for call in requests:
                spent += 1
                self.usage(step).tool_calls += 1
                args = _arguments(call)
                if on_call:
                    on_call(call.name, args)
                outputs.append({"type": "function_call_output", "call_id": call.call_id,
                                "output": _tool_output(tools, call.name, args)})
            if spent >= max_tool_calls:
                nudges += 1
                if nudges > 2:
                    raise AssessorError(
                        f"{step}: kept calling tools after {spent} calls and two "
                        "reminders without answering")
                outputs.append({"role": "user", "content":
                                f"You have used the {max_tool_calls}-call budget for "
                                "this step. Answer now from what you have read, and "
                                "say in scope_limits what you could not check."})
            payload = outputs

    # -- transport ------------------------------------------------------

    def _create(self, *, effort: str | None = None, **kwargs: Any) -> Any:
        options = {key: value for key, value in kwargs.items() if value is not None}
        if effort:
            options["reasoning"] = {"effort": effort}
        try:
            return self._client.responses.create(model=self.model, **options)
        except (self._openai.AuthenticationError, self._openai.PermissionDeniedError,
                self._openai.NotFoundError, self._openai.BadRequestError) as exc:
            raise ModelConfigError(f"{type(exc).__name__}: {exc}") from exc
        except self._openai.OpenAIError as exc:
            raise AssessorError(f"{type(exc).__name__}: {exc}") from exc


def _tool_output(tools: BundleTools, name: str, args: dict[str, Any]) -> str:
    """Run one tool. A bad id is handed back as an error the model can recover from."""
    try:
        result = dispatch(tools, name, args)
    except ToolError as exc:
        logger.info("[assess] tool %s rejected: %s", name, exc)
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    except Exception as exc:  # a defect here must not look like a model refusal
        raise AssessorError(f"tool {name} failed: {type(exc).__name__}: {exc}") from exc
    return json.dumps(result, ensure_ascii=False)


def _arguments(call: Any) -> dict[str, Any]:
    try:
        args = json.loads(call.arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return args if isinstance(args, dict) else {}


def _json_format(step: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "json_schema", "name": step.replace("-", "_"), "strict": True,
            "schema": schema}


def _parse(text: str) -> dict[str, Any]:
    if not (text or "").strip():
        raise AssessorError("empty reply")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AssessorError(f"reply is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AssessorError(f"reply is {type(parsed).__name__}, expected an object")
    return parsed
