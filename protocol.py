"""
protocol.py
-----------
Reusable building blocks for the SMAP (Simple Music Adaptive Protocol).

SMAP is a simple text-based request/response protocol layered on top of TCP.

Request format (client -> server), single text line:
    COMMAND [parameters]\n

Response format (server -> client), text header block:
    STATUS_CODE STATUS_PHRASE\n
    Header-Name: value\n
    Header-Name: value\n
    \n
followed optionally by a body (JSON text for LIST, or raw MP3 bytes for PLAY
and GET_SEGMENT, streamed separately by server.py / client.py).

----------------------------------------------------------------------------
WHY THIS FILE IS MORE THAN JUST socket.send()/recv()
----------------------------------------------------------------------------
TCP is a byte STREAM, not a message protocol. It does NOT preserve message
boundaries. A single sock.recv() call can return:
    - less than one full message (e.g. half of "PLAY 1 12" then "8\n")
    - exactly one full message
    - one full message PLUS part of the NEXT message (e.g. the response
      header immediately followed by the first bytes of the MP3 file)

BufferedSocket below solves this: it keeps reading into an internal buffer
until it finds the delimiter it was asked for (either "\n" for a request
line, or "\n\n" for a response header block), and it keeps any left-over
bytes around so later reads (e.g. reading exact segment bytes with
read_exact) don't lose data.
"""

import json

CHUNK_SIZE = 4096
LINE_DELIMITER = b"\n"
HEADER_DELIMITER = b"\n\n"
ENCODING = "utf-8"

# Bitrate ladder shared by server (catalog / segment slicing) and client
AVAILABLE_BITRATES = [64, 128, 192, 320]

# Segmentation configuration
# Each per-bitrate MP3 file is cut into fixed-size byte ranges.
SEGMENT_SIZE_BYTES = 8192

# Adaptive bitrate (ABR) configuration
THROUGHPUT_HISTORY_SIZE = 3

# Fraction of the estimated throughput we actually allow ourselves to use.
# Keeping some headroom below the raw measured number avoids constantly
# saturating the link and causing stalls.
SAFETY_FACTOR = 0.70


# Status codes used by the SMAP protocol
STATUS_PHRASES = {
    200: "OK",
    400: "BAD REQUEST",
    404: "NOT FOUND",
    406: "NOT ACCEPTABLE",
    500: "INTERNAL SERVER ERROR",
}


class ProtocolError(Exception):
    # Raised when received data does not follow the SMAP protocol format
    pass

class ConnectionClosedError(Exception):
    # Raised when the peer closes the socket before we got what we needed
    pass


# Low level send helper
def send_all(sock, data: bytes):
    """
    Send every byte in 'data', even if the OS only accepts part of it in a
    single underlying send() call. socket.sendall() already loops internally
    until everything is sent (or raises on error/disconnect), but we wrap it
    here so every outgoing write in the project goes through one place.
    """
    sock.sendall(data)


# BufferedSocket: solves the "TCP does not preserve message boundaries" issue
class BufferedSocket:
    """
    Wraps a raw socket and adds two operations that raw sockets don't give
    you for free:

        read_until(delimiter) -> bytes
            Keeps calling recv() and accumulating data in an internal
            buffer until 'delimiter' shows up. Returns everything BEFORE
            the delimiter. Anything read PAST the delimiter is kept in the
            buffer for the next call (instead of being thrown away).

        read_exact(n) -> bytes
            Returns exactly n bytes: first bytes already sitting in the
            buffer, then more bytes read from the socket, looping until
            we have n bytes total. This is what lets us reliably receive
            an MP3 file (or a single segment of one) of a known
            Content-Length.
    """

    def __init__(self, sock, recv_size=4096):
        self.sock = sock
        self.recv_size = recv_size
        self.buffer = b""

    def read_until(self, delimiter: bytes) -> bytes:
        while delimiter not in self.buffer:
            chunk = self.sock.recv(self.recv_size)
            if not chunk:
                if self.buffer:
                    raise ConnectionClosedError(
                        "Connection closed mid-message while waiting for delimiter"
                    )
                raise ConnectionClosedError("Connection closed by peer")
            self.buffer += chunk

        index = self.buffer.index(delimiter)
        data = self.buffer[:index]
        # Anything after the delimiter belongs to the NEXT message
        # (e.g. it could already be the start of the segment body) -- keep it.
        self.buffer = self.buffer[index + len(delimiter):]
        return data

    def read_exact(self, n: int) -> bytes:
        data = bytearray()

        # Use whatever is already buffered first.
        if self.buffer:
            take = min(len(self.buffer), n)
            data += self.buffer[:take]
            self.buffer = self.buffer[take:]

        # Then top up from the socket until we have exactly n bytes.
        while len(data) < n:
            remaining = n - len(data)
            chunk = self.sock.recv(min(self.recv_size, remaining))
            if not chunk:
                raise ConnectionClosedError(
                    f"Connection closed after receiving {len(data)}/{n} bytes"
                )
            data += chunk

        return bytes(data)

def receive_exact(buffered_sock: "BufferedSocket", n: int) -> bytes:
    return buffered_sock.read_exact(n)


# Request helpers (client -> server)
def parse_request(line: str):
    #Parses a request line "COMMAND [param1] [param2] ..." into (command, [params]) 
    #Raises ProtocolError on an empty line.
    line = line.strip()
    if not line:
        raise ProtocolError("Empty request line")
    parts = line.split()
    command = parts[0].upper()
    params = parts[1:]
    return command, params

def build_request(command: str, *params) -> bytes:
    #build_request('PLAY', 1, 128) -> b'PLAY 1 128\\n'
    parts = [command] + [str(p) for p in params]
    line = " ".join(parts)
    return (line + "\n").encode(ENCODING)

def send_request(sock, command: str, *params) -> str:
    #Sends a request line and returns the line (for logging)
    data = build_request(command, *params)
    send_all(sock, data)
    return data.decode(ENCODING).strip()

def receive_request(buffered_sock: "BufferedSocket"):
    #Reads one request line (up to the next \\n) and parses it & Returns (command, params, raw_line_str)
    raw = buffered_sock.read_until(LINE_DELIMITER)
    line = raw.decode(ENCODING)
    command, params = parse_request(line)
    return command, params, line


# Response helpers (server -> client)
def build_response(status_code: int, headers: dict = None, body: bytes = None) -> bytes:
    phrase = STATUS_PHRASES.get(status_code, "UNKNOWN")
    lines = [f"{status_code} {phrase}"]
    if headers:
        for key, value in headers.items():
            lines.append(f"{key}: {value}")
    header_block = ("\n".join(lines) + "\n\n").encode(ENCODING)
    if body:
        return header_block + body
    return header_block

def send_response_header(sock, status_code: int, headers: dict = None) -> str:
    # Sends only the header block (status line + headers + blank line)
    data = build_response(status_code, headers)
    send_all(sock, data)
    return data.decode(ENCODING)

def receive_response_header(buffered_sock: "BufferedSocket"):
    # Reads a full response header block & Returns (status_code, status_phrase, headers_dict, raw_text).

    raw = buffered_sock.read_until(HEADER_DELIMITER)
    text = raw.decode(ENCODING)
    lines = text.split("\n")

    if not lines or not lines[0].strip():
        raise ProtocolError("Empty response header")

    status_line = lines[0].strip()
    status_parts = status_line.split(" ", 1)
    try:
        status_code = int(status_parts[0])
    except ValueError:
        raise ProtocolError(f"Invalid status code in response: {status_line}")
    status_phrase = status_parts[1] if len(status_parts) > 1 else ""

    headers = {}
    for line in lines[1:]:
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip()] = value.strip()

    return status_code, status_phrase, headers, text


# JSON helpers (used for the LIST response body)
def to_json_bytes(obj) -> bytes:
    return json.dumps(obj, indent=2).encode(ENCODING)


# Adaptive bitrate helpers
def calculate_throughput_kbps(num_bytes: int, elapsed_seconds: float) -> float:
    
    #Throughput (kbps) = (bytes * 8) / (seconds * 1000)
    elapsed_seconds = max(elapsed_seconds, 0.001)
    return (num_bytes * 8) / (elapsed_seconds * 1000)

def select_bitrate(preferred_bitrate: int, available_throughput_kbps: float):
    """
    Chooses the bitrate to use for the NEXT segment.

    Rules (in order):
      1. Never select a bitrate higher than 'preferred_bitrate' -- the user's
         preference is a hard ceiling, not a suggestion.
      2. Among bitrates the preference allows, pick the highest one that is
         still <= 'available_throughput_kbps' (this argument is expected to
         already be a "safe" capacity -- e.g. estimated throughput with a
         safety margin already applied by the caller).
      3. If even the lowest available bitrate cannot be sustained, we still
         return the lowest bitrate (64 kbps) as a floor -- we never stream
         below the smallest option this project supports.
    """
    # Bitrates the user's preference allows at all.
    allowed = [b for b in AVAILABLE_BITRATES if b <= preferred_bitrate]
    if not allowed:
        # Preference set below our lowest bitrate -- still have to serve
        allowed = [min(AVAILABLE_BITRATES)]

    # The network can currently sustain.
    sustainable = [b for b in allowed if b <= available_throughput_kbps]

    if sustainable:
        selected = max(sustainable)
    else:
        # Use the absolute floor rather than refusing to stream at all.
        selected = min(AVAILABLE_BITRATES)

    if selected == max(allowed) and available_throughput_kbps >= selected:
        # Use the best bitrate the preference allows
        if selected == max(AVAILABLE_BITRATES):
            reason = "OPTIMAL"
        else:
            reason = "USER_PREFERENCE_LIMIT"
    else:
        reason = "NETWORK_LIMITED"

    return selected, reason