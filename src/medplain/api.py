"""Anonymous synthetic-only demo. No upload, patient record storage or login."""

import json
import logging
import os
from pathlib import Path
from threading import BoundedSemaphore
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mangum import Mangum
from starlette.concurrency import run_in_threadpool

from medplain import __version__
from medplain.clinical import render_explanation
from medplain.fixtures import get_packet, get_packets
from medplain.gate import SafetyGate, canonical_hash, verify_receipts
from medplain.schemas import ExplainRequest, PatientExplanation, ReceiptVerificationRequest

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
ACTIVE = BoundedSemaphore(2)
app = FastAPI(title="Atropos MedPlain", version=__version__, docs_url=None, redoc_url=None)


def deployed_commit() -> str:
    if os.environ.get("MEDPLAIN_COMMIT"):
        return os.environ["MEDPLAIN_COMMIT"]
    build_file = Path(__file__).parent / "_build.json"
    return json.loads(build_file.read_text())["commit"] if build_file.exists() else "development"


@app.middleware("http")
async def public_boundary(request: Request, call_next):
    # Only fixed sample selectors reach Bedrock. No submitted clinical documents.
    if request.method == "POST":
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return JSONResponse(status_code=415, content={"detail": "Send a JSON sample selector only."})
        declared = request.headers.get("content-length", "0")
        if not declared.isdecimal() or int(declared) > 131072:
            return JSONResponse(status_code=413, content={"detail": "Request is too large for this demo."})
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 131072:
                return JSONResponse(status_code=413, content={"detail": "Request is too large for this demo."})
            chunks.append(chunk)
        request._body = b"".join(chunks)
    response = await call_next(request)
    response.headers.update({
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        ),
    })
    return response


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {
        "status": "ok", "version": __version__, "commit": deployed_commit(),
        "model_mode": os.getenv("MEDPLAIN_MODEL_MODE", "fixture"),
        "model_id": os.getenv("MEDPLAIN_BEDROCK_MODEL_ID", "amazon.nova-micro-v1:0"),
        "data": "synthetic packets only", "session_storage": "request-scoped memory",
        "scope": "Source fidelity in a constrained demo profile; not clinical validation.",
    }


@app.get("/api/packets")
def packets():
    return {"packets": [{key: getattr(packet, key)
                         for key in ["packet_id", "title", "subtitle", "patient", "adversarial"]}
                        for packet in get_packets()]}


@app.get("/api/packets/{packet_id}")
def packet_detail(packet_id: str):
    try:
        return get_packet(packet_id).model_dump()
    except KeyError:
        raise HTTPException(404, "Choose one of the synthetic sample packets.") from None


@app.post("/api/explain")
async def explain(body: ExplainRequest):
    try:
        packet = get_packet(body.packet_id)
    except KeyError:
        raise HTTPException(404, "This synthetic packet does not exist.") from None
    mode = os.getenv("MEDPLAIN_MODEL_MODE", "fixture")
    if mode not in {"fixture", "bedrock"}:
        raise HTTPException(503, "The agent runtime is not configured. No explanation was published.")
    if not ACTIVE.acquire(blocking=False):
        raise HTTPException(429, "Both demo workers are busy. Please try again shortly.")
    session_id = uuid4().hex
    try:
        from medplain.engine import run_packet

        result = await run_in_threadpool(
            run_packet, packet, session_id, model_mode=mode, stress=body.stress or packet.adversarial,
        )
        if result.get("status") not in {"verified", "refused"} or result.get("session_id") != session_id:
            raise ValueError("Workflow result does not belong to this request")
        if result["status"] == "verified":
            candidate = PatientExplanation.model_validate(result["explanation"])
            verdict = SafetyGate(packet).validate(candidate)
            artifact_hash = canonical_hash({"structured": candidate.model_dump(),
                                            "text": render_explanation(candidate)})
            if (not verdict.passed or not verify_receipts(result["receipts"])
                    or result["receipts"][-1]["verdict"] != "verified"
                    or result["receipts"][-1]["artifact_hash"] != artifact_hash
                    or result["receipts"][-1]["source_hash"] != canonical_hash(packet.model_dump())
                    or result["receipts"][-1]["session_id"] != session_id
                    or result["explanation_text"] != render_explanation(candidate)):
                raise ValueError("Publication receipt or clinical artifact mismatch")
        elif result.get("explanation") is not None or result.get("explanation_text") is not None:
            raise ValueError("A refused result attempted to publish an explanation")
        result["commit"] = deployed_commit()
        return result
    except Exception:
        # No model output, health input or exception string is sent to logs or to the browser.
        logger.error("Agent execution did not reach the publication gate")
        raise HTTPException(
            503, "The agents could not finish verification. No patient explanation was published. Please retry.",
        ) from None
    finally:
        ACTIVE.release()


@app.post("/api/receipts/verify")
def receipt_verify(body: ReceiptVerificationRequest):
    return {
        "valid": verify_receipts(body.receipts),
        "scope": "Hash-chain integrity only. Unsigned; retain the final hash to detect chain replacement or truncation.",
    }


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

handler = Mangum(app, lifespan="off")
