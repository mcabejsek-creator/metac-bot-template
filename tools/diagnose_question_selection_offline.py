"""Offline SDK question-selection diagnostics. NEVER submits forecasts.

Only synthetic questions; no user secrets, Metaculus API, or model API.
Run with: poetry run python -B tools/diagnose_question_selection_offline.py
"""
import ast
import asyncio
import importlib.metadata
import os
from pathlib import Path
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SECRET_NAMES = (
    "METACULUS_TOKEN", "PERPLEXITY_API_KEY", "OPENROUTER_API_KEY",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "EXA_API_KEY",
    "ASKNEWS_API_KEY", "ASKNEWS_CLIENT_ID", "ASKNEWS_SECRET",
    "GITHUB_TOKEN",
)


def no_network(*args, **kwargs):
    raise AssertionError("OFFLINE GUARD: attempted network connection")


class OfflineQuestionSelectionTests(unittest.TestCase):
    def test_project_config_uses_both_tournaments_and_skip(self):
        # Parse rather than import main.py: importing it may publish forecasts.
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "forecast_on_tournament"
        ]
        targets = [node.args[0] for node in calls if node.args]
        self.assertTrue(any(isinstance(n, ast.Constant) and n.value == 33121 for n in targets))
        self.assertTrue(any(
            isinstance(n, ast.Attribute) and n.attr == "CURRENT_MINIBENCH_ID"
            for n in targets
        ))
        self.assertTrue(any(
            isinstance(node, ast.keyword)
            and node.arg == "skip_previously_forecasted_questions"
            and isinstance(node.value, ast.Constant) and node.value.value is True
            for node in ast.walk(tree)
        ))

    def test_sdk_tournament_request_uses_open_filter(self):
        from forecasting_tools import MetaculusClient

        fake = SimpleNamespace(
            get_questions_matching_filter=AsyncMock(return_value=[])
        )
        for target in (33121, "minibench"):
            fake.get_questions_matching_filter.reset_mock()
            result = MetaculusClient.get_all_open_questions_from_tournament(fake, target)
            self.assertEqual(result, [])
            self.assertEqual(fake.get_questions_matching_filter.await_count, 1)
            request_filter = fake.get_questions_matching_filter.await_args.args[0]
            self.assertEqual(request_filter.allowed_tournaments, [target])
            self.assertEqual(request_filter.allowed_statuses, ["open"])
            self.assertEqual(request_filter.group_question_mode, "unpack_subquestions")

    def test_sdk_default_pagination_may_stop_after_first_page(self):
        from forecasting_tools import ApiFilter, MetaculusClient

        q1 = SimpleNamespace(id_of_question=1)
        q2 = SimpleNamespace(id_of_question=2)
        fake = SimpleNamespace(MAX_QUESTIONS_FROM_QUESTION_API_PER_REQUEST=100)
        fake._grab_filtered_questions_with_offset = Mock(
            side_effect=lambda _filter, offset: {
                0: ([], True),
                100: ([q2], False),
            }[offset]
        )
        first = asyncio.run(
            MetaculusClient._filter_sequential_strategy(fake, ApiFilter(), None)
        )
        self.assertEqual(first, [])
        self.assertEqual(
            [call.args[1] for call in fake._grab_filtered_questions_with_offset.call_args_list],
            [0],
        )

        fake._grab_filtered_questions_with_offset.reset_mock()
        fake._grab_filtered_questions_with_offset.side_effect = lambda _filter, offset: {
            0: ([q1], True),
            100: ([q2], False),
        }[offset]
        paged = asyncio.run(
            MetaculusClient._filter_sequential_strategy(fake, ApiFilter(), 2)
        )
        self.assertEqual([q.id_of_question for q in paged], [1, 2])
        self.assertEqual(
            [call.args[1] for call in fake._grab_filtered_questions_with_offset.call_args_list],
            [0, 100],
        )

    def test_sdk_skips_previously_forecasted_before_running(self):
        from forecasting_tools.forecast_bots.forecast_bot import ForecastBot

        old = SimpleNamespace(id_of_question=1, already_forecasted=True)
        new = SimpleNamespace(id_of_question=2, already_forecasted=False)
        fake = SimpleNamespace(
            skip_previously_forecasted_questions=True,
            folder_to_save_reports_to=None,
            _run_individual_question_with_error_propagation=AsyncMock(
                side_effect=lambda question: question.id_of_question
            ),
        )
        result = asyncio.run(ForecastBot.forecast_questions(fake, [old, new], True))
        self.assertEqual(result, [2])
        self.assertEqual(fake._run_individual_question_with_error_propagation.await_count, 1)
        self.assertIs(fake._run_individual_question_with_error_propagation.await_args.args[0], new)

    def test_forecast_on_tournament_delegates_to_selection(self):
        from forecasting_tools.forecast_bots.forecast_bot import ForecastBot

        fake = SimpleNamespace(
            metaculus_client=SimpleNamespace(
                get_all_open_questions_from_tournament=Mock(return_value=[])
            ),
            forecast_questions=AsyncMock(return_value=[]),
        )
        result = asyncio.run(
            ForecastBot.forecast_on_tournament(fake, 33121, return_exceptions=True)
        )
        self.assertEqual(result, [])
        fake.metaculus_client.get_all_open_questions_from_tournament.assert_called_once_with(33121)
        fake.forecast_questions.assert_awaited_once_with([], True)


if __name__ == "__main__":
    # No environment credentials, even if accidentally supplied by a caller.
    for secret in SECRET_NAMES:
        os.environ.pop(secret, None)
    with patch.object(socket.socket, "connect", side_effect=no_network), \
         patch.object(socket.socket, "connect_ex", side_effect=no_network), \
         patch("socket.create_connection", side_effect=no_network):
        print("SDK package version:", importlib.metadata.version("forecasting-tools"))
        print("Network access blocked; no API credentials; synthetic questions only.")
        unittest.main(verbosity=2)
