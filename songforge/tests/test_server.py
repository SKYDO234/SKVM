"""Tests for the optional web app (skipped when FastAPI is not installed).

    pip install "songforge[server]" httpx
    python -m unittest discover -s tests
"""

import io
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

try:
    from fastapi.testclient import TestClient
    HAVE_SERVER = True
except Exception:                                    # pragma: no cover
    HAVE_SERVER = False

from songforge.audio import DEFAULT_SR, write_audio  # noqa: E402
from songforge.demo import make_demo_vocal  # noqa: E402


def wav_bytes(seconds: float = 4.0, sr: int = DEFAULT_SR) -> bytes:
    vocal = make_demo_vocal(bpm=100, sr=sr, repeats=1)[: int(seconds * sr)]
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = tmp.name
    try:
        write_audio(path, vocal, sr)
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(path)


@unittest.skipUnless(HAVE_SERVER, "fastapi/httpx not installed")
class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from songforge.serve.app import ServerConfig, create_app
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = ServerConfig(instance_dir=cls.tmp.name, max_workers=1, max_seconds=6.0,
                           max_upload_mb=5.0, api_key="", rate_per_hour=50,
                           rate_per_day=100, ttl_hours=1.0)
        cls.app = create_app(cfg)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.app.state.manager.stop()
        cls.tmp.cleanup()

    def test_health_and_ui(self):
        health = self.client.get("/api/health").json()
        self.assertEqual(health["status"], "ok")
        self.assertIn("lofi", health["limits"]["styles"])
        self.assertEqual(self.client.get("/api/styles").json()["styles"][0], "auto")
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("songforge", page.text)

    def test_upload_generate_download(self):
        data = wav_bytes(4.0)
        r = self.client.post("/api/jobs",
                             files={"file": ("vocal.wav", io.BytesIO(data), "audio/wav")},
                             data={"style": "lofi", "prompt": "test"})
        self.assertEqual(r.status_code, 202, r.text)
        job = r.json()
        self.assertIn(job["status"], ("queued", "running"))

        deadline = time.time() + 180
        while time.time() < deadline:
            status = self.client.get(f"/api/jobs/{job['id']}").json()
            if status["status"] in ("done", "error"):
                break
            time.sleep(0.5)
        self.assertEqual(status["status"], "done", status.get("error"))
        self.assertTrue(status["progress"])
        self.assertIn("song", status["files"])

        audio = self.client.get(status["files"]["song"])
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.headers["content-type"], "audio/wav")
        self.assertGreater(len(audio.content), 50_000)

        report = self.client.get(status["report_url"]).json()
        self.assertEqual(report["backend"], "procedural")
        self.assertLessEqual(report["out"]["peak_db"], -0.9)

    def test_validation(self):
        r = self.client.post("/api/jobs",
                             files={"file": ("song.txt", io.BytesIO(b"x" * 4096), "text/plain")})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/jobs",
                             files={"file": ("tiny.wav", io.BytesIO(b"x" * 100), "audio/wav")})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get("/api/jobs/nope").status_code, 404)

    def test_delete(self):
        data = wav_bytes(3.0)
        job = self.client.post("/api/jobs",
                               files={"file": ("v.wav", io.BytesIO(data), "audio/wav")}).json()
        time.sleep(0.2)
        self.assertEqual(self.client.delete(f"/api/jobs/{job['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}").status_code, 404)


@unittest.skipUnless(HAVE_SERVER, "fastapi/httpx not installed")
class AuthTests(unittest.TestCase):
    def test_api_key_required_for_upload(self):
        from songforge.serve.app import ServerConfig, create_app
        with tempfile.TemporaryDirectory() as d:
            app = create_app(ServerConfig(instance_dir=d, api_key="s3cret",
                                          max_seconds=5.0, rate_per_hour=5))
            client = TestClient(app)
            self.assertTrue(client.get("/api/health").json()["limits"]["api_key_required"])
            self.assertEqual(client.get("/").status_code, 200)   # the UI itself is public
            data = wav_bytes(3.0)
            r = client.post("/api/jobs",
                            files={"file": ("v.wav", io.BytesIO(data), "audio/wav")})
            self.assertEqual(r.status_code, 401)
            r = client.post("/api/jobs",
                            files={"file": ("v.wav", io.BytesIO(data), "audio/wav")},
                            headers={"X-API-Key": "wrong"})
            self.assertEqual(r.status_code, 401)
            r = client.post("/api/jobs",
                            files={"file": ("v.wav", io.BytesIO(data), "audio/wav")},
                            headers={"X-API-Key": "s3cret"})
            self.assertEqual(r.status_code, 202, r.text)
            app.state.manager.stop()


class LimiterTests(unittest.TestCase):
    def test_rate_limiter(self):
        from songforge.serve.manager import RateLimiter
        limiter = RateLimiter(per_hour=3, per_day=5)
        for _ in range(3):
            limiter.check("1.2.3.4")
        with self.assertRaises(RuntimeError):
            limiter.check("1.2.3.4")
        limiter.check("5.6.7.8")          # other address unaffected

    def test_manager_rejects_unknown_extension(self):
        from songforge.serve.manager import JobManager
        with tempfile.TemporaryDirectory() as d:
            manager = JobManager(root=d, max_workers=1)
            try:
                with self.assertRaises(ValueError):
                    manager.submit("track.txt", b"x" * 2048)
                with self.assertRaises(ValueError):
                    manager.submit("small.wav", b"x")
                with self.assertRaises(ValueError):
                    manager.submit("big.wav", np.zeros(int(30e6), np.uint8).tobytes())
            finally:
                manager.stop()


if __name__ == "__main__":
    unittest.main()
