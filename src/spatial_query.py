"""
src/spatial_query.py
SORAI 診断レポート用: 公的オープンデータ 7系統一撃即答クエリエンジン。
外部API通信 0回、所要時間 2〜5ms。
"""

import math
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from shapely import wkb
from shapely.geometry import Point

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tokyo_spatial.sqlite"

TOKYO_CITY_CODE_MAP: Dict[str, str] = {
    "13101": "千代田区", "13102": "中央区", "13103": "港区", "13104": "新宿区",
    "13105": "文京区", "13106": "台東区", "13107": "墨田区", "13108": "江東区",
    "13109": "品川区", "13110": "目黒区", "13111": "大田区", "13112": "世田谷区",
    "13113": "渋谷区", "13114": "中野区", "13115": "杉並区", "13116": "豊島区",
    "13117": "北区", "13118": "荒川区", "13119": "板橋区", "13120": "練馬区",
    "13121": "足立区", "13122": "葛飾区", "13123": "江戸川区",
    "13201": "八王子市", "13202": "立川市", "13203": "武蔵野市", "13204": "三鷹市",
    "13205": "青梅市", "13206": "府中市", "13207": "昭島市", "13208": "調布市",
    "13209": "町田市", "13210": "小金井市", "13211": "小平市", "13212": "日野市",
    "13213": "東村山市", "13214": "国分寺市", "13215": "国立市", "13218": "福生市",
    "13219": "狛江市", "13220": "東大和市", "13221": "清瀬市", "13222": "東久留米市",
    "13223": "武蔵村山市", "13224": "多摩市", "13225": "稲城市", "13227": "羽村市",
    "13228": "あきる野市", "13229": "西東京市",
    "13303": "瑞穂町", "13305": "日の出町", "13307": "檜原村", "13308": "奥多摩町"
}

def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
    return 2.0 * R * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

def latlon_to_mesh250(lat: float, lon: float) -> str:
    """
    指定座標から JIS 250m 地域メッシュコード（10桁）を計算する。
    所要時間: < 1μs（空間インデックス不要の O(1) 計算）
    """
    I = int(math.floor(lat * 480))
    J = int(math.floor((lon - 100) * 320))
    p, q = I // 320, J // 320
    a, b = (I % 320) // 40, (J % 320) // 40
    c, d = (I % 40) // 4, (J % 40) // 4
    r, s = I % 4, J % 4
    m500 = 1 + (s // 2) + 2 * (r // 2)
    m250 = 1 + (s % 2) + 2 * (r % 2)
    return f"{p:02d}{q:02d}{a}{b}{c}{d}{m500}{m250}"

class TokyoSpatialDB:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"空間データベースが見つかりません: {self.db_path}")
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row

    def lookup_carte_7layers(self, lat: float, lon: float) -> Dict[str, Any]:
        """
        SORAI 診断レポート（物件カルテ）に必要な公的 7系統のデータを一撃で即答する。
        所要時間: 2〜5ms（外部API通信 0回）
        """
        pt = Point(lon, lat)

        return {
            "query_coordinate": {"lat": lat, "lon": lon},
            # 1. 都市計画・用途地域
            "zoning": self._lookup_zoning(pt, lat, lon),
            # 2. 地価公示・相場
            "land_price": self._lookup_land_price(lat, lon, radius_km=1.2),
            # 3. 小中学校区
            "school_district": self._lookup_schools(pt, lat, lon),
            # 4. 洪水浸水想定
            "flood_risk": self._lookup_flood(pt, lat, lon),
            # 5. 土砂災害警戒区域
            "landslide_risk": self._lookup_landslide(pt, lat, lon),
            # 6. 近傍医療機関 (1.2km)
            "medical": self._lookup_medical(lat, lon, radius_km=1.2),
            # 7. 近傍保育施設 (1.0km)
            "nursery": self._lookup_nursery(lat, lon, radius_km=1.0),
        }

    def lookup_comprehensive_13layers(self, lat: float, lon: float) -> Dict[str, Any]:
        """
        SORAI 総合立地・防災カルテ（全13系統・公的オープンデータ）を一撃で即答する。
        基本7系統 + 追加6系統（防火、液状化、大規模盛土造成地、高潮津波、将来人口推計、地区計画）
        所要時間: 3〜5ms（外部API通信 0回）
        """
        pt = Point(lon, lat)
        mesh250 = latlon_to_mesh250(lat, lon)

        # 既存7系統を取得
        base_carte = self.lookup_carte_7layers(lat, lon)

        # 追加6系統を統合
        base_carte["mesh250_code"] = mesh250
        base_carte["fire_prevention"] = self._lookup_fire_prevention(pt, lat, lon)
        base_carte["liquefaction"] = self._lookup_liquefaction(pt, lat, lon, mesh250)
        base_carte["embankment"] = self._lookup_embankment(pt, lat, lon)
        base_carte["coastal_hazard"] = self._lookup_coastal_hazard(pt, lat, lon)
        base_carte["population_projection"] = self._lookup_population_projection(mesh250)
        base_carte["district_plan"] = self._lookup_district_plan(pt, lat, lon)

        return base_carte

    lookup_all = lookup_carte_7layers

    def _lookup_fire_prevention(self, pt: Point, lat: float, lon: float) -> Dict[str, Any]:
        """防火・準防火地域 (A55 / bouka) 判定"""
        cur = self.conn.cursor()
        tables = [t[0] for t in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "planning_zone" not in tables or "planning_zone_idx" not in tables:
            return {"status": "not_loaded", "reason": "データセット準備中"}

        try:
            cur.execute("""
            SELECT p.id, p.kind, p.name, p.source_code, p.geom_wkb
            FROM planning_zone_idx idx
            JOIN planning_zone p ON idx.id = p.id
            WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
              AND p.kind IN ('fire', 'quasi_fire')
            LIMIT 20
            """, (lon, lon, lat, lat))
            candidates = cur.fetchall()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "reason": "クエリ実行エラー"}

        if not candidates:
            return {"status": "no_match", "designation": "指定なし（法22条区域等）", "matches": []}

        matched = []
        for row in candidates:
            geom = wkb.loads(row["geom_wkb"])
            if geom.covers(pt):
                matched.append({
                    "kind": row["kind"],
                    "name": row["name"] or ("防火地域" if row["kind"] == "fire" else "準防火地域"),
                    "code": row["source_code"]
                })

        if len(matched) == 1:
            return {"status": "ok", "designation": matched[0]["name"], "matches": matched}
        elif len(matched) > 1:
            return {"status": "ambiguous", "reason": "conflicting_or_boundary", "matches": matched}
        return {"status": "no_match", "designation": "指定なし（法22条区域等）", "matches": []}

    def _lookup_liquefaction(self, pt: Point, lat: float, lon: float, mesh250: str) -> Dict[str, Any]:
        """液状化発生傾向 (XKT025 / 地形区分250mメッシュ) 判定"""
        cur = self.conn.cursor()
        tables = [t[0] for t in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "liquefaction_mesh" not in tables:
            return {"status": "not_loaded", "reason": "データセット準備中"}

        try:
            row = cur.execute("""
            SELECT landform_name, tendency_label, relative_score, assessment, note
            FROM liquefaction_mesh
            WHERE mesh_code = ?
            """, (mesh250,)).fetchone()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "reason": "クエリ実行エラー"}

        if not row:
            return {"status": "no_match", "reason": "対象メッシュの地形区分データなし"}

        return {
            "status": "ok",
            "landform_name": row["landform_name"],
            "tendency_label": row["tendency_label"],
            "relative_score": row["relative_score"],
            "note": row["note"]
        }

    def _lookup_embankment(self, pt: Point, lat: float, lon: float) -> Dict[str, Any]:
        """大規模盛土造成地 (A54 谷埋め型・腹付け型) 判定"""
        cur = self.conn.cursor()
        tables = [t[0] for t in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "embankment" not in tables or "embankment_idx" not in tables:
            return {"status": "not_loaded", "reason": "データセット準備中"}

        try:
            cur.execute("""
            SELECT e.id, e.fill_code, e.fill_number, e.geom_wkb
            FROM embankment_idx idx
            JOIN embankment e ON idx.id = e.id
            WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
            LIMIT 20
            """, (lon, lon, lat, lat))
            candidates = cur.fetchall()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "reason": "クエリ実行エラー"}

        matched = []
        for row in candidates:
            geom = wkb.loads(row["geom_wkb"])
            if geom.covers(pt):
                fill_type = "谷埋め型盛土" if row["fill_code"] == "1" else ("腹付け型盛土" if row["fill_code"] == "2" else "造成地")
                matched.append({
                    "fill_code": row["fill_code"],
                    "fill_type": fill_type,
                    "fill_number": row["fill_number"]
                })

        if matched:
            return {"status": "ok", "is_designated": True, "zones": matched}
        return {"status": "no_match", "is_designated": False, "note": "大規模盛土造成地マップの該当なし"}

    def _lookup_coastal_hazard(self, pt: Point, lat: float, lon: float) -> Dict[str, Any]:
        """高潮・津波浸水想定 (A49 / A40) 判定"""
        cur = self.conn.cursor()
        tables = [t[0] for t in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "coastal_inundation" not in tables or "coastal_inundation_idx" not in tables:
            return {"status": "not_loaded", "reason": "データセット準備中"}

        try:
            cur.execute("""
            SELECT c.id, c.kind, c.scenario_key, c.depth_raw, c.depth_min_m, c.depth_max_m, c.geom_wkb
            FROM coastal_inundation_idx idx
            JOIN coastal_inundation c ON idx.id = c.id
            WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
            LIMIT 20
            """, (lon, lon, lat, lat))
            candidates = cur.fetchall()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "reason": "クエリ実行エラー"}

        matched = []
        for row in candidates:
            geom = wkb.loads(row["geom_wkb"])
            if geom.covers(pt):
                matched.append({
                    "kind": row["kind"],
                    "depth_raw": row["depth_raw"],
                    "depth_min_m": row["depth_min_m"],
                    "depth_max_m": row["depth_max_m"]
                })

        if matched:
            return {"status": "ok", "is_inundated": True, "hazards": matched}
        return {"status": "no_match", "is_inundated": False, "note": "高潮・津波浸水想定区域外"}

    def _lookup_population_projection(self, mesh250: str) -> Dict[str, Any]:
        """将来推計人口 250mメッシュ (R6推計 / 2020年比2050年予測) 判定"""
        cur = self.conn.cursor()
        tables = [t[0] for t in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "population_mesh" not in tables:
            return {"status": "not_loaded", "reason": "データセット準備中"}

        try:
            row = cur.execute("""
            SELECT population_2020, population_2050, disclosure_2020, disclosure_2050
            FROM population_mesh
            WHERE mesh_code = ?
            """, (mesh250,)).fetchone()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "reason": "クエリ実行エラー"}

        if not row:
            return {"status": "no_match", "reason": "推計メッシュデータなし"}

        p2020 = row["population_2020"]
        p2050 = row["population_2050"]
        ratio = round((p2050 / p2020) * 100, 1) if (p2020 and p2020 > 0 and p2050 is not None) else None

        return {
            "status": "ok",
            "mesh_code": mesh250,
            "population_2020": p2020,
            "population_2050": p2050,
            "change_rate_percent": round(ratio - 100.0, 1) if ratio is not None else None,
            "maintenance_ratio_percent": ratio
        }

    def _lookup_district_plan(self, pt: Point, lat: float, lon: float) -> Dict[str, Any]:
        """地区計画・高度利用地区 (A55 / chikukei, koudori) 判定"""
        cur = self.conn.cursor()
        tables = [t[0] for t in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "planning_zone" not in tables or "planning_zone_idx" not in tables:
            return {"status": "not_loaded", "reason": "データセット準備中"}

        try:
            cur.execute("""
            SELECT p.id, p.kind, p.name, p.source_code, p.geom_wkb
            FROM planning_zone_idx idx
            JOIN planning_zone p ON idx.id = p.id
            WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
              AND p.kind IN ('district_plan', 'high_use')
            LIMIT 20
            """, (lon, lon, lat, lat))
            candidates = cur.fetchall()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "reason": "クエリ実行エラー"}

        matched = []
        for row in candidates:
            geom = wkb.loads(row["geom_wkb"])
            if geom.covers(pt):
                matched.append({
                    "kind": row["kind"],
                    "name": row["name"],
                    "code": row["source_code"]
                })

        if matched:
            return {"status": "ok", "has_plan": True, "plans": matched}
        return {"status": "no_match", "has_plan": False, "note": "地区計画等の該当なし"}

    def _lookup_zoning(self, pt: Point, lat: float, lon: float) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("""
        SELECT z.id, z.name, z.code, z.building_coverage, z.floor_area_ratio, z.city_code, z.geom_wkb
        FROM zoning_idx idx
        JOIN zoning z ON idx.id = z.id
        WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
        """, (lon, lon, lat, lat))
        candidates = cur.fetchall()
        if not candidates:
            return {"status": "no_match", "candidates": []}

        matched = []
        for row in candidates:
            geom = wkb.loads(row["geom_wkb"])
            if geom.covers(pt):
                matched.append({
                    "name": row["name"],
                    "code": row["code"],
                    "building_coverage_percent": row["building_coverage"],
                    "floor_area_ratio_percent": row["floor_area_ratio"],
                })

        if len(matched) == 1:
            return {"status": "ok", "primary": matched[0], "candidates": matched}
        elif len(matched) > 1:
            return {"status": "ambiguous", "candidates": matched}
        return {"status": "no_match", "candidates": []}

    def _lookup_schools(self, pt: Point, lat: float, lon: float) -> Dict[str, Any]:
        cur = self.conn.cursor()
        # カラムに school_address があるか判定
        cols = [c[1] for c in cur.execute("PRAGMA table_info(school_district)").fetchall()]
        has_addr = "school_address" in cols

        query = f"""
        SELECT s.id, s.school_type, s.school_name, { 's.school_address,' if has_addr else "'' AS school_address," } s.city_name, s.geom_wkb
        FROM school_district_idx idx
        JOIN school_district s ON idx.id = s.id
        WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
        """
        cur.execute(query, (lon, lon, lat, lat))
        candidates = cur.fetchall()

        elem_matches, jhs_matches = [], []
        for row in candidates:
            geom = wkb.loads(row["geom_wkb"])
            if geom.covers(pt):
                city_code = str(row["city_name"] or "")
                city_label = TOKYO_CITY_CODE_MAP.get(city_code, city_code)
                item = {
                    "school_name": row["school_name"],
                    "school_address": row["school_address"] if has_addr and row["school_address"] else None,
                    "city_name": city_label,
                    "city_code": city_code
                }
                if row["school_type"] == "elem":
                    elem_matches.append(item)
                else:
                    jhs_matches.append(item)

        return {
            "status": "ok" if (elem_matches or jhs_matches) else "no_match",
            "elementary_school": elem_matches[0] if len(elem_matches) == 1 else ({"candidates": elem_matches} if len(elem_matches) > 1 else None),
            "junior_high_school": jhs_matches[0] if len(jhs_matches) == 1 else ({"candidates": jhs_matches} if len(jhs_matches) > 1 else None),
        }

    def _lookup_flood(self, pt: Point, lat: float, lon: float) -> Dict[str, Any]:
        cur = self.conn.cursor()
        try:
            cur.execute("""
            SELECT f.river_name, f.rank_code, f.depth_label, f.geom_wkb
            FROM flood_risk_idx idx
            JOIN flood_risk f ON idx.id = f.id
            WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
            """, (lon, lon, lat, lat))
            candidates = cur.fetchall()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "is_flooded": False}

        matched = []
        for row in candidates:
            geom = wkb.loads(row["geom_wkb"])
            if geom.covers(pt):
                matched.append({
                    "river_name": row["river_name"],
                    "rank_code": row["rank_code"],
                    "depth_label": row["depth_label"]
                })

        if not matched:
            return {"status": "no_match", "is_flooded": False, "note": "対象河川の想定最大規模浸水区域外"}

        # 最悪シナリオ（最大深さランク）を抽出
        matched.sort(key=lambda x: x["rank_code"], reverse=True)
        return {
            "status": "ok",
            "is_flooded": True,
            "max_scenario": matched[0],
            "all_scenarios": matched
        }

    def _lookup_landslide(self, pt: Point, lat: float, lon: float) -> Dict[str, Any]:
        cur = self.conn.cursor()
        try:
            cur.execute("""
            SELECT l.hazard_type, l.zone_type, l.address, l.geom_wkb
            FROM landslide_risk_idx idx
            JOIN landslide_risk l ON idx.id = l.id
            WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
            """, (lon, lon, lat, lat))
            candidates = cur.fetchall()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "is_warning_zone": False}

        matched = []
        for row in candidates:
            geom = wkb.loads(row["geom_wkb"])
            if geom.covers(pt):
                matched.append({
                    "hazard_type": row["hazard_type"],
                    "zone_type": row["zone_type"],
                    "address": row["address"]
                })

        return {
            "status": "ok" if matched else "no_match",
            "is_warning_zone": len(matched) > 0,
            "is_red_zone": any("レッド" in m["zone_type"] for m in matched),
            "zones": matched
        }

    def _lookup_land_price(self, lat: float, lon: float, radius_km: float = 1.2) -> Dict[str, Any]:
        delta_deg = radius_km / 111.0
        cur = self.conn.cursor()
        cur.execute("""
        SELECT p.id, p.price, p.change_rate, p.address, p.station_name, p.station_distance, p.lat, p.lon
        FROM land_price_idx idx
        JOIN land_price p ON idx.id = p.id
        WHERE idx.minX >= ? AND idx.maxX <= ? AND idx.minY >= ? AND idx.maxY <= ?
        """, (lon - delta_deg, lon + delta_deg, lat - delta_deg, lat + delta_deg))
        rows = cur.fetchall()
        if not rows:
            return {"status": "no_match", "nearest": []}

        results = []
        for r in rows:
            dist = haversine_distance_m(lat, lon, r["lat"], r["lon"])
            results.append({
                "price_yen_per_sqm": r["price"],
                "change_rate_percent": r["change_rate"],
                "address": r["address"],
                "nearest_station": r["station_name"],
                "distance_from_query_m": round(dist)
            })
        results.sort(key=lambda x: x["distance_from_query_m"])
        return {"status": "ok", "nearest": results[:3]}

    def _lookup_medical(self, lat: float, lon: float, radius_km: float = 1.2) -> Dict[str, Any]:
        delta_deg = radius_km / 111.0
        cur = self.conn.cursor()
        try:
            cur.execute("""
            SELECT m.name, m.facility_type, m.address, m.departments, m.is_emergency, m.lat, m.lon
            FROM medical_facility_idx idx
            JOIN medical_facility m ON idx.id = m.id
            WHERE idx.minX >= ? AND idx.maxX <= ? AND idx.minY >= ? AND idx.maxY <= ?
            """, (lon - delta_deg, lon + delta_deg, lat - delta_deg, lat + delta_deg))
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "nearest": []}

        results = []
        for r in rows:
            dist = haversine_distance_m(lat, lon, r["lat"], r["lon"])
            results.append({
                "name": r["name"],
                "facility_type": r["facility_type"],
                "address": r["address"],
                "departments": r["departments"],
                "is_emergency": bool(r["is_emergency"]),
                "distance_from_query_m": round(dist)
            })
        results.sort(key=lambda x: x["distance_from_query_m"])
        return {"status": "ok", "nearest": results[:5]}

    def _lookup_nursery(self, lat: float, lon: float, radius_km: float = 1.0) -> Dict[str, Any]:
        delta_deg = radius_km / 111.0
        cur = self.conn.cursor()
        try:
            cur.execute("""
            SELECT n.name, n.address, n.facility_type_code, n.lat, n.lon
            FROM nursery_facility_idx idx
            JOIN nursery_facility n ON idx.id = n.id
            WHERE idx.minX >= ? AND idx.maxX <= ? AND idx.minY >= ? AND idx.maxY <= ?
            """, (lon - delta_deg, lon + delta_deg, lat - delta_deg, lat + delta_deg))
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "nearest": []}

        results = []
        for r in rows:
            dist = haversine_distance_m(lat, lon, r["lat"], r["lon"])
            results.append({
                "name": r["name"],
                "address": r["address"],
                "distance_from_query_m": round(dist)
            })
        results.sort(key=lambda x: x["distance_from_query_m"])
        return {"status": "ok", "nearest": results[:5]}

    def close(self):
        self.conn.close()
