import unittest
from pathlib import Path
from src.spatial_query import TokyoSpatialDB, haversine_distance_m

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tokyo_spatial.sqlite"

class TestTokyoSpatialDB(unittest.TestCase):
    def test_haversine_distance(self):
        d0 = haversine_distance_m(35.6628, 139.7188, 35.6628, 139.7188)
        self.assertEqual(round(d0), 0)

        d1 = haversine_distance_m(35.0, 139.0, 36.0, 139.0)
        self.assertTrue(110000 < d1 < 112000)

    @unittest.skipUnless(DB_PATH.exists(), "tokyo_spatial.sqlite が存在しない場合はスキップ")
    def test_tokyo_spatial_db_7layers(self):
        db = TokyoSpatialDB(DB_PATH)
        try:
            # 南青山 5丁目 (35.662846, 139.718817)
            res = db.lookup_carte_7layers(35.662846, 139.718817)

            # 1. 用途地域
            self.assertIn(res["zoning"]["status"], ("ok", "ambiguous"))
            self.assertIn("第一種中高層住居専用地域", res["zoning"]["primary"]["name"])

            # 2. 地価公示
            self.assertEqual(res["land_price"]["status"], "ok")
            self.assertGreater(len(res["land_price"]["nearest"]), 0)
            self.assertGreater(res["land_price"]["nearest"][0]["price_yen_per_sqm"], 0)

            # 3. 学校区
            self.assertEqual(res["school_district"]["status"], "ok")
            self.assertIn("青南小学校", res["school_district"]["elementary_school"]["school_name"])
            self.assertIn("青山中学校", res["school_district"]["junior_high_school"]["school_name"])
            self.assertEqual(res["school_district"]["elementary_school"]["city_name"], "港区")

            # 4. 洪水浸水想定 (南青山高台は区域外)
            self.assertEqual(res["flood_risk"]["status"], "no_match")
            self.assertFalse(res["flood_risk"]["is_flooded"])

            # 5. 土砂災害 (南青山フラット面は区域外)
            self.assertEqual(res["landslide_risk"]["status"], "no_match")
            self.assertFalse(res["landslide_risk"]["is_warning_zone"])

            # 6. 医療機関
            self.assertEqual(res["medical"]["status"], "ok")
            self.assertGreater(len(res["medical"]["nearest"]), 0)

            # 7. 保育施設
            self.assertEqual(res["nursery"]["status"], "ok")
            self.assertGreater(len(res["nursery"]["nearest"]), 0)
        finally:
            db.close()

    @unittest.skipUnless(DB_PATH.exists(), "tokyo_spatial.sqlite が存在しない場合はスキップ")
    def test_tokyo_spatial_hazard_hits(self):
        db = TokyoSpatialDB(DB_PATH)
        try:
            # 境川沿い洪水ヒット (35.503612, 139.470390)
            flood_res = db.lookup_carte_7layers(35.503612, 139.470390)
            self.assertEqual(flood_res["flood_risk"]["status"], "ok")
            self.assertTrue(flood_res["flood_risk"]["is_flooded"])
            self.assertGreater(flood_res["flood_risk"]["max_scenario"]["rank_code"], 0)

            # 稲城市急傾斜地土砂災害ヒット (35.646240, 139.492151)
            landslide_res = db.lookup_carte_7layers(35.646240, 139.492151)
            self.assertEqual(landslide_res["landslide_risk"]["status"], "ok")
            self.assertTrue(landslide_res["landslide_risk"]["is_warning_zone"])
            self.assertGreater(len(landslide_res["landslide_risk"]["zones"]), 0)
        finally:
            db.close()

    def test_mesh250_calculation(self):
        from src.spatial_query import latlon_to_mesh250
        # 南青山 (35.662846, 139.718817)
        code = latlon_to_mesh250(35.662846, 139.718817)
        self.assertEqual(len(code), 10)
        self.assertTrue(code.startswith("5339"))

    @unittest.skipUnless(DB_PATH.exists(), "tokyo_spatial.sqlite が存在しない場合はスキップ")
    def test_tokyo_spatial_db_13layers_fail_closed(self):
        db = TokyoSpatialDB(DB_PATH)
        try:
            res = db.lookup_comprehensive_13layers(35.662846, 139.718817)
            # 既存7系統のキーが存在すること
            self.assertIn("zoning", res)
            self.assertIn("land_price", res)
            self.assertIn("school_district", res)
            # 新規6系統がフェイルクローズ（未投入時は not_loaded）で安全に返却されること
            self.assertEqual(res["mesh250_code"], "5339359741")
            self.assertEqual(res["fire_prevention"]["status"], "not_loaded")
            self.assertEqual(res["embankment"]["status"], "not_loaded")
            self.assertEqual(res["coastal_hazard"]["status"], "not_loaded")
            self.assertEqual(res["liquefaction"]["status"], "not_loaded")
            self.assertEqual(res["population_projection"]["status"], "not_loaded")
            self.assertEqual(res["district_plan"]["status"], "not_loaded")
        finally:
            db.close()

if __name__ == "__main__":
    unittest.main()
