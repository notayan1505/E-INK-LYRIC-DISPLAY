
import asyncio
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager
)

async def main():
    manager = await (
        GlobalSystemMediaTransportControlsSessionManager.request_async()
    )

    print("Windows media-session detector started.")
    print("Play a song in Apple Music if you haven't already.\n")

    while True:
        try:
            session = manager.get_current_session()

            if session is None:
                print("No active media session.")
            else:
                info = await session.try_get_media_properties_async()
                playback = session.get_playback_info()
                timeline = session.get_timeline_properties()

                print("-" * 45)
                print("App ID:  ", session.source_app_user_model_id)
                print("Title:   ", info.title)
                print("Artist:  ", info.artist)
                print("Album:   ", info.album_title)
                print("Status:  ", playback.playback_status)
                print("Position:", timeline.position)
                print("Duration:", timeline.end_time)

        except Exception as exc:
            print("Read error:", repr(exc))

        await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())
