#!/usr/bin/env python3
"""
Video → Prompt Website (Flask backend)
----------------------------------------
موقع بسيط: ترفع فيديو من جهازك أو تحط رابط فيديو،
والسيرفر يحلل الفيديو ويطلع لك برومبت تفصيلي (Image + Video Prompt)
لكل مشهد، تقدر تستخدمه في Google Flow / Veo لإنتاج فيديو مشابه.

تشغيل الموقع محليًا:
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY="sk-ant-..."
    python app.py
    ثم افتح المتصفح على: http://127.0.0.1:5000
"""

import base64
import os
import subprocess
import tempfile
import shutil
import uuid
from pathlib import Path

from flask import Flask, request, jsonify

import anthropic

app = Flask(__name__)

MAX_FRAMES = 12
DEFAULT_FRAMES = 6
MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """أنت مخرج سينمائي محترف ومحلل فيديو متخصص في إعادة هندسة البرومبتات
(Prompt Reverse Engineering) لأدوات توليد الفيديو بالذكاء الاصطناعي مثل Google Flow / Veo.

هتستلم سلسلة فريمات مأخوذة بالترتيب الزمني من فيديو واحد. مهمتك:

1. افهم القصة الكاملة للفيديو: مين الشخصيات (بشر/حيوانات)، المكان، الوقت، الإضاءة،
   الحالة العاطفية، وترتيب الأحداث من البداية للنهاية.
2. قسّم الفيديو لمشاهد منطقية (Scenes) حسب عدد الفريمات وتغير الأحداث.
3. لكل مشهد اكتب:
   - **Image Prompt** (بالإنجليزي): وصف دقيق للفريم الأول في المشهد
     (المكان، الإضاءة، وضعية الشخصيات/الحيوانات، الألوان، زاوية الكاميرا).
   - **Video Prompt** (بالإنجليزي): وصف الحركة (حركة الكاميرا، حركة الشخصيات،
     الأصوات الطبيعية، الموسيقى/الإحساس العام).
   - ترجمة مختصرة بالعربي تحت كل Prompt لتسهيل الفهم.
4. في النهاية اكتب "ملخص الأسلوب العام" (Style Notes) يوصف:
   نوع الإضاءة المتكرر، حركة الكاميرا العامة (wide -> close-up مثلاً)،
   الألوان السائدة، ونبرة الفيديو (عاطفي/أكشن/هادئ...) عشان يفيد
   في توليد فيديوهات تانية بنفس الأسلوب.

اكتب الناتج بصيغة Markdown منظمة وواضحة بعناوين، بدون أي مقدمات زيادة."""


def get_video_duration(video_path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def extract_frames(video_path: str, out_dir: str, num_frames: int) -> list:
    duration = get_video_duration(video_path)
    if duration <= 0:
        raise ValueError("تعذر قراءة مدة الفيديو")

    frame_paths = []
    step = duration / (num_frames + 1)
    for i in range(1, num_frames + 1):
        timestamp = step * i
        out_path = os.path.join(out_dir, f"frame_{i:02d}.jpg")
        cmd = [
            "ffmpeg", "-y", "-ss", str(timestamp), "-i", video_path,
            "-frames:v", "1", "-q:v", "2", out_path,
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        if os.path.exists(out_path):
            frame_paths.append((timestamp, out_path))
    return frame_paths


def encode_image(path: str) -> str:
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


def build_content_blocks(frames: list) -> list:
    blocks = []
    for timestamp, path in frames:
        blocks.append({"type": "text", "text": f"[فريم عند الثانية {timestamp:.1f}]"})
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": encode_image(path),
            },
        })
    return blocks


def download_from_url(url: str, out_dir: str) -> str:
    """يحمل الفيديو من رابط (يوتيوب/تيكتوك/انستغرام/رابط مباشر...) باستخدام yt-dlp."""
    out_template = os.path.join(out_dir, "downloaded.%(ext)s")
    cmd = ["yt-dlp", "-f", "mp4/best", "-o", out_template, url]
    subprocess.run(cmd, capture_output=True, check=True, timeout=300)
    for f in os.listdir(out_dir):
        if f.startswith("downloaded"):
            return os.path.join(out_dir, f)
    raise RuntimeError("فشل تحميل الفيديو من الرابط")


@app.route("/")
def index():
    with open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8") as f:
        return f.read()


@app.route("/analyze", methods=["POST"])
def analyze():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY غير مضبوط على السيرفر"}), 500

    num_frames = int(request.form.get("frames", DEFAULT_FRAMES))
    num_frames = max(2, min(MAX_FRAMES, num_frames))

    tmp_dir = tempfile.mkdtemp(prefix="v2p_")
    try:
        video_path = None

        video_file = request.files.get("video")
        video_url = request.form.get("url", "").strip()

        if video_file and video_file.filename:
            ext = Path(video_file.filename).suffix or ".mp4"
            video_path = os.path.join(tmp_dir, f"upload{ext}")
            video_file.save(video_path)
        elif video_url:
            video_path = download_from_url(video_url, tmp_dir)
        else:
            return jsonify({"error": "لازم ترفع فيديو أو تحط رابط"}), 400

        frames = extract_frames(video_path, tmp_dir, num_frames)
        if not frames:
            return jsonify({"error": "فشل استخراج فريمات من الفيديو"}), 500

        client = anthropic.Anthropic(api_key=api_key)
        content_blocks = build_content_blocks(frames)
        content_blocks.append({
            "type": "text",
            "text": "حلل الفريمات دي بالترتيب وطلع لي البرومبت الكامل حسب التعليمات.",
        })

        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content_blocks}],
        )

        result_text = "".join(b.text for b in response.content if b.type == "text")
        return jsonify({"result": result_text, "frames_used": len(frames)})

    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"خطأ في معالجة الفيديو: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
