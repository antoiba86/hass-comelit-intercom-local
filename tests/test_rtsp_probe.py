"""Quick probe to check if the Comelit device exposes RTSP.

Run: python3 tests/test_rtsp_probe.py
Requires: pip install opencv-python-headless
"""

import socket
import sys

DEVICE_IP = "192.168.1.111"
RTSP_PORTS = [554, 8554, 8080, 64100]
RTSP_PATHS = [
    "/ch01.264",
    "/live",
    "/stream",
    "/video",
    "/cam/realmonitor",
    "/h264Preview_01_main",
    "/Streaming/Channels/1",
    "/",
    "",
]
CREDS = [
    ("admin", "comelit"),
    ("admin", "admin"),
    ("", ""),
]


def check_port(host: str, port: int, timeout: float = 2.0) -> bool:
    """Check if a TCP port is open."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


def try_rtsp_options(host: str, port: int, path: str, timeout: float = 3.0) -> str | None:
    """Send RTSP OPTIONS request and return response (or None)."""
    url = f"rtsp://{host}:{port}{path}"
    request = f"OPTIONS {url} RTSP/1.0\r\nCSeq: 1\r\n\r\n"
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(request.encode())
            response = sock.recv(4096).decode("utf-8", errors="replace")
            return response
    except (OSError, TimeoutError):
        return None


def try_opencv(url: str) -> bool:
    """Try to open the URL with OpenCV and grab one frame."""
    try:
        import cv2
    except ImportError:
        print("  [skip] opencv not installed (pip install opencv-python-headless)")
        return False

    print(f"  Trying cv2.VideoCapture('{url}') ...")
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        print("  [fail] Could not open stream")
        cap.release()
        return False

    ret, frame = cap.read()
    cap.release()
    if ret and frame is not None:
        print(f"  [OK!] Got frame: {frame.shape}")
        return True
    else:
        print("  [fail] Opened but no frame")
        return False


def main():
    print(f"=== RTSP Probe for {DEVICE_IP} ===\n")

    # Step 1: Check which ports are open
    print("1. Port scan:")
    open_ports = []
    for port in RTSP_PORTS:
        is_open = check_port(DEVICE_IP, port)
        status = "OPEN" if is_open else "closed"
        print(f"   {port}: {status}")
        if is_open:
            open_ports.append(port)

    if not open_ports:
        print("\nNo RTSP ports open. Device may not support RTSP.")
        return

    # Step 2: Try RTSP OPTIONS on open ports
    print("\n2. RTSP OPTIONS probes:")
    working_urls = []
    for port in open_ports:
        for path in RTSP_PATHS:
            resp = try_rtsp_options(DEVICE_IP, port, path)
            if resp and "RTSP/" in resp:
                status_line = resp.split("\r\n")[0]
                print(f"   :{port}{path} → {status_line}")
                if "200" in status_line:
                    for user, pw in CREDS:
                        if user:
                            url = f"rtsp://{user}:{pw}@{DEVICE_IP}:{port}{path}"
                        else:
                            url = f"rtsp://{DEVICE_IP}:{port}{path}"
                        working_urls.append(url)
                    break
            else:
                # Not RTSP
                pass

    if not working_urls:
        print("   No RTSP responses found on any port/path.")
        print("\n   The device likely requires the ICONA TCP signaling to start video.")
        return

    # Step 3: Try OpenCV on working URLs
    print(f"\n3. OpenCV test ({len(working_urls)} URLs to try):")
    for url in working_urls:
        if try_opencv(url):
            print(f"\n=== SUCCESS: {url} ===")
            return

    print("\nRTSP responded but no frames captured. May need auth or signaling first.")


if __name__ == "__main__":
    main()
