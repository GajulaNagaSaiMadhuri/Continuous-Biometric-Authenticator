from flask import Flask, render_template, request, jsonify, session
import os
import sqlite3
import base64
import time
from datetime import datetime, timezone

import cv2
import numpy as np
import mediapipe as mp

ROOT = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(ROOT, "models")
DATA = os.path.join(ROOT, "data")
DB = os.path.join(DATA, "authenticator.db")
YUNET = os.path.join(MODELS, "face_detection_yunet_2023mar.onnx")
SFACE = os.path.join(MODELS, "face_recognition_sface_2021dec.onnx")

THRESHOLD = 0.363  # Validate for your camera and users.
MAX_FAILURES = 2

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "local-demo-change-this-secret")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
os.makedirs(DATA, exist_ok=True)

detector = recognizer = None
model_error = None
try:
    if not os.path.isfile(YUNET) or not os.path.isfile(SFACE):
        raise FileNotFoundError(
            "Copy both ONNX model files into this project's models folder."
        )
    detector = cv2.FaceDetectorYN_create(YUNET, "", (320, 320), 0.8, 0.3, 5000)
    recognizer = cv2.FaceRecognizerSF_create(SFACE, "")
except Exception as exc:
    model_error = str(exc)

mp_face_mesh = mp.solutions.face_mesh

# MediaPipe Face Mesh eyelid landmarks.
LEFT_EYE = [362, 385, 387, 263, 373, 380]
RIGHT_EYE = [33, 160, 158, 133, 153, 144]


def connect():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db():
    with connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS users("
            "id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE NOT NULL, "
            "template BLOB NOT NULL, created_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS logs("
            "id INTEGER PRIMARY KEY, username TEXT, event TEXT NOT NULL, "
            "score REAL, details TEXT, created_at TEXT NOT NULL)"
        )


def log(user, event, score=None, details=None):
    with connect() as db:
        db.execute(
            "INSERT INTO logs(username,event,score,details,created_at) "
            "VALUES(?,?,?,?,?)",
            (user, event, score, details, now()),
        )


def clear_auth():
    for key in ("user", "started", "failures"):
        session.pop(key, None)


def authed():
    if not session.get("user") or time.time() - session.get("started", 0) > 3600:
        clear_auth()
        return False
    return True


def decode(data):
    if not isinstance(data, str) or "," not in data:
        raise ValueError("No camera frame received.")
    header, encoded = data.split(",", 1)
    if "base64" not in header:
        raise ValueError("Invalid image data.")
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > 5_000_000:
        raise ValueError("Image too large.")
    frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("Could not read camera image.")
    return frame


def eye_aspect_ratio(points, ids):
    p = [points[i] for i in ids]

    def distance(a, b):
        return float(np.linalg.norm(np.asarray(a) - np.asarray(b)))

    horizontal = distance(p[0], p[3])
    if horizontal < 1:
        return 1.0
    return (
        distance(p[1], p[5]) + distance(p[2], p[4])
    ) / (2.0 * horizontal)


def get_eye_ratio(frame):
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    height, width = frame.shape[:2]

    # Static image mode works for individual frames sent by the browser.
    with mp_face_mesh.FaceMesh(
        static_image_mode=True,
        max_num_faces=2,
        refine_landmarks=True,
        min_detection_confidence=0.5,
    ) as mesh:
        result = mesh.process(rgb)

    if not result.multi_face_landmarks or len(result.multi_face_landmarks) != 1:
        return None

    landmarks = result.multi_face_landmarks[0].landmark
    points = [(point.x * width, point.y * height) for point in landmarks]
    left = eye_aspect_ratio(points, LEFT_EYE)
    right = eye_aspect_ratio(points, RIGHT_EYE)
    return (left + right) / 2.0


def check_liveness_frames(images):
    """Basic blink challenge; not a guarantee against photo/video spoofing."""
    if not isinstance(images, list) or not 5 <= len(images) <= 30:
        return False, "Blink check failed. Please try again."

    ratios = []
    for image in images:
        ratio = get_eye_ratio(decode(image))
        if ratio is None:
            return False, "Keep exactly one face visible during the blink check."
        ratios.append(ratio)

    # Require eyes open, then closed, then open again.
    open_threshold = 0.19
    closed_threshold = 0.14
    opened_before = False
    blink_closed = False

    for ratio in ratios:
        if ratio >= open_threshold:
            if blink_closed:
                return True, "Blink detected successfully."
            opened_before = True
        elif ratio <= closed_threshold and opened_before:
            blink_closed = True

    return False, "Blink not detected. Look at the camera and blink once naturally."


def feature_for(frame):
    if detector is None or recognizer is None:
        raise RuntimeError(model_error or "Models not loaded.")

    height, width = frame.shape[:2]
    detector.setInputSize((width, height))
    _, faces = detector.detect(frame)

    if faces is None or len(faces) == 0:
        return None, "NO_FACE"
    if len(faces) != 1:
        return None, "MULTIPLE_FACES"

    aligned = recognizer.alignCrop(frame, faces[0])
    feature = recognizer.feature(aligned).astype(np.float32).flatten()
    norm = np.linalg.norm(feature)
    if norm == 0:
        return None, "INVALID_FEATURE"
    return feature / norm, "OK"


def unpack(blob):
    return np.frombuffer(blob, dtype=np.float32).copy()


def find_user(name):
    with connect() as db:
        return db.execute(
            "SELECT username,template FROM users WHERE username=? COLLATE NOCASE",
            (name,),
        ).fetchone()


def match_any(feature):
    with connect() as db:
        rows = db.execute("SELECT username,template FROM users").fetchall()

    best_name, best_score = None, -1.0
    for row in rows:
        template = unpack(row["template"])
        if template.shape == feature.shape:
            score = float(np.dot(feature, template))
            if score > best_score:
                best_name, best_score = row["username"], score
    return best_name, best_score


init_db()


@app.get("/")
def home():
    return render_template("index.html", model_error=model_error)


@app.get("/api/status")
def status():
    with connect() as db:
        count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    authenticated = authed()
    return jsonify(
        users=count,
        authenticated=authenticated,
        username=session.get("user") if authenticated else None,
        models_ready=model_error is None,
        model_error=model_error,
        threshold=THRESHOLD,
    )


@app.get("/api/users")
def list_users():
    with connect() as db:
        rows = db.execute(
            "SELECT username,created_at FROM users ORDER BY username"
        ).fetchall()
    return jsonify(users=[dict(row) for row in rows])


@app.post("/api/register")
def register():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("username", "")).strip()
    if not name or len(name) > 40 or any(ord(ch) < 32 for ch in name):
        return jsonify(ok=False, message="Enter a username of 1–40 characters."), 400

    try:
        live, live_message = check_liveness_frames(payload.get("liveness_images"))
        if not live:
            return jsonify(ok=False, message=live_message), 400

        feature, state = feature_for(decode(payload.get("image", "")))
        if state != "OK":
            messages = {
                "NO_FACE": "No face detected. Move into view.",
                "MULTIPLE_FACES": "Only one face may be visible during registration.",
                "INVALID_FEATURE": "Could not create a face template.",
            }
            return jsonify(ok=False, message=messages.get(state, state)), 400

        with connect() as db:
            exists = db.execute(
                "SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (name,)
            ).fetchone()
            if exists:
                return jsonify(
                    ok=False, message="Username already registered. Choose another."
                ), 409
            db.execute(
                "INSERT INTO users(username,template,created_at) VALUES(?,?,?)",
                (name, feature.astype(np.float32).tobytes(), now()),
            )

        log(name, "REGISTERED", details="Face template stored; blink challenge passed")
        return jsonify(ok=True, message=f"{name} registered successfully. Blink detected.")
    except Exception as exc:
        return jsonify(ok=False, message=str(exc)), 500


@app.post("/api/authenticate")
def authenticate():
    try:
        payload = request.get_json(silent=True) or {}
        live, live_message = check_liveness_frames(payload.get("liveness_images"))
        if not live:
            log(None, "LIVENESS_FAILED", details=live_message)
            clear_auth()
            return jsonify(ok=False, message=live_message), 401

        feature, state = feature_for(decode(payload.get("image", "")))
        if state != "OK":
            log(None, state, details="Initial authentication")
            clear_auth()
            message = "No face detected." if state == "NO_FACE" else "Exactly one face must be visible."
            return jsonify(ok=False, message=message), 401

        name, score = match_any(feature)
        if name is None or score < THRESHOLD:
            log(None, "UNKNOWN_FACE", score, "Initial authentication")
            clear_auth()
            return jsonify(
                ok=False, message="Face not recognized. Access denied.",
                score=round(score, 4)
            ), 401

        session["user"] = name
        session["started"] = time.time()
        session["failures"] = 0
        log(name, "AUTHENTICATED", score, "Blink challenge passed")
        return jsonify(
            ok=True, username=name, score=round(score, 4),
            message=f"Welcome, {name}. Blink detected; workspace unlocked."
        )
    except Exception as exc:
        return jsonify(ok=False, message=str(exc)), 500


@app.post("/api/continuous-check")
def continuous():
    if not authed():
        return jsonify(ok=False, locked=True, message="Session locked. Authenticate again."), 401

    name = session["user"]
    try:
        feature, state = feature_for(
            decode((request.get_json(silent=True) or {}).get("image", ""))
        )
        score = None

        if state == "OK":
            row = find_user(name)
            if row is None:
                clear_auth()
                return jsonify(
                    ok=False, locked=True, message="User template no longer exists."
                ), 401

            template = unpack(row["template"])
            score = float(np.dot(feature, template)) if template.shape == feature.shape else -1.0
            if score >= THRESHOLD:
                session["failures"] = 0
                log(name, "CONTINUOUS_MATCH", score)
                return jsonify(
                    ok=True, locked=False, username=name, score=round(score, 4),
                    message="Identity verified."
                )
            state = "UNKNOWN_FACE"

        failures = int(session.get("failures", 0)) + 1
        session["failures"] = failures
        log(name, state, score, f"Continuous check {failures}/{MAX_FAILURES}")

        if failures >= MAX_FAILURES:
            log(name, "SESSION_LOCKED", score, state)
            clear_auth()
            return jsonify(
                ok=False, locked=True,
                message="Workspace locked: identity could not be verified. Authenticate again."
            ), 401

        return jsonify(
            ok=False, locked=False,
            message=f"Check failed ({failures}/{MAX_FAILURES}); checking again shortly."
        )
    except Exception as exc:
        return jsonify(ok=False, locked=False, message=str(exc)), 500


@app.post("/api/logout")
def logout():
    name = session.get("user")
    if name:
        log(name, "LOGOUT")
    clear_auth()
    return jsonify(ok=True, message="Workspace locked.")


@app.get("/api/logs")
def list_logs():
    with connect() as db:
        rows = db.execute(
            "SELECT username,event,score,details,created_at FROM logs ORDER BY id DESC LIMIT 30"
        ).fetchall()
    return jsonify(logs=[dict(row) for row in rows])


if __name__ == "__main__":
    print("Open http://127.0.0.1:5000 in your browser.")
    if model_error:
        print("MODEL WARNING:", model_error)
    app.run(host="127.0.0.1", port=5000, debug=False)
