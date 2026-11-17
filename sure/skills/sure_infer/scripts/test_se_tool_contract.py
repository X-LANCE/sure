from __future__ import annotations

import importlib.util
import unittest

from model_wrapper_mcp_server import _to_plain, _tool_schema


class SeToolContractTests(unittest.TestCase):
    def test_se_tool_accepts_generation_output_path(self) -> None:
        schema = _tool_schema("SE")
        self.assertEqual(schema["required"], ["audio_path"])
        self.assertIn("noisy_audio_path", schema["properties"])
        self.assertIn("output_path", schema["properties"])


@unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy is not installed")
class ToPlainTests(unittest.TestCase):
    def test_numpy_arrays_and_scalars_become_json_values(self) -> None:
        import numpy as np

        output = {"embedding": np.array([0.5, 0.25], dtype=np.float32), "label": np.int64(3), "detected": np.bool_(True)}
        self.assertEqual(_to_plain(output), {"embedding": [0.5, 0.25], "label": 3, "detected": True})


if __name__ == "__main__":
    unittest.main()
