import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "aisbench_image.py"
spec = importlib.util.spec_from_file_location("aisbench_image", SCRIPT)
image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image)


class ImagePublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.digests = {"amd64": "sha256:" + "a" * 64, "arm64": "sha256:" + "b" * 64}
        for arch, digest in self.digests.items():
            path = self.root / arch / "digest.txt"
            path.parent.mkdir()
            path.write_text(digest + "\n")
        self.manifest = dict(digest="sha256:" + "c" * 64, manifests=[
            dict(digest=digest, platform=dict(os="linux", architecture=arch))
            for arch, digest in self.digests.items()])

    def test_index_matches_both_tested_images_in_any_order(self):
        for children in (self.manifest["manifests"], self.manifest["manifests"][::-1]):
            self.assertEqual(image.verify_index(dict(self.manifest, manifests=children), self.root), self.manifest["digest"])

    def test_single_platform_or_extra_manifest_rejected(self):
        for children in ([], self.manifest["manifests"][:1], self.manifest["manifests"] * 2):
            with self.subTest(children=children), self.assertRaises(ValueError):
                image.verify_index(dict(self.manifest, manifests=children), self.root)

    def test_wrong_platform_or_duplicate_architecture_rejected(self):
        for platform in (dict(os="windows", architecture="arm64"),
                         dict(os="linux", architecture="amd64"),
                         dict(os="linux", architecture="arm")):
            manifest = copy.deepcopy(self.manifest)
            manifest["manifests"][1]["platform"] = platform
            with self.subTest(platform=platform), self.assertRaises(ValueError):
                image.verify_index(manifest, self.root)

    def test_stale_or_swapped_digest_rejected(self):
        for digest in ("sha256:" + "d" * 64, self.digests["arm64"]):
            manifest = copy.deepcopy(self.manifest)
            manifest["manifests"][0]["digest"] = digest
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                image.verify_index(manifest, self.root)

    def test_missing_or_invalid_digest_artifact_rejected(self):
        path = self.root / "amd64/digest.txt"
        path.write_text("not-a-digest")
        with self.assertRaises(ValueError):
            image.verify_index(self.manifest, self.root)
        path.unlink()
        with self.assertRaises(OSError):
            image.verify_index(self.manifest, self.root)
        with self.assertRaises(ValueError):
            image.require_digest(None)

    def run_meta(self, runner="aisbench-acc", arch="amd64", tag="", token="fake"):
        env_path = self.root / "github-env"
        env_path.write_text("")
        env = dict(DOCKERHUB_USERNAME="exampleuser", DOCKERHUB_TOKEN=token,
                   AISBENCH_RUNNER=runner, AISBENCH_ARCH=arch, REQUESTED_TAG=tag,
                   RUNNER_NAME="GitHub Actions 1000000105", GITHUB_SHA="0" * 40,
                   GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2", GITHUB_ENV=str(env_path))
        # Read the real manifests and execute all metadata logic; fake only git and CI env.
        with patch.dict(os.environ, env, clear=True), \
             patch.object(image.subprocess, "check_output", return_value="a" * 40 + "\n"):
            image.prepare_metadata()
        return dict(line.split("=", 1) for line in env_path.read_text().splitlines())

    def test_metadata_uses_matrix_name_actual_source_and_unique_staging_tags(self):
        for runner in ("aisbench-acc", "aisbench-perf"):
            for arch in ("amd64", "arm64"):
                with self.subTest(runner=runner, arch=arch):
                    values = self.run_meta(runner, arch)
                    self.assertEqual(values["IMAGE"], f"docker.io/exampleuser/argus-runner-{runner}")
                    self.assertEqual(values["STAGING_TAG"], f"run-123-2-{arch}")
                    self.assertEqual(values["SHA_TAG"], "sha-" + "a" * 40)
                    self.assertNotIn("fake", json.dumps(values))
                    self.assertNotIn("RUNNER_NAME", values)

    def test_merge_metadata_and_custom_tag(self):
        values = self.run_meta(arch="", tag="candidate-1")
        self.assertNotIn("STAGING_TAG", values)
        self.assertEqual(values["IMAGE_TAG"], "candidate-1")
        self.assertEqual(values["INDEX_TAG"], "run-123-2-index")

    def test_unsafe_tags_invalid_runner_and_missing_credentials_rejected(self):
        for args in (dict(tag="latest"), dict(tag="sha-test"), dict(tag="run-123"),
                     dict(tag="bad/tag"), dict(tag="x\nINJECT=x"), dict(tag="$(id)"),
                     dict(tag="a" * 129), dict(runner="../other"), dict(arch="s390x"), dict(token="")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.run_meta(**args)
            self.assertEqual((self.root / "github-env").read_text(), "")


if __name__ == "__main__":
    unittest.main()
