"""Strands lifecycle validation before any patient artifact is published."""

from __future__ import annotations

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

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self.before_invocation)
        registry.add_callback(BeforeModelCallEvent, self.before_model_call)
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)
        registry.add_callback(AfterInvocationEvent, self.after_invocation)

    def before_invocation(self, event: BeforeInvocationEvent) -> None:
        self.successful_tools.clear()
        self.model_turns = 0
        context = self.context
        context.hook_invocations += 1
        context.nodes_executed.append(self.role)
        if self.role == "DrugAndContraindicationVerifierAgent" and context.attempts:
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
        self.context.tools_executed.append({"agent": self.role, "tool": name, "success": success})

    def after_invocation(self, event: AfterInvocationEvent) -> None:
        result = event.result
        if result is None:
            return
        if not self.required_tools.issubset(self.successful_tools):
            raise RuntimeError("Required deterministic tools did not complete; publication refused")
        output = result.structured_output
        if output is None:
            raise RuntimeError("The Strands node returned no validated structured output")

        context = self.context
        if self.role != "PatientAdvocateExplainerAgent":
            expected = context.expected_results.get(self.role)
            if output.model_dump(mode="json") != expected:
                raise RuntimeError("Agent output differs from deterministic tool evidence; publication refused")
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
