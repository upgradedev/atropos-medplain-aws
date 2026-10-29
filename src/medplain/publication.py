"""Request-bound references to immutable tool artifacts, before the safety gate."""

from medplain.clinical import render_explanation
from medplain.gate import canonical_hash
from medplain.schemas import PATIENT_PRESENTATION, ClinicalPacket, PatientExplanation, PatientPublicationPlan


def publication_artifact_hash(artifact: PatientExplanation) -> str:
    return canonical_hash({"structured": artifact.model_dump(mode="json"), "text": render_explanation(artifact)})


def publication_binding(packet: ClinicalPacket, execution_id: str, request_nonce: str,
                        invocation_index: int, artifact_hash: str, artifact_nonce: str) -> dict:
    return {
        "request_nonce": request_nonce, "execution_id": execution_id,
        "packet_id": packet.packet_id, "source_hash": canonical_hash(packet.model_dump(mode="json")),
        "invocation_index": invocation_index, "artifact_hash": artifact_hash,
        "artifact_nonce": artifact_nonce,
        "presentation": PATIENT_PRESENTATION,
    }


def plan_for_binding(binding: dict) -> dict:
    return PatientPublicationPlan(
        packet_id=binding["packet_id"], artifact_ref="draft-" + canonical_hash(binding)[:24],
        presentation=PATIENT_PRESENTATION,
    ).model_dump(mode="json")


def resolve_publication(snapshot: str | None, snapshot_hash: str | None, saved_binding: dict | None,
                        plan: PatientPublicationPlan, packet: ClinicalPacket, execution_id: str,
                        request_nonce: str, invocation_index: int, artifact_nonce: str | None) -> PatientExplanation:
    """Resolve only this invocation's exact plan; never correct a clinical field.

    JSON is an immutable private snapshot. A new typed object is returned for
    fault injection and the existing complete clinical gate. Reference validity
    alone is neither clinical verification nor an authenticity claim.
    """
    if (not isinstance(plan, PatientPublicationPlan) or snapshot is None or snapshot_hash is None
            or artifact_nonce is None):
        raise ValueError("A fresh typed patient publication plan and tool artifact are required")
    expected_binding = publication_binding(packet, execution_id, request_nonce, invocation_index,
                                           snapshot_hash, artifact_nonce)
    if saved_binding != expected_binding or plan.model_dump(mode="json") != plan_for_binding(expected_binding):
        raise ValueError("Patient publication plan does not match this source, request and invocation")
    artifact = PatientExplanation.model_validate_json(snapshot)
    if publication_artifact_hash(artifact) != snapshot_hash:
        raise ValueError("The immutable patient tool artifact changed before publication")
    return artifact
