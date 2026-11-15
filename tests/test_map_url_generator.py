"""
tests/test_map_url_generator.py
Pure unit tests for build_map_url (zero external network calls).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from utils.const import MAP_URL_ZOOM, SURVER_YEAR  # noqa: E402
from utils.map_url_generator import (  # noqa: E402
    LAYER_PARAMS,
    build_map_url,
)


class TestBuildMapUrl(unittest.TestCase):
    def test_api3_defaults(self):
        url = build_map_url(35.6812, 139.7671, [3])
        self.assertIn(f"surveyYear={SURVER_YEAR}", url)
        self.assertIn("layers=publicNotices", url)
        self.assertIn("layers=surveys", url)
        for code in LAYER_PARAMS["publicNotices"]:
            self.assertIn(f"publicNotices={code}", url)
        self.assertTrue(
            url.endswith(f"&x=35.6812&y=139.7671&zoom={MAP_URL_ZOOM}#{MAP_URL_ZOOM}/35.6812/139.7671")
        )

    def test_api3_year_classification_and_use_category_filtering(self):
        url_notice_only = build_map_url(
            35.68,
            139.76,
            [3],
            params={
                "year": 2024,
                "land_price_classification": "0",
                "use_category_code": [" 00 ", "05"],
            },
        )
        self.assertIn("surveyYear=2024", url_notice_only)
        self.assertIn("layers=publicNotices", url_notice_only)
        self.assertNotIn("layers=surveys", url_notice_only)
        self.assertIn("publicNotices=00", url_notice_only)
        self.assertIn("publicNotices=05", url_notice_only)
        self.assertNotIn("publicNotices=03", url_notice_only)

        url_survey_only = build_map_url(
            35.68,
            139.76,
            [3],
            params={
                "year": 2024.0,
                "land_price_classification": "1",
                "use_category_code": [" 00 ", "05"],
            },
        )
        self.assertIn("surveyYear=2024", url_survey_only)
        self.assertNotIn("layers=publicNotices", url_survey_only)
        self.assertIn("layers=surveys", url_survey_only)
        self.assertIn("publicNotices=00", url_survey_only)
        self.assertIn("publicNotices=05", url_survey_only)

    def test_api12_without_class_code_emits_all_welfare_layers(self):
        expected_layers = [
            "welfareFacility",
            "shelterLayer",
            "elderlyWelfareFacilityLayer",
            "facilityForTheHandicappedLayer",
            "socialParticipationSupportFacilitiesLayer",
            "childWelfareFacilityLayer",
            "maternalAndChildWelfareLayer",
            "otherWelfareFacilityLayer",
        ]
        for params in (None, {}, {"welfare_facility_class_code": []}):
            url = build_map_url(35.65, 139.70, [12], params=params)
            for layer_name in expected_layers:
                self.assertIn(f"layers={layer_name}", url)

    def test_api12_filtered_welfare_class_codes(self):
        url = build_map_url(
            35.65,
            139.70,
            [12],
            params={"welfare_facility_class_code": ["02", " 05 "]},
        )
        self.assertIn("layers=welfareFacility", url)
        self.assertIn("layers=elderlyWelfareFacilityLayer", url)
        self.assertIn("layers=childWelfareFacilityLayer", url)
        self.assertNotIn("layers=shelterLayer", url)
        self.assertNotIn("layers=otherWelfareFacilityLayer", url)

    def test_multi_api_layers_and_coordinates_suffix(self):
        lat, lon = 35.7497, 139.8055
        url = build_map_url(lat, lon, [4, 9, 11, 18, 29])
        for expected_layer in (
            "urbanPlanAreaLayer",
            "urbanizationControlAreaLayer",
            "school",
            "professionalTrainingCollegeLayer",
            "medicalInstitution",
            "dentalClinicLayer",
            "cityTownHall",
            "meetingFacilityLayer",
            "sedimentDisasterArea",
            "jisuberikeikaikuikiLayer",
        ):
            self.assertIn(f"layers={expected_layer}", url)
        self.assertTrue(
            url.endswith(f"&x={lat}&y={lon}&zoom={MAP_URL_ZOOM}#{MAP_URL_ZOOM}/{lat}/{lon}")
        )


if __name__ == "__main__":
    unittest.main()
