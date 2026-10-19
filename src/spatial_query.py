"""
src/spatial_query.py
SorAi 物件診断用: 公的オープンデータ 7系統一撃即答クエリエンジン。
外部API通信 0回、所要時間 2〜5ms。
"""

import json
import math
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
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

def _safe_finite_float(val: Any) -> Optional[float]:
    if isinstance(val, bool) or val is None:
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def _coastal_depth_sort_key(item: Dict[str, Any]) -> Tuple[int, float, float, str]:
    """
    高潮・津波浸水深の厳密ソートキー。
    - 既知の上限なし区分（例: 20m以上, depth_min_m=20.0, depth_max_m=None）を最上位 (tier=2) に配置。
    - 既知の上限あり区分（例: 10m以上20m未満, depth_min_m=10.0, depth_max_m=20.0）を次位 (tier=1) に配置。
    - 解析不能な深さ表記（depth_min_m=None, depth_max_m=None）を区別 (tier=0)。
    """
    d_min = _safe_finite_float(item.get("depth_min_m"))
    d_max = _safe_finite_float(item.get("depth_max_m"))
    raw = str(item.get("depth_raw") or "")
    if d_min is None and d_max is None:
        return (0, -1.0, -1.0, raw)
    if d_max is None and d_min is not None:
        return (2, d_min, d_min, raw)
    return (1, d_max if d_max is not None else (d_min or 0.0), d_min if d_min is not None else 0.0, raw)


def normalize_location_query(query: str) -> str:
    """
    駅名・住所クエリの表記揺れを吸収し、正規化文字列を返却する。
    - 全角英数字・全角スペース・ハイフン類を正規化
    - 先頭の「東京都」を除去
    - 末尾の「外」を除去
    - 末尾の「駅」を除去
    """
    if not isinstance(query, str):
        return ""
    q = unicodedata.normalize("NFKC", query).strip()
    if not q:
        return ""
    q = re.sub(r"\s+", "", q)
    q = re.sub(r"[−―‐－~〜]", "-", q)
    q = re.sub(r"^東京都", "", q)
    q = re.sub(r"外$", "", q)
    if q.endswith("駅"):
        q = q[:-1]
    return q.strip()


def extract_chome_label(address: str) -> str:
    """
    住所文字列から町丁目ラベル（例: '渋谷区恵比寿3丁目', '足立区千住旭町'）を抽出する。
    """
    if not isinstance(address, str):
        return ""
    clean = normalize_location_query(address)
    if not clean:
        return ""

    # 1. 丁目パターン（例: "渋谷区恵比寿3丁目"）
    m_chome = re.search(r"^(.+?(?:\d+|[一二三四五六七八九十]+)丁目)", clean)
    if m_chome:
        return m_chome.group(1)

    # 2. 丁目がない場合: 番地表記（例: "66番17", "16番3", "3番27", "1-2"）の前までを抽出
    m_ban = re.search(r"^(.+?)(?:\d+番地|\d+番|\d+-\d+|\d+$)", clean)
    if m_ban:
        return m_ban.group(1)

    return clean


class TokyoSpatialDB:
    MAX_SPATIAL_CANDIDATES = 2000

    def __init__(
        self,
        db_path: Union[Path, str, sqlite3.Connection] = DEFAULT_DB_PATH,
        max_spatial_candidates: int = MAX_SPATIAL_CANDIDATES
    ):
        self.max_spatial_candidates = max_spatial_candidates
        if isinstance(db_path, sqlite3.Connection):
            self.db_path = Path(":memory:")
            self.conn = db_path
        elif str(db_path) == ":memory:":
            self.db_path = Path(":memory:")
            self.conn = sqlite3.connect(":memory:")
        else:
            self.db_path = Path(db_path)
            if not self.db_path.exists():
                raise FileNotFoundError(f"空間データベースが見つかりません: {self.db_path}")
            self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_table_cache()

    def _init_table_cache(self):
        cur = self.conn.cursor()
        raw_tables = {
            t[0] for t in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        self._metadata: Dict[str, str] = {}
        for meta_table in ("dataset_meta", "metadata"):
            if meta_table in raw_tables:
                try:
                    for r in cur.execute(f"SELECT key, value FROM {meta_table}").fetchall():
                        self._metadata[str(r["key"])] = str(r["value"])
                except sqlite3.OperationalError:
                    pass

        self._loaded_tables: set = set()
        for t in [
            "zoning", "land_price", "school_district", "flood_risk",
            "landslide_risk", "medical_facility", "nursery_facility",
            "planning_zone", "embankment", "coastal_inundation",
            "liquefaction_mesh", "population_mesh"
        ]:
            if t in raw_tables:
                try:
                    has_row = cur.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
                    if has_row is not None:
                        self._loaded_tables.add(t)
                except sqlite3.OperationalError:
                    pass
        self._raw_tables = raw_tables

        # サブレイヤー・自治体別のロード状態キャッシュ（部分収録時の誤判定防止）
        self._planning_kinds: set = set()
        self._planning_city_kinds: Dict[str, set] = {}
        if "planning_zone" in self._loaded_tables:
            try:
                for r in cur.execute("SELECT DISTINCT kind, city_code FROM planning_zone").fetchall():
                    k = r["kind"]
                    c = str(r["city_code"] or "").strip()
                    if k:
                        self._planning_kinds.add(k)
                        if c:
                            self._planning_city_kinds.setdefault(c, set()).add(k)
            except sqlite3.OperationalError:
                pass

        self._coastal_kinds: set = set()
        if "coastal_inundation" in self._loaded_tables:
            try:
                for r in cur.execute("SELECT DISTINCT kind FROM coastal_inundation").fetchall():
                    if r["kind"]:
                        self._coastal_kinds.add(r["kind"])
            except sqlite3.OperationalError:
                pass

        def _parse_city_set(meta_key: str) -> set:
            val = self._metadata.get(meta_key, "").strip()
            if not val:
                return set()
            if val.startswith("["):
                try:
                    parsed = json.loads(val)
                    if isinstance(parsed, list):
                        return {str(c).strip() for c in parsed if str(c).strip()}
                except (ValueError, TypeError):
                    pass
            return {c.strip().strip('"').strip("'") for c in val.strip("[]").split(",") if c.strip()}

        self._a55_bouka_cities = _parse_city_set("a55_bouka_cities")
        self._a55_chikukei_cities = _parse_city_set("a55_chikukei_cities")
        self._a55_koudori_cities = _parse_city_set("a55_koudori_cities")
        self._a55_koudoti_cities = _parse_city_set("a55_koudoti_cities")
        self._a55_tkbt_cities = _parse_city_set("a55_tkbt_cities")

    def _is_table_ready(self, table_name: str, idx_name: Optional[str] = None) -> bool:
        if table_name not in self._loaded_tables:
            return False
        if idx_name and idx_name not in self._raw_tables:
            return False
        return True

    def lookup_carte_7layers(self, lat: float, lon: float) -> Dict[str, Any]:
        """
        SorAi 物件診断（物件カルテ）に必要な公的 7系統のデータを一撃で即答する。
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
        pop_proj = self._lookup_population_projection(mesh250)

        zoning_candidates = base_carte.get("zoning", {}).get("candidates") or []
        candidate_cities = {
            str(c.get("city_code")).strip()
            for c in zoning_candidates
            if isinstance(c, dict) and str(c.get("city_code") or "").strip()
        }
        unambiguous_candidate_city = (
            next(iter(candidate_cities)) if len(candidate_cities) == 1 else None
        )

        city_code = (
            (base_carte.get("zoning", {}).get("primary", {}) or {}).get("city_code")
            or unambiguous_candidate_city
            or pop_proj.get("city_code")
        )

        # 追加6系統を統合
        base_carte["mesh250_code"] = mesh250
        base_carte["fire_prevention"] = self._lookup_fire_prevention(pt, lat, lon, city_code=city_code)
        base_carte["liquefaction"] = self._lookup_liquefaction(pt, lat, lon, mesh250)
        base_carte["embankment"] = self._lookup_embankment(pt, lat, lon)
        base_carte["coastal_hazard"] = self._lookup_coastal_hazard(pt, lat, lon)
        base_carte["population_projection"] = pop_proj
        base_carte["district_plan"] = self._lookup_district_plan(pt, lat, lon, city_code=city_code)

        # 独自指標エンジン Sorai Index（100点満点総合スコア）を自動付与
        base_carte["sorai_index"] = compute_sorai_index(base_carte)

        return base_carte

    lookup_all = lookup_carte_7layers

    def evaluate_sorai_index(self, lat: float, lon: float) -> Dict[str, Any]:
        """
        指定座標 (lat, lon) の Sorai Index（独自指標・100点満点総合スコア）を単体評価して返却する。
        所要時間: 3〜5ms（外部API通信 0回）
        """
        carte = self.lookup_comprehensive_13layers(lat, lon)
        return carte["sorai_index"]

    def lookup_station_or_address_ranking(
        self, query: str, limit: int = 12
    ) -> Dict[str, Any]:
        """
        空間データ・Sorai Index を用いた「SorAi 番地・駅 1秒格付け（アドレス診断）」検索・格付けエンジン。
        - 外部API通信 0回（ローカル空間DBキャッシュのみ利用）
        - 最大25地点キャップ（Fail-Closed）
        """
        if not isinstance(query, str) or not query.strip():
            return {
                "status": "invalid_query",
                "query": "",
                "point_count": 0,
                "ranked_locations": [],
                "area_summary": None,
                "cvr_cta": None,
            }

        clean_q = normalize_location_query(query)
        if not clean_q:
            return {
                "status": "invalid_query",
                "query": "",
                "point_count": 0,
                "ranked_locations": [],
                "area_summary": None,
                "cvr_cta": None,
            }

        if not self._is_table_ready("land_price"):
            return {
                "status": "not_loaded",
                "query": clean_q,
                "point_count": 0,
                "ranked_locations": [],
                "area_summary": None,
                "cvr_cta": None,
            }

        cur = self.conn.cursor()
        matched_station: Optional[str] = None
        matched_rows: List[sqlite3.Row] = []

        # ① まず station_name = ?（正規化後の駅名）で完全一致検索（最大25件にキャップ）
        try:
            cur.execute("""
            SELECT id, price, change_rate, address, station_name, station_distance, lat, lon
            FROM land_price
            WHERE station_name = ?
            LIMIT 25
            """, (clean_q,))
            exact_rows = cur.fetchall()
            if exact_rows:
                matched_station = clean_q
                matched_rows = exact_rows
        except sqlite3.OperationalError:
            return {
                "status": "not_loaded",
                "query": clean_q,
                "point_count": 0,
                "ranked_locations": [],
                "area_summary": None,
                "cvr_cta": None,
            }

        # ② 0件の場合は station_name LIKE ? または正規化済み address にクエリ文字列が含まれる行を検索
        if not matched_rows:
            try:
                cur.execute("""
                SELECT id, price, change_rate, address, station_name, station_distance, lat, lon
                FROM land_price
                """)
                all_rows = cur.fetchall()
                partial_candidates = []
                for r in all_rows:
                    st_name = str(r["station_name"] or "")
                    norm_addr = normalize_location_query(str(r["address"] or ""))
                    if clean_q in st_name or clean_q in norm_addr:
                        partial_candidates.append(r)
                        if len(partial_candidates) >= 25:
                            break
                matched_rows = partial_candidates
            except sqlite3.OperationalError:
                return {
                    "status": "not_loaded",
                    "query": clean_q,
                    "point_count": 0,
                    "ranked_locations": [],
                    "area_summary": None,
                    "cvr_cta": None,
                }

        # ③ それでも0件の場合は no_match を返却
        if not matched_rows:
            return {
                "status": "no_match",
                "query": clean_q,
                "point_count": 0,
                "ranked_locations": [],
                "area_summary": None,
                "cvr_cta": None,
            }

        # ヒットした行（最大25件にキャップ）に対して evaluate_sorai_index を実行
        items: List[Dict[str, Any]] = []
        for r in matched_rows[:25]:
            lat_val = _safe_finite_float(r["lat"])
            lon_val = _safe_finite_float(r["lon"])
            if lat_val is None or lon_val is None:
                continue
            norm_addr = normalize_location_query(str(r["address"] or ""))
            chome = extract_chome_label(norm_addr)
            dist_m = _finite_int(r["station_distance"], min_val=1, max_val=100_000)
            walk_min = max(1, math.ceil(dist_m / 80)) if dist_m is not None else None

            price = _finite_int(r["price"], min_val=1, max_val=500_000_000)
            change_rate = _finite_float_bounded(r["change_rate"], min_val=-100.0, max_val=1000.0)
            sorai_idx = self.evaluate_sorai_index(lat_val, lon_val)

            svi = sorai_idx.get("signature_indices", {}).get("silent_value_index", {})
            category = str(svi.get("category") or "")
            total_score = int(sorai_idx.get("total_score", 0))
            is_hidden_gem = bool(total_score >= 78 and "隠れ静寂立地" in category)

            items.append({
                "address": norm_addr,
                "chome_label": chome,
                "station_name": r["station_name"],
                "station_distance_m": dist_m,
                "walk_minutes": walk_min,
                "price_yen_per_sqm": price,
                "change_rate_percent": change_rate,
                "lat": lat_val,
                "lon": lon_val,
                "sorai_index": sorai_idx,
                "is_hidden_gem": is_hidden_gem,
            })

        if not items:
            return {
                "status": "no_match",
                "query": clean_q,
                "point_count": 0,
                "ranked_locations": [],
                "area_summary": None,
                "cvr_cta": None,
            }

        # ソート順: (total_score, env_protection_rate, -(price or 999999999)) の降順
        def _ranking_sort_key(item: Dict[str, Any]) -> Tuple[float, float, float]:
            score = float(item["sorai_index"].get("total_score", 0))
            svi = item["sorai_index"].get("signature_indices", {}).get("silent_value_index", {})
            env_rate = float(svi.get("env_protection_rate", 0.0))
            p = item.get("price_yen_per_sqm")
            neg_price = -(p if p is not None else 999999999)
            return (score, env_rate, neg_price)

        items.sort(key=_ranking_sort_key, reverse=True)
        clamped_limit = max(1, min(20, _finite_int(limit, min_val=1, max_val=20) or 12))
        ranked_locations = items[:clamped_limit]

        # area_summary 構築（駅・エリア全体の全評価地点 items から最大・最小・格差を算出）
        area_label = f"{matched_station}駅" if matched_station else clean_q
        best = items[0]
        worst = items[-1]
        max_score = int(best["sorai_index"]["total_score"])
        min_score = int(worst["sorai_index"]["total_score"])
        score_spread = max_score - min_score
        avg_score = round(sum(loc["sorai_index"]["total_score"] for loc in items) / len(items), 1)
        best_chome = best["chome_label"]
        best_rank = best["sorai_index"]["rank"]
        lowest_chome = worst["chome_label"]
        lowest_rank = worst["sorai_index"]["rank"]
        hidden_gem_count = sum(1 for loc in items if loc["is_hidden_gem"])

        if score_spread >= 12 and len(items) >= 2:
            spread_insight = (
                f"同じ{area_label}周辺でも、「{best_chome}」（{max_score}点・{best_rank}ランク）と"
                f"「{lowest_chome}」（{min_score}点・{lowest_rank}ランク）では"
                f"用途地域や地形区分により最大{score_spread}点のスコア差が生じています。"
                f"物件選定時は駅徒歩分数だけでなく、番地単位での住環境・地盤照合が効果的です。"
            )
        else:
            spread_insight = (
                f"{area_label}周辺の標準地評価は最高{max_score}点（{best_chome}）・平均{avg_score}点と推移しています。"
                f"番地ごとの用途地域と微地形を確認することで、より静粛性の高い住戸を選択できます。"
            )

        area_summary = {
            "area_label": area_label,
            "evaluated_count": len(items),
            "max_score": max_score,
            "min_score": min_score,
            "score_spread": score_spread,
            "average_score": avg_score,
            "best_chome": best_chome,
            "best_rank": best_rank,
            "lowest_chome": lowest_chome,
            "lowest_rank": lowest_rank,
            "hidden_gem_count": hidden_gem_count,
            "spread_insight": spread_insight,
        }

        # cvr_cta 構築
        seen_chomes = set()
        top_chomes = []
        for loc in ranked_locations:
            c = loc["chome_label"]
            if c and c not in seen_chomes:
                seen_chomes.add(c)
                top_chomes.append(c)
                if len(top_chomes) >= 3:
                    break

        headline = f"「{best['chome_label']}」など上位ランク番地に絞って部屋を探す"
        description = "一般的なポータルサイトでは絞り込めない「用途地域・地盤・静寂単価効率」の上位番地に限定し、適正初期費用の空室・未公開物件をご案内します。"
        primary_button_label = f"{area_label}の上位番地（{best['sorai_index']['rank']}ランク）の空室をLINEで確認する"
        secondary_button_label = "この番地格付けカルテと内見チェックリストをLINEに保存する"
        request_query_params = f"?area={area_label}&chome={best['chome_label']}&sorai_score={max_score}&sorai_rank={best['sorai_index']['rank']}"

        cvr_cta = {
            "top_chomes": top_chomes,
            "headline": headline,
            "description": description,
            "primary_button_label": primary_button_label,
            "secondary_button_label": secondary_button_label,
            "request_query_params": request_query_params,
        }

        return {
            "status": "ok",
            "query": clean_q,
            "point_count": len(ranked_locations),
            "ranked_locations": ranked_locations,
            "area_summary": area_summary,
            "cvr_cta": cvr_cta,
        }

    def _lookup_fire_prevention(
        self, pt: Point, lat: float, lon: float, city_code: Optional[str] = None
    ) -> Dict[str, Any]:
        """防火・準防火地域 (A55 / bouka) 判定"""
        if not self._is_table_ready("planning_zone", "planning_zone_idx"):
            return {"status": "not_loaded", "reason": "データセット準備中"}
        if not (self._planning_kinds & {"fire", "quasi_fire"}):
            return {"status": "not_loaded", "reason": "防火・準防火地域サブレイヤー未収録"}

        limit_plus_one = self.max_spatial_candidates + 1
        cur = self.conn.cursor()
        try:
            cur.execute("""
            SELECT p.id, p.kind, p.name, p.source_code, p.city_code, p.geom_wkb
            FROM planning_zone_idx idx
            JOIN planning_zone p ON idx.id = p.id
            WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
              AND p.kind IN ('fire', 'quasi_fire')
            LIMIT ?
            """, (lon, lon, lat, lat, limit_plus_one))
            candidates = cur.fetchall()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "reason": "クエリ実行エラー"}

        if len(candidates) > self.max_spatial_candidates:
            return {"status": "not_loaded", "reason": "空間候補上限超過（Fail-Closed）"}

        matched = []
        seen = set()
        detected_city = str(city_code).strip() if city_code else ""
        for row in candidates:
            geom = wkb.loads(row["geom_wkb"])
            if geom.covers(pt):
                kind = row["kind"]
                name = row["name"] or ("防火地域" if kind == "fire" else "準防火地域")
                code = row["source_code"]
                if not detected_city and row["city_code"]:
                    detected_city = str(row["city_code"]).strip()
                key = (kind, name, code)
                if key not in seen:
                    seen.add(key)
                    matched.append({
                        "kind": kind,
                        "name": name,
                        "code": code
                    })

        if len(matched) == 1:
            return {"status": "ok", "designation": matched[0]["name"], "matches": matched}
        elif len(matched) > 1:
            return {
                "status": "ambiguous",
                "reason": "conflicting_or_boundary",
                "designation": " / ".join(m["name"] for m in matched) + "（境界付近・要確認）",
                "matches": matched
            }

        # 未ヒット時: 取り込み完了メタデータ (a55_bouka_cities) で確認できた場合のみ no_match とし、それ以外は not_loaded (Fail-Closed)
        if not detected_city or detected_city not in self._a55_bouka_cities:
            return {
                "status": "not_loaded",
                "reason": f"対象自治体({detected_city or '不明'})の防火地域データ収録未確認（Fail-Closed）"
            }

        return {"status": "no_match", "designation": "指定なし（法22条区域等）", "matches": []}

    def _lookup_liquefaction(self, pt: Point, lat: float, lon: float, mesh250: str) -> Dict[str, Any]:
        """液状化発生傾向 (XKT025 / 地形区分250mメッシュ) 判定"""
        if not self._is_table_ready("liquefaction_mesh"):
            return {"status": "not_loaded", "reason": "データセット準備中"}

        cur = self.conn.cursor()
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
            "mesh_code": mesh250,
            "landform_name": row["landform_name"],
            "tendency_label": row["tendency_label"],
            "relative_score": row["relative_score"],
            "assessment": row["assessment"],
            "note": row["note"]
        }

    def _lookup_embankment(self, pt: Point, lat: float, lon: float) -> Dict[str, Any]:
        """大規模盛土造成地 (A54 谷埋め型・腹付け型) 判定"""
        if not self._is_table_ready("embankment", "embankment_idx"):
            return {"status": "not_loaded", "reason": "データセット準備中"}

        limit_plus_one = self.max_spatial_candidates + 1
        cur = self.conn.cursor()
        try:
            cur.execute("""
            SELECT e.id, e.fill_code, e.fill_number, e.geom_wkb
            FROM embankment_idx idx
            JOIN embankment e ON idx.id = e.id
            WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
            LIMIT ?
            """, (lon, lon, lat, lat, limit_plus_one))
            candidates = cur.fetchall()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "reason": "クエリ実行エラー"}

        if len(candidates) > self.max_spatial_candidates:
            return {"status": "not_loaded", "reason": "空間候補上限超過（Fail-Closed）"}

        matched = []
        seen = set()
        for row in candidates:
            geom = wkb.loads(row["geom_wkb"])
            if geom.covers(pt):
                fcode = row["fill_code"]
                fnum = row["fill_number"]
                key = (fcode, fnum)
                if key not in seen:
                    seen.add(key)
                    fill_type = "谷埋め型盛土" if fcode == "1" else ("腹付け型盛土" if fcode == "2" else "造成地")
                    matched.append({
                        "fill_code": fcode,
                        "fill_type": fill_type,
                        "fill_number": fnum
                    })

        if matched:
            return {"status": "ok", "is_designated": True, "zones": matched}
        return {"status": "no_match", "is_designated": False, "note": "大規模盛土造成地マップの該当なし"}

    def _lookup_coastal_hazard(self, pt: Point, lat: float, lon: float) -> Dict[str, Any]:
        """高潮・津波浸水想定 (A49 / A40) 判定"""
        if not self._is_table_ready("coastal_inundation", "coastal_inundation_idx"):
            return {"status": "not_loaded", "reason": "データセット準備中"}

        storm_loaded = "storm_surge" in self._coastal_kinds
        tsunami_loaded = "tsunami" in self._coastal_kinds
        if not storm_loaded and not tsunami_loaded:
            return {"status": "not_loaded", "reason": "データセット準備中"}

        limit_plus_one = self.max_spatial_candidates + 1
        cur = self.conn.cursor()
        try:
            cur.execute("""
            SELECT c.id, c.kind, c.scenario_key, c.depth_raw, c.depth_min_m, c.depth_max_m, c.geom_wkb
            FROM coastal_inundation_idx idx
            JOIN coastal_inundation c ON idx.id = c.id
            WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
            LIMIT ?
            """, (lon, lon, lat, lat, limit_plus_one))
            candidates = cur.fetchall()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "reason": "クエリ実行エラー"}

        if len(candidates) > self.max_spatial_candidates:
            return {"status": "not_loaded", "reason": "空間候補上限超過（Fail-Closed）"}

        matched = []
        seen = set()
        for row in candidates:
            geom = wkb.loads(row["geom_wkb"])
            if geom.covers(pt):
                key = (row["kind"], row["depth_raw"], row["depth_min_m"], row["depth_max_m"])
                if key not in seen:
                    seen.add(key)
                    matched.append({
                        "kind": row["kind"],
                        "depth_raw": row["depth_raw"],
                        "depth_min_m": row["depth_min_m"],
                        "depth_max_m": row["depth_max_m"]
                    })

        matched.sort(key=_coastal_depth_sort_key, reverse=True)
        storm_matches = [m for m in matched if m["kind"] == "storm_surge"]
        tsunami_matches = [m for m in matched if m["kind"] == "tsunami"]

        storm_info = (
            {
                "status": "ok" if storm_matches else "no_match",
                "is_designated": len(storm_matches) > 0,
                "max_depth_raw": storm_matches[0]["depth_raw"] if storm_matches else None,
            }
            if storm_loaded
            else {
                "status": "not_loaded",
                "is_designated": None,
                "max_depth_raw": None,
            }
        )
        tsunami_info = (
            {
                "status": "ok" if tsunami_matches else "no_match",
                "is_designated": len(tsunami_matches) > 0,
                "max_depth_raw": tsunami_matches[0]["depth_raw"] if tsunami_matches else None,
            }
            if tsunami_loaded
            else {
                "status": "not_loaded",
                "is_designated": None,
                "max_depth_raw": None,
            }
        )

        if matched:
            return {
                "status": "ok" if (storm_loaded and tsunami_loaded) else "partial_not_loaded",
                "is_inundated": True,
                "storm_surge": storm_info,
                "tsunami": tsunami_info,
                "hazards": matched
            }

        if not (storm_loaded and tsunami_loaded):
            return {
                "status": "not_loaded",
                "is_inundated": None,
                "storm_surge": storm_info,
                "tsunami": tsunami_info,
                "reason": "高潮または津波サブレイヤーが未収録のため区域外断定不可"
            }

        return {
            "status": "no_match",
            "is_inundated": False,
            "storm_surge": storm_info,
            "tsunami": tsunami_info,
            "note": "高潮・津波浸水想定区域外"
        }

    def _lookup_population_projection(self, mesh250: str) -> Dict[str, Any]:
        """将来推計人口 250mメッシュ (R6推計 / 2020年比2050年予測) 判定"""
        if not self._is_table_ready("population_mesh"):
            return {"status": "not_loaded", "reason": "データセット準備中"}

        cur = self.conn.cursor()
        try:
            row = cur.execute("""
            SELECT city_code, population_2020, population_2050, disclosure_2020, disclosure_2050
            FROM population_mesh
            WHERE mesh_code = ?
            """, (mesh250,)).fetchone()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "reason": "クエリ実行エラー"}

        if not row:
            return {"status": "no_match", "mesh_code": mesh250, "reason": "推計メッシュデータなし"}

        p2020 = row["population_2020"]
        p2050 = row["population_2050"]
        d2020 = row["disclosure_2020"]
        d2050 = row["disclosure_2050"]
        c_code = str(row["city_code"] or "")

        # 秘匿や合算単位が異なる場合は比率を計算しない (Fail-Closed)
        can_compare = (
            p2020 is not None and p2020 > 0 and p2050 is not None
            and d2020 == "normal" and d2050 == "normal"
        )
        ratio = round((p2050 / p2020) * 100, 1) if can_compare else None

        return {
            "status": "ok",
            "mesh_code": mesh250,
            "city_code": c_code,
            "city_name": TOKYO_CITY_CODE_MAP.get(c_code, c_code),
            "population_2020": round(p2020, 1) if p2020 is not None else None,
            "population_2050": round(p2050, 1) if p2050 is not None else None,
            "change_rate_percent": round(ratio - 100.0, 1) if ratio is not None else None,
            "maintenance_ratio_percent": ratio,
            "disclosure_2050": d2050
        }

    def _lookup_district_plan(
        self, pt: Point, lat: float, lon: float, city_code: Optional[str] = None
    ) -> Dict[str, Any]:
        """地区計画・高度利用地区・高度地区 (A55 / chikukei, koudori, koudoti, tkbt) 判定"""
        if not self._is_table_ready("planning_zone", "planning_zone_idx"):
            return {"status": "not_loaded", "reason": "データセット準備中"}

        target_kinds = {"district_plan", "high_use", "height_district", "special_use"}
        if not (self._planning_kinds & target_kinds):
            return {"status": "not_loaded", "reason": "地区計画・高度地区サブレイヤー未収録"}

        limit_plus_one = self.max_spatial_candidates + 1
        cur = self.conn.cursor()
        try:
            cur.execute("""
            SELECT p.id, p.kind, p.name, p.source_code, p.city_code, p.geom_wkb
            FROM planning_zone_idx idx
            JOIN planning_zone p ON idx.id = p.id
            WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
              AND p.kind IN ('district_plan', 'high_use', 'height_district', 'special_use')
            LIMIT ?
            """, (lon, lon, lat, lat, limit_plus_one))
            candidates = cur.fetchall()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "reason": "クエリ実行エラー"}

        if len(candidates) > self.max_spatial_candidates:
            return {"status": "not_loaded", "reason": "空間候補上限超過（Fail-Closed）"}

        plans = []
        urban_controls = []
        seen = set()
        detected_city = str(city_code).strip() if city_code else ""

        for row in candidates:
            geom = wkb.loads(row["geom_wkb"])
            if geom.covers(pt):
                kind = row["kind"]
                name = row["name"]
                code = row["source_code"]
                if not detected_city and row["city_code"]:
                    detected_city = str(row["city_code"]).strip()
                key = (kind, name, code)
                if key not in seen:
                    seen.add(key)
                    item = {"kind": kind, "name": name, "code": code}
                    if kind in ("district_plan", "high_use"):
                        plans.append(item)
                    else:
                        urban_controls.append(item)

        both_plan_sublayers_covered = bool(
            detected_city
            and (detected_city in self._a55_chikukei_cities)
            and (detected_city in self._a55_koudori_cities)
        )

        if plans or urban_controls:
            return {
                "status": "ok",
                "has_plan": True if plans else (False if both_plan_sublayers_covered else None),
                "plans": plans,
                "urban_controls": urban_controls
            }

        if not both_plan_sublayers_covered:
            return {
                "status": "not_loaded",
                "has_plan": None,
                "plans": [],
                "urban_controls": [],
                "reason": f"対象自治体({detected_city or '不明'})の地区計画・高度利用地区サブレイヤー収録未確認"
            }

        return {
            "status": "no_match",
            "has_plan": False,
            "plans": [],
            "urban_controls": [],
            "note": "地区計画等の該当なし"
        }

    def _lookup_zoning(self, pt: Point, lat: float, lon: float) -> Dict[str, Any]:
        if not self._is_table_ready("zoning", "zoning_idx"):
            return {"status": "not_loaded", "candidates": []}

        limit_plus_one = self.max_spatial_candidates + 1
        cur = self.conn.cursor()
        try:
            cur.execute("""
            SELECT z.id, z.name, z.code, z.building_coverage, z.floor_area_ratio, z.city_code, z.geom_wkb
            FROM zoning_idx idx
            JOIN zoning z ON idx.id = z.id
            WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
            LIMIT ?
            """, (lon, lon, lat, lat, limit_plus_one))
            candidates = cur.fetchall()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "candidates": []}

        if len(candidates) > self.max_spatial_candidates:
            return {"status": "not_loaded", "candidates": [], "reason": "空間候補上限超過（Fail-Closed）"}
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
                    "city_code": str(row["city_code"] or "")
                })

        if len(matched) == 1:
            return {"status": "ok", "primary": matched[0], "candidates": matched}
        elif len(matched) > 1:
            return {"status": "ambiguous", "candidates": matched}
        return {"status": "no_match", "candidates": []}

    def _lookup_schools(self, pt: Point, lat: float, lon: float) -> Dict[str, Any]:
        if not self._is_table_ready("school_district", "school_district_idx"):
            return {"status": "not_loaded", "elementary_school": None, "junior_high_school": None}

        limit_plus_one = self.max_spatial_candidates + 1
        cur = self.conn.cursor()
        cols = [c[1] for c in cur.execute("PRAGMA table_info(school_district)").fetchall()]
        has_addr = "school_address" in cols

        query = f"""
        SELECT s.id, s.school_type, s.school_name, { 's.school_address,' if has_addr else "'' AS school_address," } s.city_name, s.geom_wkb
        FROM school_district_idx idx
        JOIN school_district s ON idx.id = s.id
        WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
        LIMIT ?
        """
        cur.execute(query, (lon, lon, lat, lat, limit_plus_one))
        candidates = cur.fetchall()
        if len(candidates) > self.max_spatial_candidates:
            return {"status": "not_loaded", "elementary_school": None, "junior_high_school": None}

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
        if not self._is_table_ready("flood_risk", "flood_risk_idx"):
            return {"status": "not_loaded", "is_flooded": None}

        limit_plus_one = self.max_spatial_candidates + 1
        cur = self.conn.cursor()
        try:
            cur.execute("""
            SELECT f.river_name, f.rank_code, f.depth_label, f.geom_wkb
            FROM flood_risk_idx idx
            JOIN flood_risk f ON idx.id = f.id
            WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
            LIMIT ?
            """, (lon, lon, lat, lat, limit_plus_one))
            candidates = cur.fetchall()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "is_flooded": None}

        if len(candidates) > self.max_spatial_candidates:
            return {"status": "not_loaded", "is_flooded": None, "reason": "空間候補上限超過（Fail-Closed）"}

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
        if not self._is_table_ready("landslide_risk", "landslide_risk_idx"):
            return {"status": "not_loaded", "is_warning_zone": None}

        limit_plus_one = self.max_spatial_candidates + 1
        cur = self.conn.cursor()
        try:
            cur.execute("""
            SELECT l.hazard_type, l.zone_type, l.address, l.geom_wkb
            FROM landslide_risk_idx idx
            JOIN landslide_risk l ON idx.id = l.id
            WHERE idx.minX <= ? AND idx.maxX >= ? AND idx.minY <= ? AND idx.maxY >= ?
            LIMIT ?
            """, (lon, lon, lat, lat, limit_plus_one))
            candidates = cur.fetchall()
        except sqlite3.OperationalError:
            return {"status": "not_loaded", "is_warning_zone": None}

        if len(candidates) > self.max_spatial_candidates:
            return {"status": "not_loaded", "is_warning_zone": None, "reason": "空間候補上限超過（Fail-Closed）"}

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
        if not self._is_table_ready("land_price", "land_price_idx"):
            return {"status": "not_loaded", "nearest": []}

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
        if not self._is_table_ready("medical_facility", "medical_facility_idx"):
            return {"status": "not_loaded", "nearest": []}

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
        if not self._is_table_ready("nursery_facility", "nursery_facility_idx"):
            return {"status": "not_loaded", "nearest": []}

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


ZONING_TRANQUILITY_BASE_SCORES: Dict[str, int] = {
    "第一種低層住居専用地域": 22,
    "第１種低層住居専用地域": 22,
    "第二種低層住居専用地域": 22,
    "第２種低層住居専用地域": 22,
    "第一種中高層住居専用地域": 20,
    "第１種中高層住居専用地域": 20,
    "田園住居地域": 20,
    "第二種中高層住居専用地域": 18,
    "第２種中高層住居専用地域": 18,
    "第一種住居地域": 16,
    "第１種住居地域": 16,
    "第二種住居地域": 14,
    "第２種住居地域": 14,
    "準住居地域": 12,
    "近隣商業地域": 10,
    "商業地域": 8,
    "準工業地域": 8,
    "工業地域": 4,
    "工業専用地域": 0,
}


def _finite_int(
    val: Any,
    min_val: Optional[int] = None,
    max_val: Optional[int] = None,
) -> Optional[int]:
    f = _safe_finite_float(val)
    if f is None:
        return None
    i = int(round(f))
    if min_val is not None and i < min_val:
        return None
    if max_val is not None and i > max_val:
        return None
    return i


def _finite_float_bounded(
    val: Any,
    min_val: Optional[float] = None,
    max_val: Optional[float] = None,
) -> Optional[float]:
    f = _safe_finite_float(val)
    if f is None:
        return None
    if min_val is not None and f < min_val:
        return None
    if max_val is not None and f > max_val:
        return None
    return f


def compute_sorai_index(carte: Dict[str, Any]) -> Dict[str, Any]:
    """
    公的13系統オープンデータから独自指標『Sorai Index』（100点満点＋4大ピラー＋2大独自指標）を算出する。
    外部API通信ゼロ・所要時間 < 0.2ms。絵文字は一切含めず、公的表記および Quiet Luxury 敬体に準拠する。
    """
    if not isinstance(carte, dict):
        carte = {}

    # -------------------------------------------------------------------------
    # 1. Tranquility (静粛性・住環境秩序: 30点満点)
    # -------------------------------------------------------------------------
    zoning_data = carte.get("zoning") if isinstance(carte.get("zoning"), dict) else {}
    z_status = zoning_data.get("status")
    z_primary = zoning_data.get("primary") if isinstance(zoning_data.get("primary"), dict) else {}
    z_candidates = zoning_data.get("candidates") if isinstance(zoning_data.get("candidates"), list) else []

    zoning_name: Optional[str] = None
    far_pct: Optional[int] = None

    if z_status == "ok" and z_primary:
        raw_zname = str(z_primary.get("name") or "").strip()
        zoning_name = raw_zname if raw_zname else None
        far_pct = _finite_int(z_primary.get("floor_area_ratio_percent"), min_val=10, max_val=2000)
        z_base = ZONING_TRANQUILITY_BASE_SCORES.get(zoning_name or "", 14) if zoning_name else 10
    elif z_candidates:
        # 境界線上の複数候補は (用途地域名, 容積率) で重複排除した上で、安全側（最小スコア・最大容積率）を採用
        unique_names: List[str] = []
        unique_scores: List[int] = []
        unique_fars: List[int] = []
        seen_cands: set = set()
        for c in z_candidates:
            if not isinstance(c, dict):
                continue
            cname = str(c.get("name") or "").strip()
            cfar = _finite_int(c.get("floor_area_ratio_percent"), min_val=10, max_val=2000)
            dedup_key = (cname, cfar)
            if dedup_key in seen_cands:
                continue
            seen_cands.add(dedup_key)
            if cname:
                if cname not in unique_names:
                    unique_names.append(cname)
                unique_scores.append(ZONING_TRANQUILITY_BASE_SCORES.get(cname, 14))
            if cfar is not None:
                unique_fars.append(cfar)
        zoning_name = " / ".join(unique_names) if unique_names else None
        z_base = min(unique_scores) if unique_scores else 10
        far_pct = max(unique_fars) if unique_fars else None
    else:
        z_base = 8

    if far_pct is not None:
        if far_pct <= 200:
            far_score = 4
        elif far_pct <= 300:
            far_score = 3
        elif far_pct <= 400:
            far_score = 2
        else:
            far_score = 1
    else:
        far_score = 1 if zoning_name is None else 2

    dp_data = carte.get("district_plan") if isinstance(carte.get("district_plan"), dict) else {}
    plans = dp_data.get("plans") if isinstance(dp_data.get("plans"), list) else []
    urban_controls = dp_data.get("urban_controls") if isinstance(dp_data.get("urban_controls"), list) else []
    has_urban_control = bool(plans or urban_controls)
    urban_control_score = 4 if has_urban_control else 1

    tranquility_score = min(30, max(0, z_base + far_score + urban_control_score))
    is_commercial_or_industrial = any(
        kw in (zoning_name or "") for kw in ("商業", "工業")
    )

    if zoning_name:
        if not is_commercial_or_industrial:
            tranquility_assessment = (
                f"用途地域は「{zoning_name}」（容積率{far_pct if far_pct is not None else '-'}%）に指定されており、"
                + ("高度地区・地区計画等の都市計画制限と相まって落ち着いた住環境秩序が保たれています。" if has_urban_control else "住居系地域として落ち着いた環境が維持されています。")
            )
        else:
            tranquility_assessment = (
                f"用途地域は「{zoning_name}」（容積率{far_pct if far_pct is not None else '-'}%）に指定されており、"
                "都市利便性が高い一方、周辺交通量や商業活動に伴う生活音の確認が推奨されます。"
            )
    else:
        tranquility_assessment = "用途地域データが未確定のため、現地および都市計画図にて周辺建物の用途・交通量をご確認ください。"

    # -------------------------------------------------------------------------
    # 2. Resilience (地盤・災害レジリエンス: 35点満点)
    # -------------------------------------------------------------------------
    liq_data = carte.get("liquefaction") if isinstance(carte.get("liquefaction"), dict) else {}
    liq_rel = _finite_int(liq_data.get("relative_score"), min_val=1, max_val=10)
    raw_landform = str(liq_data.get("landform_name") or "").strip()
    landform_name: Optional[str] = raw_landform if raw_landform else None
    if liq_rel == 1:
        liq_score = 12
    elif liq_rel == 2:
        liq_score = 9
    elif liq_rel == 3:
        liq_score = 5
    elif liq_rel is not None and liq_rel >= 4:
        liq_score = 2
    else:
        liq_score = 4

    flood_data = carte.get("flood_risk") if isinstance(carte.get("flood_risk"), dict) else {}
    is_flooded = flood_data.get("is_flooded") if isinstance(flood_data.get("is_flooded"), bool) else None
    max_scen = flood_data.get("max_scenario") if isinstance(flood_data.get("max_scenario"), dict) else {}
    depth_label = str(max_scen.get("depth_label") or "").strip() or None
    if is_flooded is False:
        flood_score = 10
        flood_status = "浸水想定区域外（公的判定：安全）"
    elif is_flooded is True:
        parsed_rank = _finite_int(max_scen.get("rank_code"), min_val=0, max_val=20)
        rank_code = parsed_rank if parsed_rank is not None else 2
        if rank_code <= 1:
            flood_score = 7
        elif rank_code == 2:
            flood_score = 4
        else:
            flood_score = 1
        flood_status = f"洪水浸水想定区域内（想定最大深：{depth_label or '要確認'}）"
    else:
        flood_score = 4
        flood_status = "浸水想定データ照会未確定"

    ls_data = carte.get("landslide_risk") if isinstance(carte.get("landslide_risk"), dict) else {}
    ls_red = ls_data.get("is_red_zone") if isinstance(ls_data.get("is_red_zone"), bool) else None
    ls_warn = ls_data.get("is_warning_zone") if isinstance(ls_data.get("is_warning_zone"), bool) else None
    if ls_red is True:
        ls_score = 0
    elif ls_warn is True:
        ls_score = 2
    elif ls_warn is False and ls_red is not True:
        ls_score = 5
    else:
        ls_score = 2

    emb_data = carte.get("embankment") if isinstance(carte.get("embankment"), dict) else {}
    emb_desig = emb_data.get("is_designated") if isinstance(emb_data.get("is_designated"), bool) else None
    if emb_desig is False:
        emb_score = 4
        emb_status = "大規模盛土造成地該当なし"
    elif emb_desig is True:
        emb_score = 1
        emb_status = "大規模盛土造成地内"
    else:
        emb_score = 2
        emb_status = "盛土造成地データ照会未確定"

    coast_data = carte.get("coastal_hazard") if isinstance(carte.get("coastal_hazard"), dict) else {}
    coast_inun = coast_data.get("is_inundated") if isinstance(coast_data.get("is_inundated"), bool) else None
    if coast_inun is False:
        coast_score = 4
        coastal_status = "高潮・津波浸水想定区域外"
    elif coast_inun is True:
        coast_score = 1
        coastal_status = "高潮・津波浸水想定区域内"
    else:
        coast_score = 2
        coastal_status = "高潮・津波データ照会未確定"

    resilience_score = min(35, max(0, liq_score + flood_score + ls_score + emb_score + coast_score))
    if landform_name:
        resilience_assessment = (
            f"地形区分は「{landform_name}」（{liq_data.get('tendency_label') or '地盤判定済'}）に位置し、"
            f"{flood_status}・{emb_status}となっています。"
        )
    else:
        resilience_assessment = f"{flood_status}・{emb_status}・{coastal_status}の公的判定結果に基づきます。"

    hazard_fully_verified_safe = bool(
        is_flooded is False
        and coast_inun is False
        and emb_desig is False
        and ls_warn is False
        and ls_red is not True
        and liq_rel in (1, 2)
    )
    any_hazard_elevated = bool(
        is_flooded is True
        or coast_inun is True
        or emb_desig is True
        or ls_warn is True
        or ls_red is True
        or (liq_rel is not None and liq_rel >= 3)
    )

    # -------------------------------------------------------------------------
    # 3. Asset Defense (資産防衛・将来持続性: 20点満点)
    # -------------------------------------------------------------------------
    pop_data = carte.get("population_projection") if isinstance(carte.get("population_projection"), dict) else {}
    pop_ratio = _finite_float_bounded(
        pop_data.get("maintenance_ratio_percent"), min_val=0.0, max_val=5000.0
    )
    if pop_ratio is not None:
        if pop_ratio >= 115.0:
            pop_score = 12
        elif pop_ratio >= 105.0:
            pop_score = 10
        elif pop_ratio >= 95.0:
            pop_score = 8
        elif pop_ratio >= 85.0:
            pop_score = 5
        else:
            pop_score = 2
    else:
        pop_score = 5

    lp_data = carte.get("land_price") if isinstance(carte.get("land_price"), dict) else {}
    lp_nearest = lp_data.get("nearest") if isinstance(lp_data.get("nearest"), list) else []
    lp_price: Optional[int] = None
    lp_change: Optional[float] = None
    if lp_nearest and isinstance(lp_nearest[0], dict):
        lp_price = _finite_int(lp_nearest[0].get("price_yen_per_sqm"), min_val=1, max_val=500_000_000)
        lp_change = _finite_float_bounded(lp_nearest[0].get("change_rate_percent"), min_val=-100.0, max_val=1000.0)

    if lp_change is not None:
        if lp_change >= 6.0:
            lp_score = 8
        elif lp_change >= 3.0:
            lp_score = 6
        elif lp_change >= 0.0:
            lp_score = 4
        else:
            lp_score = 2
    else:
        lp_score = 3

    asset_defense_score = min(20, max(0, pop_score + lp_score))
    lp_price_str = f"{lp_price:,}円/㎡" if lp_price is not None else "価格未確定"
    if pop_ratio is not None and lp_change is not None:
        asset_assessment = (
            f"2050年推計人口維持率は{pop_ratio:.1f}%（2020年比）、"
            f"最寄公示地価の前年変動率は{lp_change:+.1f}%（{lp_price_str}）と推移しています。"
        )
    elif lp_price is not None:
        lp_chg_part = f"（前年比{lp_change:+.1f}%）" if lp_change is not None else ""
        asset_assessment = f"最寄公示地価は{lp_price:,}円/㎡{lp_chg_part}と記録されています。"
    else:
        asset_assessment = "周辺地価および将来推計人口メッシュの一部データが未確定のため、近隣標準地の推移を現地でご確認ください。"

    # -------------------------------------------------------------------------
    # 4. Livability (生活・医療インフラ密度: 15点満点)
    # -------------------------------------------------------------------------
    med_data = carte.get("medical") if isinstance(carte.get("medical"), dict) else {}
    med_list = [m for m in (med_data.get("nearest") or []) if isinstance(m, dict)] if isinstance(med_data.get("nearest"), list) else []
    nearest_med_m: Optional[int] = _finite_int(med_list[0].get("distance_from_query_m"), min_val=0, max_val=100_000) if med_list else None
    has_emergency = any(bool(m.get("is_emergency")) for m in med_list)

    if nearest_med_m is not None and nearest_med_m <= 400:
        med_dist_score = 5
    elif nearest_med_m is not None and nearest_med_m <= 800:
        med_dist_score = 3
    elif nearest_med_m is not None:
        med_dist_score = 1
    else:
        med_dist_score = 0

    med_density_score = 3 if (has_emergency or len(med_list) >= 5) else (1 if med_list else 0)
    med_score = min(8, med_dist_score + med_density_score)

    nur_data = carte.get("nursery") if isinstance(carte.get("nursery"), dict) else {}
    nur_list = [n for n in (nur_data.get("nearest") or []) if isinstance(n, dict)] if isinstance(nur_data.get("nearest"), list) else []
    nearest_nur_m: Optional[int] = _finite_int(nur_list[0].get("distance_from_query_m"), min_val=0, max_val=100_000) if nur_list else None
    if nearest_nur_m is not None and nearest_nur_m <= 400:
        nur_score = 4
    elif nearest_nur_m is not None and nearest_nur_m <= 800:
        nur_score = 2
    elif nearest_nur_m is not None:
        nur_score = 1
    else:
        nur_score = 0

    sch_data = carte.get("school_district") if isinstance(carte.get("school_district"), dict) else {}
    elem_obj = sch_data.get("elementary_school") if isinstance(sch_data.get("elementary_school"), dict) else {}
    jhs_obj = sch_data.get("junior_high_school") if isinstance(sch_data.get("junior_high_school"), dict) else {}
    elem_name = str(elem_obj.get("school_name") or "").strip() or None
    jhs_name = str(jhs_obj.get("school_name") or "").strip() or None
    sch_score = 3 if (elem_name and jhs_name) else (2 if (elem_name or jhs_name) else 0)

    livability_score = min(15, max(0, med_score + nur_score + sch_score))
    livability_assessment = (
        f"最寄医療機関まで約{nearest_med_m if nearest_med_m is not None else '-'}m、"
        f"保育施設まで約{nearest_nur_m if nearest_nur_m is not None else '-'}m、"
        f"通学区域は「{elem_name or '未確定'} / {jhs_name or '未確定'}」に指定されています。"
    )

    # -------------------------------------------------------------------------
    # Total Score & Rank (Fail-Closed when core layers are unverified)
    # -------------------------------------------------------------------------
    total_score = int(tranquility_score + resilience_score + asset_defense_score + livability_score)
    core_verified_count = sum([
        zoning_name is not None,
        is_flooded is not None,
        liq_rel is not None,
        emb_desig is not None,
        coast_inun is not None,
    ])

    if core_verified_count < 3:
        rank = "C"
        rank_label = "Cランク（環境・災害リスク要確認）"
    elif total_score >= 85:
        rank = "S"
        rank_label = "Sランク（極めて良好な住環境・地盤資産）"
    elif total_score >= 72:
        rank = "A"
        rank_label = "Aランク（良好な住環境・安定資産）"
    elif total_score >= 58:
        rank = "B"
        rank_label = "Bランク（利便性優先・標準環境）"
    else:
        rank = "C"
        rank_label = "Cランク（環境・災害リスク要確認）"

    # -------------------------------------------------------------------------
    # Signature Index 1: Silent Value Index (静寂単価効率)
    # -------------------------------------------------------------------------
    env_protection_rate = round(((tranquility_score + resilience_score) / 65.0) * 100.0, 1)
    has_valid_lp = lp_price is not None and lp_price > 0
    high_land_price = bool(has_valid_lp and lp_price >= 1500000)
    moderate_land_price = bool(has_valid_lp and lp_price < 1500000)

    if high_land_price and env_protection_rate >= 78.0 and lp_price is not None:
        sv_category = "プレミアム静寂資産（高地価 × 最高位住環境）"
        sv_rationale = (
            f"公示地価水準（{lp_price:,}円/㎡）に見合う住環境・地盤保護率（{env_protection_rate}%）が確保されており、"
            "長期にわたり静粛性と資産性が両立する一等地と評価されます。"
        )
    elif moderate_land_price and env_protection_rate >= 75.0:
        sv_category = "高効率・隠れ静寂立地（適正地価 × 優良住環境）"
        sv_rationale = (
            f"周辺地価水準（{lp_price:,}円/㎡）に対して住環境・地盤保護率（{env_protection_rate}%）が高く、"
            "商業騒音や地盤リスクを抑えながら実質的な居住品質を効率よく享受できる立地です。"
        )
    elif (is_commercial_or_industrial or (far_pct is not None and far_pct >= 400)) and env_protection_rate < 75.0:
        sv_category = "利便特化・商業活性立地（高密度 × 商業近接）"
        sv_rationale = (
            f"住環境・地盤保護率は{env_protection_rate}%となり、静粛性よりも都心交通・商業利便性に重きを置いた高密度エリアです。"
            "開口部の遮音等級や高層階住戸の選択が有効です。"
        )
    elif env_protection_rate < 65.0:
        sv_category = "価格先行・環境摩擦注意（高コスト × 環境・地盤リスク）"
        sv_rationale = (
            f"住環境・地盤保護率が{env_protection_rate}%にとどまっており、賃料・価格水準に対して騒音や自然災害リスクの事前確認が特に重要なエリアです。"
        )
    else:
        sv_category = "バランス型標準住環境"
        if not has_valid_lp and env_protection_rate >= 75.0:
            sv_rationale = (
                f"住環境・地盤保護率は{env_protection_rate}%と良好な水準にありますが、最寄公示地価データが未確定のため周辺相場との比較確認が推奨されます。"
            )
        else:
            sv_rationale = (
                f"住環境・地盤保護率は{env_protection_rate}%と標準的な水準にあり、日常の利便性と居住環境のバランスが取れた立地です。"
            )

    # -------------------------------------------------------------------------
    # Signature Index 2: Demographic Momentum (2050年実需持続性 × 地価モメンタム)
    # -------------------------------------------------------------------------
    if pop_ratio is not None and lp_change is not None:
        if pop_ratio >= 105.0 and lp_change >= 3.0:
            dm_category = "実需牽引型（2050年人口増 × 地価堅調）"
            dm_rationale = (
                f"2050年推計人口維持率が{pop_ratio:.1f}%と増加基調にあり、地価変動率（{lp_change:+.1f}%）も実需の流入に裏付けられています。"
            )
        elif pop_ratio >= 95.0 and lp_change >= 0.0:
            dm_category = "安定成熟型（人口維持 × 地価安定）"
            dm_rationale = (
                f"2050年推計人口維持率{pop_ratio:.1f}%・地価変動率{lp_change:+.1f}%と、急激な人口流出リスクが低く成熟した居住需要が見込まれます。"
            )
        elif pop_ratio < 95.0 and lp_change >= 3.0:
            dm_category = "過熱警戒型（将来人口減予測 × 地価上昇）"
            dm_rationale = (
                f"地価は前年比{lp_change:+.1f}%上昇している一方、250mメッシュの2050年人口維持率は{pop_ratio:.1f}%と減少予測であり、将来的な需給ギャップに留意が必要です。"
            )
        else:
            dm_category = "再編・実需選別型"
            dm_rationale = (
                f"2050年人口維持率（{pop_ratio:.1f}%）と地価動向（{lp_change:+.1f}%）から、物件個別の管理状態や駅距離による選別が進みやすいエリアです。"
            )
    else:
        dm_category = "再編・実需選別型"
        dm_rationale = "メッシュ人口推計または地価公示データが一部未確定のため、近隣標準地の推移を参考値として評価しています。"

    # -------------------------------------------------------------------------
    # Editorial Summary & Inspection Checkpoints (Quiet Luxury, Zero Emojis, Fail-Closed)
    # -------------------------------------------------------------------------
    editorial_summary = (
        f"総合評価は{rank_label}（{total_score}点/100点）です。"
        f"{tranquility_assessment}"
        f"地盤・防災面では、{resilience_assessment}"
        f"また、{sv_rationale}"
    )

    checkpoints: List[str] = []
    if zoning_name is None:
        checkpoints.append("用途地域データが未確定のため、自治体の都市計画図原図にて用途地域・建蔽率・容積率の指定状況を必ずご確認ください。")
    elif is_commercial_or_industrial or (far_pct is not None and far_pct >= 400):
        checkpoints.append("商業・高容積率エリアのため、夜間・休日の前面道路の交通量および窓サッシの遮音性能（T等級・二重サッシの有無）を現地でご確認ください。")
    else:
        checkpoints.append("住居系地域に位置しますが、最寄駅からの夜間動線における街灯の明るさや歩道幅員を現地でご確認ください。")

    if any_hazard_elevated:
        checkpoints.append("低地・浸水または液状化想定が見られるため、マンションの受変電設備（電気室）のかさ上げ位置および止水板の配備状況をご確認ください。")
    elif hazard_fully_verified_safe:
        checkpoints.append("公的ハザード上の浸水・地盤リスクは低く抑えられていますが、建物周囲の排水勾配や擁壁・基礎まわりの健全性を念のためご確認ください。")
    else:
        checkpoints.append("一部の公的ハザードまたは地盤レイヤーが未確定のため、重要事項説明時のハザードマップ原図および地盤調査履歴を必ずご確認ください。")

    if not has_urban_control:
        checkpoints.append("高度地区や地区計画の指定が確認されないため、南側・主採光面の隣地に将来建て替え余地（コインパーキングや古家等）がないかをご確認ください。")

    return {
        "version": "1.0",
        "total_score": total_score,
        "rank": rank,
        "rank_label": rank_label,
        "pillars": {
            "tranquility": {
                "score": tranquility_score,
                "max_score": 30,
                "zoning_name": zoning_name,
                "floor_area_ratio_percent": far_pct,
                "has_urban_control": has_urban_control,
                "assessment": tranquility_assessment,
            },
            "resilience": {
                "score": resilience_score,
                "max_score": 35,
                "landform_name": landform_name,
                "flood_status": flood_status,
                "embankment_status": emb_status,
                "coastal_status": coastal_status,
                "assessment": resilience_assessment,
            },
            "asset_defense": {
                "score": asset_defense_score,
                "max_score": 20,
                "population_maintenance_2050_percent": pop_ratio,
                "land_price_yen_per_sqm": lp_price,
                "land_price_change_percent": lp_change,
                "assessment": asset_assessment,
            },
            "livability": {
                "score": livability_score,
                "max_score": 15,
                "nearest_medical_m": nearest_med_m,
                "has_emergency_hospital_nearby": has_emergency,
                "nearest_nursery_m": nearest_nur_m,
                "elementary_school": elem_name,
                "junior_high_school": jhs_name,
                "assessment": livability_assessment,
            },
        },
        "signature_indices": {
            "silent_value_index": {
                "env_protection_rate": env_protection_rate,
                "category": sv_category,
                "rationale": sv_rationale,
            },
            "demographic_momentum": {
                "population_2050_ratio_percent": pop_ratio,
                "land_price_change_percent": lp_change,
                "category": dm_category,
                "rationale": dm_rationale,
            },
        },
        "editorial_summary": editorial_summary,
        "inspection_checkpoints": checkpoints,
    }

