from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest


class SealPdf2zhProbeTests(unittest.TestCase):
    def _write(self, path: Path, value: dict[str, object]) -> Path:
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_seals_complete_hash_chain_and_rejects_visual_mismatch(self) -> None:
        from scripts.seal_snowmass_pdf2zh_probe import seal_probe

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_hash = "a" * 64
            raw_hash = "b" * 64
            final_hash = "c" * 64
            finish = self._write(
                root / "finish.json",
                {
                    "status": "translated_pending_qc",
                    "source": {"sha256": source_hash},
                    "budget": {
                        "project_max_cost_rmb": 1000.0,
                        "stage_max_cost_rmb": 20.0,
                        "stage_max_api_calls": 250,
                    },
                    "outputs": {"mono_pdf": {"sha256": raw_hash}},
                },
            )
            ir = self._write(
                root / "ir.json",
                {"zero_paid": True, "source_pdf_sha256": source_hash},
            )
            protection = self._write(
                root / "protection.json",
                {
                    "verified": True,
                    "source_pdf_sha256": source_hash,
                    "translated_pdf_sha256": raw_hash,
                    "output_pdf_sha256": final_hash,
                },
            )
            audit = self._write(
                root / "audit.json",
                {"ok": True, "pdf_sha256": final_hash, "failures": []},
            )
            semantic = self._write(
                root / "semantic.json",
                {"ok": True, "pdf_sha256": final_hash, "failures": []},
            )
            visual = self._write(
                root / "visual.json",
                {
                    "verdict": "pass",
                    "score": 93,
                    "threshold": 90,
                    "pdf_sha256": final_hash,
                },
            )

            receipt = seal_probe(
                finish_path=finish,
                ir_receipt_path=ir,
                protection_path=protection,
                semantic_path=semantic,
                audit_path=audit,
                visual_path=visual,
            )
            self.assertTrue(receipt["passed"])
            self.assertEqual(receipt["protected_pdf_sha256"], final_hash)

            visual.write_text(
                json.dumps(
                    {
                        "verdict": "pass",
                        "score": 93,
                        "threshold": 90,
                        "pdf_sha256": "d" * 64,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "visual.*hash"):
                seal_probe(
                    finish_path=finish,
                    ir_receipt_path=ir,
                    protection_path=protection,
                    semantic_path=semantic,
                    audit_path=audit,
                    visual_path=visual,
                )


if __name__ == "__main__":
    unittest.main()
