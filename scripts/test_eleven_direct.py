"""Hit the ElevenLabs HTTP API directly (no pipecat, no websocket) and play the
result with paplay. Isolates whether the API key + voice + model actually
return audio in this environment.

Usage: .venv/bin/python scripts/test_eleven_direct.py "hello world"
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx

from hasat.config import settings


def main() -> None:
    text = " ".join(sys.argv[1:]) or "Hello from eleven labs direct."
    assert settings.elevenlabs_api_key, "ELEVENLABS_API_KEY not set"

    voice_id = settings.elevenlabs_voice_id
    model_id = settings.elevenlabs_model
    out_path = "/tmp/hasat_eleven_direct.mp3"

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": settings.elevenlabs_api_key,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }

    print(f"[direct] POST {url} model={model_id} voice={voice_id}", flush=True)
    t0 = time.monotonic()
    with httpx.Client(timeout=30) as c:
        r = c.post(url, headers=headers, json=payload)
    print(f"[direct] {r.status_code} in {time.monotonic() - t0:.2f}s, {len(r.content)} bytes",
          flush=True)
    if r.status_code != 200:
        print(f"[direct] body: {r.text[:500]}", flush=True)
        sys.exit(1)

    with open(out_path, "wb") as f:
        f.write(r.content)
    print(f"[direct] wrote {out_path}; playing with paplay", flush=True)
    subprocess.run(["paplay", out_path], check=False)
    print(f"[direct] done. file kept at {out_path}", flush=True)


if __name__ == "__main__":
    main()
