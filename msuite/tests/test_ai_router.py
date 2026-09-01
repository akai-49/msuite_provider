# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

import unittest
from unittest.mock import patch, MagicMock
from msuite.api.v1.ai_router import (
    classify_intent,
    route_user_query,
    dispatch_help_query,
    dispatch_analytics_query,
    INTENT_HELP,
    INTENT_ANALYTICS,
    INTENT_GENERAL,
)


class TestAIRouter(unittest.TestCase):
    """
    Test suite for Smart Backend Intent Router.
    Verifies sub-millisecond pattern classification and microservice dispatchers.
    """

    def test_help_guidance_classification(self):
        help_queries = [
            "how to connect whatsapp",
            "how can i send bulk whatsapp message",
            "How do I connect my WhatsApp number?",
            "steps to setup meta WABA",
            "Where can I find audience segments?",
            "Where do I create a new broadcast?",
            "Guide on sending SMS campaigns",
            "Explain how to configure Meta ads connection",
            "How does lead automation work?",
        ]
        for query in help_queries:
            intent = classify_intent(query)
            self.assertEqual(
                intent,
                INTENT_HELP,
                f"Query '{query}' expected '{INTENT_HELP}', got '{intent}'",
            )

    def test_analytics_metrics_classification(self):
        analytics_queries = [
            "What was my Meta Ads spend last week?",
            "What is my WhatsApp delivery rate?",
            "How many messages were delivered today?",
            "Show me CTR and CPC for my campaign",
            "How much did I spend on Instagram ads yesterday?",
            "What are my failed messages for this month?",
            "Show performance summary for email campaign",
            "Check my ROAS and conversion metrics",
            "How many leads generated from Diwali campaign?",
        ]
        for query in analytics_queries:
            intent = classify_intent(query)
            self.assertEqual(
                intent,
                INTENT_ANALYTICS,
                f"Query '{query}' expected '{INTENT_ANALYTICS}', got '{intent}'",
            )

    def test_general_chat_classification(self):
        general_queries = [
            "hello",
            "hi",
            "hey there",
            "who are you?",
            "what can you do?",
            "help",
        ]
        for query in general_queries:
            intent = classify_intent(query)
            self.assertEqual(
                intent,
                INTENT_GENERAL,
                f"Query '{query}' expected '{INTENT_GENERAL}', got '{intent}'",
            )

    def test_precedence_how_to_vs_how_many(self):
        # "How can I see my delivery rate?" -> Asking how to navigate/view in UI -> Help
        self.assertEqual(classify_intent("how do i see my delivery rate"), INTENT_HELP)
        # "How many delivered messages do I have?" -> Quantity/Metrics -> Analytics
        self.assertEqual(classify_intent("how many delivered messages do i have"), INTENT_ANALYTICS)

    @patch("msuite.api.v1.ai_router.requests.post")
    def test_dispatch_help_query_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "reply": "To connect WhatsApp: 1. Go to Settings -> Connections..."
        }
        mock_post.return_value = mock_response

        res = dispatch_help_query("how to connect whatsapp", current_page="/connections")
        self.assertEqual(res["status"], "Success")
        self.assertIn("To connect WhatsApp", res["reply"])
        self.assertIn("search_docs", res["tools_used"])

    @patch("msuite.api.v1.ai_router.requests.post")
    def test_dispatch_analytics_query_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "reply": "Your Meta Ads spend was ₹4,200.",
            "tools_used": ["get_ad_account_performance"],
            "model_used": "openai/gpt-4o-mini",
            "usage": {"prompt_tokens": 120, "completion_tokens": 40},
            "options": [{"label": "Show CTR", "value": "Show CTR"}],
        }
        mock_post.return_value = mock_response

        res = dispatch_analytics_query("CLT-001", "What was my Meta Ads spend?")
        self.assertEqual(res["status"], "Success")
        self.assertEqual(res["reply"], "Your Meta Ads spend was ₹4,200.")
        self.assertIn("get_ad_account_performance", res["tools_used"])

    def test_route_user_query_general(self):
        res = route_user_query(
            client_code="CLT-001",
            message="hello",
        )
        self.assertEqual(res["intent"], INTENT_GENERAL)
        self.assertIn("MSuite AI Assistant", res["reply"])
        self.assertGreaterEqual(len(res["options"]), 2)
        self.assertIn("latency_ms", res)


if __name__ == "__main__":
    unittest.main()
