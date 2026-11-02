"""Two bounded anonymous GETs bind video readiness to the frozen live source."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = "https://gioep6m26pm3cwvxjp4z4erpmu0zihbk.lambda-url.us-east-1.on.aws"
SOURCE = "efe6c3e7c596697b308fffa758ae377b6422e03f"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists(), "Preserve earlier live readiness evidence"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    proof = {"status": "reserved_before_read_only_GETs", "observed_at": datetime.now(timezone.utc).isoformat(),
             "source_commit": SOURCE, "requests": [], "model_calls": 0, "case_or_session_requests": 0,
             "mutations": 0, "retries": 0, "authorization_headers": False}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(proof, stream, indent=2)
    opener = build_opener(NoRedirect())
    try:
        for endpoint in ("/", "/api/health"):
            request = Request(ROOT + endpoint, headers={"User-Agent": "MedPlain-video-readiness-read-only"})
            with opener.open(request, timeout=20) as response:
                raw = response.read(2_000_001)
                row = {"url": ROOT + endpoint, "http_status": response.status, "bytes": len(raw),
                       "raw_sha256": hashlib.sha256(raw).hexdigest()}
                proof["requests"].append(row)
                args.output.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
                assert response.status == 200 and len(raw) <= 2_000_000, "Judge URL unavailable or oversized"
                if endpoint == "/":
                    assert b"medplain" in raw.lower(), "Unexpected anonymous demo body"
                else:
                    health = json.loads(raw)
                    row["observed_commit"] = health["commit"]
                    assert health["commit"] == SOURCE, "Live source differs from recording freeze"
        proof["status"] = "passed_anonymous200_and_exact_frozen_live_source"
    except Exception as error:
        proof.update(status="failed_readiness_no_render", error_type=type(error).__name__)
        raise
    finally:
        proof["finished_at"] = datetime.now(timezone.utc).isoformat()
        args.output.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": proof["status"], "source_commit": SOURCE, "model_calls": 0}))


if __name__ == "__main__":
    main()
