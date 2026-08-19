import unittest

from praxis.plan import apply_plan_patches, mark_written, next_unwritten, plan_from_model
from praxis.transport import collapse_streamed_completion
from praxis.vault_store import merge_rows


class PlanPatchTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_from_model(
            [
                {"title": "Declarations", "angle": "What he claimed."},
                {"title": "The ministry", "angle": "What the record shows."},
                {"title": "Viability", "angle": "Whether it elects."},
            ]
        )

    def test_replace_one_line_leaves_the_others(self):
        updated = apply_plan_patches(
            self.plan,
            [{"action": "replace", "id": "sec_2", "angle": "Audits, not press lines."}],
        )
        self.assertEqual(updated[0]["title"], "Declarations")
        self.assertEqual(updated[1]["angle"], "Audits, not press lines.")
        self.assertEqual(updated[1]["title"], "The ministry")
        self.assertEqual(len(updated), 3)

    def test_insert_after_a_known_id(self):
        updated = apply_plan_patches(
            self.plan,
            [
                {
                    "action": "insert",
                    "after_id": "sec_1",
                    "title": "The law",
                    "angle": "What the statute actually requires.",
                }
            ],
        )
        self.assertEqual([item["title"] for item in updated], [
            "Declarations",
            "The law",
            "The ministry",
            "Viability",
        ])
        self.assertTrue(updated[1]["id"].startswith("sec_"))

    def test_delete_unknown_id_is_a_no_op(self):
        updated = apply_plan_patches(self.plan, [{"action": "delete", "id": "sec_99"}])
        self.assertEqual(len(updated), 3)

    def test_next_unwritten_skips_finished_sections(self):
        written = mark_written(self.plan, "sec_1")
        nxt = next_unwritten(written)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["id"], "sec_2")


class StreamCollapseTests(unittest.TestCase):
    def test_cancelled_reasoning_stream_is_not_a_fake_completion(self):
        body = (
            'data: {"choices":[{"delta":{"reasoning":"hmm"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"cancelled"}]}\n\n'
            "data: [DONE]\n"
        )
        self.assertIsNone(collapse_streamed_completion(body))

    def test_content_stream_reassembles(self):
        body = (
            'data: {"choices":[{"delta":{"content":"{\\"a\\":"},"finish_reason":null}]}\n'
            'data: {"choices":[{"delta":{"content":"1}"},"finish_reason":"stop"}]}\n'
            "data: [DONE]\n"
        )
        rebuilt = collapse_streamed_completion(body)
        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt["choices"][0]["message"]["content"], '{"a":1}')


class VaultMergeTests(unittest.TestCase):
    def test_newer_updated_at_wins(self):
        existing = {
            "lesson:a": {"ciphertext": "old", "updated_at": 10},
            "lesson:b": {"ciphertext": "keep", "updated_at": 50},
        }
        merged = merge_rows(
            existing,
            [
                {"id": "lesson:a", "ciphertext": "new", "updated_at": 20},
                {"id": "lesson:b", "ciphertext": "stale", "updated_at": 40},
                {"id": "lesson:c", "ciphertext": "fresh", "updated_at": 1},
            ],
        )
        self.assertEqual(merged["lesson:a"]["ciphertext"], "new")
        self.assertEqual(merged["lesson:b"]["ciphertext"], "keep")
        self.assertEqual(merged["lesson:c"]["ciphertext"], "fresh")


if __name__ == "__main__":
    unittest.main()
