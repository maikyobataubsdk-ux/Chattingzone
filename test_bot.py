#!/usr/bin/env python3
"""
Unit and Integration Tests for Telegram Support Bot (bot.py).
"""

import os
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

# Import components from bot.py
import bot
from bot import JSONDatabase, detect_msg_type, is_admin, is_clone_owner, check_user_forcesub

class TestSupportBot(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_file = "test_database.json"
        if os.path.exists(self.test_db_file):
            os.remove(self.test_db_file)
        self.db = JSONDatabase(filepath=self.test_db_file)
        bot.db = self.db

    async def asyncTearDown(self):
        if os.path.exists(self.test_db_file):
            os.remove(self.test_db_file)
        if os.path.exists(f"{self.test_db_file}.tmp"):
            os.remove(f"{self.test_db_file}.tmp")

    async def test_database_user_and_ticket_isolation(self):
        # Create users on master and clone
        user_mock = MagicMock()
        user_mock.id = 11111
        user_mock.first_name = "Alice"
        user_mock.username = "alice123"
        user_mock.language_code = "en"

        master_u = self.db.upsert_user("master", user_mock)
        clone_u = self.db.upsert_user("99999", user_mock)

        self.assertEqual(master_u["first_name"], "Alice")
        self.assertEqual(clone_u["bot_id"], "99999")

        # Create clone
        self.db.add_clone("99999", "fake_token", "CloneBot", "clonebot", 22222)
        clone = self.db.get_clone("99999")
        self.assertIsNotNone(clone)
        self.assertEqual(clone["owner_id"], 22222)

        # Create tickets
        t1_master = self.db.create_ticket("master", 11111, 10, 1, "Text")
        t2_master = self.db.create_ticket("master", 11111, 11, 2, "Photo")
        t1_clone = self.db.create_ticket("99999", 11111, 20, 1, "Voice")

        self.assertEqual(t1_master, 1)
        self.assertEqual(t2_master, 2)
        self.assertEqual(t1_clone, 1)

        # Verify ticket lookup isolation
        ticket_m1 = self.db.get_ticket("master", 1)
        ticket_c1 = self.db.get_ticket("99999", 1)

        self.assertEqual(ticket_m1["message_type"], "Text")
        self.assertEqual(ticket_c1["message_type"], "Voice")

        await self.db.save()
        self.assertTrue(os.path.exists(self.test_db_file))

    async def test_permission_checks(self):
        bot.SUPREME_OWNER_ID = 77777
        self.db.add_clone("88888", "token_88", "Bot88", "bot88", 55555)

        # Supreme Owner checks
        self.assertTrue(is_admin("master", 77777))
        self.assertTrue(is_admin("88888", 77777))

        # Clone Owner checks
        self.assertTrue(is_admin("88888", 55555))
        self.assertTrue(is_clone_owner("88888", 55555))
        self.assertFalse(is_clone_owner("master", 55555))

        # Random user checks
        self.assertFalse(is_admin("88888", 12345))
        self.assertFalse(is_clone_owner("88888", 12345))

    async def test_detect_msg_type(self):
        msg_text = MagicMock(text="Hello", photo=None, video=None, document=None, audio=None, voice=None, sticker=None, animation=None)
        msg_photo = MagicMock(text=None, photo=[MagicMock()], video=None, document=None, audio=None, voice=None, sticker=None, animation=None)

        self.assertEqual(detect_msg_type(msg_text), "Text")
        self.assertEqual(detect_msg_type(msg_photo), "Photo")

    async def test_forcesub_verification_logic(self):
        # Configure forcesub
        self.db.data["forcesub"] = {
            "enabled": True,
            "channels": [
                {"channel_id": "-10012345", "title": "Test Channel", "link": "https://t.me/test"}
            ]
        }

        mock_bot = AsyncMock()
        # Case 1: Member joined
        member_mock = MagicMock()
        member_mock.status = "member"
        mock_bot.get_chat_member.return_value = member_mock

        is_joined, unjoined = await check_user_forcesub(mock_bot, 12345)
        self.assertTrue(is_joined)
        self.assertEqual(len(unjoined), 0)

        # Case 2: Member left
        member_left = MagicMock()
        member_left.status = "left"
        mock_bot.get_chat_member.return_value = member_left

        is_joined, unjoined = await check_user_forcesub(mock_bot, 12345)
        self.assertFalse(is_joined)
        self.assertEqual(len(unjoined), 1)

if __name__ == "__main__":
    unittest.main()
