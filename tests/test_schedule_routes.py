import unittest

from schedule_routes import normalize_schedule_route, schedule_delivery_target


class ScheduleRouteTests(unittest.TestCase):
    def test_route_defaults_to_current_ombre_identity(self):
        self.assertEqual(normalize_schedule_route("", "gale"), "gale")
        self.assertEqual(normalize_schedule_route("EVAN", "gale"), "evan")
        with self.assertRaisesRegex(ValueError, "route_to"):
            normalize_schedule_route("somebody-else", "evan")

    def test_gale_delivery_uses_gale_endpoint_and_secret(self):
        targets = {
            "evan": ("https://evan.example/api/send", "evan-secret"),
            "gale": ("https://gale.example/api/send", "gale-secret"),
        }

        self.assertEqual(
            schedule_delivery_target("gale", "evan", targets),
            ("gale", "https://gale.example/api/send", "gale-secret"),
        )


if __name__ == "__main__":
    unittest.main()
