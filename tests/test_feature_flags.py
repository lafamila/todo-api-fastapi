"""feature_flags — 위치 축에 따른 prod/local 전용 표면 노출 판정.

local(client)은 버전 기록만 노출하고 prod 전용 표면은 숨긴다.
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
            flags,
            {
                "screenShare": False,
                "articles": False,
                "memberInvite": False,
                "memoVersionHistory": True,
            },
        )

    def test_server_shows_everything(self) -> None:
        flags = self._role(True, "")
        self.assertEqual(config.sync_role(), "server")
        self.assertEqual(
            flags,
            {
                "screenShare": True,
                "articles": True,
                "memberInvite": True,
                "memoVersionHistory": False,
            },
        )

    def test_disabled_legacy_stack_uses_prod_surfaces(self) -> None:
        flags = self._role(False, "")
        self.assertEqual(config.sync_role(), "disabled")
        self.assertEqual(
            flags,
            {
                "screenShare": True,
                "articles": True,
                "memberInvite": True,
                "memoVersionHistory": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
