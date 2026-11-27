"""Prepare CI image names and verify that an index contains the tested digests."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def require_digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("Invalid image digest")
    return value


def prepare_metadata():
    username = os.environ.get("DOCKERHUB_USERNAME", "")
    if not username or not os.environ.get("DOCKERHUB_TOKEN"):
        raise ValueError("Configure DOCKERHUB_USERNAME and DOCKERHUB_TOKEN repository secrets")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", username):
        raise ValueError("DOCKERHUB_USERNAME must be a Docker Hub username")
    runner = os.environ.get("AISBENCH_RUNNER", "")
    if runner not in ("aisbench-acc", "aisbench-perf"):
        raise ValueError("Unknown AISBench runner")
    tag = os.environ.get("REQUESTED_TAG", "")
    if not tag:
        manifest = Path("runners", runner, "runner.yaml").read_text(encoding="utf-8")
        versions = re.findall(r"^version: *([^\r\n]+)", manifest, flags=re.MULTILINE)
        if len(versions) != 1:
            raise ValueError("Expected one runner version")
        tag = versions[0].strip()
    if (not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}", tag)
            or tag == "latest" or tag.startswith(("sha-", "run-"))):
        raise ValueError("Invalid Docker tag; latest, sha-* and run-* are reserved")
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", source_sha):
        raise ValueError("Invalid source revision")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if not run_id.isdecimal() or not attempt.isdecimal():
        raise ValueError("Missing workflow run identity")
    values = dict(IMAGE=f"docker.io/{username}/argus-runner-{runner}", IMAGE_TAG=tag,
                  SOURCE_SHA=source_sha, SHA_TAG=f"sha-{source_sha}",
                  INDEX_TAG=f"run-{run_id}-{attempt}-index")
    arch = os.environ.get("AISBENCH_ARCH")
    if arch:
        if arch not in ("amd64", "arm64"):
            raise ValueError("Unsupported architecture")
        values["STAGING_TAG"] = f"run-{run_id}-{attempt}-{arch}"
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def verify_index(manifest, digests_dir):
    """Accept only exactly the two single-platform images tested in this run."""
    expected = {arch: require_digest((digests_dir / arch / "digest.txt").read_text().strip())
                for arch in ("amd64", "arm64")}
    children = manifest.get("manifests", [])
    if len(children) != 2:
        raise ValueError("Index must contain exactly amd64 and arm64")
    seen = set()
    for child in children:
        platform = child.get("platform", {})
        arch = platform.get("architecture")
        if platform.get("os") != "linux" or arch not in expected or arch in seen:
            raise ValueError("Missing, duplicate or unexpected index platform")
        if require_digest(child.get("digest")) != expected[arch]:
            raise ValueError("Index digest does not match the tested image")
        seen.add(arch)
    return require_digest(manifest.get("digest"))


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["meta"]:
            prepare_metadata()
        elif len(sys.argv) == 4 and sys.argv[1] == "verify-index":
            print(verify_index(json.loads(Path(sys.argv[2]).read_text()), Path(sys.argv[3])))
        else:
            raise ValueError("Usage: aisbench_image.py meta | verify-index MANIFEST DIGESTS_DIR")
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
