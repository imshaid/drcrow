"""
Star award helper — called by every deliver_* function.

Download stars (downloader gets dl_stars, uploader gets up_stars):
    book             : dl=3, up=2
    solution_manual  : dl=2, up=2
    solve            : dl=2, up=1
    note             : dl=2, up=1
    psq              : dl=1, up=1
    vidoc            : dl=1, up=1
    syllabus/outline : dl=1, up=0.5   (stored as utility category)
    cal/advisor/fee  : dl=1, up=0.5   (stored as utility category)
    utility          : dl=0.5, up=0.5
    waiver/regpay    : dl=0.5, up=0.5

Upload stars are awarded here only when the uploader is NOT an admin.
Every download counts (duplicates allowed per spec).
"""

import logging
from database import queries
from config.settings import settings

logger = logging.getLogger(__name__)

# (downloader_stars, uploader_stars)
_STAR_MAP: dict[str, tuple[float, float]] = {
    "book":             (3,   2),
    "solution_manual":  (2,   2),
    "solve":            (2,   1),
    "note":             (2,   1),
    "psq":              (1,   1),
    "vidoc":            (1,   1),
    "syllabus":         (1, 0.5),
    "outline":          (1, 0.5),
    "routine":          (1, 0.5),
    "cal":              (1, 0.5),
    "advisor":          (1, 0.5),
    "fee":              (1, 0.5),
    "utility":          (0.5, 0.5),
    "waiver":           (0.5, 0.5),
    "regpay":           (0.5, 0.5),
}


async def award_download(
    downloader_id: int,
    category: str,
    uploader_id: int | None,
    resource_uid: str = "",
) -> None:
    """
    Award download stars to downloader and uploader.
    Call this right after increment_*_access in every deliver_* function.

    Args:
        downloader_id : Telegram user_id of the person downloading.
        category      : One of the keys in _STAR_MAP (e.g. "book", "psq").
        uploader_id   : uploaded_by field from the resource record (None if unknown).
        resource_uid  : For analytics meta only.
    """
    cat = category.lower()
    dl_stars, up_stars = _STAR_MAP.get(cat, (0.5, 0.5))

    try:
        # Downloader always gets stars
        await queries.add_stars(downloader_id, dl_stars, f"download:{cat}:{resource_uid}")

        # Increment download_count
        from database.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET download_count = download_count + 1 WHERE user_id = $1",
                downloader_id
            )

        # Uploader gets stars only if they uploaded (not admin-seeded content)
        if uploader_id and not settings.is_admin(uploader_id):
            await queries.add_stars(uploader_id, up_stars, f"download_share:{cat}:{resource_uid}")

    except Exception as e:
        logger.warning(f"award_download failed for {downloader_id}/{cat}: {e}")


async def award_upload(uploader_id: int, category: str, resource_uid: str = "") -> None:
    """
    Award upload stars and increment upload_count.
    Call this after a resource is successfully saved to DB.
    Do NOT call for admin-seeded content.

    Upload star values:
        book=15, solution_manual=12, solve=10, note=8,
        psq=6, vidoc=6, syllabus=4, outline=4,
        routine=3, cal=3, advisor=3, fee=3,
        utility=2, waiver=2, regpay=2
    """
    _UPLOAD_MAP: dict[str, float] = {
        "book": 15, "solution_manual": 12, "solve": 10, "note": 8,
        "psq": 6, "vidoc": 6, "syllabus": 4, "outline": 4,
        "routine": 3, "cal": 3, "advisor": 3, "fee": 3,
        "utility": 2, "waiver": 2, "regpay": 2,
    }
    cat = category.lower()
    star_val = _UPLOAD_MAP.get(cat, 2)

    try:
        await queries.add_stars(uploader_id, star_val, f"upload:{cat}:{resource_uid}")
        from database.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET upload_count = upload_count + 1 WHERE user_id = $1",
                uploader_id
            )
    except Exception as e:
        logger.warning(f"award_upload failed for {uploader_id}/{cat}: {e}")