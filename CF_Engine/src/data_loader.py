"""STEP 1: watch_history → User-Item 희소 행렬 변환."""

import logging

import numpy as np
from scipy.sparse import csr_matrix

from CF_Engine.src.base import CFBase

log = logging.getLogger(__name__)


class DataLoader(CFBase):
    """watch_history DB 조회 → 희소 행렬 변환."""

    def load_matrix(self, conn, alpha: float = 40, filter_quality: bool = False):
        """watch_history 전체 로드 → csr_matrix 반환.

        Args:
            filter_quality: True이면 poster_url AND vod_embedding 둘 다 있는 VOD만 포함

        Returns:
            mat: csr_matrix (n_users x n_items), confidence = 1 + alpha * completion_rate
            user_encoder: {user_id_fk: row_idx}
            item_encoder: {vod_id_fk: col_idx}
            user_decoder: {row_idx: user_id_fk}
            item_decoder: {col_idx: vod_id_fk}
        """
        log.info("watch_history 로드 중%s...", " (품질 필터 ON)" if filter_quality else "")
        cur = conn.cursor()
        if filter_quality:
            cur.execute("""
                SELECT w.user_id_fk, w.vod_id_fk, w.completion_rate
                FROM watch_history w
                JOIN public.vod v ON w.vod_id_fk = v.full_asset_id
                JOIN public.vod_embedding ve ON w.vod_id_fk = ve.vod_id_fk
                JOIN public."user" u ON u.sha2_hash = w.user_id_fk
                WHERE w.completion_rate IS NOT NULL
                  AND v.poster_url IS NOT NULL
                  AND u.is_test = FALSE
            """)
        else:
            cur.execute("""
                SELECT w.user_id_fk, w.vod_id_fk, w.completion_rate
                FROM public.watch_history w
                JOIN public."user" u ON u.sha2_hash = w.user_id_fk
                WHERE w.completion_rate IS NOT NULL
                  AND u.is_test = FALSE
            """)
        rows = cur.fetchall()
        cur.close()
        log.info("로드 완료: %d행", len(rows))

        user_ids = sorted(set(r[0] for r in rows))
        item_ids = sorted(set(r[1] for r in rows))

        user_encoder = {u: i for i, u in enumerate(user_ids)}
        item_encoder = {v: i for i, v in enumerate(item_ids)}
        user_decoder = {i: u for u, i in user_encoder.items()}
        item_decoder = {i: v for v, i in item_encoder.items()}

        u_idx = [user_encoder[r[0]] for r in rows]
        i_idx = [item_encoder[r[1]] for r in rows]
        conf = [1.0 + alpha * float(r[2]) for r in rows]

        mat = csr_matrix(
            (conf, (u_idx, i_idx)),
            shape=(len(user_encoder), len(item_encoder)),
            dtype=np.float32,
        )

        cells = mat.shape[0] * mat.shape[1]
        sparsity = (1 - mat.nnz / cells) if cells else 0.0
        log.info(
            "행렬: %d x %d  |  상호작용: %d건  |  희소성: %.2f%%",
            mat.shape[0], mat.shape[1], mat.nnz, sparsity * 100,
        )
        return mat, user_encoder, item_encoder, user_decoder, item_decoder

    def load_interactions(self, conn, filter_quality: bool = False):
        """watch_history 상호작용을 시청 시각(strt_dt) 포함 raw 행으로 로드.

        시간순 Hold-out 분리(src.evaluation.temporal_holdout_split)용. 집계 전
        개별 상호작용을 반환하므로 반복 시청·시각 정보를 평가 단계에서 활용한다.

        Returns:
            List[tuple]: (user_id_fk, vod_id_fk, completion_rate, strt_dt)
        """
        log.info("watch_history 로드(시각 포함)%s...", " (품질 필터 ON)" if filter_quality else "")
        cur = conn.cursor()
        if filter_quality:
            cur.execute("""
                SELECT w.user_id_fk, w.vod_id_fk, w.completion_rate, w.strt_dt
                FROM public.watch_history w
                JOIN public.vod v ON w.vod_id_fk = v.full_asset_id
                JOIN public.vod_embedding ve ON w.vod_id_fk = ve.vod_id_fk
                JOIN public."user" u ON u.sha2_hash = w.user_id_fk
                WHERE w.completion_rate IS NOT NULL
                  AND v.poster_url IS NOT NULL
                  AND u.is_test = FALSE
            """)
        else:
            cur.execute("""
                SELECT w.user_id_fk, w.vod_id_fk, w.completion_rate, w.strt_dt
                FROM public.watch_history w
                JOIN public."user" u ON u.sha2_hash = w.user_id_fk
                WHERE w.completion_rate IS NOT NULL
                  AND u.is_test = FALSE
            """)
        rows = cur.fetchall()
        cur.close()
        log.info("상호작용 로드 완료: %d건", len(rows))
        return rows


# ── 모듈 레벨 싱글턴 + 하위 호환 별칭 ──────────────────────────────────────
data_loader = DataLoader()
get_conn = CFBase.get_conn
load_matrix = data_loader.load_matrix
load_interactions = data_loader.load_interactions
