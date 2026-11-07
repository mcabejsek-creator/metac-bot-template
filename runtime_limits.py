"""Cooperative deadlines and bounded work, without changing forecast parameters.

Blocking calls use daemon workers, not asyncio's default executor: cancelling an
await cannot stop Python threads or make executor shutdown wait indefinitely.
The operation permit remains occupied until the actual worker has returned.
"""

import asyncio
import contextvars
import copy
import math
import signal
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields

_blocking_cancelled = contextvars.ContextVar("blocking_call_cancelled", default=None)


class BlockingDeadlineExceeded(TimeoutError):
    """The blocking waiter expired; its worker may still own the operation."""


def check_blocking_cancelled():
    """Cooperate before the next request; never clear a reserved publication."""
    event = _blocking_cancelled.get()
    if event is not None and event.is_set():
        raise asyncio.CancelledError("Blocking caller was cancelled")


@dataclass(frozen=True)
class RuntimeLimits:
    run_seconds: float = 3 * 60 * 60
    question_seconds: float = 60 * 60
    conditional_seconds: float = 2 * 60 * 60
    model_seconds: float = 240
    blocking_seconds: float = 300
    agent_seconds: float = 210

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{field.name} must be a finite positive number")


async def daemon_call(function, *args, on_finished, **kwargs):
    """Run one blocking action; a cancelled waiter never continues its pipeline."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    context = contextvars.copy_context()
    cancelled = threading.Event()
    context.run(_blocking_cancelled.set, cancelled)

    def deliver(result, error):
        on_finished()
        if error is None:
            future.set_result(result)
        else:
            future.set_exception(error)

    def worker():
        result, error = None, None
        try:
            context.run(check_blocking_cancelled)
            result = context.run(function, *args, **kwargs)
        except BaseException as caught:
            error = caught
        try:
            loop.call_soon_threadsafe(deliver, result, error)
        except RuntimeError:
            # Closed loop: the process/run has ended. Never start another action.
            pass

    try:
        threading.Thread(target=worker, name="bounded-bot-io", daemon=True).start()
    except BaseException:
        on_finished()
        raise
    try:
        return await asyncio.shield(future)
    except BaseException:
        cancelled.set()
        # Retrieve late errors without resuming the cancelled caller.
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        raise


class RuntimeState:
    def __init__(self, capacity):
        self.loop = asyncio.get_running_loop()
        self.questions = asyncio.Semaphore(capacity)
        self.operations = asyncio.Semaphore(capacity)
        self.research = asyncio.Semaphore(capacity)
        self.in_run = contextvars.ContextVar("bot_run", default=False)
        self.batch_tasks = contextvars.ContextVar("bot_question_tasks", default=None)
        self.workers = 0


class RuntimeLimitsMixin:
    """Placed before SafePublicationMixin; all its synchronous boundaries await IO."""

    def _runtime_state(self):
        if not hasattr(self, "runtime_limits"):
            self.runtime_limits = RuntimeLimits()
        state = getattr(self, "_bounded_runtime", None)
        loop = asyncio.get_running_loop()
        if state is None or state.loop is not loop:
            if state is not None and state.workers:
                raise RuntimeError("Previous loop still owns blocking work; do not start a new run")
            capacity = self._max_concurrent_questions
            if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
                raise ValueError("Question concurrency must be a positive integer")
            state = self._bounded_runtime = RuntimeState(capacity)
        return state

    def _research_slot(self):
        return self._runtime_state().research

    async def _run_blocking(self, function, *args, _timeout=None, **kwargs):
        state = self._runtime_state()
        await state.operations.acquire()
        state.workers += 1

        def finished():
            state.workers -= 1
            state.operations.release()

        guard = asyncio.timeout(self.runtime_limits.blocking_seconds if _timeout is None else _timeout)
        try:
            async with guard:
                return await daemon_call(function, *args, on_finished=finished, **kwargs)
        except TimeoutError as error:
            if guard.expired():
                raise BlockingDeadlineExceeded("Blocking operation deadline expired; worker may still be active") from error
            raise

    def get_llm(self, purpose="default", guarantee_type=None):
        from forecasting_tools import GeneralLlm

        model = super().get_llm(purpose, guarantee_type)
        if not isinstance(model, GeneralLlm):
            return model
        if purpose == "researcher":
            from research_safety import research_model_with_safe_retry
            model = research_model_with_safe_retry(model, self)
        # Typed SDK methods must call the bounded invoke too. A shallow copy
        # preserves the model, temperature, retry policy and transport kwargs;
        # the bot's original config objects and explanation metadata stay intact.
        bounded = copy.copy(model)

        async def invoke(*args, **kwargs):
            state = self._runtime_state()
            async with state.operations:
                async with asyncio.timeout(self.runtime_limits.model_seconds):
                    return await model.invoke(*args, **kwargs)

        bounded.invoke = invoke
        return bounded

    @asynccontextmanager
    async def run_budget(self):
        state = self._runtime_state()
        if state.in_run.get():
            yield
            return
        token = state.in_run.set(True)
        try:
            async with asyncio.timeout(self.runtime_limits.run_seconds):
                yield
        finally:
            state.in_run.reset(token)

    async def forecast_on_tournament(self, tournament_id, return_exceptions=False):
        async with self.run_budget():
            questions = await self._run_blocking(
                self.metaculus_client.get_all_open_questions_from_tournament, tournament_id,
                _timeout=600,
            )
            return await self.forecast_questions(questions, return_exceptions)

    async def forecast_questions(self, questions, return_exceptions=False):
        state = self._runtime_state()
        tasks = set()
        token = state.batch_tasks.set(tasks)
        try:
            async with self.run_budget():
                return await super().forecast_questions(questions, return_exceptions)
        except BaseException:
            # asyncio.gather(return_exceptions=False) alone leaves siblings live
            # after an ordinary error. Cancel and join them before returning.
            for task in tuple(tasks):
                task.cancel()
            if tasks:
                await asyncio.gather(*tuple(tasks), return_exceptions=True)
            raise
        finally:
            state.batch_tasks.reset(token)

    async def _run_individual_question(self, question):
        state = self._runtime_state()
        task = asyncio.current_task()
        tasks = state.batch_tasks.get()
        if tasks is not None:
            tasks.add(task)
        try:
            # Queueing consumes the run budget, but not an admitted question's
            # own budget. The permit includes research through publication.
            async with state.questions:
                seconds = (self.runtime_limits.conditional_seconds if question.question_type == "conditional"
                           else self.runtime_limits.question_seconds)
                try:
                    async with asyncio.timeout(seconds):
                        return await super()._run_individual_question(question)
                finally:
                    await self._remove_notepad(question)
        finally:
            if tasks is not None:
                tasks.discard(task)


async def cancellable_cli(awaitable):
    """SIGTERM/SIGINT cancel the CLI task, including its owned question tree."""
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    previous = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            loop.add_signal_handler(signum, task.cancel)
        return await awaitable
    finally:
        for signum, handler in previous.items():
            loop.remove_signal_handler(signum)
            signal.signal(signum, handler)
