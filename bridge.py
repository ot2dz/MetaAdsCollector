# -*- coding: utf-8 -*-
"""
Local Bridge Server for Vibe Coding
=====================================
خادم محلي بسيط مبني على Flask يعمل كجسر بين واجهة Vibe Coding ونظام الملفات
على الجهاز. يوفّر ثلاث نقاط أساسية:
    - GET  /health   : للتأكد من أن الخادم يعمل وإرجاع مجلد العمل الحالي.
    - GET  /context  : لمسح مجلد العمل وإرجاع الملفات النصية ومحتوياتها.
    - POST /write    : لكتابة/تعديل الملفات بنمطين صريحين عبر حقل mode:
                       "full" للكتابة الكاملة، و "replace" للبحث والاستبدال المباشر.

التشغيل:
    pip install -r requirements.txt
    python bridge.py
"""

import os

from flask import Flask, jsonify, request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# الإعداد الأساسي
# ---------------------------------------------------------------------------

app = Flask(__name__)

# تفعيل CORS لجميع النطاقات حتى تستطيع الواجهة (في المتصفح) الاتصال بالخادم المحلي.
CORS(app)


@app.after_request
def add_cors_headers(response):
    """
    إضافة ترويسات CORS الكاملة + ترويسة Private Network Access (PNA).

    Chrome يفرض سياسة PNA عند اتصال موقع إنترنت (aistudio.google.com) بجهاز
    محلي (127.0.0.1). إرسال Access-Control-Allow-Private-Network يسمح بذلك.
    (الإضافة تتم عبر الـ Service Worker في الإضافة، وهذه حماية إضافية للوصول المباشر.)
    """
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


@app.route("/", defaults={"path": ""}, methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def handle_preflight(path):
    """الرد على طلبات الـ Preflight (OPTIONS) فوراً بكود 200."""
    return ("", 200)

# المجلدات والملفات التي يجب تجاهلها تماماً أثناء مسح السياق.
IGNORED_NAMES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".DS_Store",
}


# ---------------------------------------------------------------------------
# دوال مساعدة
# ---------------------------------------------------------------------------

def build_safe_path(relative_path):
    """
    تحويل مسار نسبي قادم من الطلب إلى مسار مطلق آمن داخل مجلد العمل الحالي.

    يمنع الخروج عن مجلد العمل (Path Traversal) عبر التأكد من أن المسار الناتج
    يقع فعلياً داخل os.getcwd().
    """
    base_dir = os.getcwd()
    # دمج المسار النسبي مع مجلد العمل ثم تطبيعه (normalize).
    absolute_path = os.path.abspath(os.path.join(base_dir, relative_path))
    real_base = os.path.realpath(base_dir)
    real_target = os.path.realpath(absolute_path)

    # التأكد من أن المسار الهدف يبدأ بمجلد العمل (أو يساويه).
    if real_target != real_base and not real_target.startswith(real_base + os.sep):
        raise ValueError("المسار المطلوب خارج مجلد العمل المسموح به.")

    return absolute_path


def scan_context():
    """
    مسح مجلد العمل الحالي وإرجاع قائمة بكل الملفات النصية.

    - يتخطى المجلدات/الملفات المذكورة في IGNORED_NAMES.
    - يقرأ الملفات بصيغة UTF-8 فقط.
    - يتخطى أي ملف يرفع UnicodeDecodeError (أي ملفات binary/صور).
    """
    base_dir = os.getcwd()
    files = []

    for root, dirs, filenames in os.walk(base_dir):
        # تعديل قائمة المجلدات في المكان (in-place) لتجاهلها أثناء النزول.
        dirs[:] = [d for d in dirs if d not in IGNORED_NAMES]

        for filename in filenames:
            if filename in IGNORED_NAMES:
                continue

            absolute_path = os.path.join(root, filename)
            relative_path = os.path.relpath(absolute_path, base_dir)

            try:
                with open(absolute_path, "r", encoding="utf-8") as file_handle:
                    content = file_handle.read()
            except (UnicodeDecodeError, OSError):
                # تخطي الملفات الثنائية أو غير القابلة للقراءة.
                continue

            files.append({"path": relative_path, "content": content})

    return files


# ---------------------------------------------------------------------------
# نقاط النهاية (Endpoints)
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    """نقطة فحص بسيطة لتأكيد أن الخادم يعمل."""
    return jsonify({"status": "running", "cwd": os.getcwd()})


@app.route("/context", methods=["GET"])
def context():
    """إرجاع سياق المشروع: كل الملفات النصية مع مساراتها ومحتوياتها."""
    try:
        files = scan_context()
        return jsonify({"files": files})
    except Exception as exc:  # noqa: BLE001 - نريد إرجاع أي خطأ بصيغة JSON.
        return jsonify({"error": str(exc)}), 500


@app.route("/write", methods=["POST"])
def write_file():
    """
    كتابة/تعديل ملف بنمطين صريحين عبر حقل `mode`:

    1) mode = "full"  -> كتابة/استبدال الملف بالكامل.
       Body: {"mode": "full", "path": "...", "content": "..."}

    2) mode = "replace" -> بحث حرفي عن `find` واستبداله بـ `replace` مرة واحدة.
       Body: {"mode": "replace", "path": "...", "find": "...", "replace": "..."}
       في حال عدم العثور على `find` بدقة، يُرجع 404 مع رسالة واضحة.
    """
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "الطلب يجب أن يحتوي على JSON صالح."}), 400

        relative_path = data.get("path")
        if not relative_path:
            return jsonify({"error": "الحقل المطلوب: path."}), 400

        # النمط الافتراضي هو الكتابة الكاملة عند غياب mode (توافقية للخلف).
        mode = (data.get("mode") or "full").lower()

        # التحقق من المسار وبناء المسار المطلق الآمن (حماية من Path Traversal).
        try:
            absolute_path = build_safe_path(relative_path)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        # إنشاء المجلدات الأبوية عند الحاجة.
        parent_dir = os.path.dirname(absolute_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        # ------------------------- نمط الكتابة الكاملة -------------------------
        if mode == "full":
            content = data.get("content")
            if content is None:
                return (
                    jsonify({"error": "الحقل المطلوب في نمط full: content."}),
                    400,
                )

            with open(absolute_path, "w", encoding="utf-8") as file_handle:
                file_handle.write(content)

            print(f"[WRITE] Mode: FULL | File: {relative_path}")
            return jsonify(
                {
                    "status": "success",
                    "mode": "full",
                    "file": relative_path,
                }
            )

        # ------------------- نمط البحث والاستبدال المباشر -------------------
        if mode == "replace":
            find_text = data.get("find")
            replace_text = data.get("replace")

            if find_text is None or replace_text is None:
                return (
                    jsonify(
                        {
                            "error": (
                                "الحقول المطلوبة في نمط replace: find و replace."
                            )
                        }
                    ),
                    400,
                )

            if not os.path.exists(absolute_path):
                return (
                    jsonify(
                        {
                            "error": (
                                f"تعذّر تطبيق التعديل: الملف غير موجود "
                                f"({relative_path})."
                            )
                        }
                    ),
                    404,
                )

            with open(absolute_path, "r", encoding="utf-8") as file_handle:
                current_content = file_handle.read()

            # فشل التطابق الدقيق -> 404 برسالة واضحة.
            if find_text not in current_content:
                print(
                    f"[WRITE] Mode: REPLACE | File: {relative_path} | "
                    f"MATCH FAILED"
                )
                return (
                    jsonify(
                        {
                            "error": (
                                "فشل التطابق: لم يتم العثور على نص `find` "
                                "بشكل مطابق تماماً في الملف."
                            ),
                            "file": relative_path,
                            "find": find_text,
                        }
                    ),
                    404,
                )

            # استبدال حرفي مرة واحدة فقط، مع حفظ باقي الملف دون مساس.
            updated_content = current_content.replace(find_text, replace_text, 1)

            with open(absolute_path, "w", encoding="utf-8") as file_handle:
                file_handle.write(updated_content)

            print(f"[WRITE] Mode: REPLACE | File: {relative_path} | MATCH OK")
            return jsonify(
                {
                    "status": "success",
                    "mode": "replace",
                    "file": relative_path,
                }
            )

        # mode غير معروف.
        return (
            jsonify(
                {
                    "error": (
                        f"قيمة mode غير مدعومة: '{mode}'. "
                        "القيم المدعومة: full, replace."
                    )
                }
            ),
            400,
        )

    except Exception as exc:  # noqa: BLE001 - إرجاع أي استثناء بصيغة JSON.
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# نقطة الدخول
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("  Vibe Coding Local Bridge يعمل الآن على:")
    print("  http://127.0.0.1:5000")
    print(f"  Working Directory: {os.getcwd()}")
    print("=" * 60)
    # host=127.0.0.1 لضمان الوصول المحلي فقط.
    app.run(host="127.0.0.1", port=5000, debug=False)
