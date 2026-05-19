"""
imgBB image upload utility.
"""

import logging
import aiohttp
import base64
from config.settings import settings

logger = logging.getLogger(__name__)


async def upload_to_imgbb(bot, file_id: str) -> str:
    """
    Download file from Telegram and upload to imgBB.
    Returns permanent image URL or empty string on failure.
    """
    if not settings.IMGBB_API_KEY:
        logger.error("IMGBB_API_KEY is not set in .env")
        return ""

    try:
        # Step 1: Get file info from Telegram
        tg_file = await bot.get_file(file_id)

        # file_path can be either:
        # (a) relative path: "photos/file_8.jpg"
        # (b) full URL: "https://api.telegram.org/file/bot.../photos/file_8.jpg"
        file_path = tg_file.file_path
        if file_path.startswith("http"):
            file_url = file_path  # already full URL
        else:
            file_url = f"https://api.telegram.org/file/bot{bot.token}/{file_path}"

        logger.info(f"Downloading: {file_url}")

        async with aiohttp.ClientSession() as session:

            # Step 2: Download image bytes
            async with session.get(file_url) as resp:
                if resp.status != 200:
                    logger.error(f"Telegram download failed: HTTP {resp.status} — URL: {file_url}")
                    return ""
                image_bytes = await resp.read()
                logger.info(f"Downloaded {len(image_bytes)} bytes")

            # Step 3: Upload to imgBB as base64
            image_b64 = base64.b64encode(image_bytes).decode("utf-8")

            async with session.post(
                "https://api.imgbb.com/1/upload",
                data={
                    "key": settings.IMGBB_API_KEY,
                    "image": image_b64,
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                response_text = await resp.text()
                logger.info(f"imgBB response: HTTP {resp.status} — {response_text[:200]}")

                if resp.status != 200:
                    logger.error(f"imgBB upload failed: {response_text[:200]}")
                    return ""

                data = await resp.json(content_type=None)

                if not data.get("success"):
                    logger.error(f"imgBB returned success=false: {data}")
                    return ""

                # Use display_url for best quality thumbnail
                url = data["data"].get("display_url") or data["data"]["url"]
                logger.info(f"imgBB upload success: {url}")
                return url

    except aiohttp.ClientError as e:
        logger.error(f"imgBB network error: {e}")
        return ""
    except Exception as e:
        logger.error(f"imgBB unexpected error: {e}", exc_info=True)
        return ""