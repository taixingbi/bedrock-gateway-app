import asyncio
import time
import unittest

from ..concurrency import BlockingCallRunner, BlockingCallTimeoutError, ConcurrencyLimiter


class ConcurrencyLimiterTests(unittest.TestCase):
    def test_acquire_succeeds_under_limit(self):
        limiter = ConcurrencyLimiter(global_max=10, default_tenant_max=5)
        self.assertTrue(limiter.try_acquire("acme"))
        self.assertEqual(limiter.current_global_count(), 1)

    def test_acquire_fails_at_tenant_limit(self):
        limiter = ConcurrencyLimiter(global_max=10, default_tenant_max=2)
        self.assertTrue(limiter.try_acquire("acme"))
        self.assertTrue(limiter.try_acquire("acme"))
        self.assertFalse(limiter.try_acquire("acme"))  # 3rd exceeds tenant_max=2
        self.assertEqual(limiter.current_global_count(), 2)

    def test_other_tenant_unaffected_by_one_tenant_at_its_limit(self):
        limiter = ConcurrencyLimiter(global_max=10, default_tenant_max=1)
        self.assertTrue(limiter.try_acquire("acme"))
        self.assertFalse(limiter.try_acquire("acme"))
        self.assertTrue(limiter.try_acquire("other"))  # different tenant, own budget

    def test_acquire_fails_at_global_limit_even_under_tenant_limit(self):
        limiter = ConcurrencyLimiter(global_max=1, default_tenant_max=10)
        self.assertTrue(limiter.try_acquire("acme"))
        self.assertFalse(limiter.try_acquire("other"))  # global cap, not tenant cap

    def test_release_frees_a_slot(self):
        limiter = ConcurrencyLimiter(global_max=1, default_tenant_max=1)
        self.assertTrue(limiter.try_acquire("acme"))
        self.assertFalse(limiter.try_acquire("acme"))
        limiter.release("acme")
        self.assertTrue(limiter.try_acquire("acme"))

    def test_release_never_goes_negative(self):
        limiter = ConcurrencyLimiter(global_max=5, default_tenant_max=5)
        limiter.release("never-acquired")  # must not raise or underflow
        self.assertEqual(limiter.current_global_count(), 0)

    def test_per_tenant_override_max(self):
        limiter = ConcurrencyLimiter(global_max=10, default_tenant_max=1)
        self.assertTrue(limiter.try_acquire("finance", tenant_max=3))
        self.assertTrue(limiter.try_acquire("finance", tenant_max=3))
        self.assertTrue(limiter.try_acquire("finance", tenant_max=3))
        self.assertFalse(limiter.try_acquire("finance", tenant_max=3))


class BlockingCallRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_sync_function_and_returns_result(self):
        runner = BlockingCallRunner(max_workers=2, default_timeout_s=5.0)
        result = await runner.run(lambda x, y: x + y, 2, 3)
        self.assertEqual(result, 5)

    async def test_passes_kwargs_through(self):
        runner = BlockingCallRunner(max_workers=2, default_timeout_s=5.0)

        def f(*, a, b):
            return a * b

        result = await runner.run(f, a=4, b=5)
        self.assertEqual(result, 20)

    async def test_does_not_block_the_event_loop(self):
        """The actual bug this module fixes: a slow synchronous call must
        not prevent other coroutines from making progress concurrently."""
        runner = BlockingCallRunner(max_workers=2, default_timeout_s=5.0)
        progressed = []

        async def ticker():
            for i in range(5):
                await asyncio.sleep(0.01)
                progressed.append(i)

        async def slow_blocking_call():
            return await runner.run(time.sleep, 0.1)

        await asyncio.gather(ticker(), slow_blocking_call())
        # If the blocking call had run on the event loop directly, the
        # ticker couldn't have advanced during those 100ms.
        self.assertEqual(progressed, [0, 1, 2, 3, 4])

    async def test_raises_blocking_call_timeout_error_on_timeout(self):
        runner = BlockingCallRunner(max_workers=2, default_timeout_s=5.0)
        with self.assertRaises(BlockingCallTimeoutError):
            await runner.run(time.sleep, 0.2, timeout_s=0.01)

    async def test_propagates_the_original_exception_on_success_path(self):
        runner = BlockingCallRunner(max_workers=2, default_timeout_s=5.0)

        def boom():
            raise ValueError("real failure")

        with self.assertRaises(ValueError):
            await runner.run(boom)


if __name__ == "__main__":
    unittest.main()
