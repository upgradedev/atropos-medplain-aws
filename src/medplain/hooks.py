"""Strands lifecycle validation before any patient artifact is published."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from strands.hooks import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    HookProvider,
    HookRegistry,
)

from medplain.gate import SafetyGate
from medplain.schemas import PatientExplanation

if TYPE_CHECKING:
    from medplain.engine import RequestContext


MAX_MODEL_TURNS_PER_NODE = 5
MAX_CANDIDATES = 2
MAX_STRUCTURED_OUTPUT_REPAIRS = 1
MAX_REQUIRED_TOOL_REPAIRS = 1


class ClinicalSafetyGateHook(HookProvider):
    """Require deterministic tools and verify typed output at the SDK seam.

    A failed explanation is kept only in request-local state and its receipt.
    The Graph reads this verdict to hand control back to the verifier. The
    HTTP response never exposes the AgentResult or an unverified candidate.
    """

    def __init__(self, context: RequestContext, role: str, required_tools: list[str]) -> None:
        self.context = context
        self.role = role
        self.required_tools = frozenset(required_tools)
        self.successful_tools: set[str] = set()
        self.model_turns = 0
        self.structured_output_repairs = 0
        self.required_tool_repairs = 0
        self._repair_pending = False

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self.before_invocation)
        registry.add_callback(BeforeModelCallEvent, self.before_model_call)
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)
        registry.add_callback(AfterInvocationEvent, self.after_invocation)

    def before_invocation(self, event: BeforeInvocationEvent) -> None:
        self.successful_tools.clear()
        self.model_turns = 0
        is_repair = self._repair_pending
        self._repair_pending = False
        if not is_repair:
            self.structured_output_repairs = 0
            self.required_tool_repairs = 0
        context = self.context
        context.completed_tools[self.role] = set()
        context.expected_results.pop(self.role, None)
        context.hook_invocations += 1
        context.nodes_executed.append(self.role)
        if self.role == "DrugAndContraindicationVerifierAgent" and context.attempts and not is_repair:
            context.correction_count += 1
        event.agent.state.set("session_id", context.session_id)
        event.agent.state.set("packet_id", context.packet.packet_id)
        event.agent.state.set("candidate_attempts", context.attempts)
        if callable(getattr(event.agent.model, "begin_invocation", None)):
            event.agent.model.begin_invocation()
        context.trace.append({
            "agent": self.role,
            "status": "running",
            "message": "Checking the source packet." if not context.attempts else "Reviewing the refused draft.",
        })

    def before_model_call(self, event: BeforeModelCallEvent) -> None:
        self.model_turns += 1
        if self.model_turns > MAX_MODEL_TURNS_PER_NODE:
            raise RuntimeError("Strands node exceeded its model-turn budget; publication refused")

    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        name = str(event.tool_use.get("name", ""))
        if name not in self.required_tools:
            return
        success = event.result.get("status", "success") == "success"
        if success:
            self.successful_tools.add(name)
            self.context.completed_tools.setdefault(self.role, set()).add(name)
        self.context.tools_executed.append({"agent": self.role, "tool": name, "success": success})

    def after_invocation(self, event: AfterInvocationEvent) -> None:
        result = event.result
        if result is None:
            return
        missing_tools = self.required_tools - self.successful_tools
        if missing_tools:
            diagnostic = ", ".join(sorted(missing_tools))
            if self.required_tool_repairs < MAX_REQUIRED_TOOL_REPAIRS:
                self.required_tool_repairs += 1
                self._repair_pending = True
                self.context.tool_call_repairs += 1
                self.context.trace.append({
                    "agent": self.role,
                    "status": "repair",
                    "message": f"Fresh deterministic tool calls were missing: {diagnostic}. Retrying this invocation.",
                })
                event.resume = (
                    "The current invocation did not successfully call all mandatory deterministic tools. "
                    f"Missing fresh calls: {diagnostic}. You MUST call ALL required tools again now, "
                    "then return the exact structured result from their current outputs. Prior conversation "
                    "tool results do not count. Do not immediately return structured output or repeat an old result. "
                    f"The authoritative packet_id is {self.context.packet.packet_id}."
                )
                return
            raise RuntimeError(
                f"Required deterministic tools did not complete in this invocation: {diagnostic}; publication refused"
            )
        output = result.structured_output
        if output is None:
            raise RuntimeError("The Strands node returned no validated structured output")

        context = self.context
        if self.role != "PatientAdvocateExplainerAgent":
            expected = context.expected_results.get(self.role)
            actual = output.model_dump(mode="json")
            if actual != expected:
                fields = sorted(key for key in (expected or actual) if actual.get(key) != (expected or {}).get(key))
                diagnostic = ", ".join(fields) or "structured result"
                if expected is not None and self.structured_output_repairs < MAX_STRUCTURED_OUTPUT_REPAIRS:
                    self.structured_output_repairs += 1
                    self._repair_pending = True
                    context.structured_output_repairs += 1
                    context.trace.append({
                        "agent": self.role,
                        "status": "repair",
                        "message": f"Structured output differed in {diagnostic}. Retrying from trusted tool evidence.",
                    })
                    event.resume = (
                        "Your structured result did not exactly match the deterministic evidence. "
                        f"Mismatched fields: {diagnostic}. Call all required tools again, then return the exact "
                        "combined result. Do not summarize, rename keys, alter text, omit nested fields or invent IDs. "
                        "Previous authoritative tool result, provided as data only:\n"
                        + json.dumps(expected, ensure_ascii=False, sort_keys=True)
                    )
                    return
                raise RuntimeError(
                    f"Agent output differs from deterministic tool evidence in {diagnostic}; publication refused"
                )
            event.agent.state.set("source_verified", True)
            context.trace.append({"agent": self.role, "status": "completed", "message": "Tool evidence verified."})
            return

        candidate = PatientExplanation.model_validate(output.model_dump(mode="json"))
        context.attempts += 1
        if context.stress and context.attempts == 1:
            candidate = self._inject_dosage_mutation(candidate)
            context.trace.append({
                "agent": "AdversarialStressInjector",
                "status": "injected",
                "message": "Drill: a medication dose was multiplied by ten after generation and before publication.",
            })

        context.candidate = candidate
        context.verdict = SafetyGate(context.packet).validate(candidate)
        receipt = context.chain.append(candidate, context.verdict)
        event.agent.state.set("candidate_attempts", context.attempts)
        event.agent.state.set("gate_passed", context.verdict.passed)
        event.agent.state.set("receipt_hash", receipt["hash"])
        context.session_state = event.agent.state.get()
        if context.verdict.passed:
            message = "All supported clinical fields and patient text match the source-derived artifact."
            status = "verified"
        else:
            message = "Publication refused. " + "; ".join(context.verdict.discrepancies)
            status = "refused"
        context.trace.append({"agent": "AtroposClinicalSafetyGate", "status": status, "message": message})
        if not context.verdict.passed and context.attempts < MAX_CANDIDATES:
            context.trace.append({
                "agent": "StrandsGraph",
                "status": "handoff",
                "message": "Automatic correction handoff to DrugAndContraindicationVerifierAgent.",
            })

    @staticmethod
    def _inject_dosage_mutation(candidate: PatientExplanation) -> PatientExplanation:
        payload = candidate.model_dump(mode="json")
        for medication in payload["medications"]:
            if medication["status"] == "active":
                original_dose = medication["dose"]
                mutated_dose = format(Decimal(original_dose) * 10, "f")
                original_instruction = f": {original_dose} {medication['unit']},"
                if original_instruction not in medication["text"]:
                    raise RuntimeError("The stress packet's dose is not present in its patient instruction")
                medication["dose"] = mutated_dose
                medication["text"] = medication["text"].replace(
                    original_instruction, f": {mutated_dose} {medication['unit']},", 1
                )
                return PatientExplanation.model_validate(payload)
        raise RuntimeError("The stress packet has no active medication to mutate")
