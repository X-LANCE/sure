from __future__ import annotations

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from adopt_reference_model import (
    link_asset_children,
    prepare_target,
    read_json,
    write_artifact_manifest,
    write_package_gate,
    write_verdict,
)
from deployment_contract import require_timestamp_after


class AdoptedDocumentStampsTest(unittest.TestCase):
    """check_package_gate.py and check_verdict.py require each adopted document to be stamped
    strictly after the documents it cites; the adopter has to write stamps that satisfy that."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.target = Path(self.temporary.name) / "model"
        (self.target / "artifacts").mkdir(parents=True)
        self.manifest_path = write_artifact_manifest(self.target, "example/demo", "example__demo")
        write_package_gate(self.target, self.manifest_path)
        write_verdict(self.target, "example/demo", "example__demo", {})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def document(self, name: str) -> dict:
        return read_json(self.target / "artifacts" / name)

    def test_the_package_gate_is_stamped_after_the_manifest_it_cites(self) -> None:
        require_timestamp_after(
            "package_gate.json",
            self.document("package_gate.json"),
            ("artifact_manifest.json", self.document("artifact_manifest.json")),
        )

    def test_the_verdict_is_stamped_after_the_package_gate(self) -> None:
        require_timestamp_after(
            "verdict.json",
            self.document("verdict.json"),
            ("package_gate.json", self.document("package_gate.json")),
        )


class PrepareTargetTest(unittest.TestCase):
    """A previous adoption may hold local state (a built .venv); only the flag may replace it."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.target = Path(self.temporary.name) / "model"
        (self.target / "artifacts").mkdir(parents=True)
        write_artifact_manifest(self.target, "example/demo", "example__demo")
        self.marker = self.target / ".venv" / "marker"
        self.marker.parent.mkdir()
        self.marker.write_text("local\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_a_previous_adoption_survives_without_the_replace_flag(self) -> None:
        with self.assertRaises(FileExistsError) as refused:
            prepare_target(self.target, replace_symlink=False, replace_existing_adoption=False)
        self.assertIn("pass --replace-existing-adoption", str(refused.exception))
        self.assertTrue(self.marker.is_file())

    def test_the_replace_flag_replaces_a_previous_adoption(self) -> None:
        prepare_target(self.target, replace_symlink=False, replace_existing_adoption=True)
        self.assertTrue(self.target.is_dir())
        self.assertEqual(list(self.target.iterdir()), [])


class LinkAssetChildrenTest(unittest.TestCase):
    """Windows refuses symlinks without Developer Mode or admin rights."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.reference = root / "reference"
        self.target = root / "model"
        (self.reference / "checkpoints" / "encoder").mkdir(parents=True)
        (self.reference / "checkpoints" / "encoder" / "weights.bin").write_bytes(b"enc")
        (self.reference / "checkpoints" / "model.pt").write_bytes(b"pt")
        (self.reference / ".runtime" / "huggingface").mkdir(parents=True)
        (self.reference / ".runtime" / "huggingface" / "token").write_text("x", encoding="utf-8")
        self.target.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_assets_are_copied_when_symlinks_are_not_permitted(self) -> None:
        refused = OSError(1314, "A required privilege is not held by the client")
        with unittest.mock.patch.object(Path, "symlink_to", side_effect=refused):
            link_asset_children(self.reference, self.target)
        self.assertEqual((self.target / "checkpoints" / "model.pt").read_bytes(), b"pt")
        self.assertEqual((self.target / "checkpoints" / "encoder" / "weights.bin").read_bytes(), b"enc")
        self.assertTrue((self.target / ".runtime" / "huggingface" / "token").is_file())


if __name__ == "__main__":
    unittest.main()
