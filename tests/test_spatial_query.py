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
    def test_tokyo_spatial_db_13layers_comprehensive(self):
        db = TokyoSpatialDB(DB_PATH)
        try:
            # 1. 南青山 5丁目 (35.662846, 139.718817)
            res = db.lookup_comprehensive_13layers(35.662846, 139.718817)
            self.assertEqual(res["mesh250_code"], "5339359741")
            self.assertEqual(res["fire_prevention"]["status"], "ok")
            self.assertEqual(res["fire_prevention"]["designation"], "準防火地域")
            self.assertEqual(res["liquefaction"]["status"], "ok")
            self.assertEqual(res["liquefaction"]["assessment"], "classified")
            self.assertEqual(res["embankment"]["status"], "no_match")
            self.assertFalse(res["embankment"]["is_designated"])
            self.assertEqual(res["coastal_hazard"]["status"], "no_match")
            self.assertFalse(res["coastal_hazard"]["is_inundated"])
            self.assertEqual(res["population_projection"]["status"], "ok")
            self.assertGreater(res["population_projection"]["maintenance_ratio_percent"], 100.0)
            self.assertEqual(res["district_plan"]["status"], "ok")

            # 2. 豊洲 3丁目 (35.6552, 139.7960) - 防火地域・埋立地・高潮浸水想定ヒット
            toyosu = db.lookup_comprehensive_13layers(35.6552, 139.7960)
            self.assertEqual(toyosu["fire_prevention"]["designation"], "防火地域")
            self.assertEqual(toyosu["liquefaction"]["landform_name"], "埋立地")
            self.assertEqual(toyosu["liquefaction"]["relative_score"], 5)
            self.assertTrue(toyosu["coastal_hazard"]["is_inundated"])
            self.assertTrue(toyosu["coastal_hazard"]["storm_surge"]["is_designated"])

            # 3. 千代田区神田練塀町 (35.7005, 139.7745) - 地区計画・高度利用地区ヒット
            kanda = db.lookup_comprehensive_13layers(35.7005, 139.7745)
            self.assertTrue(kanda["district_plan"]["has_plan"])
            plan_kinds = {p["kind"] for p in kanda["district_plan"]["plans"]}
            self.assertIn("district_plan", plan_kinds)
            self.assertIn("high_use", plan_kinds)

            # 4. 港区白金 (35.639009, 139.726019) - 大規模盛土造成地ヒット
            emb = db.lookup_comprehensive_13layers(35.639009, 139.726019)
            self.assertEqual(emb["embankment"]["status"], "ok")
            self.assertTrue(emb["embankment"]["is_designated"])
            self.assertEqual(emb["embankment"]["zones"][0]["fill_type"], "谷埋め型盛土")
        finally:
            db.close()

    @staticmethod
    def _create_empty_13layer_conn():
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE dataset_meta (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE zoning (id INTEGER PRIMARY KEY, name TEXT, code TEXT, building_coverage INTEGER, floor_area_ratio INTEGER, city_code TEXT, geom_wkb BLOB)")
        conn.execute("CREATE VIRTUAL TABLE zoning_idx USING rtree(id, minX, maxX, minY, maxY)")
        conn.execute("CREATE TABLE land_price (id INTEGER PRIMARY KEY, price INTEGER, change_rate REAL, address TEXT, station_name TEXT, station_distance INTEGER, lat REAL, lon REAL)")
        conn.execute("CREATE VIRTUAL TABLE land_price_idx USING rtree(id, minX, maxX, minY, maxY)")
        conn.execute("CREATE TABLE school_district (id INTEGER PRIMARY KEY, school_type TEXT, school_name TEXT, school_address TEXT, city_name TEXT, geom_wkb BLOB)")
        conn.execute("CREATE VIRTUAL TABLE school_district_idx USING rtree(id, minX, maxX, minY, maxY)")
        conn.execute("CREATE TABLE flood_risk (id INTEGER PRIMARY KEY, river_name TEXT, rank_code INTEGER, depth_label TEXT, geom_wkb BLOB)")
        conn.execute("CREATE VIRTUAL TABLE flood_risk_idx USING rtree(id, minX, maxX, minY, maxY)")
        conn.execute("CREATE TABLE landslide_risk (id INTEGER PRIMARY KEY, hazard_type TEXT, zone_type TEXT, address TEXT, geom_wkb BLOB)")
        conn.execute("CREATE VIRTUAL TABLE landslide_risk_idx USING rtree(id, minX, maxX, minY, maxY)")
        conn.execute("CREATE TABLE medical_facility (id INTEGER PRIMARY KEY, name TEXT, facility_type TEXT, address TEXT, departments TEXT, is_emergency INTEGER, lat REAL, lon REAL)")
        conn.execute("CREATE VIRTUAL TABLE medical_facility_idx USING rtree(id, minX, maxX, minY, maxY)")
        conn.execute("CREATE TABLE nursery_facility (id INTEGER PRIMARY KEY, name TEXT, address TEXT, facility_type_code TEXT, lat REAL, lon REAL)")
        conn.execute("CREATE VIRTUAL TABLE nursery_facility_idx USING rtree(id, minX, maxX, minY, maxY)")
        conn.execute("CREATE TABLE planning_zone (id INTEGER PRIMARY KEY, kind TEXT, name TEXT, source_code TEXT, city_code TEXT, geom_wkb BLOB)")
        conn.execute("CREATE VIRTUAL TABLE planning_zone_idx USING rtree(id, minX, maxX, minY, maxY)")
        conn.execute("CREATE TABLE embankment (id INTEGER PRIMARY KEY, fill_code TEXT, fill_number TEXT, city_code TEXT, geom_wkb BLOB)")
        conn.execute("CREATE VIRTUAL TABLE embankment_idx USING rtree(id, minX, maxX, minY, maxY)")
        conn.execute("CREATE TABLE coastal_inundation (id INTEGER PRIMARY KEY, kind TEXT, scenario_key TEXT, depth_raw TEXT, depth_min_m REAL, depth_max_m REAL, geom_wkb BLOB)")
        conn.execute("CREATE VIRTUAL TABLE coastal_inundation_idx USING rtree(id, minX, maxX, minY, maxY)")
        conn.execute("CREATE TABLE liquefaction_mesh (mesh_code TEXT PRIMARY KEY, landform_name TEXT, tendency_label TEXT, relative_score INTEGER, assessment TEXT, note TEXT) WITHOUT ROWID")
        conn.execute("CREATE TABLE population_mesh (mesh_code TEXT PRIMARY KEY, city_code TEXT, population_2020 REAL, population_2050 REAL, disclosure_2020 TEXT, disclosure_2050 TEXT) WITHOUT ROWID")
        conn.commit()
        return conn

    def test_tokyo_spatial_db_13layers_fail_closed_when_empty(self):
        conn = self._create_empty_13layer_conn()
        db = TokyoSpatialDB(conn)
        try:
            res = db.lookup_comprehensive_13layers(35.662846, 139.718817)
            self.assertEqual(res["zoning"]["status"], "not_loaded")
            self.assertEqual(res["flood_risk"]["status"], "not_loaded")
            self.assertIsNone(res["flood_risk"]["is_flooded"])
            self.assertEqual(res["landslide_risk"]["status"], "not_loaded")
            self.assertIsNone(res["landslide_risk"]["is_warning_zone"])
            self.assertEqual(res["fire_prevention"]["status"], "not_loaded")
            self.assertEqual(res["embankment"]["status"], "not_loaded")
            self.assertEqual(res["coastal_hazard"]["status"], "not_loaded")
            self.assertEqual(res["liquefaction"]["status"], "not_loaded")
            self.assertEqual(res["population_projection"]["status"], "not_loaded")
            self.assertEqual(res["district_plan"]["status"], "not_loaded")
        finally:
            db.close()

    def test_partial_sublayer_and_city_coverage_fail_closed(self):
        from shapely.geometry import box
        conn = self._create_empty_13layer_conn()
        poly_wkb = box(139.70, 35.65, 139.75, 35.68).wkb

        # 1) planning_zone に高度地区のみ収録・防火地域未収録の状態で防火地域が no_match と誤判定されないこと
        conn.execute(
            "INSERT INTO planning_zone (id, kind, name, source_code, city_code, geom_wkb) VALUES (1, 'height_district', '第3種高度地区', '3', '13103', ?)",
            (poly_wkb,)
        )
        conn.execute("INSERT INTO planning_zone_idx (id, minX, maxX, minY, maxY) VALUES (1, 139.70, 139.75, 35.65, 35.68)")

        # 2) coastal_inundation に高潮のみ収録・津波未収録の状態で津波が is_designated: False にならないこと
        far_poly_wkb = box(139.80, 35.60, 139.85, 35.65).wkb
        conn.execute(
            "INSERT INTO coastal_inundation (id, kind, scenario_key, depth_raw, depth_min_m, depth_max_m, geom_wkb) VALUES (1, 'storm_surge', 'A49', '1m以上3m未満', 1.0, 3.0, ?)",
            (far_poly_wkb,)
        )
        conn.execute("INSERT INTO coastal_inundation_idx (id, minX, maxX, minY, maxY) VALUES (1, 139.80, 139.85, 35.60, 35.65)")
        conn.commit()

        db = TokyoSpatialDB(conn)
        try:
            res = db.lookup_comprehensive_13layers(35.662846, 139.718817)
            self.assertEqual(res["fire_prevention"]["status"], "not_loaded")
            self.assertEqual(res["coastal_hazard"]["status"], "not_loaded")
            self.assertIsNone(res["coastal_hazard"]["is_inundated"])
            self.assertEqual(res["coastal_hazard"]["storm_surge"]["is_designated"], False)
            self.assertIsNone(res["coastal_hazard"]["tsunami"]["is_designated"])
            self.assertEqual(res["coastal_hazard"]["tsunami"]["status"], "not_loaded")

            # 高潮ヒット地点でも津波は not_loaded (is_designated=None) を維持すること
            hit_coastal = db.lookup_comprehensive_13layers(35.62, 139.82)
            self.assertEqual(hit_coastal["coastal_hazard"]["status"], "partial_not_loaded")
            self.assertTrue(hit_coastal["coastal_hazard"]["is_inundated"])
            self.assertTrue(hit_coastal["coastal_hazard"]["storm_surge"]["is_designated"])
            self.assertIsNone(hit_coastal["coastal_hazard"]["tsunami"]["is_designated"])
        finally:
            db.close()

        # 3) JSON配列メタデータ round-trip + 片側サブレイヤー未収録時の has_plan=None 検証
        conn2 = self._create_empty_13layer_conn()
        # 13103 のみ bouka / koudori 収録、chikukei は 13101 のみ収録
        conn2.execute("INSERT INTO dataset_meta VALUES ('a55_bouka_cities', '[\"13101\", \"13103\"]', '2026-10-14')")
        conn2.execute("INSERT INTO dataset_meta VALUES ('a55_chikukei_cities', '[\"13101\"]', '2026-10-14')")
        conn2.execute("INSERT INTO dataset_meta VALUES ('a55_koudori_cities', '[\"13101\", \"13103\"]', '2026-10-14')")
        # ダミー防火地域・地区計画・高度利用地区レコード（遠方）
        conn2.execute(
            "INSERT INTO planning_zone VALUES (10, 'fire', '防火地域', '24', '13101', ?)",
            (far_poly_wkb,)
        )
        conn2.execute("INSERT INTO planning_zone_idx VALUES (10, 139.80, 139.85, 35.60, 35.65)")
        conn2.execute(
            "INSERT INTO planning_zone VALUES (11, 'district_plan', 'テスト地区計画', '1', '13101', ?)",
            (far_poly_wkb,)
        )
        conn2.execute("INSERT INTO planning_zone_idx VALUES (11, 139.80, 139.85, 35.60, 35.65)")
        conn2.execute(
            "INSERT INTO planning_zone VALUES (12, 'high_use', '高度利用地区', '18', '13101', ?)",
            (far_poly_wkb,)
        )
        conn2.execute("INSERT INTO planning_zone_idx VALUES (12, 139.80, 139.85, 35.60, 35.65)")

        # a) 自治体不明（zoning / population_mesh 空）の場合は detected_city="" のため not_loaded にフェイルクローズ
        try:
            db2_unknown = TokyoSpatialDB(conn2)
            res_unk = db2_unknown.lookup_comprehensive_13layers(35.662846, 139.718817)
            self.assertEqual(res_unk["fire_prevention"]["status"], "not_loaded")
            self.assertIsNone(res_unk["district_plan"]["has_plan"])
            self.assertEqual(res_unk["district_plan"]["status"], "not_loaded")

            # b) 13103 (港区) と判明した場合: bouka は収録済みなので no_match、chikukei は未収録なので has_plan=None
            conn2.execute("INSERT INTO population_mesh VALUES ('5339359741', '13103', 100.0, 110.0, 'normal', 'normal')")
            conn2.commit()
            db2_minato = TokyoSpatialDB(conn2)
            res_minato = db2_minato.lookup_comprehensive_13layers(35.662846, 139.718817)
            self.assertEqual(res_minato["fire_prevention"]["status"], "no_match")
            self.assertEqual(res_minato["fire_prevention"]["designation"], "指定なし（法22条区域等）")
            self.assertIsNone(res_minato["district_plan"]["has_plan"])
            self.assertEqual(res_minato["district_plan"]["status"], "not_loaded")

            # c) 13101 (千代田区) と判明した場合: chikukei と koudori の両方が収録済みなので has_plan=False (no_match)
            conn2.execute("UPDATE population_mesh SET city_code = '13101' WHERE mesh_code = '5339359741'")
            conn2.commit()
            db2_chiyoda = TokyoSpatialDB(conn2)
            res_chiyoda = db2_chiyoda.lookup_comprehensive_13layers(35.662846, 139.718817)
            self.assertEqual(res_chiyoda["district_plan"]["status"], "no_match")
            self.assertEqual(res_chiyoda["district_plan"]["has_plan"], False)
        finally:
            conn2.close()

    def test_candidate_truncation_fail_closed(self):
        from shapely.geometry import Polygon
        conn = self._create_empty_13layer_conn()
        # 凹型（L字）ポリゴンのBBoxは (139.718817, 35.662846) を含むが covers(pt) は False のダミーを3件挿入
        l_shape = Polygon([
            (139.70, 35.65), (139.75, 35.65), (139.75, 35.655),
            (139.705, 35.655), (139.705, 35.68), (139.70, 35.68)
        ])
        for i in range(1, 5):
            conn.execute(
                "INSERT INTO embankment (id, fill_code, fill_number, city_code, geom_wkb) VALUES (?, '1', ?, '13103', ?)",
                (i, f"F{i}", l_shape.wkb)
            )
            conn.execute("INSERT INTO embankment_idx (id, minX, maxX, minY, maxY) VALUES (?, 139.70, 139.75, 35.65, 35.68)", (i,))
        conn.commit()

        # max_spatial_candidates=3 に対して候補が4件返る場合、切り捨てを検知して not_loaded にフェイルクローズすること
        db = TokyoSpatialDB(conn, max_spatial_candidates=3)
        try:
            res = db.lookup_comprehensive_13layers(35.662846, 139.718817)
            self.assertEqual(res["embankment"]["status"], "not_loaded")
        finally:
            db.close()

    def test_coastal_depth_sorting_and_population_disclosure(self):
        from shapely.geometry import Point, box
        from scripts.build_tokyo_spatial_db import _extract_polygons

        # MultiPolygon 分割・穴あき・自己交差 (make_valid) テスト
        bowtie = {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.0], [2.0, 2.0], [2.0, 0.0], [0.0, 2.0], [0.0, 0.0]]]
        }
        parts = _extract_polygons(bowtie)
        self.assertGreaterEqual(len(parts), 2)
        for p in parts:
            self.assertEqual(p.geom_type, "Polygon")
            self.assertTrue(p.is_valid)

        # 離散 MultiPolygon + 中抜き穴（ドーナツ型）の covers() 境界・穴除外テスト
        multipoly_with_hole = {
            "type": "MultiPolygon",
            "coordinates": [
                [
                    [[139.70, 35.60], [139.80, 35.60], [139.80, 35.70], [139.70, 35.70], [139.70, 35.60]],
                    [[139.73, 35.63], [139.77, 35.63], [139.77, 35.67], [139.73, 35.67], [139.73, 35.63]],
                ],
                [
                    [[139.90, 35.80], [139.95, 35.80], [139.95, 35.85], [139.90, 35.85], [139.90, 35.80]]
                ],
            ],
        }
        mp_parts = _extract_polygons(multipoly_with_hole)
        self.assertEqual(len(mp_parts), 2)
        donut = mp_parts[0]
        # 外周と内周の間は covers=True、内周の穴の内部は covers=False、穴の境界線上は covers=True
        self.assertTrue(donut.covers(Point(139.71, 35.61)))
        self.assertFalse(donut.covers(Point(139.75, 35.65)))
        self.assertTrue(donut.covers(Point(139.73, 35.65)))

        # 10m以上20m未満 vs 20m以上 の順序非依存ソート検証 + 秘匿人口テスト
        for reverse_insert in (False, True):
            conn = self._create_empty_13layer_conn()
            poly_wkb = box(139.70, 35.65, 139.80, 35.70).wkb
            rows = [
                (1, "storm_surge", "A49", "10m以上20m未満", 10.0, 20.0, poly_wkb),
                (2, "storm_surge", "A49", "20m以上", 20.0, None, poly_wkb),
                (3, "tsunami", "A40", "0.3m未満", 0.0, 0.3, poly_wkb),
            ]
            if reverse_insert:
                rows.reverse()
            for idx_id, (_, kind, skey, draw, dmin, dmax, wkb_val) in enumerate(rows, 1):
                conn.execute(
                    "INSERT INTO coastal_inundation VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (idx_id, kind, skey, draw, dmin, dmax, wkb_val)
                )
                conn.execute(
                    "INSERT INTO coastal_inundation_idx VALUES (?, 139.70, 139.80, 35.65, 35.70)",
                    (idx_id,)
                )
            # 秘匿メッシュ
            conn.execute(
                "INSERT INTO population_mesh VALUES ('5339359741', '13103', 100.0, 120.0, 'normal', 'secret')"
            )
            conn.commit()

            db = TokyoSpatialDB(conn)
            try:
                res = db.lookup_comprehensive_13layers(35.662846, 139.718817)
                self.assertEqual(res["coastal_hazard"]["storm_surge"]["max_depth_raw"], "20m以上")
                self.assertIsNone(res["population_projection"]["change_rate_percent"])
                self.assertIsNone(res["population_projection"]["maintenance_ratio_percent"])
            finally:
                db.close()

    @unittest.skipUnless(DB_PATH.exists(), "tokyo_spatial.sqlite が存在しない場合はスキップ")
    def test_sorai_index_computation_and_zero_emoji_guard(self):
        import json
        import re
        emoji_pattern = re.compile(
            r"[\U0001F300-\U0001FAFF\u2600-\u26FF\u2700-\u27BF]"
        )

        db = TokyoSpatialDB(DB_PATH)
        try:
            # 1. 台地・住居系エリア（広尾ガーデンヒルズ: 35.653, 139.721）
            hiroo_idx = db.evaluate_sorai_index(35.653, 139.721)
            self.assertEqual(hiroo_idx["version"], "1.0")
            self.assertGreaterEqual(hiroo_idx["total_score"], 85)
            self.assertEqual(hiroo_idx["rank"], "S")
            self.assertEqual(
                hiroo_idx["signature_indices"]["silent_value_index"]["category"],
                "プレミアム静寂資産（高地価 × 最高位住環境）",
            )
            self.assertEqual(
                hiroo_idx["signature_indices"]["demographic_momentum"]["category"],
                "実需牽引型（2050年人口増 × 地価堅調）",
            )

            # 2. 商業・高容積率エリア（六本木交差点付近: 35.6628, 139.7314）
            roppongi_idx = db.evaluate_sorai_index(35.6628, 139.7314)
            self.assertLess(
                roppongi_idx["pillars"]["tranquility"]["score"],
                hiroo_idx["pillars"]["tranquility"]["score"],
            )

            # 3. 低地・湾岸エリア（豊洲: 35.655, 139.796）
            toyosu_idx = db.evaluate_sorai_index(35.655, 139.796)
            self.assertLess(
                toyosu_idx["pillars"]["resilience"]["score"],
                hiroo_idx["pillars"]["resilience"]["score"],
            )

            # 4. 適正地価 × 第一種低層住専・ローム台地（目黒区八雲3丁目: 35.6195, 139.6695）
            yakumo_idx = db.evaluate_sorai_index(35.6195, 139.6695)
            self.assertEqual(yakumo_idx["rank"], "S")
            self.assertEqual(
                yakumo_idx["signature_indices"]["silent_value_index"]["category"],
                "高効率・隠れ静寂立地（適正地価 × 優良住環境）",
            )

            # 5. 絵文字ゼロ（intent_portal_hazard_emoji_cl_01）検証
            for idx_obj in (hiroo_idx, roppongi_idx, toyosu_idx, yakumo_idx):
                serialized = json.dumps(idx_obj, ensure_ascii=False)
                self.assertIsNone(
                    emoji_pattern.search(serialized),
                    f"Emoji detected in Sorai Index output: {serialized}",
                )
        finally:
            db.close()

    def test_compute_sorai_index_edge_cases_and_fail_closed(self):
        import json
        from spatial_query import compute_sorai_index

        # 1. 全欠測 ({}) 入力時に安全断定（「住居系地域」「リスクは低く抑えられています」）を出さずCランクにフェイルクローズすること
        empty_res = compute_sorai_index({})
        self.assertEqual(empty_res["rank"], "C")
        joined_cp = " ".join(empty_res["inspection_checkpoints"])
        self.assertNotIn("住居系地域に位置しますが", joined_cp)
        self.assertNotIn("公的ハザード上の浸水・地盤リスクは低く抑えられていますが", joined_cp)
        self.assertIn("未確定", joined_cp)

        # 2. 地価 price_yen_per_sqm=None かつ pop_ratio/change_rate 数値時に TypeError が起きず、「適正地価」とも誤認しないこと
        partial_carte = {
            "zoning": {"status": "ok", "primary": {"name": "第一種低層住居専用地域", "floor_area_ratio_percent": 150}},
            "liquefaction": {"relative_score": 1, "landform_name": "ローム台地"},
            "flood_risk": {"is_flooded": False},
            "landslide_risk": {"is_warning_zone": False, "is_red_zone": False},
            "embankment": {"is_designated": False},
            "coastal_hazard": {"is_inundated": False},
            "population_projection": {"maintenance_ratio_percent": 110.0},
            "land_price": {"nearest": [{"price_yen_per_sqm": None, "change_rate_percent": 3.5}]},
        }
        res_null_lp = compute_sorai_index(partial_carte)
        self.assertIn("価格未確定", res_null_lp["pillars"]["asset_defense"]["assessment"])
        self.assertNotEqual(
            res_null_lp["signature_indices"]["silent_value_index"]["category"],
            "高効率・隠れ静寂立地（適正地価 × 優良住環境）",
        )

        # 3. 非有限値 (inf, nan) および bool 混入時に満点誤判定せず json.dumps(allow_nan=False) が通ること
        non_finite_carte = {
            "population_projection": {"maintenance_ratio_percent": float("inf")},
            "land_price": {"nearest": [{"price_yen_per_sqm": True, "change_rate_percent": float("nan")}]},
            "liquefaction": {"relative_score": True},
        }
        res_nf = compute_sorai_index(non_finite_carte)
        self.assertLess(res_nf["pillars"]["asset_defense"]["score"], 20)
        serialized_nf = json.dumps(res_nf, ensure_ascii=False, allow_nan=False)
        self.assertTrue(len(serialized_nf) > 0)

        # 4. 境界候補 (ambiguous) の重複ポリゴンに対するスコア不変性と安全側評価
        cand_low = {"name": "第一種低層住居専用地域", "floor_area_ratio_percent": 150}
        cand_comm = {"name": "商業地域", "floor_area_ratio_percent": 600}
        res_single = compute_sorai_index({"zoning": {"status": "ambiguous", "candidates": [cand_low, cand_comm]}})
        res_dup = compute_sorai_index({"zoning": {"status": "ambiguous", "candidates": [cand_low, cand_low, cand_low, cand_comm]}})
        self.assertEqual(
            res_single["pillars"]["tranquility"]["score"],
            res_dup["pillars"]["tranquility"]["score"],
        )
        self.assertEqual(
            res_single["pillars"]["tranquility"]["floor_area_ratio_percent"],
            600,
        )

    @unittest.skipUnless(DB_PATH.exists(), "tokyo_spatial.sqlite が存在しない場合はスキップ")
    def test_lookup_station_or_address_ranking(self):
        import json
        import re
        from src.spatial_query import normalize_location_query, extract_chome_label

        # 1. ヘルパー関数単体テスト
        self.assertEqual(
            normalize_location_query("東京都　渋谷区恵比寿３丁目２８番２６外"),
            "渋谷区恵比寿3丁目28番26",
        )
        self.assertEqual(
            extract_chome_label("東京都　渋谷区恵比寿３丁目２８番２６外"),
            "渋谷区恵比寿3丁目",
        )
        self.assertEqual(
            extract_chome_label("東京都　足立区千住旭町６６番１７"),
            "足立区千住旭町",
        )
        self.assertEqual(normalize_location_query("恵比寿駅"), "恵比寿")
        self.assertEqual(normalize_location_query(""), "")
        self.assertEqual(normalize_location_query(None), "")

        db = TokyoSpatialDB(DB_PATH)
        try:
            # 2. 駅名完全一致検索（恵比寿駅）
            res_ebisu = db.lookup_station_or_address_ranking("恵比寿", limit=8)
            self.assertEqual(res_ebisu["status"], "ok")
            self.assertEqual(res_ebisu["area_summary"]["area_label"], "恵比寿駅")
            self.assertGreater(res_ebisu["point_count"], 0)
            self.assertLessEqual(len(res_ebisu["ranked_locations"]), 8)

            # ソート順降順の検証
            scores = [loc["sorai_index"]["total_score"] for loc in res_ebisu["ranked_locations"]]
            self.assertEqual(scores, sorted(scores, reverse=True))

            # area_summary 整合性
            self.assertEqual(res_ebisu["area_summary"]["max_score"], scores[0])
            self.assertEqual(res_ebisu["area_summary"]["min_score"], scores[-1])
            self.assertEqual(
                res_ebisu["area_summary"]["score_spread"],
                scores[0] - scores[-1],
            )
            self.assertEqual(
                res_ebisu["area_summary"]["best_chome"],
                res_ebisu["ranked_locations"][0]["chome_label"],
            )

            # cvr_cta 整合性
            cvr = res_ebisu["cvr_cta"]
            self.assertIn("恵比寿駅", cvr["primary_button_label"])
            self.assertIn(res_ebisu["area_summary"]["best_chome"], cvr["headline"])
            self.assertIn("LINE", cvr["secondary_button_label"])

            # 3. 駅名検索（中野駅）
            res_nakano = db.lookup_station_or_address_ranking("中野駅", limit=8)
            self.assertEqual(res_nakano["status"], "ok")
            self.assertEqual(res_nakano["area_summary"]["area_label"], "中野駅")
            self.assertGreater(res_nakano["point_count"], 0)

            # 4. 住所検索（渋谷区恵比寿3丁目）
            res_addr = db.lookup_station_or_address_ranking("渋谷区恵比寿3丁目", limit=5)
            self.assertEqual(res_addr["status"], "ok")
            self.assertEqual(res_addr["area_summary"]["area_label"], "渋谷区恵比寿3丁目")
            self.assertGreater(res_addr["point_count"], 0)
            self.assertIn("渋谷区恵比寿3丁目", res_addr["ranked_locations"][0]["address"])

            # 全角混じり住所検索
            res_addr_full = db.lookup_station_or_address_ranking("東京都　渋谷区恵比寿３丁目２８番２６外", limit=5)
            self.assertEqual(res_addr_full["status"], "ok")
            self.assertGreater(res_addr_full["point_count"], 0)

            # 5. Fail-Closed 動作検証
            # 空クエリ
            res_empty = db.lookup_station_or_address_ranking("")
            self.assertEqual(res_empty["status"], "invalid_query")
            self.assertEqual(res_empty["point_count"], 0)
            self.assertEqual(res_empty["ranked_locations"], [])
            self.assertIsNone(res_empty["area_summary"])

            res_spaces = db.lookup_station_or_address_ranking("   ")
            self.assertEqual(res_spaces["status"], "invalid_query")

            res_none = db.lookup_station_or_address_ranking(None)
            self.assertEqual(res_none["status"], "invalid_query")

            # 存在しない駅・住所
            res_nomatch = db.lookup_station_or_address_ranking("架空の存在しない駅名99999")
            self.assertEqual(res_nomatch["status"], "no_match")
            self.assertEqual(res_nomatch["point_count"], 0)
            self.assertEqual(res_nomatch["ranked_locations"], [])

            # 6. 絵文字ゼロ・終助詞ゼロ検証
            emoji_pattern = re.compile(
                r"[\U0001F300-\U0001FAFF\u2600-\u26FF\u2700-\u27BF]"
            )
            casual_endings = re.compile(r"(ですね|ですよ|のですね)")

            for res_obj in (res_ebisu, res_nakano, res_addr):
                serialized = json.dumps(res_obj, ensure_ascii=False)
                self.assertIsNone(
                    emoji_pattern.search(serialized),
                    f"Emoji detected in ranking output: {serialized}",
                )
                self.assertIsNone(
                    casual_endings.search(serialized),
                    f"Casual ending detected in ranking output: {serialized}",
                )
        finally:
            db.close()

        # 7. 未ロードDBでのFail-Closed検証
        empty_db = TokyoSpatialDB(":memory:")
        try:
            res_not_loaded = empty_db.lookup_station_or_address_ranking("恵比寿")
            self.assertEqual(res_not_loaded["status"], "not_loaded")
            self.assertEqual(res_not_loaded["point_count"], 0)
            self.assertEqual(res_not_loaded["ranked_locations"], [])
            self.assertIsNone(res_not_loaded["area_summary"])
        finally:
            empty_db.close()


if __name__ == "__main__":
    unittest.main()


