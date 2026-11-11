"""STEP 3: ALS 출력 → DB 저장 형식 변환.

시리즈 중복 제거:
- TV 연예/오락 → 에피소드 단위 유지 (게스트 출연이 회차별 다름)
- 그 외 → series_nm 기준 중복 제거 (같은 시리즈 에피소드 중 최고 score 1건만)
"""

import logging

from CF_Engine.src.base import CFBase

log = logging.getLogger(__name__)

# 에피소드 단위 유지 대상 ct_cl (시리즈 중복 제거 제외)
_EPISODE_LEVEL_CT_CL = frozenset(["TV 연예/오락"])


class Recommender(CFBase):
    """ALS 추천 결과 → DB 저장 형식 변환."""

    @staticmethod
    def load_vod_series_map(conn) -> dict[str, tuple[str, str]]:
        """vod_id → (series_nm, ct_cl) 매핑 로드."""
        cur = conn.cursor()
        cur.execute("SELECT full_asset_id, series_nm, ct_cl FROM public.vod")
        vod_map = {}
        for vod_id, series_nm, ct_cl in cur.fetchall():
            vod_map[vod_id] = (series_nm or vod_id, ct_cl or "")
        cur.close()
        log.info("VOD 시리즈 매핑 로드: %d건", len(vod_map))
        return vod_map

    @staticmethod
    def load_popular_items(conn, limit: int = 200) -> list[str]:
        """serving.popular_recommendation 에서 인기 VOD를 score 내림차순으로 로드.

        Cold/추천 부족 유저의 Fallback 후보. Normal_Recommendation 브랜치가 적재한
        'POPULAR' 추천을 CF가 '읽기' 로 재사용한다(중복 vod는 최고 score 1건).
        """
        cur = conn.cursor()
        cur.execute("""
            SELECT vod_id_fk, MAX(score) AS s
            FROM serving.popular_recommendation
            GROUP BY vod_id_fk
            ORDER BY s DESC
            LIMIT %s
        """, (limit,))
        items = [r[0] for r in cur.fetchall()]
        cur.close()
        log.info("Fallback 인기 아이템 로드: %d개", len(items))
        return items

    @staticmethod
    def apply_fallback(records: list[dict], popular_items: list[str],
                       top_k: int = 20, recommendation_type: str = "COLLABORATIVE",
                       vod_series_map: dict | None = None,
                       watched_by_user: dict | None = None,
                       all_user_ids: list | None = None,
                       fallback_score: float = 0.0) -> tuple[list[dict], dict]:
        """유저별 추천이 top_k 미만이면 인기 아이템으로 중복 없이 채운다.

        제외 규칙:
          - 이미 해당 유저에게 추천된 VOD
          - 이미 추천에 포함된 시리즈(에피소드 단위 유지 ct_cl 제외)
          - 해당 유저가 이미 시청한 VOD(watched_by_user 제공 시)

        Args:
            records: build_records 출력(flat). user_id_fk/vod_id_fk/rank/score/type.
            popular_items: 인기 VOD id 리스트(상위부터).
            all_user_ids: 전체 대상 유저. records 에 없는(추천 0건) cold 유저도
                이 목록에 있으면 인기 추천으로 생성한다. None이면 records 의 유저만.
            watched_by_user: {user_id: set(vod_id)} 기시청 제외용.

        Returns:
            (new_records, stats). stats: 유저/보충 건수/잔여 부족 유저 수.
        """
        # 유저별 그룹화(원래 순서 유지)
        by_user: dict = {}
        order: list = []
        for r in records:
            uid = r["user_id_fk"]
            if uid not in by_user:
                by_user[uid] = []
                order.append(uid)
            by_user[uid].append(r)

        # 추천 0건 cold 유저 포함
        if all_user_ids is not None:
            for uid in all_user_ids:
                if uid not in by_user:
                    by_user[uid] = []
                    order.append(uid)

        def series_of(vod_id):
            if vod_series_map is None:
                return None
            series_nm, ct_cl = vod_series_map.get(vod_id, (vod_id, ""))
            if ct_cl in _EPISODE_LEVEL_CT_CL:
                return None   # 에피소드 단위 유지 → 시리즈 중복 제거 대상 아님
            return series_nm

        stats = {
            "users_total": len(order),
            "users_underfilled_before": 0,
            "users_filled": 0,
            "items_added": 0,
            "users_zero_before": 0,
            "users_still_short_after": 0,
        }

        new_records: list[dict] = []
        for uid in order:
            user_recs = by_user[uid]
            if len(user_recs) == 0:
                stats["users_zero_before"] += 1
            if len(user_recs) < top_k:
                stats["users_underfilled_before"] += 1

            seen_vods = {r["vod_id_fk"] for r in user_recs}
            seen_series = {s for s in (series_of(r["vod_id_fk"]) for r in user_recs) if s}
            watched = watched_by_user.get(uid, set()) if watched_by_user else set()
            rank = max((r["rank"] for r in user_recs), default=0)

            filled = list(user_recs)
            added_here = 0
            if len(filled) < top_k:
                for vod_id in popular_items:
                    if len(filled) >= top_k:
                        break
                    if vod_id in seen_vods or vod_id in watched:
                        continue
                    s = series_of(vod_id)
                    if s is not None and s in seen_series:
                        continue
                    rank += 1
                    filled.append({
                        "user_id_fk": uid,
                        "vod_id_fk": vod_id,
                        "rank": rank,
                        "score": float(fallback_score),
                        "recommendation_type": recommendation_type,
                    })
                    seen_vods.add(vod_id)
                    if s is not None:
                        seen_series.add(s)
                    added_here += 1

            if added_here > 0:
                stats["users_filled"] += 1
                stats["items_added"] += added_here
            if len(filled) < top_k:
                stats["users_still_short_after"] += 1
            new_records.extend(filled)

        log.info(
            "Fallback 적용: 부족 유저 %d명(0건 %d명) → 보충 %d명 / %d건, 잔여 부족 %d명",
            stats["users_underfilled_before"], stats["users_zero_before"],
            stats["users_filled"], stats["items_added"], stats["users_still_short_after"],
        )
        return new_records, stats

    @staticmethod
    def build_records(user_ids, item_indices, scores,
                      user_decoder: dict, item_decoder: dict,
                      recommendation_type: str = "CF",
                      top_k: int = 20,
                      vod_series_map: dict | None = None) -> list[dict]:
        """ALS 추천 결과를 serving.vod_recommendation 저장 형식으로 변환.

        vod_series_map이 제공되면 시리즈 중복 제거 적용:
        - TV 연예/오락: 에피소드 단위 유지
        - 그 외: series_nm 기준 중복 제거 (최고 score 1건)

        Returns:
            List[dict]: user_id_fk, vod_id_fk, rank, score, recommendation_type
        """
        records = []
        skipped = 0
        for uid, item_row, score_row in zip(user_ids, item_indices, scores):
            user_id_fk = user_decoder[uid]
            seen_series: set[str] = set()
            rank = 0
            for iid, score in zip(item_row, score_row):
                vod_id = item_decoder[iid]

                # 시리즈 중복 제거
                if vod_series_map is not None:
                    series_nm, ct_cl = vod_series_map.get(vod_id, (vod_id, ""))
                    if ct_cl not in _EPISODE_LEVEL_CT_CL:
                        if series_nm in seen_series:
                            skipped += 1
                            continue
                        seen_series.add(series_nm)

                rank += 1
                if rank > top_k:
                    break
                records.append({
                    "user_id_fk": user_id_fk,
                    "vod_id_fk": vod_id,
                    "rank": rank,
                    "score": float(score),
                    "recommendation_type": recommendation_type,
                })
        log.info("레코드 생성 완료: %d건 (시리즈 중복 제거: %d건 스킵)", len(records), skipped)
        return records


# ── 모듈 레벨 싱글턴 + 하위 호환 별칭 ──────────────────────────────────────
recommender = Recommender()
load_vod_series_map = Recommender.load_vod_series_map
build_records = Recommender.build_records
load_popular_items = Recommender.load_popular_items
apply_fallback = Recommender.apply_fallback
