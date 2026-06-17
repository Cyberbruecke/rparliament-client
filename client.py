import json
import os
import shutil
import subprocess
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from hashlib import sha256
from http.server import HTTPServer, BaseHTTPRequestHandler
from json import JSONDecodeError
from pathlib import Path
from urllib.request import urlopen
from time import time, sleep
from typing import List, Tuple

from dns.exception import DNSException
from dns.resolver import resolve
from prometheus_client import Gauge, Counter, generate_latest, CONTENT_TYPE_LATEST

INTERVAL = int(os.environ.get("INTERVAL", 120))
RTR_DOMAIN = os.environ["RTR_DOMAIN"]
VALIDATE = bool(int(os.environ.get("VALIDATE", 1)))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", 32))
RTRDUMP_TIMEOUT = int(os.environ.get("RTRDUMP_TIMEOUT", 20))
FRESHNESS_WINDOW = int(os.environ.get("FRESHNESS_WINDOW", 900))
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("MAX_CONSECUTIVE_FAILURES", 5))
METRICS_PORT = int(os.environ.get("METRICS_PORT", 9282))
DATA_DIR = Path("/app/data")

# prometheus metrics
PROM_TARGETS_TOTAL = Gauge("rparliament_targets_total", "Number of targets discovered via DNS")
PROM_FETCH_SUCCESS = Gauge("rparliament_fetch_success", "Targets that returned VRPs in last cycle")
PROM_FETCH_FAIL = Gauge("rparliament_fetch_fail", "Targets that failed in last cycle")
PROM_FETCH_TIMEOUT = Counter("rparliament_fetch_timeout_total", "Total rtrdump timeouts")
PROM_FETCH_SUCCESS_RATIO = Gauge("rparliament_fetch_success_ratio", "Fraction of targets successfully fetched in last cycle")
PROM_CONSENSUS_RATIO = Gauge("rparliament_consensus_ratio", "Fraction of targets agreeing on selected output")
PROM_DISTINCT_HASHES = Gauge("rparliament_distinct_hashes", "Number of distinct VRP hashes seen")
PROM_VRPS = Gauge("rparliament_vrps", "Number of VRPs in the selected output")
PROM_LAST_UPDATE = Gauge("rparliament_last_update_timestamp", "Unix timestamp of last successful output update")
PROM_CONSECUTIVE_FAILURES = Gauge("rparliament_consecutive_failures", "Current consecutive main loop failure count")

STAYRTR_METRICS_URL = "http://localhost:9847/metrics"


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics/client":
            data = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/metrics/stayrtr":
            try:
                with urlopen(STAYRTR_METRICS_URL, timeout=5) as resp:
                    data = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", CONTENT_TYPE_LATEST)
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_error(502, "stayrtr metrics unavailable")
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass  # suppress per-request logs


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log(f"RParliament Client Aggregator ({rtrdump_version()})")
    server = HTTPServer(("", METRICS_PORT), MetricsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"prometheus metrics on :{METRICS_PORT}/metrics/client and :{METRICS_PORT}/metrics/stayrtr")

    consecutive_failures = 0
    try:
        target_metrics = {}
        while True:
            try:
                targets = get_targets(RTR_DOMAIN)
                PROM_TARGETS_TOTAL.set(len(targets))

                log(f"fetching {len(targets)} targets...")
                with ThreadPoolExecutor(max_workers=max(1, min(len(targets), MAX_WORKERS))) as pool:
                    procs = list(pool.map(run_rtrdump, targets))

                fetch_time = time()
                fetch_ok = 0
                fetch_fail = 0

                # evict targets no longer in DNS
                stale_targets = set(target_metrics.keys()) - set(targets)
                for t in stale_targets:
                    del target_metrics[t]
                    log(f"evicted {t} (no longer in DNS)")

                # evict cached files older than the freshness window
                evict_stale_files(fetch_time)

                for target, proc in procs:
                    target_metrics.setdefault(target, {})

                    if proc.returncode != 0:
                        log(proc.stderr, src="rtrdump")
                        cached = True
                        fetch_fail += 1
                    else:
                        target_metrics[target]["time"] = fetch_time
                        cached = False
                        fetch_ok += 1

                    try:
                        target_metrics[target].update(get_metrics(target))
                        log(f"{target}: {target_metrics[target]['hash']} ({target_metrics[target]['n_vrps']} VRPs){' [CACHED]' if cached else ''}")

                    except FileNotFoundError:
                        log(f"{target}: no output (file not found)")
                    except JSONDecodeError as e:
                        log(f"{target}: invalid JSON - {e.__class__} - {e}")

                PROM_FETCH_SUCCESS.set(fetch_ok)
                PROM_FETCH_FAIL.set(fetch_fail)
                PROM_FETCH_SUCCESS_RATIO.set(fetch_ok / len(procs) if procs else 0)

                hash_count = {}
                for output in target_metrics.values():
                    if len(output.get("hash", "")) > 0:
                        hash_count[output["hash"]] = hash_count.get(output["hash"], 0) + 1

                PROM_DISTINCT_HASHES.set(len(hash_count))

                try:
                    best_target, metrics = sorted(target_metrics.items(), key=lambda x: (hash_count[x[1]["hash"]], x[1]["n_vrps"], x[1]["time"]), reverse=True)[0]
                    best_hash = metrics["hash"]
                    shutil.copyfile(DATA_DIR / f"{best_target}.json", DATA_DIR / "output.new.json")
                    os.rename(DATA_DIR / "output.new.json", DATA_DIR / "output.json")
                    log(f" updated output.json to {best_hash} ({hash_count[best_hash]}/{sum(hash_count.values())} support)")

                    total_votes = sum(hash_count.values())
                    PROM_CONSENSUS_RATIO.set(hash_count[best_hash] / total_votes if total_votes > 0 else 0)
                    PROM_VRPS.set(metrics["n_vrps"])
                    PROM_LAST_UPDATE.set(time())

                except (IndexError, KeyError):
                    log("no outputs")

                consecutive_failures = 0
                PROM_CONSECUTIVE_FAILURES.set(0)

            except Exception:
                consecutive_failures += 1
                PROM_CONSECUTIVE_FAILURES.set(consecutive_failures)
                log(f"unhandled exception ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}):\n{traceback.format_exc()}")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log(f"reached {MAX_CONSECUTIVE_FAILURES} consecutive failures, exiting")
                    raise

            sleep(INTERVAL)

    except KeyboardInterrupt:
        log("canceled")


def rtrdump_version() -> str:
    return subprocess.check_output(["/app/rtrdump", "-version"], text=True).strip()


def run_rtrdump(target: str) -> Tuple[str, subprocess.CompletedProcess]:
    tmp_path = DATA_DIR / f"{target}.tmp.json"
    final_path = DATA_DIR / f"{target}.json"
    try:
        proc = subprocess.run(
            ["/app/rtrdump", "-connect", f"{target}:8282", "-type", "tls", f"-tls.validate={str(VALIDATE).lower()}", "-file", str(tmp_path)],
                  stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE,
                  text=True,
                  check=False,
                  timeout=RTRDUMP_TIMEOUT)
    except subprocess.TimeoutExpired:
        PROM_FETCH_TIMEOUT.inc()
        tmp_path.unlink(missing_ok=True)
        raise
    if proc.returncode == 0:
        os.rename(tmp_path, final_path)
    else:
        tmp_path.unlink(missing_ok=True)
    return target, proc


def evict_stale_files(now: float) -> None:
    for f in DATA_DIR.glob("*.json"):
        if f.name in ("output.json", "output.new.json"):
            continue
        if f.name.endswith(".tmp.json"):
            continue
        age = now - f.stat().st_mtime
        if age > FRESHNESS_WINDOW:
            f.unlink()
            log(f"evicted stale file {f.name} (age: {int(age)}s)")


def get_targets(domain: str) -> List[str]:
    try:
        targets = [str(rr).strip('"') for rr in resolve(domain, "TXT").rrset]
        log(f"updated targets for {domain} ({len(targets)} found)")
        return targets

    except DNSException as e:
        log(f"failed to resolve targets for {domain}: {e.__class__} - {e}")
        return []


def recursive_sort(d: dict | list) -> dict | list:
    if isinstance(d, dict):
        return {k: recursive_sort(d[k]) for k in sorted(d.keys())}
    if isinstance(d, list):
        return sorted([recursive_sort(v) for v in d], key=lambda x: json.dumps(x, sort_keys=True))
    return d


def get_metrics(target: str) -> dict:
    with open(DATA_DIR / f"{target}.json") as f:
        data = recursive_sort(json.load(f))
    del data["metadata"]
    return {"hash": sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest(),
            "n_vrps": len(data["roas"])}


def log(msg: str, src: str = __file__) -> None:
    msg = msg.replace("\n", "\\n")
    print(f"{datetime.now().isoformat()}\t{src}\t{msg}", flush=True)


if __name__ == '__main__':
    main()
