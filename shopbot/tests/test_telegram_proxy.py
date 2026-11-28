import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class TelegramProxyTests(unittest.IsolatedAsyncioTestCase):
    @patch.object(bot, "BOT_TOKEN", "123456:TEST")
    def test_direct_mode_without_proxy_dependency(self):
        with patch.dict("os.environ", {"TELEGRAM_PROXY": ""}), patch.object(bot, "AiohttpSession") as session:
            client = bot.create_bot()
        session.assert_not_called()
        self.assertIsNone(client.session.proxy)

    @patch.object(bot, "BOT_TOKEN", "123456:TEST")
    def test_proxy_only_configures_bot_session(self):
        url = "socks5://127.0.0.1:1080"
        with patch.dict("os.environ", {"TELEGRAM_PROXY": url, "OZON_PROXY": "", "WB_PROXY": ""}), \
             patch.object(bot, "AiohttpSession") as session:
            client = bot.create_bot()
            self.assertEqual(bot.os.environ["OZON_PROXY"], "")
            self.assertEqual(bot.os.environ["WB_PROXY"], "")
        session.assert_called_once_with(proxy=url)
        self.assertIs(client.session, session.return_value)

    def test_invalid_url_is_refused_without_showing_password(self):
        url = "mtproto://name:secret@host:1080"
        with patch.dict("os.environ", {"TELEGRAM_PROXY": url}):
            with self.assertRaises(SystemExit) as error:
                bot.create_bot()
        self.assertIn("TELEGRAM_PROXY", str(error.exception))
        self.assertNotIn("secret", str(error.exception))

    @patch.object(bot, "BOT_TOKEN", "123456:TEST")
    async def test_check_calls_get_me_without_opening_queue(self):
        manager = AsyncMock()
        client = manager.__aenter__.return_value
        client.get_me.return_value = SimpleNamespace(username="test_bot")
        with patch.object(bot, "create_bot", return_value=manager), patch.object(bot, "DraftStore") as store:
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(await bot.check_telegram(), 0)
        client.get_me.assert_awaited_once_with(request_timeout=15)
        store.assert_not_called()
        self.assertIn("@test_bot", output.getvalue())
