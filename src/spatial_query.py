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

    lookup_all = lookup_carte_7layers

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
