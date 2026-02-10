import unittest

from trakt_scrobbler.file_info import cleanup_guess


class TestCleanupGuess(unittest.TestCase):
    def test_episode_season_year_collision_defaults_to_season_1(self):
        # Common case: a year in a parent folder is parsed as "season".
        guess = {
            "type": "episode",
            "title": "Reply 1988",
            "year": 2015,
            "season": 2015,
            "episode": 3,
        }
        cleaned = cleanup_guess(guess)
        self.assertIsNotNone(cleaned)
        self.assertEqual(cleaned["season"], 1)
        self.assertEqual(cleaned["episode"], 3)
        self.assertEqual(cleaned["year"], 2015)

    def test_episode_normal_season_untouched(self):
        guess = {
            "type": "episode",
            "title": "Reply 1988",
            "year": 2015,
            "season": 1,
            "episode": 2,
        }
        cleaned = cleanup_guess(guess)
        self.assertIsNotNone(cleaned)
        self.assertEqual(cleaned["season"], 1)
        self.assertEqual(cleaned["episode"], 2)

