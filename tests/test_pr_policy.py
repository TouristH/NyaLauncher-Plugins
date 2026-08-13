import unittest

from tools.check_pr import evaluate_policy, parse_name_status


TRUSTED = {"touristh"}


def evaluate(changes, actor="contributor", yank_only=False, review_matches=True):
    return evaluate_policy(
        changes,
        actor,
        TRUSTED,
        lambda _path: yank_only,
        lambda _path, _actor: review_matches,
    )


class PullRequestPolicyTests(unittest.TestCase):
    def test_name_status_parser_preserves_rename_paths(self):
        self.assertEqual(
            parse_name_status("R100\0plugins/a/old.json\0plugins/a/new.json\0"),
            [("R100", ["plugins/a/old.json", "plugins/a/new.json"])],
        )

    def test_name_status_parser_handles_newlines_in_paths(self):
        self.assertEqual(
            parse_name_status("A\0plugins/dev.example.test/new\nname.json\0"),
            [("A", ["plugins/dev.example.test/new\nname.json"])],
        )

    def test_untrusted_actor_cannot_add_review(self):
        errors = evaluate([("A", ["reviews/dev.example.test/1.0.0.json"])])
        self.assertTrue(any("不能修改" in error for error in errors))

    def test_trusted_actor_can_add_or_revoke_review(self):
        for status in ("A", "D"):
            with self.subTest(status=status):
                self.assertEqual(
                    evaluate(
                        [(status, ["reviews/dev.example.test/1.0.0.json"])],
                        actor="TouristH",
                    ),
                    [],
                )

    def test_trust_is_case_insensitive(self):
        self.assertEqual(
            evaluate(
                [("A", ["reviews/dev.example.test/1.0.0.json"])],
                actor="TOURISTH",
            ),
            [],
        )

    def test_trusted_actor_cannot_claim_another_reviewer_identity(self):
        errors = evaluate(
            [("A", ["reviews/dev.example.test/1.0.0.json"])],
            actor="TouristH",
            review_matches=False,
        )
        self.assertTrue(any("reviewer 必须与 PR 作者" in error for error in errors))

    def test_untrusted_actor_cannot_change_protected_configuration(self):
        errors = evaluate([("M", ["repository.json"])])
        self.assertTrue(errors)

    def test_new_release_is_allowed_for_contributor(self):
        self.assertEqual(
            evaluate([("A", ["plugins/dev.example.test/releases/1.1.0.json"])]),
            [],
        )

    def test_historical_release_is_immutable_for_contributor(self):
        errors = evaluate([("M", ["plugins/dev.example.test/releases/1.0.0.json"])])
        self.assertTrue(any("历史版本" in error for error in errors))

    def test_trusted_actor_may_only_make_yank_only_release_change(self):
        change = [("M", ["plugins/dev.example.test/releases/1.0.0.json"])]
        self.assertEqual(evaluate(change, actor="TouristH", yank_only=True), [])
        self.assertTrue(evaluate(change, actor="TouristH", yank_only=False))

    def test_one_pull_request_cannot_span_plugins_and_reviews(self):
        errors = evaluate(
            [
                ("A", ["plugins/dev.example.one/releases/1.0.0.json"]),
                ("A", ["reviews/dev.example.two/1.0.0.json"]),
            ],
            actor="TouristH",
        )
        self.assertTrue(any("只能涉及一个插件" in error for error in errors))

    def test_renames_are_rejected(self):
        errors = evaluate(
            [
                (
                    "R100",
                    [
                        "plugins/dev.example.test/releases/1.0.0.json",
                        "plugins/dev.example.test/releases/1.0.1.json",
                    ],
                )
            ]
        )
        self.assertTrue(any("不能重命名" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
