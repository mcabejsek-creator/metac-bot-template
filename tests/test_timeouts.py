import asyncio
from unittest.mock import patch

from .support import OfflineTestCase, app, question


class QuestionDeadlineTests(OfflineTestCase):
    async def test_question_deadline_ends_a_stalled_model_call(self):
        """Accelerate app deadlines; a separate harness timer observes the task.

        No 180/600-second waits and no assertion based on machine speed. This
        contract tests an asyncio question deadline, not the SDK transport timeout.
        A future implementation using another deadline engine must adapt this
        test's clock accelerator, rather than weaken the completion assertion.
        """
        real_timeout = asyncio.timeout
        real_wait_for = asyncio.wait_for
        budgets = []
        stalled = asyncio.Event()

        async def model(*args, **kwargs):
            await stalled.wait()
            raise AssertionError("Stalled model must not produce a prediction")

        def accelerated_timeout(delay):
            if delay is not None:
                budgets.append(delay)
            return real_timeout(0 if delay == bot.runtime_limits.question_seconds else delay)

        async def accelerated_wait_for(awaitable, timeout):
            if timeout is not None:
                budgets.append(timeout)
            return await real_wait_for(awaitable, 0 if timeout is not None else None)

        bot = self.bot()
        from runtime_limits import RuntimeLimits
        bot.runtime_limits = RuntimeLimits()
        with patch.object(app.GeneralLlm, "invoke", model), \
             patch.object(asyncio, "timeout", accelerated_timeout), \
             patch.object(asyncio, "wait_for", accelerated_wait_for):
            task = asyncio.create_task(bot.forecast_question(question("japanese_diet"), return_exceptions=True))
            try:
                done, _ = await asyncio.wait({task}, timeout=.01)
                if done:
                    # Unexpected application exceptions remain errors, not xfails.
                    self.assertIsInstance(task.result(), BaseException)
                self.assertTrue(bool(done) and bool(budgets),
                                   "A stalled model keeps the question pending; no overall deadline applied")
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
