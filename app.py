from flask import Flask, send_from_directory

app = Flask(__name__, static_folder="webapp", static_url_path="")

@app.get("/")
def root():
    return send_from_directory("webapp", "index.html")

# Servir les images
@app.get("/img/<path:path>")
def images(path):
    return send_from_directory("webapp/img", path)

# Servir les vidéos
@app.get("/video/<path:path>")
def videos(path):
    return send_from_directory("webapp/video", path)

@app.get("/health")
def health():
    return "ok", 200
