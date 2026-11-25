"""Diagnostics: what GSMTC and Core Audio actually expose on this machine.

Run:  .venv\\Scripts\\python.exe probe.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from importlib.metadata import version

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
)
from winrt.windows.storage.streams import DataReader, InputStreamOptions

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from smtc.model import PlaybackStatus, app_label_for  # noqa: E402


def pkg(name: str) -> str:
    try:
        return version(name)
    except Exception:
        return "?"


def buf_to_bytes(buffer) -> bytes | None:
    """Try every reasonable way to get raw bytes out of an IBuffer."""
    try:
        return bytes(memoryview(buffer))
    except Exception:
        pass
    try:
        return bytes(buffer)
    except Exception:
        pass
    try:
        size = int(buffer.length)
        reader = DataReader.from_buffer(buffer)
        out = bytearray(size)
        reader.read_bytes(out)
        return bytes(out)
    except Exception as exc:
        print("    buffer conversion failed:", exc)
    return None


async def read_thumbnail(ref) -> bytes | None:
    stream = await ref.open_read_async()
    if stream is None:
        return None
    size = int(stream.size)
    print(f"    stream size: {size} bytes")
    if size <= 0:
        return None
    reader = DataReader(stream)
    reader.input_stream_options = InputStreamOptions.PARTIAL
    loaded = await reader.load_async(size)
    print(f"    loaded={int(getattr(loaded, 'length', 0))} unconsumed={reader.unconsumed_buffer_length}")
    count = int(reader.unconsumed_buffer_length) or size
    buf = reader.read_buffer(count)
    data = buf_to_bytes(buf)
    reader.close()
    stream.close()
    return data


def dump_audio() -> None:
    print("\n[2] Core Audio (pycaw):")
    try:
        from pycaw.pycaw import AudioUtilities

        sessions = AudioUtilities.GetAllSessions()
        print(f"  audio sessions: {len(sessions)}")
        for a in sessions:
            try:
                name = a.Process.name() if a.Process else "?"
            except Exception:
                name = "?"
            try:
                vol = a.SimpleAudioVolume
                pct = int(round(vol.GetMasterVolume() * 100))
                print(f"   pid={a.ProcessId:<7} {name:<26} state={a.State} vol={pct:>3}% mute={bool(vol.GetMute())}")
            except Exception as exc:
                print(f"   pid={a.ProcessId} {name} -> error: {exc}")
        dev = AudioUtilities.GetSpeakers()
        ev = dev.EndpointVolume
        print(
            f"  endpoint: {dev.FriendlyName!r} "
            f"{int(round(ev.GetMasterVolumeLevelScalar() * 100))}% mute={bool(ev.GetMute())}"
        )
    except Exception as exc:
        print("  pycaw ERROR:", type(exc).__name__, exc)


async def main() -> int:
    print("Python:", sys.version.split()[0])
    print(
        "winrt-runtime:",
        pkg("winrt-runtime"),
        "| media.control:",
        pkg("winrt-Windows.Media.Control"),
        "| PySide6:",
        pkg("PySide6"),
        "| pycaw:",
        pkg("pycaw"),
        "| comtypes:",
        pkg("comtypes"),
    )

    print("\n[1] Requesting the session manager...")
    try:
        manager = await SessionManager.request_async()
    except OSError as exc:
        hr = getattr(exc, "winerror", None)
        print("  request_async ERROR:", exc, f"(hr=0x{hr & 0xFFFFFFFF:08X})" if hr else "")
        return 1
    print("  manager:", "ok" if manager else "None")

    sessions = list(manager.get_sessions())
    current = manager.get_current_session()
    print(f"  sessions: {len(sessions)}")

    for idx, s in enumerate(sessions):
        aumid = s.source_app_user_model_id or ""
        print(f"\n  --- [{idx}] aumid={aumid!r} app={app_label_for(aumid)!r} current={s is current}")
        try:
            info = s.get_playback_info()
        except Exception as exc:
            print("      playback_info ERROR:", exc)
            info = None
        if info is not None:
            status = int(info.playback_status)
            print(f"      status   : {status} ({PlaybackStatus(status).name})")
            c = info.controls
            print(
                "      controls : play={} pause={} toggle={} next={} prev={} stop={} seek={} shuffle={} repeat={}".format(
                    c.is_play_enabled, c.is_pause_enabled, c.is_play_pause_toggle_enabled,
                    c.is_next_enabled, c.is_previous_enabled, c.is_stop_enabled,
                    c.is_playback_position_enabled, c.is_shuffle_enabled, c.is_repeat_enabled,
                )
            )
            print("      repeat   :", info.auto_repeat_mode, "| shuffle:", info.is_shuffle_active)
            print("      rate     :", info.playback_rate, "| type:", info.playback_type)

        try:
            props = await asyncio.wait_for(s.try_get_media_properties_async(), 2.0)
        except Exception as exc:
            print("      props ERROR:", type(exc).__name__, exc)
            props = None
        if props is not None:
            print(f"      title    : {props.title!r}")
            print(f"      artist   : {props.artist!r}")
            print(f"      album    : {props.album_title!r}")
            print(f"      thumbnail: {'none' if props.thumbnail is None else type(props.thumbnail).__name__}")
            if props.thumbnail is not None:
                try:
                    data = await asyncio.wait_for(read_thumbnail(props.thumbnail), 3.0)
                    if data:
                        png = data[:8] == bytes([137, 80, 78, 71, 13, 10, 26, 10])
                        print(f"      thumb    : {len(data)} bytes, png={png}, head={data[:8].hex()}")
                    else:
                        print("      thumb    : <empty>")
                except Exception as exc:
                    print("      thumb ERROR:", type(exc).__name__, exc)

    dump_audio()

    print("\n[3] Threading:")
    import threading

    print("  thread:", threading.current_thread().name)
    print("  WinRT apartment initialized OK in this thread (no RPC_E_CHANGED_MODE)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
