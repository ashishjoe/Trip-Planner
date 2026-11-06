import importlib
import os
import unittest


class TripPlannerDbInitializationTest(unittest.TestCase):
    def setUp(self):
        self.db_path = "trips_v2.db"
        for path in [self.db_path]:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    def test_create_trip_when_db_is_missing(self):
        import main

        importlib.reload(main)
        trip = main.create_trip("Test Trip", "Alice", 2000, max_hotel_stars=3, allow_business_class=True)

        self.assertIsNotNone(trip["trip_id"])
        self.assertEqual(len(main.list_trips()), 1)


if __name__ == "__main__":
    unittest.main()
