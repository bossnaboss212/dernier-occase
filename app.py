from flask import Flask, send_from_directory

# On dit à Flask que les fichiers statiques sont dans "webapp"
app = Flask(__name__, static_folder="webapp")

# Route principale -> sert index.html
@app.route("/")
def index():
    return send_from_directory("webapp", "index.html")

# Route pour tout autre fichier statique (CSS, JS, images, vidéos…)
@app.route("/<path:path>")
def static_files(path):
    return send_from_directory("webapp", path)

# ✅ Healthcheck (pour Railway)
@app.route("/health")
def health():
    return "ok", 200


if __name__ == "__main__":
    # Pour le debug local uniquement
    app.run(host="0.0.0.0", port=8080, debug=True)
