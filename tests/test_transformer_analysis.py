"""Presentation boundaries for proposed model connections in the public Space."""
import json
import unittest

from deploy.huggingface.transformer_analysis import connection_analysis, connection_markdown


class TransformerAnalysisTests(unittest.TestCase):
    def test_proposals_never_report_measured_transformer_performance(self):
        for choice in ("price", "news", "both"):
            report = connection_analysis(choice)
            self.assertFalse(report["connected"])
            self.assertIsNone(report["measured_uplift"])
            for path in report["selected_paths"]:
                self.assertFalse(path["connected"])
                self.assertIsNone(path["measured_uplift"])
            json.dumps(report, allow_nan=False)
            text = connection_markdown(choice)
            self.assertIn("not measured", text)
            self.assertIn("No transformer is connected", text)

    def test_selector_cannot_introduce_a_model_identifier_or_external_url(self):
        for invalid in (None, {}, "https://attacker.example/model", "other/model", "<script>"):
            with self.assertRaises(ValueError):
                connection_analysis(invalid)
        self.assertEqual(connection_analysis("Price sequences")["choice"], "price")
        self.assertEqual(connection_analysis("News")["choice"], "news")
        self.assertEqual(len(connection_analysis("Both")["selected_paths"]), 2)

    def test_public_visitors_cannot_mutate_another_visitors_research_metadata(self):
        first = connection_analysis()
        first["selected_paths"][0]["connected"] = True
        first["selected_paths"][0]["models"][0]["url"] = "https://attacker.example"
        first["experiments"].clear()
        second = connection_analysis()
        self.assertFalse(second["selected_paths"][0]["connected"])
        self.assertTrue(second["selected_paths"][0]["models"][0]["url"].startswith("https://huggingface.co/"))
        self.assertEqual(len(second["experiments"]), 4)

    def test_graph_distinguishes_current_paths_from_schema_changes(self):
        for choice in ("price", "news", "both"):
            graph = connection_analysis(choice)["architecture"]
            ids = {node["id"] for node in graph["nodes"]}
            for edge in graph["edges"]:
                self.assertIn(edge["source"], ids)
                self.assertIn(edge["target"], ids)
                if edge["source"] in {"price", "news"}:
                    self.assertEqual(edge["status"], "proposed_new_schema")

    def test_news_artifact_license_is_not_inferred_from_source_license(self):
        model = connection_analysis("news")["selected_paths"][0]["models"][0]
        self.assertIn("Hub weights license needs confirmation", model["license"])
        self.assertIn("source code only confirmed", model["license_scope"])


if __name__ == "__main__":
    unittest.main()
