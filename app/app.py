import os
import time
import psycopg2
import requests as req_lib
from flask import Flask, render_template, request, redirect, url_for, abort, Response
from minio import Minio
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

DATABASE_URL     = os.environ.get("DATABASE_URL")
MINIO_ENDPOINT   = os.environ.get("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin123")
MINIO_BUCKET     = os.environ.get("MINIO_BUCKET", "images")

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

minio_client = Minio(
    MINIO_ENDPOINT,
    access_key=MINIO_ACCESS_KEY,
    secret_key=MINIO_SECRET_KEY,
    secure=False,
)

def ensure_bucket():
    if not minio_client.bucket_exists(MINIO_BUCKET):
        minio_client.make_bucket(MINIO_BUCKET)

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS images (
            id          SERIAL PRIMARY KEY,
            title       VARCHAR(255) NOT NULL,
            description TEXT,
            filename    VARCHAR(255) NOT NULL,
            filetype    VARCHAR(50)  NOT NULL,
            filesize    BIGINT       NOT NULL,
            object_key  VARCHAR(512) NOT NULL,
            uploaded_at TIMESTAMPTZ  DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route("/")
def index():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, title, description, filename, uploaded_at FROM images ORDER BY uploaded_at DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    images = [
        {"id": r[0], "title": r[1], "description": r[2], "filename": r[3], "uploaded_at": r[4]}
        for r in rows
    ]
    return render_template("index.html", images=images)

@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        title       = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        file        = request.files.get("image")

        if not title or not file or file.filename == "":
            return render_template("upload.html", error="Başlık ve resim dosyası zorunludur.")

        if not allowed_file(file.filename):
            return render_template("upload.html", error="Sadece resim dosyaları yüklenebilir.")

        filename   = secure_filename(file.filename)
        filetype   = filename.rsplit(".", 1)[1].lower()
        object_key = f"{int(time.time())}_{filename}"

        file_data = file.read()
        filesize  = len(file_data)

        import io
        minio_client.put_object(
            MINIO_BUCKET,
            object_key,
            io.BytesIO(file_data),
            length=filesize,
            content_type=file.content_type or "application/octet-stream",
        )

        conn = get_conn()
        cur  = conn.cursor()
        cur.execute(
            """INSERT INTO images (title, description, filename, filetype, filesize, object_key)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (title, description, filename, filetype, filesize, object_key),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        return redirect(url_for("detail", image_id=new_id))

    return render_template("upload.html")

@app.route("/image/<int:image_id>")
def detail(image_id):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT id, title, description, filename, filetype, filesize, object_key, uploaded_at FROM images WHERE id = %s", (image_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        abort(404)

    image = {
        "id": row[0], "title": row[1], "description": row[2],
        "filename": row[3], "filetype": row[4], "filesize": row[5],
        "object_key": row[6], "uploaded_at": row[7],
    }

    url = url_for("serve_image", image_id=image_id)
    return render_template("detail.html", image=image, url=url)

@app.route("/serve/<int:image_id>")
def serve_image(image_id):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT object_key, filetype FROM images WHERE id = %s", (image_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        abort(404)
    url = minio_client.presigned_get_object(MINIO_BUCKET, row[0])
    r = req_lib.get(url, stream=True)
    return Response(r.content, content_type=f"image/{row[1]}")

@app.route("/download/<int:image_id>")
def download(image_id):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT object_key, filename FROM images WHERE id = %s", (image_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        abort(404)
    url = minio_client.presigned_get_object(MINIO_BUCKET, row[0])
    r = req_lib.get(url)
    return Response(
        r.content,
        headers={"Content-Disposition": f"attachment; filename={row[1]}"},
        content_type=r.headers.get("content-type", "application/octet-stream")
    )

if __name__ == "__main__":
    for _ in range(10):
        try:
            init_db()
            ensure_bucket()
            break
        except Exception as e:
            print(f"Servisler hazır değil, bekleniyor... ({e})")
            time.sleep(3)
    app.run(host="0.0.0.0", port=5000, debug=True)