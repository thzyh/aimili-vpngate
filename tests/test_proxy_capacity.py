import unittest

import proxy_server


class FakeSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FailingThread:
    def start(self):
        raise RuntimeError("can't start new thread")


class ProxyCapacityTests(unittest.TestCase):
    def test_one_listener_exhaustion_does_not_block_another_listener(self):
        capacity = proxy_server.ProxyCapacity(global_limit=4, per_listener_limit=2)

        self.assertTrue(capacity.try_acquire("30000"))
        self.assertTrue(capacity.try_acquire("30000"))
        self.assertFalse(capacity.try_acquire("30000"))
        self.assertTrue(capacity.try_acquire("30001"))

        capacity.release("30000")
        capacity.release("30000")
        capacity.release("30001")

    def test_thread_start_failure_closes_socket_and_releases_both_limits(self):
        capacity = proxy_server.ProxyCapacity(global_limit=1, per_listener_limit=1)
        client = FakeSocket()

        started = proxy_server.start_proxy_client_thread(
            client,
            ("127.0.0.1", 12345),
            "tun101",
            "30000",
            capacity,
            thread_factory=lambda **_kwargs: FailingThread(),
        )

        self.assertFalse(started)
        self.assertTrue(client.closed)
        self.assertTrue(capacity.try_acquire("30000"))
        capacity.release("30000")


if __name__ == "__main__":
    unittest.main()
