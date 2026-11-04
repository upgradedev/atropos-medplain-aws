"""Dependency-free negative hash admission control; does not decode media."""
from pathlib import Path
import tempfile

import native_review_qa as qa


def main():
    with tempfile.TemporaryDirectory() as temporary:
        changed = Path(temporary) / "changed.zip"
        changed.write_bytes(b"not the accepted native archive")
        try:
            qa.archive_proof(changed)
        except ValueError as error:
            assert "archive hash changed" in str(error)
        else:
            raise AssertionError("Hash drift was accepted")
    print("PASS: changed archive rejected before media parsing or decoding")


if __name__ == "__main__":
    main()
