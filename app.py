# app.py  (à la racine)
from flask import Flask, send_from_directory

app = Flask(__name__, static_folder="webapp", static_url_path="")

@app.get("/")
def root():
    return send_from_directory("webapp", "index.html")

# servir les images/vidéos dans webapp/img
@app.get("/img/<path:path>")
def images(path):
    return send_from_directory("webapp/img", path)

@app.get("/health")
def health():
    return "ok", 200
