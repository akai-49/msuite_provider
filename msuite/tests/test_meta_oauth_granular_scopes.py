"""
Unit tests for Meta OAuth Granular Scopes and Asset Selection.

Verifies that when a user selects specific Facebook Pages, Instagram Accounts,
Businesses, or Ad Accounts in Meta's consent dialog, only the explicitly selected
assets are discovered and connected, and other unselected child pages / assets
under the business are excluded.
"""
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite.services.oauth import meta_base, meta_social, meta_ads, meta_catalogue


class TestMetaOAuthGranularScopes(IntegrationTestCase):
    def test_inspect_debug_token_categorization(self):
        """Verify inspect_debug_token correctly splits granular_scopes into categories."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {
                "is_valid": True,
                "granular_scopes": [
                    {
                        "scope": "pages_show_list",
                        "target_ids": ["663546843508361"]
                    },
                    {
                        "scope": "pages_manage_posts",
                        "target_ids": ["663546843508361"]
                    },
                    {
                        "scope": "business_management",
                        "target_ids": ["1375119377855016", "24594170630259336"]
                    },
                    {
                        "scope": "instagram_basic",
                        "target_ids": ["17841448597931577"]
                    },
                    {
                        "scope": "ads_management",
                        "target_ids": ["act_101010101"]
                    },
                    {
                        "scope": "catalog_management",
                        "target_ids": ["cat_99999"]
                    }
                ]
            }
        }

        with patch("msuite.services.oauth.meta_base.get_msuite_app") as mock_get_app, \
             patch("requests.get", return_value=mock_response):
            mock_app = MagicMock()
            mock_app.app_id = "test_app_id"
            mock_app.get_password.return_value = "test_app_secret"
            mock_get_app.return_value = mock_app

            debug_info = meta_base.inspect_debug_token("test_user_token")

            self.assertIn("663546843508361", debug_info["page_target_ids"])
            self.assertEqual(len(debug_info["page_target_ids"]), 1)

            self.assertIn("17841448597931577", debug_info["instagram_target_ids"])
            self.assertEqual(len(debug_info["instagram_target_ids"]), 1)

            self.assertIn("1375119377855016", debug_info["business_target_ids"])
            self.assertIn("24594170630259336", debug_info["business_target_ids"])

            self.assertIn("101010101", debug_info["ad_target_ids"])
            self.assertIn("act_101010101", debug_info["ad_target_ids"])

            self.assertIn("cat_99999", debug_info["catalog_target_ids"])

    def test_meta_social_discovers_only_selected_page(self):
        """When user selects 1 page (663546843508361) under business (1375119377855016),
        only that page must be connected, and other unselected business pages must be skipped."""
        client_name = "test_client"
        token_data = {"access_token": "valid_token", "expires_in": 3600, "app_name": "Meta Social"}

        mock_debug_info = {
            "page_target_ids": {"663546843508361"},
            "instagram_target_ids": {"17841448597931577"},
            "business_target_ids": {"1375119377855016"},
            "target_ids": {"663546843508361", "17841448597931577", "1375119377855016"},
        }

        mock_businesses = [{"id": "1375119377855016", "name": "walue.ai"}]

        # /me/accounts returns Walue.biz
        mock_me_accounts_resp = MagicMock()
        mock_me_accounts_resp.status_code = 200
        mock_me_accounts_resp.json.return_value = {
            "data": [
                {"id": "663546843508361", "name": "Walue.biz", "access_token": "page_token_walue_biz"}
            ]
        }

        # /1375119377855016/owned_pages returns multiple pages belonging to the business
        mock_owned_pages_resp = MagicMock()
        mock_owned_pages_resp.status_code = 200
        mock_owned_pages_resp.json.return_value = {
            "data": [
                {"id": "663546843508361", "name": "Walue.biz", "access_token": "page_token_walue_biz"},
                {"id": "1160846680448894", "name": "Walue.ai"},
                {"id": "960592913807350", "name": "Wotomate.ai"},
                {"id": "984093611447026", "name": "The Tasty Table"},
                {"id": "916895004845187", "name": "Empire Builders"},
            ]
        }

        # IG check for Walue.biz returns test.frappe
        mock_ig_resp = MagicMock()
        mock_ig_resp.status_code = 200
        mock_ig_resp.json.return_value = {
            "instagram_business_account": {
                "id": "17841448597931577",
                "username": "test.frappe"
            }
        }

        def mock_requests_get(url, *args, **kwargs):
            if "/me/accounts" in url:
                return mock_me_accounts_resp
            elif "/owned_pages" in url:
                return mock_owned_pages_resp
            elif "/client_pages" in url:
                empty_resp = MagicMock()
                empty_resp.status_code = 200
                empty_resp.json.return_value = {"data": []}
                return empty_resp
            elif "/663546843508361" in url:
                return mock_ig_resp
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {}
            return resp

        with patch("msuite.services.oauth.meta_social.inspect_debug_token", return_value=mock_debug_info), \
             patch("msuite.services.oauth.meta_social.discover_businesses", return_value=mock_businesses), \
             patch("msuite.services.oauth.meta_social.upsert_businesses_as_auth_accounts", return_value={"1375119377855016": "AUTH-1"}), \
             patch("msuite.services.oauth.meta_social.upsert_connected_account", return_value="CA-1") as mock_upsert_ca, \
             patch("msuite.services.oauth.meta_social.upsert_facebook_account", return_value="FB-1"), \
             patch("msuite.services.oauth.meta_social.upsert_instagram_account", return_value="IG-1"), \
             patch("msuite.services.oauth.meta_social.push_account_to_client") as mock_push, \
             patch("msuite.services.oauth.meta_social._subscribe_page_to_webhooks"), \
             patch("requests.get", side_effect=mock_requests_get):

            connected = meta_social.discover_accounts(client_name, token_data)

            # Only Walue.biz (Facebook) and test.frappe (Instagram) should be connected
            fb_connected = [c for c in connected if c.get("platform") == "Facebook"]
            ig_connected = [c for c in connected if c.get("platform") == "Instagram"]

            self.assertEqual(len(fb_connected), 1)
            self.assertEqual(fb_connected[0]["name"], "Walue.biz")

            self.assertEqual(len(ig_connected), 1)
            self.assertEqual(ig_connected[0]["name"], "@test.frappe")

            # Check that unselected pages (Walue.ai, Wotomate.ai, etc.) were never upserted
            connected_names = [call.args[2] for call in mock_upsert_ca.call_args_list]
            self.assertIn("663546843508361", connected_names)
            self.assertIn("17841448597931577", connected_names)
            self.assertNotIn("1160846680448894", connected_names)
            self.assertNotIn("960592913807350", connected_names)
            self.assertNotIn("984093611447026", connected_names)
            self.assertNotIn("916895004845187", connected_names)
