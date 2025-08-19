"""
Artist endpoint router.
"""

from fastapi import APIRouter, HTTPException, Request, Depends, Query
from fastapi.responses import JSONResponse
import json
import traceback
from routes.utils.artist import download_artist_albums

# Imports for merged watch functionality
import logging
import threading
from routes.utils.watch.db import (
    add_artist_to_watch as add_artist_db,
    remove_artist_from_watch as remove_artist_db,
    get_watched_artist,
    get_watched_artists,
    add_specific_albums_to_artist_table,
    remove_specific_albums_from_artist_table,
    is_album_in_artist_db,
)
from routes.utils.watch.manager import check_watched_artists, get_watch_config
from routes.utils.get_info import get_spotify_info

# Import authentication dependencies
from routes.auth.middleware import require_auth_from_state, User

router = APIRouter()

# Existing log_json can be used, or a logger instance.
# Let's initialize a logger for consistency with merged code.
logger = logging.getLogger(__name__)


def construct_spotify_url(item_id: str, item_type: str = "track") -> str:
    """Construct a Spotify URL for a given item ID and type."""
    return f"https://open.spotify.com/{item_type}/{item_id}"


def log_json(message_dict):
    print(json.dumps(message_dict))


@router.get("/download/{artist_id}")
async def handle_artist_download(
    artist_id: str,
    request: Request,
    current_user: User = Depends(require_auth_from_state),
):
    """
    Enqueues album download tasks for the given artist.
    Expected query parameters:
      - album_type: string(s); comma-separated values such as "album,single,appears_on,compilation"
    """
    # Construct the artist URL from artist_id
    url = construct_spotify_url(artist_id, "artist")

    # Retrieve essential parameters from the request.
    album_type = request.query_params.get("album_type", "album,single,compilation")

    # Validate required parameters
    if not url:  # This check is mostly for safety, as url is constructed
        return JSONResponse(
            content={"error": "Missing required parameter: url"}, status_code=400
        )

    try:
        # Import and call the updated download_artist_albums() function.
        # from routes.utils.artist import download_artist_albums # Already imported at top

        # Delegate to the download_artist_albums function which will handle album filtering
        successfully_queued_albums, duplicate_albums = download_artist_albums(
            url=url,
            album_type=album_type,
            request_args=dict(request.query_params),
            username=current_user.username,
        )

        # Return the list of album task IDs.
        response_data = {
            "status": "complete",
            "message": f"Artist discography processing initiated. {len(successfully_queued_albums)} albums queued.",
            "queued_albums": successfully_queued_albums,
        }
        if duplicate_albums:
            response_data["duplicate_albums"] = duplicate_albums
            response_data["message"] += (
                f" {len(duplicate_albums)} albums were already in progress or queued."
            )

        return JSONResponse(
            content=response_data,
            status_code=202,  # Still 202 Accepted as some operations may have succeeded
        )
    except Exception as e:
        return JSONResponse(
            content={
                "status": "error",
                "message": str(e),
                "traceback": traceback.format_exc(),
            },
            status_code=500,
        )


@router.get("/download/cancel")
async def cancel_artist_download():
    """
    Cancelling an artist download is not supported since the endpoint only enqueues album tasks.
    (Cancellation for individual album tasks can be implemented via the queue manager.)
    """
    return JSONResponse(
        content={"error": "Artist download cancellation is not supported."},
        status_code=400,
    )


@router.get("/info")
async def get_artist_info(
    request: Request, current_user: User = Depends(require_auth_from_state),
    limit: int = Query(10, ge=1),   # default=10, must be >=1
    offset: int = Query(0, ge=0)    # default=0, must be >=0
):
    """
    Retrieves Spotify artist metadata given a Spotify artist ID.
    Expects a query parameter 'id' with the Spotify artist ID.
    """
    spotify_id = request.query_params.get("id")

    if not spotify_id:
        return JSONResponse(content={"error": "Missing parameter: id"}, status_code=400)

    try:
        # Get artist metadata first
        artist_metadata = get_spotify_info(spotify_id, "artist")

        # Get artist discography for albums
        artist_discography = get_spotify_info(spotify_id, "artist_discography", limit=limit, offset=offset)

        # Combine metadata with discography
        artist_info = {**artist_metadata, "albums": artist_discography}

        # If artist_info is successfully fetched and has albums,
        # check if the artist is watched and augment album items with is_locally_known status
        if (
            artist_info
            and artist_info.get("albums")
            and artist_info["albums"].get("items")
        ):
            watched_artist_details = get_watched_artist(
                spotify_id
            )  # spotify_id is the artist ID
            if watched_artist_details:  # Artist is being watched
                for album_item in artist_info["albums"]["items"]:
                    if album_item and album_item.get("id"):
                        album_id = album_item["id"]
                        album_item["is_locally_known"] = is_album_in_artist_db(
                            spotify_id, album_id
                        )
                    elif album_item:  # Album object exists but no ID
                        album_item["is_locally_known"] = False
            # If not watched, or no albums, is_locally_known will not be added.
            # Frontend should handle absence of this key as false.

        return JSONResponse(content=artist_info, status_code=200)
    except Exception as e:
        return JSONResponse(
            content={"error": str(e), "traceback": traceback.format_exc()},
            status_code=500,
        )


# --- Merged Artist Watch Routes ---


@router.put("/watch/{artist_spotify_id}")
async def add_artist_to_watchlist(
    artist_spotify_id: str, current_user: User = Depends(require_auth_from_state)
):
    """Adds an artist to the watchlist."""
    watch_config = get_watch_config()
    if not watch_config.get("enabled", False):
        raise HTTPException(
            status_code=403,
            detail={"error": "Watch feature is currently disabled globally."},
        )

    logger.info(f"Attempting to add artist {artist_spotify_id} to watchlist.")
    try:
        if get_watched_artist(artist_spotify_id):
            return {"message": f"Artist {artist_spotify_id} is already being watched."}

        # Get artist metadata directly for name and basic info
        artist_metadata = get_spotify_info(artist_spotify_id, "artist")

        # Get artist discography for album count
        artist_album_list_data = get_spotify_info(
            artist_spotify_id, "artist_discography"
        )

        # Check if we got artist metadata
        if not artist_metadata or not artist_metadata.get("name"):
            logger.error(
                f"Could not fetch artist metadata for {artist_spotify_id} from Spotify."
            )
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"Could not fetch artist metadata for {artist_spotify_id} to initiate watch."
                },
            )

        # Check if we got album data
        if not artist_album_list_data or not isinstance(
            artist_album_list_data.get("items"), list
        ):
            logger.warning(
                f"Could not fetch album list details for artist {artist_spotify_id} from Spotify. Proceeding with metadata only."
            )

        # Construct the artist_data object expected by add_artist_db
        artist_data_for_db = {
            "id": artist_spotify_id,
            "name": artist_metadata.get("name", "Unknown Artist"),
            "albums": {  # Mimic structure if add_artist_db expects it for total_albums
                "total": artist_album_list_data.get("total", 0)
                if artist_album_list_data
                else 0
            },
            # Add any other fields add_artist_db might expect from a true artist object if necessary
        }

        add_artist_db(artist_data_for_db)

        logger.info(
            f"Artist {artist_spotify_id} ('{artist_metadata.get('name', 'Unknown Artist')}') added to watchlist. Their albums will be processed by the watch manager."
        )
        return {
            "message": f"Artist {artist_spotify_id} added to watchlist. Albums will be processed shortly."
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error adding artist {artist_spotify_id} to watchlist: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail={"error": f"Could not add artist to watchlist: {str(e)}"},
        )


@router.get("/watch/{artist_spotify_id}/status")
async def get_artist_watch_status(
    artist_spotify_id: str, current_user: User = Depends(require_auth_from_state)
):
    """Checks if a specific artist is being watched."""
    logger.info(f"Checking watch status for artist {artist_spotify_id}.")
    try:
        artist = get_watched_artist(artist_spotify_id)
        if artist:
            return {"is_watched": True, "artist_data": dict(artist)}
        else:
            return {"is_watched": False}
    except Exception as e:
        logger.error(
            f"Error checking watch status for artist {artist_spotify_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail={"error": f"Could not check watch status: {str(e)}"}
        )


@router.delete("/watch/{artist_spotify_id}")
async def remove_artist_from_watchlist(
    artist_spotify_id: str, current_user: User = Depends(require_auth_from_state)
):
    """Removes an artist from the watchlist."""
    watch_config = get_watch_config()
    if not watch_config.get("enabled", False):
        raise HTTPException(
            status_code=403,
            detail={"error": "Watch feature is currently disabled globally."},
        )

    logger.info(f"Attempting to remove artist {artist_spotify_id} from watchlist.")
    try:
        if not get_watched_artist(artist_spotify_id):
            raise HTTPException(
                status_code=404,
                detail={"error": f"Artist {artist_spotify_id} not found in watchlist."},
            )

        remove_artist_db(artist_spotify_id)
        logger.info(f"Artist {artist_spotify_id} removed from watchlist successfully.")
        return {"message": f"Artist {artist_spotify_id} removed from watchlist."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error removing artist {artist_spotify_id} from watchlist: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail={"error": f"Could not remove artist from watchlist: {str(e)}"},
        )


@router.get("/watch/list")
async def list_watched_artists_endpoint(
    current_user: User = Depends(require_auth_from_state),
):
    """Lists all artists currently in the watchlist."""
    try:
        artists = get_watched_artists()
        return [dict(artist) for artist in artists]
    except Exception as e:
        logger.error(f"Error listing watched artists: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": f"Could not list watched artists: {str(e)}"},
        )


@router.post("/watch/trigger_check")
async def trigger_artist_check_endpoint(
    current_user: User = Depends(require_auth_from_state),
):
    """Manually triggers the artist checking mechanism for all watched artists."""
    watch_config = get_watch_config()
    if not watch_config.get("enabled", False):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "Watch feature is currently disabled globally. Cannot trigger check."
            },
        )

    logger.info("Manual trigger for artist check received for all artists.")
    try:
        thread = threading.Thread(target=check_watched_artists, args=(None,))
        thread.start()
        return {
            "message": "Artist check triggered successfully in the background for all artists."
        }
    except Exception as e:
        logger.error(
            f"Error manually triggering artist check for all: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail={"error": f"Could not trigger artist check for all: {str(e)}"},
        )


@router.post("/watch/trigger_check/{artist_spotify_id}")
async def trigger_specific_artist_check_endpoint(
    artist_spotify_id: str, current_user: User = Depends(require_auth_from_state)
):
    """Manually triggers the artist checking mechanism for a specific artist."""
    watch_config = get_watch_config()
    if not watch_config.get("enabled", False):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "Watch feature is currently disabled globally. Cannot trigger check."
            },
        )

    logger.info(
        f"Manual trigger for specific artist check received for ID: {artist_spotify_id}"
    )
    try:
        watched_artist = get_watched_artist(artist_spotify_id)
        if not watched_artist:
            logger.warning(
                f"Trigger specific check: Artist ID {artist_spotify_id} not found in watchlist."
            )
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"Artist {artist_spotify_id} is not in the watchlist. Add it first."
                },
            )

        thread = threading.Thread(
            target=check_watched_artists, args=(artist_spotify_id,)
        )
        thread.start()
        logger.info(
            f"Artist check triggered in background for specific artist ID: {artist_spotify_id}"
        )
        return {
            "message": f"Artist check triggered successfully in the background for {artist_spotify_id}."
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error manually triggering specific artist check for {artist_spotify_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": f"Could not trigger artist check for {artist_spotify_id}: {str(e)}"
            },
        )


@router.post("/watch/{artist_spotify_id}/albums")
async def mark_albums_as_known_for_artist(
    artist_spotify_id: str,
    request: Request,
    current_user: User = Depends(require_auth_from_state),
):
    """Fetches details for given album IDs and adds/updates them in the artist's local DB table."""
    watch_config = get_watch_config()
    if not watch_config.get("enabled", False):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "Watch feature is currently disabled globally. Cannot mark albums."
            },
        )

    logger.info(f"Attempting to mark albums as known for artist {artist_spotify_id}.")
    try:
        album_ids = await request.json()
        if not isinstance(album_ids, list) or not all(
            isinstance(aid, str) for aid in album_ids
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "Invalid request body. Expecting a JSON array of album Spotify IDs."
                },
            )

        if not get_watched_artist(artist_spotify_id):
            raise HTTPException(
                status_code=404,
                detail={"error": f"Artist {artist_spotify_id} is not being watched."},
            )

        fetched_albums_details = []
        for album_id in album_ids:
            try:
                # We need full album details. get_spotify_info with type "album" should provide this.
                album_detail = get_spotify_info(album_id, "album")
                if album_detail and album_detail.get("id"):
                    fetched_albums_details.append(album_detail)
                else:
                    logger.warning(
                        f"Could not fetch details for album {album_id} when marking as known for artist {artist_spotify_id}."
                    )
            except Exception as e:
                logger.error(
                    f"Failed to fetch Spotify details for album {album_id}: {e}"
                )

        if not fetched_albums_details:
            return {
                "message": "No valid album details could be fetched to mark as known.",
                "processed_count": 0,
            }

        processed_count = add_specific_albums_to_artist_table(
            artist_spotify_id, fetched_albums_details
        )
        logger.info(
            f"Successfully marked/updated {processed_count} albums as known for artist {artist_spotify_id}."
        )
        return {
            "message": f"Successfully processed {processed_count} albums for artist {artist_spotify_id}."
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error marking albums as known for artist {artist_spotify_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail={"error": f"Could not mark albums as known: {str(e)}"},
        )


@router.delete("/watch/{artist_spotify_id}/albums")
async def mark_albums_as_missing_locally_for_artist(
    artist_spotify_id: str,
    request: Request,
    current_user: User = Depends(require_auth_from_state),
):
    """Removes specified albums from the artist's local DB table."""
    watch_config = get_watch_config()
    if not watch_config.get("enabled", False):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "Watch feature is currently disabled globally. Cannot mark albums."
            },
        )

    logger.info(
        f"Attempting to mark albums as missing (delete locally) for artist {artist_spotify_id}."
    )
    try:
        album_ids = await request.json()
        if not isinstance(album_ids, list) or not all(
            isinstance(aid, str) for aid in album_ids
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "Invalid request body. Expecting a JSON array of album Spotify IDs."
                },
            )

        if not get_watched_artist(artist_spotify_id):
            raise HTTPException(
                status_code=404,
                detail={"error": f"Artist {artist_spotify_id} is not being watched."},
            )

        deleted_count = remove_specific_albums_from_artist_table(
            artist_spotify_id, album_ids
        )
        logger.info(
            f"Successfully removed {deleted_count} albums locally for artist {artist_spotify_id}."
        )
        return {
            "message": f"Successfully removed {deleted_count} albums locally for artist {artist_spotify_id}."
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error marking albums as missing (deleting locally) for artist {artist_spotify_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail={"error": f"Could not mark albums as missing: {str(e)}"},
        )
