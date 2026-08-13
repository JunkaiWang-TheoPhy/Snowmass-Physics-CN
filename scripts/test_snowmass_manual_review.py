#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import snowmass_manual_review as review


class ManualReviewQueueTests(unittest.TestCase):
    def test_collects_only_unresolved_literal_rebinding_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            article = root / "papers" / "arxiv_1"
            (article / "chunk_status").mkdir(parents=True)
            (article / "chunk0001.md").write_text("Source 24 nodes.\n", encoding="utf-8")
            (article / "stage2_chunk0001.md").write_text("源文有24个节点。\n", encoding="utf-8")
            (article / "04-critique.md").write_text(
                "- chunk0001: move 24 beside nodes.\n", encoding="utf-8"
            )
            (article / "chunk_status" / "chunk0001.json").write_text(
                json.dumps(
                    {
                        "record_id": "arxiv:1",
                        "chunk_id": "chunk0001",
                        "source_file": "chunk0001.md",
                        "stages": {
                            "revision": {
                                "status": "complete",
                                "decision": {
                                    "action": "copy_prior_text",
                                    "reason": "revision_literal_rebinding_requires_manual_review",
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            queue = review.collect_manual_review_queue(root)

            self.assertEqual(queue["unresolved_count"], 1)
            item = queue["items"][0]
            self.assertEqual(item["record_id"], "arxiv:1")
            self.assertEqual(item["chunk_id"], "chunk0001")
            self.assertEqual(item["terminology_draft"], "源文有24个节点。\n")
            self.assertEqual(item["critique"], ["- chunk0001: move 24 beside nodes."])
            self.assertEqual(len(item["source_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
