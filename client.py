"""
client.py
---------
SMAP (Simple Music Adaptive Protocol) client.

Presents a simple text menu that lets the user list songs, play a song
(now with adaptive bitrate streaming), set a preferred bitrate, stop, and
quit. Prints every request and response so the SMAP protocol traffic is
fully visible.
"""

import json
import os
import socket
import time

import pygame

pygame.mixer.init()

from protocol import (
    AVAILABLE_BITRATES,
    SAFETY_FACTOR,
    THROUGHPUT_HISTORY_SIZE,
    BufferedSocket,
    ConnectionClosedError,
    ProtocolError,
    calculate_throughput_kbps,
    receive_exact,
    receive_response_header,
    select_bitrate,
    send_request,
)

# Client Configuration
HOST = "127.0.0.1"
PORT = 5000
DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")

# PER-CLIENT PLAYBACK DIRECTORY
# Each running client.py process gets its own subfolder under downloads/,
# named after this process's own OS process ID. os.getpid() is already
# guaranteed unique among every process currently running on this machine
# -- no server round-trip, no protocol change, and no extra coordination
# file/lock is needed to hand out an ID. This is what stops two client.py
# instances on the same Windows PC from ever opening/truncating the same
# current_song.mp3 at the same time (the root cause of the
# "[Errno 13] Permission denied" crash).
CLIENT_ID = os.getpid()
CLIENT_DOWNLOAD_DIR = os.path.join(DOWNLOADS_DIR, f"client_{CLIENT_ID}")

# The ONE playback file reused for every playback session, in every run --
# now scoped to THIS client's own subfolder rather than the shared
# downloads/ root. Never create song_1.mp3 / song_123456.mp3 /
# chunk_001_320.mp3 / etc., and never create more than one subfolder per
# running client process.
CURRENT_SONG_FILENAME = "current_song.mp3"
CURRENT_SONG_PATH = os.path.join(CLIENT_DOWNLOAD_DIR, CURRENT_SONG_FILENAME)

# Client-side preferred bitrate used directly by the adaptive algorithm.
# Updated by cmd_bitrate(). Starts at the top of the ladder ("prefer best
# quality, let the network decide how close we can get").
PREFERRED_BITRATE = max(AVAILABLE_BITRATES)

# Playback status (console-only, for demo visibility)
# Lifecycle: IDLE -> PLAYING -> FINISHED -> IDLE, or PLAYING -> STOPPED -> ...
PLAYBACK_STATUS = "IDLE"
CURRENT_SONG_NAME = None  # display name of the song currently loaded/playing

# Cache of song_id -> song name, populated by "List Songs" so status
# messages can show a real name instead of just the numeric ID.
SONG_NAMES = {}



# Console status printing
def print_status_playing(song_name, song_id, segment, total, bitrate):
    print("\n[PLAYBACK]")
    print(f"Now Playing: {song_name} (ID:{song_id})")
    print("Status: PLAYING")
    print(f"Segment: {segment}/{total}" if total else f"Segment: {segment}")
    print(f"Bitrate: {bitrate} kbps")

def print_status_finished(song_name, song_id):
    print("\n[PLAYBACK]")
    print(f"Song: {song_name} (ID:{song_id})")
    print("Status: FINISHED")

def print_status_stopped(previous_song=None, previous_song_id=None):
    print("\n[PLAYBACK]")
    print("Status: STOPPED")
    if previous_song:
        print(f"Previous Song: {previous_song} (ID:{previous_song_id})")

def print_response_header(status_code, status_phrase, headers, raw_text):
    print("[RECEIVE RESPONSE]")
    print(raw_text.strip())


# Command implementations
def cmd_list(sock, buffered):
    line = send_request(sock, "LIST")
    print("\n[SEND REQUEST]")
    print(line)

    status_code, status_phrase, headers, raw_text = receive_response_header(buffered)
    print_response_header(status_code, status_phrase, headers, raw_text)

    if status_code != 200:
        print(f"[STATUS] Server returned {status_code} {status_phrase}")
        return

    try:
        content_length = int(headers["Content-Length"])
    except (KeyError, ValueError):
        print("[ERROR] Missing or invalid Content-Length header for LIST body")
        return

    body_bytes = receive_exact(buffered, content_length)

    try:
        songs = json.loads(body_bytes.decode("utf-8"))
    except json.JSONDecodeError:
        print("[ERROR] Could not parse song list JSON")
        return

    print("\n========== AVAILABLE SONGS ==========")
    for song in songs:
        print(f"  ID {song['id']:<3} {song['name']:<15} bitrates: {song['bitrates']}")
        SONG_NAMES[song["id"]] = song["name"]  # for nicer [PLAYBACK] status messages
    print("======================================")

def cmd_play(sock, buffered):
    """
    Adaptive streaming: requests the song one segment at a time, choosing
    a bitrate before each request based on measured throughput and the
    user's preferred bitrate. Assembles all segments into the single
    reusable downloads/current_song.mp3 file, then plays it.

    Works identically whether this is the first playback, a replay of the
    same song, or a switch to a different song: the previous playback (if
    any) is stopped, current_song.mp3 is truncated/overwritten, and the
    ABR state (throughput history, bitrate) starts fresh.
    """
    global PREFERRED_BITRATE, PLAYBACK_STATUS, CURRENT_SONG_NAME, CURRENT_SONG_ID

    try:
        song_id = int(input("Enter Song ID: ").strip())
    except ValueError:
        print("[ERROR] Song ID must be a number")
        return

    song_name = SONG_NAMES.get(song_id, f"Song {song_id}")
    CURRENT_SONG_ID = song_id

    # If something is already playing, stop it and report the transition
    # before starting the new one.
    if PLAYBACK_STATUS == "PLAYING":
        previous_song = CURRENT_SONG_NAME
        previous_song_id = CURRENT_SONG_ID
        pygame.mixer.music.stop()
        print_status_stopped(previous_song=previous_song, previous_song_id=previous_song_id)

    print(f"[INFO] Streaming song {song_id} adaptively (preferred bitrate cap: {PREFERRED_BITRATE} kbps)")

    # PER-CLIENT PLAYBACK DIRECTORY: create THIS client's own subfolder
    # (downloads/client_<pid>/), never the shared downloads/ root directly,
    # so this process never touches another running client's files.
    os.makedirs(CLIENT_DOWNLOAD_DIR, exist_ok=True)
    out_path = CURRENT_SONG_PATH

    # Make sure nothing still holds a lock on current_song.mp3 (e.g. from a
    # song that just finished playing) before we overwrite it.
    pygame.mixer.music.stop()
    try:
        pygame.mixer.music.unload()
    except AttributeError:
        pass  # older pygame versions don't have unload(); stop() is enough

    PLAYBACK_STATUS = "PLAYING"
    CURRENT_SONG_NAME = song_name

    throughput_history = []
    # Bootstrap: no measurement yet for the first segment, so start with
    # the lowest bitrate the user's preference allows -- a conservative,
    # safe first guess.
    allowed = [b for b in AVAILABLE_BITRATES if b <= PREFERRED_BITRATE]
    current_bitrate = min(allowed) if allowed else min(AVAILABLE_BITRATES)

    segment_number = 0
    total_bytes = 0
    total_segments = None

    try:
        # "wb" truncates any previous current_song.mp3 -- this IS the
        # "delete/reset the old file before a fresh playback" step.
        with open(out_path, "wb") as f:
            while True:
                used_bitrate = current_bitrate
                line = send_request(sock, "GET_SEGMENT", song_id, segment_number, used_bitrate)
                print("\n[SEND REQUEST]")
                print(line)

                status_code, status_phrase, headers, raw_text = receive_response_header(buffered)
                print_response_header(status_code, status_phrase, headers, raw_text)

                if status_code == 404:
                    print(f"[STATUS] {headers.get('Message', 'No more segments')} -- stopping")
                    break
                if status_code != 200:
                    print(f"[STATUS] Server returned {status_code} {status_phrase} -- stopping playback")
                    PLAYBACK_STATUS = "IDLE"
                    return

                try:
                    segment_length = int(headers["Content-Length"])
                except (KeyError, ValueError):
                    print("[ERROR] Missing or invalid Content-Length header")
                    PLAYBACK_STATUS = "IDLE"
                    return

                try:
                    total_segments = int(headers["Total-Segments"])
                except (KeyError, ValueError):
                    total_segments = None

                start_time = time.perf_counter()
                segment_data = receive_exact(buffered, segment_length)
                elapsed = time.perf_counter() - start_time

                f.write(segment_data)
                total_bytes += len(segment_data)

                throughput_kbps = calculate_throughput_kbps(len(segment_data), elapsed)
                print("\n[NETWORK MEASUREMENT]")
                print(f"Download Time: {elapsed:.2f} seconds")
                print(f"Segment Size: {len(segment_data)} bytes")
                print(f"Throughput: {throughput_kbps:.0f} kbps")

                print_status_playing(song_name, song_id, segment_number + 1, total_segments, used_bitrate)

                throughput_history.append(throughput_kbps)
                if len(throughput_history) > THROUGHPUT_HISTORY_SIZE:
                    throughput_history.pop(0)
                estimated_throughput = sum(throughput_history) / len(throughput_history)
                safe_throughput = estimated_throughput * SAFETY_FACTOR

                next_bitrate, reason = select_bitrate(PREFERRED_BITRATE, safe_throughput)

                print("\n[ADAPTATION]")
                print(f"Preferred Bitrate: {PREFERRED_BITRATE} kbps")
                print(f"Segment Throughput: {throughput_kbps:.0f} kbps")
                print(f"Estimated Throughput: {estimated_throughput:.0f} kbps")
                print(f"Safety Factor: {SAFETY_FACTOR}")
                print(f"Safe Throughput: {safe_throughput:.0f} kbps")
                print(f"Selected Bitrate: {next_bitrate} kbps")
                print(f"Reason: {reason}")

                current_bitrate = next_bitrate
                segment_number += 1
    except ConnectionClosedError:
        print(f"[ERROR] Connection closed after {total_bytes} bytes -- incomplete transfer")
        PLAYBACK_STATUS = "IDLE"
        return

    if total_bytes > 0:
        print(
            f"\n[SUCCESS] Adaptive stream saved to {os.path.relpath(out_path)} "
            f"({total_bytes} bytes across {segment_number} segment(s))"
        )
        PLAYBACK_STATUS = "FINISHED"
        print_status_finished(song_name, song_id)
        print("[STATUS] Playing audio right now!")
        pygame.mixer.music.load(out_path)
        pygame.mixer.music.play()
    else:
        print("[ERROR] No audio data received")
        PLAYBACK_STATUS = "IDLE"

def cmd_bitrate(sock, buffered):
    """
    Sets the preferred bitrate both on the server (unchanged, informational)
    and locally
    """
    global PREFERRED_BITRATE

    try:
        bitrate = int(input("Enter preferred bitrate (64/128/192/320): ").strip())
    except ValueError:
        print("[ERROR] Bitrate must be a number")
        return

    line = send_request(sock, "BITRATE", bitrate)
    print("\n[SEND REQUEST]")
    print(line)

    status_code, status_phrase, headers, raw_text = receive_response_header(buffered)
    print_response_header(status_code, status_phrase, headers, raw_text)

    if status_code == 200:
        PREFERRED_BITRATE = bitrate
        print(f"[INFO] Preferred bitrate for adaptive streaming set to {PREFERRED_BITRATE} kbps")

def cmd_network(sock, buffered):
    """
    # MULTI-CLIENT SUPPORT
    Sends NETWORK <profile> so THIS client's connection is pinned to a
    specific simulated bandwidth cap on the server, independent of every
    other connected client.py process and independent of the server
    operator's own GOOD/NORMAL/POOR console setting. This is what lets
    several client.py processes demo different network conditions at the
    same time (e.g. this one on POOR while another is on GOOD).

    Entering AUTO clears this client's override so it goes back to
    following the server's shared/live-console default profile, exactly
    like the original single-client behavior.
    """
    choice = input("Enter network profile (GOOD/NORMAL/POOR/AUTO): ").strip()
    if not choice:
        print("[ERROR] Network profile cannot be empty")
        return

    line = send_request(sock, "NETWORK", choice)
    print("\n[SEND REQUEST]")
    print(line)

    status_code, status_phrase, headers, raw_text = receive_response_header(buffered)
    print_response_header(status_code, status_phrase, headers, raw_text)

    if status_code == 200:
        if headers.get("Network-Profile") == "AUTO":
            print(f"[INFO] Network simulation override cleared -- following server default ({headers.get('Effective-Profile', '?')})")
        else:
            print(f"[INFO] This client is now pinned to the {headers.get('Network-Profile', choice.upper())} network profile")
    else:
        print(f"[STATUS] Server returned {status_code} {status_phrase}")

def cmd_stop(sock, buffered):
    global PLAYBACK_STATUS
    
    # If nothing is playing report it and return early
    if not pygame.mixer.music.get_busy() or PLAYBACK_STATUS == "PLAYING":
        print("[INFO] No song is currently playing")
        return

    line = send_request(sock, "STOP")
    print("\n[SEND REQUEST]")
    print(line)

    status_code, status_phrase, headers, raw_text = receive_response_header(buffered)
    print_response_header(status_code, status_phrase, headers, raw_text)
    print("[STATUS] Stopping audio playback.")
    pygame.mixer.music.stop()

    print_status_stopped(previous_song=CURRENT_SONG_NAME, previous_song_id=CURRENT_SONG_ID)
    PLAYBACK_STATUS = "IDLE"

def cmd_quit(sock, buffered):
    global PLAYBACK_STATUS

    line = send_request(sock, "QUIT")
    print("\n[SEND REQUEST]")
    print(line)

    status_code, status_phrase, headers, raw_text = receive_response_header(buffered)
    print_response_header(status_code, status_phrase, headers, raw_text)

    if PLAYBACK_STATUS == "PLAYING":
        pygame.mixer.music.stop()
        print_status_stopped(previous_song=CURRENT_SONG_NAME, previous_song_id=CURRENT_SONG_ID)
        PLAYBACK_STATUS = "IDLE"

# Menu
MENU = """
========== MiniMP3Stream ==========
1. List Songs
2. Play Song
3. Stop
4. Set Preferred Bitrate
5. Set Network Simulation Profile (For Multi-Client Demo)
6. Quit
===================================
"""

def main():
    print("========== CLIENT ==========")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((HOST, PORT))
    except ConnectionRefusedError:
        print(f"[ERROR] Could not connect to {HOST}:{PORT} -- is the server running?")
        return
    except OSError as e:
        print(f"[ERROR] Connection failed: {e}")
        return

    print(f"[CONNECTED] Connected to server {HOST}:{PORT}")
    # PER-CLIENT PLAYBACK DIRECTORY: makes it obvious, especially when
    # running several client.py processes side by side on the same
    # machine, which downloads/client_<pid>/ folder belongs to this one.
    print(f"[INFO] This client's local playback directory: {os.path.relpath(CLIENT_DOWNLOAD_DIR)}")
    buffered = BufferedSocket(sock)

    try:
        while True:
            print(MENU)
            choice = input("Select an option: ").strip()

            try:
                if choice == "1":
                    cmd_list(sock, buffered)
                elif choice == "2":
                    cmd_play(sock, buffered)
                elif choice == "3":
                    cmd_stop(sock, buffered)
                elif choice == "4":
                    cmd_bitrate(sock, buffered)
                elif choice == "5":
                    cmd_network(sock, buffered)
                elif choice == "6":
                    cmd_quit(sock, buffered)
                    break
                else:
                    print("[ERROR] Invalid menu option, try again")
            except ConnectionClosedError:
                print("[ERROR] Server closed the connection unexpectedly")
                break
            except ProtocolError as e:
                print(f"[ERROR] Protocol error: {e}")
                break
            except OSError as e:
                print(f"[ERROR] Network error: {e}")
                break
    finally:
        sock.close()
        print("[CLOSED] Connection closed. Goodbye!")


if __name__ == "__main__":
    main()
