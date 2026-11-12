# Lu automatiquement par gunicorn (la commande de demarrage Render n'a pas d'options).
# Les PDF officiels (ERRIAL, extrait cadastral) peuvent prendre plus de 30 s depuis Render.
timeout = 300
graceful_timeout = 30
