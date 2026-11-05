import os, sqlite3, subprocess, sys, tempfile, time, hashlib, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
class T(unittest.TestCase):
    def test_singleton(self):
        d = tempfile.mkdtemp(); env = dict(os.environ, COLLECTOR_LOCK=f"{d}/l", COLLECTOR_DB=f"{d}/db")
        import fcntl; f = open(f"{d}/l", "w"); fcntl.flock(f, fcntl.LOCK_EX)
        out = subprocess.run([sys.executable, f"{HERE}/collector.py", "loop"], env=env, capture_output=True, text=True, timeout=20)
        self.assertIn("already running", out.stdout)
    def test_readonly_and_no_trade_code(self):
        tdb = os.path.join(HERE, "..", "btc_hourly.db")
        h = hashlib.sha256(open(tdb, "rb").read()).hexdigest() if os.path.exists(tdb) else None
        sys.path.insert(0, HERE); import collector
        fake = lambda u: (_ for _ in ()).throw(RuntimeError("offline"))
        r = collector.collect(fake); d = tempfile.mkdtemp(); collector.save(r, f"{d}/x.db")
        self.assertEqual(sqlite3.connect(f"{d}/x.db").execute("select count(*) from market_snapshots").fetchone()[0], 1)
        if h: self.assertEqual(h, hashlib.sha256(open(tdb, "rb").read()).hexdigest())
        src = open(f"{HERE}/collector.py").read().lower()
        for bad in ("btc_hourly.db", "broker", "run.py", "openai", "anthropic", "/orders", "positions"):
            self.assertNotIn(bad, src.replace("never touches btc_hourly.db", ""))
        self.assertFalse(collector.LIVE_TRADING)
if __name__ == "__main__": unittest.main()
