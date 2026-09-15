#!/usr/bin/env python3
"""Exercise the actual image using synthetic data; remove the test container."""
import argparse
import http.client
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("oracle", ROOT / "tools/contract-check/validate.py")
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)


def docker(*args, env=None, check=True):
    result = subprocess.run(["docker", *args], env=env, capture_output=True, timeout=60)
    if check and result.returncode:
        # Docker errors can include environment values; retain only fixed codes.
        raise RuntimeError("docker_command_failed:" + args[0])
    return result


class Engine(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(self.path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version",type=int,choices=[1,2],default=1)
    p.add_argument("--image", default="codex-meter-api:cm005")
    p.add_argument("--output", type=Path, default=ROOT / "artifacts/cm-005/container")
    p.add_argument("--seconds", type=int, default=60, help="seconds per idle/request phase")
    p.add_argument("--lan", default=None, help="explicitly selected Mac LAN address")
    args = p.parse_args()
    out = args.output.resolve()
    if not out.is_relative_to(ROOT / "artifacts") or out.exists() or args.seconds < 30:
        p.error("use a new repository artifacts directory and at least 30 seconds per phase")
    out.mkdir(parents=True, mode=0o700)
    data = out / "data"
    data.mkdir(mode=0o750)
    token = secrets.token_hex(32)
    env = os.environ.copy()
    env["METER_API_TOKEN"] = token
    name = "cm005-check-" + uuid.uuid4().hex[:10]
    host = args.lan or "127.0.0.1"
    created = False
    visibility_seconds = []

    def publish(raw):
        temp = data / "next"
        with temp.open("wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fchmod(stream.fileno(), 0o640)
            os.fsync(stream.fileno())
        temp.replace(data / "usage.json")
        fd = os.open(data, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def request(route=None, authorized=True):
        route=route or f"/v{args.version}/usage"
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            connection.request("GET", route, headers={"Authorization": "Bearer " + token} if authorized else {})
            response = connection.getresponse()
            raw = response.read(4097)
            if response.getheader("Cache-Control") != "no-store" or response.getheader("Content-Type") != "application/json":
                raise RuntimeError("response_headers")
            return response.status, raw
        finally:
            connection.close()

    try:
        docker("run", "--detach", "--name", name, "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--memory", "32m", "--memory-swap", "32m",
               "--cpus", "0.25", "--pids-limit", "64", "--user", f"65532:{os.getgid()}",
               "--publish", f"{host}::8080", "--mount", f"type=bind,src={data},dst=/data,readonly",
               "--env", "METER_API_TOKEN", "--env", "METER_TIMEZONE=America/Los_Angeles",
               "--env", "METER_API_BIND_ADDRESS=0.0.0.0", "--env", "GOMEMLIMIT=24MiB",
               "--env", "GOMAXPROCS=2", args.image, env=env)
        created = True
        info = json.loads(docker("inspect", name).stdout)[0]
        port = int(info["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"])
        for _ in range(30):
            try:
                if request("/healthz", False)[0] == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("container_startup")
        status, raw = request(authorized=False)
        if status != 401:
            raise RuntimeError("authentication")
        oracle.validate(raw, "error",version=args.version)
        status, raw = request()
        if status != 200 or oracle.validate(raw,version=args.version)["observed_at"] is not None:
            raise RuntimeError("missing_snapshot")
        cases = json.loads((ROOT / f"contracts/v{args.version}/fixtures/manifest.json").read_text())
        def verify_publication(raw):
            publish(raw)
            start = time.monotonic()
            try:
                source = oracle.validate_snapshot(raw,version=args.version)
                expected_error = None
            except oracle.Invalid as error:
                source = None
                expected_error = {"version": "snapshot_version", "oversized": "snapshot_oversized"}.get(error.code, "snapshot_invalid")
            # Desktop's host/VM file-sharing metadata may lag the rename. Bound
            # and record visibility, while still rejecting every malformed body.
            while True:
                status, received = request()
                if status == 200:
                    result = oracle.validate(received,version=args.version)
                    match = source is not None and result == oracle.project(source, result["as_of"])
                elif status == 503:
                    result = oracle.validate(received, "error",version=args.version)
                    match = expected_error is not None and result["error"] == expected_error
                else:
                    raise RuntimeError("unexpected_snapshot_status")
                elapsed = time.monotonic() - start
                if match:
                    visibility_seconds.append(elapsed)
                    return
                if elapsed >= 3:
                    raise RuntimeError("snapshot_not_visible_within_three_seconds")
                time.sleep(0.05)

        tested = 0
        for case in cases:
            if case.get("kind","usage") != "usage":
                continue
            raw = (ROOT / f"contracts/v{args.version}/fixtures" / case["file"]).read_bytes()
            verify_publication(raw)
            if request("/healthz", False) != (200, b'{"status":"ok","version":1}'):
                raise RuntimeError("health_depends_on_source")
            tested += 1
        normal = (ROOT / f"contracts/v{args.version}/fixtures/normal.json").read_bytes()
        for i in range(20):
            verify_publication(normal if i % 2 == 0 else (ROOT / f"contracts/v{args.version}/fixtures/zero-remaining.json").read_bytes())
        # Verify an attempted write against this container's actual mount fails.
        attempt = out / "write-probe"
        attempt.write_text("synthetic write probe")
        result = docker("cp", str(attempt), name + ":/data/write-probe", check=False)
        if not result.returncode or b"read-only" not in result.stderr.lower() or (data / "write-probe").exists():
            raise RuntimeError("read_only_write_probe")
        mounts = info["Mounts"]
        if len(mounts) != 1 or mounts[0]["Destination"] != "/data" or mounts[0]["RW"]:
            raise RuntimeError("mount_isolation")
        if not info["HostConfig"]["ReadonlyRootfs"] or info["Config"]["User"] != f"65532:{os.getgid()}":
            raise RuntimeError("runtime_isolation")
        allowed = {"PATH", "METER_API_TOKEN", "METER_TIMEZONE", "METER_API_BIND_ADDRESS", "GOMEMLIMIT", "GOMAXPROCS"}
        if any(entry.partition("=")[0] not in allowed for entry in info["Config"]["Env"]):
            raise RuntimeError("environment_isolation")
        if docker("logs", name).stdout or docker("logs", name).stderr:
            raise RuntimeError("unexpected_runtime_logging")
        image = json.loads(docker("image", "inspect", args.image).stdout)[0]
        if any("METER_API_TOKEN=" in entry for entry in image["Config"].get("Env") or []):
            raise RuntimeError("baked_token")
        history = docker("history", "--no-trunc", args.image).stdout
        if token.encode() in history:
            raise RuntimeError("baked_secret")
        publish(normal)
        context = json.loads(docker("context", "inspect").stdout)[0]
        endpoint = context["Endpoints"]["docker"]["Host"]
        if not endpoint.startswith("unix://"):
            raise RuntimeError("measurement_requires_local_unix_docker")
        engine = Engine(endpoint[7:])

        def stats():
            engine.request("GET", f"/containers/{name}/stats?stream=false&one-shot=true")
            response = engine.getresponse()
            value = json.loads(response.read())
            if response.status != 200:
                raise RuntimeError("docker_stats")
            # docker top executes ps in the VM; the sole application process's
            # RSS includes resident file-backed pages omitted by cgroup anon.
            rows = docker("top", name, "-o", "pid,rss,args").stdout.decode().splitlines()[1:]
            if len(rows) != 1 or "/meter-api" not in rows[0]:
                raise RuntimeError("unexpected_process_tree")
            return {"cpu_ns": value["cpu_stats"]["cpu_usage"]["total_usage"],
                    "rss_bytes": int(rows[0].split()[1]) * 1024,
                    "cgroup_memory_bytes": value["memory_stats"]["usage"]}

        time.sleep(5)
        phases = {}
        for phase in ("idle", "requests"):
            start = time.monotonic()
            before = stats()
            samples = [before]
            count = 0
            next_request = start
            next_sample = start + 5
            while time.monotonic() - start < args.seconds:
                now = time.monotonic()
                if phase == "requests" and now >= next_request:
                    status, raw = request()
                    if status != 200:
                        raise RuntimeError("measurement_http")
                    oracle.validate(raw,version=args.version)
                    count += 1
                    next_request += 1  # 60 times the default device traffic.
                if now >= next_sample:
                    samples.append(stats())
                    next_sample += 5
                time.sleep(0.05)
            after = stats()
            elapsed = time.monotonic() - start
            samples.append(after)
            phases[phase] = {"elapsed_seconds": elapsed, "requests": count,
                             "cpu_percent_one_core": (after["cpu_ns"] - before["cpu_ns"]) / 1e9 / elapsed * 100,
                             "max_rss_bytes": max(s["rss_bytes"] for s in samples),
                             "max_cgroup_memory_bytes": max(s["cgroup_memory_bytes"] for s in samples),
                             "samples": len(samples)}
            print(json.dumps({"phase_complete": phase, **phases[phase]}), flush=True)
        engine.close()
        docker("restart", name)
        restarted = json.loads(docker("inspect", name).stdout)[0]
        port = int(restarted["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"])
        for _ in range(30):
            try:
                if request()[0] == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("restart_recovery")
        docker("stop", "--time", "6", name)
        stopped = json.loads(docker("inspect", name).stdout)[0]
        if stopped["State"]["ExitCode"] != 0 or stopped["State"]["OOMKilled"]:
            raise RuntimeError("shutdown_or_oom")
        report = {"image_id": image["Id"], "image_bytes": image["Size"],
                  "image_layers": len(image["RootFS"]["Layers"]), "fixtures": tested,
                  "atomic_replacements": 20, "max_publication_visibility_seconds": max(visibility_seconds), "read_only_write_refused": True,
                  "credential_mounts": 0, "runtime_processes": 1, "graceful_shutdown": True,
                  "network_test": "selected Mac LAN interface" if args.lan else "loopback",
                  "phases": phases,
                  "measurement_scope": "API process in Docker VM; Docker VM overhead and combined 15-minute/device acceptance belong to CM-010"}
        (out / "measurement.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"container_gates": "passed", "image_bytes": image["Size"], "fixtures": tested}), flush=True)
    finally:
        if created:
            docker("rm", "--force", name, check=False)


if __name__ == "__main__":
    main()
