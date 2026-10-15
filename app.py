import os
import json
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    req = request.get_json() or {}
    
    # Ответ для Сбер Бума
    response = {
        "messageName": "ANSWER_TO_USER",
        "sessionId": req.get("sessionId", ""),
        "messageId": req.get("messageId", 0),
        "uuid": req.get("uuid", {}),
        "payload": {
            "pronounceText": "Привет! Облачный мост успешно работает.",
            "items": [{"bubble": {"text": "Привет! Облачный мост успешно работает."}}]
        }
    }
    return jsonify(response)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)