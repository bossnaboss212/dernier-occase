# app.py (racine)
from flask import Flask, send_from_directory

# On sert tout ce qui est dans /webapp comme "static"
app = Flask(__name__, static_folder="webapp", static_url_path="")

# Page d'accueil -> webapp/index.html
@app.get("/")
def root():
    return send_from_directory("webapp", "index.html")

# Images webapp/img/AMNESIA.JPG -> https://.../img/AMNESIA.JPG
@app.get("/img/<path:path>")
def images(path):
    return send_from_directory("webapp/img", path)

# Vidéos webapp/video/el_jefe.MP4 -> https://.../video/el_jefe.MP4
@app.get("/video/<path:path>")
def videos(path):
    return send_from_directory("webapp/video", path)

# Santé (Railway pinge parfois ce genre d’endpoint)
@app.get("/health")
def health():
    return ("ok", 200)

if __name__ == "__main__":
    # Pour tests locaux: python app.py
    app.run(host="0.0.0.0", port=8080, debug=False)
