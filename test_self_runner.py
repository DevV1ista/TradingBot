"""No network / no real Telegram or trading in these tests."""
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import self_runner

NY = ZoneInfo("America/New_York")


class AutoRunnerTests(unittest.TestCase):
    def test_trading_segments(self):
        samples = (
            (0, 49, None), (0, 50, 1), (5, 39, 1), (5, 40, 2),
            (10, 59, 2), (11, 0, 3), (16, 4, 3), (16, 5, None),
        )
        for hour, minute, expected in samples:
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(
                    self_runner.segment_at(datetime(2026, 11, 20, hour, minute, tzinfo=NY)),
                    expected,
                )

    def test_weekend_and_daylight_saving(self):
        self.assertIsNone(self_runner.segment_at(datetime(2026, 11, 21, 10, tzinfo=NY)))
        self.assertIsNone(self_runner.segment_at(datetime(2026, 11, 22, 10, tzinfo=NY)))
        self.assertEqual(self_runner.segment_at(datetime(2026, 7, 20, 11, tzinfo=NY)), 3)

    def test_telegram_status_requires_secrets(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Missing Telegram"):
                self_runner.telegram_status("test")

    @patch("self_runner.requests.post")
    def test_telegram_status_delivery(self, post):
        post.return_value.ok = True
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_CHAT_ID": "test"}):
            self_runner.telegram_status("online")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["data"]["text"], "online")

    @patch("self_runner.subprocess.run")
    def test_checkpoint_without_state_changes_does_not_commit(self, run):
        run.side_effect = [MagicMock(returncode=0), MagicMock(returncode=0)]
        self_runner.checkpoint()
        self.assertEqual(run.call_count, 2)

    @patch("self_runner.checkpoint")
    @patch("self_runner.telegram_status")
    @patch("self_runner.time.sleep")
    @patch("self_runner.time.monotonic")
    @patch("self_runner.datetime")
    def test_closed_weekend_does_not_start_trading(self, date_mock, monotonic, sleep, send, checkpoint):
        date_mock.now.return_value = datetime(2026, 11, 21, 11, tzinfo=NY)
        monotonic.side_effect = [0, 0, 0, 0, 1]
        with patch("self_runner.Bot") as bot:
            self_runner.run_service(1, initial=True)
            bot.assert_not_called()
        send.assert_called_once()
        checkpoint.assert_called_once()


    @patch("self_runner.checkpoint")
    @patch("self_runner.time.sleep")
    @patch("self_runner.time.monotonic")
    @patch("self_runner.datetime")
    @patch("self_runner.telegram_status")
    def test_repeated_market_errors_stop_the_chain(self, send, date_mock, monotonic, sleep, checkpoint):
        date_mock.now.return_value = datetime(2026, 11, 20, 11, 0, tzinfo=NY)
        calls = iter(range(1000))
        monotonic.side_effect = lambda: next(calls)
        with patch("self_runner.Bot") as bot_type:
            bot_type.return_value.s = {"sent": ["bot_online"]}
            bot_type.return_value.tick.side_effect = RuntimeError("data source offline")
            with self.assertRaisesRegex(RuntimeError, "10 consecutive checks"):
                self_runner.run_service(300)
            self.assertEqual(bot_type.return_value.tick.call_count, 10)
            bot_type.return_value.save.assert_called()
            checkpoint.assert_called_once()



if __name__ == "__main__":
    unittest.main()
