#!/usr/bin/env python3
"""Embedding daemon — keeps the sentence-transformer in RAM, serves /embed.

HTTP server on 127.0.0.1:8079.
POST /embed  body = raw text                       → {"vector": [384 floats], "model": "..."}
POST /batch  body = JSON {"texts": ["a","b",...]}  → {"vectors": [[...],[...]], "model": "..."}
GET  /ping                                         → {"ok":true,"model":"...","loaded_at":...}

Multilingual model (ru+en+50+), 384-d. Load once, encode in <50 ms.
Run as:
  nohup python3 bin/embed_serve.py > /tmp/embed_serve.log 2>&1 &
"""
import sys, json, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
HOST, PORT = "127.0.0.1", 8079

print(f"[embed] loading {MODEL_NAME} …", flush=True)
_t0 = time.time()
from sentence_transformers import SentenceTransformer
MODEL = SentenceTransformer(MODEL_NAME, device="cpu")
LOADED_AT = time.time()
print(f"[embed] loaded in {LOADED_AT - _t0:.1f}s, dim={MODEL.get_sentence_embedding_dimension()}",
      flush=True)

_LOCK = threading.Lock()

class H(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/ping"):
            self._send(200, json.dumps({
                "ok": True, "model": MODEL_NAME,
                "dim": MODEL.get_sentence_embedding_dimension(),
                "loaded_at": LOADED_AT,
                "uptime_s": round(time.time() - LOADED_AT, 1),
            }))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            if self.path.startswith("/batch"):
                obj = json.loads(raw.decode("utf-8"))
                texts = obj.get("texts") or []
                if not isinstance(texts, list): raise ValueError("texts must be list")
                with _LOCK:
                    vecs = MODEL.encode(texts, normalize_embeddings=True).tolist()
                self._send(200, json.dumps({"vectors": vecs, "model": MODEL_NAME}))
                return
            if self.path.startswith("/embed"):
                # body = raw text (utf-8)
                text = raw.decode("utf-8", errors="replace")
                if not text.strip():
                    self._send(400, json.dumps({"error": "empty"})); return
                with _LOCK:
                    v = MODEL.encode([text], normalize_embeddings=True)[0].tolist()
                self._send(200, json.dumps({"vector": v, "model": MODEL_NAME, "len": len(text)}))
                return
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)})); return
        self._send(404, json.dumps({"error": "not found"}))

if __name__ == "__main__":
    srv = ThreadingHTTPServer((HOST, PORT), H)
    print(f"[embed] serving http://{HOST}:{PORT}/ (POST /embed | /batch, GET /ping)", flush=True)
    srv.serve_forever()
