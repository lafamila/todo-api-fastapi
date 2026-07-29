"""feature_flags — 스택 성격별 prod 전용 표면 노출 판정.

client(노트북 실사용) 만 숨기고, disabled(dev)·server(prod) 는 전부 노출한다.
"""

import unittest

from src import config


class FeatureFlagsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._enabled = config.SYNC_ENABLED
        self._peer = config.SYNC_PEER_URL

    def tearDown(self) -> None:
        config.SYNC_ENABLED = self._enabled
        config.SYNC_PEER_URL = self._peer

    def _role(self, enabled: bool, peer: str) -> dict:
        config.SYNC_ENABLED = enabled
        config.SYNC_PEER_URL = peer
        return config.feature_flags()

    def test_client_hides_prod_only_surfaces(self) -> None:
        flags = self._role(True, "https://todo.example")
        self.assertEqual(config.sync_role(), "client")
        self.assertEqual(
            flags, {"screenShare": False, "articles": False, "memberInvite": False}
        )

    def test_server_shows_everything(self) -> None:
        flags = self._role(True, "")
        self.assertEqual(config.sync_role(), "server")
        self.assertTrue(all(flags.values()))

    def test_disabled_dev_stack_shows_everything(self) -> None:
        flags = self._role(False, "")
        self.assertEqual(config.sync_role(), "disabled")
        self.assertTrue(all(flags.values()))


if __name__ == "__main__":
    unittest.main()
