"""
server.py
---------
SMAP (Simple Music Adaptive Protocol) server.

Listens for TCP connections, serves a catalog of songs, and streams MP3
files at the requested bitrate. Uses a simple thread-per-client model so
multiple clients can connect at once without blocking each other.
"""

import argparse
import math
import os
import socket
import threading
import time

from protocol import (
    AVAILABLE_BITRATES,
    CHUNK_SIZE,
    SEGMENT_SIZE_BYTES,
    BufferedSocket,
    ConnectionClosedError,
    ProtocolError,
    receive_request,
    send_response_header,
    send_all,
    to_json_bytes,
)

# Configuration -- easy to change
HOST = "0.0.0.0"
PORT = 5000
SONGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "songs")
# Reusable, pre-generated segment files live here -- see ensure_segments_exist().
MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")
SUPPORTED_BITRATES = AVAILABLE_BITRATES  # kept for backward compatibility with the rest of this file

# Smaller send granularity used ONLY for GET_SEGMENT's rate-limited streaming
# (legacy PLAY keeps using protocol.CHUNK_SIZE, untouched, at full speed).
# A finer granularity makes time.sleep()-based throttling track the target
# kbps much more closely: with big 4096B chunks over a fast loopback
# connection, the OS can buffer an entire chunk "in flight" instantly, so a
# coarse throttle ends up measuring roughly double the intended rate. Pacing
# in smaller pieces spreads the required sleeping out much more evenly.
STREAM_CHUNK_BYTES = 1024


# MULTI-CLIENT SUPPORT: client-tagged console logging
# A single lock shared by every client thread. Each call to log_lines()
# prints its whole block of lines while holding the lock, so blocks from
# different clients' threads never get chopped up and interleaved with each
# other on the shared terminal -- purely a readability fix for the
# multi-client console output, no protocol/behavior change.
_print_lock = threading.Lock()

def client_tag(addr) -> str:
    """[CLIENT host:port] identifier used to distinguish clients in logs."""
    return f"[CLIENT {addr[0]}:{addr[1]}]"

def log_lines(addr, *blocks):
    """
    Prints one or more text blocks, each line prefixed with the requesting
    client's [CLIENT host:port] tag, atomically (the whole call happens
    under one lock acquisition) so concurrent clients' output doesn't
    interleave mid-block.
    """
    tag = client_tag(addr)
    with _print_lock:
        for block in blocks:
            for line in str(block).split("\n"):
                print(f"{tag} {line}")


# Network simulation profiles for DEMO
# Value = bandwidth cap in kbps. None means "no artificial cap" (GOOD).
NETWORK_PROFILES = {
    "GOOD": None,
    "NORMAL": 500,
    "POOR": 150,
}

_profile_lock = threading.Lock()
# Default/global starting profile -- unchanged behavior: applies to any
# client that has not selected its own profile with NETWORK (see below).
_current_profile_name = "GOOD"

def set_network_profile(name: str) -> bool:
    """Thread-safe update of the GLOBAL default network profile. Returns True on success."""
    global _current_profile_name
    name = name.upper()
    if name not in NETWORK_PROFILES:
        return False
    with _profile_lock:
        _current_profile_name = name
    return True

def get_network_profile():
    """Thread-safe read of the GLOBAL default profile. Returns (profile_name, bandwidth_limit_kbps_or_None)."""
    with _profile_lock:
        name = _current_profile_name
    return name, NETWORK_PROFILES[name]

def get_effective_network_profile(state):
    """
    # MULTI-CLIENT SUPPORT / PER-CLIENT STATE
    Returns the (profile_name, bandwidth_limit_kbps_or_None) that actually
    applies to THIS client's connection:

      - If this client has pinned its own profile via NETWORK <PROFILE>,
        that per-client choice (stored in its own 'state' dict -- never a
        global variable) always wins, regardless of what any other client
        is doing or what the global/live-console profile currently is.
      - Otherwise, it falls back to the shared global default profile,
        exactly like the original single-client behavior.

    This is what lets Client 1 run under GOOD while Client 2 runs under
    POOR and Client 3 under NORMAL, all at the same time (see README,
    Section 11 / demo scenario).
    """
    override = state.get("network_profile")
    if override is not None:
        return override, NETWORK_PROFILES[override]
    return get_network_profile()

def format_limit(limit_kbps):
    return "unlimited" if limit_kbps is None else f"{limit_kbps} kbps"

def apply_rate_limit(bytes_sent_so_far: int, segment_start_time: float, bandwidth_limit_kbps):
    """
    Sleeps just long enough that, since 'segment_start_time', we have not
    sent data faster than 'bandwidth_limit_kbps'. No-op when the limit is
    None (GOOD / unlimited profile).

    This is what makes the network simulation "real" rather than cosmetic:
    the actual wall-clock time taken to send a segment is affected, so the
    client's own throughput measurement (based on real elapsed time) will
    genuinely reflect the simulated bandwidth cap.
    """
    if not bandwidth_limit_kbps:
        return
    bytes_per_second_limit = (bandwidth_limit_kbps * 1000) / 8
    expected_elapsed = bytes_sent_so_far / bytes_per_second_limit
    actual_elapsed = time.perf_counter() - segment_start_time
    if expected_elapsed > actual_elapsed:
        time.sleep(expected_elapsed - actual_elapsed)

def network_console_loop():
    """
    Background thread: lets whoever is running the server type GOOD / NORMAL
    / POOR (and press Enter) at any time to change the simulated GLOBAL
    default network profile live, without restarting the server. This still
    only affects clients that have NOT pinned their own profile via NETWORK
    (see get_effective_network_profile()). This is what makes DEMO 4
    (network conditions changing mid-demo) easy to show.
    """
    with _print_lock:
        print("[NETWORK CONSOLE] Type GOOD, NORMAL, or POOR to change the GLOBAL default network profile.")
        print("[NETWORK CONSOLE] (Only affects clients that haven't set their own profile with the NETWORK command.)")
    while True:
        try:
            line = input().strip()
        except EOFError:
            break
        if not line:
            continue
        if set_network_profile(line):
            name, limit = get_network_profile()
            with _print_lock:
                print(f"[NETWORK SIMULATION] Global default profile changed to {name} ({format_limit(limit)})")
        else:
            with _print_lock:
                print(f"[NETWORK CONSOLE] Unknown profile '{line}'. Valid options: {', '.join(NETWORK_PROFILES)}")

# Song catalog
# Built once at startup by scanning the songs/ folder. Each subfolder is a
# song; files inside must be named "<songname>_<bitrate>.mp3".
# catalog example:
# {
#   1: {
#       "name": "song1",
#       "bitrates": {64: "songs/song1/song1_64.mp3", ...},   # original per-bitrate files
#       "segment_dirs": {64: "media/song1/64", ...},          # reusable pre-sliced segments
#       "segment_counts": {64: 12, ...},                      # how many segments exist
#   },
#   2: {...},
# }
# THREAD SAFETY: 'catalog' is built once, synchronously, in build_catalog()
# before the server starts accept()-ing connections (see main()). After
# that point every client thread only ever READS it -- no client thread
# mutates 'catalog', and no new segments are generated at request time (see
# ensure_segments_exist() below), so this shared dict needs no lock: there
# is no concurrent writer to race against.
catalog = {}

def ensure_segments_exist(song_name: str, bitrate: int, source_path: str):
    """
    Makes sure media/<song_name>/<bitrate>/segment_0001.mp3, segment_0002.mp3,
    ... exist on disk, slicing them out of 'source_path' (the full per-bitrate
    MP3) exactly once. If the expected number of segment files is already
    present, nothing is (re)generated -- they are simply reused.

    Returns (segment_dir, num_segments).

    Segment numbering: file segment_0001.mp3 corresponds to protocol
    segment_number 0 (GET_SEGMENT's segment_number is 0-based; filenames are
    1-based for readability on disk).

    THREAD-SAFE SEGMENT GENERATION: this function is only ever called from
    build_catalog(), which itself only ever runs once, synchronously, in
    main() BEFORE the accept() loop (and therefore before any client thread
    exists). So there is no scenario in this design where two client
    threads could call this concurrently for the same (song, bitrate) and
    race to generate the same files -- generation happens exactly once,
    up front, and every later GET_SEGMENT (from any number of simultaneous
    clients) only ever performs a read-only os.path.isfile()/open() against
    files that already exist. No additional lock is needed for this reason;
    if a future version moved segment generation to be lazy/on-demand at
    request time, a threading.Lock() keyed by (song_id, bitrate) would be
    the place to add it, to make sure only one thread generates a given
    segment set while any others wait and then reuse the result.
    """
    segment_dir = os.path.join(MEDIA_DIR, song_name, str(bitrate))
    file_size = os.path.getsize(source_path)
    num_segments = max(1, math.ceil(file_size / SEGMENT_SIZE_BYTES))

    if os.path.isdir(segment_dir):
        existing = [f for f in os.listdir(segment_dir) if f.startswith("segment_")]
        if len(existing) == num_segments:
            print(f"    {bitrate} kbps: already exists ({num_segments} segment(s))")
            return segment_dir, num_segments

    print(f"    {bitrate} kbps: generating {num_segments} segment(s)...")
    os.makedirs(segment_dir, exist_ok=True)
    with open(source_path, "rb") as src:
        for i in range(num_segments):
            src.seek(i * SEGMENT_SIZE_BYTES)
            chunk = src.read(SEGMENT_SIZE_BYTES)
            seg_path = os.path.join(segment_dir, f"segment_{i + 1:04d}.mp3")
            with open(seg_path, "wb") as out:
                out.write(chunk)
    return segment_dir, num_segments

def build_catalog():
    catalog.clear()
    if not os.path.isdir(SONGS_DIR):
        print(f"[WARN] Songs directory not found: {SONGS_DIR}")
        return

    song_id = 1
    for folder_name in sorted(os.listdir(SONGS_DIR)):
        folder_path = os.path.join(SONGS_DIR, folder_name)
        if not os.path.isdir(folder_path):
            continue

        bitrates = {}
        for bitrate in SUPPORTED_BITRATES:
            expected_file = f"{folder_name}_{bitrate}.mp3"
            file_path = os.path.join(folder_path, expected_file)
            if os.path.isfile(file_path):
                bitrates[bitrate] = file_path

        if bitrates:
            catalog[song_id] = {
                "name": folder_name,
                "bitrates": bitrates,
                "segment_dirs": {},
                "segment_counts": {},
            }
            song_id += 1

    print(f"[CATALOG] Loaded {len(catalog)} song(s) from {SONGS_DIR}")
    for sid, info in catalog.items():
        print(f"    id={sid} name={info['name']} bitrates={sorted(info['bitrates'].keys())}")

    print("\n[MEDIA] Checking segments...")
    for info in catalog.values():
        print(f"Song: {info['name']}")
        for bitrate, source_path in sorted(info["bitrates"].items()):
            segment_dir, num_segments = ensure_segments_exist(info["name"], bitrate, source_path)
            info["segment_dirs"][bitrate] = segment_dir
            info["segment_counts"][bitrate] = num_segments


# Command handlers
def handle_list(sock, addr):
    """LIST -> 200 OK with a JSON array of songs and their bitrates."""
    songs = [
        {"id": sid, "name": info["name"], "bitrates": sorted(info["bitrates"].keys())}
        for sid, info in catalog.items()
    ]
    body = to_json_bytes(songs)
    headers = {
        "Content-Type": "application/json",
        "Count": len(songs),
        # Content-Length lets the client know exactly how many body bytes
        # to read, the same framing technique used for the MP3 stream in
        # PLAY / GET_SEGMENT. Without it the client would have no reliable
        # way to know where the JSON body ends within the TCP byte stream.
        "Content-Length": len(body),
    }
    header_text = send_response_header(sock, 200, headers)
    send_all(sock, body)
    log_lines(addr, "[SEND RESPONSE]", header_text.strip(), body.decode("utf-8"))

def handle_play(sock, addr, params, state):
    """
    PLAY <song_id> <bitrate> -> streams the ENTIRE MP3 file at a single,
    fixed bitrate. This is the original, legacy whole-file command; it is
    left unchanged and still works exactly as before. The client's menu now
    uses GET_SEGMENT for adaptive streaming instead, but PLAY remains
    available (e.g. for testing or non-adaptive use).
    """
    if len(params) != 2:
        header_text = send_response_header(sock, 400, {"Message": "Usage: PLAY <song_id> <bitrate>"})
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    try:
        song_id = int(params[0])
        bitrate = int(params[1])
    except ValueError:
        header_text = send_response_header(sock, 400, {"Message": "song_id and bitrate must be integers"})
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    # 1. Does the song exist?
    song = catalog.get(song_id)
    if song is None:
        header_text = send_response_header(sock, 404, {"Message": f"Song {song_id} not found"})
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    # 2. Is the bitrate supported for this song?
    file_path = song["bitrates"].get(bitrate)
    if bitrate not in SUPPORTED_BITRATES or file_path is None:
        header_text = send_response_header(
            sock, 406, {"Message": f"Bitrate {bitrate} not available for song {song_id}"}
        )
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    # 3. Does the MP3 file actually exist on disk?
    if not os.path.isfile(file_path):
        header_text = send_response_header(sock, 500, {"Message": "Song file missing on server"})
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    file_size = os.path.getsize(file_path)
    headers = {
        "Content-Type": "audio/mpeg",
        "Song-ID": song_id,
        "Bitrate": bitrate,
        "Content-Length": file_size,
        "Chunk-Size": CHUNK_SIZE,
    }
    header_text = send_response_header(sock, 200, headers)
    log_lines(addr, "[SEND RESPONSE]", header_text.strip())

    state["streaming"] = True

    log_lines(addr, "[STATUS] Sending MP3 chunks...")
    sent = 0
    last_reported = -1
    with open(file_path, "rb") as f:
        while sent < file_size:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            send_all(sock, chunk)
            sent += len(chunk)
            percent = int(sent * 100 / file_size)
            milestone = (percent // 25) * 25
            if milestone != last_reported and milestone > 0:
                log_lines(addr, f"[PROGRESS] {milestone}%")
                last_reported = milestone

    state["streaming"] = False
    log_lines(addr, f"[SUCCESS] Sent {sent} bytes for {song['name']} @ {bitrate}kbps")

def handle_get_segment(sock, addr, params, state):
    """
    GET_SEGMENT <song_id> <segment_number> <bitrate>

    Streams ONE pre-generated segment file:
        media/<song_name>/<bitrate>/segment_<segment_number + 1>.mp3
    (see ensure_segments_exist() -- these files are created once at server
    startup and reused for every subsequent request; nothing is sliced or
    regenerated per-request here).

    If the requested segment number is past the last segment that exists
    for this bitrate, there is nothing left to stream -- we respond 404
    with a "no more segments" message, which the client treats as a normal
    end-of-stream signal (not necessarily an error).

    # PER-CLIENT STATE: the network profile applied while sending this
    # segment's bytes is THIS client's effective profile (its own NETWORK
    # override if it set one, otherwise the shared global/live-console
    # default) -- see get_effective_network_profile(). Two clients
    # streaming at the same time can therefore be throttled completely
    # differently.
    """
    if len(params) != 3:
        header_text = send_response_header(
            sock, 400, {"Message": "Usage: GET_SEGMENT <song_id> <segment_number> <bitrate>"}
        )
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    try:
        song_id = int(params[0])
        segment_number = int(params[1])
        bitrate = int(params[2])
    except ValueError:
        header_text = send_response_header(sock, 400, {"Message": "song_id, segment_number, bitrate must be integers"})
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    if segment_number < 0:
        header_text = send_response_header(sock, 400, {"Message": "segment_number must be >= 0"})
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    # 1. Does the song exist?
    song = catalog.get(song_id)
    if song is None:
        header_text = send_response_header(sock, 404, {"Message": f"Song {song_id} not found"})
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    # 2. Is the bitrate supported for this song?
    segment_dir = song["segment_dirs"].get(bitrate)
    if bitrate not in SUPPORTED_BITRATES or segment_dir is None:
        header_text = send_response_header(
            sock, 406, {"Message": f"Bitrate {bitrate} not available for song {song_id}"}
        )
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    total_segments = song["segment_counts"].get(bitrate, 0)
    # segment_number is 0-based on the wire; segment files are 1-based on disk.
    segment_path = os.path.join(segment_dir, f"segment_{segment_number + 1:04d}.mp3")

    # End of stream for this bitrate: nothing left to send.
    if segment_number >= total_segments or not os.path.isfile(segment_path):
        header_text = send_response_header(
            sock,
            404,
            {
                "Song-ID": song_id,
                "Segment-ID": segment_number,
                "Bitrate": bitrate,
                "Message": "No more segments (end of stream)",
            },
        )
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    segment_length = os.path.getsize(segment_path)

    # PER-CLIENT STATE: this client's own effective profile, not a single
    # shared global value -- see get_effective_network_profile().
    profile_name, bandwidth_limit = get_effective_network_profile(state)
    source_tag = "per-client override" if state.get("network_profile") else "global default"
    log_lines(
        addr,
        "[NETWORK SIMULATION]",
        f"Profile: {profile_name} ({source_tag})",
        f"Bandwidth Limit: {format_limit(bandwidth_limit)}",
    )

    headers = {
        "Content-Type": "audio/mpeg",
        "Song-ID": song_id,
        "Segment-ID": segment_number,
        "Bitrate": bitrate,
        "Total-Segments": total_segments,
        "Content-Length": segment_length,
    }
    header_text = send_response_header(sock, 200, headers)
    log_lines(addr, "[SEND RESPONSE]", header_text.strip())

    log_lines(addr, f"[STREAMING] Sending Segment {segment_number} at {bitrate} kbps (reused from {segment_path})")
    state["streaming"] = True

    sent = 0
    segment_start_time = time.perf_counter()
    with open(segment_path, "rb") as f:
        while sent < segment_length:
            to_read = min(STREAM_CHUNK_BYTES, segment_length - sent)
            chunk = f.read(to_read)
            if not chunk:
                break
            send_all(sock, chunk)
            sent += len(chunk)
            # Real rate limiting: sleep if we're sending faster than the
            # currently active simulated bandwidth allows. Skipped after the
            # very last chunk -- the data is already gone, so pacing it
            # further would only waste time without changing what the
            # client actually measures.
            if sent < segment_length:
                apply_rate_limit(sent, segment_start_time, bandwidth_limit)

    state["streaming"] = False
    log_lines(addr, f"[SUCCESS] Segment {segment_number} sent ({sent} bytes)")

def handle_bitrate(sock, addr, params, state):
    """BITRATE <value> -> stores the client's preferred bitrate for future PLAY calls."""
    if len(params) != 1:
        header_text = send_response_header(sock, 400, {"Message": "Usage: BITRATE <value>"})
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    try:
        bitrate = int(params[0])
    except ValueError:
        header_text = send_response_header(sock, 400, {"Message": "Bitrate must be an integer"})
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    if bitrate not in SUPPORTED_BITRATES:
        header_text = send_response_header(sock, 406, {"Message": f"Bitrate {bitrate} not supported"})
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    # PER-CLIENT STATE: written into this connection's own 'state' dict
    # only -- never a shared global -- so it cannot affect any other
    # client's preferred bitrate.
    state["preferred_bitrate"] = bitrate
    header_text = send_response_header(sock, 200, {"Preferred-Bitrate": bitrate})
    log_lines(addr, "[SEND RESPONSE]", header_text.strip())

def handle_network(sock, addr, params, state):
    """
    # MULTI-CLIENT SUPPORT / PER-CLIENT STATE
    NETWORK <PROFILE> -> pins THIS client's connection to a specific
    simulated bandwidth profile (GOOD / NORMAL / POOR), stored only in its
    own per-connection 'state' dict. This lets one client demo GOOD while
    another demos POOR at the same time, instead of every client being
    forced to share one global profile (see get_effective_network_profile()
    and README Section 11).

    NETWORK AUTO -> clears the override, so this client goes back to
    following the shared global/live-console default profile.

    This command is purely additive: a client that never sends it behaves
    exactly as before (following the global default), so existing demo
    steps and existing client.py behavior are unaffected.
    """
    if len(params) != 1:
        header_text = send_response_header(
            sock, 400, {"Message": "Usage: NETWORK <GOOD|NORMAL|POOR|AUTO>"}
        )
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    choice = params[0].upper()
    if choice == "AUTO":
        state["network_profile"] = None
        name, limit = get_network_profile()
        header_text = send_response_header(
            sock, 200, {"Network-Profile": "AUTO", "Effective-Profile": name}
        )
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    if choice not in NETWORK_PROFILES:
        header_text = send_response_header(
            sock, 406, {"Message": f"Unknown network profile: {choice}"}
        )
        log_lines(addr, "[SEND RESPONSE]", header_text.strip())
        return

    state["network_profile"] = choice
    header_text = send_response_header(sock, 200, {"Network-Profile": choice})
    log_lines(addr, "[SEND RESPONSE]", header_text.strip())
    log_lines(addr, f"[NETWORK SIMULATION] This client pinned its own profile to {choice} ({format_limit(NETWORK_PROFILES[choice])})")

def handle_stop(sock, addr, state):
    """
    STOP -> 200 OK, Message: Streaming stopped

    NOTE ON HOW STOP WORKS IN THIS SIMPLE, SYNCHRONOUS DESIGN:
    Because this server handles one request fully before reading the next
    one (a classic synchronous request/response loop, one thread per
    client), a PLAY/GET_SEGMENT transfer is never running "in the
    background" while we're waiting to read a new command -- by the time
    the server can read a STOP command, any previous transfer has already
    finished sending all of its bytes.

    So STOP here does NOT interrupt an in-flight transfer. Instead, it
    resets streaming state and returns 200 OK. A fuller implementation
    could have the client check for a STOP between GET_SEGMENT requests
    (since segments are naturally chunked, there IS a break point between
    them) -- left as a possible extension, not required for this project.
    """
    state["streaming"] = False
    header_text = send_response_header(sock, 200, {"Message": "Streaming stopped"})
    log_lines(addr, "[SEND RESPONSE]", header_text.strip())

def handle_quit(sock, addr):
    header_text = send_response_header(sock, 200, {"Message": "Connection closing"})
    log_lines(addr, "[SEND RESPONSE]", header_text.strip())


# Per-client thread
def handle_client(conn, addr):
    """
    # MULTI-CLIENT SUPPORT
    Runs in its own daemon thread (one per accepted connection -- see
    main()). Everything this function touches that is specific to THIS
    client (its socket, its buffered reader, and its 'state' dict) is
    local to this function/thread, so two clients being handled at the
    same time by two different threads never see or modify each other's
    data. The only things read from outside this function are the shared,
    read-only 'catalog' and the network-profile helpers above, both of
    which are safe to read concurrently (see their own docstrings/comments).
    """
    with _print_lock:
        print("\n========== SERVER ==========")
        print(f"{client_tag(addr)} [CONNECTED] Client connected: {addr}")

    buffered = BufferedSocket(conn)

    # PER-CLIENT STATE: everything about this specific connection lives in
    # this local dict -- preferred bitrate, whether a stream is "active",
    # and (new) an optional per-client network-profile override. Nothing
    # here is a module-level global, so it can never leak into another
    # client's session.
    state = {
        "preferred_bitrate": 128,
        "streaming": False,
        "network_profile": None,  # None = follow the shared global/live-console default
    }

    try:
        while True:
            try:
                command, params, raw_line = receive_request(buffered)
            except ConnectionClosedError:
                log_lines(addr, "[DISCONNECTED] Client closed the connection")
                break
            except ProtocolError as e:
                log_lines(addr, f"[ERROR] Bad request: {e}")
                try:
                    header_text = send_response_header(conn, 400, {"Message": str(e)})
                    log_lines(addr, "[SEND RESPONSE]", header_text.strip())
                except OSError:
                    break
                continue

            log_lines(addr, "[RECEIVE REQUEST]", raw_line)

            try:
                if command == "LIST":
                    handle_list(conn, addr)
                elif command == "PLAY":
                    handle_play(conn, addr, params, state)
                elif command == "GET_SEGMENT":
                    handle_get_segment(conn, addr, params, state)
                elif command == "BITRATE":
                    handle_bitrate(conn, addr, params, state)
                elif command == "NETWORK":
                    handle_network(conn, addr, params, state)
                elif command == "STOP":
                    handle_stop(conn, addr, state)
                elif command == "QUIT":
                    handle_quit(conn, addr)
                    break
                else:
                    header_text = send_response_header(
                        conn, 400, {"Message": f"Unknown command: {command}"}
                    )
                    log_lines(addr, "[SEND RESPONSE]", header_text.strip())
            # THREAD SAFETY / ERROR ISOLATION: any error while handling THIS
            # client (a dropped socket, or any other unexpected exception)
            # is caught here and only breaks THIS client's loop/thread --
            # it can never propagate out and take down the main server
            # thread or any other client's thread.
            except (OSError, ConnectionClosedError) as e:
                log_lines(addr, f"[DISCONNECTED] Client dropped during request handling: {e}")
                break
            except Exception as e:
                log_lines(addr, f"[ERROR] Unexpected server error: {e}")
                try:
                    header_text = send_response_header(conn, 500, {"Message": "Internal server error"})
                    log_lines(addr, "[SEND RESPONSE]", header_text.strip())
                except OSError:
                    break
    finally:
        conn.close()
        log_lines(addr, "[CLOSED] Connection closed")


# Main server loop
def parse_args():
    parser = argparse.ArgumentParser(description="MiniMP3Stream / SMAP server")
    parser.add_argument(
        "--network",
        choices=list(NETWORK_PROFILES.keys()),
        default="GOOD",
        help="Initial simulated GLOBAL default network profile (can be changed live by typing into this terminal; individual clients can override it with NETWORK <PROFILE>)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    set_network_profile(args.network)

    build_catalog()

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((HOST, PORT))
    server_sock.listen()
    name, limit = get_network_profile()
    print(f"[LISTENING] StreamMiniMP3 server on {HOST}:{PORT}")
    print(f"[NETWORK SIMULATION] Starting GLOBAL default profile: {name} ({format_limit(limit)})")

    # Lets the operator change GOOD/NORMAL/POOR live from this terminal
    # while the server keeps serving clients.
    console_thread = threading.Thread(target=network_console_loop, daemon=True)
    console_thread.start()

    # MULTI-CLIENT SUPPORT: the main thread's only job from here on is to
    # keep calling accept() and handing each new connection off to its own
    # thread. It never blocks on any one client's request loop, so a new
    # client can connect (and get its own thread) at any time, no matter
    # what any previously connected client is doing.
    try:
        while True:
            conn, addr = server_sock.accept()
            client_thread = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            client_thread.start()
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Server stopping (Ctrl+C)")
    finally:
        server_sock.close()

if __name__ == "__main__":
    main()
