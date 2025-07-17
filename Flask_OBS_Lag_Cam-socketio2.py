from flask import Flask, jsonify, render_template
from flask_socketio import SocketIO, emit
import threading
import time
from pathlib import Path
import collections
import random
import math
import cv2
import pyvirtualcam
from pyvirtualcam import PixelFormat
import obsws_python as obs
from concurrent.futures import ThreadPoolExecutor
import signal
import sys
import atexit


# Flask App with SocketIO
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# Configuration
OBS_HOST = "192.168.50.26"
OBS_PORT = 4455
OBS_PASSWORD = "hwtCauYIK9owRB7X"
RECORD_SCENE = "RecordCam"
PLAY_SCENE = "Playback"
MEDIA_INPUT = "LagCamInput"
FIXED_NAME = "generated_video.mp4"
CLIP_PATH = Path.cwd() / FIXED_NAME
SECS = 10
BASE_LAG_S = 0.6
FREEZE_PROB = 0.1
FREEZE_MIN_S = 0.1
FREEZE_MAX_S = 0.66
RESOLUTION_DOWNGRADE_PROB = 0.59
LAG_PROB = 0.3
MIN_DOWNGRADE_FRAMES = 15
MAX_DOWNGRADE_FRAMES = 240

# Thread and State Management
executor = ThreadPoolExecutor(max_workers=2)
stop_event = threading.Event()
script_thread = None
recording_start_time = None



def connect():
    """ Establish OBS WebSocket connection. """
    return obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=5)


def reset_obs_scene():
    """Reset OBS to record scene and cleanup."""
    print("\nPerforming emergency OBS scene reset...")
    ws = None
    try:
        ws = connect()
        ws.set_current_program_scene(name=RECORD_SCENE)
        ws.set_current_preview_scene(name=RECORD_SCENE)
        print("Successfully reset OBS to RecordCam scene")
    except Exception as e:
        print(f"Warning: Failed to reset OBS scene: {e}")
    finally:
        if ws:
            try:
                ws.disconnect()
            except:
                pass


def signal_handler(signum, frame):
    """Handle termination signals."""
    print(f"\nReceived signal {signum}. Cleaning up...")
    reset_obs_scene()
    sys.exit(0)


def initialize_obs():
    """Initialize OBS connection and set initial scene."""
    try:
        ws = connect()
        ws.set_current_program_scene(name=RECORD_SCENE)
        ws.set_current_preview_scene(name=RECORD_SCENE)
        ws.disconnect()
        print("Successfully initialized OBS with RecordCam scene")
    except Exception as e:
        print(f"Warning: Failed to initialize OBS scene: {e}")


def wait_file_unlocked(f: Path, tries=30, delay=0.25):
    """ Wait until the file is unlocked by OBS. """
    for _ in range(tries):
        try:
            f.rename(f)
            return
        except PermissionError:
            time.sleep(delay)
    raise TimeoutError("OBS never released recording file.")


def new_take(seconds: int):
    """ Start new recording and save the file path. """
    ws = connect()
    ws.set_current_program_scene(name=RECORD_SCENE)
    if ws.get_record_status().output_active:
        ws.stop_record()
    ws.start_record()
    ws.set_current_preview_scene(name=RECORD_SCENE)
    while not ws.get_record_status().output_active:
        time.sleep(0.05)
    time.sleep(seconds)
    try:
        rec = ws.stop_record()
        src = Path(rec.output_path)
    except Exception:
        src = Path(ws.get_record_status().output_path)
    ws.disconnect()
    wait_file_unlocked(src)
    if CLIP_PATH.exists():
        CLIP_PATH.unlink()
    src.replace(CLIP_PATH)
    return CLIP_PATH


def playback_loop():
    """Playback video through virtual camera with lag and freeze effects."""
    ws = None
    cap = None
    cam = None
    try:
        ws = connect()
        cap = cv2.VideoCapture(str(CLIP_PATH))
        if not cap.isOpened():
            raise RuntimeError("Failed to open video file")
        width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 15
        lag = collections.deque(maxlen=math.ceil(BASE_LAG_S * fps))
        # Initialize lag buffer
        ret, first_frame = cap.read()
        if not ret:
            raise RuntimeError("Failed to read the first frame.")
        for _ in range(lag.maxlen):
            if stop_event.is_set():
                return
            ret, frame = cap.read()
            if not ret:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            lag.append(frame)
        cam = pyvirtualcam.Camera(width, height, fps, fmt=PixelFormat.BGR)
        cam.send(first_frame)
        cam.sleep_until_next_frame()
        if stop_event.is_set():
            return
        ws.set_current_program_scene(name=PLAY_SCENE)
        ws.set_current_preview_scene(name=RECORD_SCENE)
        freeze_left, last = 0, None
        downgrade_active = False
        downgrade_frames_left = 0
        downgrade_scale_factor = 1.0
        while not stop_event.is_set():
            if stop_event.is_set():  # Double check stop event
                break
            ret, frame = cap.read()
            if not ret:
                if stop_event.is_set():
                    break
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            # Randomly trigger a downgrade if not already active
            if not downgrade_active and random.random() < RESOLUTION_DOWNGRADE_PROB:
                downgrade_active = True
                downgrade_scale_factor = random.uniform(0.3, 1.0)
                downgrade_frames_left = random.randint(MIN_DOWNGRADE_FRAMES, MAX_DOWNGRADE_FRAMES)
            # Apply downgrade if active
            if downgrade_active:
                new_width, new_height = int(width * downgrade_scale_factor), int(height * downgrade_scale_factor)
                frame = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_LINEAR)
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_NEAREST)
                downgrade_frames_left -= 1
                if downgrade_frames_left <= 0:
                    downgrade_active = False
            # Artificial lag (optional)
            if random.random() < LAG_PROB:
                additional_lag_frames = random.randint(1, 5)
                for _ in range(additional_lag_frames):
                    if stop_event.is_set():
                        break
                    lag.append(frame)
            lag.append(frame)
            if freeze_left == 0 and random.random() < FREEZE_PROB:
                freeze_left = math.ceil(random.uniform(FREEZE_MIN_S, FREEZE_MAX_S) * fps)
            out = last if freeze_left else (lag.popleft() if len(lag) == lag.maxlen else frame * 0)
            if freeze_left:
                freeze_left -= 1
            if stop_event.is_set():
                break
            cam.send(out)
            last = out
            cam.sleep_until_next_frame()
    except Exception as e:
        print(f"Error in playback loop: {e}")
        socketio.emit('error', {'message': str(e)})
        raise
    finally:
        print("Cleaning up playback resources...")
        try:
            if ws:
                ws.set_current_program_scene(name=RECORD_SCENE)
                ws.set_current_preview_scene(name=RECORD_SCENE)
                ws.disconnect()
        except Exception as e:
            print(f"Error disconnecting OBS: {e}")

        try:
            if cap and cap.isOpened():
                cap.release()
        except Exception as e:
            print(f"Error releasing video capture: {e}")

        try:
            if cam:
                cam.close()
        except Exception as e:
            print(f"Error closing virtual camera: {e}")
        print("Playback cleanup complete")


def countdown_and_record(seconds: int):
    """Perform countdown and then start recording."""
    # 3-second countdown
    for i in range(3, 0, -1):
        socketio.emit('countdown_update', {'count': i, 'type': 'pre'})
        time.sleep(1)
    socketio.emit('countdown_update', {'count': 0, 'type': 'pre'})

    # Start recording timer updates before starting the recording
    global recording_start_time
    recording_start_time = time.time()
    # Start a separate thread for the timer updates

    def update_timer():
        global recording_start_time  # Use global since recording_start_time is a global variable
        while not stop_event.is_set() and time.time() - recording_start_time < seconds:
            remaining = max(0, seconds - (time.time() - recording_start_time))
            socketio.emit('countdown_update', {
                'count': round(remaining, 1),
                'type': 'recording'
            })
            time.sleep(0.1)
        recording_start_time = None
        socketio.emit('countdown_update', {'count': 0, 'type': 'recording'})
    timer_thread = threading.Thread(target=update_timer)
    timer_thread.start()
    # Start the actual recording
    new_take(seconds)
    # Wait for timer thread to complete
    timer_thread.join()


def run_script():
    """Main script execution: countdown, record and play."""
    try:
        countdown_and_record(SECS)
        playback_loop()
    except Exception as e:
        socketio.emit('error', {'message': str(e)})
        raise


@app.route('/start', methods=['POST'])
def start_script():
    global script_thread, stop_event
    if script_thread and script_thread.is_alive():
        return jsonify({"status": "already running"}), 400
    stop_event.clear()
    script_thread = threading.Thread(target=run_script)
    script_thread.start()
    return jsonify({"status": "started"}), 200


@app.route('/stop', methods=['POST'])
def stop_script():
    global script_thread, stop_event, recording_start_time
    ws = None
    print("Stop request received")
    if not script_thread or not script_thread.is_alive():
        print("No active script thread")
        return jsonify({"status": "not running"}), 400
    try:
        print("Setting stop event")
        stop_event.set()
        recording_start_time = None
        # Emit stop signals
        socketio.emit('countdown_update', {'count': 0, 'type': 'pre'})
        socketio.emit('countdown_update', {'count': 0, 'type': 'recording'})
        print("Waiting for thread to complete")
        # Wait for thread to complete with timeout
        script_thread.join(timeout=5.0)
        # Force cleanup if thread is still alive
        if script_thread.is_alive():
            print("Warning: Script thread did not terminate gracefully")
            # Reset state
            stop_event.clear()
            script_thread = None
            return jsonify({"status": "force stopped"}), 200
        print("Thread stopped successfully")
        return jsonify({"status": "stopped"}), 200
    except Exception as e:
        print(f"Error during stop: {e}")
        return jsonify({"status": f"error during stop: {str(e)}"}), 500
    finally:
        print("Resetting stop state")
        # Always reset the event for next run
        stop_event.clear()
        script_thread = None
        # Ensure OBS is set back to record scene
        try:
            ws = connect()
            ws.set_current_program_scene(name=RECORD_SCENE)
            ws.set_current_preview_scene(name=RECORD_SCENE)
            ws.disconnect()
            print("Successfully reset OBS to RecordCam scene")
        except Exception as e:
            print(f"Warning: Failed to reset OBS scene: {e}")
        finally:
            if ws:
                try:
                    ws.disconnect()
                except:
                    pass


@app.route('/')
def index():
    return render_template('index.html')

# Register signal handlers for graceful termination
signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
signal.signal(signal.SIGTERM, signal_handler)  # Termination signal
if hasattr(signal, 'SIGBREAK'):  # Windows Ctrl+Break
    signal.signal(signal.SIGBREAK, signal_handler)
# Register cleanup function to run on normal exit
atexit.register(reset_obs_scene)
# Initialize OBS when app starts
initialize_obs()

if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)
